"""Injectable clock, especially the virtual one (§64 prerequisite).

The virtual clock is what makes §64's 24 h / 72 h / 7 d endurance simulations
possible at all — they must complete in seconds, which requires the whole station
to be driveable by simulated time. These tests pin the two properties endurance
runs depend on: **wake-up order is reproducible**, and **a runaway zero-delay loop
fails loudly instead of hanging**.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from tradefix_radio.core.clock import UTC, Clock, SystemClock, VirtualClock

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------- protocol


def test_both_clocks_satisfy_the_protocol() -> None:
    assert isinstance(SystemClock(), Clock)
    assert isinstance(VirtualClock(), Clock)


def test_system_clock_returns_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_system_clock_monotonic_never_goes_backwards() -> None:
    clock = SystemClock()
    readings = [clock.monotonic() for _ in range(50)]
    assert readings == sorted(readings)


async def test_system_clock_sleep_handles_zero_and_negative() -> None:
    """A zero or negative sleep must return promptly, not raise."""
    clock = SystemClock()
    await asyncio.wait_for(clock.sleep(0), timeout=1.0)
    await asyncio.wait_for(clock.sleep(-5), timeout=1.0)


# ---------------------------------------------------------------- virtual basics


def test_virtual_clock_starts_at_the_requested_instant() -> None:
    clock = VirtualClock(start=START)
    assert clock.now() == START
    assert clock.monotonic() == 0.0


def test_virtual_clock_rejects_a_naive_start() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        VirtualClock(start=datetime(2026, 1, 1))


def test_virtual_clock_default_start_is_aware() -> None:
    assert VirtualClock().now().tzinfo is not None


async def test_advance_moves_wall_clock_and_monotonic_together() -> None:
    clock = VirtualClock(start=START)
    await clock.advance(3600)
    assert clock.monotonic() == pytest.approx(3600.0)
    assert clock.now() == START + timedelta(hours=1)


async def test_advance_backwards_is_rejected() -> None:
    clock = VirtualClock(start=START)
    with pytest.raises(ValueError, match="backwards"):
        await clock.advance(-1)


async def test_sleep_blocks_until_time_advances() -> None:
    clock = VirtualClock(start=START)
    done = asyncio.Event()

    async def sleeper() -> None:
        await clock.sleep(60)
        done.set()

    task = asyncio.create_task(sleeper())
    await asyncio.sleep(0)
    assert not done.is_set()
    assert clock.pending_waiters == 1

    await clock.advance(59)
    assert not done.is_set()

    await clock.advance(1)
    await asyncio.wait_for(task, timeout=1.0)
    assert done.is_set()
    assert clock.pending_waiters == 0


async def test_zero_sleep_yields_without_requiring_advance() -> None:
    """Otherwise a polling loop with no delay would deadlock the simulation."""
    clock = VirtualClock(start=START)
    await asyncio.wait_for(clock.sleep(0), timeout=1.0)
    assert clock.pending_waiters == 0


async def test_waiters_wake_in_deadline_order() -> None:
    clock = VirtualClock(start=START)
    order: list[str] = []

    async def sleeper(name: str, seconds: float) -> None:
        await clock.sleep(seconds)
        order.append(name)

    tasks = [
        asyncio.create_task(sleeper("third", 30)),
        asyncio.create_task(sleeper("first", 10)),
        asyncio.create_task(sleeper("second", 20)),
    ]
    await asyncio.sleep(0)
    await clock.advance(30)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=1.0)
    assert order == ["first", "second", "third"]


async def test_equal_deadlines_wake_in_insertion_order() -> None:
    """Reproducibility: scheduler and playout often wake on the same tick.

    Without a stable tie-break, two endurance runs of the same scenario could
    produce different results, which would make §64's findings unactionable.
    """
    clock = VirtualClock(start=START)
    order: list[int] = []

    async def sleeper(index: int) -> None:
        await clock.sleep(10)
        order.append(index)

    tasks = [asyncio.create_task(sleeper(i)) for i in range(6)]
    await asyncio.sleep(0)
    await clock.advance(10)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=1.0)
    assert order == [0, 1, 2, 3, 4, 5]


async def test_next_deadline_reports_the_earliest_waiter() -> None:
    clock = VirtualClock(start=START)
    assert clock.next_deadline is None
    tasks = [
        asyncio.create_task(clock.sleep(50)),
        asyncio.create_task(clock.sleep(5)),
    ]
    await asyncio.sleep(0)
    assert clock.next_deadline == pytest.approx(5.0)
    await clock.advance(50)
    await asyncio.gather(*tasks)


async def test_advance_to_next_jumps_exactly_to_the_waiter() -> None:
    clock = VirtualClock(start=START)
    task = asyncio.create_task(clock.sleep(123.5))
    await asyncio.sleep(0)
    reached = await clock.advance_to_next()
    assert reached == pytest.approx(123.5)
    await asyncio.wait_for(task, timeout=1.0)


async def test_advance_to_next_returns_none_when_quiescent() -> None:
    """The signal that a simulation has finished all its work."""
    clock = VirtualClock(start=START)
    assert await clock.advance_to_next() is None


async def test_woken_tasks_can_register_new_sleeps_before_time_moves_again() -> None:
    """A periodic loop must actually tick, not be skipped over.

    If ``advance`` jumped straight to the target, a task sleeping 1 s in a loop
    would run once instead of 3600 times in an hour — and every rate-based metric
    in the endurance report would be wrong.
    """
    clock = VirtualClock(start=START)
    ticks = 0

    async def periodic() -> None:
        nonlocal ticks
        for _ in range(10):
            await clock.sleep(1)
            ticks += 1

    task = asyncio.create_task(periodic())
    await asyncio.sleep(0)
    await clock.advance(10)
    await asyncio.wait_for(task, timeout=2.0)
    assert ticks == 10


async def test_run_for_executes_a_long_simulated_span_quickly() -> None:
    """One simulated hour of 1 s ticks, in well under a second of real time."""
    clock = VirtualClock(start=START)
    ticks = 0
    stop = False

    async def heartbeat() -> None:
        nonlocal ticks
        while not stop:
            await clock.sleep(1.0)
            ticks += 1

    task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    await asyncio.wait_for(clock.run_for(3600), timeout=30.0)
    stop = True
    assert ticks >= 3600
    assert clock.monotonic() >= 3600
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_run_for_reaches_the_target_when_nothing_is_waiting() -> None:
    clock = VirtualClock(start=START)
    await clock.run_for(86_400)
    assert clock.monotonic() == pytest.approx(86_400.0)
    assert clock.now() == START + timedelta(days=1)


async def test_run_for_stops_at_the_target_not_past_a_later_waiter() -> None:
    clock = VirtualClock(start=START)
    task = asyncio.create_task(clock.sleep(1000))
    await asyncio.sleep(0)
    await clock.run_for(10)
    assert clock.monotonic() == pytest.approx(10.0)
    assert clock.pending_waiters == 1
    await clock.advance(1000)
    await asyncio.wait_for(task, timeout=1.0)


async def test_run_for_guards_against_a_runaway_zero_delay_loop() -> None:
    """A bug must surface as a clear error, never as a hang.

    An endurance run that hangs gives no information; one that raises names the
    virtual time at which the loop misbehaved.
    """
    clock = VirtualClock(start=START)
    stop = False

    async def busy() -> None:
        # Sleeps a positive but vanishing amount, so time barely advances.
        while not stop:
            await clock.sleep(1e-9)

    task = asyncio.create_task(busy())
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="zero-delay loop"):
        await clock.run_for(10, max_steps=200)
    stop = True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_virtual_time_does_not_advance_on_its_own() -> None:
    """Determinism: only explicit advancement moves the clock."""
    clock = VirtualClock(start=START)
    before = clock.monotonic()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert clock.monotonic() == before


async def test_a_week_of_simulated_time_is_representable() -> None:
    """§64 asks for a 7-day run; confirm no precision or overflow surprise."""
    clock = VirtualClock(start=START)
    await clock.advance(7 * 86_400)
    assert clock.now() == START + timedelta(days=7)
    assert clock.monotonic() == pytest.approx(604_800.0)


def test_utc_constant_is_exported() -> None:
    assert UTC is timezone.utc
