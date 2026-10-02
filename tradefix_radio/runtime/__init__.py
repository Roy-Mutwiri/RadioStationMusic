"""Runtime wiring: the event bus façade and subsystem lifecycle (§91, milestone 4.3).

The station is five subsystems — market, director, generation, queue, playout — and the thing
that makes them a station rather than a tangle is that **none of them holds a reference to
another**. They publish facts and subscribe to facts, and
:class:`~tradefix_radio.runtime.coordinator.RuntimeCoordinator` owns the wiring and the
background tasks.

The coordinator contains no policy. A decision that ends up here instead of in a subsystem is a
design error, because a decision in the wiring layer is one nobody can unit-test.
"""

from tradefix_radio.runtime.coordinator import (
    BackgroundTask,
    CoordinatorStats,
    EventHandler,
    RuntimeCoordinator,
)

__all__ = [
    "BackgroundTask",
    "CoordinatorStats",
    "EventHandler",
    "RuntimeCoordinator",
]
