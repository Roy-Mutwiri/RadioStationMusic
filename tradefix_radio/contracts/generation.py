"""Generation request/result contracts (§90, §18).

``GenerationRequestV1`` is where provider specifics are allowed to appear — and
the *only* place. It wraps an opaque, provider-agnostic
:class:`~tradefix_radio.contracts.music.MusicBlueprintV1` with the settings a
particular backend needs. That containment is what §18 means by "do not let
ACE-Step-specific details leak throughout the core architecture".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, computed_field, field_validator

from tradefix_radio.contracts.base import Contract
from tradefix_radio.contracts.enums import GenerationPriority
from tradefix_radio.contracts.lyrics import LyricsV1
from tradefix_radio.contracts.music import MusicBlueprintV1


class GenerationRequestV1(Contract):
    """A unit of work handed to a provider."""

    job_id: str = Field(min_length=1, max_length=64)
    track_id: str = Field(min_length=1, max_length=64)
    blueprint: MusicBlueprintV1
    lyrics: LyricsV1 | None = None

    priority: GenerationPriority = GenerationPriority.NORMAL
    #: Hard deadline. §19 requires generation to have timeouts; a request without
    #: one is not representable.
    timeout_seconds: float = Field(gt=0.0, le=3600.0)
    attempt: int = Field(ge=1, le=10)

    #: Opaque provider knobs (steps, guidance, precision, offload). Validated by
    #: the provider, not here — the core must not know what they mean.
    provider_options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("lyrics")
    @classmethod
    def _lyrics_match_blueprint(cls, value: LyricsV1 | None, info: Any) -> LyricsV1 | None:
        """Guard against pairing a blueprint with another track's lyrics.

        Cheap check, high value: a mix-up here would persist wrong lyrics against
        a track and corrupt the §12 topic history permanently.
        """
        blueprint: MusicBlueprintV1 | None = info.data.get("blueprint")
        if value is not None and blueprint is not None and value.track_id != blueprint.track_id:
            raise ValueError(
                f"lyrics.track_id ({value.track_id}) does not match "
                f"blueprint.track_id ({blueprint.track_id})"
            )
        if value is None and blueprint is not None and blueprint.lyrics.enabled:
            raise ValueError("blueprint requires lyrics but none were supplied")
        return value


class GenerationResultV1(Contract):
    """What a provider returns on success.

    ``audio_path`` rather than bytes: ACE-Step runs out-of-process (ADR-02) on the
    same machine, so a path avoids moving tens of megabytes through HTTP for no
    reason. A future remote worker returns a URL instead, which the provider
    adapter resolves to a local path before constructing this.
    """

    job_id: str = Field(min_length=1, max_length=64)
    track_id: str = Field(min_length=1, max_length=64)
    audio_path: str = Field(min_length=1, max_length=1024)
    provider: str = Field(min_length=1, max_length=64)
    model_identifier: str = Field(min_length=1, max_length=128)

    seed_used: int = Field(ge=0)
    #: Wall-clock generation time. Feeds the §93 capacity ratio and §56 metrics.
    generation_seconds: float = Field(ge=0.0)
    #: Duration the provider actually produced, which may differ from the request.
    audio_duration_seconds: float = Field(gt=0.0)
    sample_rate: int = Field(gt=0)
    channels: int = Field(ge=1, le=2)

    peak_vram_bytes: int | None = Field(default=None, ge=0)
    completed_at: datetime
    #: Anything the provider wants recorded for reproducibility.
    provider_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("completed_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("completed_at must be timezone-aware (UTC)")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def realtime_factor(self) -> float:
        """Audio seconds produced per second of compute (§80, §93).

        Above 1.0 means the station can generate faster than it broadcasts, which
        is the condition for a stable buffer. Below 1.0 means guaranteed eventual
        starvation and is an alertable state, not a performance footnote.
        """
        if self.generation_seconds <= 0:
            return float("inf")
        return self.audio_duration_seconds / self.generation_seconds


class GenerationFailureV1(Contract):
    """Structured failure detail. Persisted so §56 failure rates are real."""

    job_id: str = Field(min_length=1, max_length=64)
    track_id: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=64)
    #: Short machine-readable class, e.g. ``timeout``, ``oom``, ``unavailable``.
    kind: str = Field(min_length=1, max_length=48)
    message: str = Field(min_length=1, max_length=2000)
    attempt: int = Field(ge=1)
    retryable: bool
    failed_at: datetime
    elapsed_seconds: float = Field(ge=0.0)

    @field_validator("failed_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("failed_at must be timezone-aware (UTC)")
        return value


class ProviderHealthV1(Contract):
    """Result of :meth:`MusicGenerationProvider.healthcheck` (§18)."""

    provider: str = Field(min_length=1, max_length=64)
    available: bool
    model_loaded: bool
    detail: str = Field(default="", max_length=500)
    latency_ms: float | None = Field(default=None, ge=0.0)
    vram_total_bytes: int | None = Field(default=None, ge=0)
    vram_free_bytes: int | None = Field(default=None, ge=0)
    checked_at: datetime

    @field_validator("checked_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("checked_at must be timezone-aware (UTC)")
        return value


__all__ = [
    "GenerationFailureV1",
    "GenerationRequestV1",
    "GenerationResultV1",
    "ProviderHealthV1",
]
