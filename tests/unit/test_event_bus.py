"""Event bus (§91, milestone 1.5).

The exit criteria are subscriber isolation and bounded memory. Both are properties
a 24/7 process depends on absolutely: an unbounded queue is a slow memory leak
(§64 checks for exactly that), and a subscriber that can break ``publish`` means a
wedged OBS client can stall the scheduler.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tradefix_radio.contracts.enums import MarketDirection, MarketRegime, PlayoutTier
from tradefix_radio.contracts.events import (
    MarketEnergyChanged,
    MarketStateChanged,
    PlayoutFallbackEntered,
    TrackReady,
)
from tradefix_radio.contracts.market import MarketStateV1
from tradefix_radio.core.events import EventBus, OverflowPolicy
from tests.conftest import FIXED_NOW


def make_market_state() -> MarketStateV1:
    from tradefix_radio.contracts.enums import FeedStatus, TradingSession

    return MarketStateV1(
        symbol="XAUUSD",
        timestamp=FIXED_NOW,
        regime=MarketRegime.BULLISH_BREAKOUT,
        direction=MarketDirection.BULLISH,
        session=TradingSession.LONDON,
        feed_status=FeedStatus.SIMULATED,
        energy=80.0,
        energy_velocity=2.0,
        volatility=70.0,
        trend_strength=85.0,
        momentum=70.0,
        compression=10.0,
        confidence=0.8,
        regime_age_seconds=300.0,
        data_age_seconds=0.5,
    )


def energy_event(value: float = 50.0) -> MarketEnergyChanged:
    return MarketEnergyChanged(
        at=FIXED_NOW,
        symbol="XAUUSD",
        energy=value,
        energy_velocity=0.0,
        smoothed_energy=value,
    )


# ---------------------------------------------------------------- routing


async def test_exact_topic_match_delivers() -> None:
    bus = EventBus()
    received: list[Any] = []
    bus.subscribe("market.energy_changed", lambda e: _collect(received, e))
    await bus.publish(energy_event(42.0))
    await bus.drain()
    assert len(received) == 1
    assert received[0].energy == 42.0
    await bus.aclose()


async def test_prefix_wildcard_matches_the_domain() -> None:
    bus = EventBus()
    market: list[Any] = []
    bus.subscribe("market.*", lambda e: _collect(market, e))
    await bus.publish(energy_event())
    await bus.publish(
        MarketStateChanged(at=FIXED_NOW, state=make_market_state(), previous_regime="quiet")
    )
    await bus.publish(TrackReady(at=FIXED_NOW, track_id="TF-1", duration_seconds=200.0, novelty_score=0.9))
    await bus.drain()
    assert len(market) == 2
    await bus.aclose()


async def test_global_wildcard_matches_everything() -> None:
    bus = EventBus()
    seen: list[Any] = []
    bus.subscribe("*", lambda e: _collect(seen, e))
    await bus.publish(energy_event())
    await bus.publish(TrackReady(at=FIXED_NOW, track_id="TF-1", duration_seconds=200.0, novelty_score=0.9))
    await bus.drain()
    assert len(seen) == 2
    await bus.aclose()


async def test_non_matching_subscriber_receives_nothing() -> None:
    bus = EventBus()
    seen: list[Any] = []
    bus.subscribe("obs.*", lambda e: _collect(seen, e))
    await bus.publish(energy_event())
    await bus.drain()
    assert seen == []
    await bus.aclose()


async def test_prefix_wildcard_does_not_match_a_sibling_prefix() -> None:
    """``market.*`` must not match a hypothetical ``marketing.*`` topic."""
    bus = EventBus()
    sub = bus.subscribe("market.*", lambda e: None)
    assert sub.matches("market.state_changed")
    assert not sub.matches("marketing.state_changed")
    assert not sub.matches("market")
    await bus.aclose()


async def test_multiple_subscribers_all_receive_the_event() -> None:
    bus = EventBus()
    first: list[Any] = []
    second: list[Any] = []
    bus.subscribe("market.*", lambda e: _collect(first, e), name="a")
    bus.subscribe("*", lambda e: _collect(second, e), name="b")
    await bus.publish(energy_event())
    await bus.drain()
    assert len(first) == 1
    assert len(second) == 1
    await bus.aclose()


# ---------------------------------------------------------------- isolation


async def test_a_raising_subscriber_does_not_affect_others() -> None:
    """§91's core requirement: one bad subscriber must not break the bus."""
    bus = EventBus()
    good: list[Any] = []

    async def explode(_event: Any) -> None:
        raise RuntimeError("subscriber bug")

    bus.subscribe("market.*", explode, name="bad")
    bus.subscribe("market.*", lambda e: _collect(good, e), name="good")

    for _ in range(3):
        await bus.publish(energy_event())
    await bus.drain()

    assert len(good) == 3
    stats = bus.stats()
    assert stats["bad"].failed == 3
    assert stats["bad"].delivered == 0
    assert stats["good"].delivered == 3
    await bus.aclose()


async def test_a_raising_subscriber_keeps_consuming_later_events() -> None:
    """The worker must survive its handler, not die on the first exception."""
    bus = EventBus()
    attempts: list[int] = []

    async def flaky(_event: Any) -> None:
        attempts.append(len(attempts))
        raise ValueError("always fails")

    bus.subscribe("market.*", flaky, name="flaky")
    for _ in range(5):
        await bus.publish(energy_event())
    await bus.drain()
    assert len(attempts) == 5
    await bus.aclose()


async def test_a_slow_subscriber_does_not_block_publish() -> None:
    """``publish`` only enqueues; a wedged consumer cannot stall the producer."""
    bus = EventBus(default_queue_size=64)
    release = asyncio.Event()

    async def slow(_event: Any) -> None:
        await release.wait()

    bus.subscribe("market.*", slow, name="slow")
    # If publish awaited the handler this would hang rather than complete.
    await asyncio.wait_for(
        asyncio.gather(*(bus.publish(energy_event()) for _ in range(10))), timeout=2.0
    )
    release.set()
    await bus.drain()
    await bus.aclose()


# ---------------------------------------------------------------- backpressure


async def test_drop_oldest_bounds_memory_and_keeps_the_newest() -> None:
    """Correct for state snapshots: only the most recent value matters."""
    bus = EventBus(default_queue_size=3)
    release = asyncio.Event()
    received: list[float] = []

    async def blocked(event: Any) -> None:
        await release.wait()
        received.append(event.energy)

    sub = bus.subscribe(
        "market.*", blocked, name="snapshot", overflow=OverflowPolicy.DROP_OLDEST
    )
    for value in range(10):
        await bus.publish(energy_event(float(value)))

    assert sub.queue.qsize() <= 3
    assert sub.stats.dropped > 0
    release.set()
    await bus.drain()
    # The newest event must have survived eviction.
    assert 9.0 in received
    await bus.aclose()


async def test_drop_newest_keeps_the_earliest_entries() -> None:
    """Correct for append-only logs, where earlier entries carry more context."""
    bus = EventBus(default_queue_size=3)
    release = asyncio.Event()
    received: list[float] = []

    async def blocked(event: Any) -> None:
        await release.wait()
        received.append(event.energy)

    sub = bus.subscribe(
        "market.*", blocked, name="log", overflow=OverflowPolicy.DROP_NEWEST
    )
    for value in range(10):
        await bus.publish(energy_event(float(value)))

    assert sub.stats.dropped > 0
    release.set()
    await bus.drain()
    assert 0.0 in received
    assert 9.0 not in received
    await bus.aclose()


async def test_block_policy_applies_real_backpressure() -> None:
    """BLOCK must actually wait rather than drop."""
    bus = EventBus(default_queue_size=2)
    processed: list[float] = []

    async def steady(event: Any) -> None:
        processed.append(event.energy)

    sub = bus.subscribe("market.*", steady, name="steady", overflow=OverflowPolicy.BLOCK)
    for value in range(8):
        await bus.publish(energy_event(float(value)))
    await bus.drain()
    assert sub.stats.dropped == 0
    assert len(processed) == 8
    await bus.aclose()


async def test_queue_depth_high_water_mark_is_recorded() -> None:
    """§56 needs this to spot a subscriber that is falling behind."""
    bus = EventBus(default_queue_size=16)
    release = asyncio.Event()
    sub = bus.subscribe("market.*", lambda _e: release.wait(), name="watch")
    for _ in range(5):
        await bus.publish(energy_event())
    assert sub.stats.max_queue_depth > 0
    release.set()
    await bus.drain()
    await bus.aclose()


# ---------------------------------------------------------------- lifecycle


async def test_unsubscribe_stops_delivery_and_leaves_nothing_behind() -> None:
    bus = EventBus()
    seen: list[Any] = []
    sub = bus.subscribe("market.*", lambda e: _collect(seen, e))
    await bus.publish(energy_event())
    await bus.drain()
    await bus.unsubscribe(sub)
    await bus.publish(energy_event())
    await bus.drain()
    assert len(seen) == 1
    assert bus.subscriptions == ()
    await bus.aclose()


async def test_unsubscribing_twice_is_safe() -> None:
    bus = EventBus()
    sub = bus.subscribe("market.*", lambda _e: asyncio.sleep(0))
    await bus.unsubscribe(sub)
    await bus.unsubscribe(sub)
    await bus.aclose()


async def test_close_flushes_pending_events() -> None:
    """§74 graceful shutdown: in-flight persistence writes must land."""
    bus = EventBus()
    seen: list[Any] = []
    bus.subscribe("*", lambda e: _collect(seen, e))
    for _ in range(5):
        await bus.publish(energy_event())
    await bus.aclose()
    assert len(seen) == 5


async def test_publish_after_close_raises() -> None:
    bus = EventBus()
    await bus.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await bus.publish(energy_event())


async def test_subscribe_after_close_raises() -> None:
    bus = EventBus()
    await bus.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        bus.subscribe("*", lambda _e: asyncio.sleep(0))


async def test_close_is_idempotent() -> None:
    bus = EventBus()
    await bus.aclose()
    await bus.aclose()


async def test_drain_returns_false_when_a_subscriber_never_finishes() -> None:
    """A hung subscriber must not hang shutdown indefinitely."""
    bus = EventBus()
    forever = asyncio.Event()
    bus.subscribe("*", lambda _e: forever.wait(), name="hung")
    await bus.publish(energy_event())
    assert await bus.drain(timeout=0.1) is False
    forever.set()
    await bus.aclose(drain_timeout=0.2)


async def test_empty_bus_drains_immediately() -> None:
    bus = EventBus()
    assert await bus.drain(timeout=0.5) is True
    await bus.aclose()


async def test_published_count_tracks_every_publish() -> None:
    bus = EventBus()
    for _ in range(7):
        await bus.publish(energy_event())
    assert bus.published_count == 7
    await bus.aclose()


async def test_duplicate_subscription_names_are_allowed() -> None:
    """Two overlay instances may legitimately register the same handler name."""
    bus = EventBus()
    seen: list[Any] = []
    bus.subscribe("*", lambda e: _collect(seen, e), name="dup")
    bus.subscribe("*", lambda e: _collect(seen, e), name="dup")
    await bus.publish(energy_event())
    await bus.drain()
    assert len(seen) == 2
    await bus.aclose()


async def test_empty_pattern_is_rejected() -> None:
    bus = EventBus()
    with pytest.raises(ValueError, match="must not be empty"):
        bus.subscribe("", lambda _e: asyncio.sleep(0))
    await bus.aclose()


def test_zero_queue_size_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be >= 1"):
        EventBus(default_queue_size=0)


# ---------------------------------------------------------------- event shapes


def test_every_event_declares_a_namespaced_topic() -> None:
    """§91 topics are ``<domain>.<past_tense_fact>``; commands have no place here."""
    from tradefix_radio.contracts import events as event_module

    from tradefix_radio.contracts.events import BaseEvent

    classes = [
        value
        for name, value in vars(event_module).items()
        if isinstance(value, type)
        and issubclass(value, BaseEvent)
        and value is not BaseEvent
        and not name.startswith("_")
    ]
    assert len(classes) >= 18
    topics = set()
    for cls in classes:
        topic = cls.TOPIC
        assert "." in topic, f"{cls.__name__} topic {topic!r} is not namespaced"
        assert topic.islower(), f"{cls.__name__} topic {topic!r} is not lowercase"
        assert topic not in topics, f"duplicate topic {topic!r}"
        topics.add(topic)


def test_event_topic_property_reflects_the_class_constant() -> None:
    event = PlayoutFallbackEntered(
        at=FIXED_NOW,
        tier=PlayoutTier.PROCEDURAL,
        previous_tier=PlayoutTier.SCHEDULED,
        reason="queue empty",
    )
    assert event.topic == "playout.fallback_entered"
    assert event.topic == PlayoutFallbackEntered.TOPIC


async def _collect(sink: list[Any], event: Any) -> None:
    sink.append(event)
