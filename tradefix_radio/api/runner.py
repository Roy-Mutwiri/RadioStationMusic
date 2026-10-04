"""The single-process development runner: a real station, with an API attached.

§72's development mode — "api + worker + playout in one loop" — made concrete. The station is
built exactly as the soak and the gates build it, started exactly the same way, and then an
API is pointed at it. Nothing about the runtime changes because a browser is watching.

That is the whole design argument for this module existing. The alternative — an API process
that reads the database and infers what is playing — would have to *guess* the audio position,
and would be wrong by up to a block at all times. Sharing a process means the dashboard reads
the same `PlayoutEngine` the sink is being fed from, so "4:12 elapsed" is the engine's own
frame count rather than a reconstruction.

Production (ADR-08) splits into three processes, and that split is Phase 9's to make properly.
What this runner must not do is pretend it has already happened.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

import structlog
import uvicorn

from tradefix_radio.api.app import create_app
from tradefix_radio.api.capabilities import detect_capabilities, gpu_is_present
from tradefix_radio.api.snapshot import RuntimeView
from tradefix_radio.audio.factory import build_sink, describe_sink
from tradefix_radio.config.schema import AppSettings, RunMode
from tradefix_radio.contracts.enums import MarketRegime, StartupMode
from tradefix_radio.core.clock import UTC, SystemClock
from tradefix_radio.director.library import load_content_library
from tradefix_radio.director.music_director import MusicDirector
from tradefix_radio.director.selection import WeightedSelector
from tradefix_radio.generation.factory import build_provider
from tradefix_radio.generation.manager import DatabaseJobUnitOfWork, GenerationManager
from tradefix_radio.market.active import ActiveMarketService
from tradefix_radio.market.feeds.simulated import SimulatedFeed
from tradefix_radio.market.service import MarketDataService
from tradefix_radio.market.simulation import Scenario
from tradefix_radio.persistence.database import Database
from tradefix_radio.postprocess.pipeline import PostProductionPipeline
from tradefix_radio.radio.emergency import EmergencyManager, reserve_from_directory
from tradefix_radio.radio.station import RadioStation
from tradefix_radio.radio.station_ids import StationIdLibrary, default_library
from tradefix_radio.runtime.coordinator import RuntimeCoordinator

_log = structlog.get_logger(__name__)

#: The shipped content library (genres, topics, personas), beside the package's settings.
CONFIG_DIR: Final = Path(__file__).resolve().parents[1] / "config"

__all__ = ["ControlCenterRunner", "serve"]

#: How often the feed is polled, in seconds.
#:
#: One second, because one poll is one *tick* and the simulator builds a bar from
#: ``ticks_per_bar`` of them — 60 at the defaults. At one tick per second that is a
#: one-minute bar every minute, which is what the model represents.
#:
#: It was five seconds, matching the station's scheduling interval, and that reasoning was
#: wrong in a way that took a running station to see: a bar then took five minutes, the
#: regime engine had nothing to classify, the scheduler had no market to plan against, and
#: the dashboard sat on an empty Tier 3 broadcast looking broken.
MARKET_POLL_SECONDS: Final = 1.0

#: Polls allowed while warming the feed at startup.
#:
#: Generous — the regime engine needs ``warmup_bars * ticks_per_bar`` ticks, thousands at the
#: defaults — but bounded, so a misconfigured feed warns rather than hangs.
MAX_WARMUP_POLLS: Final = 20_000

#: Seconds between energy-timeline samples.
#:
#: Five, which fills the view's 2 880-sample buffer with exactly four hours — the longest
#: window the timeline offers.
TIMELINE_SAMPLE_SECONDS: Final = 5

#: Plausible starting prices for the simulated feeds, by symbol.
#:
#: The *level* is irrelevant to the music — §6 forbids musical decisions depending on gold's
#: absolute price, and the same holds for Bitcoin. It matters only because the Market page
#: shows a price, and a four-thousand-dollar Bitcoin would read as a broken feed rather than
#: as a simulation. A symbol with no entry here falls back to the feed's own default.
_SIMULATED_START_PRICES: Final = {"XAUUSD": 4_000.0, "BTCUSD": 103_000.0}

#: Scenario assigned to a fallback symbol when the primary's scenario is the default.
#:
#: The two feeds must not be the same price process under different names, or the "no
#: regime-state contamination" property would be untestable in a running station — both
#: symbols would agree by construction. When the operator has *chosen* a scenario it is
#: applied to every symbol, because then the choice is the point.
_FALLBACK_SCENARIO: Final = Scenario.VOLATILITY_SPIKE

#: Run modes in which scenario controls are offered (§72).
_SIMULATION_MODES: Final = frozenset({RunMode.DEVELOPMENT, RunMode.SIMULATION})


def _station_energy(station: RadioStation) -> float | None:
    """The energy the station has committed to, for the timeline's second series.

    Read from what is playing or queued rather than from the director's internal planner:
    the planner's state is its own business, and what an operator wants to see is the energy
    the station actually chose. Scaled to 0–100 to match the market's.

    It is deliberately the energy *on air*, which trails the market by the depth of the
    buffer. That lag is the station scheduling ahead rather than a delay in reacting, and the
    chart names it so nobody reads a flat line as the director ignoring the market.
    """
    snapshot = station.queue.snapshot()
    entry = snapshot.playing or (snapshot.entries[0] if snapshot.entries else None)
    if entry is None:
        return None
    return entry.blueprint.composition.energy * 100.0


def _all_classified(market: ActiveMarketService) -> bool:
    """True once every configured symbol has a state the regime engine will stand behind."""
    return all(
        (state := service.current_state) is not None
        and state.regime is not MarketRegime.UNKNOWN
        for service in market.services.values()
    )


class CatchUpClock:
    """A clock that starts in the past and is wound forward to the present.

    The problem it solves is specific to running a *simulated* feed on a *real* clock. The
    feature engine aggregates ticks into bars by wall-clock bucket (``timestamp //
    bar_seconds``) and the regime engine needs ``warmup_bars`` of them before it will classify
    anything — thirty one-minute bars at the defaults. On a system clock that is thirty real
    minutes during which the station has no regime, the director composes from a neutral
    reading, and the dashboard shows ``UNKNOWN`` at 0 % confidence while appearing broken.

    Rather than fake a regime or shorten the market's own definition of a bar, this starts the
    feed's clock half an hour *behind* and lets the warm-up wind it forward quickly. The bars
    that result are real bars of the simulator's real price process; they simply arrive faster
    than wall time. When the offset reaches zero the clock *is* the system clock, so every
    timestamp from then on is genuine and nothing downstream can tell it was ever otherwise.

    Only the market feed and service use this. The station, the generator and the playout
    engine all run on :class:`SystemClock`.
    """

    __slots__ = ("_offset",)

    def __init__(self, behind_seconds: float) -> None:
        if behind_seconds < 0:
            raise ValueError("behind_seconds must not be negative")
        self._offset = -float(behind_seconds)

    @property
    def caught_up(self) -> bool:
        return self._offset >= 0.0

    @property
    def behind_seconds(self) -> float:
        return max(0.0, -self._offset)

    def advance(self, seconds: float) -> None:
        """Wind forward, never past the present."""
        self._offset = min(0.0, self._offset + max(0.0, seconds))

    def now(self) -> datetime:
        return datetime.now(tz=UTC) + timedelta(seconds=self._offset)

    def monotonic(self) -> float:
        return time.monotonic() + self._offset

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)

    def hold(self) -> AbstractContextManager[None]:
        """No-op: this is wall-clock time with an offset, and cannot be held."""
        return contextlib.nullcontext()


class ControlCenterRunner:
    """Owns the station, the market feed and the API for one process."""

    def __init__(
        self,
        settings: AppSettings,
        *,
        scenario: Scenario = Scenario.RANDOM_WALK,
        seed: int = 2026,
        startup_mode: StartupMode = StartupMode.CONTROLLED_START,
    ) -> None:
        self._settings = settings
        self._scenario = scenario
        self._seed = seed
        self._startup_mode = startup_mode
        self._clock = SystemClock()
        self._database: Database | None = None
        self._station: RadioStation | None = None
        self._market: ActiveMarketService | None = None
        self._feed_clock: CatchUpClock | None = None
        self._market_task: asyncio.Task[None] | None = None
        self._view: RuntimeView | None = None

    @property
    def view(self) -> RuntimeView:
        if self._view is None:  # pragma: no cover - programming error, not a runtime state
            raise RuntimeError("the runner has not been started")
        return self._view

    async def start(self) -> RuntimeView:
        """Build and start everything, returning the view the API reads."""
        settings = self._settings
        settings.paths.data_dir.mkdir(parents=True, exist_ok=True)
        settings.paths.generated_dir.mkdir(parents=True, exist_ok=True)

        database = Database(settings.database, clock=self._clock)
        await database.connect()
        await database.create_all()
        self._database = database

        # The feed runs on a clock that starts behind and catches up — see ``CatchUpClock``.
        # Everything else runs on the system clock.
        warmup_seconds = settings.market.bar_seconds * (settings.market.warmup_bars + 4)
        feed_clock = CatchUpClock(warmup_seconds)
        self._feed_clock = feed_clock

        coordinator = RuntimeCoordinator(clock=self._clock)

        # One market service per configured symbol, behind the router. The station still
        # consumes a single "current state"; which symbol produced it is the router's
        # business, not the scheduler's.
        market = ActiveMarketService(
            settings,
            self._build_market_services(settings, feed_clock),
            coordinator=coordinator,
            clock=self._clock,
        )
        await market.start()
        self._market = market
        await self._warm_feed(market, feed_clock, settings.market.bar_seconds)

        director = MusicDirector(
            settings,
            load_content_library(config_dir=CONFIG_DIR),
            selector=WeightedSelector(random.Random(self._seed)),  # noqa: S311 - creative
        )
        # The factory, not a direct construction: §7 requires ACE-Step to be one
        # implementation of the interface rather than a branch at every call site.
        provider = build_provider(settings, clock=self._clock, seed=self._seed)
        generation = GenerationManager(
            provider=provider,
            settings=settings.generation,
            unit_of_work=DatabaseJobUnitOfWork(database.session),
            clock=self._clock,
        )
        # The output device, resolved before anything else starts: a misconfigured sink
        # should fail at launch rather than after the first track has been generated.
        sink = build_sink(settings, clock=self._clock, realtime=True)
        self._sink = sink

        # Post-production (§6). Constructed before the station because the station holds it
        # for the life of the run, and because `warm_analysis` pays librosa's one-off numba
        # JIT cost here rather than on the first track the station generates — which is
        # exactly when the buffer is emptiest.
        post_production = PostProductionPipeline(
            settings,
            clock=self._clock,
            master_dir=settings.paths.generated_dir / "mastered",
        )

        station = RadioStation(
            settings,
            database=database,
            coordinator=coordinator,
            director=director,
            generation=generation,
            # Built from configuration, not hardcoded. `audio.sink: null_sink` is still the
            # development default — §72 keeps audio output out of development mode and the
            # whole runtime is exercised identically either way — but `sounddevice` now
            # actually reaches a device. Before this the setting was read by `doctor` and by
            # nothing that played audio.
            sink=sink,
            station_ids=StationIdLibrary(
                default_library(settings.paths.root_dir / "station_ids"),
                repeat_horizon=settings.radio.station_id_repeat_horizon,
                rng=random.Random(self._seed),  # noqa: S311 - creative choice
            ),
            emergency=EmergencyManager(
                reserve=reserve_from_directory(settings.paths.emergency_dir)
            ),
            clock=self._clock,
            audio_dir=settings.paths.generated_dir,
            playout_block_seconds=1.0,
            post_production=post_production,
            startup_mode=self._startup_mode,
        )
        # Pay the cold start here rather than on the first scheduled track. §7.6 keeps the
        # model loaded across tracks; this is where the first load happens, alongside the
        # market warm-up, so a 60-second checkpoint load does not land on an empty buffer.
        loader = getattr(provider, "load", None)
        if loader is not None:
            try:
                await loader()
            except Exception as error:  # noqa: BLE001 - a cold model must not stop start-up
                _log.warning(
                    "control_center.provider_load_failed",
                    error_type=type(error).__name__,
                    error=str(error),
                    detail=(
                        "the station will start and retry on the first generation; "
                        "run `tradefix doctor` to check the ACE-Step service"
                    ),
                )

        await station.start()
        self._station = station

        # Hand over the warmed state before the first loop tick, so the scheduler has a
        # market to plan against from the station's very first cycle.
        warmed = market.current_state
        if warmed is not None:
            station.set_market(warmed)

        self._market_task = asyncio.create_task(self._market_loop(), name="api-market")

        self._view = RuntimeView(
            settings=settings,
            station=station,
            market_service=market,
            routing=market,
            generation=generation,
            database=database,
            started_at=datetime.now(tz=UTC),
            capabilities=detect_capabilities(
                has_station=True,
                has_market_feed=True,
                has_generation=True,
                simulation_allowed=settings.mode in _SIMULATION_MODES,
                gpu_present=gpu_is_present(),
                provider_name=settings.generation.provider,
                has_post_production=True,
            ),
        )
        _log.info(
            "control_center.started",
            mode=settings.mode.value,
            scenario=self._scenario.value,
            sink=describe_sink(sink),
        )
        return self._view

    async def stop(self) -> None:
        """Shut down in the order that loses least, mirroring §74."""
        if self._market_task is not None:
            self._market_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._market_task
            self._market_task = None
        if self._station is not None:
            await self._station.stop()
            self._station = None
        if self._market is not None:
            await self._market.stop()
            self._market = None
        if self._database is not None:
            await self._database.disconnect()
            self._database = None
        _log.info("control_center.stopped")

    def _build_market_services(
        self, settings: AppSettings, feed_clock: CatchUpClock
    ) -> dict[str, MarketDataService]:
        """A warmed, independent feed and engine stack per configured symbol.

        Every symbol gets its own seed and — unless the operator chose a scenario — its own
        price process, so the fallback is a genuinely different market rather than gold
        under another ticker. They share the catch-up clock because they must warm together:
        a fallback that is still cold when the primary closes would hand the director an
        UNKNOWN regime at exactly the moment it is needed.
        """
        services: dict[str, MarketDataService] = {}
        for index, symbol in enumerate(settings.markets.symbols_in_order):
            scenario = self._scenario
            if index > 0 and scenario is Scenario.RANDOM_WALK:
                scenario = _FALLBACK_SCENARIO
            feed = SimulatedFeed(
                symbol=symbol,
                scenario=scenario,
                # Offset per symbol: the same seed would make two feeds that differ only in
                # their label, and every "the two markets read differently" assertion —
                # in tests and on the dashboard alike — would be vacuous.
                seed=self._seed + index * 1_013,
                start_price=_SIMULATED_START_PRICES.get(symbol, 4_000.0),
                clock=feed_clock,
            )
            services[symbol] = MarketDataService(settings, feed, clock=feed_clock)
        return services

    async def _warm_feed(
        self, market: ActiveMarketService, clock: CatchUpClock, bar_seconds: int
    ) -> None:
        """Wind the feed's clock to the present, polling as it goes.

        The simulator builds a bar from ``ticks_per_bar`` ticks and the regime engine wants
        several bars before it will classify anything, so at the normal five-second cadence
        the first state is minutes away. For those minutes the station has no market to
        compose from: the scheduler plans nothing, the queue stays empty, and the dashboard
        opens on an empty Tier 3 broadcast that looks broken and is not.

        So the feed is wound forward at startup, as fast as it will go, before the station is
        built. Bounded, so a feed that never produces a state logs a warning instead of
        hanging the command.
        """
        started = time.monotonic()
        # Ten ticks per bar: enough for a meaningful open/high/low/close, few enough that
        # thirty-odd bars cost a few hundred cheap polls.
        step = max(1.0, bar_seconds / 10.0)
        for _ in range(MAX_WARMUP_POLLS):
            if not clock.caught_up:
                clock.advance(step)
            await market.poll_once()
            state = market.current_state
            # Warmed means *classified*, not merely present. The engine's first states are
            # UNKNOWN until it has enough bars to judge, and a station that opens on an
            # unknown regime composes from a neutral reading of a market that is in fact
            # doing something — for the twenty minutes it takes to accumulate bars at one
            # per minute.
            # *Every* symbol, not just the active one. The fallback has to be warm before
            # it is needed: a switch that handed the director an UNKNOWN regime would make
            # the station's first minutes on Bitcoin its least market-aware, which is the
            # opposite of the point.
            if clock.caught_up and state is not None and _all_classified(market):
                _log.info(
                    "control_center.feed_warmed",
                    seconds=round(time.monotonic() - started, 2),
                    active_symbol=market.active_symbol,
                    regime=state.regime.value,
                    energy=round(state.energy, 1),
                    bars=market.bars_processed,
                    symbols=sorted(market.services),
                )
                return
            # Yield so the loop stays responsive; the poll itself does no I/O worth awaiting.
            await asyncio.sleep(0)
        _log.warning(
            "control_center.feed_never_warmed",
            polls=MAX_WARMUP_POLLS,
            detail="the station will broadcast on emergency tiers until a state arrives",
        )

    async def _market_loop(self) -> None:
        """Poll the feed and hand each state to the station.

        Its own task, not the API's and not the station's. The station takes market state as
        an input and never reaches for it — that is what keeps the director testable — so
        something has to do the handing over, and a process that owns both is the right place.
        """
        samples = 0
        while True:
            try:
                await self._market.poll_once()  # type: ignore[union-attr]
                # Hand over ``current_state`` rather than the poll's return value: a poll
                # that does not complete a bar returns ``None``, and the station still wants
                # the latest state it has — the alternative is a station whose market view
                # only updates once a minute.
                state = self._market.current_state  # type: ignore[union-attr]
                if state is not None and self._station is not None:
                    self._station.set_market(state)
                    # Sample the timeline here, so it fills whether or not a browser is
                    # attached. One sample every ``TIMELINE_SAMPLE_SECONDS`` keeps four
                    # hours of history inside the view's bounded buffer.
                    samples += 1
                    if samples % TIMELINE_SAMPLE_SECONDS == 0 and self._view is not None:
                        self._view.record_market(state, _station_energy(self._station))
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - a bad poll must not stop the station
                _log.error(
                    "control_center.market_poll_failed",
                    error_type=type(error).__name__,
                    error=str(error),
                    exc_info=True,
                )
            await asyncio.sleep(MARKET_POLL_SECONDS)


async def serve(
    settings: AppSettings,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    scenario: Scenario = Scenario.RANDOM_WALK,
    seed: int = 2026,
    startup_mode: StartupMode = StartupMode.CONTROLLED_START,
) -> None:
    """Run the station and serve the Control Center until interrupted."""
    runner = ControlCenterRunner(settings, scenario=scenario, seed=seed, startup_mode=startup_mode)
    view = await runner.start()
    app = create_app(view)

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=None,  # structlog owns logging; uvicorn's would duplicate every line
        access_log=False,
    )
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await runner.stop()


def frontend_dist() -> Path:
    """Where the built UI is expected, for the CLI's diagnostics."""
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"
