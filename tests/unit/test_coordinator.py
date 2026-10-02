"""Runtime coordinator: event ordering, subscriber isolation, task supervision (§4.3).

The coordinator's whole purpose is that **one component's failure stays that component's
failure**. So most of what is asserted here is negative: a handler that raises does not stop
the publisher, a background task that crashes does not take the station down, and a shutdown
completes even when something refuses to.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from tests.conftest import FIXED_NOW
from tradefix_radio.contracts.events import StationIdPlayed, TrackFinished, TrackReady
from tradefix_radio.core.clock import VirtualClock
from tradefix_radio.core.events import Event
from tradefix_radio.runtime.coordinator import RuntimeCoordinator

pytestmark = pytest.mark.asyncio


@dataclass
class Recorder:
    """Collects the topics it was handed, in order."""

    topics: list[str]

    def __init__(self) -> None:
        self.topics = []

    async def __call__(self, event: Event) -> None:
        self.topics.append(event.topic)


def ready(track_id: str) -> TrackReady:
    return TrackReady(
        at=FIXED_NOW, track_id=track_id, duration_seconds=180.0, novelty_score=0.9
    )


def finished(track_id: str) -> TrackFinished:
    return TrackFinished(
        at=FIXED_NOW, track_id=track_id, played_seconds=180.0, completed=True
    )


@pytest.fixture
def coordinator() -> RuntimeCoordinator:
    return RuntimeCoordinator(clock=VirtualClock())


# -- delivery and ordering -------------------------------------------------


async def test_a_subscriber_receives_a_published_event(
    coordinator: RuntimeCoordinator,
) -> None:
    seen = Recorder()
    coordinator.subscribe("track.*", seen, name="seen")
    async with coordinator:
        await coordinator.publish(ready("1"))
        await coordinator.drain()
    assert seen.topics == ["track.ready"]


async def test_b_events_arrive_in_publication_order(
    coordinator: RuntimeCoordinator,
) -> None:
    """§4.3 requires ordering. A queue is only useful here if it preserves it."""
    seen = Recorder()
    coordinator.subscribe("*", seen, name="seen")
    async with coordinator:
        for index in range(25):
            await coordinator.publish(ready(str(index)))
        await coordinator.publish(finished("x"))
        await coordinator.drain()
    assert seen.topics == ["track.ready"] * 25 + ["track.finished"]


async def test_c_a_subscriber_only_receives_its_pattern(
    coordinator: RuntimeCoordinator,
) -> None:
    tracks, radio = Recorder(), Recorder()
    coordinator.subscribe("track.*", tracks, name="tracks")
    coordinator.subscribe("radio.*", radio, name="radio")
    async with coordinator:
        await coordinator.publish(ready("1"))
        await coordinator.publish(StationIdPlayed(at=FIXED_NOW, station_id_key="brand-1"))
        await coordinator.drain()
    assert tracks.topics == ["track.ready"]
    assert radio.topics == ["radio.station_id_played"]


async def test_d_subscribing_requires_a_name(coordinator: RuntimeCoordinator) -> None:
    """The name is not decoration: it is how an error is attributed in §4.3's counters."""
    with pytest.raises(TypeError):
        coordinator.subscribe("track.*", Recorder())  # type: ignore[call-arg]


# -- isolation -------------------------------------------------------------


async def test_e_a_failing_subscriber_does_not_stop_the_publisher(
    coordinator: RuntimeCoordinator,
) -> None:
    async def explode(_event: Event) -> None:
        raise RuntimeError("subscriber is broken")

    coordinator.subscribe("track.*", explode, name="broken")
    async with coordinator:
        for index in range(5):
            await coordinator.publish(ready(str(index)))  # must not raise
        await coordinator.drain()
    assert coordinator.subscriber_errors() == {"broken": 5}


async def test_f_a_failing_subscriber_does_not_starve_the_others(
    coordinator: RuntimeCoordinator,
) -> None:
    """The isolation that matters: playout must still hear about a ready track when
    some metrics listener is throwing."""

    async def explode(_event: Event) -> None:
        raise RuntimeError("broken")

    healthy = Recorder()
    coordinator.subscribe("track.*", explode, name="broken")
    coordinator.subscribe("track.*", healthy, name="healthy")
    async with coordinator:
        await coordinator.publish(ready("1"))
        await coordinator.publish(ready("2"))
        await coordinator.drain()
    assert healthy.topics == ["track.ready", "track.ready"]
    assert coordinator.stats.handler_errors == 2


async def test_g_a_cancelled_subscriber_is_not_counted_as_an_error(
    coordinator: RuntimeCoordinator,
) -> None:
    """Cancellation is shutdown, not failure. Counting it would make every clean stop
    look like a fault in the logs §57 alerts on."""

    async def cancel_itself(_event: Event) -> None:
        raise asyncio.CancelledError

    coordinator.subscribe("track.*", cancel_itself, name="cancelled")
    async with coordinator:
        await coordinator.publish(ready("1"))
        await coordinator.drain()
    assert coordinator.subscriber_errors() == {}


# -- supervised tasks ------------------------------------------------------


async def test_h_spawned_tasks_run_and_are_counted(
    coordinator: RuntimeCoordinator,
) -> None:
    started = asyncio.Event()

    async def worker() -> None:
        started.set()
        await asyncio.Event().wait()  # never returns; stop() must cancel it

    coordinator.spawn("worker", worker)
    async with coordinator:
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert coordinator.live_task_count() == 1
        assert "worker" in coordinator.task_names
    assert coordinator.live_task_count() == 0


async def test_i_a_crashing_task_does_not_bring_down_the_coordinator(
    coordinator: RuntimeCoordinator,
) -> None:
    """§86: one model crash must not stop the radio. The same rule applies to every
    task the coordinator owns."""

    async def doomed() -> None:
        raise RuntimeError("generation worker died")

    survivor_ran = asyncio.Event()

    async def survivor() -> None:
        survivor_ran.set()
        await asyncio.Event().wait()

    coordinator.spawn("doomed", doomed)
    coordinator.spawn("survivor", survivor)
    async with coordinator:
        await asyncio.wait_for(survivor_ran.wait(), timeout=1.0)
        for _ in range(20):
            if coordinator.stats.tasks_crashed:
                break
            await asyncio.sleep(0.01)
        assert coordinator.stats.tasks_crashed == 1
        assert coordinator.is_running
        await coordinator.publish(ready("1"))  # bus still works
        await coordinator.drain()


async def test_j_stop_cancels_a_task_that_will_not_finish(
    coordinator: RuntimeCoordinator,
) -> None:
    """A task that ignores the stop signal must not hold the process open — §74's
    shutdown has to terminate whatever the components do."""
    entered = asyncio.Event()

    async def stubborn() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    coordinator.spawn("stubborn", stubborn)
    await coordinator.start()
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    await asyncio.wait_for(coordinator.stop(timeout_seconds=0.2), timeout=5.0)
    assert coordinator.live_task_count() == 0
    assert not coordinator.is_running


async def test_k_stop_is_idempotent(coordinator: RuntimeCoordinator) -> None:
    """Both the watchdog and the signal handler can call it, possibly at once."""
    await coordinator.start()
    await coordinator.stop()
    await coordinator.stop()
    assert not coordinator.is_running


async def test_l_tasks_stop_before_the_bus(coordinator: RuntimeCoordinator) -> None:
    """Order matters: a task publishing during teardown must not meet a closed bus,
    because the resulting error would be logged as a fault on every clean shutdown."""
    seen = Recorder()
    coordinator.subscribe("track.*", seen, name="seen")

    async def chatty() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await coordinator.publish(ready("last-gasp"))
            raise

    coordinator.spawn("chatty", chatty)
    await coordinator.start()
    await asyncio.sleep(0.01)
    await coordinator.stop(timeout_seconds=1.0)
    assert seen.topics == ["track.ready"]


async def test_m_publishing_after_stop_does_not_raise(
    coordinator: RuntimeCoordinator,
) -> None:
    """A late event is a non-event. §86 forbids suppressed errors, but this is not an
    error: it is a component shutting down a moment after the bus did."""
    await coordinator.start()
    await coordinator.stop()
    await coordinator.publish(ready("1"))


async def test_n_stats_count_by_topic(coordinator: RuntimeCoordinator) -> None:
    async with coordinator:
        await coordinator.publish(ready("1"))
        await coordinator.publish(ready("2"))
        await coordinator.publish(finished("x"))
        await coordinator.drain()
    assert coordinator.topic_counts() == {"track.ready": 2, "track.finished": 1}
    assert coordinator.stats.published == 3


async def test_o_drain_returns_once_the_queue_is_empty(
    coordinator: RuntimeCoordinator,
) -> None:
    """Every deterministic test in the suite depends on drain() actually meaning
    "delivery is finished", so it is worth asserting directly."""
    slow_seen: list[str] = []

    async def slow(event: Event) -> None:
        await asyncio.sleep(0.005)
        slow_seen.append(event.topic)

    coordinator.subscribe("track.*", slow, name="slow")
    async with coordinator:
        for index in range(10):
            await coordinator.publish(ready(str(index)))
        await coordinator.drain()
        assert len(slow_seen) == 10
