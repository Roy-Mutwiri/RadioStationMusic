"""Market feed abstraction (ADR-03).

A pull-based interface, deliberately. Three reasons:

**The virtual clock drives it.** A push/callback feed owns its own timing, which
would make §64's accelerated endurance runs impossible. With ``poll()``, the market
service decides when to ask, and under a :class:`~tradefix_radio.core.clock.VirtualClock`
a simulated week elapses in seconds.

**Staleness is observable.** The service knows exactly when it last received data,
so :class:`~tradefix_radio.contracts.enums.FeedStatus` is derived from a measurement
rather than from a callback that stopped arriving — which is indistinguishable from
a quiet market.

**``poll()`` returning ``None`` is normal.** Between ticks there is simply nothing
new. A feed that blocked until data arrived would hide a dead connection behind a
hang, and §63-E requires a dead feed to be a *visible state* rather than a stall.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tradefix_radio.contracts.market import MarketSnapshotV1


@runtime_checkable
class MarketFeed(Protocol):
    """Source of market snapshots."""

    @property
    def name(self) -> str:
        """Short identifier used in logs and on the §49 System page."""
        ...

    @property
    def is_simulated(self) -> bool:
        """Whether data is synthetic.

        Drives ``FeedStatus.SIMULATED``, which the UI must display distinctly so an
        operator can never mistake a simulation for the live market (§7).
        """
        ...

    @property
    def resolved_symbol(self) -> str:
        """The symbol actually in use, after broker alias discovery (ADR-03)."""
        ...

    async def open(self) -> None:
        """Connect or initialise. Raises :class:`MarketDataError` on failure."""
        ...

    async def close(self) -> None:
        """Release resources. Must be safe to call when never opened."""
        ...

    async def poll(self) -> MarketSnapshotV1 | None:
        """Return the newest snapshot, or ``None`` if nothing is new.

        Must not raise for ordinary absence of data. Raising is reserved for a
        genuinely broken connection, which the service converts into
        ``FeedStatus.DISCONNECTED``.
        """
        ...


class FeedBase:
    """Shared bookkeeping for concrete feeds.

    Tracks open/closed state so every implementation does not reinvent the
    "close() before open()" and "double open()" cases, both of which occur in real
    operation: the watchdog may restart a feed that failed mid-open.
    """

    __slots__ = ("_is_open", "_symbol")

    def __init__(self, symbol: str) -> None:
        self._symbol = symbol
        self._is_open = False

    @property
    def resolved_symbol(self) -> str:
        return self._symbol

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def is_simulated(self) -> bool:
        return False


__all__ = ["FeedBase", "MarketFeed"]
