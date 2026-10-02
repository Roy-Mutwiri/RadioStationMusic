"""In-process asynchronous event bus (§91).

Why a bus at all: §91 explicitly asks to avoid "spaghetti cross-service calls".
The market engine should not know that OBS exists; the playout engine should not
call the metrics collector. They publish facts; interested parties subscribe.

Design decisions
----------------
**Per-subscription worker task.** Each subscription owns a queue and a task.
``publish`` only enqueues, so a slow or crashing subscriber can never block the
publisher or any other subscriber. This is the §91 requirement that matters most
operationally: a wedged OBS client must not stall the scheduler.

**Explicit backpressure, never unbounded.** A 24/7 process cannot have an
unbounded queue — that is a memory leak with extra steps (§64 checks for exactly
this). Each subscription declares an overflow policy and drops are *counted and
logged*, never silent.

**Handler exceptions are contained.** A raising handler is logged with full
context and the worker continues with the next event. §86 forbids blind excepts
without logging; this is the one place a broad catch is correct, and it logs.

**Wildcards are prefix-only.** ``market.*`` and ``*``. Deliberately not a regex
or glob engine: topic routing is on the hot path, and prefix matching is
predictable. Keeping it dumb also keeps the future Redis/NATS adapter honest,
since both support prefix/subject routing natively.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import structlog

_log = structlog.get_logger(__name__)


@runtime_checkable
class Event(Protocol):
    """Anything publishable on the bus.

    Concrete events are declared in :mod:`tradefix_radio.contracts.events`. The
    protocol lives here so ``core`` stays dependency-free.
    """

    @property
    def topic(self) -> str:
        """Dot-separated routing key, e.g. ``market.state_changed``."""
        ...


EventHandler = Callable[[Any], Awaitable[None]]


class OverflowPolicy(str, enum.Enum):
    """What to do when a subscriber's queue is full."""

    DROP_OLDEST = "drop_oldest"
    """Discard the stale event. Correct for state snapshots where only the most
    recent value matters (market state, now-playing)."""

    DROP_NEWEST = "drop_newest"
    """Discard the arriving event. Correct for append-only logs where earlier
    entries carry more context than the newest one."""

    BLOCK = "block"
    """Apply real backpressure by awaiting space. Only safe for subscribers that
    are guaranteed to drain, because it stalls the publisher."""


@dataclass
class SubscriptionStats:
    """Observable per-subscription counters, surfaced by §56 metrics."""

    delivered: int = 0
    dropped: int = 0
    failed: int = 0
    max_queue_depth: int = 0


@dataclass
class Subscription:
    """A live subscription. Returned by :meth:`EventBus.subscribe`."""

    pattern: str
    handler: EventHandler
    name: str
    queue: asyncio.Queue[Any]
    overflow: OverflowPolicy
    stats: SubscriptionStats = field(default_factory=SubscriptionStats)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)

    def matches(self, topic: str) -> bool:
        """Prefix-wildcard topic match."""
        if self.pattern == "*":
            return True
        if self.pattern.endswith(".*"):
            prefix = self.pattern[:-1]  # keep the trailing dot
            return topic.startswith(prefix)
        return self.pattern == topic


class EventBus:
    """Asynchronous fan-out event bus.

    Usage::

        bus = EventBus()
        sub = bus.subscribe("market.*", on_market, name="obs-overlay")
        await bus.publish(MarketStateChanged(...))
        ...
        await bus.aclose()
    """

    def __init__(self, *, default_queue_size: int = 256) -> None:
        if default_queue_size < 1:
            raise ValueError("default_queue_size must be >= 1")
        self._default_queue_size = default_queue_size
        self._subscriptions: list[Subscription] = []
        self._closed = False
        self._published = 0

    # -- introspection -----------------------------------------------------

    @property
    def published_count(self) -> int:
        return self._published

    @property
    def subscriptions(self) -> tuple[Subscription, ...]:
        return tuple(self._subscriptions)

    def stats(self) -> dict[str, SubscriptionStats]:
        """Snapshot of per-subscriber counters keyed by subscription name."""
        return {s.name: s.stats for s in self._subscriptions}

    # -- subscription ------------------------------------------------------

    def subscribe(
        self,
        pattern: str,
        handler: EventHandler,
        *,
        name: str | None = None,
        queue_size: int | None = None,
        overflow: OverflowPolicy = OverflowPolicy.DROP_OLDEST,
    ) -> Subscription:
        """Register ``handler`` for topics matching ``pattern``.

        The handler runs in its own task; it is never invoked from the
        publisher's call stack.
        """
        if self._closed:
            raise RuntimeError("cannot subscribe to a closed EventBus")
        if not pattern:
            raise ValueError("pattern must not be empty")
        qualname = getattr(handler, "__qualname__", None)
        derived_name = str(qualname) if isinstance(qualname, str) else repr(handler)
        subscription = Subscription(
            pattern=pattern,
            handler=handler,
            name=name or derived_name,
            queue=asyncio.Queue(maxsize=queue_size or self._default_queue_size),
            overflow=overflow,
        )
        subscription._task = asyncio.create_task(
            self._run_subscription(subscription),
            name=f"eventbus:{subscription.name}",
        )
        self._subscriptions.append(subscription)
        _log.debug("event_bus.subscribed", pattern=pattern, subscriber=subscription.name)
        return subscription

    async def unsubscribe(self, subscription: Subscription) -> None:
        """Remove a subscription and stop its worker task."""
        with contextlib.suppress(ValueError):
            self._subscriptions.remove(subscription)
        task = subscription._task
        subscription._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        _log.debug("event_bus.unsubscribed", subscriber=subscription.name)

    # -- publication -------------------------------------------------------

    async def publish(self, event: Event) -> None:
        """Deliver ``event`` to every matching subscriber.

        Returns as soon as the event is enqueued everywhere. Handlers run
        concurrently afterwards, so callers must not assume the event has been
        *processed* when this returns — only that it has been accepted.
        """
        if self._closed:
            raise RuntimeError("cannot publish to a closed EventBus")
        topic = event.topic
        self._published += 1
        for subscription in list(self._subscriptions):
            if subscription.matches(topic):
                await self._enqueue(subscription, event, topic)

    async def _enqueue(self, subscription: Subscription, event: Event, topic: str) -> None:
        queue = subscription.queue
        subscription.stats.max_queue_depth = max(
            subscription.stats.max_queue_depth, queue.qsize()
        )
        if subscription.overflow is OverflowPolicy.BLOCK:
            await queue.put(event)
            return
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            if subscription.overflow is OverflowPolicy.DROP_NEWEST:
                subscription.stats.dropped += 1
                _log.warning(
                    "event_bus.dropped_newest",
                    subscriber=subscription.name,
                    event_topic=topic,
                    queue_size=queue.maxsize,
                    dropped_total=subscription.stats.dropped,
                )
                return
            # DROP_OLDEST: evict the head, then enqueue. The get_nowait cannot
            # fail because the queue is full, so at least one item is present.
            queue.get_nowait()
            queue.task_done()
            subscription.stats.dropped += 1
            _log.warning(
                "event_bus.dropped_oldest",
                subscriber=subscription.name,
                event_topic=topic,
                queue_size=queue.maxsize,
                dropped_total=subscription.stats.dropped,
            )
            queue.put_nowait(event)

    # -- worker ------------------------------------------------------------

    async def _run_subscription(self, subscription: Subscription) -> None:
        """Drain one subscription's queue forever, isolating handler failures."""
        while True:
            event = await subscription.queue.get()
            try:
                await subscription.handler(event)
                subscription.stats.delivered += 1
            except asyncio.CancelledError:
                subscription.queue.task_done()
                raise
            except Exception:  # noqa: BLE001 - see comment below
                # Deliberate broad catch, and the only sanctioned one in the
                # codebase: a subscriber bug must not silently kill its worker or
                # affect other subscribers. Nothing is suppressed — the exception
                # is logged with traceback and counted, satisfying §86.
                subscription.stats.failed += 1
                _log.exception(
                    "event_bus.handler_failed",
                    subscriber=subscription.name,
                    event_topic=getattr(event, "topic", "<unknown>"),
                    failed_total=subscription.stats.failed,
                )
            finally:
                subscription.queue.task_done()

    # -- lifecycle ---------------------------------------------------------

    async def drain(self, timeout: float = 5.0) -> bool:
        """Wait until every subscriber queue is empty.

        Primarily a test affordance — production code should not depend on
        delivery completion — and used by graceful shutdown (§74) to flush
        pending persistence writes. Returns ``False`` on timeout.
        """
        try:
            await asyncio.wait_for(
                asyncio.gather(*(s.queue.join() for s in list(self._subscriptions))),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            _log.warning("event_bus.drain_timeout", timeout=timeout)
            return False
        return True

    async def aclose(self, *, drain_timeout: float = 5.0) -> None:
        """Flush what we can, then stop all workers (§74)."""
        if self._closed:
            return
        self._closed = True
        await self.drain(timeout=drain_timeout)
        tasks: list[asyncio.Task[None]] = []
        for subscription in list(self._subscriptions):
            task = subscription._task
            subscription._task = None
            if task is not None:
                task.cancel()
                tasks.append(task)
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._subscriptions.clear()
        _log.debug("event_bus.closed", published_total=self._published)


__all__ = [
    "Event",
    "EventBus",
    "EventHandler",
    "OverflowPolicy",
    "Subscription",
    "SubscriptionStats",
]
