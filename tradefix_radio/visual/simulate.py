"""The behaviour simulator: run the director for simulated hours, without artwork.

The brief's requirement, and the reason the director lives in Python: a 24-hour
behavioural soak has to complete in seconds, be reproducible from a seed, and produce
numbers a human can argue with. None of that is practical in a browser.

What it measures, and why each one
----------------------------------
``action counts`` / ``category distribution``  does any family dominate
``longest repetition`` / ``trigram share``     is there a visible loop
``coffee`` / ``headphone`` / ``posture`` rates is the pacing plausible
``blink intervals``                            is the distribution varying
``gaze distribution``                          are his eyes on his work
``lock violations``                            **must be zero** — structural
``impossible overlaps``                        **must be zero** — structural
``state transitions``                          is `EXECUTING` always deliberated

The two structural counters are the important ones. Everything else is a judgement call
a human reviews; those two are invariants, and a non-zero value is a bug rather than a
tuning question.

Nothing here renders. There is no artwork, and that is the point — `BehaviorDirector` is
pure logic and the whole of this module is a harness around it.
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Final

from tradefix_radio.core.clock import UTC, VirtualClock
from tradefix_radio.visual.catalog import (
    CATALOG,
    CHAINS,
    is_body_maintenance,
    is_major_posture,
    spec,
)
from tradefix_radio.visual.contracts import (
    NO_ACTIVE_MARKET,
    ActionCategory,
    CharacterActionV1,
    CharacterState,
    FeedTrust,
    GazeTarget,
    IntensityBand,
    InteractionLock,
    MarketDirection,
    StationMode,
    VisualStateV1,
)
from tradefix_radio.visual.director import (
    EXECUTING_PREDECESSORS,
    TICK_SECONDS,
    BehaviorDirector,
)
from tradefix_radio.visual.geometry import Blockout, default_blockout
from tradefix_radio.visual.modulation import behavior_energy

# ============================================================ scenarios


@dataclass(frozen=True, slots=True)
class Scenario:
    """One set of simulated conditions.

    Deliberately expressed as a `VisualStateV1` factory rather than as market inputs:
    the simulator exercises the *director*, and feeding it a market would mean
    re-implementing the bridge. The bridge has its own tests.
    """

    name: str
    band: IntensityBand
    market_energy: float | None
    energy_velocity: float
    direction: MarketDirection
    confidence: float
    bpm: int | None
    music_energy: float | None
    genre: str | None
    symbol: str = "XAUUSD"
    feed_trust: FeedTrust = FeedTrust.LIVE
    station_mode: StationMode = StationMode.NORMAL
    broadcasting: bool = True
    #: Salience fed each tick. Low by default; spikes are injected separately.
    base_salience: float = 0.05

    def state(self, at: datetime, *, salience: float | None = None) -> VisualStateV1:
        effective = self.base_salience if salience is None else salience
        if self.feed_trust is FeedTrust.STALE:
            effective = 0.0
        return VisualStateV1(
            at=at,
            source_age_seconds=0.5,
            feed_trust=self.feed_trust,
            degraded_reason=None if self.feed_trust is FeedTrust.LIVE else "simulated degradation",
            active_symbol=self.symbol,
            market_regime=None if self.symbol == NO_ACTIVE_MARKET else "simulated",
            intensity_band=self.band,
            market_energy=self.market_energy,
            market_energy_velocity=self.energy_velocity,
            market_direction=self.direction,
            market_confidence=self.confidence,
            session="simulated",
            music_bpm=self.bpm,
            music_energy=self.music_energy,
            music_genre=self.genre,
            track_progress=0.4,
            station_mode=self.station_mode,
            broadcasting=self.broadcasting,
            behavior_energy=behavior_energy(
                band=self.band,
                market_energy=self.market_energy,
                energy_velocity=self.energy_velocity,
                music_energy=self.music_energy,
            ),
            reaction_salience=effective,
        )


#: The scenarios the brief names, plus the three the acceptance criteria require.
SCENARIOS: Final[dict[str, Scenario]] = {
    "quiet": Scenario(
        "quiet", IntensityBand.B1_QUIET, 16.0, 0.2, MarketDirection.NEUTRAL, 0.80,
        92, 0.32, "lofi",
    ),
    "range": Scenario(
        "range", IntensityBand.B2_STEADY, 46.0, 0.5, MarketDirection.NEUTRAL, 0.74,
        108, 0.48, "jazzhop",
    ),
    "trend": Scenario(
        "trend", IntensityBand.B3_FOCUSED, 62.0, 2.1, MarketDirection.BULLISH, 0.78,
        128, 0.62, "uk_trap", base_salience=0.18,
    ),
    "breakout": Scenario(
        "breakout", IntensityBand.B4_ALERT, 79.0, 4.6, MarketDirection.BULLISH, 0.82,
        148, 0.80, "dnb", base_salience=0.26,
    ),
    "extreme_volatility": Scenario(
        "extreme_volatility", IntensityBand.B5_PEAK, 94.0, 7.2, MarketDirection.BEARISH,
        0.86, 168, 0.90, "dnb", base_salience=0.34,
    ),
    "low_bpm": Scenario(
        "low_bpm", IntensityBand.B2_STEADY, 44.0, 0.4, MarketDirection.NEUTRAL, 0.72,
        72, 0.28, "ambient",
    ),
    "high_bpm": Scenario(
        "high_bpm", IntensityBand.B2_STEADY, 44.0, 0.4, MarketDirection.NEUTRAL, 0.72,
        174, 0.88, "dnb",
    ),
    "btcusd": Scenario(
        "btcusd", IntensityBand.B3_FOCUSED, 58.0, 1.8, MarketDirection.BULLISH, 0.70,
        134, 0.64, "uk_trap", symbol="BTCUSD",
    ),
    "no_active_market": Scenario(
        "no_active_market", IntensityBand.B0_DORMANT, None, 0.0, MarketDirection.NEUTRAL,
        0.0, 88, 0.30, "lofi", symbol=NO_ACTIVE_MARKET, feed_trust=FeedTrust.STALE,
    ),
    "feed_down": Scenario(
        "feed_down", IntensityBand.B0_DORMANT, None, 0.0, MarketDirection.NEUTRAL, 0.0,
        None, None, None, symbol=NO_ACTIVE_MARKET, feed_trust=FeedTrust.STALE,
        station_mode=StationMode.OFFLINE, broadcasting=False,
    ),
}

#: Named durations, seconds. The brief's four, plus a short one for fast iteration.
DURATIONS: Final[dict[str, float]] = {
    "5m": 300.0,
    "30m": 1_800.0,
    "2h": 7_200.0,
    "8h": 28_800.0,
    "24h": 86_400.0,
}


# ============================================================ events


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    """One line of the human-readable timeline."""

    at: datetime
    monotonic: float
    kind: str
    detail: str
    character_state: CharacterState

    def format(self) -> str:
        stamp = self.at.strftime("%H:%M:%S")
        return f"{stamp}  {self.kind:<12} {self.detail}"


# ============================================================ report


@dataclass
class SimulationReport:
    """Everything one run measured. Printable, and assertable in tests."""

    scenario: str
    duration_seconds: float
    seed: int
    ticks: int = 0

    actions_total: int = 0
    deliberate_total: int = 0
    blinks_total: int = 0
    gaze_shifts_total: int = 0
    state_changes: int = 0

    action_counts: Counter[str] = field(default_factory=Counter)
    category_counts: Counter[str] = field(default_factory=Counter)
    gaze_counts: Counter[str] = field(default_factory=Counter)
    state_counts: Counter[str] = field(default_factory=Counter)
    transitions: Counter[tuple[str, str]] = field(default_factory=Counter)

    chains_completed: Counter[str] = field(default_factory=Counter)
    reactions: int = 0
    reactions_deferred: int = 0
    reactions_dropped: int = 0

    blink_interval_mean: float | None = None
    blink_interval_min: float | None = None
    blink_interval_max: float | None = None

    # -- the long-run work rhythm (fix 2). The brief's requested statistics.
    fatigue_min: float = 0.0
    fatigue_max: float = 0.0
    fatigue_mean: float = 0.0
    fatigue_turning_points: int = 0
    fatigue_cycles: int = 0
    fatigue_time_near_floor: float = 0.0
    fatigue_time_near_ceiling: float = 0.0
    fatigue_pinned_ceiling: bool = False
    fatigue_pinned_floor: bool = False
    work_phase_shares: dict[str, float] = field(default_factory=dict)
    cycle_durations_hours: list[float] = field(default_factory=list)

    # -- body maintenance (fix 1)
    posture_major_total: int = 0
    posture_minor_total: int = 0
    posture_during_chain: list[str] = field(default_factory=list)
    posture_during_reaction: list[str] = field(default_factory=list)

    # -- the two structural invariants. Both must be zero.
    lock_violations: list[str] = field(default_factory=list)
    overlap_violations: list[str] = field(default_factory=list)
    illegal_transitions: list[str] = field(default_factory=list)
    unreachable_anchors: list[str] = field(default_factory=list)
    unknown_gaze_targets: list[str] = field(default_factory=list)
    cooldown_violations: list[str] = field(default_factory=list)

    # -- repetition
    longest_identical_run: int = 1
    back_to_back_repeats: int = 0
    top_trigram: tuple[str, str, str] | None = None
    top_trigram_count: int = 0
    top_trigram_share: float = 0.0
    trigram_diversity: float = 0.0
    distinct_trigrams: int = 0

    fatigue_phase_final: float = 0.0
    behavior_energy_mean: float = 0.0

    # -- derived rates -----------------------------------------------------

    @property
    def hours(self) -> float:
        return self.duration_seconds / 3600.0

    def per_hour(self, action_id: str) -> float:
        return self.action_counts.get(action_id, 0) / max(1e-9, self.hours)

    def category_per_hour(self, category: ActionCategory) -> float:
        return self.category_counts.get(category.value, 0) / max(1e-9, self.hours)

    def category_share(self, category: ActionCategory) -> float:
        total = sum(self.category_counts.values())
        return self.category_counts.get(category.value, 0) / max(1, total)

    @property
    def coffee_per_hour(self) -> float:
        return self.chains_completed.get("COFFEE_DRINK", 0) / max(1e-9, self.hours)

    @property
    def headphone_per_hour(self) -> float:
        return self.category_per_hour(ActionCategory.HEADPHONES)

    @property
    def posture_major_per_hour(self) -> float:
        """Major body-maintenance resets per hour. The brief: several, not dozens."""
        return self.posture_major_total / max(1e-9, self.hours)

    @property
    def posture_minor_per_hour(self) -> float:
        return self.posture_minor_total / max(1e-9, self.hours)

    @property
    def posture_per_hour(self) -> float:
        return (self.posture_major_total + self.posture_minor_total) / max(1e-9, self.hours)

    @property
    def fatigue_cycles_per_day(self) -> float:
        return self.fatigue_turning_points / 2.0 / max(1e-9, self.hours / 24.0)

    @property
    def rhythm_is_healthy(self) -> bool:
        """Whether the long-run rhythm rises and falls as the brief requires.

        Four failure modes, all of which the V6 accumulator hit at least one of:
        monotonic rise, pinned at ceiling, pinned at floor, and — the one easy to miss —
        oscillating on a fixed schedule. The last is checked as spread in cycle length,
        because a metronome and a rhythm look identical in min/max/mean.
        """
        if self.hours < 4.0:
            return True  # too short for a cycle to complete; not a judgement
        if self.fatigue_pinned_ceiling or self.fatigue_pinned_floor:
            return False
        if self.fatigue_turning_points < 2:
            return False  # never came back down
        if self.fatigue_time_near_ceiling > 0.30:
            return False
        if len(self.cycle_durations_hours) >= 3:
            spread = max(self.cycle_durations_hours) - min(self.cycle_durations_hours)
            if spread < 0.15:
                return False  # a metronome, not a rhythm
        return True

    @property
    def gaze_screen_share(self) -> float:
        """Fraction of gaze shifts landing on a screen surface.

        The headline gaze number: 89 % is what a working trader's eyes do, and it is
        what makes the remaining 11 % read correctly.
        """
        total = sum(self.gaze_counts.values())
        if not total:
            return 0.0
        # Derived from the blockout rather than restated, so a target that stops being a
        # screen cannot silently keep counting as one.
        screens = default_blockout().screen_targets()
        hits = sum(self.gaze_counts.get(target.value, 0) for target in screens)
        return hits / total

    @property
    def camera_gaze_share(self) -> float:
        total = sum(self.gaze_counts.values())
        return self.gaze_counts.get(GazeTarget.CAMERA.value, 0) / max(1, total)

    @property
    def is_structurally_sound(self) -> bool:
        """Whether every invariant held. A `False` here is a bug, not a tuning note."""
        return not (
            self.lock_violations
            or self.overlap_violations
            or self.illegal_transitions
            or self.unreachable_anchors
            or self.unknown_gaze_targets
            or self.cooldown_violations
        )

    @property
    def has_visible_loop(self) -> bool:
        """Whether a reviewer would plausibly notice a cycle.

        Thresholds taken from the soak rather than from theory. With 51 schedulable
        actions and a working penalty the most common triple lands near 1 %, so 4 % is
        comfortably abnormal without being so loose a real regression slips through.
        """
        return (
            self.top_trigram_share > 0.04
            or self.longest_identical_run > 1
            or self.back_to_back_repeats > 0
            or (self.distinct_trigrams > 20 and self.trigram_diversity < 0.25)
        )

    # -- presentation ------------------------------------------------------

    def summary(self) -> str:
        lines = [
            f"scenario           {self.scenario}",
            f"duration           {self.hours:.2f} h  ({self.ticks:,} ticks, seed {self.seed})",
            "",
            f"actions            {self.actions_total:,} total "
            f"({self.deliberate_total:,} deliberate, {self.blinks_total:,} blinks)",
            f"deliberate rate    {self.deliberate_total / max(1e-9, self.hours):.1f} / h",
            f"gaze shifts        {self.gaze_shifts_total:,} "
            f"({self.gaze_shifts_total / max(1e-9, self.hours):.1f} / h)",
            f"state changes      {self.state_changes:,}",
            f"distinct actions   {len(self.action_counts)} of {len(CATALOG)} catalogued",
            "",
            "-- pacing",
            f"coffee             {self.coffee_per_hour:.2f} / h",
            f"headphones         {self.headphone_per_hour:.2f} / h",
            f"posture major      {self.posture_major_per_hour:.2f} / h",
            f"posture minor      {self.posture_minor_per_hour:.2f} / h",
            f"reactions          {self.reactions} "
            f"({self.reactions / max(1e-9, self.hours):.2f} / h, "
            f"{self.reactions_deferred} deferred, {self.reactions_dropped} dropped)",
            f"behaviour energy   {self.behavior_energy_mean:.3f} mean",
            "",
            "-- long-run work rhythm",
            f"fatigue            min {self.fatigue_min:.3f}  max {self.fatigue_max:.3f}  "
            f"mean {self.fatigue_mean:.3f}  end {self.fatigue_phase_final:.3f}",
            f"cycles             {self.fatigue_cycles} completed, "
            f"{self.fatigue_turning_points} turning points",
            f"cycle lengths (h)  {self.cycle_durations_hours}",
            f"near floor         {self.fatigue_time_near_floor * 100:.1f} %   "
            f"near ceiling {self.fatigue_time_near_ceiling * 100:.1f} %",
            f"pinned             ceiling={self.fatigue_pinned_ceiling}  "
            f"floor={self.fatigue_pinned_floor}",
            f"phase shares       {self.work_phase_shares}",
            f"rhythm healthy?    {'yes' if self.rhythm_is_healthy else 'NO - INVESTIGATE'}",
            "",
            "-- gaze",
            f"on screens         {self.gaze_screen_share * 100:.1f} %  (target 89 %)",
            f"at camera          {self.camera_gaze_share * 100:.2f} %  (cap 0.6 %)",
        ]
        if self.blink_interval_mean is not None:
            lines.append(
                f"blink interval     {self.blink_interval_mean:.2f} s mean "
                f"({self.blink_interval_min:.2f}-{self.blink_interval_max:.2f} s)"
            )

        lines += ["", "-- category distribution"]
        total = max(1, sum(self.category_counts.values()))
        for name, count in self.category_counts.most_common():
            bar = "#" * max(1, round(40 * count / total))
            lines.append(f"  {name:<11} {count / total * 100:5.1f} %  {count:>6,}  {bar}")

        lines += ["", "-- repetition"]
        if self.top_trigram:
            triple = " -> ".join(self.top_trigram)
            lines.append(
                f"top triple         {triple}  x{self.top_trigram_count} "
                f"({self.top_trigram_share * 100:.2f} % of all triples)"
            )
        lines += [
            f"distinct triples   {self.distinct_trigrams:,} "
            f"(diversity {self.trigram_diversity:.3f})",
            f"longest same-run   {self.longest_identical_run}",
            f"back-to-back       {self.back_to_back_repeats}",
            f"visible loop?      {'YES - INVESTIGATE' if self.has_visible_loop else 'no'}",
            "",
            "-- invariants",
        ]
        for label, failures in (
            ("lock violations", self.lock_violations),
            ("impossible overlaps", self.overlap_violations),
            ("illegal transitions", self.illegal_transitions),
            ("unreachable anchors", self.unreachable_anchors),
            ("unknown gaze targets", self.unknown_gaze_targets),
            ("cooldown violations", self.cooldown_violations),
        ):
            mark = "ok" if not failures else f"FAIL ({len(failures)})"
            lines.append(f"  {label:<22} {mark}")
            for failure in failures[:5]:
                lines.append(f"      {failure}")
        lines.append("")
        lines.append(
            "RESULT             "
            + ("STRUCTURALLY SOUND" if self.is_structurally_sound else "INVARIANT BROKEN")
        )
        return "\n".join(lines)

    def top_actions(self, limit: int = 20) -> str:
        lines = ["-- most frequent actions"]
        for action_id, count in self.action_counts.most_common(limit):
            lines.append(f"  {action_id:<24} {count:>7,}  {self.per_hour(action_id):>8.1f} / h")
        return "\n".join(lines)


# ============================================================ the simulator


class BehaviorSimulator:
    """Drives a `BehaviorDirector` through simulated time and measures it.

    Uses `VirtualClock.advance_sync`: a behaviour soak is pure computation with nothing
    awaiting, so there are no sleepers to wake and the synchronous path is both correct
    and the fastest available.
    """

    def __init__(
        self,
        scenario: Scenario,
        *,
        seed: int = 1,
        blockout: Blockout | None = None,
        start: datetime | None = None,
        collect_timeline: bool = False,
        timeline_limit: int = 4000,
        salience_spike_every_seconds: float = 0.0,
        salience_spike_value: float = 0.85,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        self.scenario = scenario
        self.seed = seed
        self._geometry = blockout or default_blockout()
        self._clock = VirtualClock(start=start or datetime(2026, 10, 3, 21, 0, tzinfo=UTC))
        self._rng = random.Random(seed)  # noqa: S311 - reproducibility, not security
        self.director = BehaviorDirector(
            blockout=self._geometry, clock=self._clock, rng=self._rng
        )
        self.collect_timeline = collect_timeline
        self.timeline_limit = timeline_limit
        self.timeline: list[TimelineEvent] = []
        self._spike_every = salience_spike_every_seconds
        self._spike_value = salience_spike_value
        #: Sampling interval. Defaults to the director's own 20 Hz; a long soak may
        #: coarsen it, because the director's decisions land on 1.4-4.2 s intervals and
        #: 4 Hz samples those amply at a fifth of the cost. Coarsening changes reaction
        #: latency, not the statistics, so it is right for a soak and wrong for a
        #: latency test.
        self._tick_seconds = tick_seconds

        self._known_anchors = frozenset(self._geometry.anchors)
        self._known_gaze = set(self._geometry.gaze)
        self._chain_steps = {
            step for chain in CHAINS.values() for step in chain.steps
        }
        #: Object locks held at the moment the current action started. Recomputed per
        #: action so the posture-safety check reads the state the gate would have seen.
        self._held_objects: frozenset[InteractionLock] = frozenset()

    def run(self, duration_seconds: float) -> SimulationReport:
        report = SimulationReport(
            scenario=self.scenario.name,
            duration_seconds=duration_seconds,
            seed=self.seed,
        )
        director = self.director
        trigrams: list[tuple[str, str, str]] = []
        previous_deliberate: list[str] = []
        energy_sum = 0.0
        last_spike = 0.0
        # Actions in flight, for the overlap check: action_id -> (locks, ends_monotonic)
        in_flight: dict[str, tuple[frozenset[InteractionLock], float]] = {}
        armed_cooldowns: dict[str, float] = {}

        elapsed = 0.0
        while elapsed < duration_seconds:
            now = self._clock.monotonic()
            salience: float | None = None
            if self._spike_every > 0.0 and now - last_spike >= self._spike_every:
                salience = self._spike_value
                last_spike = now

            state = self.scenario.state(self._clock.now(), salience=salience)
            before = director.character_state
            output = director.tick(state)
            report.ticks += 1
            energy_sum += director.behavior_energy

            # retire finished in-flight entries before checking the new ones
            for action_id in [a for a, (_, ends) in in_flight.items() if ends <= now]:
                del in_flight[action_id]

            for action in output.actions:
                self._record_action(
                    action, report, now, in_flight, armed_cooldowns, previous_deliberate, trigrams
                )
                # Action-forced gaze is gaze. Counting only GazeShiftV1 understated the
                # screen share, because most forced targets are the main chart.
                if action.gaze_target is not None:
                    report.gaze_counts[action.gaze_target.value] += 1
                    report.gaze_shifts_total += 1

            for shift in output.gaze_shifts:
                report.gaze_shifts_total += 1
                report.gaze_counts[shift.target.value] += 1
                if shift.target not in self._known_gaze:
                    report.unknown_gaze_targets.append(shift.target.value)
                if shift.transit_ms < self._geometry.min_gaze_transit_ms:
                    report.overlap_violations.append(
                        f"gaze to {shift.target.value} transited in {shift.transit_ms} ms, "
                        f"below the {self._geometry.min_gaze_transit_ms} ms floor"
                    )

            if output.state_changed is not None:
                report.state_changes += 1
                report.transitions[(before.value, output.state_changed.value)] += 1
                if (
                    output.state_changed is CharacterState.EXECUTING
                    and before not in EXECUTING_PREDECESSORS
                ):
                    permitted = ", ".join(sorted(s.value for s in EXECUTING_PREDECESSORS))
                    report.illegal_transitions.append(
                        f"EXECUTING entered from {before.value}; permitted: {permitted}"
                    )

            for note in output.notes:
                if note.startswith("chain complete "):
                    report.chains_completed[note.removeprefix("chain complete ")] += 1
                elif note == "reaction fired":
                    report.reactions += 1
                elif note.startswith("reaction deferred"):
                    report.reactions_deferred += 1
                elif note.startswith("deferred reaction dropped"):
                    report.reactions_dropped += 1
                if self.collect_timeline and len(self.timeline) < self.timeline_limit:
                    self.timeline.append(
                        TimelineEvent(
                            at=self._clock.now(), monotonic=now, kind="note",
                            detail=note, character_state=director.character_state,
                        )
                    )

            report.state_counts[director.character_state.value] += 1
            self._clock.advance_sync(self._tick_seconds)
            elapsed += self._tick_seconds

        self._finalise(report, trigrams, energy_sum)
        return report

    # -- per-action bookkeeping -------------------------------------------

    def _record_action(
        self,
        action: CharacterActionV1,
        report: SimulationReport,
        now: float,
        in_flight: dict[str, tuple[frozenset[InteractionLock], float]],
        armed_cooldowns: dict[str, float],
        previous_deliberate: list[str],
        trigrams: list[tuple[str, str, str]],
    ) -> None:
        report.actions_total += 1
        report.action_counts[action.action_id] += 1
        report.category_counts[action.category.value] += 1

        is_blink = action.action_id in ("blink", "double_blink", "slow_blink")
        if is_blink:
            report.blinks_total += 1
        else:
            report.deliberate_total += 1

        definition = spec(action.action_id)

        # -- body maintenance: count it, and prove it never lands inside an object chain
        # or a reaction. Both are conditions of the posture gate, so a hit here means
        # the gate was bypassed rather than that the tuning is off.
        if is_body_maintenance(action.action_id):
            if is_major_posture(action.action_id):
                report.posture_major_total += 1
            else:
                report.posture_minor_total += 1
            if InteractionLock.COFFEE in self._held_objects or (
                InteractionLock.PEN in self._held_objects
            ):
                report.posture_during_chain.append(action.action_id)
            if action.character_state is CharacterState.MARKET_REACTION:
                report.posture_during_reaction.append(action.action_id)

        # -- anchors and gaze must resolve to frozen geometry
        if definition.required_anchor and definition.required_anchor not in self._known_anchors:
            report.unreachable_anchors.append(
                f"{action.action_id} -> {definition.required_anchor}"
            )
        if action.gaze_target is not None and action.gaze_target not in self._known_gaze:
            report.unknown_gaze_targets.append(
                f"{action.action_id} -> {action.gaze_target.value}"
            )

        # -- the lock invariant: no two in-flight actions may share a lock
        claimed = frozenset(action.locks)
        for other_id, (other_locks, _) in in_flight.items():
            overlap = claimed & other_locks
            if overlap:
                names = ", ".join(sorted(lock.value for lock in overlap))
                report.lock_violations.append(
                    f"{action.action_id} and {other_id} both hold {names}"
                )
        if claimed:
            in_flight[action.action_id] = (claimed, action.ends_at_monotonic(now))
        self._held_objects = frozenset(
            lock
            for locks, _ in ((v[0], v[1]) for v in in_flight.values())
            for lock in locks
            if lock in (InteractionLock.COFFEE, InteractionLock.PEN)
        )

        # -- the overlap invariant: conflicting body regions
        self._check_body_conflicts(action, claimed, in_flight, report)

        # -- cooldowns. Two exemptions, both principled:
        #
        # Chain steps run back to back by design; arming a cooldown between them would
        # stall the chain. Reflex blinks are timed by the BlinkDriver's own log-normal
        # distribution, not by the CooldownTable, and a blink forced across a large
        # saccade deliberately bypasses the interval — that is required realism, not a
        # violation. Their spec cooldown range documents the distribution rather than
        # gating it.
        is_reflex = "reflex" in definition.tags
        if not definition.chain_only and not is_reflex and (
            action.action_id not in self._chain_steps
        ):
            previous = armed_cooldowns.get(action.action_id)
            low, _ = definition.cooldown_range_seconds
            if previous is not None and low > 0.0 and now - previous < low - 1e-6:
                report.cooldown_violations.append(
                    f"{action.action_id} repeated after {now - previous:.1f} s, "
                    f"below its {low:.1f} s floor"
                )
            armed_cooldowns[action.action_id] = now

        # -- repetition, over deliberate actions only. Blinks are reflexes and would
        # swamp the signal the anti-repetition rules exist to police.
        if not is_blink:
            if previous_deliberate and previous_deliberate[-1] == action.action_id:
                report.back_to_back_repeats += 1
            previous_deliberate.append(action.action_id)
            if len(previous_deliberate) >= 3:
                trigrams.append(tuple(previous_deliberate[-3:]))  # type: ignore[arg-type]
            if len(previous_deliberate) > 4096:
                del previous_deliberate[:2048]

        if self.collect_timeline and len(self.timeline) < self.timeline_limit:
            detail = action.action_id
            if action.chain_id:
                detail = f"{action.action_id}  [{action.chain_id} {action.chain_step}]"
            if action.gaze_target:
                detail += f"  gaze->{action.gaze_target.value}"
            self.timeline.append(
                TimelineEvent(
                    at=action.started_at, monotonic=now,
                    kind="blink" if is_blink else action.category.value,
                    detail=detail, character_state=action.character_state,
                )
            )

    def _check_body_conflicts(
        self,
        action: CharacterActionV1,
        claimed: frozenset[InteractionLock],
        in_flight: dict[str, tuple[frozenset[InteractionLock], float]],
        report: SimulationReport,
    ) -> None:
        """The brief's named impossible combinations, checked explicitly.

        The lock check above already covers these, which is the point — this is a second,
        independent assertion written in the brief's own terms, so a bug in the lock
        expansion cannot hide behind the lock check that uses it.
        """
        held: set[InteractionLock] = set()
        for other_id, (other_locks, _) in in_flight.items():
            if other_id == action.action_id:
                continue
            held |= other_locks

        if InteractionLock.COFFEE in held and InteractionLock.KEYBOARD in claimed:
            report.overlap_violations.append(
                f"{action.action_id} types while the mug is held"
            )
        if InteractionLock.PEN in held and InteractionLock.MOUSE in claimed:
            report.overlap_violations.append(
                f"{action.action_id} uses the mouse while the pen is held"
            )
        if InteractionLock.COFFEE in held and InteractionLock.PEN in claimed:
            report.overlap_violations.append(
                f"{action.action_id} starts a pen chain while the mug is held"
            )
        if InteractionLock.PEN in held and InteractionLock.COFFEE in claimed:
            report.overlap_violations.append(
                f"{action.action_id} starts a coffee chain while the pen is held"
            )

    def _finalise(
        self, report: SimulationReport, trigrams: list[tuple[str, str, str]], energy_sum: float
    ) -> None:
        counts = Counter(trigrams)
        if counts:
            triple, count = counts.most_common(1)[0]
            report.top_trigram = triple
            report.top_trigram_count = count
            report.top_trigram_share = count / len(trigrams)
            report.trigram_diversity = len(counts) / len(trigrams)
            report.distinct_trigrams = len(counts)

        runs: dict[str, int] = defaultdict(int)
        longest = 1
        previous: str | None = None
        run = 0
        for triple in trigrams:
            current = triple[-1]
            run = run + 1 if current == previous else 1
            longest = max(longest, run)
            runs[current] = max(runs[current], run)
            previous = current
        report.longest_identical_run = longest

        trace = self.director.trace
        rhythm = self.director.rhythm
        report.fatigue_min = trace.minimum
        report.fatigue_max = trace.maximum
        report.fatigue_mean = trace.mean
        report.fatigue_turning_points = trace.turning_points()
        report.fatigue_cycles = rhythm.cycles_completed
        report.fatigue_time_near_floor = trace.time_near_floor
        report.fatigue_time_near_ceiling = trace.time_near_ceiling
        report.fatigue_pinned_ceiling = trace.pinned_at_ceiling
        report.fatigue_pinned_floor = trace.pinned_at_floor
        report.work_phase_shares = trace.phase_shares()
        report.cycle_durations_hours = [
            round(seconds / 3600.0, 3) for seconds in rhythm.cycle_durations
        ]

        stats = self.director.blink.interval_stats()
        if stats is not None:
            report.blink_interval_mean, report.blink_interval_min, report.blink_interval_max = stats

        report.fatigue_phase_final = rhythm.fatigue_level
        report.behavior_energy_mean = energy_sum / max(1, report.ticks)

    # -- timeline ----------------------------------------------------------

    def format_timeline(self, limit: int = 60, skip: int = 0) -> str:
        """The brief's debug representation.

        Useful precisely because it is readable before any artwork exists: a reviewer can
        judge whether the *behaviour* feels human from the text alone.
        """
        rows = self.timeline[skip : skip + limit]
        if not rows:
            return "(no timeline collected; construct the simulator with collect_timeline=True)"
        lines = []
        state: CharacterState | None = None
        for event in rows:
            if event.character_state is not state:
                state = event.character_state
                lines.append(f"{'':<10}  -- {state.value.upper()}")
            lines.append(event.format())
        return "\n".join(lines)


def run_scenario(
    scenario_name: str,
    duration: str | float = "30m",
    *,
    seed: int = 1,
    collect_timeline: bool = False,
    salience_spike_every_seconds: float = 0.0,
    tick_seconds: float = TICK_SECONDS,
) -> tuple[SimulationReport, BehaviorSimulator]:
    """Convenience entry point for the CLI and tests."""
    if scenario_name not in SCENARIOS:
        available = ", ".join(sorted(SCENARIOS))
        raise KeyError(f"unknown scenario {scenario_name!r}; available: {available}")
    seconds = DURATIONS[duration] if isinstance(duration, str) else float(duration)
    simulator = BehaviorSimulator(
        SCENARIOS[scenario_name],
        seed=seed,
        collect_timeline=collect_timeline,
        salience_spike_every_seconds=salience_spike_every_seconds,
        tick_seconds=tick_seconds,
    )
    return simulator.run(seconds), simulator


def symbol_switch_run(
    *, duration_seconds: float = 1_200.0, seed: int = 1, switch_at_seconds: float = 600.0
) -> tuple[SimulationReport, list[str], list[str]]:
    """Run across a XAUUSD -> BTCUSD switch and return the action ids either side.

    Exists to assert the brief's requirement directly: *"When symbol changes, do NOT
    reset character."* The two lists let a test check that behaviour continued rather
    than restarting — no state reset, no history clear, no cooldown reset.
    """
    before_scenario = SCENARIOS["trend"]
    after_scenario = replace(before_scenario, symbol="BTCUSD")

    simulator = BehaviorSimulator(before_scenario, seed=seed)
    director = simulator.director
    clock = simulator._clock
    before: list[str] = []
    after: list[str] = []
    report = SimulationReport(
        scenario="symbol_switch", duration_seconds=duration_seconds, seed=seed
    )

    elapsed = 0.0
    while elapsed < duration_seconds:
        scenario = before_scenario if elapsed < switch_at_seconds else after_scenario
        output = director.tick(scenario.state(clock.now()))
        for action in output.actions:
            bucket = before if elapsed < switch_at_seconds else after
            bucket.append(action.action_id)
            report.actions_total += 1
            report.action_counts[action.action_id] += 1
        clock.advance_sync(TICK_SECONDS)
        elapsed += TICK_SECONDS
    return report, before, after


__all__ = [
    "DURATIONS",
    "SCENARIOS",
    "BehaviorSimulator",
    "Scenario",
    "SimulationReport",
    "TimelineEvent",
    "run_scenario",
    "symbol_switch_run",
]
