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
import heapq
import itertools
import time
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

UTC = timezone.utc


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
    """

    __slots__ = ("_counter", "_start", "_virtual", "_waiters")

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            start = datetime(2026, 1, 1, tzinfo=UTC)
        if start.tzinfo is None:
            raise ValueError("VirtualClock start must be timezone-aware")
        self._start = start.astimezone(UTC)
        self._virtual = 0.0
        self._waiters: list[tuple[float, int, asyncio.Future[None]]] = []
        self._counter = itertools.count()

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
        heapq.heappush(
            self._waiters,
            (self._virtual + seconds, next(self._counter), future),
        )
        await future

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
            await asyncio.sleep(0)
        self._virtual = target
        await asyncio.sleep(0)

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

    async def run_for(self, seconds: float, *, max_steps: int = 5_000_000) -> None:
        """Run the simulation forward by ``seconds`` of virtual time.

        Unlike :meth:`advance` this stops early if the system quiesces (nothing
        sleeping), and guards against a runaway zero-delay loop via ``max_steps``
        so a bug produces a clear failure instead of a hang.
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
            deadline = self.next_deadline
            if deadline is None:
                # Nothing is waiting: jump to the end rather than spinning.
                self._virtual = target
                await asyncio.sleep(0)
                return
            if deadline > target:
                self._virtual = target
                await asyncio.sleep(0)
                return
            await self.advance_to_next()


__all__ = ["UTC", "Clock", "SystemClock", "VirtualClock"]
