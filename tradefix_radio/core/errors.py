"""Exception hierarchy.

Every error the station raises deliberately derives from :class:`RadioError`, so
supervisory code can distinguish *our* failures from genuine bugs (which should
propagate loudly rather than be swallowed). §86 forbids blind excepts; catching
``RadioError`` is the sanctioned narrow alternative.
"""

from __future__ import annotations

from typing import Any


class RadioError(Exception):
    """Base class for all deliberate Trade Fix Radio errors.

    ``context`` carries structured detail for the logger so that messages stay
    human-readable while diagnostics stay machine-queryable (§55).
    """

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = context

    def __str__(self) -> str:
        if not self.context:
            return self.message
        detail = " ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"{self.message} ({detail})"


# ---------------------------------------------------------------- configuration


class ConfigurationError(RadioError):
    """Configuration is invalid. Raised at startup; never recovered from (§71)."""


class DependencyMissingError(RadioError):
    """A required external dependency is absent.

    Carries ``remediation`` so ``tradefix doctor`` can tell the operator what to
    do instead of only what is broken (§79).
    """

    def __init__(self, message: str, remediation: str, **context: Any) -> None:
        super().__init__(message, **context)
        self.remediation = remediation


# ---------------------------------------------------------------- market


class MarketDataError(RadioError):
    """The market feed failed or returned unusable data."""


class StaleMarketDataError(MarketDataError):
    """Market data exists but is too old to be presented as current.

    Raised rather than silently returning a last-known price, because §32 and
    §86 forbid fabricating or misrepresenting prices.
    """

    def __init__(self, message: str, age_seconds: float, **context: Any) -> None:
        super().__init__(message, age_seconds=age_seconds, **context)
        self.age_seconds = age_seconds


# ---------------------------------------------------------------- generation


class GenerationError(RadioError):
    """Music generation failed. Must never reach the playout engine (§26)."""


class GenerationTimeoutError(GenerationError):
    """Generation exceeded its configured deadline (§19)."""


class GenerationCancelledError(GenerationError):
    """Generation was cancelled by the scheduler or an operator."""


class ProviderUnavailableError(GenerationError):
    """The generation provider is unreachable or unhealthy."""


class GpuResourceError(GenerationError):
    """Insufficient or unusable GPU resources (§20)."""


class GpuOutOfMemoryError(GpuResourceError):
    """The generator ran out of VRAM. Triggers the §20 recovery ladder."""


# ---------------------------------------------------------------- quality


class QualityControlError(RadioError):
    """Generated audio failed automated quality control (§24)."""

    def __init__(self, message: str, reasons: list[str], **context: Any) -> None:
        super().__init__(message, reasons=reasons, **context)
        self.reasons = reasons


class OriginalityRejectedError(RadioError):
    """A candidate was rejected by the duplication-prevention policy (§22).

    Note the deliberate wording: this is *internal duplication prevention*, not a
    copyright or originality guarantee (§21, §86).
    """

    def __init__(
        self,
        message: str,
        novelty_score: float,
        closest_track_id: str | None = None,
        **context: Any,
    ) -> None:
        super().__init__(
            message,
            novelty_score=novelty_score,
            closest_track_id=closest_track_id,
            **context,
        )
        self.novelty_score = novelty_score
        self.closest_track_id = closest_track_id


class LyricsRejectedError(RadioError):
    """Lyrics failed safety or quality validation (§17)."""

    def __init__(self, message: str, violations: list[str], **context: Any) -> None:
        super().__init__(message, violations=violations, **context)
        self.violations = violations


# ---------------------------------------------------------------- state machine


class IllegalTransitionError(RadioError):
    """An illegal state transition was attempted (§92)."""

    def __init__(self, from_state: str, to_state: str, **context: Any) -> None:
        super().__init__(
            f"illegal transition {from_state} -> {to_state}",
            from_state=from_state,
            to_state=to_state,
            **context,
        )
        self.from_state = from_state
        self.to_state = to_state


# ---------------------------------------------------------------- radio


class QueueError(RadioError):
    """An invalid queue operation was attempted."""


class ProtectedItemError(QueueError):
    """Refused to mutate a locked or currently-playing queue item (§44)."""


class AudioError(RadioError):
    """Audio could not be read, written or decoded.

    Raised instead of letting libsndfile's ``RuntimeError`` escape, because that exception
    carries the library's message and **not the path** — and on track 4 000 at 3 a.m. the
    path is the only part that matters.
    """


class AudioSinkError(AudioError):
    """The audio output device or sink failed."""


class PersistenceError(RadioError):
    """A database operation failed in a way the caller must handle."""


__all__ = [
    "AudioError",
    "AudioSinkError",
    "ConfigurationError",
    "DependencyMissingError",
    "GenerationCancelledError",
    "GenerationError",
    "GenerationTimeoutError",
    "GpuOutOfMemoryError",
    "GpuResourceError",
    "IllegalTransitionError",
    "LyricsRejectedError",
    "MarketDataError",
    "OriginalityRejectedError",
    "PersistenceError",
    "ProtectedItemError",
    "ProviderUnavailableError",
    "QualityControlError",
    "QueueError",
    "RadioError",
    "StaleMarketDataError",
]
