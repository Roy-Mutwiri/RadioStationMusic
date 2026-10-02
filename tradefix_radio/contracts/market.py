"""Market-side contracts: snapshot, features, state (§4).

Three layers, deliberately separate:

``MarketSnapshotV1``  raw observation — what the feed said
``MarketFeaturesV1``  derived numbers — what the maths says
``MarketStateV1``     interpretation — what the station should believe

Keeping them apart is what makes §47's Market Lab possible: the page can show
raw price, then features, then the classification, and an operator tuning the
regime engine can see exactly which layer is wrong.

Normalisation note (§6): every ``*_percentile`` field is a rank within a rolling
historical window, not an absolute. This is the mechanism by which the engine
makes "no assumptions about absolute XAUUSD price values" — gold at 1 800 and
gold at 4 000 produce identical music for identical *relative* behaviour.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import Field, computed_field, field_validator, model_validator

from tradefix_radio.contracts.base import Contract
from tradefix_radio.contracts.enums import (
    FeedStatus,
    MarketDirection,
    MarketRegime,
    TradingSession,
)

#: A value already normalised to a 0–100 percentile rank.
Percentile = Annotated[float, Field(ge=0.0, le=100.0)]

#: A 0–100 score. Separate alias from Percentile so intent reads clearly.
Score100 = Annotated[float, Field(ge=0.0, le=100.0)]

#: A unit-interval confidence or ratio.
Unit = Annotated[float, Field(ge=0.0, le=1.0)]


class MarketSnapshotV1(Contract):
    """One observation of the instrument (§4).

    Both tick fields (``bid``/``ask``) and candle fields (``open``..``close``) are
    present because feeds supply one, the other, or both: MT5 gives ticks and
    bars; REST providers usually give bars only. Candle fields are optional so a
    tick-only feed does not have to fabricate them — §86 forbids fake market data,
    and that includes synthesising a candle we were not given.
    """

    symbol: str = Field(min_length=1, max_length=32)
    timestamp: datetime

    bid: float = Field(gt=0.0)
    ask: float = Field(gt=0.0)

    open: float | None = Field(default=None, gt=0.0)
    high: float | None = Field(default=None, gt=0.0)
    low: float | None = Field(default=None, gt=0.0)
    close: float | None = Field(default=None, gt=0.0)
    tick_volume: float | None = Field(default=None, ge=0.0)

    #: Marks synthetic data so it can never be mistaken for live (§7).
    synthetic: bool = False

    @field_validator("timestamp")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware (UTC)")
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> MarketSnapshotV1:
        if self.ask < self.bid:
            raise ValueError(f"ask ({self.ask}) is below bid ({self.bid})")
        if self.high is not None and self.low is not None and self.high < self.low:
            raise ValueError(f"high ({self.high}) is below low ({self.low})")
        # A candle is only meaningful if open/close sit inside the range.
        for name in ("open", "close"):
            value = getattr(self, name)
            if value is None or self.high is None or self.low is None:
                continue
            if not (self.low <= value <= self.high):
                raise ValueError(f"{name} ({value}) outside [low, high]")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mid(self) -> float:
        """Mid price. Computed rather than stored so it cannot disagree."""
        return (self.bid + self.ask) / 2.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def spread(self) -> float:
        return self.ask - self.bid


class MarketFeaturesV1(Contract):
    """The §4 feature vector.

    ``sufficient_history`` is the honest flag: during warm-up the engine has real
    but unreliable numbers. Rather than emitting garbage or zeros that look like
    data, features are published with this set to ``False`` and the regime engine
    refuses to classify (yielding ``UNKNOWN``). §24's spirit — never pass
    unusable output downstream — applied to market data.
    """

    symbol: str = Field(min_length=1, max_length=32)
    timestamp: datetime
    sufficient_history: bool
    samples_observed: int = Field(ge=0)

    # -- returns over several horizons
    returns_1m: float
    returns_5m: float
    returns_15m: float

    # -- volatility
    atr: float = Field(ge=0.0)
    atr_percentile: Percentile
    realized_volatility: float = Field(ge=0.0)
    range_percentile: Percentile

    # -- trend / momentum
    adx: float = Field(ge=0.0, le=100.0)
    rsi: float = Field(ge=0.0, le=100.0)
    moving_average_slope: float
    trend_strength: Score100
    momentum: float

    # -- participation
    volume_percentile: Percentile

    # -- positioning within the session
    session_range_position: Unit
    distance_from_high: float = Field(ge=0.0)
    distance_from_low: float = Field(ge=0.0)

    # -- structural state
    breakout_strength: Score100
    compression_score: Score100
    expansion_score: Score100

    @field_validator("timestamp")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware (UTC)")
        return value


class MarketEnergyV1(Contract):
    """The §6 energy triple.

    ``energy_velocity`` exists because §6 notes music can *prepare* for rising
    intensity. It is the smoothed rate of change in points-per-minute, signed.
    """

    raw_energy: Score100
    smoothed_energy: Score100
    energy_velocity: float
    #: Per-component contributions, for the §47 Market Lab breakdown. Keys are
    #: configured weight names, values are the weighted contribution in points.
    components: dict[str, float] = Field(default_factory=dict)


class MarketStateV1(Contract):
    """The station's belief about the market (§4 example schema).

    This is the single object the music director consumes. It deliberately does
    *not* carry raw price for the director's benefit — musical decisions must not
    depend on gold's absolute level (§6). ``price`` is present only so the UI and
    AI DJ can display it, and is gated by ``feed_status`` (§32).
    """

    symbol: str = Field(min_length=1, max_length=32)
    timestamp: datetime

    regime: MarketRegime
    direction: MarketDirection
    session: TradingSession
    feed_status: FeedStatus

    energy: Score100
    energy_velocity: float
    volatility: Score100
    trend_strength: Score100
    momentum: Score100
    compression: Score100

    confidence: Unit
    #: Seconds the current regime has been held — feeds §5 minimum-duration and
    #: the §43 regime timeline.
    regime_age_seconds: float = Field(ge=0.0)
    #: Age of the underlying observation. Drives the stale indicator (§63-E).
    data_age_seconds: float = Field(ge=0.0)

    #: Present only when the feed is trustworthy. ``None`` is a deliberate,
    #: meaningful value: "we do not know the price right now".
    price: float | None = Field(default=None, gt=0.0)

    @field_validator("timestamp")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware (UTC)")
        return value

    @model_validator(mode="after")
    def _enforce_price_honesty(self) -> MarketStateV1:
        """A price may only be carried when the feed status permits it.

        This is a hard contract-level rule rather than a UI convention, because
        §32/§86 forbid fabricating prices and the cheapest way to guarantee that
        is to make an untrustworthy price *unrepresentable*.
        """
        if self.price is not None and not self.feed_status.is_trustworthy_for_price_display:
            raise ValueError(
                f"price must be None when feed_status is {self.feed_status.value}; "
                "presenting non-live data as a current price is forbidden"
            )
        if self.regime is MarketRegime.UNKNOWN and self.confidence > 0.5:
            raise ValueError("UNKNOWN regime cannot carry confidence above 0.5")
        return self

    @property
    def is_simulated(self) -> bool:
        return self.feed_status is FeedStatus.SIMULATED

    @property
    def is_usable_for_programming(self) -> bool:
        """Whether music decisions may use this state's energy and regime.

        A disconnected feed yields neutral programming (§63-E) rather than
        programming driven by a frozen snapshot that may be hours old.
        """
        return self.feed_status in {FeedStatus.LIVE, FeedStatus.SIMULATED}

    def neutralised(self) -> MarketStateV1:
        """A safe, directionless version of this state for degraded operation.

        Used when the feed dies: the station keeps broadcasting, but at mid
        energy with no directional or regime claim, so nothing it implies about
        the market can be wrong.
        """
        return self.model_copy(
            update={
                "regime": MarketRegime.UNKNOWN,
                "direction": MarketDirection.NEUTRAL,
                "energy": 50.0,
                "energy_velocity": 0.0,
                "volatility": 50.0,
                "trend_strength": 0.0,
                "momentum": 50.0,
                "compression": 50.0,
                "confidence": 0.0,
                "price": None,
            }
        )


__all__ = [
    "MarketEnergyV1",
    "MarketFeaturesV1",
    "MarketSnapshotV1",
    "MarketStateV1",
    "Percentile",
    "Score100",
    "Unit",
]
