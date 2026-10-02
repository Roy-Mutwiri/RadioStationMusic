"""Queue and playout contracts (§26–§30, §44).

§28's layered priorities are modelled as an explicit :class:`QueueLockLevel`
rather than being inferred from position. Inferring from position looks simpler
but breaks the moment an operator manually pins a track (§44 "lock"), and makes
the §29 rule "keep current playback uninterrupted; possibly keep next track;
recalculate later tracks" impossible to express without re-deriving intent from
indices.
"""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import Field, computed_field, field_validator, model_validator

from tradefix_radio.contracts.base import Contract, MutableContract
from tradefix_radio.contracts.enums import PlayoutTier, TransitionType
from tradefix_radio.contracts.market import Unit
from tradefix_radio.core.state_machine import TrackState


class QueueLockLevel(str, enum.Enum):
    """How strongly a queue slot resists replanning (§28)."""

    LOCKED = "locked"
    """Playing or imminent; never replaced by automatic replanning. Operator
    destructive actions require explicit confirmation (§44)."""

    SEMI_LOCKED = "semi_locked"
    """Replaced only by a *large* market shift, and never mid-generation."""

    REPLACEABLE = "replaceable"
    """Freely recalculated when the market changes (§28)."""

    OPERATOR_PINNED = "operator_pinned"
    """Explicitly pinned by a human. Outranks every automatic rule — the system
    must not quietly overrule a deliberate human choice."""

    @property
    def is_protected(self) -> bool:
        """Whether automatic replanning must leave this slot alone."""
        return self in {
            QueueLockLevel.LOCKED,
            QueueLockLevel.OPERATOR_PINNED,
        }


class QueueItemV1(MutableContract):
    """One slot in the forward schedule.

    Mutable (unlike most contracts) because ``state`` and ``generation_progress``
    change many times per track and copy-on-write would add noise without
    safety — the queue is already the single owner of these objects.
    """

    item_id: str = Field(min_length=1, max_length=64)
    track_id: str = Field(min_length=1, max_length=64)
    position: int = Field(ge=0)
    state: TrackState
    lock_level: QueueLockLevel = QueueLockLevel.REPLACEABLE
    tier: PlayoutTier = PlayoutTier.SCHEDULED

    #: Display metadata, denormalised so the §44 panel needs no joins.
    title: str = Field(min_length=1, max_length=120)
    genre: str = Field(min_length=1, max_length=64)
    bpm: int = Field(ge=40, le=220)
    duration_seconds: float = Field(gt=0.0)
    is_instrumental: bool
    regime_at_generation: str = Field(min_length=1, max_length=48)

    generation_progress: Unit = 0.0
    transition_in: TransitionType = TransitionType.CROSSFADE
    #: Crossfade length into this track; 0 for a hard cut.
    transition_seconds: float = Field(default=0.0, ge=0.0, le=30.0)

    enqueued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @field_validator("enqueued_at", "started_at", "finished_at")
    @classmethod
    def _require_tz(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (UTC)")
        return value

    @model_validator(mode="after")
    def _coherent_timing(self) -> QueueItemV1:
        if self.transition_in is TransitionType.HARD_CUT and self.transition_seconds != 0.0:
            raise ValueError("HARD_CUT must have transition_seconds == 0")
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("finished_at precedes started_at")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_playable(self) -> bool:
        return self.state in {TrackState.READY, TrackState.QUEUED, TrackState.PLAYING}


class BufferHealthV1(Contract):
    """Forward-buffer measurement (§26, §41, §57).

    ``minutes_ready`` counts only tracks that could actually air right now. In-
    flight generation is reported separately as ``minutes_in_flight`` because
    counting optimistic work as buffer is how a station walks into silence
    believing it has 40 minutes in hand.
    """

    minutes_ready: float = Field(ge=0.0)
    minutes_in_flight: float = Field(ge=0.0)
    minimum_minutes: float = Field(ge=0.0)
    target_minutes: float = Field(ge=0.0)
    maximum_minutes: float = Field(ge=0.0)
    ready_count: int = Field(ge=0)
    in_flight_count: int = Field(ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_below_minimum(self) -> bool:
        return self.minutes_ready < self.minimum_minutes

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_critical(self) -> bool:
        """§57 raises a critical alert below five minutes of real buffer."""
        return self.minutes_ready < 5.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fill_ratio(self) -> float:
        """Ready buffer as a fraction of target; drives §95 creative temperature."""
        if self.target_minutes <= 0:
            return 1.0
        return min(1.0, self.minutes_ready / self.target_minutes)


class NowPlayingV1(Contract):
    """Everything the §42 card and §51 OBS sources need, in one object."""

    track_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    persona: str | None = Field(default=None, max_length=64)
    genre: str = Field(min_length=1, max_length=64)
    secondary_genre: str | None = Field(default=None, max_length=64)
    bpm: int = Field(ge=40, le=220)
    key: str = Field(min_length=1, max_length=32)

    duration_seconds: float = Field(gt=0.0)
    elapsed_seconds: float = Field(ge=0.0)

    is_instrumental: bool
    vocal_style: str = Field(min_length=1, max_length=32)

    regime_at_generation: str = Field(min_length=1, max_length=48)
    energy_at_generation: float = Field(ge=0.0, le=100.0)
    generation_model: str = Field(min_length=1, max_length=128)
    novelty_score: Unit
    tier: PlayoutTier
    artwork_path: str | None = Field(default=None, max_length=1024)
    started_at: datetime

    @field_validator("started_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("started_at must be timezone-aware (UTC)")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.duration_seconds - self.elapsed_seconds)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def progress(self) -> float:
        if self.duration_seconds <= 0:
            return 0.0
        return min(1.0, self.elapsed_seconds / self.duration_seconds)


class PlayEventV1(Contract):
    """An airing record (§37 ``play_events``).

    ``completed`` is deliberately separate from the track's state: §75 forbids
    marking an incomplete track as successfully played, and this is where that
    distinction is persisted for the §46 play history.
    """

    track_id: str = Field(min_length=1, max_length=64)
    started_at: datetime
    ended_at: datetime | None = None
    played_seconds: float = Field(ge=0.0)
    completed: bool
    tier: PlayoutTier
    transition_in: TransitionType
    #: ``operator_skip``, ``finished``, ``sink_failure``, ``emergency_override``…
    end_reason: str | None = Field(default=None, max_length=48)

    @field_validator("started_at", "ended_at")
    @classmethod
    def _require_tz(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (UTC)")
        return value

    @model_validator(mode="after")
    def _completion_requires_end(self) -> PlayEventV1:
        if self.completed and self.ended_at is None:
            raise ValueError("a completed play event must have ended_at")
        return self


__all__ = [
    "BufferHealthV1",
    "NowPlayingV1",
    "PlayEventV1",
    "QueueItemV1",
    "QueueLockLevel",
]
