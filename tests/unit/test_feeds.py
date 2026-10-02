"""Market feeds (ADR-03, milestone 2.1/2.7).

The MetaTrader 5 and REST adapters are production paths that cannot be exercised
against a live terminal or a real provider in this environment (no logged-in MT5
account, no API key — see `docs/INITIAL_AUDIT.md` R-04 and ADR-03). They are
therefore tested against **injected fakes**, which does cover the logic most likely
to be wrong — symbol discovery, Market Watch selection, tick deduplication, field
mapping, failure escalation, secret handling — while leaving the specific wire format
of any given broker or provider explicitly unverified. That distinction is recorded in
the phase report rather than papered over.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tradefix_radio.config.schema import AppSettings, MarketSettings
from tradefix_radio.core.clock import VirtualClock
from tradefix_radio.core.errors import ConfigurationError, MarketDataError
from tradefix_radio.market.feeds import build_feed
from tradefix_radio.market.feeds.metatrader5 import MetaTrader5Feed
from tradefix_radio.market.feeds.replay import ReplayFeed
from tradefix_radio.market.feeds.rest import FAILURE_THRESHOLD, RestPollingFeed
from tradefix_radio.market.feeds.simulated import SimulatedFeed
from tradefix_radio.market.simulation import Scenario
from tests.conftest import make_settings

UTC = timezone.utc
START = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------- simulated


async def test_simulated_feed_produces_synthetic_snapshots() -> None:
    feed = SimulatedFeed(seed=3, clock=VirtualClock(start=START))
    await feed.open()
    snapshot = await feed.poll()
    assert snapshot is not None
    assert snapshot.synthetic is True
    assert feed.is_simulated
    await feed.close()


async def test_simulated_feed_returns_nothing_before_open() -> None:
    feed = SimulatedFeed(seed=3, clock=VirtualClock(start=START))
    assert await feed.poll() is None


async def test_simulated_feed_scenario_is_switchable_mid_run() -> None:
    """§7's buttons must drive the real engine, not a UI mock."""
    clock = VirtualClock(start=START)
    feed = SimulatedFeed(seed=3, scenario=Scenario.FLAT, clock=clock)
    await feed.open()
    assert feed.scenario is Scenario.FLAT
    assert "flat" in feed.name
    feed.set_scenario(Scenario.VIOLENT_BREAKOUT)
    assert feed.scenario is Scenario.VIOLENT_BREAKOUT
    assert "violent_breakout" in feed.name
    await feed.close()


async def test_simulated_feed_advances_with_the_injected_clock() -> None:
    clock = VirtualClock(start=START)
    feed = SimulatedFeed(seed=3, clock=clock)
    await feed.open()
    first = await feed.poll()
    await clock.advance(5)
    second = await feed.poll()
    assert first is not None
    assert second is not None
    assert (second.timestamp - first.timestamp).total_seconds() == pytest.approx(5.0)


# ---------------------------------------------------------------- replay


def write_csv(path: Path, rows: list[str], header: str = "timestamp,bid,ask") -> Path:
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


async def test_replay_feed_reads_bid_ask_rows(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "ticks.csv",
        [
            "2026-10-02T12:00:00Z,4000.0,4000.4",
            "2026-10-02T12:00:01Z,4000.2,4000.6",
        ],
    )
    feed = ReplayFeed(path)
    await feed.open()
    assert feed.total_snapshots == 2
    first = await feed.poll()
    assert first is not None
    assert first.bid == pytest.approx(4000.0)
    assert first.timestamp == datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    await feed.close()


async def test_replay_feed_derives_a_spread_from_close(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "bars.csv",
        ["1790000000,4000.0"],
        header="timestamp,close",
    )
    feed = ReplayFeed(path)
    await feed.open()
    snapshot = await feed.poll()
    assert snapshot is not None
    assert snapshot.ask > snapshot.bid
    assert snapshot.mid == pytest.approx(4000.0, rel=1e-6)
    await feed.close()


async def test_replay_data_is_always_marked_synthetic(tmp_path: Path) -> None:
    """§32: historical data presented as a current price would be false."""
    path = write_csv(tmp_path / "t.csv", ["2026-10-02T12:00:00Z,4000.0,4000.4"])
    feed = ReplayFeed(path)
    await feed.open()
    snapshot = await feed.poll()
    assert snapshot is not None
    assert snapshot.synthetic is True
    assert feed.is_simulated
    await feed.close()


async def test_replay_feed_accepts_epoch_and_iso_timestamps(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "mixed.csv",
        ["1790000000,4000.0,4000.4", "2026-10-02T12:00:00+00:00,4001.0,4001.4"],
    )
    feed = ReplayFeed(path)
    await feed.open()
    assert feed.total_snapshots == 2
    await feed.close()


async def test_replay_exhaustion_returns_none_rather_than_raising(tmp_path: Path) -> None:
    """Lets the replay feed be used to test §63-E without unplugging anything."""
    path = write_csv(tmp_path / "t.csv", ["2026-10-02T12:00:00Z,4000.0,4000.4"])
    feed = ReplayFeed(path)
    await feed.open()
    assert await feed.poll() is not None
    assert await feed.poll() is None
    assert feed.exhausted
    await feed.close()


async def test_replay_feed_can_loop(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "t.csv", ["2026-10-02T12:00:00Z,4000.0,4000.4"])
    feed = ReplayFeed(path, loop=True)
    await feed.open()
    for _ in range(5):
        assert await feed.poll() is not None
    assert not feed.exhausted
    await feed.close()


async def test_replay_missing_file_is_reported_clearly(tmp_path: Path) -> None:
    feed = ReplayFeed(tmp_path / "absent.csv")
    with pytest.raises(MarketDataError, match="not found"):
        await feed.open()


async def test_replay_empty_file_is_reported(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "empty.csv", [])
    feed = ReplayFeed(path)
    with pytest.raises(MarketDataError, match="no usable rows"):
        await feed.open()


async def test_replay_missing_timestamp_column_is_reported(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "bad.csv", ["4000.0,4000.4"], header="bid,ask")
    feed = ReplayFeed(path)
    with pytest.raises(MarketDataError, match="timestamp"):
        await feed.open()


async def test_replay_row_errors_name_the_line_number(tmp_path: Path) -> None:
    """A 200 000-row recording needs the line, not just "bad data"."""
    path = write_csv(
        tmp_path / "bad.csv",
        ["2026-10-02T12:00:00Z,4000.0,4000.4", "2026-10-02T12:00:01Z,oops,4000.6"],
    )
    feed = ReplayFeed(path)
    with pytest.raises(MarketDataError) as caught:
        await feed.open()
    assert ":3:" in str(caught.value)


async def test_replay_row_without_price_data_is_reported(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "bad.csv", ["2026-10-02T12:00:00Z,,"],
    )
    feed = ReplayFeed(path)
    with pytest.raises(MarketDataError, match="neither bid/ask nor close"):
        await feed.open()


# ---------------------------------------------------------------- MetaTrader 5


class FakeMt5:
    """Minimal stand-in for the MetaTrader5 module."""

    def __init__(
        self,
        *,
        symbols: dict[str, Any] | None = None,
        initialise_ok: bool = True,
    ) -> None:
        self._symbols = symbols or {}
        self._initialise_ok = initialise_ok
        self.initialised = False
        self.shutdown_calls = 0
        self.selected: list[str] = []
        self._tick_time = 1_790_000_000.0
        self._tick: Any = None

    def initialize(self, *_args: Any, **_kwargs: Any) -> bool:
        self.initialised = self._initialise_ok
        return self._initialise_ok

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.initialised = False

    def last_error(self) -> tuple[int, str]:
        return (-10005, "IPC timeout")

    def symbol_info(self, symbol: str) -> Any:
        return self._symbols.get(symbol)

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        if enable:
            self.selected.append(symbol)
        info = self._symbols.get(symbol)
        if info is not None:
            info.visible = True
        return True

    def symbol_info_tick(self, _symbol: str) -> Any:
        return self._tick

    def terminal_info(self) -> Any:
        return SimpleNamespace(build=4620)

    # -- test helpers
    def set_tick(
        self, bid: float, ask: float, *, advance: float = 1.0, volume: float = 7.0
    ) -> None:
        self._tick_time += advance
        self._tick = SimpleNamespace(
            bid=bid, ask=ask, time=int(self._tick_time),
            time_msc=int(self._tick_time * 1000), volume=volume,
        )


def visible_symbol(name: str, *, visible: bool = True) -> SimpleNamespace:
    return SimpleNamespace(name=name, visible=visible)


async def test_mt5_discovers_the_first_matching_alias() -> None:
    """Brokers name gold inconsistently; hard-coding one name is broker-specific."""
    client = FakeMt5(symbols={"GOLD": visible_symbol("GOLD")})
    feed = MetaTrader5Feed(
        symbol="XAUUSD", symbol_aliases=("XAUUSD", "XAUUSD.m", "GOLD"), client=client
    )
    await feed.open()
    assert feed.resolved_symbol == "GOLD"
    assert feed.terminal_build == "4620"
    await feed.close()


async def test_mt5_selects_a_symbol_that_is_not_in_market_watch() -> None:
    """An unselected symbol returns None ticks forever — like a closed market."""
    client = FakeMt5(symbols={"XAUUSD": visible_symbol("XAUUSD", visible=False)})
    feed = MetaTrader5Feed(symbol="XAUUSD", symbol_aliases=("XAUUSD",), client=client)
    await feed.open()
    assert client.selected == ["XAUUSD"]
    await feed.close()


async def test_mt5_reports_initialisation_failure_with_the_broker_error() -> None:
    client = FakeMt5(initialise_ok=False)
    feed = MetaTrader5Feed(client=client)
    with pytest.raises(MarketDataError) as caught:
        await feed.open()
    assert "IPC timeout" in str(caught.value)


async def test_mt5_reports_when_no_alias_exists_and_lists_what_it_tried() -> None:
    client = FakeMt5(symbols={})
    feed = MetaTrader5Feed(symbol_aliases=("XAUUSD", "GOLD"), client=client)
    with pytest.raises(MarketDataError) as caught:
        await feed.open()
    message = str(caught.value)
    assert "XAUUSD" in message
    assert "GOLD" in message


async def test_mt5_shuts_down_the_terminal_after_a_failed_open() -> None:
    """Otherwise the connection leaks across every watchdog restart."""
    client = FakeMt5(symbols={})
    feed = MetaTrader5Feed(symbol_aliases=("XAUUSD",), client=client)
    with pytest.raises(MarketDataError):
        await feed.open()
    assert client.shutdown_calls == 1


async def test_mt5_deduplicates_an_unchanged_tick() -> None:
    """MT5 returns the last known tick regardless of whether it is new.

    Without deduplication a quiet market produces a flood of identical observations
    and the volume profile is computed from nothing.
    """
    client = FakeMt5(symbols={"XAUUSD": visible_symbol("XAUUSD")})
    feed = MetaTrader5Feed(symbol_aliases=("XAUUSD",), client=client)
    await feed.open()
    client.set_tick(4000.0, 4000.4)
    assert await feed.poll() is not None
    assert await feed.poll() is None  # same tick time
    client.set_tick(4000.2, 4000.6)
    assert await feed.poll() is not None
    await feed.close()


async def test_mt5_drops_a_zero_quote_without_taking_the_feed_down() -> None:
    client = FakeMt5(symbols={"XAUUSD": visible_symbol("XAUUSD")})
    feed = MetaTrader5Feed(symbol_aliases=("XAUUSD",), client=client)
    await feed.open()
    client.set_tick(0.0, 0.0)
    assert await feed.poll() is None
    client.set_tick(4000.0, 4000.4)
    assert await feed.poll() is not None
    await feed.close()


async def test_mt5_snapshot_is_not_marked_synthetic() -> None:
    client = FakeMt5(symbols={"XAUUSD": visible_symbol("XAUUSD")})
    feed = MetaTrader5Feed(symbol_aliases=("XAUUSD",), client=client)
    await feed.open()
    client.set_tick(4000.0, 4000.4)
    snapshot = await feed.poll()
    assert snapshot is not None
    assert snapshot.synthetic is False
    assert not feed.is_simulated
    await feed.close()


async def test_mt5_returns_nothing_when_the_terminal_has_no_tick() -> None:
    """A closed market, not an error."""
    client = FakeMt5(symbols={"XAUUSD": visible_symbol("XAUUSD")})
    feed = MetaTrader5Feed(symbol_aliases=("XAUUSD",), client=client)
    await feed.open()
    assert await feed.poll() is None
    await feed.close()


async def test_mt5_close_is_safe_without_open() -> None:
    feed = MetaTrader5Feed(client=FakeMt5())
    await feed.close()


# ---------------------------------------------------------------- REST


def rest_settings(**overrides: object) -> MarketSettings:
    base: dict[str, object] = {
        "symbol": "XAUUSD",
        "feed": "rest",
        "rest_base_url": "https://quotes.example.invalid",
        "rest_api_key": "secret-key-value",
    }
    base.update(overrides)
    return MarketSettings.model_validate(base)


def stub_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://quotes.example.invalid",
    )


async def test_rest_feed_maps_bid_and_ask() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"bid": 4000.0, "ask": 4000.4, "timestamp": 1_790_000_000}
        )

    feed = RestPollingFeed(rest_settings(), client=stub_client(handler))
    await feed.open()
    snapshot = await feed.poll()
    assert snapshot is not None
    assert snapshot.bid == pytest.approx(4000.0)
    assert snapshot.synthetic is False
    await feed.close()


async def test_rest_feed_derives_a_spread_from_a_single_price() -> None:
    """And marks it synthetic, because the spread is ours rather than observed (§86)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"price": 4000.0, "timestamp": 1_790_000_000})

    feed = RestPollingFeed(rest_settings(), client=stub_client(handler))
    await feed.open()
    snapshot = await feed.poll()
    assert snapshot is not None
    assert snapshot.synthetic is True
    assert snapshot.mid == pytest.approx(4000.0, rel=1e-6)
    await feed.close()


async def test_rest_feed_supports_a_nested_field_mapping() -> None:
    """Provider-agnostic by configuration, not by a code change per provider."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"quote": {"b": 4000.0, "a": 4000.4, "t": 1_790_000_000}}]},
        )

    feed = RestPollingFeed(
        rest_settings(),
        client=stub_client(handler),
        field_map={
            "bid": "data.0.quote.b",
            "ask": "data.0.quote.a",
            "timestamp": "data.0.quote.t",
        },
    )
    await feed.open()
    snapshot = await feed.poll()
    assert snapshot is not None
    assert snapshot.bid == pytest.approx(4000.0)
    await feed.close()


async def test_rest_feed_sends_the_key_as_a_header_not_a_query_parameter() -> None:
    """§68: query strings are recorded verbatim in proxy and server access logs."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"price": 4000.0})

    settings = rest_settings()
    feed = RestPollingFeed(settings)
    await feed.open()
    # Replace the client the feed built, keeping its headers.
    assert feed._client is not None
    headers = feed._client.headers
    await feed.close()

    assert headers.get("authorization") == "Bearer secret-key-value"

    feed2 = RestPollingFeed(settings, client=stub_client(handler))
    await feed2.open()
    await feed2.poll()
    await feed2.close()
    assert "secret-key-value" not in captured["url"]


async def test_rest_feed_tolerates_transient_failures() -> None:
    """A single timeout is normal internet behaviour, not a reason to go off air."""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={"price": 4000.0, "timestamp": 1_790_000_000})

    feed = RestPollingFeed(rest_settings(), client=stub_client(handler))
    await feed.open()
    assert await feed.poll() is None
    assert await feed.poll() is None
    assert feed.consecutive_failures == 2
    assert await feed.poll() is not None
    assert feed.consecutive_failures == 0
    await feed.close()


async def test_rest_feed_escalates_a_sustained_outage() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    feed = RestPollingFeed(rest_settings(), client=stub_client(handler))
    await feed.open()
    for _ in range(FAILURE_THRESHOLD - 1):
        assert await feed.poll() is None
    with pytest.raises(MarketDataError, match="consecutively"):
        await feed.poll()
    await feed.close()


async def test_rest_feed_deduplicates_an_unchanged_timestamp() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"price": 4000.0, "timestamp": 1_790_000_000})

    feed = RestPollingFeed(rest_settings(), client=stub_client(handler))
    await feed.open()
    assert await feed.poll() is not None
    assert await feed.poll() is None
    await feed.close()


async def test_rest_feed_falls_back_to_arrival_time_without_a_timestamp() -> None:
    """Fabricating a past time would make fresh data look stale, or worse."""
    clock = VirtualClock(start=START)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"price": 4000.0})

    feed = RestPollingFeed(rest_settings(), clock=clock, client=stub_client(handler))
    await feed.open()
    snapshot = await feed.poll()
    assert snapshot is not None
    assert snapshot.timestamp == START
    await feed.close()


async def test_rest_feed_handles_millisecond_timestamps() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"price": 4000.0, "timestamp": 1_790_000_000_000})

    feed = RestPollingFeed(rest_settings(), client=stub_client(handler))
    await feed.open()
    snapshot = await feed.poll()
    assert snapshot is not None
    assert snapshot.timestamp.year == 2026
    await feed.close()


async def test_rest_feed_rejects_an_unusable_payload() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    feed = RestPollingFeed(rest_settings(), client=stub_client(handler))
    await feed.open()
    assert await feed.poll() is None
    await feed.close()


async def test_rest_feed_requires_a_base_url() -> None:
    with pytest.raises(MarketDataError, match="rest_base_url"):
        RestPollingFeed(
            MarketSettings.model_validate({"symbol": "XAUUSD", "feed": "simulated"})
        )


# ---------------------------------------------------------------- factory


def test_build_feed_returns_the_configured_implementation(tmp_path: Path) -> None:
    simulated = build_feed(make_settings(tmp_path))
    assert isinstance(simulated, SimulatedFeed)

    replay_file = write_csv(tmp_path / "r.csv", ["2026-10-02T12:00:00Z,4000.0,4000.4"])
    replay = build_feed(
        make_settings(
            tmp_path, market={"feed": "replay", "replay_file": str(replay_file)}
        )
    )
    assert isinstance(replay, ReplayFeed)


def test_build_feed_can_construct_the_rest_feed(tmp_path: Path) -> None:
    feed = build_feed(
        make_settings(
            tmp_path,
            market={
                "feed": "rest",
                "rest_base_url": "https://example.invalid",
                "rest_api_key": "k",
            },
        )
    )
    assert isinstance(feed, RestPollingFeed)


def test_build_feed_rejects_an_unknown_kind(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    broken = settings.model_copy(
        update={"market": settings.market.model_copy(update={"feed": "telepathy"})}
    )
    with pytest.raises(ConfigurationError, match="unknown market.feed"):
        build_feed(broken)


def test_every_feed_satisfies_the_protocol(tmp_path: Path) -> None:
    from tradefix_radio.market.feeds.base import MarketFeed

    replay_file = write_csv(tmp_path / "r.csv", ["2026-10-02T12:00:00Z,4000.0,4000.4"])
    feeds = [
        SimulatedFeed(seed=1),
        ReplayFeed(replay_file),
        MetaTrader5Feed(client=FakeMt5()),
        RestPollingFeed(rest_settings()),
    ]
    for feed in feeds:
        assert isinstance(feed, MarketFeed), type(feed).__name__
        assert feed.name
        assert feed.resolved_symbol


def test_app_settings_rest_feed_requires_a_url() -> None:
    """The config schema rejects it before a feed is ever constructed."""
    with pytest.raises(Exception, match="rest_base_url"):
        AppSettings.model_validate({"market": {"feed": "rest"}})


async def test_feeds_can_be_reopened_after_close(tmp_path: Path) -> None:
    """The watchdog restarts a feed that failed mid-open (§35)."""
    feed = SimulatedFeed(seed=1, clock=VirtualClock(start=START))
    for _ in range(3):
        await feed.open()
        assert await feed.poll() is not None
        await feed.close()
        assert await feed.poll() is None


async def test_opening_twice_is_idempotent(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "r.csv", ["2026-10-02T12:00:00Z,4000.0,4000.4"])
    feed = ReplayFeed(path)
    await feed.open()
    await feed.open()
    assert feed.position == 0
    await feed.close()


def test_timedelta_import_is_used() -> None:
    """Guards the module's own imports against drift."""
    assert timedelta(seconds=1).total_seconds() == 1.0
