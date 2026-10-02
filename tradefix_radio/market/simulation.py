"""Market simulation engine (§7).

§7 calls this "essential", and it is: without it the station cannot be developed,
tested, demonstrated or endurance-run. "The entire application must be testable
without internet or broker credentials."

All 15 §7 scenarios are implemented as **generators of synthetic price paths**
rather than recordings, so a scenario can run indefinitely and be reproduced
exactly from a seed.

Design decisions
----------------

**Everything is relative.** A scenario is defined in terms of volatility *as a
fraction of price* and drift *per bar*, never in dollars. That means a scenario
behaves identically whether gold is at 1 800 or 4 000 — the same property §6
demands of the feature engine, enforced here at the source.

**Scenarios have internal phases.** A real breakout is not a step change: it
compresses, builds, expands, then normalises. ``FAKE_BREAKOUT`` and
``VIOLENT_BREAKOUT`` differ only in what happens after the expansion phase, which
is exactly the distinction the regime engine has to learn to make. A simulator
built from single constant volatility numbers would let a naive classifier pass.

**Deterministic from a seed.** Every path is produced by a seeded
``random.Random``, so an endurance finding can be reproduced from the report. The
module never touches the global RNG.

**Bar-synchronous, clock-driven.** The engine produces one tick per call and does
not sleep; the feed decides pacing using the injected clock. This is what lets §64
run a simulated week in seconds.
"""

from __future__ import annotations

import enum
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from tradefix_radio.contracts.market import MarketSnapshotV1
from tradefix_radio.core.clock import UTC


class Scenario(str, enum.Enum):
    """The 15 §7 scenarios, in the order the brief lists them."""

    FLAT = "flat"
    LOW_VOLATILITY_RANGE = "low_volatility_range"
    SLOW_BULL_TREND = "slow_bull_trend"
    SLOW_BEAR_TREND = "slow_bear_trend"
    COMPRESSION = "compression"
    BREAKOUT_UP = "breakout_up"
    BREAKOUT_DOWN = "breakout_down"
    VIOLENT_BREAKOUT = "violent_breakout"
    FAKE_BREAKOUT = "fake_breakout"
    VOLATILITY_SPIKE = "volatility_spike"
    MEAN_REVERSION = "mean_reversion"
    FLASH_MOVE = "flash_move"
    EVENT_VOLATILITY = "event_volatility"
    RANDOM_WALK = "random_walk"
    HISTORICAL_REPLAY = "historical_replay"

    @property
    def label(self) -> str:
        """Human label for the §7 simulation buttons."""
        return self.value.replace("_", " ").title()


#: Scenarios the §7 UI exposes as buttons, mapped to their button label.
UI_SCENARIOS: dict[Scenario, str] = {
    Scenario.FLAT: "Quiet",
    Scenario.LOW_VOLATILITY_RANGE: "Range",
    Scenario.SLOW_BULL_TREND: "Bull Trend",
    Scenario.SLOW_BEAR_TREND: "Bear Trend",
    Scenario.COMPRESSION: "Compression",
    Scenario.BREAKOUT_UP: "Breakout Up",
    Scenario.BREAKOUT_DOWN: "Breakout Down",
    Scenario.VIOLENT_BREAKOUT: "Extreme Volatility",
    Scenario.RANDOM_WALK: "Random",
}


@dataclass(frozen=True)
class ScenarioProfile:
    """Parameters of one scenario phase.

    All magnitudes are **fractions of price per bar**, never absolute amounts, so a
    profile is valid at any price level.
    """

    #: Standard deviation of per-bar return.
    volatility: float
    #: Mean per-bar return. Positive is bullish.
    drift: float
    #: Fraction of the bar's move that pulls back toward the anchor price. 0 is a
    #: pure random walk; 1 pins price to the anchor.
    mean_reversion: float = 0.0
    #: Multiplier on tick volume relative to a quiet baseline.
    volume_multiplier: float = 1.0
    #: Bars this phase lasts. ``None`` means "until the scenario changes".
    bars: int | None = None

    def __post_init__(self) -> None:
        if self.volatility < 0:
            raise ValueError("volatility must not be negative")
        if not 0.0 <= self.mean_reversion <= 1.0:
            raise ValueError("mean_reversion must be within [0, 1]")
        if self.volume_multiplier <= 0:
            raise ValueError("volume_multiplier must be positive")
        if self.bars is not None and self.bars < 1:
            raise ValueError("bars must be at least 1")


#: Baseline per-bar volatility for a *typical* XAUUSD minute, as a fraction of
#: price. Roughly 0.035 %, which at 4 000 is about 1.4 points — the right order of
#: magnitude for gold on a one-minute bar. Every scenario scales from this, so the
#: whole simulator can be recalibrated by changing one number.
BASE_VOLATILITY = 0.00035


def _phases(scenario: Scenario) -> tuple[ScenarioProfile, ...]:
    """Phase sequence for a scenario.

    A single-element tuple means the scenario is stationary. Multi-phase scenarios
    cycle: the last phase repeats once the sequence is exhausted, unless the
    scenario explicitly loops (compression and event volatility do, because both
    describe recurring market behaviour rather than a one-off).
    """
    v = BASE_VOLATILITY
    if scenario is Scenario.FLAT:
        # Near-dead. Asian lunch-hour gold. Strong mean reversion keeps it pinned.
        return (ScenarioProfile(volatility=v * 0.18, drift=0.0, mean_reversion=0.35,
                                volume_multiplier=0.4),)

    if scenario is Scenario.LOW_VOLATILITY_RANGE:
        return (ScenarioProfile(volatility=v * 0.5, drift=0.0, mean_reversion=0.18,
                                volume_multiplier=0.7),)

    if scenario is Scenario.SLOW_BULL_TREND:
        # Drift chosen so the trend is clearly directional but not a breakout:
        # about a third of a standard deviation per bar, which accumulates into an
        # unmistakable slope over an hour while each individual bar looks ordinary.
        return (ScenarioProfile(volatility=v * 0.9, drift=v * 0.3, mean_reversion=0.02,
                                volume_multiplier=1.0),)

    if scenario is Scenario.SLOW_BEAR_TREND:
        return (ScenarioProfile(volatility=v * 0.9, drift=-v * 0.3, mean_reversion=0.02,
                                volume_multiplier=1.0),)

    if scenario is Scenario.COMPRESSION:
        # Volatility decays stepwise and volume drains: the classic pre-breakout
        # coil. Loops, because compression recurs rather than resolving once.
        return (
            ScenarioProfile(volatility=v * 0.8, drift=0.0, mean_reversion=0.2,
                            volume_multiplier=0.9, bars=20),
            ScenarioProfile(volatility=v * 0.45, drift=0.0, mean_reversion=0.35,
                            volume_multiplier=0.65, bars=20),
            ScenarioProfile(volatility=v * 0.22, drift=0.0, mean_reversion=0.5,
                            volume_multiplier=0.45, bars=25),
            ScenarioProfile(volatility=v * 0.12, drift=0.0, mean_reversion=0.6,
                            volume_multiplier=0.35, bars=30),
        )

    if scenario is Scenario.BREAKOUT_UP:
        # Coil, BUILDUP, expansion, trending continuation.
        #
        # The buildup phase is not decoration. Volume leads price: participation
        # arrives while the range is still tight, and that combination is precisely
        # what distinguishes BREAKOUT_BUILDUP from both COMPRESSION (tight, quiet)
        # and BULLISH_BREAKOUT (wide, loud). Without it the regime was unreachable
        # from the whole scenario library, so the programming mapped to it would
        # never have aired.
        return (
            ScenarioProfile(volatility=v * 0.3, drift=0.0, mean_reversion=0.45,
                            volume_multiplier=0.5, bars=25),
            ScenarioProfile(volatility=v * 0.38, drift=v * 0.2, mean_reversion=0.3,
                            volume_multiplier=3.2, bars=8),
            ScenarioProfile(volatility=v * 3.2, drift=v * 4.5, mean_reversion=0.0,
                            volume_multiplier=4.0, bars=12),
            ScenarioProfile(volatility=v * 1.6, drift=v * 1.2, mean_reversion=0.02,
                            volume_multiplier=1.8),
        )

    if scenario is Scenario.BREAKOUT_DOWN:
        return (
            ScenarioProfile(volatility=v * 0.3, drift=0.0, mean_reversion=0.45,
                            volume_multiplier=0.5, bars=25),
            ScenarioProfile(volatility=v * 0.38, drift=-v * 0.2, mean_reversion=0.3,
                            volume_multiplier=3.2, bars=8),
            ScenarioProfile(volatility=v * 3.2, drift=-v * 4.5, mean_reversion=0.0,
                            volume_multiplier=4.0, bars=12),
            ScenarioProfile(volatility=v * 1.6, drift=-v * 1.2, mean_reversion=0.02,
                            volume_multiplier=1.8),
        )

    if scenario is Scenario.VIOLENT_BREAKOUT:
        # Sustained extreme expansion. This is the EXTREME_VOLATILITY driver.
        return (
            ScenarioProfile(volatility=v * 0.25, drift=0.0, mean_reversion=0.5,
                            volume_multiplier=0.5, bars=15),
            ScenarioProfile(volatility=v * 8.0, drift=v * 9.0, mean_reversion=0.0,
                            volume_multiplier=9.0, bars=20),
            ScenarioProfile(volatility=v * 5.0, drift=v * 2.0, mean_reversion=0.0,
                            volume_multiplier=5.0),
        )

    if scenario is Scenario.FAKE_BREAKOUT:
        # Expands upward convincingly, then reverses *through* the origin. The
        # reversal drift is deliberately larger than the breakout drift so the
        # retrace fully invalidates the move — which is what makes it a fakeout
        # rather than a pullback, and what REVERSAL must detect.
        return (
            ScenarioProfile(volatility=v * 0.3, drift=0.0, mean_reversion=0.45,
                            volume_multiplier=0.5, bars=22),
            ScenarioProfile(volatility=v * 3.0, drift=v * 4.0, mean_reversion=0.0,
                            volume_multiplier=3.5, bars=8),
            ScenarioProfile(volatility=v * 3.4, drift=-v * 5.5, mean_reversion=0.0,
                            volume_multiplier=4.2, bars=14),
            ScenarioProfile(volatility=v * 1.1, drift=0.0, mean_reversion=0.2,
                            volume_multiplier=1.2),
        )

    if scenario is Scenario.VOLATILITY_SPIKE:
        # Volatility explodes with no direction: wide two-sided bars. Tests that
        # the engine does not invent a trend from noise.
        return (
            ScenarioProfile(volatility=v * 0.7, drift=0.0, mean_reversion=0.15,
                            volume_multiplier=0.9, bars=18),
            ScenarioProfile(volatility=v * 6.5, drift=0.0, mean_reversion=0.05,
                            volume_multiplier=6.0, bars=16),
            ScenarioProfile(volatility=v * 1.3, drift=0.0, mean_reversion=0.2,
                            volume_multiplier=1.4),
        )

    if scenario is Scenario.MEAN_REVERSION:
        # Strong pull to the anchor: price oscillates across a level.
        return (ScenarioProfile(volatility=v * 1.5, drift=0.0, mean_reversion=0.55,
                                volume_multiplier=1.1),)

    if scenario is Scenario.FLASH_MOVE:
        # One violent bar, then immediate partial recovery and an elevated tail.
        # POST_EVENT_NORMALIZATION should appear during the decay.
        return (
            ScenarioProfile(volatility=v * 0.5, drift=0.0, mean_reversion=0.3,
                            volume_multiplier=0.8, bars=12),
            ScenarioProfile(volatility=v * 22.0, drift=-v * 26.0, mean_reversion=0.0,
                            volume_multiplier=18.0, bars=2),
            ScenarioProfile(volatility=v * 7.0, drift=v * 9.0, mean_reversion=0.1,
                            volume_multiplier=7.0, bars=5),
            ScenarioProfile(volatility=v * 2.2, drift=0.0, mean_reversion=0.25,
                            volume_multiplier=2.0, bars=20),
            ScenarioProfile(volatility=v * 0.9, drift=0.0, mean_reversion=0.25,
                            volume_multiplier=1.0),
        )

    if scenario is Scenario.EVENT_VOLATILITY:
        # Quiet pre-release, violent two-sided reaction, then decay. Loops, because
        # an unattended station meets several releases a day.
        return (
            ScenarioProfile(volatility=v * 0.2, drift=0.0, mean_reversion=0.5,
                            volume_multiplier=0.35, bars=25),
            ScenarioProfile(volatility=v * 9.0, drift=0.0, mean_reversion=0.0,
                            volume_multiplier=11.0, bars=6),
            ScenarioProfile(volatility=v * 4.0, drift=0.0, mean_reversion=0.1,
                            volume_multiplier=4.5, bars=12),
            ScenarioProfile(volatility=v * 1.6, drift=0.0, mean_reversion=0.2,
                            volume_multiplier=1.5, bars=30),
        )

    if scenario is Scenario.RANDOM_WALK:
        return (ScenarioProfile(volatility=v, drift=0.0, mean_reversion=0.0,
                                volume_multiplier=1.0),)

    # HISTORICAL_REPLAY is driven by recorded data, not by a profile; the replay
    # feed supplies the snapshots directly. A neutral profile is returned so the
    # enum is total and callers need no special case.
    return (ScenarioProfile(volatility=v, drift=0.0, mean_reversion=0.0,
                            volume_multiplier=1.0),)


#: Scenarios whose phase sequence restarts rather than holding the final phase.
LOOPING_SCENARIOS = frozenset({Scenario.COMPRESSION, Scenario.EVENT_VOLATILITY})


@dataclass
class SimulationState:
    """Mutable position within a running simulation.

    Exposed (rather than private) because the §47 Market Lab shows which phase of a
    scenario is active, and because an endurance report that says "failed in phase
    2 of breakout_up" is far more useful than one that says "failed".
    """

    scenario: Scenario
    phase_index: int = 0
    bars_in_phase: int = 0
    bars_total: int = 0
    price: float = 0.0
    anchor: float = 0.0
    #: Bar accumulator: the ticks seen so far within the current bar.
    bar_open: float = 0.0
    bar_high: float = 0.0
    bar_low: float = 0.0
    bar_volume: float = 0.0
    ticks_in_bar: int = 0
    history: list[float] = field(default_factory=list)


class MarketSimulationEngine:
    """Generates synthetic XAUUSD paths for any §7 scenario.

    Usage::

        engine = MarketSimulationEngine(seed=42, start_price=4000.0)
        engine.set_scenario(Scenario.BREAKOUT_UP)
        snapshot = engine.next_tick(now)

    The engine is **synchronous and pull-based**: it never sleeps and never touches
    a clock of its own. Callers supply the timestamp, which is what allows a
    virtual clock to drive a simulated week in seconds (§64).
    """

    def __init__(
        self,
        *,
        symbol: str = "XAUUSD",
        start_price: float = 4_000.0,
        seed: int = 0,
        ticks_per_bar: int = 60,
        spread_fraction: float = 0.00005,
    ) -> None:
        if start_price <= 0:
            raise ValueError("start_price must be positive")
        if ticks_per_bar < 1:
            raise ValueError("ticks_per_bar must be at least 1")
        if spread_fraction < 0:
            raise ValueError("spread_fraction must not be negative")
        self._symbol = symbol
        self._start_price = start_price
        self._ticks_per_bar = ticks_per_bar
        self._spread_fraction = spread_fraction
        self._random = random.Random(seed)  # noqa: S311 - simulation, not security
        self._state = SimulationState(
            scenario=Scenario.RANDOM_WALK,
            price=start_price,
            anchor=start_price,
            bar_open=start_price,
            bar_high=start_price,
            bar_low=start_price,
        )
        self._completed_bars: list[tuple[float, float, float, float, float]] = []

    # -- control -----------------------------------------------------------

    @property
    def state(self) -> SimulationState:
        return self._state

    @property
    def scenario(self) -> Scenario:
        return self._state.scenario

    @property
    def current_profile(self) -> ScenarioProfile:
        phases = _phases(self._state.scenario)
        index = min(self._state.phase_index, len(phases) - 1)
        return phases[index]

    def set_scenario(self, scenario: Scenario, *, reset_anchor: bool = True) -> None:
        """Switch scenario, restarting its phase sequence.

        Price is deliberately **not** reset: §7's simulation buttons must be usable
        mid-run, and teleporting price would create an artificial gap that the
        feature engine would correctly read as a flash crash. The anchor moves to
        the current price so mean-reverting scenarios revert to where we are rather
        than dragging price back to where the previous scenario started.
        """
        self._state.scenario = scenario
        self._state.phase_index = 0
        self._state.bars_in_phase = 0
        if reset_anchor:
            self._state.anchor = self._state.price

    def reset(self, *, price: float | None = None, seed: int | None = None) -> None:
        """Return to a clean initial state. Used between test scenarios."""
        if seed is not None:
            self._random = random.Random(seed)  # noqa: S311 - simulation
        start = price if price is not None else self._start_price
        self._state = SimulationState(
            scenario=self._state.scenario,
            price=start,
            anchor=start,
            bar_open=start,
            bar_high=start,
            bar_low=start,
        )
        self._completed_bars.clear()

    # -- generation --------------------------------------------------------

    def next_tick(self, now: datetime) -> MarketSnapshotV1:
        """Advance one tick and return the resulting snapshot.

        The returned snapshot always carries ``synthetic=True``. That flag is how
        every layer above stays able to distinguish simulated from live data, which
        §7 and §86 both require.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        state = self._state
        profile = self.current_profile

        # Per-tick volatility is the per-bar figure divided by sqrt(ticks), so that
        # aggregating ticks into a bar reproduces the intended bar volatility
        # instead of inflating it by the tick count.
        tick_volatility = profile.volatility / math.sqrt(self._ticks_per_bar)
        tick_drift = profile.drift / self._ticks_per_bar

        shock = self._random.gauss(0.0, 1.0) * tick_volatility
        pull = 0.0
        if profile.mean_reversion > 0.0 and state.anchor > 0.0:
            # Pull is proportional to the *relative* gap, so it is scale-free. The
            # mean_reversion factor is divided by ticks_per_bar so the per-bar
            # strength matches the profile regardless of tick granularity.
            relative_gap = (state.anchor - state.price) / state.anchor
            pull = relative_gap * profile.mean_reversion / self._ticks_per_bar

        state.price = max(
            0.01, state.price * (1.0 + tick_drift + shock + pull)
        )

        if state.ticks_in_bar == 0:
            state.bar_open = state.price
            state.bar_high = state.price
            state.bar_low = state.price
            state.bar_volume = 0.0
        state.bar_high = max(state.bar_high, state.price)
        state.bar_low = min(state.bar_low, state.price)
        # Volume as a positive integer-ish count scaled by the phase multiplier,
        # with noise so volume percentiles are not degenerate.
        state.bar_volume += max(
            0.0, self._random.gauss(10.0, 3.0) * profile.volume_multiplier
        )
        state.ticks_in_bar += 1

        bar_complete = state.ticks_in_bar >= self._ticks_per_bar
        if bar_complete:
            self._close_bar()

        half_spread = state.price * self._spread_fraction / 2.0
        return MarketSnapshotV1(
            symbol=self._symbol,
            timestamp=now.astimezone(UTC),
            bid=state.price - half_spread,
            ask=state.price + half_spread,
            open=state.bar_open,
            high=state.bar_high,
            low=state.bar_low,
            close=state.price,
            tick_volume=state.bar_volume,
            synthetic=True,
        )

    def _close_bar(self) -> None:
        """Finalise the current bar and advance the scenario phase."""
        state = self._state
        self._completed_bars.append(
            (state.bar_open, state.bar_high, state.bar_low, state.price, state.bar_volume)
        )
        if len(self._completed_bars) > 5_000:
            # Bounded: a 7-day endurance run would otherwise accumulate ~10 000
            # bars of history nobody reads. §64 watches for exactly this.
            del self._completed_bars[: len(self._completed_bars) - 5_000]

        state.ticks_in_bar = 0
        state.bars_in_phase += 1
        state.bars_total += 1
        state.history.append(state.price)
        if len(state.history) > 1_000:
            del state.history[: len(state.history) - 1_000]

        phases = _phases(state.scenario)
        current = phases[min(state.phase_index, len(phases) - 1)]
        if current.bars is not None and state.bars_in_phase >= current.bars:
            state.bars_in_phase = 0
            if state.phase_index + 1 < len(phases):
                state.phase_index += 1
            elif state.scenario in LOOPING_SCENARIOS:
                state.phase_index = 0
                state.anchor = state.price
            # Otherwise hold the final phase.

        # Re-anchor mean-reverting phases to the current price periodically, so a
        # long range drifts naturally instead of being pinned to a stale level for
        # a week.
        if current.mean_reversion > 0.0 and state.bars_total % 120 == 0:
            state.anchor = state.price

    # -- inspection --------------------------------------------------------

    def warm_up(self, bars: int, start: datetime, bar_seconds: int = 60) -> list[MarketSnapshotV1]:
        """Generate ``bars`` complete bars of history in one call.

        Used by tests and by the Market Lab to populate the feature engine's
        rolling windows without waiting. Timestamps advance by one tick interval so
        the resulting series is contiguous.
        """
        if bars < 1:
            raise ValueError("bars must be at least 1")
        interval = timedelta(seconds=bar_seconds / self._ticks_per_bar)
        snapshots: list[MarketSnapshotV1] = []
        moment = start
        for _ in range(bars * self._ticks_per_bar):
            snapshots.append(self.next_tick(moment))
            moment += interval
        return snapshots

    @property
    def completed_bars(self) -> tuple[tuple[float, float, float, float, float], ...]:
        """Closed bars as ``(open, high, low, close, volume)``, oldest first."""
        return tuple(self._completed_bars)

    def realised_volatility(self, bars: int = 30) -> float:
        """Standard deviation of recent bar returns — a self-check for tests.

        Lets a scenario test assert its own statistical signature ("compression
        reduces volatility") against the path actually produced, rather than
        against the parameters that were meant to produce it.
        """
        closes = [bar[3] for bar in self._completed_bars[-(bars + 1) :]]
        if len(closes) < 3:
            return 0.0
        returns = [
            (closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes))
            if closes[i - 1] > 0
        ]
        if len(returns) < 2:
            return 0.0
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / len(returns)
        return math.sqrt(variance)


__all__ = [
    "BASE_VOLATILITY",
    "LOOPING_SCENARIOS",
    "UI_SCENARIOS",
    "MarketSimulationEngine",
    "Scenario",
    "ScenarioProfile",
    "SimulationState",
]
