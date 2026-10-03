"""Market routing: XAUUSD primary, BTCUSD fallback.

The fourteen scenarios the requirement names, plus the ones that fall out of them. Every
test drives a `VirtualClock`, so hysteresis is exercised by *advancing time deliberately*
rather than by sleeping — which is the only way a 300-second confirmation window can be
tested at all.

The test this file exists for is `test_a_feed_failure_during_trading_hours_is_not_a_closure`.
Everything else is scaffolding around that one distinction.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from tests.conftest import make_settings
from tradefix_radio.contracts.enums import FeedStatus
from tradefix_radio.core.clock import UTC, VirtualClock
from tradefix_radio.market.availability import (
    AvailabilityAssessment,
    MarketAvailability,
    assess_availability,
)
from tradefix_radio.market.router import (
    NO_ACTIVE_MARKET,
    MarketRouter,
    SwitchReason,
)

PRIMARY = "XAUUSD"
FALLBACK = "BTCUSD"

#: A Wednesday at 15:00 UTC — mid-London/New-York overlap, gold unambiguously trading.
TRADING = datetime(2026, 10, 7, 15, 0, tzinfo=UTC)
#: A Saturday. Gold is shut; Bitcoin is not.
WEEKEND = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock(start=TRADING)


@pytest.fixture
def router(clock: VirtualClock, tmp_path, clean_environ) -> MarketRouter:
    settings = make_settings(tmp_path, clean_environ)
    return MarketRouter(settings.markets, clock=clock)


def state(
    symbol: str,
    availability: MarketAvailability,
    *,
    degraded: bool = False,
) -> AvailabilityAssessment:
    """An assessment stated directly, so routing is tested apart from assessment."""
    return AvailabilityAssessment(
        symbol=symbol,
        state=availability,
        reason="constructed by test",
        feed_status=FeedStatus.LIVE,
        feed_degraded=degraded,
    )


def both(primary: MarketAvailability, fallback: MarketAvailability) -> dict:
    return {PRIMARY: state(PRIMARY, primary), FALLBACK: state(FALLBACK, fallback)}


# ------------------------------------------------------------------ 1 and 2


def test_1_an_open_primary_stays_primary(router: MarketRouter) -> None:
    decision = router.evaluate(
        both(MarketAvailability.OPEN, MarketAvailability.OPEN), now=TRADING
    )
    assert decision.active.symbol == PRIMARY
    assert decision.active.is_primary
    assert decision.active.reason is SwitchReason.INITIAL_SELECTION


def test_2_a_confirmed_closure_selects_the_fallback(
    router: MarketRouter, clock: VirtualClock
) -> None:
    router.evaluate(both(MarketAvailability.OPEN, MarketAvailability.OPEN), now=TRADING)
    clock.advance_sync(200.0)  # past the minimum dwell

    closed = both(MarketAvailability.CLOSED, MarketAvailability.OPEN)
    first = router.evaluate(closed, now=WEEKEND)
    assert not first.changed, "a closure must be confirmed before it moves the station"
    assert first.active.symbol == PRIMARY

    clock.advance_sync(130.0)  # past switch_confirmation_seconds (120 s)
    confirmed = router.evaluate(closed, now=WEEKEND)
    assert confirmed.changed
    assert confirmed.active.symbol == FALLBACK
    assert confirmed.active.reason is SwitchReason.PRIMARY_MARKET_CLOSED
    assert confirmed.previous_symbol == PRIMARY


# ----------------------------------------------------------------------- 3


def test_3_a_single_stale_tick_does_not_switch(
    router: MarketRouter, clock: VirtualClock
) -> None:
    """STALE is late data, not a closed market. The station keeps programming on it."""
    router.evaluate(both(MarketAvailability.OPEN, MarketAvailability.OPEN), now=TRADING)
    clock.advance_sync(500.0)

    for _ in range(5):
        decision = router.evaluate(
            both(MarketAvailability.STALE, MarketAvailability.OPEN), now=TRADING
        )
        clock.advance_sync(120.0)
        assert decision.active.symbol == PRIMARY
    assert router.switch_count == 0


# ----------------------------------------------------------------------- 4


def test_4_a_feed_failure_during_trading_hours_is_not_a_closure() -> None:
    """The distinction the whole subsystem exists for.

    Gold is trading — it is Wednesday afternoon — and the feed has died. That is a broker
    or network fault. Calling it "market closed" and swinging the station to Bitcoin would
    change every subsequent track's character because a TCP connection dropped.
    """
    assessment = assess_availability(
        symbol=PRIMARY,
        now=TRADING,
        settings=_default_symbol_settings(),
        data_age_seconds=9_999.0,
        feed_status=FeedStatus.DISCONNECTED,
        bars_processed=250,
    )
    assert assessment.state is MarketAvailability.UNAVAILABLE
    assert assessment.state is not MarketAvailability.CLOSED
    assert assessment.feed_degraded, "the operator must see this as a feed fault"
    assert not assessment.state.authorises_fallback
    assert assessment.calendar_open is True


def test_4b_a_broken_primary_feed_keeps_the_primary_active(
    router: MarketRouter, clock: VirtualClock
) -> None:
    """The routing consequence of the above, which is the part that reaches a listener."""
    router.evaluate(both(MarketAvailability.OPEN, MarketAvailability.OPEN), now=TRADING)
    clock.advance_sync(600.0)

    for _ in range(6):
        decision = router.evaluate(
            {
                PRIMARY: state(PRIMARY, MarketAvailability.UNAVAILABLE, degraded=True),
                FALLBACK: state(FALLBACK, MarketAvailability.OPEN),
            },
            now=TRADING,
        )
        clock.advance_sync(300.0)
        assert decision.active.symbol == PRIMARY
    assert router.switch_count == 0


# ----------------------------------------------------------------------- 5


def test_5_a_reopened_primary_is_restored_after_confirmation(
    router: MarketRouter, clock: VirtualClock
) -> None:
    router.evaluate(both(MarketAvailability.CLOSED, MarketAvailability.OPEN), now=WEEKEND)
    assert router.active_symbol == FALLBACK
    clock.advance_sync(400.0)

    reopened = both(MarketAvailability.OPEN, MarketAvailability.OPEN)
    assert not router.evaluate(reopened, now=TRADING).changed

    # Reopening is held longer than closing: 300 s, not 120 s.
    clock.advance_sync(150.0)
    assert not router.evaluate(reopened, now=TRADING).changed, (
        "the reopen window must be the longer one"
    )

    clock.advance_sync(200.0)
    decision = router.evaluate(reopened, now=TRADING)
    assert decision.changed
    assert decision.active.symbol == PRIMARY
    assert decision.active.reason is SwitchReason.PRIMARY_MARKET_REOPENED


# ----------------------------------------------------------------------- 6


def test_6_noise_around_the_open_does_not_flap(
    router: MarketRouter, clock: VirtualClock
) -> None:
    """Straggling ticks either side of the boundary must not swing the station.

    The failure this prevents is audible: XAUUSD -> BTCUSD -> XAUUSD -> BTCUSD across a few
    minutes, which a listener hears as the station changing its mind about what it is.
    """
    router.evaluate(both(MarketAvailability.CLOSED, MarketAvailability.OPEN), now=WEEKEND)
    assert router.active_symbol == FALLBACK
    clock.advance_sync(400.0)

    # Alternating open/closed every 60 s for half an hour.
    for index in range(30):
        flapping = both(
            MarketAvailability.OPEN if index % 2 == 0 else MarketAvailability.CLOSED,
            MarketAvailability.OPEN,
        )
        router.evaluate(flapping, now=TRADING)
        clock.advance_sync(60.0)

    assert router.switch_count == 0, (
        f"the station switched {router.switch_count} time(s) on boundary noise"
    )
    assert router.active_symbol == FALLBACK


def test_6b_a_sustained_reopen_still_gets_through(
    router: MarketRouter, clock: VirtualClock
) -> None:
    """The paired positive: hysteresis must not mean "never switch"."""
    router.evaluate(both(MarketAvailability.CLOSED, MarketAvailability.OPEN), now=WEEKEND)
    clock.advance_sync(400.0)
    reopened = both(MarketAvailability.OPEN, MarketAvailability.OPEN)
    for _ in range(8):
        router.evaluate(reopened, now=TRADING)
        clock.advance_sync(60.0)
    assert router.active_symbol == PRIMARY


# ----------------------------------------------------------------------- 7


def test_7_both_markets_unavailable_gives_no_active_market(
    router: MarketRouter, clock: VirtualClock
) -> None:
    """The radio keeps playing; only *planning against live data* stops."""
    router.evaluate(both(MarketAvailability.OPEN, MarketAvailability.OPEN), now=TRADING)
    clock.advance_sync(400.0)

    dead = {
        PRIMARY: state(PRIMARY, MarketAvailability.CLOSED),
        FALLBACK: state(FALLBACK, MarketAvailability.UNAVAILABLE, degraded=True),
    }
    router.evaluate(dead, now=WEEKEND)
    clock.advance_sync(200.0)
    decision = router.evaluate(dead, now=WEEKEND)

    # The fallback is UNAVAILABLE rather than CLOSED, so it is held rather than skipped —
    # a broken crypto feed is still the best candidate while gold is shut.
    assert decision.active.symbol in (FALLBACK, NO_ACTIVE_MARKET)


def test_7b_no_market_at_all_is_stated_not_invented(
    router: MarketRouter, clock: VirtualClock
) -> None:
    """With nothing configured as reachable, the router says so rather than guessing."""
    decision = router.evaluate({}, now=TRADING)
    assert decision.active.symbol == NO_ACTIVE_MARKET
    assert not decision.active.is_active
    assert decision.active.reason is SwitchReason.NO_MARKET_AVAILABLE


def test_7c_recovery_from_no_active_market(
    router: MarketRouter, clock: VirtualClock
) -> None:
    router.evaluate({}, now=TRADING)
    clock.advance_sync(400.0)
    restored = both(MarketAvailability.OPEN, MarketAvailability.OPEN)
    router.evaluate(restored, now=TRADING)
    clock.advance_sync(200.0)
    decision = router.evaluate(restored, now=TRADING)
    assert decision.active.symbol == PRIMARY
    assert decision.active.reason in (
        SwitchReason.MARKET_RESTORED,
        SwitchReason.PRIMARY_MARKET_REOPENED,
    )


# --------------------------------------------------------------- hysteresis


def test_a_newly_active_market_is_not_replaced_immediately(
    router: MarketRouter, clock: VirtualClock
) -> None:
    """The minimum dwell, independent of the confirmation windows.

    Without it, a market that has just been selected can be replaced the moment its
    assessment wobbles, and the two timers together are what make the station steady.
    """
    router.evaluate(both(MarketAvailability.OPEN, MarketAvailability.OPEN), now=TRADING)
    clock.advance_sync(10.0)

    closed = both(MarketAvailability.CLOSED, MarketAvailability.OPEN)
    for _ in range(3):
        router.evaluate(closed, now=WEEKEND)
        clock.advance_sync(30.0)
    assert router.active_symbol == PRIMARY, "replaced before the minimum dwell elapsed"


def test_a_pending_switch_is_cancelled_when_the_condition_clears(
    router: MarketRouter, clock: VirtualClock
) -> None:
    router.evaluate(both(MarketAvailability.OPEN, MarketAvailability.OPEN), now=TRADING)
    clock.advance_sync(400.0)

    router.evaluate(both(MarketAvailability.CLOSED, MarketAvailability.OPEN), now=WEEKEND)
    assert router.pending_symbol == FALLBACK

    clock.advance_sync(30.0)
    router.evaluate(both(MarketAvailability.OPEN, MarketAvailability.OPEN), now=TRADING)
    assert router.pending_symbol is None
    assert router.active_symbol == PRIMARY


def test_the_pending_wait_is_visible(router: MarketRouter, clock: VirtualClock) -> None:
    """An operator watching a switch that has not happened needs to see why."""
    router.evaluate(both(MarketAvailability.OPEN, MarketAvailability.OPEN), now=TRADING)
    clock.advance_sync(400.0)
    router.evaluate(both(MarketAvailability.CLOSED, MarketAvailability.OPEN), now=WEEKEND)
    remaining = router.pending_seconds_remaining()
    assert remaining is not None
    assert 0.0 < remaining <= 120.0


# ------------------------------------------------------------- assessment


def test_a_weekend_closes_gold_but_not_bitcoin() -> None:
    """Calendar knowledge, applied per instrument rather than globally."""
    settings = _default_symbol_settings()
    gold = assess_availability(
        symbol="XAUUSD", now=WEEKEND, settings=settings,
        data_age_seconds=5.0, feed_status=FeedStatus.LIVE, bars_processed=100,
    )
    bitcoin = assess_availability(
        symbol="BTCUSD", now=WEEKEND, settings=settings,
        data_age_seconds=5.0, feed_status=FeedStatus.LIVE, bars_processed=100,
    )
    assert gold.state is MarketAvailability.CLOSED
    assert gold.calendar_open is False
    assert bitcoin.state is MarketAvailability.OPEN
    assert bitcoin.calendar_open is True


def test_crypto_is_never_closed_for_a_calendar_reason() -> None:
    """So a crypto outage can only ever be classified as a feed problem."""
    settings = _default_symbol_settings()
    for moment in (TRADING, WEEKEND):
        assessment = assess_availability(
            symbol="BTCUSD", now=moment, settings=settings,
            data_age_seconds=99_999.0, feed_status=FeedStatus.DISCONNECTED,
            bars_processed=500,
        )
        assert assessment.state is not MarketAvailability.CLOSED
        assert assessment.feed_degraded


def test_a_provider_reported_closure_outranks_the_calendar() -> None:
    """A venue knows its own holidays; our calendar deliberately does not model them."""
    assessment = assess_availability(
        symbol="XAUUSD", now=TRADING, settings=_default_symbol_settings(),
        data_age_seconds=5.0, feed_status=FeedStatus.LIVE, bars_processed=100,
        feed_reported_closed=True,
    )
    assert assessment.state is MarketAvailability.CLOSED
    assert assessment.state.authorises_fallback


def test_a_feed_that_has_never_produced_data_is_unknown_not_closed() -> None:
    assessment = assess_availability(
        symbol="XAUUSD", now=TRADING, settings=_default_symbol_settings(),
        data_age_seconds=None, feed_status=FeedStatus.DISCONNECTED, bars_processed=0,
    )
    assert assessment.state is MarketAvailability.UNKNOWN


def test_only_closed_authorises_a_fallback() -> None:
    """The rule, asserted directly so widening it fails here first."""
    assert MarketAvailability.CLOSED.authorises_fallback
    for other in MarketAvailability:
        if other is not MarketAvailability.CLOSED:
            assert not other.authorises_fallback, f"{other} must not authorise a fallback"


# ------------------------------------------------------------------- event


def test_the_switch_event_carries_everything_the_requirement_names(
    router: MarketRouter, clock: VirtualClock
) -> None:
    router.evaluate(both(MarketAvailability.OPEN, MarketAvailability.OPEN), now=TRADING)
    clock.advance_sync(400.0)
    closed = both(MarketAvailability.CLOSED, MarketAvailability.OPEN)
    router.evaluate(closed, now=WEEKEND)
    clock.advance_sync(200.0)
    decision = router.evaluate(closed, now=WEEKEND)

    payload = decision.event_payload
    assert payload["previous_symbol"] == PRIMARY
    assert payload["new_symbol"] == FALLBACK
    assert payload["reason"] == "primary_market_closed"
    assert payload["previous_state"] == "open"
    assert payload["new_state"] == "open"
    assert payload["timestamp"]


def _default_symbol_settings():
    from tradefix_radio.config.schema import MarketSymbolSettings

    return MarketSymbolSettings()


# ------------------------------------------------- acquiring a market at startup


def test_the_first_feed_to_come_up_is_adopted_immediately(
    router: MarketRouter, clock: VirtualClock
) -> None:
    """The ordinary startup sequence, and a defect the restart test caught.

    A process begins with nothing assessed, so the first evaluation lands on
    NO_ACTIVE_MARKET. If the arrival of real data were then treated as an ordinary switch,
    every launch would spend the full confirmation window — two minutes at the defaults —
    planning against no market and broadcasting on emergency tiers, and would then log a
    switch nobody made.
    """
    first = router.evaluate(
        both(MarketAvailability.UNKNOWN, MarketAvailability.UNKNOWN), now=TRADING
    )
    assert first.active.symbol == NO_ACTIVE_MARKET

    # Gold's feed comes up. No time passes at all.
    decision = router.evaluate(
        both(MarketAvailability.OPEN, MarketAvailability.UNKNOWN), now=TRADING
    )
    assert decision.active.symbol == PRIMARY, (
        "the station waited out a confirmation window before adopting its first market"
    )
    assert router.pending_symbol is None
    assert router.switch_count == 0, (
        "acquiring a first market was counted as a market switch"
    )


def test_acquiring_a_market_does_not_bypass_later_hysteresis(
    router: MarketRouter, clock: VirtualClock
) -> None:
    """The immediate path must not leave the router permanently impatient.

    Only the transition *out of* no-market is free. Once a market is active, every
    subsequent change goes through the dwell time and the confirmation window as before —
    otherwise the startup fix would have quietly disabled the anti-flap behaviour.
    """
    router.evaluate(
        both(MarketAvailability.UNKNOWN, MarketAvailability.UNKNOWN), now=TRADING
    )
    router.evaluate(both(MarketAvailability.OPEN, MarketAvailability.OPEN), now=TRADING)
    assert router.active_symbol == PRIMARY

    # Past the minimum dwell, then close gold. The switch must still be confirmed.
    clock.advance_sync(600.0)
    decision = router.evaluate(
        both(MarketAvailability.CLOSED, MarketAvailability.OPEN), now=TRADING
    )
    assert not decision.changed
    assert router.pending_symbol == FALLBACK
