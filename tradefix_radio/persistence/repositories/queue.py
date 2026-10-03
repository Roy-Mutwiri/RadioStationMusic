"""Queue persistence (§27, §75, milestone 4.9).

The queue is the station's short-term memory, and a restart that loses it drops every track
that was generated but not yet played — audio that exists on disk and cost real GPU time.

**This layer does not know about ``RadioQueue``.** It stores and returns
:class:`PersistedQueueSlot`, a flat row-shaped value, and the radio layer converts. The first
version imported :class:`~tradefix_radio.radio.queue.QueueEntry` directly and produced a genuine
import cycle — persistence → radio → generation → persistence — but the cycle was only the
symptom. The cause was a storage layer reaching upward into a domain layer, and the fix is the
boundary rather than a deferred import.

**What is persisted and what is recomputed.** Positions, track ids, readiness and audio paths are
persisted; **lock levels are not restored**, they are recomputed. A lock level is a function of
position, so persisting it stores a derived value that goes stale the moment anything shifts.
The exception is an operator pin, which is a human decision rather than a derived one and is
carried through.

**The blueprint is rehydrated from ``track_blueprints``, not duplicated here.** A second copy
would be a second thing to keep in sync, and §8 already requires every track to have its
blueprint stored. A slot whose blueprint has gone is dropped on restore with a loud log: a slot
the director cannot explain is a slot the §46 page cannot render.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import delete, select

from tradefix_radio.contracts.music import MusicBlueprintV1
from tradefix_radio.persistence.models import QueueItem, TrackBlueprint
from tradefix_radio.persistence.repositories.base import Repository

_log = structlog.get_logger(__name__)

#: Marks the row that was playing when the snapshot was taken.
#:
#: A sentinel position rather than a separate table: the playing slot is a queue row in every
#: respect except that it has left the list, and a one-row table would be a join for no reason.
PLAYING_POSITION = -1


@dataclass(frozen=True)
class PersistedQueueSlot:
    """One stored queue row, as plain data.

    Strings rather than the radio layer's enums, because this is the storage boundary: the
    database holds strings, and converting here would make persistence depend on the domain's
    type definitions — which is exactly the dependency this module exists without.
    """

    track_id: str
    position: int
    blueprint: MusicBlueprintV1
    duration_seconds: float
    enqueued_at: datetime
    state: str
    readiness: str
    lock_level: str
    lock_reason: str
    tier: str
    planned_regime: str
    planned_energy: float
    transition_in: str
    transition_seconds: float
    generation_progress: float
    audio_path: str | None
    started_at: datetime | None

    @property
    def is_playing(self) -> bool:
        return self.position == PLAYING_POSITION


class QueueRepository(Repository):
    """Stores and restores the forward schedule."""

    #: Track ids dropped by the most recent :meth:`load` for want of a blueprint.
    #:
    #: Populated per call rather than accumulated, so a second load does not resurrect
    #: the findings of the first.
    dropped_track_ids: list[str]

    async def save(self, slots: list[PersistedQueueSlot]) -> int:
        """Replace the persisted queue with ``slots``.

        Delete-then-insert rather than a diff. The queue is at most a few dozen rows, a replan
        can change nearly all of them, and a diff would be more code with more ways to leave a
        stale row behind — which would then be restored as programming nobody planned.
        """
        await self._session.execute(delete(QueueItem))
        self._session.add_all([_to_row(slot) for slot in slots])
        await self._session.flush()
        return len(slots)

    async def load(self) -> list[PersistedQueueSlot]:
        """Every stored slot, ordered by position. Rows without a blueprint are dropped.

        Which rows were dropped is recorded on :attr:`dropped_track_ids`, because the
        caller needs it: the generation job for such a slot is still pending, can never
        succeed — the provider is handed a blueprint and there is nothing to hand it — and
        will otherwise be claimed, fail and retry on every scheduling cycle.
        """
        self.dropped_track_ids = []
        result = await self._session.execute(
            select(QueueItem).order_by(QueueItem.position)
        )
        rows = list(result.scalars())
        if not rows:
            return []

        blueprints = await self._blueprints_for([row.track_id for row in rows])
        slots: list[PersistedQueueSlot] = []
        for row in rows:
            blueprint = blueprints.get(row.track_id)
            if blueprint is None:
                _log.error(
                    "queue.restore_missing_blueprint",
                    track_id=row.track_id,
                    detail="dropped; a slot with no blueprint cannot be explained or replanned",
                )
                self.dropped_track_ids.append(row.track_id)
                continue
            slots.append(_to_slot(row, blueprint))
        return slots

    async def _blueprints_for(self, track_ids: list[str]) -> dict[str, MusicBlueprintV1]:
        if not track_ids:
            return {}
        result = await self._session.execute(
            select(TrackBlueprint).where(TrackBlueprint.track_id.in_(track_ids))
        )
        blueprints: dict[str, MusicBlueprintV1] = {}
        for row in result.scalars():
            try:
                blueprints[row.track_id] = MusicBlueprintV1.model_validate(row.payload)
            except Exception as error:  # noqa: BLE001 - one bad row must not block recovery
                _log.error(
                    "queue.blueprint_unreadable",
                    track_id=row.track_id,
                    error=str(error),
                )
        return blueprints

    async def clear(self) -> None:
        await self._session.execute(delete(QueueItem))


def _to_row(slot: PersistedQueueSlot) -> QueueItem:
    return QueueItem(
        item_id=f"{slot.track_id}@{slot.position}",
        track_id=slot.track_id,
        position=slot.position,
        state=slot.state,
        lock_level=slot.lock_level,
        tier=slot.tier,
        transition_in=slot.transition_in,
        transition_seconds=slot.transition_seconds,
        generation_progress=slot.generation_progress,
        enqueued_at=slot.enqueued_at,
        started_at=slot.started_at,
        finished_at=None,
        display={
            "readiness": slot.readiness,
            "audio_path": slot.audio_path,
            "duration_seconds": slot.duration_seconds,
            "planned_regime": slot.planned_regime,
            "planned_energy": slot.planned_energy,
            "lock_reason": slot.lock_reason,
        },
    )


def _to_slot(row: QueueItem, blueprint: MusicBlueprintV1) -> PersistedQueueSlot:
    display: dict[str, Any] = row.display or {}
    return PersistedQueueSlot(
        track_id=row.track_id,
        position=row.position,
        blueprint=blueprint,
        duration_seconds=float(
            display.get("duration_seconds") or blueprint.composition.duration_seconds
        ),
        enqueued_at=row.enqueued_at,
        state=row.state,
        readiness=str(display.get("readiness", "pending")),
        lock_level=row.lock_level,
        lock_reason=str(display.get("lock_reason", "")),
        tier=row.tier,
        planned_regime=str(display.get("planned_regime", "unknown")),
        planned_energy=float(display.get("planned_energy") or 0.0),
        transition_in=row.transition_in,
        transition_seconds=row.transition_seconds,
        generation_progress=row.generation_progress,
        audio_path=display.get("audio_path"),
        started_at=row.started_at,
    )


__all__ = ["PLAYING_POSITION", "PersistedQueueSlot", "QueueRepository"]
