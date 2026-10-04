"""Repositories.

One per aggregate. All take an :class:`~sqlalchemy.ext.asyncio.AsyncSession` and
none commit — the caller owns the transaction, so a single logical operation can
span several repositories atomically. See :mod:`.base`.
"""

from tradefix_radio.persistence.repositories.base import Repository
from tradefix_radio.persistence.repositories.events import (
    HealthEventRepository,
    MetricPoint,
    MetricRepository,
    SystemEventRepository,
)
from tradefix_radio.persistence.repositories.files import TrackFileRepository
from tradefix_radio.persistence.repositories.jobs import (
    GenerationJobRepository,
    JobRecord,
)
from tradefix_radio.persistence.repositories.lyrics import LyricsRepository
from tradefix_radio.persistence.repositories.memory import (
    MemoryKeys,
    RadioMemoryRepository,
)
from tradefix_radio.persistence.repositories.originality import (
    DEFAULT_LIBRARY_LIMIT,
    OriginalityRepository,
    StoredQcCheck,
    StoredQcResult,
    TrackEvidence,
)
from tradefix_radio.persistence.repositories.queue import QueueRepository
from tradefix_radio.persistence.repositories.submissions import (
    ProviderSubmissionsRepository,
)
from tradefix_radio.persistence.repositories.tracks import (
    TrackHistoryEntry,
    TrackRepository,
    normalise_title,
)

__all__ = [
    "DEFAULT_LIBRARY_LIMIT",
    "GenerationJobRepository",
    "HealthEventRepository",
    "JobRecord",
    "LyricsRepository",
    "MemoryKeys",
    "MetricPoint",
    "MetricRepository",
    "OriginalityRepository",
    "ProviderSubmissionsRepository",
    "QueueRepository",
    "RadioMemoryRepository",
    "Repository",
    "StoredQcCheck",
    "StoredQcResult",
    "SystemEventRepository",
    "TrackEvidence",
    "TrackFileRepository",
    "TrackHistoryEntry",
    "TrackRepository",
    "normalise_title",
]
