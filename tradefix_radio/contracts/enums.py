"""Closed vocabularies shared across contracts.

These are ``str`` enums so they serialise readably and compare against database
TEXT columns without a conversion layer.

Deliberately *not* here: genres, lyric topics, personas, and lyric formats. §10
and §14 require those to be config-driven, so they are validated against the
loaded configuration at startup rather than frozen into code. Making them enums
would mean a code change to add a genre — exactly what the brief forbids.
"""

from __future__ import annotations

import enum


class MarketRegime(str, enum.Enum):
    """The §5 regime taxonomy."""

    QUIET = "quiet"
    LOW_VOLATILITY_RANGE = "low_volatility_range"
    NORMAL_RANGE = "normal_range"
    COMPRESSION = "compression"
    BREAKOUT_BUILDUP = "breakout_buildup"
    BULLISH_BREAKOUT = "bullish_breakout"
    BEARISH_BREAKOUT = "bearish_breakout"
    BULLISH_TREND = "bullish_trend"
    BEARISH_TREND = "bearish_trend"
    HIGH_VOLATILITY_RANGE = "high_volatility_range"
    EXTREME_VOLATILITY = "extreme_volatility"
    REVERSAL = "reversal"
    POST_EVENT_NORMALIZATION = "post_event_normalization"
    UNKNOWN = "unknown"

    @property
    def is_directional(self) -> bool:
        """Whether the regime itself implies a direction.

        Used by the lyric director: a directional regime licenses lyrics about
        momentum and continuation; a non-directional one does not, and §14
        forbids implying direction where none is established.
        """
        return self in {
            MarketRegime.BULLISH_BREAKOUT,
            MarketRegime.BEARISH_BREAKOUT,
            MarketRegime.BULLISH_TREND,
            MarketRegime.BEARISH_TREND,
        }

    @property
    def is_high_energy(self) -> bool:
        """Regimes that justify the upper end of the §1 energy mapping."""
        return self in {
            MarketRegime.BULLISH_BREAKOUT,
            MarketRegime.BEARISH_BREAKOUT,
            MarketRegime.EXTREME_VOLATILITY,
            MarketRegime.HIGH_VOLATILITY_RANGE,
        }


class MarketDirection(str, enum.Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class TradingSession(str, enum.Enum):
    """Sessions and overlaps, in XAUUSD-relevant terms (§97)."""

    SYDNEY = "sydney"
    ASIAN = "asian"
    ASIAN_LONDON_OVERLAP = "asian_london_overlap"
    LONDON = "london"
    LONDON_NEW_YORK_OVERLAP = "london_new_york_overlap"
    NEW_YORK = "new_york"
    NEW_YORK_LATE = "new_york_late"
    #: Gold is not continuously traded; the weekend gap is a real state, not an
    #: absence of data. Programming during it must not reference live prices.
    CLOSED = "closed"


class FeedStatus(str, enum.Enum):
    """Health of the market data source (§63-E)."""

    LIVE = "live"
    """Fresh data arriving within the staleness budget."""

    STALE = "stale"
    """Connected but data is older than the budget. Prices MUST NOT be presented
    as current (§32, §86)."""

    DISCONNECTED = "disconnected"
    """No connection. Programming falls back to safe neutral (§63-E)."""

    SIMULATED = "simulated"
    """Data is synthetic. Must be visibly distinguished in the UI so an operator
    can never mistake a simulation for the live market."""

    @property
    def is_trustworthy_for_price_display(self) -> bool:
        """Only LIVE data may be shown or spoken as a current price."""
        return self is FeedStatus.LIVE


class VocalStyle(str, enum.Enum):
    """Broad vocal delivery families (§8 ``vocal.style``)."""

    NONE = "none"
    RAP = "rap"
    MELODIC_RAP = "melodic_rap"
    SUNG = "sung"
    SPOKEN = "spoken"
    CHOPPED_HOOK = "chopped_hook"
    AD_LIB = "ad_lib"


class RunMode(str, enum.Enum):
    """§72 development modes."""

    DEVELOPMENT = "development"
    """Mock market + mock generator. No credentials, no GPU, no internet."""

    SIMULATION = "simulation"
    """Real architecture, simulated market. Real generator optional."""

    PRODUCTION = "production"
    """Real feed, real model, real OBS."""


class HealthStatus(str, enum.Enum):
    """§35 watchdog health states, also used per-component by §73."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    RECOVERING = "recovering"
    UNKNOWN = "unknown"

    @property
    def severity(self) -> int:
        """Orderable severity so the worst component can set overall status."""
        return {
            HealthStatus.HEALTHY: 0,
            HealthStatus.UNKNOWN: 1,
            HealthStatus.RECOVERING: 2,
            HealthStatus.DEGRADED: 3,
            HealthStatus.CRITICAL: 4,
        }[self]


class GenerationPriority(str, enum.Enum):
    """§94 job priorities. Artistic risk falls as operational risk rises."""

    CRITICAL = "critical"
    """Queue is near starvation. Only safe, proven configurations."""

    HIGH = "high"
    """Buffer below target. Conservative choices."""

    NORMAL = "normal"
    """Healthy buffer. Full programming range."""

    EXPERIMENTAL = "experimental"
    """Surplus buffer. Unusual genres and structures permitted (§95)."""

    @property
    def rank(self) -> int:
        """Lower sorts first in the work queue."""
        return {
            GenerationPriority.CRITICAL: 0,
            GenerationPriority.HIGH: 1,
            GenerationPriority.NORMAL: 2,
            GenerationPriority.EXPERIMENTAL: 3,
        }[self]

    @property
    def allows_experimentation(self) -> bool:
        return self in {GenerationPriority.NORMAL, GenerationPriority.EXPERIMENTAL}


class NoveltyVerdict(str, enum.Enum):
    """§22 candidate outcomes."""

    APPROVE = "approve"
    REVIEW = "review"
    REJECT = "reject"


class TransitionType(str, enum.Enum):
    """§30 transition styles."""

    CROSSFADE = "crossfade"
    BEAT_MATCHED_CROSSFADE = "beat_matched_crossfade"
    STATION_ID = "station_id"
    ENERGY_BRIDGE = "energy_bridge"
    AMBIENT = "ambient"
    HARD_CUT = "hard_cut"


class PlayoutTier(str, enum.Enum):
    """§33 playout tiers, in preference order."""

    SCHEDULED = "scheduled"
    """Tier 1 — generated, scheduled music."""

    EMERGENCY_RESERVE = "emergency_reserve"
    """Tier 2 — approved tracks held back for starvation."""

    PROCEDURAL = "procedural"
    """Tier 3 — synthesised layered ambient. Never a single looping file (§33)."""

    SILENCE = "silence"
    """Not a tier so much as a failure to be alerted on loudly (§57)."""


__all__ = [
    "FeedStatus",
    "GenerationPriority",
    "HealthStatus",
    "MarketDirection",
    "MarketRegime",
    "NoveltyVerdict",
    "PlayoutTier",
    "RunMode",
    "TradingSession",
    "TransitionType",
    "VocalStyle",
]
