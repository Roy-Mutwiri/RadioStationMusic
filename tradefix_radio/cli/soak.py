"""``tradefix soak`` — accelerated continuous-broadcast proof (§64, milestone 4.10).

Runs **the real runtime**: the real scheduler, the real queue, the real mixer, the real
generation manager with the §62 mock provider, writing to a ``NullSink`` under a
:class:`~tradefix_radio.core.clock.VirtualClock`. There is deliberately no parallel soak
implementation — a soak that exercises a simplified copy of the station proves something about
the copy.

The exit code is the point. Every acceptance invariant is checked and a breach exits non-zero,
so this belongs in CI rather than being a report someone reads optimistically.

**What accelerates and what does not.** Virtual time removes the *waiting*: a four-minute track
airs in the time it takes to write its blocks to a sink that discards them. It does not remove
the *work* — the mock provider renders real audio, and that is real CPU. A 24-hour soak
generates a few hundred tracks, which is minutes of rendering however fast the clock runs.
``--track-seconds`` is the lever that trades fidelity for speed, and the report prints what was
used so a fast run cannot be mistaken for a full-length one.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import structlog

from tradefix_radio.audio.format import PLAYOUT_CHANNELS, PLAYOUT_SAMPLE_RATE
from tradefix_radio.audio.sinks import NullSink
from tradefix_radio.config.schema import AppSettings
from tradefix_radio.core.clock import VirtualClock
from tradefix_radio.director.library import load_content_library
from tradefix_radio.director.music_director import MusicDirector
from tradefix_radio.director.selection import WeightedSelector
from tradefix_radio.generation.defects import DefectInjectingProvider
from tradefix_radio.generation.manager import DatabaseJobUnitOfWork, GenerationManager
from tradefix_radio.generation.mock import MockMusicProvider
from tradefix_radio.market.feeds.simulated import SimulatedFeed
from tradefix_radio.market.service import MarketDataService
from tradefix_radio.market.simulation import Scenario
from tradefix_radio.persistence.database import Database
from tradefix_radio.postprocess.pipeline import PostProductionPipeline
from tradefix_radio.radio.emergency import EmergencyManager, ProceduralSource
from tradefix_radio.radio.station import RadioStation
from tradefix_radio.radio.station_ids import StationIdLibrary, default_library
from tradefix_radio.runtime.coordinator import RuntimeCoordinator

_log = structlog.get_logger(__name__)

#: Config directory holding the creative libraries.
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

#: Sample rate the soak renders at.
#:
#: Below the 48 kHz playout rate on purpose, and the conversion boundary is exercised rather
#: than bypassed. A 24-hour soak writes several hundred tracks to disk; at full rate that is
#: gigabytes of temporary audio for a run whose subject is scheduling and continuity, not
#: fidelity. The crossfade and format tests cover audio quality at full rate.
SOAK_SAMPLE_RATE = 16_000

#: Track length used when the caller does not override it.
#:
#: Short, because every second of audio is a real render. Stated in the report so a fast run is
#: never mistaken for a full-length one.
DEFAULT_TRACK_SECONDS = 45

#: Real seconds the virtual clock yields per step.
#:
#: Zero. Virtual time cannot accelerate real work, and what accounts for real work here is
#: ``clock.hold()`` — the provider holds the clock while it renders, the playout engine while
#: it assembles a block — so there is nothing left for a blind sleep to buy.
#:
#: It was 0.001, which sounds negligible and was not: the host event loop's timer granularity
#: on Windows is about 15 ms, so each yield waited roughly fifteen times what it asked for. A
#: profile of a six-minute simulated broadcast spent 70.7 of its 75.6 seconds waiting on the
#: completion port across 15 435 calls, against under two seconds of actual audio work.
REAL_YIELD_SECONDS = 0.0

#: Virtual seconds the clock advances per driver iteration.
#:
#: Matched to the playout block size: advancing less would spin, and advancing much more would
#: let the clock outrun a station task that is between sleeps.
CLOCK_CHUNK_SECONDS = 10.0

#: Polls allowed while warming the feed before the broadcast starts.
#:
#: Generous: §4 needs ``warmup_bars * ticks_per_bar`` polls, which is 3 600 at the defaults.
#: Bounded all the same, so a misconfigured feed produces a warning rather than a hang.
MAX_WARMUP_POLLS = 20_000

#: How long to let the station driver finish its current step before cancelling it.
#:
#: Cancelling mid-step aborted a database transaction and surfaced a ``CancelledError`` from
#: the connection pool's rollback. Asking it to stop and waiting is both cleaner and closer to
#: what §74 does in production.
DRIVER_STOP_TIMEOUT_SECONDS = 5.0

#: Gap between market-state handovers, in simulated seconds.
INPUT_INTERVAL_SECONDS = 5.0

#: Fraction of broadcast time that must be covered by audio written to the sink.
#:
#: Not 1.0: the run stops on a clock boundary that can fall mid-block, and the first moments
#: before any tier is ready are legitimately uncovered. 0.98 leaves room for that and nothing
#: like enough room for a structural gap.
MINIMUM_AUDIO_COVERAGE = 0.98


@dataclass
class SoakResult:
    """Everything the report prints, and everything the exit code depends on."""

    simulated_seconds: float = 0.0
    wall_seconds: float = 0.0
    #: Simulated seconds spent warming the feed before the broadcast began.
    warmup_seconds: float = 0.0
    steps: int = 0
    track_seconds: int = DEFAULT_TRACK_SECONDS
    #: Throwaway database this run used, when it created its own.
    database_path: Path | None = None

    generated: int = 0
    played: int = 0
    failed: int = 0
    skipped: int = 0
    retries: int = 0
    timeouts: int = 0

    unintended_silence_seconds: float = 0.0
    #: Audio actually written to the sink. Compared against broadcast time for coverage.
    seconds_on_air: float = 0.0
    #: Broadcast seconds the run was supposed to cover.
    broadcast_seconds: float = 0.0
    transitions: int = 0
    transition_failures: int = 0
    peak_sample: float = 0.0
    underruns: int = 0

    tier2_activations: int = 0
    tier3_activations: int = 0
    tier2_seconds: float = 0.0
    tier3_seconds: float = 0.0

    latency_p50: float = 0.0
    latency_p95: float = 0.0
    capacity_ratio: float = 0.0

    genres_used: int = 0
    longest_genre_run: int = 1
    distinct_bpms: int = 0
    #: Post-production (§6). Zero across the board when the pipeline is not attached,
    #: which is why ``post_production`` records whether it ran rather than letting a row of
    #: zeros be read as "nothing was rejected".
    post_production: bool = False
    defects_injected: int = 0
    defects_by_kind: dict[str, int] = field(default_factory=dict)
    post_production_rejected: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    injected_ids_that_aired: list[str] = field(default_factory=list)

    blueprint_duplicates: int = 0
    #: ``(first_track_id, repeat_track_id, signature_prefix)`` for each repeat.
    duplicate_pairs: list[tuple[str, str, str]] = field(default_factory=list)
    track_id_duplicates: int = 0

    unhandled_exceptions: int = 0
    leaked_tasks: int = 0
    pending_leases: int = 0
    subscriber_errors: int = 0

    memory_start_mb: float = 0.0
    memory_peak_mb: float = 0.0
    memory_end_mb: float = 0.0

    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def _memory_mb() -> float:
    """Resident set size, or 0 when psutil is unavailable.

    Zero rather than an exception: a memory figure is diagnostic, and a soak that refused to run
    because it could not measure its own RSS would be prioritising the report over the test.
    """
    try:
        import psutil  # noqa: PLC0415 - optional, and only needed here

        return float(psutil.Process().memory_info().rss) / 1_048_576
    except Exception:  # noqa: BLE001 - diagnostics must never fail a run
        return 0.0


async def run_soak(
    settings: AppSettings,
    *,
    simulated_hours: float,
    seed: int = 2026,
    track_seconds: int = DEFAULT_TRACK_SECONDS,
    scenario: Scenario = Scenario.RANDOM_WALK,
    database: Database | None = None,
    kill_generator_from: float | None = None,
    kill_generator_until: float | None = None,
    post_production: bool = False,
    invalid_rate: float = 0.0,
    duplicate_rate: float = 0.0,
) -> SoakResult:
    """Broadcast for ``simulated_hours`` and report what happened.

    ``kill_generator_from`` / ``until`` drive Gate B and Gate D — the provider is made unhealthy
    for a window, and the run proves both that playback continues and that the station rebuilds
    afterwards. Expressed as a window rather than a flag so one run can prove both halves.

    ``post_production`` attaches the Phase 6 pipeline, and ``invalid_rate`` /
    ``duplicate_rate`` spoil that fraction of the generator's output (§6.23). The two are
    separable on purpose: injecting defects *without* the pipeline is the control run that
    shows what the station does when nothing validates, and it is the comparison that makes
    the pipeline run mean something.
    """
    import time  # noqa: PLC0415 - wall-clock measurement is this function's own concern

    result = SoakResult(track_seconds=track_seconds)
    result.memory_start_mb = _memory_mb()
    wall_started = time.perf_counter()

    clock = VirtualClock(real_yield_seconds=REAL_YIELD_SECONDS)
    owns_database = database is None
    scratch: Path | None = None
    if database is None:
        # A throwaway database, **not** the station's own.
        #
        # This used to open ``settings.database`` directly, which meant an endurance run wrote
        # its tracks, jobs and queue into the real ``data/tradefix.db`` and left them there.
        # Two consequences, both bad: the command polluted the station's own history, and
        # every run inherited the previous one's — so "blueprint duplicates: 1" could be a
        # collision against a track from a soak that ran an hour ago, and no run was
        # reproducible. A soak is a measurement, and a measurement that mutates the thing it
        # measures is not one.
        scratch = Path(tempfile.mkdtemp(prefix="tradefix-soak-"))
        database = Database(
            settings.database.model_copy(
                update={"url": f"sqlite+aiosqlite:///{(scratch / 'soak.db').as_posix()}"}
            ),
            # The virtual clock, so every unit of work holds it — see ``Database.session``.
            clock=clock,
        )
        await database.connect()
        await database.create_all()
        result.database_path = scratch / "soak.db"

    # Every track is this long. Pinning it keeps the run's size predictable, which matters when
    # the cost is real rendering.
    music = settings.music.model_copy(
        update={
            "min_duration_seconds": track_seconds,
            "max_duration_seconds": track_seconds,
        }
    )
    soak_settings = settings.model_copy(update={"music": music})

    coordinator = RuntimeCoordinator(clock=clock)
    library = load_content_library(config_dir=CONFIG_DIR)
    director = MusicDirector(
        soak_settings, library, selector=WeightedSelector(random.Random(seed))  # noqa: S311
    )
    provider = MockMusicProvider(
        soak_settings.generation.mock.model_copy(
            update={"sample_rate": SOAK_SAMPLE_RATE, "latency_seconds": 20.0}
        ),
        clock=clock,
        seed=seed,
    )
    injector: DefectInjectingProvider | None = None
    if invalid_rate > 0.0 or duplicate_rate > 0.0:
        injector = DefectInjectingProvider(
            provider,
            invalid_rate=invalid_rate,
            duplicate_rate=duplicate_rate,
            seed=seed,
        )
    generation = GenerationManager(
        provider=injector or provider,
        settings=soak_settings.generation,
        unit_of_work=DatabaseJobUnitOfWork(database.session),
        clock=clock,
    )

    pipeline: PostProductionPipeline | None = None
    if post_production:
        pipeline = PostProductionPipeline(
            soak_settings,
            clock=clock,
            master_dir=soak_settings.paths.data_dir / "soak-masters",
        )
    result.post_production = post_production
    sink = NullSink(
        sample_rate=PLAYOUT_SAMPLE_RATE,
        channels=PLAYOUT_CHANNELS,
        clock=clock,
        realtime=True,
    )
    station = RadioStation(
        soak_settings,
        database=database,
        coordinator=coordinator,
        director=director,
        generation=generation,
        sink=sink,
        station_ids=StationIdLibrary(
            default_library(soak_settings.paths.root_dir / "station_ids"),
            repeat_horizon=soak_settings.radio.station_id_repeat_horizon,
            rng=random.Random(seed),  # noqa: S311
        ),
        emergency=EmergencyManager(
            procedural=ProceduralSource(
                sample_rate=PLAYOUT_SAMPLE_RATE, block_seconds=30.0
            )
        ),
        clock=clock,
        post_production=pipeline,
        audio_dir=soak_settings.paths.data_dir / "soak-audio",
        # A large block keeps the step count sane. At one second, 24 simulated hours is 86 400
        # steps and the yield cost alone dominates the run; at ten it is 8 640.
        playout_block_seconds=10.0,
    )

    feed = SimulatedFeed(scenario=scenario, seed=seed, clock=clock)
    market = MarketDataService(soak_settings, feed, clock=clock)

    broadcast_seconds = simulated_hours * 3600.0
    genre_sequence: list[str] = []
    seen_signatures: dict[str, str] = {}
    seen_track_ids: set[str] = set()
    stopping = asyncio.Event()

    # Warm the feed before the broadcast starts, exactly as a real station does.
    #
    # §4's feature engine needs ``warmup_bars`` before it will report a state, and at the
    # configured 60 ticks per bar that is an hour of simulated time. Without pre-warming, the
    # first hour of every soak ran on Tier 3 with nothing planned — the station behaving
    # correctly, but the run measuring the warm-up rather than the radio. Pre-warming is driven
    # synchronously here because nothing else is running yet.
    # The feed has to be open before it will produce anything. ``SimulatedFeed.poll`` returns
    # ``None`` until then, so without this every warm-up poll was discarded and the loop ran to
    # its cap having achieved nothing — the market then warmed up *during* the broadcast, which
    # is what the first runs were actually measuring.
    await feed.open()
    warmup_polls = 0
    while market.current_state is None and warmup_polls < MAX_WARMUP_POLLS:
        await market.poll_once()
        clock.advance_sync(soak_settings.market.poll_interval_seconds)
        warmup_polls += 1
    result.warmup_seconds = clock.monotonic()
    # Measured from the end of warm-up, so ``--simulated-hours 2`` means two hours *on air*.
    target_seconds = clock.monotonic() + broadcast_seconds
    if market.current_state is None:
        _log.warning(
            "soak.market_never_warmed",
            polls=warmup_polls,
            detail="the run will broadcast on emergency tiers only",
        )

    async def drive_inputs() -> None:
        """Feed the station its market state and apply chaos. Its own task.

        The station runs its own loops — playout, generation, scheduling — so this does not
        step it. What it does is the part only the soak knows: hand over the current market
        state and, for Gates B and D, decide when the provider is broken.

        Why a task rather than the clock loop: the station's playout engine sleeps on the clock
        between blocks, and a single loop that both advanced the clock and awaited the station
        would deadlock on the first block — the sink waiting for time only the caller could
        provide, from inside the caller. The first version did exactly that and hung.
        """
        while clock.monotonic() < target_seconds and not stopping.is_set():
            state = market.current_state
            if state is not None:
                station.set_market(state)

            if kill_generator_from is not None:
                killed = clock.monotonic() >= kill_generator_from and (
                    kill_generator_until is None
                    or clock.monotonic() < kill_generator_until
                )
                # Reaching into the provider is deliberate chaos injection (§66). A public
                # setter would be an API that only a test wants.
                provider._settings = provider._settings.model_copy(
                    update={"failure_rate": 1.0 if killed else 0.0}
                )

            result.steps += 1
            result.memory_peak_mb = max(result.memory_peak_mb, _memory_mb())
            observe_history()
            await clock.sleep(INPUT_INTERVAL_SECONDS)

    def observe_history() -> None:
        """Record what actually aired, from the station's history.

        Read from history rather than by sampling ``playout.current``. Sampling misses any
        track that starts and finishes between two clock chunks — the first run reported zero
        genres across four played tracks because of exactly that, and the §81-18 check then
        failed on an artefact of the observer.
        """
        for entry in reversed(station.history().entries):
            if entry.track_id in seen_track_ids:
                continue
            seen_track_ids.add(entry.track_id)
            if not genre_sequence or genre_sequence[-1] != entry.genre:
                genre_sequence.append(entry.genre)
            first = seen_signatures.get(entry.blueprint_signature)
            if first is not None:
                result.blueprint_duplicates += 1
                # Recorded with the pair, not just counted. "1 duplicate" tells an operator
                # that something is wrong and nothing about what; the two track ids and the
                # signature prefix are what make it findable in the log.
                result.duplicate_pairs.append(
                    (first, entry.track_id, entry.blueprint_signature[:12])
                )
            else:
                seen_signatures[entry.blueprint_signature] = entry.track_id

    await market.start()
    await station.start()
    driver = asyncio.create_task(drive_inputs(), name="soak-inputs")
    try:
        # Advance in chunks rather than one jump, so the station's own sleeps are honoured in
        # order and a long run cannot be fast-forwarded past work that is mid-flight.
        while clock.monotonic() < target_seconds and not driver.done():
            # ``run_to``, not ``run_for``: never advance past a waiter. See the method's
            # docstring — jumping past a station task that is mid-I/O loses playout blocks.
            await clock.run_to(
                min(clock.monotonic() + CLOCK_CHUNK_SECONDS, target_seconds)
            )
        stopping.set()
        done, _ = await asyncio.wait({driver}, timeout=DRIVER_STOP_TIMEOUT_SECONDS)
        if not done:
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)
        observe_history()
        played_ids = [entry.track_id for entry in station.history().entries]
        result.track_id_duplicates = len(played_ids) - len(set(played_ids))
    finally:
        await market.stop()
        await station.stop()
        if owns_database:
            await database.disconnect()
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)

    _collect(
        result,
        station=station,
        generation=generation,
        coordinator=coordinator,
        clock=clock,
        genre_sequence=genre_sequence,
        injector=injector,
        played_ids=played_ids,
    )
    result.broadcast_seconds = broadcast_seconds
    result.wall_seconds = time.perf_counter() - wall_started
    result.memory_end_mb = _memory_mb()
    _assess(result)
    _assess_post_production(result)
    return result


def _collect(
    result: SoakResult,
    *,
    station: RadioStation,
    generation: GenerationManager,
    coordinator: RuntimeCoordinator,
    clock: VirtualClock,
    genre_sequence: list[str],
    injector: DefectInjectingProvider | None = None,
    played_ids: list[str] | None = None,
) -> None:
    playout = station.playout.stats
    emergency = station.emergency.stats
    capacity = generation.capacity_snapshot()

    result.simulated_seconds = clock.monotonic()
    result.seconds_on_air = playout.seconds_on_air
    result.generated = generation.stats.completed
    result.played = playout.tracks_completed
    result.failed = generation.stats.failed
    result.skipped = playout.tracks_skipped
    result.retries = generation.stats.retries
    result.timeouts = generation.stats.timeouts

    result.unintended_silence_seconds = playout.unintended_silence_seconds
    result.transitions = playout.transitions
    result.transition_failures = playout.transition_failures
    result.peak_sample = playout.peak_sample
    result.underruns = playout.underruns

    result.post_production_rejected = station.stats.post_production_rejected
    result.rejection_reasons = dict(station.stats.rejection_reasons)
    if injector is not None:
        result.defects_injected = injector.stats.injected
        result.defects_by_kind = dict(injector.stats.by_kind)
        # The assertion that matters, computed rather than assumed: did anything the injector
        # deliberately broke actually reach air? One name in this list is a §6 failure, and
        # it is checked against the *played* history rather than against a counter, because a
        # counter can be right while the wrong track is on the radio.
        spoiled = set(injector.stats.corrupted_track_ids) | set(
            injector.stats.duplicate_track_ids
        )
        result.injected_ids_that_aired = sorted(spoiled & set(played_ids or ()))

    result.tier2_activations = emergency.tier2_activations
    result.tier3_activations = emergency.tier3_activations
    result.tier2_seconds = emergency.seconds_in_tier2
    result.tier3_seconds = emergency.seconds_in_tier3

    result.latency_p50 = capacity.latency_p50_seconds
    result.latency_p95 = capacity.latency_p95_seconds
    result.capacity_ratio = capacity.capacity_ratio

    result.genres_used = len(set(genre_sequence))
    result.longest_genre_run = _longest_run(genre_sequence)
    result.distinct_bpms = len(
        {entry.bpm for entry in station.history().entries}
    )

    result.leaked_tasks = coordinator.live_task_count()
    result.subscriber_errors = coordinator.stats.handler_errors
    result.unhandled_exceptions = coordinator.stats.tasks_crashed
    result.pending_leases = generation.in_flight


def _longest_run(sequence: list[str]) -> int:
    """Longest consecutive repeat. ``genre_sequence`` already collapses repeats to 1."""
    if not sequence:
        return 0
    longest = current = 1
    for index in range(1, len(sequence)):
        current = current + 1 if sequence[index] == sequence[index - 1] else 1
        longest = max(longest, current)
    return longest


def _assess(result: SoakResult) -> None:
    """Apply the Phase 4 acceptance invariants. Each breach is recorded, not raised.

    Recorded rather than raised so one run reports *every* problem. A soak that aborted on the
    first breach would need as many runs as there are defects, and each run is minutes.
    """
    if result.unintended_silence_seconds > 0.0:
        result.failures.append(
            f"{result.unintended_silence_seconds:.3f}s of unintended silence — Gate A requires "
            "zero"
        )
    # The honest continuity check, and the one that caught a real defect.
    #
    # Counting underruns alone reported zero silence while the station had written 290 seconds
    # of audio across 3 600 seconds of broadcast: the playout task shared a loop with
    # scheduling, so the clock advanced over time no audio covered. Coverage compares what
    # reached the sink against what the run was supposed to cover, and cannot be fooled that
    # way.
    if result.broadcast_seconds > 0:
        coverage = result.seconds_on_air / result.broadcast_seconds
        if coverage < MINIMUM_AUDIO_COVERAGE:
            result.failures.append(
                f"audio covered only {coverage:.1%} of the broadcast "
                f"({result.seconds_on_air:.0f}s of {result.broadcast_seconds:.0f}s) — Gate A"
            )
    if result.underruns > 0:
        result.failures.append(f"{result.underruns} queue underrun(s) — Gate A requires zero")
    if result.transition_failures > 0:
        result.failures.append(f"{result.transition_failures} transition failure(s)")
    if result.peak_sample > 1.0:
        result.failures.append(
            f"peak sample {result.peak_sample:.3f} exceeds full scale — Gate I"
        )
    if result.unhandled_exceptions > 0:
        result.failures.append(
            f"{result.unhandled_exceptions} background task(s) crashed — Gate G"
        )
    if result.leaked_tasks > 0:
        result.failures.append(f"{result.leaked_tasks} task(s) still running at shutdown")
    if result.pending_leases > 0:
        result.failures.append(f"{result.pending_leases} generation lease(s) still held")
    if result.track_id_duplicates > 0:
        result.failures.append(
            f"{result.track_id_duplicates} track id(s) played twice — Gate G"
        )
    if result.blueprint_duplicates > 0:
        detail = "; ".join(
            f"{first} == {repeat} ({sig})" for first, repeat, sig in result.duplicate_pairs[:5]
        )
        result.failures.append(
            f"{result.blueprint_duplicates} blueprint signature(s) repeated — §11: {detail}"
        )
    if result.played > 0 and result.genres_used < 3:
        result.failures.append(
            f"only {result.genres_used} genre(s) across {result.played} tracks — §81-18"
        )
    if result.longest_genre_run > 2:
        result.failures.append(
            f"a genre ran {result.longest_genre_run} times consecutively — §11 allows 2"
        )


#: Rejection rate above which the pipeline is reported as starving the station.
#:
#: 0.60. A run that injects 30 % defects should reject somewhere near that plus whatever the
#: generator legitimately repeats. Twice the injected rate means something other than the
#: injected defects is being rejected en masse, and the station is being kept on air by its
#: emergency tiers rather than by its programming — a state that looks healthy on every
#: continuity metric and is not.
_STARVATION_REJECTION_RATE: Final = 0.60


def _assess_post_production(result: SoakResult) -> None:
    """§6.24: the station kept broadcasting, and nothing broken reached air."""
    if not result.post_production:
        return
    if result.injected_ids_that_aired:
        result.failures.append(
            f"{len(result.injected_ids_that_aired)} deliberately broken track(s) reached "
            f"air: {', '.join(result.injected_ids_that_aired[:5])}"
        )
    if result.defects_injected and result.post_production_rejected == 0:
        result.failures.append(
            f"{result.defects_injected} defects were injected and post-production rejected "
            "nothing — the pipeline is not being consulted"
        )

    # Rejecting too much is a failure as well as rejecting too little, and it is the one
    # that hides: audio coverage stays at 100 %, silence stays at zero, and the station is
    # running entirely on procedural filler. Measured and reported rather than inferred from
    # the genre-diversity gate, which detects the symptom without naming the cause.
    generated = result.generated
    if generated:
        rate = result.post_production_rejected / generated
        if rate > _STARVATION_REJECTION_RATE:
            result.failures.append(
                f"post-production rejected {result.post_production_rejected} of {generated} "
                f"tracks ({rate:.0%}); {result.defects_injected} were deliberately broken, so "
                "the programming is being starved rather than filtered"
            )


def _coverage(result: SoakResult) -> float:
    if result.broadcast_seconds <= 0:
        return 0.0
    return result.seconds_on_air / result.broadcast_seconds


def render(result: SoakResult) -> str:
    """The §101 report."""
    hours = result.simulated_seconds / 3600.0
    lines = [
        "",
        "  TRADE FIX RADIO SOAK REPORT",
        "",
        f"  Simulated runtime:  {hours:.2f}h  "
        f"({result.steps} steps in {result.wall_seconds:.1f}s wall clock)",
        f"  Feed warm-up:       {result.warmup_seconds / 60:.0f} min simulated, "
        "before the broadcast began",
        f"  Track length:       {result.track_seconds}s (fixed for this run)",
        "",
        "  Tracks",
        f"    Generated:        {result.generated}",
        f"    Played:           {result.played}",
        f"    Failed:           {result.failed}",
        f"    Skipped:          {result.skipped}",
        "",
        "  Audio",
        f"    Unintended silence: {result.unintended_silence_seconds * 1000:.0f} ms",
        f"    Audio coverage:     {_coverage(result):.2%} "
        f"({result.seconds_on_air:.0f}s of {result.broadcast_seconds:.0f}s)",
        f"    Transitions:        {result.transitions}",
        f"    Transition failures: {result.transition_failures}",
        f"    Peak sample:        {result.peak_sample:.4f}",
        "",
        "  Radio",
        f"    Queue underruns:    {result.underruns}",
        f"    Tier 2 activations: {result.tier2_activations} "
        f"({result.tier2_seconds / 60:.1f} min)",
        f"    Tier 3 activations: {result.tier3_activations} "
        f"({result.tier3_seconds / 60:.1f} min)",
        "",
        "  Generation",
        f"    p50:              {result.latency_p50:.1f}s",
        f"    p95:              {result.latency_p95:.1f}s",
        f"    capacity ratio:   {result.capacity_ratio:.2f}x",
        f"    Retries:          {result.retries}",
        f"    Timeouts:         {result.timeouts}",
        "",
        "  Creative",
        f"    Genres used:      {result.genres_used}",
        f"    Longest identical genre run: {result.longest_genre_run}",
        f"    Distinct BPMs:    {result.distinct_bpms}",
        f"    Blueprint duplicates: {result.blueprint_duplicates}",
        f"    Track id duplicates:  {result.track_id_duplicates}",
        "",
        *_post_production_lines(result),
        "  Runtime",
        f"    Unhandled exceptions: {result.unhandled_exceptions}",
        f"    Subscriber errors:    {result.subscriber_errors}",
        f"    Leaked tasks:         {result.leaked_tasks}",
        f"    Pending leases:       {result.pending_leases}",
        "",
        "  Memory",
        f"    Start:            {result.memory_start_mb:.0f} MB",
        f"    Peak:             {result.memory_peak_mb:.0f} MB",
        f"    End:              {result.memory_end_mb:.0f} MB",
        "",
    ]
    if result.passed:
        lines.append("  PASS: every acceptance invariant held")
    else:
        lines.append(f"  FAIL: {len(result.failures)} invariant(s) breached")
        lines.extend(f"    - {failure}" for failure in result.failures)
    lines.append("")
    return "\n".join(lines)


def _post_production_lines(result: SoakResult) -> list[str]:
    """The §6 section of the report, or a single line saying it did not run.

    The explicit "not attached" line exists because a block of zeros reads as "nothing was
    rejected", which is a very different claim from "nothing was checked".
    """
    if not result.post_production:
        return ["  Post-production", "    Not attached for this run.", ""]

    lines = [
        "  Post-production (§6)",
        f"    Defects injected:   {result.defects_injected}",
    ]
    for kind, count in sorted(result.defects_by_kind.items()):
        if kind != "none":
            lines.append(f"      {kind:16s}{count}")
    lines.append(f"    Tracks rejected:    {result.post_production_rejected}")
    for reason, count in sorted(result.rejection_reasons.items()):
        lines.append(f"      {reason:16s}{count}")
    aired = result.injected_ids_that_aired
    lines.append(
        f"    Broken tracks aired: {len(aired)}"
        + (f"  ({', '.join(aired[:5])})" if aired else "  — none, which is the requirement")
    )
    lines.append("")
    return lines


async def command(args: argparse.Namespace, settings: AppSettings) -> int:
    result = await run_soak(
        settings,
        simulated_hours=args.simulated_hours,
        seed=args.seed,
        track_seconds=args.track_seconds,
        scenario=Scenario(args.scenario),
        kill_generator_from=args.kill_generator_from,
        kill_generator_until=args.kill_generator_until,
        post_production=args.post_production,
        invalid_rate=args.invalid_rate,
        duplicate_rate=args.duplicate_rate,
    )
    print(render(result))
    if args.out:
        destination = Path(args.out)
        text = render(result)
        # Off the event loop: a soak can be writing a large report while the station's own
        # tasks are still unwinding, and a blocking write would stall them.
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(destination.write_text, text, encoding="utf-8")
        print(f"  written to {destination}")
    return 0 if result.passed else 1


def register(subparsers: object) -> None:
    """Add the ``soak`` subcommand."""
    parser = subparsers.add_parser(  # type: ignore[attr-defined]
        "soak",
        help="run an accelerated continuous broadcast and check the §64 invariants",
    )
    parser.add_argument(
        "--simulated-hours",
        type=float,
        default=2.0,
        help="hours of simulated broadcast (2, 24, 72, 168)",
    )
    parser.add_argument("--seed", type=int, default=2026, help="RNG seed (reproducible)")
    parser.add_argument(
        "--track-seconds",
        type=int,
        default=DEFAULT_TRACK_SECONDS,
        help="fixed track length; shorter runs faster because audio is really rendered",
    )
    parser.add_argument(
        "--scenario",
        default=Scenario.RANDOM_WALK.value,
        choices=[scenario.value for scenario in Scenario],
        help="§7 market scenario to broadcast against",
    )
    parser.add_argument(
        "--post-production",
        action="store_true",
        help=(
            "run every generated track through the Phase 6 QC/originality/mastering "
            "pipeline before it may become READY (§6.14)"
        ),
    )
    parser.add_argument(
        "--invalid-rate",
        type=float,
        default=0.0,
        help=(
            "fraction of generated tracks to deliberately corrupt — silence, clipping, "
            "truncation or a dropout (§6.23). Use 0.2 for the acceptance run."
        ),
    )
    parser.add_argument(
        "--duplicate-rate",
        type=float,
        default=0.0,
        help=(
            "fraction of generated tracks to re-emit as byte-identical copies of an "
            "earlier track (§6.23). Use 0.1 for the acceptance run."
        ),
    )
    parser.add_argument(
        "--kill-generator-from",
        type=float,
        default=None,
        help="simulated second at which the provider starts failing (Gate B)",
    )
    parser.add_argument(
        "--kill-generator-until",
        type=float,
        default=None,
        help="simulated second at which the provider recovers (Gate D)",
    )
    parser.add_argument("--out", default=None, help="also write the report to this path")


__all__ = [
    "DEFAULT_TRACK_SECONDS",
    "SOAK_SAMPLE_RATE",
    "SoakResult",
    "command",
    "register",
    "render",
    "run_soak",
]
