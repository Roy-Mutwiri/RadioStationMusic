"""RuntimeCoordinator — the one place services are wired together (§91, milestone 4.3).

Without this, the scheduler holds a reference to the playout engine, the playout engine calls
back into the scheduler, the generation manager needs both, and three months later nobody can
change one without reading all of them. That graph is the thing being prevented here.

**What it is.** A façade over the event bus that owns subsystem lifecycle (start, stop,
background tasks) and nothing else. Services publish facts and subscribe to facts. The
coordinator never contains policy — if a decision lives here instead of in a subsystem, that is
a design error, because a decision in the wiring layer is a decision nobody can unit-test.

**Deliberately not a distributed message bus.** §88 lists that as a non-goal for V1, and the
whole station runs in at most three processes (ADR-08). What matters is that the *interface* is
replaceable: subsystems depend on :meth:`publish` and :meth:`subscribe`, never on
``asyncio.Queue``, so putting Redis or NATS behind it later is an adapter rather than a rewrite.

**Subscriber failure is isolated.** One subscriber raising must not stop delivery to the others,
and must not stop the publisher. The bus already runs each handler in its own task
(:mod:`tradefix_radio.core.events`); this adds the counting and logging that makes a failing
subscriber *visible* rather than merely survivable — a silently broken OBS overlay looks exactly
like a working one.

**Ordering, honestly stated.** Events arrive at each subscriber in publication order, because
each subscription has its own FIFO queue. There is no ordering guarantee *between* subscribers,
and none is needed: an event is a fact that already happened, so no subscriber's view of it
depends on another's.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import structlog

from tradefix_radio.core.clock import Clock, SystemClock
from tradefix_radio.core.events import Event, EventBus, OverflowPolicy, Subscription

_log = structlog.get_logger(__name__)

EventHandler = Callable[[Any], Awaitable[None]]

#: A long-running coroutine the coordinator owns. Returns when asked to stop.
BackgroundTask = Callable[[], Awaitable[None]]


@dataclass
class CoordinatorStats:
    """What happened, for the §101 report and the soak command's exit code."""

    published: int = 0
    handler_errors: int = 0
    tasks_started: int = 0
    tasks_crashed: int = 0
    by_topic: dict[str, int] = field(default_factory=dict)
    errors_by_subscriber: dict[str, int] = field(default_factory=dict)

    def record_publish(self, topic: str) -> None:
        self.published += 1
        self.by_topic[topic] = self.by_topic.get(topic, 0) + 1

    def record_handler_error(self, subscriber: str) -> None:
        self.handler_errors += 1
        self.errors_by_subscriber[subscriber] = (
            self.errors_by_subscriber.get(subscriber, 0) + 1
        )


class RuntimeCoordinator:
    """Owns the event bus and the station's background tasks."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        bus: EventBus | None = None,
        queue_size: int = 512,
    ) -> None:
        self._clock = clock or SystemClock()
        self._bus = bus or EventBus(default_queue_size=queue_size)
        self._stats = CoordinatorStats()
        self._subscriptions: list[Subscription] = []
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stopping = asyncio.Event()
        self._started = False

    # -- introspection -----------------------------------------------------

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def bus(self) -> EventBus:
        return self._bus

    @property
    def stats(self) -> CoordinatorStats:
        return self._stats

    @property
    def is_running(self) -> bool:
        return self._started and not self._stopping.is_set()

    @property
    def task_names(self) -> tuple[str, ...]:
        return tuple(self._tasks)

    def live_task_count(self) -> int:
        """Tasks still running. The soak report's "leaked tasks" figure."""
        return sum(1 for task in self._tasks.values() if not task.done())

    # -- events ------------------------------------------------------------

    async def publish(self, event: Event) -> None:
        """Announce a fact. Never raises for a subscriber's sake.

        A publisher is almost always in the middle of something more important than the event —
        finishing a track, starting playback — and a subscriber's failure must not interrupt it.
        """
        self._stats.record_publish(event.topic)
        try:
            await self._bus.publish(event)
        except Exception as error:  # noqa: BLE001 - publication must never break a publisher
            _log.error(
                "coordinator.publish_failed",
                topic=event.topic,
                error=str(error),
                detail="the event was dropped; the publisher continues",
            )

    def subscribe(
        self,
        pattern: str,
        handler: EventHandler,
        *,
        name: str,
        overflow: OverflowPolicy = OverflowPolicy.DROP_OLDEST,
    ) -> Subscription:
        """Register ``handler`` for ``pattern``, wrapped so its failures are isolated.

        ``name`` is required rather than derived. A derived name is the handler's ``__qualname__``,
        which for the method of a subsystem there are two of reads identically for both — and the
        error counters are keyed by name, so an operator chasing "which subscriber is failing"
        would get an answer that cannot be acted on.
        """
        wrapped = self._isolate(handler, name)
        subscription = self._bus.subscribe(
            pattern, wrapped, name=name, overflow=overflow
        )
        self._subscriptions.append(subscription)
        return subscription

    def _isolate(self, handler: EventHandler, name: str) -> EventHandler:
        """Wrap a handler so an exception is counted and logged, never propagated."""

        async def isolated(event: Any) -> None:
            try:
                await handler(event)
            except asyncio.CancelledError:
                # Shutdown, not a failure. Must propagate or the task never stops.
                raise
            except Exception as error:  # noqa: BLE001 - isolation is this function's whole job
                self._stats.record_handler_error(name)
                _log.error(
                    "coordinator.subscriber_failed",
                    subscriber=name,
                    topic=getattr(event, "topic", "unknown"),
                    error_type=type(error).__name__,
                    error=str(error),
                    exc_info=True,
                )

        return isolated

    # -- background tasks --------------------------------------------------

    def spawn(self, name: str, coroutine_factory: BackgroundTask) -> None:
        """Run a long-lived loop under the coordinator's ownership.

        Owned rather than fire-and-forget because an unreferenced task is both invisible and
        garbage-collectable mid-await. Every loop the station runs — the playout engine, the
        generation worker, the lease reclaimer — goes through here so that
        :meth:`live_task_count` is the truth and shutdown can wait for all of them.
        """
        if name in self._tasks and not self._tasks[name].done():
            raise RuntimeError(f"a background task named {name!r} is already running")

        async def supervised() -> None:
            try:
                await coroutine_factory()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - a crashed loop must not be silent
                self._stats.tasks_crashed += 1
                _log.error(
                    "coordinator.task_crashed",
                    task=name,
                    error_type=type(error).__name__,
                    error=str(error),
                    exc_info=True,
                )

        self._tasks[name] = asyncio.create_task(supervised(), name=f"radio:{name}")
        self._stats.tasks_started += 1
        _log.debug("coordinator.task_started", task=name)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._started = True
        self._stopping.clear()
        _log.info("coordinator.started", subscribers=len(self._subscriptions))

    async def wait_for_stop(self) -> None:
        """Block until :meth:`stop` is called. What a background loop awaits to exit."""
        await self._stopping.wait()

    @property
    def should_stop(self) -> bool:
        return self._stopping.is_set()

    async def stop(self, *, timeout_seconds: float = 15.0) -> None:
        """Graceful shutdown (§74).

        Order matters and is the opposite of the intuitive one: **tasks first, bus second**. A
        task shut down after the bus would publish into a closed bus during its own teardown —
        a "finished playing" event that nobody records, which is exactly the state §75's
        recovery then has to guess at.
        """
        if not self._started:
            return
        self._stopping.set()

        pending = [task for task in self._tasks.values() if not task.done()]
        if pending:
            _, still_running = await asyncio.wait(pending, timeout=timeout_seconds)
            for task in still_running:
                task.cancel()
            if still_running:
                await asyncio.gather(*still_running, return_exceptions=True)
                _log.warning(
                    "coordinator.tasks_cancelled",
                    count=len(still_running),
                    names=[t.get_name() for t in still_running],
                )

        for subscription in self._subscriptions:
            with contextlib.suppress(Exception):
                await self._bus.unsubscribe(subscription)
        self._subscriptions.clear()

        with contextlib.suppress(Exception):
            await self._bus.aclose()

        self._started = False
        _log.info(
            "coordinator.stopped",
            published=self._stats.published,
            handler_errors=self._stats.handler_errors,
            tasks_crashed=self._stats.tasks_crashed,
        )

    async def __aenter__(self) -> RuntimeCoordinator:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    # -- testing support ---------------------------------------------------

    async def drain(self, *, timeout_seconds: float = 5.0) -> bool:
        """Wait until every published event has been **handled**. Returns ``False`` on timeout.

        For tests and for the soak command's final accounting. Publication is asynchronous, so
        asserting on a subscriber's effects right after publishing is a race — and a test that
        sleeps instead is a test that is slow *and* flaky.

        Delegates to the bus, which tracks completion with ``Queue.join`` — i.e. against
        ``task_done`` called *after* the handler returns.

        The first version of this method polled ``queue.empty()`` instead, and was wrong in the
        one case it existed to cover: a handler that has taken the last event off its queue but
        is still awaiting leaves the queue empty while delivery is unfinished. A test asserting
        on ten events saw nine. Polling emptiness cannot distinguish "nothing left to deliver"
        from "nothing left to *start* delivering", and only the former is what a caller means.
        """
        drained = await self._bus.drain(timeout=timeout_seconds)
        if not drained:
            _log.warning("coordinator.drain_incomplete", timeout_seconds=timeout_seconds)
        return drained

    def subscriber_errors(self) -> dict[str, int]:
        return dict(self._stats.errors_by_subscriber)

    def topic_counts(self, topics: Iterable[str] | None = None) -> dict[str, int]:
        if topics is None:
            return dict(self._stats.by_topic)
        return {topic: self._stats.by_topic.get(topic, 0) for topic in topics}


__all__ = ["BackgroundTask", "CoordinatorStats", "EventHandler", "RuntimeCoordinator"]
