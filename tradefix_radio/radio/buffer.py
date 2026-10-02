"""Buffer health, including the predictive part (§26, §93, milestone 4.5).

The brief's example states the whole problem:

    45 minutes queued but generation has been dead for 30 minutes is not equally healthy to
    45 minutes queued while generation is producing normally.

A level-only view reports both as "healthy, 45 minutes". One of them is a station that will be
silent in forty-five minutes and does not know it. So health here has two independent axes:

**Level** — how much playable audio exists right now. ``HEALTHY`` / ``LOW`` / ``CRITICAL`` /
``EMPTY`` against the configured thresholds.

**Trajectory** — whether that level is rising, holding or falling, and if falling, *when it
reaches zero*. Derived from §93's measured generation capacity against the consumption rate,
which is just "one second of audio per second" by definition.

The useful output is :attr:`BufferAssessment.seconds_to_failure`. It is ``None`` when the
buffer is not shrinking, because inventing a number there would be worse than admitting there
isn't one — a dashboard that always shows a countdown trains an operator to ignore it.

**Ready audio, not queued audio.** Every figure here counts only slots whose files exist. In-
flight generation is reported separately as ``pending``. Counting optimistic work as buffer is
precisely how a station walks into silence believing it has forty minutes in hand, and §26 says
so explicitly.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from tradefix_radio.config.schema import RadioSettings
from tradefix_radio.contracts.queue import BufferHealthV1
from tradefix_radio.generation.capacity import CapacitySnapshot

#: Capacity ratio within this distance of break-even counts as "holding" rather than rising or
#: falling. Without a dead band the trajectory flips on measurement noise, and a station that
#: reports a different trajectory every thirty seconds is reporting nothing.
TRAJECTORY_DEAD_BAND = 0.08

#: Minutes of ready audio below which the station is considered to have nothing.
#:
#: Not zero. A single part-played track is not a buffer, and treating it as one delays the
#: emergency escalation until there is genuinely no time left to escalate in.
EMPTY_THRESHOLD_MINUTES = 0.5


class BufferLevel(str, enum.Enum):
    """How much playable audio exists, against the §26 thresholds."""

    HEALTHY = "healthy"
    """At or above the configured minimum."""

    LOW = "low"
    """Below the minimum. Generation urgency rises, experimentation stops (§94)."""

    CRITICAL = "critical"
    """Below the §57 critical line. Emergency tiers prepare; operators are alerted."""

    EMPTY = "empty"
    """Nothing playable. Tier 2 or Tier 3 must already be carrying the output."""

    @property
    def is_degraded(self) -> bool:
        return self is not BufferLevel.HEALTHY

    @property
    def is_urgent(self) -> bool:
        return self in {BufferLevel.CRITICAL, BufferLevel.EMPTY}


class BufferTrajectory(str, enum.Enum):
    """Which way the buffer is going — the half a level-only view misses."""

    FILLING = "filling"
    HOLDING = "holding"
    DRAINING = "draining"
    STALLED = "stalled"
    """Generation has produced nothing for long enough that it is presumed dead.

    Distinct from ``DRAINING``: draining means generation is losing a race it is still running,
    stalled means it is not running. The responses differ — draining wants fewer, shorter,
    safer tracks, while stalled wants a provider healthcheck and the emergency tiers warmed up.
    """


@dataclass(frozen=True)
class BufferAssessment:
    """Everything the scheduler and the §41 panel need about buffer health."""

    level: BufferLevel
    trajectory: BufferTrajectory

    ready_minutes: float
    pending_minutes: float
    minimum_minutes: float
    target_minutes: float
    maximum_minutes: float

    #: §93's measured ratio, carried through so one object answers every buffer question.
    capacity_ratio: float
    #: Net rate of change in buffer minutes per minute of wall clock. Negative is draining.
    drain_rate: float
    #: Projected seconds until nothing playable remains. ``None`` unless actually shrinking.
    seconds_to_failure: float | None
    #: Seconds since generation last delivered anything.
    seconds_since_last_delivery: float
    reason: str

    @property
    def fill_ratio(self) -> float:
        """Ready audio as a fraction of target, capped at 1. Drives §95 temperature."""
        if self.target_minutes <= 0:
            return 1.0
        return min(1.0, self.ready_minutes / self.target_minutes)

    @property
    def deficit_minutes(self) -> float:
        """How far below target the buffer is. Zero when at or above."""
        return max(0.0, self.target_minutes - self.ready_minutes)

    @property
    def headroom_minutes(self) -> float:
        """How much more may be queued before hitting the §26 maximum."""
        return max(0.0, self.maximum_minutes - self.ready_minutes - self.pending_minutes)

    @property
    def is_failing_soon(self) -> bool:
        """Whether the buffer will empty before a track could plausibly be generated.

        The question the brief's example is really asking. A healthy *level* with a failing
        trajectory answers ``True`` here, which is the whole point.
        """
        return self.seconds_to_failure is not None and self.seconds_to_failure < 600.0

    def to_contract(self, *, ready_count: int, in_flight_count: int) -> BufferHealthV1:
        """The §41/§57 wire form."""
        return BufferHealthV1(
            minutes_ready=self.ready_minutes,
            minutes_in_flight=self.pending_minutes,
            minimum_minutes=self.minimum_minutes,
            target_minutes=self.target_minutes,
            maximum_minutes=self.maximum_minutes,
            ready_count=ready_count,
            in_flight_count=in_flight_count,
        )


class BufferMonitor:
    """Turns queue depth and §93 capacity into a health assessment.

    Stateless apart from "when did generation last deliver", which cannot be derived from a
    snapshot: a queue holding thirty minutes looks identical whether it was filled a minute ago
    or an hour ago, and that difference is the entire subject of this module.
    """

    def __init__(
        self,
        settings: RadioSettings,
        *,
        critical_minutes: float | None = None,
        stall_seconds: float = 900.0,
    ) -> None:
        self._settings = settings
        # The §57 critical line. Defaults to half the minimum rather than a fixed figure, so a
        # station configured with a 60-minute minimum does not treat 5 minutes as merely "low".
        self._critical_minutes = (
            critical_minutes
            if critical_minutes is not None
            else settings.minimum_buffer_minutes / 2.0
        )
        self._stall_seconds = stall_seconds
        self._last_delivery_monotonic: float | None = None

    # -- delivery tracking -------------------------------------------------

    def record_delivery(self, monotonic_now: float) -> None:
        """Note that generation just produced playable audio."""
        self._last_delivery_monotonic = monotonic_now

    def seconds_since_delivery(self, monotonic_now: float) -> float:
        """Zero before the first delivery.

        Zero rather than infinity: at startup nothing has been delivered *yet*, which is not
        the same as generation having stopped. Reporting a stall during the first minute of a
        run would escalate to the emergency tiers before the first track had a chance to exist.
        """
        if self._last_delivery_monotonic is None:
            return 0.0
        return max(0.0, monotonic_now - self._last_delivery_monotonic)

    # -- assessment --------------------------------------------------------

    def assess(
        self,
        *,
        ready_seconds: float,
        pending_seconds: float,
        capacity: CapacitySnapshot,
        monotonic_now: float,
        generation_active: bool = True,
    ) -> BufferAssessment:
        """Combine level and trajectory into one answer."""
        settings = self._settings
        ready_minutes = ready_seconds / 60.0
        pending_minutes = pending_seconds / 60.0
        since_delivery = self.seconds_since_delivery(monotonic_now)

        level = self._level(ready_minutes)
        trajectory, drain_rate, reason = self._trajectory(
            capacity=capacity,
            since_delivery=since_delivery,
            generation_active=generation_active,
            pending_minutes=pending_minutes,
        )
        seconds_to_failure = self._seconds_to_failure(
            ready_minutes=ready_minutes,
            pending_minutes=pending_minutes,
            drain_rate=drain_rate,
            trajectory=trajectory,
            capacity=capacity,
        )

        return BufferAssessment(
            level=level,
            trajectory=trajectory,
            ready_minutes=ready_minutes,
            pending_minutes=pending_minutes,
            minimum_minutes=settings.minimum_buffer_minutes,
            target_minutes=settings.target_buffer_minutes,
            maximum_minutes=settings.maximum_buffer_minutes,
            capacity_ratio=capacity.capacity_ratio,
            drain_rate=drain_rate,
            seconds_to_failure=seconds_to_failure,
            seconds_since_last_delivery=since_delivery,
            reason=reason,
        )

    def _level(self, ready_minutes: float) -> BufferLevel:
        if ready_minutes < EMPTY_THRESHOLD_MINUTES:
            return BufferLevel.EMPTY
        if ready_minutes < self._critical_minutes:
            return BufferLevel.CRITICAL
        if ready_minutes < self._settings.minimum_buffer_minutes:
            return BufferLevel.LOW
        return BufferLevel.HEALTHY

    def _trajectory(
        self,
        *,
        capacity: CapacitySnapshot,
        since_delivery: float,
        generation_active: bool,
        pending_minutes: float,
    ) -> tuple[BufferTrajectory, float, str]:
        """Which way the buffer is going, and how fast, in buffer-minutes per minute.

        The arithmetic: playback consumes one minute of buffer per minute, and generation adds
        ``capacity_ratio`` minutes per minute. So the net rate is ``ratio - 1``, and the units
        work out because both sides are audio-minutes over wall-minutes.
        """
        if not generation_active:
            return (
                BufferTrajectory.STALLED,
                -1.0,
                "generation is not running; the buffer drains at playback rate",
            )

        # A stall is detected by *silence from the generator*, not by a low ratio. The two are
        # genuinely different: a ratio of 0.4 is a provider that is slow, while no delivery for
        # fifteen minutes is a provider that has stopped — and a slow provider still has a
        # ratio, so the ratio alone can never report a stop.
        if since_delivery > self._stall_seconds and pending_minutes <= 0.0:
            return (
                BufferTrajectory.STALLED,
                -1.0,
                (
                    f"nothing delivered for {since_delivery / 60:.0f} minutes and nothing "
                    "in flight"
                ),
            )

        net = capacity.capacity_ratio - 1.0
        if abs(net) <= TRAJECTORY_DEAD_BAND:
            return (
                BufferTrajectory.HOLDING,
                net,
                f"generating at {capacity.capacity_ratio:.2f}x, about break-even",
            )
        if net > 0:
            return (
                BufferTrajectory.FILLING,
                net,
                f"generating at {capacity.capacity_ratio:.2f}x playback",
            )
        return (
            BufferTrajectory.DRAINING,
            net,
            f"generating at only {capacity.capacity_ratio:.2f}x playback",
        )

    def _seconds_to_failure(
        self,
        *,
        ready_minutes: float,
        pending_minutes: float,
        drain_rate: float,
        trajectory: BufferTrajectory,
        capacity: CapacitySnapshot,
    ) -> float | None:
        """When the buffer empties, or ``None`` if it is not heading there.

        ``None`` rather than a large number. A dashboard that always shows a countdown trains
        an operator to ignore it, and "no estimate" is the honest answer when the buffer is
        holding or filling.

        In-flight work counts, but only what is expected to *land in time*: a job that will
        finish in twenty minutes does not help a buffer with ten minutes left, and including it
        would produce exactly the optimistic estimate §26 warns against.
        """
        if trajectory in {BufferTrajectory.FILLING, BufferTrajectory.HOLDING}:
            return None
        if drain_rate >= 0:
            return None

        runway_seconds = ready_minutes * 60.0
        if pending_minutes > 0 and trajectory is not BufferTrajectory.STALLED:
            arrival = capacity.latency_p95_seconds
            if arrival > 0 and arrival < runway_seconds:
                runway_seconds += pending_minutes * 60.0
        return max(0.0, runway_seconds / abs(drain_rate))


__all__ = [
    "EMPTY_THRESHOLD_MINUTES",
    "TRAJECTORY_DEAD_BAND",
    "BufferAssessment",
    "BufferLevel",
    "BufferMonitor",
    "BufferTrajectory",
]
