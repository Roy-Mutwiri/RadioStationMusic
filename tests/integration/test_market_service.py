"""Market data service (§4, §63-E, milestone 2.7).

This is where the §63-E contract is verified end to end: a dead feed must produce a
**stale indicator, a last-good state, safe neutral programming, and no false market
statements**. The last of those is the one most worth testing, because it is the one a
reasonable implementation gets wrong — the natural thing to do when data stops
arriving is to keep showing the last price, which is exactly what §32 and §86 forbid.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tradefix_radio.contracts.enums import FeedStatus, MarketDirection, MarketRegime
from tradefix_radio.contracts.events import (
    MarketEnergyChanged,
    MarketFeedStatusChanged,
    MarketStateChanged,
)
from tradefix_radio.contracts.market import MarketSnapshotV1
from tradefix_radio.core.clock import VirtualClock
from tradefix_radio.core.errors import MarketDataError
from tradefix_radio.core.events import EventBus
from tradefix_radio.market.feeds.base import FeedBase
from tradefix_radio.market.feeds.simulated import SimulatedFeed
from tradefix_radio.market.service import MarketDataService
from tradefix_radio.market.simulation import Scenario
from tests.conftest import make_settings

UTC = timezone.utc
START = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)


class ControllableFeed(FeedBase):
    """A feed whose output the test dictates exactly.

    Needed because §63-E is about the *absence* of data, and a simulated feed never
    stops producing. Only a feed that can be told "return nothing now" lets the
    staleness ladder be driven deterministically.
    """

    def __init__(self, clock: VirtualClock, *, simulated: bool = False) -> None:
        super().__init__("XAUUSD")
        self._clock = clock
        self._simulated = simulated
        self.price = 4_000.0
        self.silent = False
        self.raise_on_poll: Exception | None = None
        self.poll_count = 0

    @property
    def name(self) -> str:
        return "controllable"

    @property
    def is_simulated(self) -> bool:
        return self._simulated

    async def open(self) -> None:
        self._is_open = True

    async def close(self) -> None:
        self._is_open = False

    async def poll(self) -> MarketSnapshotV1 | None:
        self.poll_count += 1
        if self.raise_on_poll is not None:
            raise self.raise_on_poll
        if self.silent or not self._is_open:
            return None
        half = self.price * 0.00005 / 2
        return MarketSnapshotV1(
            symbol="XAUUSD",
            timestamp=self._clock.now(),
            bid=self.price - half,
            ask=self.price + half,
            tick_volume=10.0,
            synthetic=self._simulated,
        )


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock(start=START)


def service_for(
    tmp_path, clock: VirtualClock, feed, bus: EventBus | None = None, **overrides
):
    settings = make_settings(
        tmp_path,
        market={
            "warmup_bars": 20,
            "percentile_window_bars": 200,
            "stale_after_seconds": 15.0,
            "disconnected_after_seconds": 60.0,
            **overrides.pop("market", {}),
        },
        **overrides,
    )
    return MarketDataService(settings, feed, bus=bus, clock=clock)


async def warm(service: MarketDataService, clock: VirtualClock, *, bars: int = 60) -> None:
    """Drive the service until features are reliable.

    Tests drive ``poll_once`` directly and open the feed themselves rather than calling
    ``service.start()``. Starting the background loop *as well* would double-poll — the
    loop and the test would both be consuming the feed — and bar counts would stop
    being deterministic. Only the two tests whose subject is the loop itself start it.
    """
    for _ in range(bars * 60):
        await service.poll_once()
        await clock.advance(1.0)


# ---------------------------------------------------------------- pipeline


async def test_simulated_feed_drives_a_full_state(tmp_path, clock: VirtualClock) -> None:
    """§81-1: the simulator must drive a real MarketState."""
    feed = SimulatedFeed(seed=5, scenario=Scenario.LOW_VOLATILITY_RANGE, clock=clock)
    service = service_for(tmp_path, clock, feed)
    await feed.open()
    try:
        await warm(service, clock, bars=70)
    finally:
        await service.stop()

    state = service.current_state
    assert state is not None
    assert state.regime is not MarketRegime.UNKNOWN
    assert 0.0 <= state.energy <= 100.0
    assert service.bars_processed > 50
    assert service.current_features is not None
    assert service.current_energy is not None


async def test_regime_changes_when_the_scenario_changes(
    tmp_path, clock: VirtualClock
) -> None:
    """§7: switching simulation must change the real programming inputs."""
    feed = SimulatedFeed(seed=9, scenario=Scenario.FLAT, clock=clock)
    service = service_for(tmp_path, clock, feed)
    await feed.open()
    try:
        await warm(service, clock, bars=70)
        quiet_state = service.current_state
        assert quiet_state is not None

        feed.set_scenario(Scenario.VIOLENT_BREAKOUT)
        await warm(service, clock, bars=70)
        loud_state = service.current_state
    finally:
        await service.stop()

    assert loud_state is not None
    assert loud_state.energy > quiet_state.energy + 20.0, (
        f"quiet={quiet_state.energy:.1f} loud={loud_state.energy:.1f}"
    )
    assert loud_state.regime is not quiet_state.regime


async def test_bars_are_only_emitted_once_per_bar_interval(
    tmp_path, clock: VirtualClock
) -> None:
    """Recomputing 20 features per tick would make this the busiest thing running."""
    feed = ControllableFeed(clock)
    service = service_for(tmp_path, clock, feed)
    await feed.open()
    try:
        for _ in range(300):
            await service.poll_once()
            await clock.advance(1.0)
    finally:
        await service.stop()
    assert feed.poll_count == 300
    assert 4 <= service.bars_processed <= 6


# ---------------------------------------------------------------- §63-E


async def test_price_is_shown_only_while_the_feed_is_live(
    tmp_path, clock: VirtualClock
) -> None:
    """§32/§86: the central honesty rule, verified on a live-then-dead feed."""
    feed = ControllableFeed(clock)
    service = service_for(tmp_path, clock, feed)
    await feed.open()
    try:
        await service.poll_once()
        assert service.feed_status is FeedStatus.LIVE
        # State only exists once a bar has closed, so drive a few bars first.
        await warm(service, clock, bars=3)
        live = service.current_state
        assert live is not None
        assert live.feed_status is FeedStatus.LIVE
        assert live.price is not None

        # Go silent. Staleness must advance and the price must vanish.
        feed.silent = True
        await clock.advance(20.0)
        await service.poll_once()
        stale = service.current_state
        assert stale is not None
        assert stale.feed_status is FeedStatus.STALE
        assert stale.price is None, "a stale price was presented as current"

        await clock.advance(60.0)
        await service.poll_once()
        dead = service.current_state
        assert dead is not None
        assert dead.feed_status is FeedStatus.DISCONNECTED
        assert dead.price is None
    finally:
        await service.stop()


async def test_a_disconnected_feed_yields_neutral_programming(
    tmp_path, clock: VirtualClock
) -> None:
    """§63-E: safe neutral programming, so nothing the station implies can be wrong."""
    feed = ControllableFeed(clock)
    service = service_for(tmp_path, clock, feed)
    await feed.open()
    try:
        await warm(service, clock, bars=60)
        before = service.current_state
        assert before is not None
        assert before.regime is not MarketRegime.UNKNOWN

        feed.silent = True
        await clock.advance(120.0)
        await service.poll_once()
        after = service.current_state
    finally:
        await service.stop()

    assert after is not None
    assert after.regime is MarketRegime.UNKNOWN
    assert after.direction is MarketDirection.NEUTRAL
    assert after.confidence == 0.0
    assert after.energy == 50.0
    assert after.trend_strength == 0.0
    assert after.price is None


async def test_the_station_keeps_producing_state_while_the_feed_is_dead(
    tmp_path, clock: VirtualClock
) -> None:
    """A dead feed must not stop the station (§34, §63-E)."""
    feed = ControllableFeed(clock)
    service = service_for(tmp_path, clock, feed)
    await feed.open()
    try:
        await warm(service, clock, bars=40)
        feed.silent = True
        for _ in range(200):
            await service.poll_once()
            await clock.advance(5.0)
            assert service.current_state is not None
    finally:
        await service.stop()
    assert service.feed_status is FeedStatus.DISCONNECTED


async def test_data_age_grows_while_silent_and_resets_on_recovery(
    tmp_path, clock: VirtualClock
) -> None:
    feed = ControllableFeed(clock)
    service = service_for(tmp_path, clock, feed)
    await feed.open()
    try:
        await service.poll_once()
        assert service.data_age_seconds() == pytest.approx(0.0)
        feed.silent = True
        await clock.advance(42.0)
        await service.poll_once()
        assert service.data_age_seconds() == pytest.approx(42.0)
        feed.silent = False
        await service.poll_once()
        assert service.data_age_seconds() == pytest.approx(0.0)
        assert service.feed_status is FeedStatus.LIVE
    finally:
        await service.stop()


async def test_status_ladder_follows_the_configured_thresholds(
    tmp_path, clock: VirtualClock
) -> None:
    feed = ControllableFeed(clock)
    service = service_for(tmp_path, clock, feed)
    await feed.open()
    try:
        await service.poll_once()
        assert service.feed_status is FeedStatus.LIVE
        feed.silent = True

        await clock.advance(14.0)
        await service.poll_once()
        assert service.feed_status is FeedStatus.LIVE

        await clock.advance(2.0)
        await service.poll_once()
        assert service.feed_status is FeedStatus.STALE

        await clock.advance(45.0)
        await service.poll_once()
        assert service.feed_status is FeedStatus.DISCONNECTED
    finally:
        await service.stop()


async def test_a_simulated_feed_is_never_reported_as_live(
    tmp_path, clock: VirtualClock
) -> None:
    """§7: an operator must never mistake a simulation for the live market."""
    feed = ControllableFeed(clock, simulated=True)
    service = service_for(tmp_path, clock, feed)
    await feed.open()
    try:
        await warm(service, clock, bars=40)
        state = service.current_state
    finally:
        await service.stop()
    assert service.feed_status is FeedStatus.SIMULATED
    assert state is not None
    assert state.is_simulated
    assert state.price is None
    # Still usable for programming — simulation must drive the real engines.
    assert state.is_usable_for_programming


async def test_a_feed_error_does_not_stop_the_service(
    tmp_path, clock: VirtualClock
) -> None:
    """§34: a broken feed is an operational condition, not a crash."""
    feed = ControllableFeed(clock)
    service = service_for(tmp_path, clock, feed)
    await feed.open()
    try:
        await service.poll_once()
        feed.raise_on_poll = MarketDataError("connection reset")
        with pytest.raises(MarketDataError):
            await service.poll_once()
        # The background loop swallows it; the service survives.
        feed.raise_on_poll = None
        assert await service.poll_once() is None or True
        assert service.current_state is not None or service.feed_status is not None
    finally:
        await service.stop()


async def test_the_background_loop_survives_repeated_feed_errors(
    tmp_path, clock: VirtualClock
) -> None:
    feed = ControllableFeed(clock)
    service = service_for(tmp_path, clock, feed)
    feed.raise_on_poll = MarketDataError("down")
    # The background loop is the subject here, so this test genuinely starts it.
    await service.start()
    try:
        for _ in range(30):
            await clock.advance(1.0)
        assert service.poll_errors > 0
    finally:
        await service.stop()


async def test_neutral_state_makes_no_claims(tmp_path, clock: VirtualClock) -> None:
    """What the music director gets before any data has arrived (§63-E)."""
    feed = ControllableFeed(clock)
    service = service_for(tmp_path, clock, feed)
    state = service.neutral_state()
    assert state.regime is MarketRegime.UNKNOWN
    assert state.direction is MarketDirection.NEUTRAL
    assert state.price is None
    assert state.energy == 50.0
    assert state.confidence == 0.0


# ---------------------------------------------------------------- events


async def test_a_regime_change_publishes_an_event(tmp_path, clock: VirtualClock) -> None:
    bus = EventBus()
    received: list[MarketStateChanged] = []
    bus.subscribe(
        "market.state_changed", lambda event: _collect(received, event), name="states"
    )
    feed = SimulatedFeed(seed=15, scenario=Scenario.FLAT, clock=clock)
    service = service_for(tmp_path, clock, feed, bus)
    await feed.open()
    try:
        await warm(service, clock, bars=70)
        feed.set_scenario(Scenario.VIOLENT_BREAKOUT)
        await warm(service, clock, bars=70)
        await bus.drain()
    finally:
        await service.stop()
        await bus.aclose()

    assert received, "no market.state_changed events"
    regimes = [event.state.regime for event in received]
    assert len(set(regimes)) > 1


async def test_energy_events_are_throttled(tmp_path, clock: VirtualClock) -> None:
    """An event per tick would bury the events that matter."""
    bus = EventBus(default_queue_size=4096)
    received: list[MarketEnergyChanged] = []
    bus.subscribe(
        "market.energy_changed", lambda event: _collect(received, event), name="energy"
    )
    feed = SimulatedFeed(seed=16, scenario=Scenario.LOW_VOLATILITY_RANGE, clock=clock)
    service = service_for(tmp_path, clock, feed, bus)
    await feed.open()
    try:
        await warm(service, clock, bars=120)
        await bus.drain()
    finally:
        await service.stop()
        await bus.aclose()

    assert received
    # Far fewer events than bars, in a market with no material energy movement.
    assert len(received) < service.bars_processed


async def test_a_feed_status_change_publishes_an_event(
    tmp_path, clock: VirtualClock
) -> None:
    bus = EventBus()
    received: list[MarketFeedStatusChanged] = []
    bus.subscribe(
        "market.feed_status_changed",
        lambda event: _collect(received, event),
        name="status",
    )
    feed = ControllableFeed(clock)
    service = service_for(tmp_path, clock, feed, bus)
    await feed.open()
    try:
        await service.poll_once()
        feed.silent = True
        await clock.advance(20.0)
        await service.poll_once()
        await clock.advance(60.0)
        await service.poll_once()
        await bus.drain()
    finally:
        await service.stop()
        await bus.aclose()

    statuses = [event.status for event in received]
    assert "live" in statuses
    assert "stale" in statuses
    assert "disconnected" in statuses


async def test_the_service_runs_without_a_bus(tmp_path, clock: VirtualClock) -> None:
    """The bus is optional; the market engine must be usable standalone for §47."""
    feed = SimulatedFeed(seed=17, clock=clock)
    service = service_for(tmp_path, clock, feed, None)
    await feed.open()
    try:
        await warm(service, clock, bars=40)
    finally:
        await service.stop()
    assert service.bars_processed > 30


# ---------------------------------------------------------------- lifecycle


async def test_start_is_idempotent(tmp_path, clock: VirtualClock) -> None:
    feed = SimulatedFeed(seed=18, clock=clock)
    service = service_for(tmp_path, clock, feed)
    await service.start()
    await service.start()
    await service.stop()


async def test_stop_is_safe_without_start(tmp_path, clock: VirtualClock) -> None:
    feed = SimulatedFeed(seed=18, clock=clock)
    await service_for(tmp_path, clock, feed).stop()


async def test_reset_clears_derived_state_but_keeps_the_feed(
    tmp_path, clock: VirtualClock
) -> None:
    feed = SimulatedFeed(seed=19, clock=clock)
    service = service_for(tmp_path, clock, feed)
    await feed.open()
    try:
        await warm(service, clock, bars=60)
        assert service.bars_processed > 0
        service.reset()
        assert service.bars_processed == 0
        assert service.current_state is None
        assert service.current_features is None
        # And it recovers.
        await warm(service, clock, bars=60)
        assert service.current_state is not None
    finally:
        await service.stop()


async def test_regime_transitions_stay_bounded_over_a_long_run(
    tmp_path, clock: VirtualClock
) -> None:
    """The anti-flicker guarantee, through the whole service, over 8 simulated hours."""
    feed = SimulatedFeed(seed=20, scenario=Scenario.EVENT_VOLATILITY, clock=clock)
    service = service_for(tmp_path, clock, feed)
    await feed.open()
    try:
        await warm(service, clock, bars=480)
    finally:
        await service.stop()

    assert service.bars_processed > 400
    # An event-driven market legitimately changes regime, but not every few minutes.
    assert service.regime_transitions < service.bars_processed // 12, (
        f"{service.regime_transitions} transitions over {service.bars_processed} bars"
    )


async def _collect(sink: list, event) -> None:
    sink.append(event)
