"""Injectable clock.

Every subsystem that cares about time takes a :class:`Clock` rather than calling
``datetime.now()`` or ``asyncio.sleep`` directly. This exists for one concrete
reason: §64 requires 24 h / 72 h / 7 d endurance simulations that complete in
seconds. That is only achievable if the whole station — scheduler, playout,
rotation horizons, retention windows — can be driven by a virtual clock.

A secondary benefit is determinism: tests that assert on "within the last 4
tracks" or "not inside 24 hours" are otherwise flaky.

Rules
-----
* Always produce timezone-aware UTC datetimes. Naive datetimes are a bug and
  ruff's ``DTZ`` rules are enabled to catch them.
* ``monotonic()`` is for measuring durations; ``now()`` is for timestamps. Never
  use ``now()`` differences to measure elapsed time — it moves under NTP and DST.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import itertools
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

import structlog

_log = structlog.get_logger(__name__)

UTC = timezone.utc

#: Event-loop yields allowed before each virtual-time advance in :meth:`VirtualClock.run_to`.
#:
#: Gives tasks that were just woken a chance to finish their work and register their next
#: sleep, so the clock does not step over an interval one of them was about to claim. Each
#: yield costs ``real_yield_seconds``, so this trades a little wall time for correctness.
_SETTLE_YIELDS = 6


@runtime_checkable
class Clock(Protocol):
    """Time source abstraction."""

    def now(self) -> datetime:
        """Current wall-clock time, timezone-aware UTC."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary fixed origin; never moves backwards."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Suspend the caller for ``seconds`` of this clock's time."""
        ...

    def hold(self) -> AbstractContextManager[None]:
        """Declare that the caller is mid-cycle and time must not advance past it.

        Real time cannot be held, so :class:`SystemClock` returns a no-op. It exists for
        :class:`VirtualClock`, where a task that is *runnable* holds no sleep waiter — and a
        driver advancing virtual time can therefore step straight over an interval that task
        was about to claim. For the playout engine that interval is audio never written.
        """
        ...


class SystemClock:
    """Real time. The only clock used in development, simulation and production."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(tz=UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)

    def hold(self) -> AbstractContextManager[None]:
        """No-op: wall-clock time cannot be held."""
        return contextlib.nullcontext()


class VirtualClock:
    """Deterministic clock whose time only advances when work is waiting.

    ``sleep`` registers a waiter at a future virtual time and yields. Time jumps
    forward to the earliest waiter as soon as every task is blocked, so a
    simulated week elapses as fast as the CPU can run the logic.

    Deliberate design notes
    -----------------------
    * Waiters are ordered by ``(deadline, sequence)``. The monotonically
      increasing sequence makes wake-up order among equal deadlines
      **insertion-ordered and reproducible**, which matters because scheduler and
      playout frequently wake on the same tick and a reordering would make
      endurance results irreproducible.
    * ``advance`` drains waiters one deadline at a time and yields to the event
      loop between batches, so woken tasks actually get to run (and can register
      new sleeps) before virtual time moves again. Jumping straight to the final
      deadline would starve them.
    * This class is **not** thread-safe. It is used from a single event loop.

    ``real_yield_seconds`` — the one thing that is not obvious
    ---------------------------------------------------------
    A virtual clock accelerates **waiting**. It cannot accelerate **work**, and the
    difference bites as soon as a task does I/O that runs on a thread — which, in this
    station, means every database round trip (aiosqlite hands work to a thread pool).

    Such a task is *runnable*, not sleeping, while its thread is busy. ``asyncio.sleep(0)``
    yields the loop but does not let a worker thread finish, so a driver that advances virtual
    time using only zero-sleeps can lap a task that is waiting on real I/O — indefinitely, if
    some other task holds a far-future deadline that keeps the waiter heap non-empty.

    The symptom is subtle and was found by a test rather than by reasoning: a lease renewer
    that renews every 40 virtual seconds renewed exactly **once** across 150 virtual seconds,
    because its database write never got the wall time to complete. In a soak that would look
    like leases expiring under live generation.

    So ``real_yield_seconds`` is a small *real* sleep used wherever this class yields. Zero is
    correct for a simulation that is pure computation; anything driving tasks that touch the
    database should pass a small positive value. It is a cost — see
    :meth:`run_for` — so the figure wants to be as small as works, not generous.
    """

    __slots__ = (
        "_counter",
        "_hold_released",
        "_holds",
        "_real_yield",
        "_start",
        "_virtual",
        "_waiters",
    )

    def __init__(
        self, start: datetime | None = None, *, real_yield_seconds: float = 0.0
    ) -> None:
        if start is None:
            start = datetime(2026, 1, 1, tzinfo=UTC)
        if start.tzinfo is None:
            raise ValueError("VirtualClock start must be timezone-aware")
        if real_yield_seconds < 0:
            raise ValueError("real_yield_seconds must not be negative")
        self._start = start.astimezone(UTC)
        self._virtual = 0.0
        self._real_yield = real_yield_seconds
        self._waiters: list[tuple[float, int, asyncio.Future[None]]] = []
        self._counter = itertools.count()
        self._holds: dict[asyncio.Task[Any], int] = {}
        self._hold_released: asyncio.Event | None = None

    @property
    def real_yield_seconds(self) -> float:
        return self._real_yield

    @property
    def pending_holds(self) -> int:
        """How many *working* tasks have declared themselves mid-cycle.

        Excludes holders that are currently asleep on this clock — see :meth:`hold`.
        """
        return sum(self._holds.values())

    @contextlib.contextmanager
    def hold(self) -> Iterator[None]:
        """Stop :meth:`run_to` advancing while the caller is working.

        The mechanism that makes virtual time *faithful* rather than merely fast. A task
        between sleeps is runnable and holds no waiter, so a driver is free to advance past the
        moment that task was about to claim — and for the playout engine, skipped time is audio
        that was never written.

        Measured on a half-hour soak: audio coverage went from 97.22 % to 100.00 % once the
        playout engine held the clock, and the spurious Tier 3 emergency activation at startup
        disappeared with it — the station had not actually been failing to produce music, the
        clock had been running past the moment it was asked for. Every underrun counter read
        zero throughout, in both runs, which is why this needed a coverage metric to find.

        **A hold is suspended for the duration of any sleep the holder makes**, and that is what
        keeps the mechanism safe to use. The naive version — a flag held across the whole work
        cycle — deadlocks the moment the holder sleeps, because the sleep needs the clock to
        advance and the hold forbids it. The playout engine hits that immediately: its sink
        paces the broadcast by sleeping for each block's duration from *inside* the cycle.

        Suspension gives both halves the right meaning without the caller having to split them:

        - computing (runnable)  -> held, time waits for the work
        - sleeping (a waiter)   -> not held, time advances as it should

        Per-task, so one task's hold never blocks another's sleep. Nesting is counted.
        """
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - defensive; holds are for async callers
            yield
            return
        self._holds[task] = self._holds.get(task, 0) + 1
        try:
            yield
        finally:
            remaining = self._holds.get(task, 0) - 1
            if remaining > 0:
                self._holds[task] = remaining
            else:
                self._holds.pop(task, None)
            if not self._holds:
                self._wake_holders()

    def _wake_holders(self) -> None:
        """Tell a waiting :meth:`run_to` that the last hold has been released."""
        if self._hold_released is not None and not self._hold_released.is_set():
            self._hold_released.set()

    async def _await_hold_release(self, timeout_seconds: float) -> bool:
        """Wait for every hold to be released. ``False`` if it timed out.

        Event-driven rather than polled, and that distinction turned out to dominate the
        cost of every accelerated run. Polling meant one ``asyncio.sleep`` per pass, and on
        Windows the proactor loop's timer granularity is around 15 ms — so a 1 ms yield
        actually waited fifteen. A profile of a six-minute simulated broadcast put 70.7 of
        its 75.6 seconds inside ``GetQueuedCompletionStatus`` across 15 435 waits, against
        under two seconds of actual audio work.
        """
        event = self._hold_released
        if event is None:
            event = asyncio.Event()
            self._hold_released = event
        event.clear()
        if not self._holds:
            # Released between the check and the clear.
            return True
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return False
        return True

    async def _yield_to_loop(self) -> None:
        """Give other tasks a turn — and, if configured, real time to finish I/O.

        ``real_yield_seconds`` of zero yields without sleeping, which is nearly free; any
        positive value costs at least one timer tick of the host event loop, which on Windows
        is far more than the value requested. Prefer zero and let :meth:`hold` account for
        work that genuinely needs real time.
        """
        if self._real_yield <= 0:
            await asyncio.sleep(0)
            return
        await asyncio.sleep(self._real_yield)

    # -- Clock protocol ----------------------------------------------------

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self._virtual)

    def monotonic(self) -> float:
        return self._virtual

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            # Still yield, so a zero-sleep loop cannot starve the event loop.
            await asyncio.sleep(0)
            return
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        entry = (self._virtual + seconds, next(self._counter), future)
        heapq.heappush(self._waiters, entry)
        # Suspend this task's holds while it waits. A sleeping task is waiting *for* the clock,
        # so continuing to hold it would deadlock — see :meth:`hold`.
        task = asyncio.current_task()
        suspended = self._holds.pop(task, 0) if task is not None else 0
        try:
            await future
        except asyncio.CancelledError:
            # Drop the waiter on cancellation. Without this it stays in the heap forever:
            # ``pending_waiters`` is then permanently wrong, ``advance_sync`` refuses to run
            # for the rest of the process, and over a long run — where cancelling a sleeping
            # task is routine, as every generation timeout and shutdown does it — the heap
            # grows without bound.
            with contextlib.suppress(ValueError):
                self._waiters.remove(entry)
                heapq.heapify(self._waiters)
            raise
        finally:
            if suspended and task is not None:
                self._holds[task] = self._holds.get(task, 0) + suspended

    # -- virtual-time control ---------------------------------------------

    @property
    def pending_waiters(self) -> int:
        """How many tasks are currently sleeping on this clock."""
        return len(self._waiters)

    @property
    def next_deadline(self) -> float | None:
        """Virtual monotonic time of the earliest waiter, or ``None`` if idle."""
        return self._waiters[0][0] if self._waiters else None

    def advance_sync(self, seconds: float) -> None:
        """Advance virtual time without an event loop.

        For fully synchronous drivers — the ``tradefix market-sim`` report, offline
        analysis, batch replays — where nothing is ever awaiting this clock, so there
        are no waiters to wake. Raises if anyone *is* waiting, because silently
        skipping past a sleeping task would make a simulation quietly wrong rather
        than loudly broken.

        Async callers want :meth:`advance`.
        """
        if seconds < 0:
            raise ValueError("cannot advance a clock backwards")
        if self._waiters:
            raise RuntimeError(
                f"advance_sync called with {len(self._waiters)} task(s) sleeping on "
                "this clock; use the async advance() so they are woken in order"
            )
        self._virtual += seconds

    async def advance(self, seconds: float) -> None:
        """Advance virtual time by ``seconds``, waking waiters in order."""
        if seconds < 0:
            raise ValueError("cannot advance a clock backwards")
        target = self._virtual + seconds
        while self._waiters and self._waiters[0][0] <= target:
            deadline = self._waiters[0][0]
            self._virtual = deadline
            # Wake every waiter sharing this exact deadline, in insertion order.
            batch: list[asyncio.Future[None]] = []
            while self._waiters and self._waiters[0][0] <= deadline:
                _, _, future = heapq.heappop(self._waiters)
                if not future.done():
                    batch.append(future)
            for future in batch:
                future.set_result(None)
            # Let the woken tasks run before time moves again.
            await self._yield_to_loop()
        self._virtual = target
        await self._yield_to_loop()

    async def advance_to_next(self) -> float | None:
        """Jump to the earliest waiter. Returns the virtual time reached.

        Returns ``None`` when nothing is sleeping, which is the signal that a
        simulation has quiesced.
        """
        deadline = self.next_deadline
        if deadline is None:
            return None
        await self.advance(max(0.0, deadline - self._virtual))
        return self._virtual

    async def run_for(
        self,
        seconds: float,
        *,
        max_steps: int = 5_000_000,
        settle_passes: int = 8,
    ) -> None:
        """Run the simulation forward by ``seconds`` of virtual time.

        Unlike :meth:`advance` this stops early if the system genuinely quiesces, and guards
        against a runaway zero-delay loop via ``max_steps`` so a bug produces a clear failure
        instead of a hang.

        ``settle_passes`` is the subtle part. "Nothing is sleeping" does **not** mean "nothing
        is happening": a task that has just woken and is doing real asynchronous work — a
        database round trip, a file write — is runnable rather than sleeping, and will register
        its next sleep shortly. An earlier version jumped straight to the target in that state,
        which fast-forwarded past the work. In a station where every state change is a database
        round trip that is not an edge case, it is the normal condition, and it silently skipped
        lease renewals during an accelerated run.

        So before concluding the system is idle, the loop yields to the event loop a bounded
        number of times and re-checks. Bounded rather than unbounded because a genuinely idle
        simulation must still terminate.
        """
        target = self._virtual + seconds
        steps = 0
        while self._virtual < target:
            steps += 1
            if steps > max_steps:
                raise RuntimeError(
                    f"VirtualClock.run_for exceeded {max_steps} steps at "
                    f"t={self._virtual:.3f}s; suspect a zero-delay loop"
                )
            deadline = await self._settled_deadline(settle_passes)
            if deadline is None or deadline > target:
                self._virtual = target
                await self._yield_to_loop()
                return
            await self.advance_to_next()

    async def run_to(
        self,
        target: float,
        *,
        idle_passes: int = 64,
        max_steps: int = 5_000_000,
        hold_timeout_seconds: float = 30.0,
    ) -> None:
        """Advance to ``target``, **never past a waiter**.

        The difference from :meth:`run_for` matters for anything driving real tasks. ``run_for``
        jumps straight to its target once nothing is momentarily sleeping, which is the right
        behaviour for a quiesced simulation and the wrong behaviour for a station: a task that
        is mid-database-call is runnable rather than sleeping, and jumping past it skips the
        work it was about to schedule.

        The symptom was concrete. A playout engine writing one 10-second block per sleep
        produced 41 blocks across 3 600 virtual seconds instead of 360, because most advances
        happened while it was between sleeps. Every lost block is audio that never reached the
        sink.

        So this advances only to actual deadlines, and when nothing is waiting it yields —
        ``idle_passes`` times — before concluding the system is genuinely idle and jumping. The
        allowance is generous because a yield is cheap and a lost block is not.

        A task can also protect itself explicitly with :meth:`hold`, which this method honours
        before looking at deadlines at all. That closes the same gap from the other side, for
        the case where yielding a fixed number of times is not enough — see :meth:`hold`.
        """
        if target < self._virtual:
            raise ValueError("cannot run a clock backwards")
        steps = 0
        idle = 0
        while self._virtual < target:
            steps += 1
            if steps > max_steps:
                raise RuntimeError(
                    f"VirtualClock.run_to exceeded {max_steps} steps at "
                    f"t={self._virtual:.3f}s; suspect a zero-delay loop"
                )
            if self._holds:
                # Somebody is mid-cycle. Wait for them rather than stepping over their work.
                #
                # Bounded in *real* seconds rather than in yields, because what we are waiting
                # for is real work — a disk read, a render, a database round trip — and a yield
                # budget would be denominated in the wrong unit.
                if not await self._await_hold_release(hold_timeout_seconds):
                    _log.warning(
                        "virtual_clock.hold_stuck",
                        holds=self.pending_holds,
                        virtual=round(self._virtual, 3),
                        waited_seconds=round(hold_timeout_seconds, 1),
                        detail="advancing anyway; a hold was never released",
                    )
                continue
            deadline = self.next_deadline
            if deadline is None:
                idle += 1
                if idle > idle_passes:
                    self._virtual = target
                    await self._yield_to_loop()
                    return
                await self._yield_to_loop()
                continue
            idle = 0
            if deadline > target:
                self._virtual = target
                await self._yield_to_loop()
                return
            # Let runnable tasks register their next sleep before time moves.
            #
            # A task that has just been woken is runnable, not sleeping, and holds no waiter
            # while it does its work. If the clock advances past the deadline that task was
            # about to claim, the interval is simply skipped — and for a playout engine, a
            # skipped interval is audio that was never written. Measured: coverage rose from
            # 88.6 % to near-complete once the loop settled before each advance.
            for _ in range(_SETTLE_YIELDS):
                await self._yield_to_loop()
                earliest = self.next_deadline
                if earliest is not None and earliest < deadline:
                    deadline = earliest
                    if deadline > target:
                        self._virtual = target
                        await self._yield_to_loop()
                        return
            await self.advance_to_next()

    async def _settled_deadline(self, passes: int) -> float | None:
        """The next deadline, after giving runnable tasks a chance to register one."""
        for _ in range(max(1, passes)):
            deadline = self.next_deadline
            if deadline is not None:
                return deadline
            await self._yield_to_loop()
        return self.next_deadline


__all__ = ["UTC", "Clock", "SystemClock", "VirtualClock"]
