"""The radio queue and its three locking layers (§27, §28, milestone 4.4).

This implements the station's central tension, stated in §28:

    The station reacts to the market without destroying playback continuity.

Those two goals conflict directly. Reacting means replacing future programming when the market
changes; continuity means not yanking a track out from under a listener. The resolution is that
**not all future programming is equally future**, so the queue carries an explicit lock level
per slot rather than inferring one from position.

Inferring from position looks simpler and breaks immediately: an operator pinning a track (§44)
has no position-based expression, and "keep current, maybe keep next, recalculate the rest"
becomes index arithmetic that every caller re-derives slightly differently.

The three layers:

``HARD LOCKED``   playing now, or close enough that replacing it would be audible. Automatic
                  replanning may never touch it. ``OPERATOR_PINNED`` is hard-locked too, and
                  outranks every automatic rule — the system must not quietly overrule a
                  deliberate human choice.
``SOFT LOCKED``   generated and expected soon. Replaced only by a *large* market change, and
                  never while its audio is still being generated.
``FLEXIBLE``      far enough out that nobody has heard it coming. Freely recalculated.

**Operations are transactional.** Every mutation goes through :meth:`RadioQueue.transaction`,
which snapshots the entries and restores them if the body raises. A replan that fails halfway
would otherwise leave the queue holding a mixture of old and new programming with duplicate
positions — and the §41 panel would render it, so the corruption would look like a feature.

**Positions are derived, never stored as the source of truth.** The entry list *is* the order.
Storing a position field and a list ordering means two representations that can disagree, and
the one that disagrees is always the one being read.
"""

from __future__ import annotations

import contextlib
import enum
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

import structlog

from tradefix_radio.contracts.enums import MarketRegime, PlayoutTier, TransitionType
from tradefix_radio.contracts.music import MusicBlueprintV1
from tradefix_radio.contracts.queue import QueueLockLevel
from tradefix_radio.core.errors import ProtectedItemError, QueueError
from tradefix_radio.core.state_machine import TrackState

_log = structlog.get_logger(__name__)


class ReadinessState(str, enum.Enum):
    """Whether a slot can actually be played right now.

    Separate from both ``TrackState`` and the generation job's state, because it answers a
    different question: those describe *progress*, this describes *usability*. A slot can be
    ``TrackState.READY`` and still unplayable because its file went missing (§36's retention
    sweeper, or a disk fault), and the playout engine needs that answer in one field rather than
    inferring it from three.
    """

    PENDING = "pending"
    """No audio yet. Generation has not finished."""

    READY = "ready"
    """Audio exists and has been verified present."""

    UNAVAILABLE = "unavailable"
    """Audio was expected and is not there. The slot must be skipped, loudly."""


@dataclass(frozen=True)
class QueueEntry:
    """One slot in the forward schedule.

    Frozen. Mutations produce a new entry through :func:`dataclasses.replace`, which makes the
    transactional snapshot in :meth:`RadioQueue.transaction` a shallow list copy rather than a
    deep one — and means a caller holding an entry cannot change the queue by accident.
    """

    track_id: str
    blueprint: MusicBlueprintV1
    duration_seconds: float
    inserted_at: datetime

    #: §27 track state, mirroring the database.
    state: TrackState = TrackState.PLANNED
    readiness: ReadinessState = ReadinessState.PENDING
    lock_level: QueueLockLevel = QueueLockLevel.REPLACEABLE
    #: Why this slot is locked, in words. Shown in the §44 panel, and the difference between
    #: an operator understanding a refusal and filing a bug about it.
    lock_reason: str = ""
    tier: PlayoutTier = PlayoutTier.SCHEDULED

    #: Market regime when this slot was planned. The replanner compares it against the current
    #: regime to decide whether the slot is still apt.
    planned_regime: MarketRegime = MarketRegime.UNKNOWN
    planned_energy: float = 0.0

    transition_in: TransitionType = TransitionType.CROSSFADE
    transition_seconds: float = 0.0
    generation_progress: float = 0.0
    #: Set once the audio exists, so the playout engine needs no repository lookup on the
    #: critical path.
    audio_path: str | None = None

    started_at: datetime | None = None

    @property
    def is_playable(self) -> bool:
        return self.readiness is ReadinessState.READY and self.audio_path is not None

    @property
    def is_protected(self) -> bool:
        """Whether automatic replanning must leave this slot alone."""
        return self.lock_level.is_protected

    @property
    def is_flexible(self) -> bool:
        return self.lock_level is QueueLockLevel.REPLACEABLE

    def with_lock(self, level: QueueLockLevel, reason: str) -> QueueEntry:
        return replace(self, lock_level=level, lock_reason=reason)


@dataclass(frozen=True)
class QueueSnapshot:
    """An immutable view, for the §41 panel and for buffer maths.

    Returned instead of the live list so a caller cannot iterate the queue while the scheduler
    mutates it — and so the numbers in one render are all from the same instant.
    """

    entries: tuple[QueueEntry, ...]
    playing: QueueEntry | None
    total_seconds: float
    ready_seconds: float
    pending_seconds: float

    @property
    def depth(self) -> int:
        return len(self.entries)

    @property
    def ready_count(self) -> int:
        return sum(1 for entry in self.entries if entry.is_playable)

    def at(self, position: int) -> QueueEntry | None:
        return self.entries[position] if 0 <= position < len(self.entries) else None


@dataclass
class QueueStats:
    """Lifetime counters, for the soak report."""

    inserted: int = 0
    removed: int = 0
    replaced: int = 0
    rejected_duplicates: int = 0
    protected_refusals: int = 0
    advanced: int = 0
    rolled_back: int = 0


class RadioQueue:
    """The forward schedule, with §28's layered locking.

    Not thread-safe and not meant to be: the whole station runs on one event loop per process
    (ADR-08), and adding a lock would invite the belief that concurrent mutation is supported
    when the invariants assume it is not.
    """

    def __init__(
        self,
        *,
        locked_slots: int = 2,
        semi_locked_slots: int = 2,
        allow_duplicate_tracks: bool = False,
    ) -> None:
        if locked_slots < 1:
            raise ValueError(
                "locked_slots must be at least 1: the track that is playing has to be "
                "protected, or the station can replace what a listener is hearing"
            )
        if semi_locked_slots < 0:
            raise ValueError("semi_locked_slots must not be negative")
        self._locked_slots = locked_slots
        self._semi_locked_slots = semi_locked_slots
        self._allow_duplicates = allow_duplicate_tracks
        self._entries: list[QueueEntry] = []
        self._playing: QueueEntry | None = None
        self._stats = QueueStats()

    # -- introspection -----------------------------------------------------

    @property
    def stats(self) -> QueueStats:
        return self._stats

    @property
    def playing(self) -> QueueEntry | None:
        return self._playing

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[QueueEntry]:
        return iter(tuple(self._entries))

    def position_of(self, track_id: str) -> int | None:
        for index, entry in enumerate(self._entries):
            if entry.track_id == track_id:
                return index
        return None

    def get(self, track_id: str) -> QueueEntry | None:
        index = self.position_of(track_id)
        return None if index is None else self._entries[index]

    def contains(self, track_id: str) -> bool:
        """Whether a track is anywhere in the queue, including the playing slot."""
        if self._playing is not None and self._playing.track_id == track_id:
            return True
        return self.position_of(track_id) is not None

    def snapshot(self) -> QueueSnapshot:
        ready = sum(e.duration_seconds for e in self._entries if e.is_playable)
        total = sum(e.duration_seconds for e in self._entries)
        return QueueSnapshot(
            entries=tuple(self._entries),
            playing=self._playing,
            total_seconds=total,
            ready_seconds=ready,
            pending_seconds=total - ready,
        )

    def total_seconds(self) -> float:
        """Every queued slot, ready or not.

        Optimistic by definition — it counts audio that does not exist yet — which is why
        :meth:`ready_seconds` exists alongside it. Counting in-flight work as buffer is how a
        station walks into silence believing it has forty minutes in hand.
        """
        return sum(entry.duration_seconds for entry in self._entries)

    def ready_seconds(self) -> float:
        """Only slots that could actually air right now. The honest buffer figure."""
        return sum(e.duration_seconds for e in self._entries if e.is_playable)

    # -- transactions ------------------------------------------------------

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """Apply a group of mutations atomically.

        A replan that fails halfway would otherwise leave the queue holding a mixture of old
        and new programming — and because the §41 panel renders whatever is there, the
        corruption would look like a feature rather than a fault.

        The snapshot is a shallow list copy, which is sufficient because entries are frozen.
        """
        saved_entries = list(self._entries)
        saved_playing = self._playing
        try:
            yield
        except Exception:
            self._entries = saved_entries
            self._playing = saved_playing
            self._stats.rolled_back += 1
            _log.warning(
                "queue.transaction_rolled_back",
                depth=len(self._entries),
                detail="the queue was restored to its state before the failed operation",
            )
            raise
        self._relock()

    # -- insertion ---------------------------------------------------------

    def append(self, entry: QueueEntry) -> int:
        """Add to the end. Returns the new position."""
        return self.insert(len(self._entries), entry)

    def insert(self, position: int, entry: QueueEntry) -> int:
        """Insert at ``position``, clamped into range.

        Refuses a duplicate track id unless duplicates were explicitly enabled. Two slots
        sharing an id is not a cosmetic problem: ``mark_played`` and the §11 history both look
        tracks up by id, so the second occurrence silently corrupts both.
        """
        if not self._allow_duplicates and self.contains(entry.track_id):
            self._stats.rejected_duplicates += 1
            raise QueueError(
                f"track {entry.track_id!r} is already queued; two slots sharing an id "
                "corrupt play history and the §11 diversity window",
                track_id=entry.track_id,
            )
        index = max(0, min(position, len(self._entries)))
        with self.transaction():
            self._entries.insert(index, entry)
            self._stats.inserted += 1
        return index

    # -- removal and replacement -------------------------------------------

    def discard(self, track_id: str, *, force: bool = False) -> QueueEntry | None:
        """Remove a slot if it is present; return ``None`` if it was not.

        The automatic counterpart to :meth:`remove`. Two callers want opposite things from
        a missing track: an operator pressing "remove" on a slot that has vanished needs to
        be told, while automatic cleanup of a permanently failed job only needs the slot
        *gone* — and already-gone satisfies that completely.

        The distinction is not hypothetical. Recovery legitimately drops a restored slot
        whose blueprint is missing, while the generation job for that same track is still
        pending; the job then fails, and cleanup used to raise `QueueError` from inside the
        failure handler, replacing the real failure with a confusing secondary one on every
        scheduling cycle.
        """
        if self.position_of(track_id) is None:
            return None
        return self.remove(track_id, force=force)

    def remove(self, track_id: str, *, force: bool = False) -> QueueEntry:
        """Remove a slot. Refuses protected slots unless ``force``.

        Raises when the track is absent — see :meth:`discard` for the automatic path that
        treats absence as success.

        ``force`` exists for §44's operator actions, which are allowed to override automatic
        protection — with confirmation, which is the UI's business rather than this method's.
        """
        index = self.position_of(track_id)
        if index is None:
            raise QueueError(f"track {track_id!r} is not in the queue", track_id=track_id)
        entry = self._entries[index]
        if entry.is_protected and not force:
            self._stats.protected_refusals += 1
            raise ProtectedItemError(
                f"track {track_id!r} is {entry.lock_level.value} "
                f"({entry.lock_reason or 'no reason recorded'}) and cannot be removed "
                "automatically",
                track_id=track_id,
                lock_level=entry.lock_level.value,
            )
        with self.transaction():
            removed = self._entries.pop(index)
            self._stats.removed += 1
        return removed

    def replace_flexible(
        self,
        replacements: Sequence[QueueEntry],
        *,
        from_position: int | None = None,
    ) -> int:
        """§28's replan: swap out flexible programming, keep everything protected.

        The core operation of the whole module. Protected and soft-locked slots are retained in
        their original order; flexible ones are dropped and ``replacements`` appended after
        them.

        Returns how many slots were replaced. Transactional: a failure mid-replan restores the
        previous schedule rather than leaving a half-built one.
        """
        start = 0 if from_position is None else max(0, from_position)
        with self.transaction():
            kept: list[QueueEntry] = []
            dropped = 0
            for index, entry in enumerate(self._entries):
                if index < start or not entry.is_flexible:
                    kept.append(entry)
                else:
                    dropped += 1
            existing = {entry.track_id for entry in kept}
            if self._playing is not None:
                existing.add(self._playing.track_id)
            for candidate in replacements:
                if not self._allow_duplicates and candidate.track_id in existing:
                    self._stats.rejected_duplicates += 1
                    continue
                kept.append(candidate)
                existing.add(candidate.track_id)
            self._entries = kept
            self._stats.replaced += dropped
        _log.info(
            "queue.replanned",
            dropped=dropped,
            added=len(replacements),
            depth=len(self._entries),
        )
        return dropped

    def flexible_entries(self) -> tuple[QueueEntry, ...]:
        return tuple(entry for entry in self._entries if entry.is_flexible)

    def protected_entries(self) -> tuple[QueueEntry, ...]:
        return tuple(entry for entry in self._entries if entry.is_protected)

    # -- updates -----------------------------------------------------------

    def update(self, track_id: str, **changes: object) -> QueueEntry:
        """Change fields on one slot, preserving its position.

        Position is preserved implicitly by replacing in place rather than removing and
        reinserting. Remove-then-insert is the obvious implementation and it silently moves the
        slot to the end whenever the caller forgets to pass the index back.
        """
        index = self.position_of(track_id)
        if index is None:
            raise QueueError(f"track {track_id!r} is not in the queue", track_id=track_id)
        with self.transaction():
            self._entries[index] = replace(self._entries[index], **changes)  # type: ignore[arg-type]
        return self._entries[index]

    def mark_ready(self, track_id: str, *, audio_path: str, duration_seconds: float) -> QueueEntry:
        """Record that a slot's audio exists and is playable.

        Takes the measured duration rather than trusting the blueprint's: the §24
        duration-deviation check exists because providers do not always deliver what was asked
        for, and the buffer must be computed from what will actually air.
        """
        return self.update(
            track_id,
            readiness=ReadinessState.READY,
            state=TrackState.READY,
            audio_path=audio_path,
            duration_seconds=duration_seconds,
            generation_progress=1.0,
        )

    def mark_unavailable(self, track_id: str, *, reason: str) -> QueueEntry:
        """Record that a slot's audio has gone missing (§36, disk fault).

        The slot is kept rather than removed. Removing it would silently shorten the queue and
        make the §41 panel show programming that never existed; keeping it ``UNAVAILABLE`` means
        the playout engine skips it *visibly* and the operator can see what happened.
        """
        _log.error("queue.audio_unavailable", track_id=track_id, reason=reason)
        return self.update(
            track_id, readiness=ReadinessState.UNAVAILABLE, lock_reason=reason
        )

    def set_lock(self, track_id: str, level: QueueLockLevel, *, reason: str) -> QueueEntry:
        """Set a lock explicitly. Used for §44's operator pin."""
        return self.update(track_id, lock_level=level, lock_reason=reason)

    # -- playback ----------------------------------------------------------

    def begin_playing(self, now: datetime) -> QueueEntry | None:
        """Move the head of the queue into the playing slot.

        The playing entry leaves the queue list. Keeping it in place and tracking an index
        makes every length, duration and position calculation conditional on whether index 0 is
        playing — and one of those conditions is always wrong.
        """
        playable = next((e for e in self._entries if e.is_playable), None)
        if playable is None:
            return None
        with self.transaction():
            index = self._entries.index(playable)
            entry = self._entries.pop(index)
            entry = replace(entry, state=TrackState.PLAYING, started_at=now)
            self._playing = entry
            self._stats.advanced += 1
        return entry

    def finish_playing(self) -> QueueEntry | None:
        """Clear the playing slot and return what was there."""
        finished = self._playing
        self._playing = None
        return finished

    def skipped_unplayable(self) -> tuple[QueueEntry, ...]:
        """Slots at the head that cannot be played, in order.

        The playout engine uses this to report *what* it skipped. Silently stepping over an
        unavailable slot makes a disk fault look like a short queue.
        """
        skipped: list[QueueEntry] = []
        for entry in self._entries:
            if entry.is_playable:
                break
            skipped.append(entry)
        return tuple(skipped)

    def drop_unplayable_head(self) -> tuple[QueueEntry, ...]:
        """Remove unplayable slots blocking the head, returning them."""
        doomed = [e for e in self.skipped_unplayable() if e.readiness is ReadinessState.UNAVAILABLE]
        if not doomed:
            return ()
        with self.transaction():
            for entry in doomed:
                self._entries.remove(entry)
                self._stats.removed += 1
        return tuple(doomed)

    # -- locking -----------------------------------------------------------

    def _relock(self) -> None:
        """Recompute automatic lock levels after any change.

        Called from :meth:`transaction`, so it is impossible to mutate the queue and forget.
        An operator pin is never overwritten — §44 says the system must not quietly overrule a
        deliberate human choice, and "quietly" is exactly what an automatic relock would be.
        """
        for index, entry in enumerate(self._entries):
            if entry.lock_level is QueueLockLevel.OPERATOR_PINNED:
                continue
            level, reason = self._automatic_lock(index, entry)
            if entry.lock_level is not level:
                self._entries[index] = entry.with_lock(level, reason)

    def _automatic_lock(self, index: int, entry: QueueEntry) -> tuple[QueueLockLevel, str]:
        if index < self._locked_slots:
            return (
                QueueLockLevel.LOCKED,
                f"position {index}: too close to playback to replace safely",
            )
        if index < self._locked_slots + self._semi_locked_slots:
            return (
                QueueLockLevel.SEMI_LOCKED,
                f"position {index}: generated and expected soon",
            )
        if entry.generation_progress > 0.0 and entry.readiness is ReadinessState.PENDING:
            # Mid-generation. Replacing it would waste the work already spent and leave the
            # provider writing to a path nothing references.
            return (
                QueueLockLevel.SEMI_LOCKED,
                "generation in progress; replacing would discard work already done",
            )
        return QueueLockLevel.REPLACEABLE, ""

    # -- restoration (§75) -------------------------------------------------

    def restore(
        self, entries: Sequence[QueueEntry], *, playing: QueueEntry | None = None
    ) -> None:
        """Rebuild from persistence after a restart (milestone 4.9).

        Locks are **recomputed** rather than restored. A lock level is a function of position,
        and positions are what a restart re-establishes — so persisting the level and trusting
        it would reinstate a stale answer. Operator pins are the exception and are carried
        through, because those are a human decision rather than a derived value.
        """
        seen: set[str] = set()
        unique: list[QueueEntry] = []
        for entry in entries:
            if entry.track_id in seen and not self._allow_duplicates:
                _log.error(
                    "queue.duplicate_dropped_on_restore",
                    track_id=entry.track_id,
                    detail="persisted queue contained the same track twice",
                )
                continue
            seen.add(entry.track_id)
            unique.append(entry)
        self._entries = unique
        self._playing = playing
        self._relock()
        _log.info(
            "queue.restored",
            depth=len(self._entries),
            playing=None if playing is None else playing.track_id,
            ready_seconds=round(self.ready_seconds(), 1),
        )

    def clear(self) -> None:
        self._entries.clear()
        self._playing = None


def build_entry(
    blueprint: MusicBlueprintV1,
    *,
    now: datetime,
    regime: MarketRegime,
    energy: float,
    tier: PlayoutTier = PlayoutTier.SCHEDULED,
    transition: TransitionType = TransitionType.CROSSFADE,
    transition_seconds: float = 0.0,
) -> QueueEntry:
    """A pending slot from a fresh blueprint."""
    return QueueEntry(
        track_id=blueprint.track_id,
        blueprint=blueprint,
        duration_seconds=float(blueprint.composition.duration_seconds),
        inserted_at=now,
        state=TrackState.PLANNED,
        readiness=ReadinessState.PENDING,
        planned_regime=regime,
        planned_energy=energy,
        tier=tier,
        transition_in=transition,
        transition_seconds=transition_seconds,
    )


__all__ = [
    "QueueEntry",
    "QueueSnapshot",
    "QueueStats",
    "RadioQueue",
    "ReadinessState",
    "build_entry",
]
