"""Versioned cross-boundary data contracts (§90).

Import contracts from this package rather than from submodules, so the public
surface stays stable if a model moves. When a breaking change becomes necessary,
add ``...V2`` alongside ``...V1`` and migrate consumers deliberately — never edit
a V1 in place, because persisted rows and in-flight WebSocket frames contain it.
"""

from tradefix_radio.contracts.audio import (
    AudioFingerprintV1,
    AudioMetricsV1,
    MasteringResultV1,
    QualityIssueV1,
    SimilarityComponentV1,
    SimilarityResultV1,
    TrackAnalysisV1,
)
from tradefix_radio.contracts.base import Contract, MutableContract
from tradefix_radio.contracts.enums import (
    FeedStatus,
    GenerationPriority,
    HealthStatus,
    MarketDirection,
    MarketRegime,
    NoveltyVerdict,
    PlayoutTier,
    RunMode,
    TradingSession,
    TransitionType,
    VocalStyle,
)
from tradefix_radio.contracts.generation import (
    GenerationFailureV1,
    GenerationRequestV1,
    GenerationResultV1,
    ProviderHealthV1,
)
from tradefix_radio.contracts.lyrics import (
    LyricLineV1,
    LyricsV1,
    LyricValidationResultV1,
    LyricViolationV1,
)
from tradefix_radio.contracts.market import (
    MarketEnergyV1,
    MarketFeaturesV1,
    MarketSnapshotV1,
    MarketStateV1,
)
from tradefix_radio.contracts.music import (
    BlueprintMarketContextV1,
    CompositionSpecV1,
    LyricsSpecV1,
    MusicBlueprint,
    MusicBlueprintV1,
    NoveltySpecV1,
    VocalSpecV1,
)
from tradefix_radio.contracts.queue import (
    BufferHealthV1,
    NowPlayingV1,
    PlayEventV1,
    QueueItemV1,
    QueueLockLevel,
)

__all__ = [
    "AudioFingerprintV1",
    "AudioMetricsV1",
    "BlueprintMarketContextV1",
    "BufferHealthV1",
    "CompositionSpecV1",
    "Contract",
    "FeedStatus",
    "GenerationFailureV1",
    "GenerationPriority",
    "GenerationRequestV1",
    "GenerationResultV1",
    "HealthStatus",
    "LyricLineV1",
    "LyricValidationResultV1",
    "LyricViolationV1",
    "LyricsSpecV1",
    "LyricsV1",
    "MarketDirection",
    "MarketEnergyV1",
    "MarketFeaturesV1",
    "MarketRegime",
    "MarketSnapshotV1",
    "MarketStateV1",
    "MasteringResultV1",
    "MusicBlueprint",
    "MusicBlueprintV1",
    "MutableContract",
    "NoveltySpecV1",
    "NoveltyVerdict",
    "NowPlayingV1",
    "PlayEventV1",
    "PlayoutTier",
    "ProviderHealthV1",
    "QualityIssueV1",
    "QueueItemV1",
    "QueueLockLevel",
    "RunMode",
    "SimilarityComponentV1",
    "SimilarityResultV1",
    "TrackAnalysisV1",
    "TradingSession",
    "TransitionType",
    "VocalSpecV1",
    "VocalStyle",
]
