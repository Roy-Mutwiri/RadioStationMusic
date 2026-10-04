"""Is a market tradable right now, and how confident are we?

The question sounds binary and is not. "No ticks are arriving" is consistent with a closed
market, a dead feed, a network partition, and a broker that logged us out — and those call
for opposite responses. Switching the station to Bitcoin because the gold feed crashed
would be a bug that looks like a feature.

So availability is an explicit state with **evidence attached**, and the two failure shapes
are kept apart by name:

``CLOSED``       the market is not trading, and we believe that for a calendar reason
``OPEN``         fresh data is arriving, or the calendar says trading and the feed is well
``STALE``        data was arriving and has slowed past tolerance while the calendar says open
``UNAVAILABLE``  the feed itself is down or was never reachable
``UNKNOWN``      not enough information yet — a feed that has not produced its first tick

Only ``CLOSED`` authorises a fallback switch. ``STALE`` and ``UNAVAILABLE`` are feed
problems, they are surfaced as feed problems, and they leave the active market alone.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Final

from tradefix_radio.contracts.enums import FeedStatus
from tradefix_radio.market.sessions import is_market_open

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradefix_radio.config.schema import MarketSymbolSettings

__all__ = [
    "AvailabilityAssessment",
    "MarketAvailability",
    "assess_availability",
]


class MarketAvailability(str, enum.Enum):
    """What we believe about one symbol's tradability."""

    OPEN = "open"
    CLOSED = "closed"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"

    @property
    def is_usable(self) -> bool:
        """Whether the station can program against this market.

        ``STALE`` counts as usable: the data is late, not wrong, and a brief slowdown
        mid-session should not take the market away from the director. The feed status is
        surfaced separately so an operator can see it degrade.
        """
        return self in (MarketAvailability.OPEN, MarketAvailability.STALE)

    @property
    def authorises_fallback(self) -> bool:
        """Whether this state justifies moving the station to another symbol.

        **Only CLOSED.** This property exists so the rule lives in one place and reads the
        same at every call site, rather than being spelled out as a comparison that someone
        later widens to "not usable" in a hurry.
        """
        return self is MarketAvailability.CLOSED


#: Calendar knowledge per symbol family.
#:
#: Crypto trades continuously; FX and metals do not. Keyed by a symbol *trait* rather than
#: by symbol so adding ETHUSD does not need a code change.
_CONTINUOUS_SUFFIXES: Final = ("BTC", "ETH", "SOL", "XRP", "DOGE", "LTC", "ADA")


def _is_continuous(symbol: str) -> bool:
    """Whether this market trades 24/7.

    A crypto symbol is never CLOSED for calendar reasons, so an absence of data there is
    always a feed problem — which is the distinction this whole module exists to keep.
    """
    upper = symbol.upper()
    return any(upper.startswith(prefix) for prefix in _CONTINUOUS_SUFFIXES)


@dataclass(frozen=True)
class AvailabilityAssessment:
    """One symbol's availability, and why.

    ``reason`` is written for an operator reading the Market page at 3 a.m., not for a log
    parser. The structured fields underneath it are what tests and the router use.
    """

    symbol: str
    state: MarketAvailability
    reason: str
    #: Seconds since the last accepted snapshot. ``None`` when nothing has ever arrived.
    data_age_seconds: float | None = None
    feed_status: FeedStatus = FeedStatus.DISCONNECTED
    #: What the trading calendar says, independent of whether data is flowing.
    calendar_open: bool | None = None
    assessed_at: datetime | None = None
    #: Set when the market is expected to be trading but data is not arriving — the
    #: condition that must never be mistaken for a closure.
    feed_degraded: bool = False
    extras: dict[str, object] = field(default_factory=dict)

    def as_payload(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "state": self.state.value,
            "reason": self.reason,
            "data_age_seconds": self.data_age_seconds,
            "feed_status": self.feed_status.value,
            "calendar_open": self.calendar_open,
            "feed_degraded": self.feed_degraded,
            "assessed_at": None if self.assessed_at is None else self.assessed_at.isoformat(),
        }


def assess_availability(
    *,
    symbol: str,
    now: datetime,
    settings: MarketSymbolSettings,
    data_age_seconds: float | None,
    feed_status: FeedStatus,
    bars_processed: int,
    feed_reported_closed: bool | None = None,
    calendar_open_override: bool | None = None,
) -> AvailabilityAssessment:
    """Decide one symbol's availability from everything we know about it.

    The inputs are deliberately plain values rather than a service object, so this is a pure
    function that tests can drive through every combination without a running feed.

    ``feed_reported_closed`` is the provider's own answer when it has one — MetaTrader can
    say a symbol's session is shut. It outranks the calendar, because a venue knows its own
    holidays and our calendar deliberately does not model them.

    ``calendar_open_override`` replaces the calendar verdict and nothing else. It exists for
    the simulator, and it is applied *here* rather than by rewriting the result afterwards
    for a reason worth stating: the calendar branch returns early, so patching its output
    later discards every check that sits below it. An override applied after the fact made
    a symbol with a dead feed report OPEN all weekend — masking exactly the staleness the
    override was added to let someone test.
    """
    continuous = _is_continuous(symbol)
    if calendar_open_override is not None:
        calendar_open = calendar_open_override
    else:
        calendar_open = True if continuous else is_market_open(now)

    def build(
        state: MarketAvailability, reason: str, *, degraded: bool = False
    ) -> AvailabilityAssessment:
        return AvailabilityAssessment(
            symbol=symbol,
            state=state,
            reason=reason,
            data_age_seconds=data_age_seconds,
            feed_status=feed_status,
            calendar_open=calendar_open,
            assessed_at=now,
            feed_degraded=degraded,
            extras={"continuous": continuous},
        )

    # 1. The provider's own word, when it has one. A venue knows its holidays.
    if feed_reported_closed is True:
        return build(MarketAvailability.CLOSED, "the data provider reports the session closed")

    # 2. Calendar closure. For a continuous market this branch is unreachable, which is the
    #    point: crypto is never CLOSED for a calendar reason, so a crypto outage can only
    #    ever be classified as a feed problem.
    if not calendar_open:
        return build(
            MarketAvailability.CLOSED,
            "outside the trading week for this instrument",
        )

    # 3. The feed never connected, or dropped. The market may well be open; we cannot see it.
    if feed_status is FeedStatus.DISCONNECTED:
        if bars_processed == 0:
            return build(
                MarketAvailability.UNKNOWN,
                "the feed has not produced any data yet",
            )
        return build(
            MarketAvailability.UNAVAILABLE,
            "the feed is disconnected while the market should be trading",
            degraded=True,
        )

    # 4. Nothing has ever arrived from a feed that claims to be up.
    if bars_processed == 0 or data_age_seconds is None:
        return build(MarketAvailability.UNKNOWN, "awaiting the first observation")

    # 5. Data is late. Late is not closed: the calendar says this market is trading, so the
    #    honest reading is a degraded feed, and the station keeps programming against it.
    if data_age_seconds > settings.unavailable_after_seconds:
        return build(
            MarketAvailability.UNAVAILABLE,
            f"no data for {data_age_seconds:.0f}s while the market should be trading",
            degraded=True,
        )
    if data_age_seconds > settings.stale_after_seconds:
        return build(
            MarketAvailability.STALE,
            f"data is {data_age_seconds:.0f}s old",
            degraded=True,
        )

    return build(MarketAvailability.OPEN, "fresh data is arriving")
