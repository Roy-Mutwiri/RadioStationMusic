"""RadioQueue and §28's layered locking (§27, §28, milestone 4.4).

The milestone's exit tests, quoted, are the section headings below:

* the current track cannot disappear;
* hard locked items cannot move accidentally;
* flexible tracks can be replaced;
* queue duration is always correct;
* removing tracks updates the buffer calculation;
* duplicate track ids cannot accidentally enter twice;
* restart reconstructs the queue correctly.

The last group is the one worth reading: a transaction that rolls back must leave the queue
*exactly* as it was, because a half-applied replan renders in the §41 panel and therefore looks
deliberate.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tradefix_radio.contracts.enums import MarketRegime, PlayoutTier
from tradefix_radio.contracts.queue import QueueLockLevel
from tradefix_radio.core.errors import ProtectedItemError, QueueError
from tradefix_radio.core.state_machine import TrackState
from tradefix_radio.radio.queue import (
    QueueEntry,
    RadioQueue,
    ReadinessState,
    build_entry,
)
from tests.conftest import FIXED_NOW, make_blueprint


def entry(
    index: int,
    *,
    duration: float = 200.0,
    ready: bool = False,
    regime: MarketRegime = MarketRegime.NORMAL_RANGE,
    energy: float = 50.0,
) -> QueueEntry:
    blueprint = make_blueprint(
        track_id=f"TF-{index:05d}", duration_seconds=int(duration)
    )
    built = build_entry(
        blueprint,
        now=FIXED_NOW + timedelta(seconds=index),
        regime=regime,
        energy=energy,
    )
    if not ready:
        return built
    from dataclasses import replace

    return replace(
        built,
        readiness=ReadinessState.READY,
        state=TrackState.READY,
        audio_path=f"/audio/{blueprint.track_id}.flac",
    )


def queue(count: int = 0, *, ready: bool = True, **kwargs: object) -> RadioQueue:
    radio_queue = RadioQueue(**kwargs)  # type: ignore[arg-type]
    for index in range(count):
        radio_queue.append(entry(index, ready=ready))
    return radio_queue


# ---------------------------------------------------------------- construction


def test_a_queue_must_protect_at_least_the_playing_slot() -> None:
    """Zero locked slots means the station can replace what a listener is hearing."""
    with pytest.raises(ValueError, match="locked_slots must be at least 1"):
        RadioQueue(locked_slots=0)


def test_negative_semi_locked_slots_are_rejected() -> None:
    with pytest.raises(ValueError, match="semi_locked_slots"):
        RadioQueue(semi_locked_slots=-1)


def test_an_empty_queue_reports_zero_everywhere() -> None:
    empty = RadioQueue()
    assert len(empty) == 0
    assert empty.total_seconds() == 0.0
    assert empty.ready_seconds() == 0.0
    assert empty.playing is None
    assert empty.snapshot().depth == 0


# ---------------------------------------------------------------- locking layers


def test_leading_slots_are_hard_locked() -> None:
    """§28: too close to playback to replace safely."""
    radio_queue = queue(8, locked_slots=2, semi_locked_slots=2)
    entries = radio_queue.snapshot().entries
    assert entries[0].lock_level is QueueLockLevel.LOCKED
    assert entries[1].lock_level is QueueLockLevel.LOCKED
    assert entries[0].is_protected
    assert entries[0].lock_reason, "a lock with no reason is unexplainable in the §44 panel"


def test_the_next_band_is_soft_locked() -> None:
    radio_queue = queue(8, locked_slots=2, semi_locked_slots=2)
    entries = radio_queue.snapshot().entries
    assert entries[2].lock_level is QueueLockLevel.SEMI_LOCKED
    assert entries[3].lock_level is QueueLockLevel.SEMI_LOCKED
    # Soft-locked is *not* protected: a large enough market change may replace it (§28).
    assert not entries[2].is_protected


def test_everything_further_out_is_flexible() -> None:
    radio_queue = queue(8, locked_slots=2, semi_locked_slots=2)
    for item in radio_queue.snapshot().entries[4:]:
        assert item.lock_level is QueueLockLevel.REPLACEABLE
        assert item.is_flexible


def test_locks_are_recomputed_after_every_mutation() -> None:
    """A lock is a function of position, so it has to follow the slot as positions shift."""
    radio_queue = queue(6, locked_slots=2, semi_locked_slots=1)
    tail = radio_queue.snapshot().entries[5]
    assert tail.is_flexible

    for index in range(4):
        radio_queue.remove(f"TF-{index:05d}", force=True)
    assert radio_queue.get(tail.track_id) is not None
    assert radio_queue.get(tail.track_id).lock_level is QueueLockLevel.LOCKED  # type: ignore[union-attr]


def test_a_track_being_generated_is_soft_locked_wherever_it_sits() -> None:
    """Replacing mid-generation discards work and leaves a provider writing to nothing."""
    radio_queue = queue(8, ready=False, locked_slots=1, semi_locked_slots=1)
    radio_queue.update("TF-00006", generation_progress=0.4)
    assert radio_queue.get("TF-00006").lock_level is QueueLockLevel.SEMI_LOCKED  # type: ignore[union-attr]


def test_an_operator_pin_is_never_overwritten_by_automatic_locking() -> None:
    """§44: the system must not quietly overrule a deliberate human choice."""
    radio_queue = queue(8)
    radio_queue.set_lock(
        "TF-00007", QueueLockLevel.OPERATOR_PINNED, reason="operator pinned"
    )
    radio_queue.append(entry(99))
    radio_queue.remove("TF-00005", force=True)
    pinned = radio_queue.get("TF-00007")
    assert pinned is not None
    assert pinned.lock_level is QueueLockLevel.OPERATOR_PINNED
    assert pinned.is_protected


# ---------------------------------------------------------------- the current track


def test_the_playing_track_leaves_the_queue_list() -> None:
    """Keeping it in place makes every length and position calculation conditional."""
    radio_queue = queue(4)
    playing = radio_queue.begin_playing(FIXED_NOW)
    assert playing is not None
    assert playing.state is TrackState.PLAYING
    assert radio_queue.playing is playing
    assert len(radio_queue) == 3
    assert radio_queue.position_of(playing.track_id) is None


def test_the_playing_track_still_counts_as_present() -> None:
    """Otherwise the scheduler could queue it again while it is on air."""
    radio_queue = queue(3)
    playing = radio_queue.begin_playing(FIXED_NOW)
    assert playing is not None
    assert radio_queue.contains(playing.track_id)
    with pytest.raises(QueueError, match="already queued"):
        radio_queue.append(entry(0))


def test_the_current_track_cannot_be_removed_by_a_replan() -> None:
    """Milestone 4.4's headline: the current track cannot disappear."""
    radio_queue = queue(6)
    playing = radio_queue.begin_playing(FIXED_NOW)
    assert playing is not None
    radio_queue.replace_flexible([entry(50), entry(51)])
    assert radio_queue.playing is playing


def test_beginning_playback_with_nothing_ready_returns_nothing() -> None:
    radio_queue = queue(3, ready=False)
    assert radio_queue.begin_playing(FIXED_NOW) is None
    assert radio_queue.playing is None


def test_playback_skips_over_a_pending_head_to_the_first_ready_slot() -> None:
    """A slot still generating must not block one behind it that is finished."""
    radio_queue = RadioQueue()
    radio_queue.append(entry(0, ready=False))
    radio_queue.append(entry(1, ready=True))
    playing = radio_queue.begin_playing(FIXED_NOW)
    assert playing is not None
    assert playing.track_id == "TF-00001"
    assert radio_queue.position_of("TF-00000") == 0


# ---------------------------------------------------------------- replanning (§28)


def test_flexible_tracks_are_replaced_and_protected_ones_are_kept() -> None:
    radio_queue = queue(8, locked_slots=2, semi_locked_slots=2)
    kept = [e.track_id for e in radio_queue.snapshot().entries[:4]]
    dropped = radio_queue.replace_flexible([entry(50), entry(51), entry(52)])

    assert dropped == 4
    remaining = [e.track_id for e in radio_queue.snapshot().entries]
    assert remaining[:4] == kept
    assert remaining[4:] == ["TF-00050", "TF-00051", "TF-00052"]


def test_a_replan_preserves_the_order_of_what_it_keeps() -> None:
    radio_queue = queue(8, locked_slots=3, semi_locked_slots=0)
    before = [e.track_id for e in radio_queue.snapshot().entries[:3]]
    radio_queue.replace_flexible([entry(60)])
    assert [e.track_id for e in radio_queue.snapshot().entries[:3]] == before


def test_a_replan_can_be_limited_to_positions_beyond_a_point() -> None:
    """§29's "keep current, maybe keep next, recalculate later" as an explicit argument."""
    radio_queue = queue(8, locked_slots=1, semi_locked_slots=0)
    radio_queue.replace_flexible([entry(70)], from_position=5)
    remaining = [e.track_id for e in radio_queue.snapshot().entries]
    assert remaining[:5] == [f"TF-{i:05d}" for i in range(5)]
    assert remaining[-1] == "TF-00070"


def test_a_replan_will_not_reintroduce_something_already_queued() -> None:
    radio_queue = queue(6, locked_slots=2, semi_locked_slots=1)
    radio_queue.replace_flexible([entry(0), entry(80)])
    ids = [e.track_id for e in radio_queue.snapshot().entries]
    assert ids.count("TF-00000") == 1
    assert "TF-00080" in ids


def test_replacing_nothing_is_allowed_and_empties_the_flexible_tail() -> None:
    """A market shift with no replacement blueprints yet must not corrupt the queue."""
    radio_queue = queue(6, locked_slots=2, semi_locked_slots=1)
    dropped = radio_queue.replace_flexible([])
    assert dropped == 3
    assert len(radio_queue) == 3


# ---------------------------------------------------------------- duration accounting


def test_queue_duration_is_the_sum_of_its_slots() -> None:
    radio_queue = RadioQueue()
    for index, duration in enumerate((120.0, 200.0, 240.0)):
        radio_queue.append(entry(index, duration=duration, ready=True))
    assert radio_queue.total_seconds() == pytest.approx(560.0)
    assert radio_queue.ready_seconds() == pytest.approx(560.0)


def test_pending_audio_is_counted_separately_from_ready_audio() -> None:
    """§26: counting in-flight work as buffer is how a station walks into silence."""
    radio_queue = RadioQueue()
    radio_queue.append(entry(0, duration=200.0, ready=True))
    radio_queue.append(entry(1, duration=200.0, ready=False))
    assert radio_queue.total_seconds() == pytest.approx(400.0)
    assert radio_queue.ready_seconds() == pytest.approx(200.0)
    snapshot = radio_queue.snapshot()
    assert snapshot.pending_seconds == pytest.approx(200.0)
    assert snapshot.ready_count == 1


def test_removing_a_track_updates_the_duration() -> None:
    radio_queue = queue(4)
    before = radio_queue.ready_seconds()
    removed = radio_queue.remove("TF-00003", force=True)
    assert radio_queue.ready_seconds() == pytest.approx(before - removed.duration_seconds)


def test_duration_never_goes_negative() -> None:
    radio_queue = queue(3)
    for index in range(3):
        radio_queue.remove(f"TF-{index:05d}", force=True)
    assert radio_queue.total_seconds() == 0.0
    assert radio_queue.ready_seconds() == 0.0


def test_marking_ready_uses_the_measured_duration_not_the_requested_one() -> None:
    """§24 exists because providers do not always deliver what was asked for."""
    radio_queue = RadioQueue()
    radio_queue.append(entry(0, duration=200.0, ready=False))
    radio_queue.mark_ready("TF-00000", audio_path="/a.flac", duration_seconds=183.5)
    assert radio_queue.ready_seconds() == pytest.approx(183.5)


# ---------------------------------------------------------------- duplicates


def test_a_duplicate_track_id_is_refused() -> None:
    """Two slots sharing an id corrupt play history and the §11 diversity window."""
    radio_queue = queue(2)
    with pytest.raises(QueueError, match="already queued"):
        radio_queue.append(entry(0))
    assert radio_queue.stats.rejected_duplicates == 1


def test_duplicates_can_be_allowed_explicitly() -> None:
    radio_queue = RadioQueue(allow_duplicate_tracks=True)
    radio_queue.append(entry(0))
    radio_queue.append(entry(0))
    assert len(radio_queue) == 2


def test_a_refused_duplicate_leaves_the_queue_untouched() -> None:
    radio_queue = queue(3)
    before = [e.track_id for e in radio_queue.snapshot().entries]
    with pytest.raises(QueueError):
        radio_queue.append(entry(1))
    assert [e.track_id for e in radio_queue.snapshot().entries] == before


# ---------------------------------------------------------------- protection


def test_a_protected_slot_refuses_automatic_removal() -> None:
    radio_queue = queue(5, locked_slots=2)
    with pytest.raises(ProtectedItemError, match="cannot be removed automatically"):
        radio_queue.remove("TF-00000")
    assert radio_queue.stats.protected_refusals == 1
    assert radio_queue.contains("TF-00000")


def test_the_refusal_names_the_lock_and_its_reason() -> None:
    """An operator who cannot see why a refusal happened files a bug about it."""
    radio_queue = queue(5, locked_slots=2)
    with pytest.raises(ProtectedItemError) as caught:
        radio_queue.remove("TF-00000")
    assert "locked" in str(caught.value)
    assert "too close to playback" in str(caught.value)


def test_an_operator_can_force_removal() -> None:
    """§44's destructive actions, which the UI confirms rather than this method."""
    radio_queue = queue(5, locked_slots=2)
    radio_queue.remove("TF-00000", force=True)
    assert not radio_queue.contains("TF-00000")


def test_removing_something_absent_says_so() -> None:
    with pytest.raises(QueueError, match="not in the queue"):
        RadioQueue().remove("TF-99999")


# ---------------------------------------------------------------- transactions


def test_a_failed_mutation_restores_the_previous_queue() -> None:
    """A half-applied replan renders in the §41 panel, so it looks deliberate."""
    radio_queue = queue(5)
    before = [e.track_id for e in radio_queue.snapshot().entries]

    with pytest.raises(RuntimeError), radio_queue.transaction():
        radio_queue._entries.pop()
        radio_queue._entries.pop()
        raise RuntimeError("replan blew up halfway")

    assert [e.track_id for e in radio_queue.snapshot().entries] == before
    assert radio_queue.stats.rolled_back == 1


def test_a_rollback_restores_the_playing_slot_too() -> None:
    radio_queue = queue(4)
    playing = radio_queue.begin_playing(FIXED_NOW)

    with pytest.raises(RuntimeError), radio_queue.transaction():
        radio_queue._playing = None
        raise RuntimeError("boom")

    assert radio_queue.playing is playing


def test_locks_are_recomputed_after_a_successful_transaction() -> None:
    radio_queue = queue(6, locked_slots=2, semi_locked_slots=1)
    with radio_queue.transaction():
        radio_queue._entries.reverse()
    assert radio_queue.snapshot().entries[0].lock_level is QueueLockLevel.LOCKED


# ---------------------------------------------------------------- availability


def test_an_unavailable_slot_is_kept_rather_than_silently_dropped() -> None:
    """Removing it would make a disk fault look like a short queue."""
    radio_queue = queue(3)
    radio_queue.mark_unavailable("TF-00001", reason="file missing")
    assert radio_queue.contains("TF-00001")
    entry_now = radio_queue.get("TF-00001")
    assert entry_now is not None
    assert entry_now.readiness is ReadinessState.UNAVAILABLE
    assert not entry_now.is_playable


def test_an_unavailable_slot_is_excluded_from_the_ready_buffer() -> None:
    radio_queue = queue(3)
    before = radio_queue.ready_seconds()
    radio_queue.mark_unavailable("TF-00001", reason="file missing")
    assert radio_queue.ready_seconds() < before


def test_unplayable_head_slots_can_be_identified_and_dropped() -> None:
    radio_queue = queue(4)
    radio_queue.mark_unavailable("TF-00000", reason="gone")
    radio_queue.mark_unavailable("TF-00001", reason="gone")
    skipped = radio_queue.skipped_unplayable()
    assert [e.track_id for e in skipped] == ["TF-00000", "TF-00001"]
    dropped = radio_queue.drop_unplayable_head()
    assert len(dropped) == 2
    assert len(radio_queue) == 2


def test_a_pending_head_is_not_dropped_as_unplayable() -> None:
    """Pending means "not yet", not "never". Dropping it would discard live work."""
    radio_queue = RadioQueue()
    radio_queue.append(entry(0, ready=False))
    assert radio_queue.drop_unplayable_head() == ()
    assert len(radio_queue) == 1


# ---------------------------------------------------------------- restoration (§75)


def test_restoring_rebuilds_the_queue_in_order() -> None:
    radio_queue = RadioQueue(locked_slots=2, semi_locked_slots=1)
    restored = [entry(index, ready=True) for index in range(5)]
    radio_queue.restore(restored)
    assert [e.track_id for e in radio_queue.snapshot().entries] == [
        f"TF-{i:05d}" for i in range(5)
    ]
    assert radio_queue.ready_seconds() == pytest.approx(5 * 200.0)


def test_restoring_recomputes_locks_rather_than_trusting_persisted_ones() -> None:
    """A lock level is a function of position, so a persisted one is a stale answer."""
    from dataclasses import replace

    stale = [
        replace(entry(index, ready=True), lock_level=QueueLockLevel.REPLACEABLE)
        for index in range(5)
    ]
    radio_queue = RadioQueue(locked_slots=2, semi_locked_slots=1)
    radio_queue.restore(stale)
    entries = radio_queue.snapshot().entries
    assert entries[0].lock_level is QueueLockLevel.LOCKED
    assert entries[2].lock_level is QueueLockLevel.SEMI_LOCKED
    assert entries[4].lock_level is QueueLockLevel.REPLACEABLE


def test_restoring_carries_an_operator_pin_through() -> None:
    """A pin is a human decision, not a derived value, so recomputation must not clear it."""
    from dataclasses import replace

    pinned = replace(
        entry(4, ready=True),
        lock_level=QueueLockLevel.OPERATOR_PINNED,
        lock_reason="operator pinned before the restart",
    )
    radio_queue = RadioQueue(locked_slots=2, semi_locked_slots=1)
    radio_queue.restore([entry(i, ready=True) for i in range(4)] + [pinned])
    assert radio_queue.get("TF-00004").lock_level is QueueLockLevel.OPERATOR_PINNED  # type: ignore[union-attr]


def test_restoring_drops_a_duplicate_rather_than_accepting_it() -> None:
    """A persisted queue holding one track twice is corruption, and importing it spreads it."""
    radio_queue = RadioQueue()
    radio_queue.restore([entry(0, ready=True), entry(1, ready=True), entry(0, ready=True)])
    ids = [e.track_id for e in radio_queue.snapshot().entries]
    assert ids == ["TF-00000", "TF-00001"]


def test_restoring_reinstates_the_playing_track() -> None:
    radio_queue = RadioQueue()
    playing = entry(9, ready=True)
    radio_queue.restore([entry(0, ready=True)], playing=playing)
    assert radio_queue.playing is not None
    assert radio_queue.playing.track_id == "TF-00009"
    assert radio_queue.contains("TF-00009")


# ---------------------------------------------------------------- snapshots


def test_a_snapshot_does_not_change_when_the_queue_does() -> None:
    """The §41 panel must render one instant, not a moving target."""
    radio_queue = queue(3)
    snapshot = radio_queue.snapshot()
    radio_queue.remove("TF-00002", force=True)
    assert snapshot.depth == 3
    assert radio_queue.snapshot().depth == 2


def test_snapshot_indexing_is_bounds_safe() -> None:
    snapshot = queue(2).snapshot()
    assert snapshot.at(0) is not None
    assert snapshot.at(5) is None
    assert snapshot.at(-1) is None


def test_the_tier_defaults_to_scheduled() -> None:
    """Tiers 2 and 3 are set explicitly by the emergency manager (§33)."""
    assert queue(1).snapshot().entries[0].tier is PlayoutTier.SCHEDULED
