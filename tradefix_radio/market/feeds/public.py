"""Keyless public feed: real prices with no account, no terminal and no API key.

The station's production feeds need something a listener does not have: a MetaTrader
terminal or a quote vendor's key. This feed needs nothing. It reads public endpoints that
answer without credentials, so the desktop app can play against the real market on any
machine straight after install.

Per symbol, two sources — one for the live quote, one for history:

======  ==============================================  ==================================
symbol  live quote                                      history (one-minute bars)
======  ==============================================  ==================================
XAUUSD  Binance PAXG ticks anchored to gold-api.com     Yahoo Finance front-month gold
        spot (see below)                                future
BTCUSD  Binance ``bookTicker`` (bid/ask, instant)       Binance one-minute klines
======  ==============================================  ==================================

Spot gold's public source updates about once a minute, which on a dashboard reads as a
price that is stuck while the market moves. PAXG - gold held in a vault, traded on Binance
tick by tick around the clock - moves with gold second by second but sits a small premium
away from spot. So the gold quote is PAXG's tick scaled by the ratio of spot to PAXG at the
last spot update: spot's *level*, PAXG's *motion*. The ratio is refreshed with every spot
update and rejected if the two drift further apart than a basis ever should.

History matters because the regime engine wants ``warmup_bars`` closed bars before it will
classify anything, and ``percentile_window_bars`` before its normalisation means much. At
one bar a minute that is an hour of silence about the market, so :meth:`open` fetches the
recent bars and :meth:`poll` replays them before the first live quote. Each bar is replayed
as four ticks — open, high, low, close — spaced inside its minute, so the aggregator rebuilds
a bar with a real range rather than a flat line. Replayed ticks are marked ``synthetic``:
they are reshaped from real bars, but the ticks themselves were never observed.

Gold's history and live quote come from different venues (a future versus spot), which sit
a fraction of a percent apart. The history is scaled onto the live level before replay so
the join is not read as a price jump; shape, not level, is what the features use.

Everything degrades rather than fails: no history means a cold start, a failed live poll is
``None`` until :data:`FAILURE_THRESHOLD` of them in a row, and the calendar in
:mod:`tradefix_radio.market.sessions` — not this feed — says when gold is closed.
"""
from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timedelta
from typing import Any

import httpx
import structlog

from tradefix_radio.contracts.market import MarketSnapshotV1
from tradefix_radio.core.clock import UTC, Clock, SystemClock
from tradefix_radio.core.errors import MarketDataError
from tradefix_radio.market.feeds.base import FeedBase

_log = structlog.get_logger(__name__)

GOLD_LIVE_URL = "https://api.gold-api.com/price/XAU"
GOLD_HISTORY_URL = "https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF"
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/bookTicker"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

#: Binance's spelling of the symbols this feed knows how to serve.
_BINANCE_SYMBOLS = {"BTCUSD": "BTCUSDT", "ETHUSD": "ETHUSDT"}
#: Binance's tokenised gold, the tick source behind XAUUSD.
_GOLD_TICK_SYMBOL = "PAXGUSDT"
#: How often spot gold is re-read to refresh the PAXG anchor (its source updates ~1/min).
GOLD_ANCHOR_SECONDS = 60.0
#: Consecutive live failures after which the feed reports itself broken (§63-E).
FAILURE_THRESHOLD = 5
#: Bars of history to replay: more than the engine's percentile window, fewer than a day.
HISTORY_BARS = 800
#: Four ticks per replayed bar, inside the minute, in the order a bar actually forms.
_TICK_OFFSETS = (0, 15, 30, 45)
#: Reject a history/live splice this far apart: a different instrument, not a basis.
_MAX_SPLICE_RATIO = 0.05


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


class PublicFeed(FeedBase):
    """Real quotes from public, keyless endpoints, with replayed history (see module doc)."""

    def __init__(
        self,
        symbol: str,
        *,
        clock: Clock | None = None,
        client: httpx.AsyncClient | None = None,
        live_interval_seconds: float | None = None,
        history_bars: int = HISTORY_BARS,
        timeout_seconds: float = 8.0,
    ) -> None:
        super().__init__(symbol)
        upper = symbol.upper()
        if upper not in ("XAUUSD", *_BINANCE_SYMBOLS):
            raise MarketDataError(
                f"the public feed serves XAUUSD and {', '.join(_BINANCE_SYMBOLS)}, not {symbol!r}"
            )
        self._clock: Clock = clock or SystemClock()
        self._owns_client = client is None
        self._client = client
        self._timeout = timeout_seconds
        self._history_bars = history_bars
        # Spot gold updates about once a minute at its source; Binance is instant. Polling
        # faster than the source updates only spends requests.
        self._live_interval = live_interval_seconds if live_interval_seconds is not None else 1.0
        self._backlog: deque[MarketSnapshotV1] = deque()
        #: Gold only: ``(spot / paxg)`` at the last spot read, and when it was read.
        self._gold_ratio: float | None = None
        self._gold_anchor_monotonic: float | None = None
        self._last_live_monotonic: float | None = None
        self._last_live: MarketSnapshotV1 | None = None
        self._consecutive_failures = 0
        self._history_bars_loaded = 0

    # -- identity ----------------------------------------------------------

    @property
    def name(self) -> str:
        return "public"

    @property
    def is_simulated(self) -> bool:
        return False

    @property
    def backlog(self) -> int:
        """Replayed history ticks still to deliver. Zero once the feed is live."""
        return len(self._backlog)

    @property
    def history_bars_loaded(self) -> int:
        return self._history_bars_loaded

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    # -- lifecycle ---------------------------------------------------------

    async def open(self) -> None:
        if self._is_open:
            return
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={"User-Agent": "TradeFixRadio/0.1 (+https://github.com/Roy-Mutwiri)"},
            )
        self._is_open = True
        live = await self._fetch_live()
        bars = await self._fetch_history()
        if bars:
            self._backlog.extend(self._replay_ticks(bars, anchor=live))
            self._history_bars_loaded = len(bars)
        _log.info(
            "feed.public_opened",
            symbol=self._symbol,
            history_bars=len(bars),
            live=None if live is None else round(live.mid, 2),
            replay_ticks=len(self._backlog),
        )

    async def close(self) -> None:
        self._is_open = False
        self._backlog.clear()
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- polling -----------------------------------------------------------

    async def poll(self) -> MarketSnapshotV1 | None:
        if not self._is_open or self._client is None:
            return None
        if self._backlog:
            return self._backlog.popleft()
        now = self._clock.monotonic()
        if (
            self._last_live_monotonic is not None
            and now - self._last_live_monotonic < self._live_interval
        ):
            return None
        self._last_live_monotonic = now
        snapshot = await self._fetch_live()
        if snapshot is None:
            self._consecutive_failures += 1
            _log.warning(
                "feed.public_poll_failed",
                symbol=self._symbol,
                consecutive_failures=self._consecutive_failures,
            )
            if self._consecutive_failures >= FAILURE_THRESHOLD:
                raise MarketDataError(
                    f"public feed for {self._symbol} failed "
                    f"{self._consecutive_failures} times consecutively"
                )
            return None
        self._consecutive_failures = 0
        self._last_live = snapshot
        return snapshot

    # -- sources -----------------------------------------------------------

    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        assert self._client is not None
        response = await self._client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def _binance_bid_ask(self, binance_symbol: str) -> tuple[float, float] | None:
        payload = await self._get_json(BINANCE_TICKER_URL, {"symbol": binance_symbol})
        bid = _as_float(payload.get("bidPrice"))
        ask = _as_float(payload.get("askPrice"))
        if bid is None or ask is None:
            return None
        return bid, max(ask, bid)

    async def _fetch_live(self) -> MarketSnapshotV1 | None:
        try:
            if self._symbol.upper() == "XAUUSD":
                return await self._fetch_gold()
            quote = await self._binance_bid_ask(_BINANCE_SYMBOLS[self._symbol.upper()])
            if quote is None:
                return None
            bid, ask = quote
            return MarketSnapshotV1(
                symbol=self._symbol, timestamp=self._clock.now(), bid=bid, ask=ask
            )
        except (httpx.HTTPError, ValueError, AttributeError, TypeError) as error:
            _log.debug("feed.public_live_error", symbol=self._symbol, error=str(error))
            return None

    async def _fetch_gold(self) -> MarketSnapshotV1 | None:
        """PAXG's tick at spot gold's level (module doc). Spot alone if PAXG is unavailable."""
        now = self._clock.monotonic()
        paxg: tuple[float, float] | None = None
        try:
            paxg = await self._binance_bid_ask(_GOLD_TICK_SYMBOL)
        except (httpx.HTTPError, ValueError, AttributeError, TypeError) as error:
            _log.debug("feed.public_paxg_error", error=str(error))
        anchor_due = (
            self._gold_anchor_monotonic is None
            or now - self._gold_anchor_monotonic >= GOLD_ANCHOR_SECONDS
        )
        spot: float | None = None
        if anchor_due or paxg is None or self._gold_ratio is None:
            payload = await self._get_json(GOLD_LIVE_URL)
            spot = _as_float(payload.get("price"))
            if spot is not None and paxg is not None:
                ratio = spot / ((paxg[0] + paxg[1]) / 2)
                if abs(ratio - 1.0) <= _MAX_SPLICE_RATIO:
                    self._gold_ratio = ratio
                else:
                    _log.warning(
                        "feed.public_gold_anchor_rejected",
                        spot=round(spot, 2),
                        paxg=round((paxg[0] + paxg[1]) / 2, 2),
                        detail="too far apart for a basis; quoting spot alone",
                    )
                    self._gold_ratio = None
                self._gold_anchor_monotonic = now
        stamp = self._clock.now()
        if paxg is not None and self._gold_ratio is not None:
            bid, ask = paxg
            return MarketSnapshotV1(
                symbol=self._symbol,
                timestamp=stamp,
                bid=bid * self._gold_ratio,
                ask=ask * self._gold_ratio,
            )
        if spot is not None:
            # The source stamps its last update; the quote is current *now* as far as the
            # station is concerned, and staleness is measured monotonically anyway.
            return MarketSnapshotV1(symbol=self._symbol, timestamp=stamp, bid=spot, ask=spot)
        return None

    async def _fetch_history(self) -> list[tuple[datetime, float, float, float, float]]:
        """Recent one-minute bars as ``(opened_at, open, high, low, close)``, oldest first."""
        bars: list[tuple[datetime, float, float, float, float]]
        try:
            if self._symbol.upper() == "XAUUSD":
                payload = await self._get_json(
                    GOLD_HISTORY_URL, {"interval": "1m", "range": "1d"}
                )
                result = payload["chart"]["result"][0]
                stamps = result["timestamp"]
                quote = result["indicators"]["quote"][0]
                bars = []
                for i, stamp in enumerate(stamps):
                    o, h, lo, c = (_as_float(quote[k][i]) for k in ("open", "high", "low", "close"))
                    if o is None or h is None or lo is None or c is None:
                        continue  # Yahoo leaves holes for minutes with no trade
                    opened = datetime.fromtimestamp(int(stamp), tz=UTC)
                    bars.append((opened, o, h, lo, c))
            else:
                payload = await self._get_json(
                    BINANCE_KLINES_URL,
                    {
                        "symbol": _BINANCE_SYMBOLS[self._symbol.upper()],
                        "interval": "1m",
                        "limit": min(1000, self._history_bars),
                    },
                )
                bars = []
                for row in payload:
                    o, h, lo, c = (_as_float(v) for v in row[1:5])
                    if o is None or h is None or lo is None or c is None:
                        continue
                    opened = datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)
                    bars.append((opened, o, h, lo, c))
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as error:
            _log.warning(
                "feed.public_history_unavailable",
                symbol=self._symbol,
                error=str(error),
                detail="starting cold; the regime will take a while to classify",
            )
            return []
        bars.sort(key=lambda b: b[0])
        return bars[-self._history_bars :]

    def _replay_ticks(
        self,
        bars: list[tuple[datetime, float, float, float, float]],
        *,
        anchor: MarketSnapshotV1 | None,
    ) -> list[MarketSnapshotV1]:
        """Turn bars into open/high/low/close ticks, scaled onto the live level."""
        scale = 1.0
        if anchor is not None and bars:
            last_close = bars[-1][4]
            ratio = anchor.mid / last_close
            if abs(ratio - 1.0) <= _MAX_SPLICE_RATIO:
                scale = ratio
            else:
                _log.warning(
                    "feed.public_history_not_spliced",
                    symbol=self._symbol,
                    history_close=round(last_close, 2),
                    live=round(anchor.mid, 2),
                    detail="too far apart to be the same instrument; replaying unscaled",
                )
        ticks: list[MarketSnapshotV1] = []
        # The live quote's own bar must not be contaminated by replayed history: stop
        # replaying at the start of the current minute.
        cutoff = self._clock.now().replace(second=0, microsecond=0)
        for opened, open_, high, low, close in bars:
            if opened >= cutoff:
                continue
            for offset, price in zip(_TICK_OFFSETS, (open_, high, low, close), strict=True):
                scaled = price * scale
                ticks.append(
                    MarketSnapshotV1(
                        symbol=self._symbol,
                        timestamp=opened + timedelta(seconds=offset),
                        bid=scaled,
                        ask=scaled,
                        synthetic=True,
                    )
                )
        return ticks


__all__ = [
    "BINANCE_KLINES_URL",
    "BINANCE_TICKER_URL",
    "FAILURE_THRESHOLD",
    "GOLD_ANCHOR_SECONDS",
    "GOLD_HISTORY_URL",
    "GOLD_LIVE_URL",
    "HISTORY_BARS",
    "PublicFeed",
]

