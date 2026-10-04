"""Airing records (§37, §75).

One row per thing that went on air, whichever tier produced it. Deliberately **not** a child
of `tracks`: a Tier 3 procedural block has no track row and is still airtime, and the
question this table exists to answer — "what was the station actually broadcasting, and from
which tier" — is unanswerable if the emergency tiers are excluded from it.

The table, its model, its contract and five indexes all predate this module. What did not
exist was a writer, so after 247 tracks and 1,577 state transitions it held zero rows and
§75's rule ("never mark an incomplete track as successfully played") was intended but not
verifiable after the fact.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select

from tradefix_radio.persistence.models import PlayEvent
from tradefix_radio.persistence.repositories.base import Repository

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

_log = structlog.get_logger(__name__)


class PlayEventRepository(Repository):
    """Records what aired, and answers questions about it afterwards."""

    async def record(
        self,
        *,
        track_id: str,
        tier: str,
        started_at: datetime,
        ended_at: datetime,
        played_seconds: float,
        completed: bool,
        transition_in: str,
        end_reason: str | None = None,
        regime_at_play: str | None = None,
        symbol_at_play: str | None = None,
    ) -> PlayEvent:
        """Store one airing.

        ``played_seconds`` must come from the playout engine's frame counter. It is the only
        source that is right for a partial airing, and a partial airing is the case the row
        exists for — a completed one is already implied by the track's own state.
        """
        row = PlayEvent(
            track_id=track_id,
            started_at=started_at,
            ended_at=ended_at,
            played_seconds=max(0.0, played_seconds),
            completed=completed,
            tier=tier,
            transition_in=transition_in,
            end_reason=end_reason,
            regime_at_play=regime_at_play,
            symbol_at_play=symbol_at_play,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def seconds_by_tier(self, *, since: datetime | None = None) -> dict[str, float]:
        """Airtime per tier. The honest denominator for "how much of the hour was music".

        Reads `played_seconds` rather than counting rows, because a skipped track and a
        completed one are not the same amount of airtime and the §33 question is about
        time, not occurrences.
        """
        query = select(
            PlayEvent.tier, func.sum(PlayEvent.played_seconds)
        ).group_by(PlayEvent.tier)
        if since is not None:
            query = query.where(PlayEvent.started_at >= since)
        result = await self._session.execute(query)
        return {tier: float(total or 0.0) for tier, total in result.all()}

    async def completion_counts(self, *, since: datetime | None = None) -> dict[str, int]:
        """How many airings completed against how many were cut short (§75)."""
        query = select(PlayEvent.completed, func.count()).group_by(PlayEvent.completed)
        if since is not None:
            query = query.where(PlayEvent.started_at >= since)
        result = await self._session.execute(query)
        counts = {"completed": 0, "incomplete": 0}
        for completed, total in result.all():
            counts["completed" if completed else "incomplete"] = int(total)
        return counts

    async def recent(self, *, limit: int = 100) -> list[PlayEvent]:
        """Most recent airings first."""
        result = await self._session.execute(
            select(PlayEvent).order_by(PlayEvent.started_at.desc()).limit(limit)
        )
        return list(result.scalars())

    async def first_started_at(self) -> datetime | None:
        """When the station first put anything on air in this database.

        The anchor for "how long until the first generated track aired", which is the
        ready-buffer question.
        """
        result = await self._session.execute(
            select(func.min(PlayEvent.started_at))
        )
        return result.scalar_one_or_none()


__all__ = ["PlayEventRepository"]
