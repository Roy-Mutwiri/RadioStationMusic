"""The keyless public feed: history replay, live polling, throttling and failure."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from tradefix_radio.core.clock import UTC, VirtualClock
from tradefix_radio.core.errors import MarketDataError
from tradefix_radio.market.feeds.public import FAILURE_THRESHOLD, PublicFeed

NOW = datetime(2026, 10, 9, 14, 30, 20, tzinfo=UTC)
#: Providers align bars to the minute; the clock is deliberately mid-minute.
MINUTE = NOW.replace(second=0)


def _yahoo_payload(bars: list[tuple[datetime, float, float, float, float]]) -> dict[str, Any]:
    return {
        "chart": {
            "result": [
                {
                    "timestamp": [int(b[0].timestamp()) for b in bars],
                    "indicators": {
                        "quote": [
                            {
                                "open": [b[1] for b in bars],
                                "high": [b[2] for b in bars],
                                "low": [b[3] for b in bars],
                                "close": [b[4] for b in bars],
                            }
                        ]
                    },
                }
            ]
        }
    }


def _binance_klines(bars: list[tuple[datetime, float, float, float, float]]) -> list[list[Any]]:
    return [
        [int(b[0].timestamp() * 1000), str(b[1]), str(b[2]), str(b[3]), str(b[4]), "1.0"]
        for b in bars
    ]


def _bars(count: int, *, start: datetime, base: float) -> list[tuple[datetime, float, float, float, float]]:
    return [
        (start + timedelta(minutes=i), base + i, base + i + 2, base + i - 1, base + i + 1)
        for i in range(count)
    ]


class Routes:
    """A fake of the three public hosts, with knobs for what they return."""

    def __init__(self) -> None:
        self.gold_price: float | None = 4000.0
        self.gold_bars = _bars(3, start=MINUTE - timedelta(minutes=10), base=3990.0)
        self.btc_bid_ask: tuple[float, float] | None = (82000.0, 82000.5)
        self.btc_bars = _bars(3, start=MINUTE - timedelta(minutes=10), base=81990.0)
        #: Tokenised gold on Binance: a little above spot, as it trades.
        self.paxg_bid_ask: tuple[float, float] | None = (4008.0, 4010.0)
        self.history_status = 200
        self.spot_calls = 0
        self.ticker_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "api.gold-api.com":
            self.spot_calls += 1
            if self.gold_price is None:
                return httpx.Response(503)
            return httpx.Response(200, json={"price": self.gold_price, "updatedAt": "x"})
        if host == "query1.finance.yahoo.com":
            if self.history_status != 200:
                return httpx.Response(self.history_status)
            return httpx.Response(200, json=_yahoo_payload(self.gold_bars))
        if host == "api.binance.com" and path.endswith("bookTicker"):
            self.ticker_calls += 1
            quote = self.paxg_bid_ask if request.url.params["symbol"] == "PAXGUSDT" else self.btc_bid_ask
            if quote is None:
                return httpx.Response(503)
            bid, ask = quote
            return httpx.Response(200, json={"bidPrice": str(bid), "askPrice": str(ask)})
        if host == "api.binance.com" and path.endswith("klines"):
            return httpx.Response(200, json=_binance_klines(self.btc_bars))
        return httpx.Response(404)


def _feed(symbol: str, routes: Routes, clock: VirtualClock, **kw: Any) -> PublicFeed:
    return PublicFeed(
        symbol,
        clock=clock,
        client=httpx.AsyncClient(transport=httpx.MockTransport(routes)),
        **kw,
    )


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock(start=NOW)


async def test_a_history_is_replayed_as_four_ticks_per_bar_then_live(clock: VirtualClock) -> None:
    routes = Routes()
    feed = _feed("XAUUSD", routes, clock)
    await feed.open()
    assert feed.history_bars_loaded == 3
    assert feed.backlog == 12
    first = await feed.poll()
    assert first is not None and first.synthetic is True
    assert first.timestamp == routes.gold_bars[0][0]
    ticks = [first] + [await feed.poll() for _ in range(11)]
    assert feed.backlog == 0
    # Open, high, low, close in order, inside the bar's minute.
    seconds = [t.timestamp.second for t in ticks[:4]]
    assert seconds == [0, 15, 30, 45]
    live = await feed.poll()
    assert live is not None and live.synthetic is False
    assert live.mid == pytest.approx(4000.0)
    assert live.timestamp == NOW
    await feed.close()


async def test_b_gold_history_is_scaled_onto_the_live_level(clock: VirtualClock) -> None:
    """A future and spot gold differ by a basis; the join must not read as a price jump."""
    routes = Routes()
    routes.gold_price = 4020.0  # last history close is 3993.0: ~0.7% apart
    feed = _feed("XAUUSD", routes, clock)
    await feed.open()
    ticks = [await feed.poll() for _ in range(12)]
    last_close = ticks[-1]
    assert last_close is not None
    assert last_close.mid == pytest.approx(4020.0)
    assert ticks[0] is not None
    assert ticks[0].mid == pytest.approx(3990.0 * 4020.0 / 3993.0)
    await feed.close()


async def test_c_history_too_far_from_live_is_left_unscaled(clock: VirtualClock) -> None:
    routes = Routes()
    routes.gold_price = 8000.0  # not the same instrument
    feed = _feed("XAUUSD", routes, clock)
    await feed.open()
    first = await feed.poll()
    assert first is not None
    assert first.mid == pytest.approx(3990.0)
    await feed.close()


async def test_d_live_polls_are_throttled_to_the_source_cadence(clock: VirtualClock) -> None:
    routes = Routes()
    routes.gold_bars = []
    feed = _feed("XAUUSD", routes, clock, live_interval_seconds=5.0)
    await feed.open()
    assert (routes.ticker_calls, routes.spot_calls) == (1, 1), "the anchor fetch at open"
    assert await feed.poll() is not None
    assert await feed.poll() is None, "within the interval: no request"
    assert (routes.ticker_calls, routes.spot_calls) == (2, 1), "ticks only; spot is anchored"
    await clock.run_to(clock.monotonic() + 5.0)
    assert await feed.poll() is not None
    assert (routes.ticker_calls, routes.spot_calls) == (3, 1)
    await feed.close()


async def test_d2_gold_moves_with_paxg_ticks_at_spot_level(clock: VirtualClock) -> None:
    """Spot updates once a minute; PAXG ticks every second. Level from spot, motion from PAXG."""
    routes = Routes()
    routes.gold_bars = []
    feed = _feed("XAUUSD", routes, clock, live_interval_seconds=0.0)
    await feed.open()
    first = await feed.poll()
    assert first is not None
    assert first.mid == pytest.approx(4000.0), "anchored: PAXG 4009 reads as spot 4000"
    routes.paxg_bid_ask = (4012.0, 4014.0)  # gold ticks up 0.125 % between spot updates
    moved = await feed.poll()
    assert moved is not None
    assert moved.mid == pytest.approx(4000.0 * 4013.0 / 4009.0)
    assert routes.spot_calls == 1, "spot was not re-read for the tick"
    await clock.run_to(clock.monotonic() + 61.0)
    routes.gold_price = 4100.0
    anchored = await feed.poll()
    assert anchored is not None
    assert routes.spot_calls == 2, "a minute later the anchor is refreshed"
    assert anchored.mid == pytest.approx(4100.0)
    await feed.close()


async def test_d3_gold_falls_back_to_spot_when_paxg_is_not_a_basis(clock: VirtualClock) -> None:
    routes = Routes()
    routes.gold_bars = []
    routes.paxg_bid_ask = (9000.0, 9001.0)  # nowhere near gold: not usable as a basis
    feed = _feed("XAUUSD", routes, clock, live_interval_seconds=0.0)
    await feed.open()
    quote = await feed.poll()
    assert quote is not None
    assert quote.mid == pytest.approx(4000.0)
    assert routes.spot_calls == 2, "without an anchor, spot is read on every poll"
    routes.paxg_bid_ask = None  # and with no PAXG at all, spot still serves
    quote = await feed.poll()
    assert quote is not None and quote.mid == pytest.approx(4000.0)
    await feed.close()


async def test_e_bitcoin_uses_binance_bid_and_ask(clock: VirtualClock) -> None:
    routes = Routes()
    routes.btc_bars = []
    feed = _feed("BTCUSD", routes, clock)
    await feed.open()
    live = await feed.poll()
    assert live is not None
    assert (live.bid, live.ask) == (82000.0, 82000.5)
    assert live.synthetic is False
    await feed.close()


async def test_f_missing_history_means_a_cold_start_not_a_failure(clock: VirtualClock) -> None:
    routes = Routes()
    routes.history_status = 500
    feed = _feed("XAUUSD", routes, clock)
    await feed.open()
    assert feed.backlog == 0
    assert feed.history_bars_loaded == 0
    assert await feed.poll() is not None
    await feed.close()


async def test_g_repeated_live_failures_escalate_after_the_threshold(clock: VirtualClock) -> None:
    routes = Routes()
    routes.gold_bars = []
    routes.gold_price = None
    feed = _feed("XAUUSD", routes, clock, live_interval_seconds=0.0)
    await feed.open()
    for _ in range(FAILURE_THRESHOLD - 1):
        assert await feed.poll() is None
    with pytest.raises(MarketDataError):
        await feed.poll()
    await feed.close()


async def test_h_bars_inside_the_current_minute_are_not_replayed(clock: VirtualClock) -> None:
    """The live quote's own bar must not be polluted by history for the same minute."""
    routes = Routes()
    routes.gold_bars = _bars(2, start=MINUTE - timedelta(minutes=1), base=3990.0)
    feed = _feed("XAUUSD", routes, clock)
    await feed.open()
    assert feed.backlog == 4, "only the previous minute's bar; the current minute is live"
    await feed.close()


def test_i_unknown_symbols_are_refused_up_front() -> None:
    with pytest.raises(MarketDataError):
        PublicFeed("EURUSD")
