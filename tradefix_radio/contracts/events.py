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


class RadioFallbackStarted(BaseEvent):
    """Tier escalation (§33). Always alertable — the station is degraded."""

    TOPIC: ClassVar[str] = "radio.fallback_started"

    tier: PlayoutTier
    reason: str = Field(min_length=1, max_length=200)


class RadioFallbackEnded(BaseEvent):
    TOPIC: ClassVar[str] = "radio.fallback_ended"

    tier: PlayoutTier
    duration_seconds: float = Field(ge=0.0)


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
    "GeneratorFailed",
    "GeneratorRecovered",
    "HealthChanged",
    "MarketEnergyChanged",
    "MarketFeedStatusChanged",
    "MarketStateChanged",
    "ObsConnected",
    "ObsDisconnected",
    "RadioBufferLow",
    "RadioFallbackEnded",
    "RadioFallbackStarted",
    "StationIdPlayed",
    "TrackFinished",
    "TrackGenerated",
    "TrackGenerationStarted",
    "TrackPlanned",
    "TrackPlaying",
    "TrackReady",
    "TrackRejected",
]
