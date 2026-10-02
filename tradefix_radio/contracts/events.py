"""Concrete event payloads for the §91 bus.

Each class declares its own ``topic`` as a class attribute, exposed through the
``topic`` property the :class:`~tradefix_radio.core.events.Event` protocol
requires. Topics are strings rather than an enum so a future subsystem can
introduce one without editing a central file — the bus routes by prefix and does
not need a closed set.

Topic naming follows §91 exactly: ``<domain>.<past_tense_fact>``. Events are
*facts that already happened*, never commands. If something reads like an
instruction (``track.generate_now``) it belongs in a service call, not here.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from pydantic import Field

from tradefix_radio.contracts.base import Contract
from tradefix_radio.contracts.enums import HealthStatus, PlayoutTier
from tradefix_radio.contracts.market import MarketStateV1
from tradefix_radio.contracts.music import MusicBlueprintV1
from tradefix_radio.contracts.queue import BufferHealthV1, NowPlayingV1


class BaseEvent(Contract):
    """Common envelope for every bus event."""

    TOPIC: ClassVar[str] = "base"

    at: datetime

    @property
    def topic(self) -> str:
        return type(self).TOPIC


# ---------------------------------------------------------------- market


class MarketStateChanged(BaseEvent):
    """Emitted when the regime, direction or session changes — not every tick.

    Tick-rate events would make the bus the busiest thing in the process and
    would push §56 metrics into noise. Energy moves continuously and gets its own
    throttled event below.
    """

    TOPIC: ClassVar[str] = "market.state_changed"

    state: MarketStateV1
    previous_regime: str | None = None


class MarketRegimeChanged(BaseEvent):
    """The regime specifically changed (§5, §28).

    ``market.state_changed`` fires for a regime, direction *or* session change. §28's replan
    trigger is the regime alone, and a subscriber filtering the combined event has to re-derive
    "did the regime actually change?" from a nullable previous value — which is the sort of
    thing that works until someone publishes the combined event without the previous regime
    filled in.
    """

    TOPIC: ClassVar[str] = "market.regime_changed"

    state: MarketStateV1
    previous_regime: str = Field(min_length=1, max_length=48)
    new_regime: str = Field(min_length=1, max_length=48)
    #: Absolute change in §6 energy across the transition, 0-100. The scheduler uses this to
    #: decide whether the change is big enough to disturb soft-locked programming (§28).
    energy_delta: float = Field(ge=0.0, le=100.0)


class MarketEnergyChanged(BaseEvent):
    """Throttled energy update. Rate limited by the publisher, not the bus."""

    TOPIC: ClassVar[str] = "market.energy_changed"

    symbol: str = Field(min_length=1, max_length=32)
    energy: float = Field(ge=0.0, le=100.0)
    energy_velocity: float
    smoothed_energy: float = Field(ge=0.0, le=100.0)


class MarketFeedStatusChanged(BaseEvent):
    """Feed went live / stale / disconnected (§63-E)."""

    TOPIC: ClassVar[str] = "market.feed_status_changed"

    symbol: str = Field(min_length=1, max_length=32)
    status: str = Field(min_length=1, max_length=32)
    previous_status: str | None = Field(default=None, max_length=32)
    data_age_seconds: float = Field(ge=0.0)


# ---------------------------------------------------------------- track lifecycle


class TrackPlanned(BaseEvent):
    TOPIC: ClassVar[str] = "track.planned"

    track_id: str = Field(min_length=1, max_length=64)
    blueprint: MusicBlueprintV1


class TrackGenerationStarted(BaseEvent):
    TOPIC: ClassVar[str] = "track.generation_started"

    track_id: str = Field(min_length=1, max_length=64)
    job_id: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=64)
    attempt: int = Field(ge=1)


class TrackGenerated(BaseEvent):
    TOPIC: ClassVar[str] = "track.generated"

    track_id: str = Field(min_length=1, max_length=64)
    job_id: str = Field(min_length=1, max_length=64)
    generation_seconds: float = Field(ge=0.0)
    audio_duration_seconds: float = Field(gt=0.0)


class TrackRejected(BaseEvent):
    """QC or duplication-policy rejection. Feeds the §48 page."""

    TOPIC: ClassVar[str] = "track.rejected"

    track_id: str = Field(min_length=1, max_length=64)
    stage: str = Field(min_length=1, max_length=32)
    reasons: tuple[str, ...] = Field(default_factory=tuple)
    novelty_score: float | None = Field(default=None, ge=0.0, le=1.0)
    closest_track_id: str | None = Field(default=None, max_length=64)


class TrackReady(BaseEvent):
    TOPIC: ClassVar[str] = "track.ready"

    track_id: str = Field(min_length=1, max_length=64)
    duration_seconds: float = Field(gt=0.0)
    novelty_score: float = Field(ge=0.0, le=1.0)


class TrackPlaying(BaseEvent):
    TOPIC: ClassVar[str] = "track.playing"

    now_playing: NowPlayingV1


class TrackFinished(BaseEvent):
    TOPIC: ClassVar[str] = "track.finished"

    track_id: str = Field(min_length=1, max_length=64)
    played_seconds: float = Field(ge=0.0)
    completed: bool
    end_reason: str | None = Field(default=None, max_length=48)


class TrackQueued(BaseEvent):
    """A ready track took a position in the forward schedule (§27).

    Separate from ``track.ready`` because the two are genuinely different moments: a track
    becomes ready when its audio passes QC, and enters the queue when the scheduler decides
    *where* it goes. The §41 panel shows position, which only exists at the second.
    """

    TOPIC: ClassVar[str] = "track.queued"

    track_id: str = Field(min_length=1, max_length=64)
    position: int = Field(ge=0)
    lock_level: str = Field(min_length=1, max_length=24)
    queue_duration_seconds: float = Field(ge=0.0)


# ---------------------------------------------------------------- generation jobs
#
# Job events are distinct from the track events above. One track can own several jobs over its
# life — a timeout, a retry, a success — so a subscriber counting "generation attempts" needs
# the job stream, while one counting "tracks produced" needs the track stream. Collapsing them
# would make both counts wrong.


class GenerationJobPlanned(BaseEvent):
    TOPIC: ClassVar[str] = "generation.job_planned"

    job_id: str = Field(min_length=1, max_length=64)
    track_id: str = Field(min_length=1, max_length=64)
    priority: str = Field(min_length=1, max_length=24)
    provider: str = Field(min_length=1, max_length=64)


class GenerationStarted(BaseEvent):
    TOPIC: ClassVar[str] = "generation.started"

    job_id: str = Field(min_length=1, max_length=64)
    track_id: str = Field(min_length=1, max_length=64)
    attempt: int = Field(ge=1)
    worker_id: str = Field(min_length=1, max_length=96)
    timeout_seconds: float = Field(gt=0.0)


class GenerationCompleted(BaseEvent):
    TOPIC: ClassVar[str] = "generation.completed"

    job_id: str = Field(min_length=1, max_length=64)
    track_id: str = Field(min_length=1, max_length=64)
    wall_seconds: float = Field(ge=0.0)
    audio_seconds: float = Field(gt=0.0)
    capacity_ratio: float = Field(ge=0.0)


class GenerationFailed(BaseEvent):
    """One job attempt failed. ``will_retry`` distinguishes a setback from a loss."""

    TOPIC: ClassVar[str] = "generation.failed"

    job_id: str = Field(min_length=1, max_length=64)
    track_id: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=48)
    detail: str = Field(default="", max_length=2000)
    attempt: int = Field(ge=1)
    will_retry: bool
    retry_after_seconds: float | None = Field(default=None, ge=0.0)


# ---------------------------------------------------------------- generator


class GeneratorFailed(BaseEvent):
    TOPIC: ClassVar[str] = "generator.failed"

    provider: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=48)
    message: str = Field(min_length=1, max_length=2000)
    consecutive_failures: int = Field(ge=1)


class GeneratorRecovered(BaseEvent):
    TOPIC: ClassVar[str] = "generator.recovered"

    provider: str = Field(min_length=1, max_length=64)
    downtime_seconds: float = Field(ge=0.0)


# ---------------------------------------------------------------- radio


class RadioBufferLow(BaseEvent):
    TOPIC: ClassVar[str] = "radio.buffer_low"

    buffer: BufferHealthV1
    critical: bool


class RadioBufferCritical(BaseEvent):
    """Separate from ``buffer_low`` so subscribers can route them differently.

    ``low`` is a scheduling signal — generate harder, stop experimenting. ``critical`` is an
    operator signal (§57) and the point at which emergency tiers prepare. A single event with a
    boolean flag forced every subscriber to re-derive the distinction, and §57's alerting would
    have had to filter rather than subscribe.
    """

    TOPIC: ClassVar[str] = "radio.buffer_critical"

    buffer: BufferHealthV1
    #: Projected seconds until the buffer empties, when the trajectory allows an estimate.
    seconds_to_failure: float | None = Field(default=None, ge=0.0)


class RadioBufferRecovered(BaseEvent):
    TOPIC: ClassVar[str] = "radio.buffer_recovered"

    buffer: BufferHealthV1
    #: How long the station spent below the minimum, for the §101 report.
    degraded_seconds: float = Field(ge=0.0)


class PlayoutFallbackEntered(BaseEvent):
    """Tier escalation (§33). Always alertable — the station is degraded."""

    TOPIC: ClassVar[str] = "playout.fallback_entered"

    tier: PlayoutTier
    previous_tier: PlayoutTier
    reason: str = Field(min_length=1, max_length=200)


class PlayoutFallbackExited(BaseEvent):
    TOPIC: ClassVar[str] = "playout.fallback_exited"

    tier: PlayoutTier
    previous_tier: PlayoutTier
    duration_seconds: float = Field(ge=0.0)


class StationIdRequested(BaseEvent):
    """The scheduler decided an identifier would improve the station (§31).

    A request, not a play: the decision and the airing are separated so the §41 panel can show
    an upcoming identifier, and so a request that the playout engine declines (because the queue
    changed underneath it) is visible rather than silent.
    """

    TOPIC: ClassVar[str] = "station_id.requested"

    category: str = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=1, max_length=200)
    tracks_since_last: int = Field(ge=0)


class StationIdPlayed(BaseEvent):
    TOPIC: ClassVar[str] = "radio.station_id_played"

    station_id_key: str = Field(min_length=1, max_length=64)


# ---------------------------------------------------------------- obs / health


class ObsConnected(BaseEvent):
    TOPIC: ClassVar[str] = "obs.connected"

    host: str = Field(min_length=1, max_length=128)
    obs_version: str | None = Field(default=None, max_length=32)


class ObsDisconnected(BaseEvent):
    TOPIC: ClassVar[str] = "obs.disconnected"

    host: str = Field(min_length=1, max_length=128)
    reason: str = Field(default="", max_length=400)


class HealthChanged(BaseEvent):
    """A component's health status changed (§35)."""

    TOPIC: ClassVar[str] = "health.changed"

    component: str = Field(min_length=1, max_length=64)
    status: HealthStatus
    previous_status: HealthStatus
    detail: str = Field(default="", max_length=500)


class AlertRaised(BaseEvent):
    """A §57 alert condition became true."""

    TOPIC: ClassVar[str] = "alert.raised"

    key: str = Field(min_length=1, max_length=64)
    severity: str = Field(min_length=1, max_length=16)
    message: str = Field(min_length=1, max_length=500)


class AlertCleared(BaseEvent):
    TOPIC: ClassVar[str] = "alert.cleared"

    key: str = Field(min_length=1, max_length=64)
    active_seconds: float = Field(ge=0.0)


__all__ = [
    "AlertCleared",
    "AlertRaised",
    "BaseEvent",
    "GenerationCompleted",
    "GenerationFailed",
    "GenerationJobPlanned",
    "GenerationStarted",
    "GeneratorFailed",
    "GeneratorRecovered",
    "HealthChanged",
    "MarketEnergyChanged",
    "MarketFeedStatusChanged",
    "MarketRegimeChanged",
    "MarketStateChanged",
    "ObsConnected",
    "ObsDisconnected",
    "PlayoutFallbackEntered",
    "PlayoutFallbackExited",
    "RadioBufferCritical",
    "RadioBufferLow",
    "RadioBufferRecovered",
    "StationIdPlayed",
    "StationIdRequested",
    "TrackFinished",
    "TrackGenerated",
    "TrackGenerationStarted",
    "TrackPlanned",
    "TrackPlaying",
    "TrackQueued",
    "TrackReady",
    "TrackRejected",
]
