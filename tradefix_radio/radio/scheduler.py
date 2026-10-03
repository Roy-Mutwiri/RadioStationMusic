"""The scheduler: how much to make, what to make, and when to replan (§26–§29, §94, milestone 4.5).

Built independently of the playout engine, and the separation is the point. The scheduler decides
*what should exist*; the playout engine plays *what does exist*. They share only the queue. A
scheduler that could reach into playback would eventually be asked to "just skip this one", and
the audio path has to stay boring.

Its whole job in one sentence: keep enough playable audio ahead of the listener, make it fit the
market, and never disturb what is already committed.

Four responsibilities, in priority order — the order matters because they conflict:

1. **Survival.** Keep the buffer above §26's minimum. Everything else yields to this.
2. **Fit.** Replan flexible programming when the market changes materially (§28, §29).
3. **Variety.** Preserve §11's diversity through the director's own constraints.
4. **Stability.** Do not oscillate. A station that replans every tick produces a queue nobody
   ever hears, and §98 forbids the audible version of the same thing.

The **Creative Risk Controller** is the coupling §94 asks for, and it is expressed as a
translation rather than a rule: operational health becomes a
:class:`~tradefix_radio.director.temperature.CreativeStance`, and the director already knows how
to be more or less adventurous given one. So "the station becomes conservative before it starves"
needs no new policy here — it falls out of passing the buffer's real state to a component that
was built to respond to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import structlog

from tradefix_radio.config.schema import AppSettings
from tradefix_radio.contracts.enums import GenerationPriority, MarketRegime
from tradefix_radio.contracts.market import MarketStateV1
from tradefix_radio.contracts.queue import BufferHealthV1
from tradefix_radio.core.clock import Clock, SystemClock
from tradefix_radio.director.history import ProgrammingHistory
from tradefix_radio.director.music_director import DirectorDecision, MusicDirector
from tradefix_radio.generation.capacity import CapacitySnapshot
from tradefix_radio.radio.buffer import BufferAssessment, BufferLevel, BufferTrajectory
from tradefix_radio.radio.queue import QueueEntry, RadioQueue, build_entry

_log = structlog.get_logger(__name__)

#: Energy change, 0–100, large enough to justify disturbing soft-locked programming (§28).
#:
#: §29's example is a quiet market that "suddenly breaks out", which is a 40-plus point move. A
#: lower threshold would make an ordinary session drift replan the queue repeatedly, which is the
#: oscillation §98 forbids expressed as scheduling rather than as energy.
MAJOR_SHIFT_ENERGY_DELTA = 32.0

#: Smallest gap between automatic replans, in seconds.
#:
#: Independent of the energy threshold and needed as well as it: a market can cross the threshold
#: repeatedly within a minute, and each crossing is individually justified. This bounds the
#: *rate* rather than the *reason*.
REPLAN_COOLDOWN_SECONDS = 180.0

#: How many tracks ahead the scheduler will plan beyond what the buffer target needs.
#:
#: A small overshoot, because a queue sized exactly to target shrinks below it the moment a track
#: starts playing — so the station would spend its whole life marginally under target and
#: reporting ``LOW``.
TARGET_OVERSHOOT_TRACKS = 1


@dataclass(frozen=True)
class ScheduleDecision:
    """What the scheduler decided to do this cycle, and why.

    Returned rather than acted on silently so the §47 explainability pane and the soak report can
    both read the reasoning. A scheduler whose decisions cannot be inspected is one nobody can
    debug at hour nine of a soak.
    """

    tracks_to_plan: int
    priority: GenerationPriority
    allow_experimental: bool
    replan: bool
    replan_from_position: int | None
    reason: str
    buffer_level: BufferLevel
    buffer_trajectory: BufferTrajectory
    seconds_to_failure: float | None

    @property
    def wants_work(self) -> bool:
        return self.tracks_to_plan > 0


@dataclass
class SchedulerStats:
    """Counters for the soak report."""

    cycles: int = 0
    planned: int = 0
    replans: int = 0
    replans_suppressed: int = 0
    tracks_replaced: int = 0
    station_ids_requested: int = 0
    by_priority: dict[str, int] = field(default_factory=dict)

    def record_priority(self, priority: GenerationPriority, count: int) -> None:
        self.by_priority[priority.value] = (
            self.by_priority.get(priority.value, 0) + count
        )


class Scheduler:
    """Decides how much programming should exist and what it should be."""

    def __init__(
        self,
        settings: AppSettings,
        *,
        director: MusicDirector,
        queue: RadioQueue,
        clock: Clock | None = None,
    ) -> None:
        self._settings = settings
        self._director = director
        self._queue = queue
        self._clock = clock or SystemClock()
        self._stats = SchedulerStats()
        self._last_replan_monotonic: float | None = None
        self._last_regime: MarketRegime | None = None
        self._last_energy: float | None = None
        #: Which symbol the last planning cycle ran against, for market-switch replanning.
        self._last_symbol: str | None = None
        self._tracks_since_station_id = 0
        self._sequence = 0

    # -- introspection -----------------------------------------------------

    @property
    def stats(self) -> SchedulerStats:
        return self._stats

    @property
    def tracks_since_station_id(self) -> int:
        return self._tracks_since_station_id

    def note_station_id_played(self) -> None:
        self._tracks_since_station_id = 0

    def note_track_aired(self) -> None:
        self._tracks_since_station_id += 1

    def seed_sequence(self, sequence: int) -> None:
        """Continue track-id numbering from ``sequence`` (§36, §75).

        Track ids are ``TF-YYYYMMDD-NNNNN`` so they sort by day on disk, which means the
        number is the only unique part within a day. An in-memory counter restarts at 1 on
        every process start, so a station restarted on the same day reissues ids it has
        already used — and the second track with a given id silently collides with the first
        in the tracks table, the play history and the §11 diversity window.

        A soak made it visible: 17 duplicate track ids across a run, every one of them a
        collision with the *previous* run's numbering.
        """
        self._sequence = max(self._sequence, sequence)

    # -- the decision ------------------------------------------------------

    def decide(
        self,
        *,
        assessment: BufferAssessment,
        state: MarketStateV1,
        capacity: CapacitySnapshot,
        provider_healthy: bool = True,
    ) -> ScheduleDecision:
        """Work out what this cycle should do. Pure — no side effects, no queue mutation.

        Pure on purpose. Every interesting scheduling question is "why did it do that at 04:00
        on Tuesday", and a decision that is a value can be logged, replayed and asserted on,
        whereas one that is a set of mutations cannot.
        """
        self._stats.cycles += 1
        deficit = self._tracks_needed(assessment)
        priority = self._priority(assessment, capacity, provider_healthy=provider_healthy)
        allow_experimental = self._allow_experimental(assessment, capacity, provider_healthy)
        replan, from_position, replan_reason = self._should_replan(state, assessment)

        reasons: list[str] = [
            f"buffer {assessment.level.value}/{assessment.trajectory.value} "
            f"({assessment.ready_minutes:.1f}min ready)"
        ]
        if deficit:
            reasons.append(f"planning {deficit} track(s) at {priority.value}")
        if not allow_experimental:
            reasons.append("experimentation withheld")
        if replan:
            reasons.append(replan_reason)
        if assessment.seconds_to_failure is not None:
            reasons.append(f"~{assessment.seconds_to_failure / 60:.0f}min to failure")

        return ScheduleDecision(
            tracks_to_plan=deficit,
            priority=priority,
            allow_experimental=allow_experimental,
            replan=replan,
            replan_from_position=from_position,
            reason="; ".join(reasons),
            buffer_level=assessment.level,
            buffer_trajectory=assessment.trajectory,
            seconds_to_failure=assessment.seconds_to_failure,
        )

    def _tracks_needed(self, assessment: BufferAssessment) -> int:
        """How many more tracks to plan, from the buffer deficit and mean track length.

        Counts **ready plus pending** against the target. Counting only ready audio would make
        the scheduler plan a fresh batch every cycle while the previous batch was still
        generating — a queue of hundreds, each one obsolete by the time it finished.
        """
        average = self._average_track_minutes()
        if average <= 0:
            return 0
        committed = assessment.ready_minutes + assessment.pending_minutes
        wanted = assessment.target_minutes + average * TARGET_OVERSHOOT_TRACKS
        shortfall = wanted - committed
        if shortfall <= 0:
            return 0
        # Respect §26's maximum: a station that queued past it would hold hours of programming
        # planned against a market that no longer exists, and §28 could not replan it because
        # most of it would be soft-locked by the time the market moved.
        headroom = assessment.maximum_minutes - committed
        return max(0, int(min(shortfall, headroom) // average))

    def _average_track_minutes(self) -> float:
        music = self._settings.music
        return (music.min_duration_seconds + music.max_duration_seconds) / 2.0 / 60.0

    def _priority(
        self,
        assessment: BufferAssessment,
        capacity: CapacitySnapshot,
        *,
        provider_healthy: bool,
    ) -> GenerationPriority:
        """§94's priority, from the *worst* applicable condition.

        Worst rather than an average, because operational safety does not average: a full buffer
        with a dead provider is not "moderately healthy", it is a station that will be silent in
        forty-five minutes.
        """
        if assessment.level in {BufferLevel.EMPTY, BufferLevel.CRITICAL}:
            return GenerationPriority.CRITICAL
        if not provider_healthy or assessment.trajectory is BufferTrajectory.STALLED:
            return GenerationPriority.CRITICAL
        if assessment.level is BufferLevel.LOW or assessment.is_failing_soon:
            return GenerationPriority.HIGH
        if (
            assessment.fill_ratio >= self._settings.generation.experimental_fill_ratio
            and assessment.trajectory is BufferTrajectory.FILLING
            and not capacity.is_losing
        ):
            return GenerationPriority.EXPERIMENTAL
        return GenerationPriority.NORMAL

    def _allow_experimental(
        self,
        assessment: BufferAssessment,
        capacity: CapacitySnapshot,
        provider_healthy: bool,
    ) -> bool:
        """§94: artistic risk falls when operational risk rises.

        Note the trajectory term. A buffer at target but *draining* must not license
        experimentation — that is precisely the brief's "45 minutes queued while generation has
        been dead" case, and a level-only check would wave it through.
        """
        if not provider_healthy:
            return False
        if assessment.level.is_degraded:
            return False
        if assessment.trajectory in {BufferTrajectory.DRAINING, BufferTrajectory.STALLED}:
            return False
        if capacity.is_losing:
            return False
        return assessment.fill_ratio >= self._settings.generation.experimental_fill_ratio

    def _should_replan(
        self, state: MarketStateV1, assessment: BufferAssessment
    ) -> tuple[bool, int | None, str]:
        """Whether the market has moved enough to justify replacing future programming (§28).

        Two independent gates, both needed. The energy threshold asks whether the change
        *matters*; the cooldown bounds how often the question may be answered yes. A market can
        cross the threshold repeatedly inside a minute and each crossing is individually
        justified — which is how a station ends up with a queue nobody ever hears.
        """
        regime = state.regime
        energy = state.energy
        previous_regime, previous_energy = self._last_regime, self._last_energy
        previous_symbol = self._last_symbol
        self._last_regime, self._last_energy = regime, energy
        self._last_symbol = state.symbol

        if previous_regime is None or previous_energy is None:
            return False, None, "first cycle: nothing to compare against"

        # A market switch is its own replan trigger, independent of the energy test.
        #
        # The energy gate asks "did this market move enough to matter". After a switch the
        # question is different: the flexible slots were planned against a market the
        # station is no longer on, and they would stay that way however quiet Bitcoin
        # happens to be at the moment gold closes. Two points still hold — the buffer gate
        # below, because survival outranks fit here as everywhere, and the position gate,
        # because anything already generated and imminent is protected. The replan cooldown
        # is skipped: the router's own hysteresis is minutes long and far outlasts it, so
        # applying both would only mean a switch sometimes failed to replan at all.
        symbol_changed = previous_symbol is not None and previous_symbol != state.symbol
        if not symbol_changed:
            if regime is previous_regime:
                return False, None, "regime unchanged"

            delta = abs(energy - previous_energy)
            if delta < MAJOR_SHIFT_ENERGY_DELTA:
                return (
                    False,
                    None,
                    f"regime moved {previous_regime.value}->{regime.value} but energy only "
                    f"{delta:.0f} points",
                )

            if (
                self._last_replan_monotonic is not None
                and self._clock.monotonic() - self._last_replan_monotonic
                < REPLAN_COOLDOWN_SECONDS
            ):
                self._stats.replans_suppressed += 1
                elapsed = self._clock.monotonic() - self._last_replan_monotonic
                return False, None, f"replan suppressed: last one {elapsed:.0f}s ago"

        # A starving station does not replan. Replacing flexible slots discards audio that
        # already exists or is already being generated, and when the buffer is the problem,
        # throwing programming away makes it worse — however badly the queue now fits the
        # market. Fit is worth less than not being silent.
        trigger = (
            f"active market {previous_symbol}->{state.symbol}"
            if symbol_changed
            else f"regime {previous_regime.value}->{regime.value}"
        )
        if assessment.level.is_urgent:
            return (
                False,
                None,
                f"{trigger} ignored: buffer is {assessment.level.value}, "
                "survival outranks fit",
            )

        # Where the replan would start: the first slot §28 allows to be replaced.
        #
        # Computed rather than assumed. The field was previously always ``None`` while
        # ``replan`` was ``True``, which made the decision misreport itself to anything
        # reading it — including §50's panel — and let the station count replans that
        # replaced nothing.
        position = self._first_replaceable_position()
        if position is None:
            return (
                False,
                None,
                f"{trigger} ignored: no replaceable slot (every queued track is locked or "
                "the queue is empty)",
            )

        self._last_replan_monotonic = self._clock.monotonic()
        self._stats.replans += 1
        detail = (
            "the queue was planned against a market the station has left"
            if symbol_changed
            else f"{abs(energy - previous_energy):.0f}-point energy shift"
        )
        return True, position, f"{trigger} with {detail}; replacing from position {position}"


    def _first_replaceable_position(self) -> int | None:
        """Position of the first slot a replan may touch, or ``None`` if there is none.

        Read from the queue rather than derived from ``locked_slots``, because lock level is
        a function of position *and* of readiness (§28) — a track already generated near the
        head is protected whatever the configured slot counts say.
        """
        for position, entry in enumerate(self._queue):
            if entry.is_flexible:
                return position
        return None

    # -- acting on the decision --------------------------------------------

    def plan_tracks(
        self,
        decision: ScheduleDecision,
        *,
        state: MarketStateV1,
        history: ProgrammingHistory,
        buffer: BufferHealthV1,
        used_titles: tuple[str, ...] = (),
        used_signatures: frozenset[str] = frozenset(),
        used_seeds: frozenset[int] = frozenset(),
        capacity_ratio: float = 1.0,
        provider_healthy: bool = True,
    ) -> list[DirectorDecision]:
        """Ask the director for blueprints. The Creative Risk Controller lives here.

        §94's coupling needs no policy of its own: the director already decides how adventurous
        to be from a buffer and a capacity ratio, so connecting operational health to creative
        risk is a matter of handing it the *real* numbers rather than optimistic ones. Passing a
        buffer that counted in-flight work as ready, for instance, would make the station
        experiment exactly when it should not.
        """
        decisions: list[DirectorDecision] = []
        if not decision.wants_work:
            return decisions

        now = self._clock.now()
        titles = list(used_titles)
        signatures = set(used_signatures)
        seeds = set(used_seeds)

        for _ in range(decision.tracks_to_plan):
            self._sequence += 1
            produced = self._director.create_blueprint(
                track_id=self._next_track_id(now),
                state=state,
                history=history,
                buffer=buffer,
                now=now,
                capacity_ratio=capacity_ratio,
                provider_healthy=provider_healthy,
                used_titles=tuple(titles),
                recent_titles=tuple(titles[-40:]),
                used_signatures=frozenset(signatures),
                used_seeds=frozenset(seeds),
            )
            decisions.append(produced)
            # Accumulate within the batch. Without this, planning four tracks in one cycle lets
            # all four pick the same title and seed — each call would see an identical history,
            # and §11's duplicate prevention only ever sees what it is told.
            titles.append(produced.blueprint.title)
            signatures.add(produced.blueprint.signature())
            seeds.add(produced.blueprint.seed)

        self._stats.planned += len(decisions)
        self._stats.record_priority(decision.priority, len(decisions))
        _log.info(
            "scheduler.planned",
            count=len(decisions),
            priority=decision.priority.value,
            experimental_allowed=decision.allow_experimental,
            reason=decision.reason,
        )
        return decisions

    def _next_track_id(self, now: datetime) -> str:
        """``TF-YYYYMMDD-NNNNN``. Date-prefixed so the id sorts by day on disk (§36)."""
        return f"TF-{now:%Y%m%d}-{self._sequence:05d}"

    def enqueue(
        self, decisions: list[DirectorDecision], *, state: MarketStateV1
    ) -> list[QueueEntry]:
        """Add planned blueprints to the queue as pending slots."""
        added: list[QueueEntry] = []
        now = self._clock.now()
        for produced in decisions:
            entry = build_entry(
                produced.blueprint,
                now=now,
                regime=state.regime,
                energy=state.energy,
            )
            try:
                self._queue.append(entry)
            except Exception as error:  # noqa: BLE001 - a rejected slot must not stop the rest
                _log.warning(
                    "scheduler.enqueue_rejected",
                    track_id=entry.track_id,
                    error=str(error),
                )
                continue
            added.append(entry)
        return added

    def replan(
        self, replacements: list[DirectorDecision], *, state: MarketStateV1
    ) -> int:
        """§28's replan: swap flexible slots for programming that fits the new market.

        Returns how many slots were replaced. The queue enforces what may not be touched; this
        only decides *that* it should happen.
        """
        now = self._clock.now()
        entries = [
            build_entry(
                produced.blueprint, now=now, regime=state.regime, energy=state.energy
            )
            for produced in replacements
        ]
        replaced = self._queue.replace_flexible(entries)
        self._stats.tracks_replaced += replaced
        return replaced

    def cancelled_track_ids(self, before: tuple[str, ...], after: tuple[str, ...]) -> list[str]:
        """Tracks that a replan dropped, so their generation jobs can be cancelled.

        Computed from before/after rather than returned by the queue, because the queue's job is
        to hold the schedule — not to know that a dropped slot has a provider holding a GPU for
        it. Leaving those jobs running would waste capacity generating audio nothing references.
        """
        return [track_id for track_id in before if track_id not in set(after)]

    # -- station IDs (§31) -------------------------------------------------

    def wants_station_id(self, *, assessment: BufferAssessment) -> bool:
        """Whether inserting an identifier would improve the station right now (§31).

        Deliberately not "after every N tracks". §31 says the scheduler decides, and the useful
        decision is conditional: never while the buffer is in trouble, because an identifier
        occupies a slot that music needs and the listener would rather have music than branding
        during a near-miss.
        """
        if assessment.level.is_degraded:
            return False
        every = self._settings.radio.station_id_every_n_tracks
        if self._tracks_since_station_id < every:
            return False
        self._stats.station_ids_requested += 1
        return True


__all__ = [
    "MAJOR_SHIFT_ENERGY_DELTA",
    "REPLAN_COOLDOWN_SECONDS",
    "TARGET_OVERSHOOT_TRACKS",
    "ScheduleDecision",
    "Scheduler",
    "SchedulerStats",
]
