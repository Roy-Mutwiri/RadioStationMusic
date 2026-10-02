"""Music generation: the swappable backend and the work that drives it (§18–§20, §62, §70).

:class:`~tradefix_radio.generation.provider.MusicGenerationProvider` is §18's seam. Nothing
downstream of it knows which model produced a track, and nothing upstream of it knows that a
model exists — which is what makes ACE-Step a new adapter in Phase 7 rather than a rewrite.

:class:`~tradefix_radio.generation.mock.MockMusicProvider` is not a stub. §62 requires real
audio from the mock, because the entire station is built and proven against it before any
model is installed, and a provider returning silence would let every downstream stage be
written against input that cannot exercise it.
"""

from tradefix_radio.generation.capacity import CapacitySnapshot, CapacityTracker
from tradefix_radio.generation.manager import (
    DatabaseJobUnitOfWork,
    GenerationManager,
    GenerationOutcome,
    ManagerStats,
    classify_failure,
)
from tradefix_radio.generation.mock import MockMusicProvider
from tradefix_radio.generation.provider import (
    GenerationRequest,
    GenerationResult,
    MusicGenerationProvider,
    ProgressCallback,
    ProviderDescription,
    ProviderHealth,
)

__all__ = [
    "CapacitySnapshot",
    "CapacityTracker",
    "DatabaseJobUnitOfWork",
    "GenerationManager",
    "GenerationOutcome",
    "GenerationRequest",
    "GenerationResult",
    "ManagerStats",
    "MockMusicProvider",
    "MusicGenerationProvider",
    "ProgressCallback",
    "ProviderDescription",
    "ProviderHealth",
    "classify_failure",
]
