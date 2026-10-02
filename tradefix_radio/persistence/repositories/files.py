"""Track file repository — the expendable layer (ADR-07, §36).

This is the only place that knows where audio bytes live. Everything above refers
to tracks, never to paths, which is what makes retention able to delete files
without any other subsystem noticing.

:meth:`TrackFileRepository.retention_candidates` is the join that feeds the
retention planner. It is a single query rather than per-file lookups because a
sweep considers every file in the library, and N+1 queries over tens of thousands
of rows would make the sweep itself the reason the station stuttered.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select, update

from tradefix_radio.core.state_machine import TrackState
from tradefix_radio.persistence.models import QueueItem, Track, TrackFile
from tradefix_radio.persistence.repositories.base import Repository, affected_rows
from tradefix_radio.storage.paths import FileRole
from tradefix_radio.storage.retention import RetentionCandidate

#: Queue states in which a file must never be reclaimed.
_LIVE_QUEUE_STATES = (
    TrackState.READY.value,
    TrackState.QUEUED.value,
    TrackState.PLAYING.value,
)


class TrackFileRepository(Repository):
    """Records and reclaims the files belonging to tracks."""

    async def add(
        self,
        *,
        track_id: str,
        role: FileRole,
        path: Path,
        size_bytes: int,
        file_format: str,
        now: datetime,
        sample_rate: int | None = None,
        channels: int | None = None,
        sha256: str | None = None,
        retain_forever: bool = False,
    ) -> TrackFile:
        row = TrackFile(
            track_id=track_id,
            role=role.value,
            path=str(path),
            file_format=file_format,
            size_bytes=size_bytes,
            sample_rate=sample_rate,
            channels=channels,
            sha256=sha256,
            created_at=now,
            retain_forever=retain_forever,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, track_id: str, role: FileRole) -> TrackFile | None:
        result = await self._session.execute(
            select(TrackFile).where(
                TrackFile.track_id == track_id, TrackFile.role == role.value
            )
        )
        return result.scalar_one_or_none()

    async def live_path(self, track_id: str, role: FileRole) -> Path | None:
        """Path of a file whose bytes still exist, or ``None``.

        Returns ``None`` for a reclaimed file rather than a path that no longer
        resolves, so callers cannot accidentally try to play archived audio.
        """
        row = await self.get(track_id, role)
        if row is None or row.deleted_at is not None:
            return None
        return Path(row.path)

    async def for_track(self, track_id: str) -> list[TrackFile]:
        result = await self._session.execute(
            select(TrackFile)
            .where(TrackFile.track_id == track_id)
            .order_by(TrackFile.role)
        )
        return list(result.scalars())

    async def mark_reclaimed(self, file_ids: list[int], *, now: datetime) -> int:
        """Mark rows as having had their bytes deleted.

        The row survives deliberately (ADR-07): the §46 library shows "audio
        archived" rather than pretending the track never had any, and the sweep
        stays idempotent if it is interrupted between unlinking and committing.
        """
        if not file_ids:
            return 0
        result = await self._session.execute(
            update(TrackFile)
            .where(TrackFile.id.in_(file_ids), TrackFile.deleted_at.is_(None))
            .values(deleted_at=now)
        )
        return affected_rows(result)

    async def set_retain_forever(self, file_id: int, *, retain: bool) -> None:
        """Pin or unpin a file against the sweeper (§33 Tier 2 reserve)."""
        await self._session.execute(
            update(TrackFile).where(TrackFile.id == file_id).values(retain_forever=retain)
        )

    async def total_bytes(self, *, include_reclaimed: bool = False) -> int:
        statement = select(func.coalesce(func.sum(TrackFile.size_bytes), 0))
        if not include_reclaimed:
            statement = statement.where(TrackFile.deleted_at.is_(None))
        result = await self._session.execute(statement)
        return int(result.scalar_one() or 0)

    async def retention_candidates(self, *, limit: int | None = None) -> list[RetentionCandidate]:
        """Every live file, joined to the context the planner needs (§36).

        The ``in_queue`` flag comes from a correlated EXISTS against live queue
        states rather than a Python-side set, so there is no window in which the
        planner's view of the queue is stale relative to the files it is judging.
        """
        in_queue = (
            select(QueueItem.item_id)
            .where(
                QueueItem.track_id == TrackFile.track_id,
                QueueItem.state.in_(_LIVE_QUEUE_STATES),
            )
            .exists()
        )
        statement = (
            select(
                TrackFile.id,
                TrackFile.track_id,
                TrackFile.role,
                TrackFile.path,
                TrackFile.size_bytes,
                TrackFile.created_at,
                TrackFile.retain_forever,
                TrackFile.deleted_at,
                Track.state,
                Track.last_played_at,
                in_queue.label("in_queue"),
            )
            .join(Track, Track.track_id == TrackFile.track_id)
            .where(TrackFile.deleted_at.is_(None))
            .order_by(TrackFile.created_at)
        )
        if limit is not None:
            statement = statement.limit(limit)

        result = await self._session.execute(statement)
        candidates: list[RetentionCandidate] = []
        for row in result.all():
            try:
                role = FileRole(row.role)
            except ValueError:
                # An unknown role is data we do not understand; refusing to
                # consider it for deletion is the only safe reading.
                continue
            candidates.append(
                RetentionCandidate(
                    file_id=row.id,
                    track_id=row.track_id,
                    role=role,
                    path=Path(row.path),
                    size_bytes=row.size_bytes,
                    created_at=row.created_at,
                    track_state=TrackState(row.state),
                    last_played_at=row.last_played_at,
                    retain_forever=bool(row.retain_forever),
                    in_queue=bool(row.in_queue),
                    already_deleted=row.deleted_at is not None,
                )
            )
        return candidates


__all__ = ["TrackFileRepository"]
