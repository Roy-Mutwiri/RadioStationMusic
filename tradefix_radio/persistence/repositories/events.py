"""System, health and metric event repositories (§37, §55, §56).

These tables are the queryable subset of observability — what the UI shows and
what a post-mortem reads. Log files remain the full record and rotate (§55).

All three are append-mostly and grow for the life of the station, so each has a
``prune_before`` method. §64 checks database growth explicitly; a table that only
ever grows is a slow-motion disk failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select

from tradefix_radio.contracts.enums import HealthStatus
from tradefix_radio.persistence.models import HealthEvent, MetricSample, SystemEvent
from tradefix_radio.persistence.repositories.base import Repository, affected_rows


class SystemEventRepository(Repository):
    """Durable record of notable station events."""

    async def record(
        self,
        *,
        now: datetime,
        service: str,
        level: str,
        event: str,
        track_id: str | None = None,
        job_id: str | None = None,
        market_regime: str | None = None,
        duration_seconds: float | None = None,
        error: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> SystemEvent:
        row = SystemEvent(
            at=now,
            service=service,
            level=level,
            event=event,
            track_id=track_id,
            job_id=job_id,
            market_regime=market_regime,
            duration_seconds=duration_seconds,
            # Truncate rather than reject: an over-long traceback must not be the
            # reason a failure goes unrecorded.
            error=error[:2000] if error else None,
            detail=detail,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def recent(
        self,
        limit: int = 100,
        *,
        level: str | None = None,
        track_id: str | None = None,
    ) -> list[SystemEvent]:
        statement = select(SystemEvent).order_by(SystemEvent.at.desc()).limit(limit)
        if level is not None:
            statement = statement.where(SystemEvent.level == level)
        if track_id is not None:
            statement = statement.where(SystemEvent.track_id == track_id)
        result = await self._session.execute(statement)
        return list(result.scalars())

    async def count_since(self, event: str, since: datetime) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(SystemEvent)
            .where(SystemEvent.event == event, SystemEvent.at >= since)
        )
        return int(result.scalar_one() or 0)

    async def prune_before(self, cutoff: datetime, *, keep_errors: bool = True) -> int:
        """Delete old rows, returning how many went.

        ``keep_errors`` defaults to true: informational chatter is what fills the
        table, while the error rows are exactly what a post-mortem needs weeks
        later. Pruning both equally would optimise for space at the cost of the
        table's only real purpose.
        """
        statement = delete(SystemEvent).where(SystemEvent.at < cutoff)
        if keep_errors:
            statement = statement.where(SystemEvent.level.notin_(["ERROR", "CRITICAL"]))
        result = await self._session.execute(statement)
        return affected_rows(result)


class HealthEventRepository(Repository):
    """Health transition history (§35)."""

    async def record(
        self,
        *,
        now: datetime,
        component: str,
        status: HealthStatus,
        previous_status: HealthStatus,
        detail: str = "",
        measurements: dict[str, float] | None = None,
    ) -> HealthEvent:
        row = HealthEvent(
            at=now,
            component=component,
            status=status.value,
            previous_status=previous_status.value,
            detail=detail[:500],
            measurements=measurements,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def recent(self, limit: int = 100, *, component: str | None = None) -> list[HealthEvent]:
        statement = select(HealthEvent).order_by(HealthEvent.at.desc()).limit(limit)
        if component is not None:
            statement = statement.where(HealthEvent.component == component)
        result = await self._session.execute(statement)
        return list(result.scalars())

    async def prune_before(self, cutoff: datetime) -> int:
        result = await self._session.execute(delete(HealthEvent).where(HealthEvent.at < cutoff))
        return affected_rows(result)


@dataclass(frozen=True)
class MetricPoint:
    """One time-series sample."""

    at: datetime
    value: float


class MetricRepository(Repository):
    """Time series behind the §56 metric list."""

    async def record(
        self,
        name: str,
        value: float,
        *,
        now: datetime,
        labels: dict[str, str] | None = None,
    ) -> None:
        self._session.add(MetricSample(at=now, name=name, value=value, labels=labels))

    async def record_many(
        self, samples: dict[str, float], *, now: datetime, labels: dict[str, str] | None = None
    ) -> None:
        for name, value in samples.items():
            self._session.add(MetricSample(at=now, name=name, value=value, labels=labels))

    async def series(
        self, name: str, *, since: datetime, limit: int = 2_000
    ) -> list[MetricPoint]:
        """Ascending samples for a chart.

        Queried descending with a LIMIT then reversed, so that when the window
        holds more points than the limit we keep the *newest* ones. Ascending with
        a limit would silently show the oldest slice, which looks like a stalled
        chart.
        """
        result = await self._session.execute(
            select(MetricSample.at, MetricSample.value)
            .where(MetricSample.name == name, MetricSample.at >= since)
            .order_by(MetricSample.at.desc())
            .limit(limit)
        )
        rows = [MetricPoint(at=at, value=value) for at, value in result.all()]
        rows.reverse()
        return rows

    async def latest(self, name: str) -> MetricPoint | None:
        result = await self._session.execute(
            select(MetricSample.at, MetricSample.value)
            .where(MetricSample.name == name)
            .order_by(MetricSample.at.desc())
            .limit(1)
        )
        row = result.first()
        return None if row is None else MetricPoint(at=row[0], value=row[1])

    async def aggregate(
        self, name: str, *, since: datetime
    ) -> tuple[float, float, float] | None:
        """``(min, avg, max)`` over a window, computed in SQL.

        In SQL rather than Python because the §56 p95 generation-time panel covers
        a 24-hour window, which can be tens of thousands of rows the API should not
        be transferring on every refresh.
        """
        result = await self._session.execute(
            select(
                func.min(MetricSample.value),
                func.avg(MetricSample.value),
                func.max(MetricSample.value),
            ).where(MetricSample.name == name, MetricSample.at >= since)
        )
        row = result.first()
        if row is None or row[0] is None:
            return None
        return (float(row[0]), float(row[1]), float(row[2]))

    async def prune_before(self, cutoff: datetime) -> int:
        result = await self._session.execute(delete(MetricSample).where(MetricSample.at < cutoff))
        return affected_rows(result)


__all__ = [
    "HealthEventRepository",
    "MetricPoint",
    "MetricRepository",
    "SystemEventRepository",
]
