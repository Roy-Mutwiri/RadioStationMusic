"""Validated, layered configuration (§50, §71).

Application code should obtain settings through :func:`load_settings` once at
startup and pass the resulting :class:`AppSettings` down explicitly. There is no
module-level singleton on purpose: a global would be mutated by tests, would make
the three processes (ADR-08) appear to share state they do not, and would hide
which subsystems actually depend on which settings.
"""

from tradefix_radio.config.loader import (
    CONFIG_DIR,
    ENV_NESTED_DELIMITER,
    ENV_PREFIX,
    ensure_directories,
    load_settings,
)
from tradefix_radio.config.schema import (
    AceStepSettings,
    ApiSettings,
    AppSettings,
    AudioSettings,
    BpmBand,
    DatabaseSettings,
    DiversitySettings,
    EnergySettings,
    EnergyWeights,
    GenerationSettings,
    LoggingSettings,
    LyricsSettings,
    MarketSettings,
    MasteringSettings,
    MockProviderSettings,
    MonitoringSettings,
    MusicSettings,
    ObsSettings,
    OriginalitySettings,
    OriginalityWeights,
    PathSettings,
    QualityControlSettings,
    RadioSettings,
    RegimeSettings,
    RetentionSettings,
)

__all__ = [
    "CONFIG_DIR",
    "ENV_NESTED_DELIMITER",
    "ENV_PREFIX",
    "AceStepSettings",
    "ApiSettings",
    "AppSettings",
    "AudioSettings",
    "BpmBand",
    "DatabaseSettings",
    "DiversitySettings",
    "EnergySettings",
    "EnergyWeights",
    "GenerationSettings",
    "LoggingSettings",
    "LyricsSettings",
    "MarketSettings",
    "MasteringSettings",
    "MockProviderSettings",
    "MonitoringSettings",
    "MusicSettings",
    "ObsSettings",
    "OriginalitySettings",
    "OriginalityWeights",
    "PathSettings",
    "QualityControlSettings",
    "RadioSettings",
    "RegimeSettings",
    "RetentionSettings",
    "ensure_directories",
    "load_settings",
]
