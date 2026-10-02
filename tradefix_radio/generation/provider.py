"""The music generation interface (§18, ADR-02).

§18: "Define a clean interface so a different model can be swapped in." Everything after
this boundary — QC, fingerprinting, mastering, the queue, playout — works on a
:class:`GenerationResult` and has never heard of ACE-Step, of a mock, or of HTTP.

The interface is deliberately **narrow and async**. Four methods:

``generate``     blueprint in, audio file out
``healthcheck``  can this provider work right now? (§18, §34)
``cancel``       stop a job in flight (§28's replan, and operator skip)
``describe``     what model, what version — for the §46 detail page and §101 reports

Two design points that are not obvious:

**The provider writes the file; it does not return samples.** A generated track is tens of
megabytes of PCM, and ACE-Step runs out of process (ADR-02) where returning samples would
mean serialising them over HTTP. Returning a path keeps the mock and the real provider
honest about the same contract, and keeps the §26 critical path free of large copies.

**Progress is a callback, not a return value.** §41's queue panel shows per-track generation
progress, which has to arrive *during* the work. A provider that only reported progress at
the end would make the panel a lie that happens to resolve correctly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from tradefix_radio.contracts.lyrics import LyricsV1
from tradefix_radio.contracts.music import MusicBlueprintV1

#: Called with 0.0–1.0 as generation proceeds. Must be cheap and must not raise; a provider
#: is not required to defend against a callback that throws, so the caller's callback is the
#: wrong place to do work.
ProgressCallback = Callable[[float], None]


@dataclass(frozen=True)
class GenerationRequest:
    """Everything a provider needs for one track.

    Carries the blueprint rather than a flattened prompt, so a provider can use as much or as
    little of it as its model understands. ACE-Step takes a tag string and lyrics; the mock
    reads the intensities directly. Flattening to a prompt here would make the richer
    provider impossible without changing the interface.
    """

    blueprint: MusicBlueprintV1
    #: Destination for the raw audio. The provider creates parents and writes atomically.
    output_path: Path
    #: Composed lyrics, when the track has them (§19 passes lyrics as input).
    lyrics: LyricsV1 | None = None
    #: Hard deadline in seconds. A provider that cannot meet it must raise
    #: :class:`~tradefix_radio.core.errors.GenerationTimeoutError`, not return short audio.
    timeout_seconds: float = 600.0
    #: Attempt number, 1-based. Providers may use it to back off or to reduce quality.
    attempt: int = 1

    @property
    def track_id(self) -> str:
        return self.blueprint.track_id

    @property
    def duration_seconds(self) -> float:
        return float(self.blueprint.composition.duration_seconds)


@dataclass(frozen=True)
class GenerationResult:
    """What a provider produced.

    ``duration_seconds`` is the duration of the **file on disk**, measured, not the duration
    that was requested. The difference is exactly what §24's duration-deviation check exists
    to catch, and a result that echoed the request back would make that check structurally
    incapable of failing.
    """

    track_id: str
    audio_path: Path
    duration_seconds: float
    sample_rate: int
    channels: int
    #: Wall-clock seconds the provider spent. Feeds §93's capacity ratio.
    generation_seconds: float
    #: Model identity, persisted per track (§37) and shown on the §46 detail page.
    model_identifier: str
    provider_name: str
    #: Peak VRAM, when the provider can measure it (§20). ``None`` for CPU providers.
    peak_vram_bytes: int | None = None
    #: Free-form provider diagnostics, persisted as JSON.
    detail: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderDescription:
    """Static provider identity, for reports and the settings page."""

    name: str
    model_identifier: str
    #: Whether this provider can produce vocals from lyrics. The director needs to know:
    #: scheduling a vocal track against an instrumental-only provider would mean a blueprint
    #: that cannot be realised.
    supports_vocals: bool
    #: Whether generation is expected to take meaningful wall-clock time. ``False`` for the
    #: mock, which lets tests skip the §93 capacity warm-up.
    is_realtime_costly: bool
    #: Longest duration this provider can produce in one call, if limited.
    max_duration_seconds: float | None = None


@dataclass(frozen=True)
class ProviderHealth:
    """Result of a healthcheck (§18, §34)."""

    healthy: bool
    detail: str
    #: Round-trip time of the check itself, so a provider that is "up" but 40 s slow is
    #: distinguishable from one that is genuinely fine.
    latency_seconds: float = 0.0

    @classmethod
    def ok(cls, detail: str = "ready", latency_seconds: float = 0.0) -> ProviderHealth:
        return cls(healthy=True, detail=detail, latency_seconds=latency_seconds)

    @classmethod
    def down(cls, detail: str, latency_seconds: float = 0.0) -> ProviderHealth:
        return cls(healthy=False, detail=detail, latency_seconds=latency_seconds)


@runtime_checkable
class MusicGenerationProvider(Protocol):
    """§18's swappable generation backend.

    A Protocol rather than an ABC so a test double needs no inheritance, and so the ACE-Step
    adapter in Phase 7 can be written without importing anything from here at runtime.
    """

    async def generate(
        self,
        request: GenerationRequest,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> GenerationResult:
        """Produce audio for ``request``, writing it to ``request.output_path``.

        Raises :class:`~tradefix_radio.core.errors.GenerationError` or a subclass on failure.
        Must not return a result pointing at a file it did not finish writing.
        """
        ...

    async def healthcheck(self) -> ProviderHealth:
        """Report whether generation would work right now. Must not raise."""
        ...

    async def cancel(self, track_id: str) -> bool:
        """Request cancellation of an in-flight job. ``True`` if anything was cancelled.

        Cooperative: the generate call raises
        :class:`~tradefix_radio.core.errors.GenerationCancelledError`. Returning ``False``
        for an unknown track is normal — the job may have finished between the decision to
        cancel and this call.
        """
        ...

    def describe(self) -> ProviderDescription:
        """Static identity. Synchronous because it must never fail or block."""
        ...


__all__ = [
    "GenerationRequest",
    "GenerationResult",
    "MusicGenerationProvider",
    "ProgressCallback",
    "ProviderDescription",
    "ProviderHealth",
]
