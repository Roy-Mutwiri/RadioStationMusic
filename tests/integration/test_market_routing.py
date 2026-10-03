"""Market routing through the real services (§Market routing acceptance).

`test_market_router.py` tests the decision in isolation with hand-written assessments.
This file wires two real `MarketDataService` instances over two real simulated feeds and
asserts the things that only show up once the parts are connected:

* the inactive symbol keeps being polled, which is what makes a reopen detectable at all;
* each symbol keeps its own regime state, so a switch cannot contaminate the other;
* the active state the director receives carries the active symbol;
* events reach the coordinator.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

import pytest

from tests.conftest import make_settings
from tradefix_radio.config.schema import AppSettings
from tradefix_radio.contracts.events import (
    ActiveSymbolChanged,
    MarketAvailabilityChanged,
)
from tradefix_radio.core.clock import UTC, VirtualClock
from tradefix_radio.market.active import ActiveMarketService
from tradefix_radio.market.availability import MarketAvailability
from tradefix_radio.market.feeds.simulated import SimulatedFeed
from tradefix_radio.market.router import NO_ACTIVE_MARKET
from tradefix_radio.market.service import MarketDataService
from tradefix_radio.market.simulation import Scenario
from tradefix_radio.api.snapshot import RuntimeView, routing_to_dto
from tradefix_radio.runtime.coordinator import RuntimeCoordinator

#: Wednesday 15:00 UTC — gold trading.
TRADING = datetime(2026, 10, 7, 15, 0, tzinfo=UTC)

PRIMARY = "XAUUSD"
FALLBACK = "BTCUSD"


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock(start=TRADING, real_yield_seconds=0.0)


@pytest.fixture
def settings(tmp_path, clean_environ) -> AppSettings:
    # Short windows so the test does not have to advance virtual time by ten minutes to
    # observe a switch. The *ordering* that matters — reopen held longer than close — is
    # preserved, because that asymmetry is the behaviour under test.
    return make_settings(
        tmp_path,
        clean_environ,
        markets={
            "primary": PRIMARY,
            "fallback": [FALLBACK],
            "switch_confirmation_seconds": 10.0,
            "reopen_confirmation_seconds": 30.0,
            "minimum_active_market_seconds": 5.0,
        },
    )


@pytest.fixture
async def market(
    settings: AppSettings, clock: VirtualClock
) -> AsyncIterator[tuple[ActiveMarketService, RuntimeCoordinator]]:
    coordinator = RuntimeCoordinator(clock=clock)
    await coordinator.start()
    services = {
        PRIMARY: MarketDataService(
            settings,
            SimulatedFeed(
                symbol=PRIMARY, scenario=Scenario.RANDOM_WALK, seed=1, clock=clock
            ),
            clock=clock,
        ),
        FALLBACK: MarketDataService(
            settings,
            SimulatedFeed(
                symbol=FALLBACK,
                scenario=Scenario.VOLATILITY_SPIKE,
                seed=2,
                clock=clock,
            ),
            clock=clock,
        ),
    }
    active = ActiveMarketService(
        settings, services, coordinator=coordinator, clock=clock
    )
    await active.start()
    try:
        yield active, coordinator
    finally:
        await active.stop()
        # Drain and stop the bus. Without this a subscriber coroutine outlives the test and
        # pytest attributes the resulting unraisable warning to whichever *unrelated* test
        # runs next — the same hazard pyproject documents for ResourceWarning.
        await coordinator.drain()
        await coordinator.stop()


async def warm(active: ActiveMarketService, clock: VirtualClock, bars: int = 40) -> None:
    """Push enough bars through both feeds that regimes can classify."""
    for _ in range(bars):
        await active.poll_once()
        await clock.advance(60.0)


# -------------------------------------------------------------------- basics


async def test_the_primary_is_selected_when_it_is_trading(
    market: tuple[ActiveMarketService, RuntimeCoordinator], clock: VirtualClock
) -> None:
    active, _ = market
    await warm(active, clock, bars=10)
    assert active.active_symbol == PRIMARY
    assert active.current_state is not None
    assert active.current_state.symbol == PRIMARY


async def test_both_feeds_are_polled_even_though_one_is_inactive(
    market: tuple[ActiveMarketService, RuntimeCoordinator], clock: VirtualClock
) -> None:
    """The reopen path depends on this.

    A market nobody polls cannot be assessed, so if the inactive symbol were parked the
    station could never notice gold coming back while Bitcoin was on air.
    """
    active, _ = market
    await warm(active, clock, bars=10)
    for symbol, service in active.services.items():
        assert service.bars_processed > 0, f"{symbol} was not polled"


async def test_each_symbol_keeps_its_own_market_state(
    market: tuple[ActiveMarketService, RuntimeCoordinator], clock: VirtualClock
) -> None:
    """No regime-state contamination between symbols.

    The two feeds run different scenarios, so their states must differ. If one engine were
    shared, the second symbol's reading would be built from the first's rolling history and
    the two would converge.
    """
    active, _ = market
    await warm(active, clock, bars=40)

    gold = active.state_for(PRIMARY)
    bitcoin = active.state_for(FALLBACK)
    assert gold is not None and bitcoin is not None
    assert gold.symbol == PRIMARY
    assert bitcoin.symbol == FALLBACK
    # Different scenarios, so at least one of the headline readings must differ.
    assert (gold.regime, round(gold.energy, 1), round(gold.volatility, 1)) != (
        bitcoin.regime,
        round(bitcoin.energy, 1),
        round(bitcoin.volatility, 1),
    )


# ------------------------------------------------------------------ switching


async def test_a_forced_closure_moves_the_station_to_bitcoin(
    market: tuple[ActiveMarketService, RuntimeCoordinator], clock: VirtualClock
) -> None:
    active, coordinator = market
    received: list[ActiveSymbolChanged] = []
    coordinator.subscribe(
        ActiveSymbolChanged.TOPIC, received.append, name="test-switch-watcher"
    )

    await warm(active, clock, bars=20)
    assert active.active_symbol == PRIMARY

    active.force_closed(PRIMARY)
    # Confirmation window, then the switch.
    for _ in range(4):
        await active.poll_once()
        await clock.advance(10.0)
    await coordinator.drain()

    assert active.active_symbol == FALLBACK
    assert active.current_state is not None
    assert active.current_state.symbol == FALLBACK

    assert received, "no market.active_symbol_changed event was published"
    event = received[-1]
    assert event.previous_symbol == PRIMARY
    assert event.new_symbol == FALLBACK
    assert event.reason == "primary_market_closed"


async def test_the_station_returns_to_gold_when_it_reopens(
    market: tuple[ActiveMarketService, RuntimeCoordinator], clock: VirtualClock
) -> None:
    active, _ = market
    await warm(active, clock, bars=20)

    active.force_closed(PRIMARY)
    for _ in range(4):
        await active.poll_once()
        await clock.advance(10.0)
    assert active.active_symbol == FALLBACK

    active.force_closed(PRIMARY, closed=False)
    # The reopen window is the longer one, so a short wait must not be enough.
    await active.poll_once()
    await clock.advance(15.0)
    await active.poll_once()
    assert active.active_symbol == FALLBACK, "returned before the reopen window elapsed"

    for _ in range(6):
        await clock.advance(10.0)
        await active.poll_once()
    assert active.active_symbol == PRIMARY


async def test_a_dead_primary_feed_does_not_move_the_station(
    market: tuple[ActiveMarketService, RuntimeCoordinator], clock: VirtualClock
) -> None:
    """The distinction, end to end.

    Gold is trading and its feed has stopped. Nothing is forced closed — the data simply
    stops arriving, which is what a broker disconnect looks like from here. The station
    must stay on gold and report the feed as degraded.
    """
    active, _ = market
    await warm(active, clock, bars=20)
    assert active.active_symbol == PRIMARY

    # Stop the gold service outright: that is what a broker disconnect looks like from
    # here — the feed stops producing while the calendar still says the market is trading.
    # Stopping it also halts its own polling task, which would otherwise keep the data
    # fresh and make the test assert nothing.
    await active.services[PRIMARY].stop()
    bitcoin = active.services[FALLBACK]
    for _ in range(12):
        await bitcoin.poll_once()
        await clock.advance(120.0)
        await active.evaluate()

    gold = active.assessments()[PRIMARY]
    assert gold.state in (MarketAvailability.STALE, MarketAvailability.UNAVAILABLE)
    assert gold.state is not MarketAvailability.CLOSED
    assert gold.feed_degraded
    assert active.active_symbol == PRIMARY, (
        "a dead feed was treated as a closed market and moved the station"
    )


async def test_no_active_market_when_everything_is_closed(
    market: tuple[ActiveMarketService, RuntimeCoordinator], clock: VirtualClock
) -> None:
    """The radio keeps playing; only live-data planning stops."""
    active, _ = market
    await warm(active, clock, bars=20)

    active.force_closed(PRIMARY)
    active.force_closed(FALLBACK)
    for _ in range(6):
        await active.poll_once()
        await clock.advance(10.0)

    assert active.active_symbol == NO_ACTIVE_MARKET
    assert active.current_state is None, (
        "with no market, the director must get nothing rather than a fabricated state"
    )


async def test_availability_changes_are_published_for_the_inactive_symbol_too(
    market: tuple[ActiveMarketService, RuntimeCoordinator], clock: VirtualClock
) -> None:
    """An operator needs to see the other market's health before it matters."""
    active, coordinator = market
    received: list[MarketAvailabilityChanged] = []
    coordinator.subscribe(
        MarketAvailabilityChanged.TOPIC, received.append, name="test-availability-watcher"
    )

    await warm(active, clock, bars=20)
    active.force_closed(FALLBACK)
    await active.poll_once()
    await coordinator.drain()

    assert any(event.symbol == FALLBACK for event in received)
    # And the station did not move: the fallback closing is irrelevant while gold is open.
    assert active.active_symbol == PRIMARY


# ------------------------------------------------------------------ reporting


async def test_the_api_rendering_describes_both_markets(
    market: tuple[ActiveMarketService, RuntimeCoordinator], clock: VirtualClock
) -> None:
    """The one rendering of routing state, exercised end to end.

    Asserted through `routing_to_dto` rather than against the service's attributes, because
    the DTO is what the header and the Market page actually receive — and it is the layer
    where an infinite data age or a missing price would become an invalid frame.
    """
    active, _ = market
    await warm(active, clock, bars=20)

    dto = routing_to_dto(RuntimeView(settings=None, routing=active))
    assert dto is not None
    assert dto.active_symbol == PRIMARY
    assert dto.primary_symbol == PRIMARY
    assert dto.is_primary is True
    assert dto.has_active_market is True

    symbols = {entry.symbol: entry for entry in dto.symbols}
    assert set(symbols) == {PRIMARY, FALLBACK}
    assert symbols[PRIMARY].is_active is True
    assert symbols[FALLBACK].is_active is False
    for entry in symbols.values():
        assert entry.state
        assert entry.reason
        assert entry.bars_processed > 0
        assert entry.feed_degraded is False

    # The frame must survive serialisation: an infinite data age would emit `Infinity`,
    # which is not JSON, and the browser would drop the whole live frame rather than one
    # field.
    assert "Infinity" not in dto.model_dump_json()


async def test_a_closed_market_is_not_reported_as_a_degraded_feed(
    market: tuple[ActiveMarketService, RuntimeCoordinator], clock: VirtualClock
) -> None:
    """The distinction, as the operator sees it.

    `feed_degraded` is what tells someone looking at the Market page whether to go and fix
    something. A closed market needs no action; a silent feed does.
    """
    active, _ = market
    await warm(active, clock, bars=20)
    active.force_closed(FALLBACK)
    await active.poll_once()

    dto = routing_to_dto(RuntimeView(settings=None, routing=active))
    assert dto is not None
    bitcoin = next(entry for entry in dto.symbols if entry.symbol == FALLBACK)
    assert bitcoin.state == "closed"
    assert bitcoin.feed_degraded is False, "a closed market must not read as a broken feed"


# -------------------------------------------------------------------- restart


async def test_a_restart_resumes_on_the_right_market_without_waiting(
    settings: AppSettings, clock: VirtualClock
) -> None:
    """Routing state is re-derived at startup, not restored from disk.

    Deliberately so. The question "which market should be on air" has one correct answer
    at any instant and it is a pure function of current availability — persisting the last
    answer could only ever let a stale one override a fresh observation, and the worst
    case is the one that matters: restarting on Monday morning onto Saturday's Bitcoin
    because that is what the file said.

    What must hold is that the re-derivation is *immediate*. The confirmation windows exist
    to stop a running station flapping between markets; a process that has just started has
    nothing to flap against, and making it wait five minutes would mean every restart
    during a gold closure spent five minutes planning against a shut market.
    """
    coordinator = RuntimeCoordinator(clock=clock)
    await coordinator.start()
    services = {
        PRIMARY: MarketDataService(
            settings,
            SimulatedFeed(symbol=PRIMARY, scenario=Scenario.RANDOM_WALK, seed=1, clock=clock),
            clock=clock,
        ),
        FALLBACK: MarketDataService(
            settings,
            SimulatedFeed(
                symbol=FALLBACK, scenario=Scenario.VOLATILITY_SPIKE, seed=2, clock=clock
            ),
            clock=clock,
        ),
    }
    restarted = ActiveMarketService(
        settings, services, coordinator=coordinator, clock=clock
    )
    # Gold is shut — the state the previous process would have been shut down in.
    restarted.force_closed(PRIMARY)
    await restarted.start()
    try:
        await warm(restarted, clock, bars=10)

        assert restarted.active_symbol == FALLBACK, (
            "a restart during a gold closure did not land on the fallback"
        )
        assert restarted.router.switch_count == 0, (
            "the startup selection was counted as a market switch"
        )
        assert restarted.router.pending_symbol is None, (
            "the startup selection went through a confirmation window"
        )

        # And a reopen from here is a real switch, held for the full reopen window.
        restarted.force_closed(PRIMARY, closed=False)
        await restarted.poll_once()
        await clock.advance(15.0)
        await restarted.poll_once()
        assert restarted.active_symbol == FALLBACK
        for _ in range(6):
            await clock.advance(10.0)
            await restarted.poll_once()
        assert restarted.active_symbol == PRIMARY
        assert restarted.router.switch_count == 1
    finally:
        await restarted.stop()
        await coordinator.drain()
        await coordinator.stop()
