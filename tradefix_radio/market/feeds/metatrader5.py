"""MetaTrader 5 feed — the production default (ADR-03).

Attaches to an already-running, logged-in MT5 terminal. No API key, no broker
credentials in our configuration, no paid data subscription: the terminal is already
authenticated, and ``MetaTrader5.initialize()`` simply connects to it.

Three things this module takes care of that are easy to get wrong:

**Symbol discovery.** Brokers name gold inconsistently — ``XAUUSD``, ``XAUUSD.m``,
``GOLD``, ``XAUUSD_``. The configured aliases are tried in order and the first that
exists wins. Hard-coding one name would make the station broker-specific.

**Market Watch selection.** A symbol that exists but is not selected in Market Watch
returns ``None`` ticks forever. ``symbol_select`` is called explicitly, because the
failure mode otherwise looks exactly like a dead market.

**Blocking calls go to a thread.** Every MT5 entry point is synchronous and some take
hundreds of milliseconds. Called directly they would stall the event loop, and with
it the playout mixer.

> **Verification status:** this adapter is written against the documented MT5 Python
> API but has **not** been exercised against a live terminal in this environment (no
> account was logged in — see `docs/INITIAL_AUDIT.md` R-04). It is covered by unit
> tests using an injected fake client, and is marked for validation against a real
> terminal before production use. Nothing else depends on it: development and
> simulation run on `SimulatedFeed`.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

import structlog

from tradefix_radio.contracts.market import MarketSnapshotV1
from tradefix_radio.core.clock import UTC
from tradefix_radio.core.errors import MarketDataError
from tradefix_radio.market.feeds.base import FeedBase

_log = structlog.get_logger(__name__)


class Mt5Client(Protocol):
    """The subset of the MetaTrader5 module this feed uses.

    Declared as a Protocol so tests inject a fake. Without it, the only way to test
    symbol discovery, Market Watch selection and tick conversion would be to have a
    broker account logged in — which would make the suite untestable in CI and on any
    machine but one.
    """

    def initialize(self, *args: Any, **kwargs: Any) -> bool: ...
    def shutdown(self) -> None: ...
    def last_error(self) -> tuple[int, str]: ...
    def symbol_info(self, symbol: str) -> Any: ...
    def symbol_select(self, symbol: str, enable: bool) -> bool: ...
    def symbol_info_tick(self, symbol: str) -> Any: ...
    def terminal_info(self) -> Any: ...


class MetaTrader5Feed(FeedBase):
    """Live XAUUSD ticks from a running MetaTrader 5 terminal."""

    def __init__(
        self,
        *,
        symbol: str = "XAUUSD",
        symbol_aliases: Sequence[str] = ("XAUUSD",),
        client: Mt5Client | None = None,
    ) -> None:
        super().__init__(symbol)
        self._configured_symbol = symbol
        self._aliases = tuple(symbol_aliases) or (symbol,)
        self._client = client
        self._last_tick_time: float | None = None
        self._terminal_build: str | None = None

    @property
    def name(self) -> str:
        return f"metatrader5:{self._symbol}"

    @property
    def is_simulated(self) -> bool:
        return False

    @property
    def terminal_build(self) -> str | None:
        """Terminal build string, shown on the §49 System page."""
        return self._terminal_build

    # -- lifecycle ---------------------------------------------------------

    async def open(self) -> None:
        if self._is_open:
            return
        client = self._resolve_client()

        if not await asyncio.to_thread(client.initialize):
            code, message = await asyncio.to_thread(client.last_error)
            raise MarketDataError(
                "MetaTrader 5 initialize() failed; the terminal must be running and "
                "logged in to an account that quotes gold",
                mt5_code=code,
                mt5_message=message,
            )

        try:
            resolved = await self._discover_symbol(client)
        except MarketDataError:
            # Leaving the terminal connection open after a failed open would leak it
            # across watchdog restarts.
            await asyncio.to_thread(client.shutdown)
            raise

        self._symbol = resolved
        terminal = await asyncio.to_thread(client.terminal_info)
        self._terminal_build = str(getattr(terminal, "build", "") or "") or None
        self._is_open = True
        _log.info(
            "feed.mt5_connected",
            symbol=resolved,
            configured=self._configured_symbol,
            terminal_build=self._terminal_build,
        )

    async def _discover_symbol(self, client: Mt5Client) -> str:
        """Find the broker's name for gold and enable it in Market Watch."""
        tried: list[str] = []
        for alias in self._aliases:
            tried.append(alias)
            info = await asyncio.to_thread(client.symbol_info, alias)
            if info is None:
                continue
            # A symbol can exist yet be absent from Market Watch, in which case
            # symbol_info_tick returns None indefinitely — indistinguishable from a
            # closed market unless we select it explicitly.
            if not getattr(info, "visible", True):
                selected = await asyncio.to_thread(client.symbol_select, alias, True)
                if not selected:
                    _log.warning("feed.mt5_symbol_select_failed", symbol=alias)
                    continue
            return alias
        raise MarketDataError(
            "no configured gold symbol exists on this broker",
            tried=tried,
            remedy="add the broker's gold symbol to market.symbol_aliases",
        )

    def _resolve_client(self) -> Mt5Client:
        if self._client is not None:
            return self._client
        if sys.platform != "win32":
            raise MarketDataError(
                "the MetaTrader5 feed is Windows-only; use market.feed=rest or "
                "market.feed=simulated on this platform"
            )
        try:
            import MetaTrader5  # noqa: PLC0415 - optional Windows-only extra
        except ImportError as exc:
            raise MarketDataError(
                "the MetaTrader5 package is not installed; "
                'install the Windows extra with pip install -e ".[mt5]"'
            ) from exc
        client: Mt5Client = MetaTrader5
        self._client = client
        return client

    async def close(self) -> None:
        if not self._is_open:
            return
        self._is_open = False
        if self._client is not None:
            await asyncio.to_thread(self._client.shutdown)
        _log.info("feed.mt5_disconnected", symbol=self._symbol)

    # -- polling -----------------------------------------------------------

    async def poll(self) -> MarketSnapshotV1 | None:
        """Fetch the latest tick, or ``None`` if it has not changed.

        Deduplicating on the tick's own timestamp matters: MT5 returns the last known
        tick regardless of whether it is new, so without this the feature engine would
        see a flood of identical observations in a quiet market and compute a volume
        profile from nothing.
        """
        if not self._is_open or self._client is None:
            return None
        tick = await asyncio.to_thread(self._client.symbol_info_tick, self._symbol)
        if tick is None:
            # Not an error: the market may be closed, or the symbol momentarily
            # unavailable. The service's staleness tracking reports it correctly.
            return None

        tick_time = float(getattr(tick, "time_msc", 0) or 0) / 1000.0
        if tick_time <= 0:
            tick_time = float(getattr(tick, "time", 0) or 0)
        if tick_time <= 0:
            return None
        if self._last_tick_time is not None and tick_time <= self._last_tick_time:
            return None
        self._last_tick_time = tick_time

        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
        if bid <= 0 or ask <= 0:
            # A zero quote is bad data, not a price. Contracts would reject it; we
            # drop it here with a log rather than raising, because a single malformed
            # tick must not take the feed down.
            _log.warning("feed.mt5_invalid_quote", symbol=self._symbol, bid=bid, ask=ask)
            return None

        volume = getattr(tick, "volume_real", None) or getattr(tick, "volume", None)
        return MarketSnapshotV1(
            symbol=self._symbol,
            timestamp=datetime.fromtimestamp(tick_time, tz=UTC),
            bid=bid,
            ask=max(ask, bid),
            tick_volume=float(volume) if volume else None,
            synthetic=False,
        )


__all__ = ["MetaTrader5Feed", "Mt5Client"]
