"""Health registry (§35, §73).

A check is an async callable returning :class:`ComponentHealthV1`. The registry's
job is the boring-but-essential part: run them all concurrently, bound each by a
timeout, never let one failing check hide the others, and aggregate to a single
status.

Three decisions worth stating:

**A check that hangs is a failing check.** Each is wrapped in
``asyncio.wait_for``. A probe to a wedged OBS or a locked database would otherwise
stall the whole health endpoint, which is precisely when an operator needs it.

**A check that raises is reported, not propagated.** The registry converts an
exception into a CRITICAL component with the exception text as ``detail``. §86
forbids suppressing errors — nothing is suppressed, it is converted into the
observable form the caller asked for, and logged.

**Overall status is the worst required component**, with optional components able
to degrade but not block. Without that distinction, a station in development with
no GPU and no OBS would report CRITICAL forever and the signal would be useless.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import structlog

from tradefix_radio.contracts.enums import HealthStatus
from tradefix_radio.contracts.health import ComponentHealthV1, SystemHealthV1
from tradefix_radio.core.clock import Clock, SystemClock

_log = structlog.get_logger(__name__)

HealthCheck = Callable[[], Awaitable[ComponentHealthV1]]

#: Checks slower than this are treated as failed. Generous, because a cold
#: database connection or a GPU query can legitimately take a second or two.
DEFAULT_CHECK_TIMEOUT_SECONDS = 10.0


class HealthRegistry:
    """Collects health checks and aggregates their results."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        timeout_seconds: float = DEFAULT_CHECK_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._clock: Clock = clock or SystemClock()
        self._timeout = timeout_seconds
        self._checks: dict[str, tuple[HealthCheck, bool]] = {}
        self._started_monotonic = self._clock.monotonic()
        self._last: dict[str, ComponentHealthV1] = {}

    # -- registration ------------------------------------------------------

    def register(self, name: str, check: HealthCheck, *, required: bool = True) -> None:
        """Add a check.

        ``required`` is recorded here rather than inside the check so the same
        check function can be required in production and optional in development
        — which is exactly the GPU and OBS situation.
        """
        if not name:
            raise ValueError("health check name must not be empty")
        if name in self._checks:
            raise ValueError(f"health check {name!r} is already registered")
        self._checks[name] = (check, required)

    def unregister(self, name: str) -> None:
        self._checks.pop(name, None)
        self._last.pop(name, None)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._checks)

    # -- execution ---------------------------------------------------------

    async def check_one(self, name: str) -> ComponentHealthV1:
        """Run a single check, converting timeouts and exceptions to results."""
        entry = self._checks.get(name)
        if entry is None:
            raise KeyError(f"no health check registered as {name!r}")
        check, required = entry
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(check(), timeout=self._timeout)
        except asyncio.TimeoutError:
            elapsed_ms = (time.perf_counter() - started) * 1000
            _log.warning("health.check_timeout", component=name, timeout=self._timeout)
            result = ComponentHealthV1(
                name=name,
                status=HealthStatus.CRITICAL,
                detail=f"check did not complete within {self._timeout:.0f}s",
                remediation=(
                    "The component is unresponsive rather than merely unhealthy. "
                    "Check whether its process is alive and not blocked on I/O."
                ),
                required=required,
                latency_ms=elapsed_ms,
                checked_at=self._clock.now(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - converted to an observable result
            # Not suppression: the failure is logged with a traceback and surfaced
            # as CRITICAL. A health probe must answer even when the thing it
            # probes explodes.
            elapsed_ms = (time.perf_counter() - started) * 1000
            _log.exception("health.check_raised", component=name)
            result = ComponentHealthV1(
                name=name,
                status=HealthStatus.CRITICAL,
                detail=f"{type(exc).__name__}: {exc}",
                remediation=getattr(exc, "remediation", "") or "See logs for the traceback.",
                required=required,
                latency_ms=elapsed_ms,
                checked_at=self._clock.now(),
            )
        else:
            if result.required != required:
                # The registry owns `required`; a check returning the wrong value
                # would make an optional dependency block startup.
                result = result.model_copy(update={"required": required})
            if result.latency_ms is None:
                result = result.model_copy(
                    update={"latency_ms": (time.perf_counter() - started) * 1000}
                )

        self._last[name] = result
        return result

    async def check_all(self) -> SystemHealthV1:
        """Run every check concurrently and aggregate."""
        names = tuple(self._checks)
        results: list[ComponentHealthV1]
        if names:
            results = list(
                await asyncio.gather(*(self.check_one(name) for name in names))
            )
        else:
            results = []
        return self._aggregate(results)

    def last_known(self) -> SystemHealthV1:
        """Aggregate of the most recent result per component, without re-running.

        Used by high-frequency consumers (the WebSocket push, the §49 page) so a
        one-second UI refresh does not re-probe the GPU and the database every
        tick.
        """
        return self._aggregate([self._last[name] for name in self._checks if name in self._last])

    def _aggregate(self, results: list[ComponentHealthV1]) -> SystemHealthV1:
        overall = HealthStatus.HEALTHY
        for result in results:
            # Optional components cannot push the station past DEGRADED: a missing
            # GPU in development is a fact, not an emergency.
            effective = result.status
            if not result.required and effective is HealthStatus.CRITICAL:
                effective = HealthStatus.DEGRADED
            if effective.severity > overall.severity:
                overall = effective
        if not results:
            overall = HealthStatus.UNKNOWN
        return SystemHealthV1(
            status=overall,
            components=tuple(results),
            checked_at=self._clock.now(),
            uptime_seconds=max(0.0, self._clock.monotonic() - self._started_monotonic),
        )


def healthy(
    name: str,
    *,
    clock: Clock,
    detail: str = "",
    required: bool = True,
    latency_ms: float | None = None,
    measurements: dict[str, float] | None = None,
) -> ComponentHealthV1:
    """Convenience constructor for a passing check.

    ``measurements`` is an explicit dict rather than ``**kwargs``: with kwargs, a
    measurement named ``required`` or ``detail`` would silently bind to the
    keyword parameter instead, which is a real hazard given the free-form
    measurement names the §49 page uses.
    """
    return ComponentHealthV1(
        name=name,
        status=HealthStatus.HEALTHY,
        detail=detail,
        required=required,
        latency_ms=latency_ms,
        checked_at=clock.now(),
        measurements=dict(measurements or {}),
    )


def unhealthy(
    name: str,
    status: HealthStatus,
    detail: str,
    *,
    clock: Clock,
    remediation: str = "",
    required: bool = True,
    latency_ms: float | None = None,
    measurements: dict[str, float] | None = None,
) -> ComponentHealthV1:
    """Convenience constructor for a failing or degraded check."""
    if status is HealthStatus.HEALTHY:
        raise ValueError("use healthy() for a HEALTHY result")
    return ComponentHealthV1(
        name=name,
        status=status,
        detail=detail,
        remediation=remediation,
        required=required,
        latency_ms=latency_ms,
        checked_at=clock.now(),
        measurements=dict(measurements or {}),
    )


__all__ = [
    "DEFAULT_CHECK_TIMEOUT_SECONDS",
    "HealthCheck",
    "HealthRegistry",
    "healthy",
    "unhealthy",
]
