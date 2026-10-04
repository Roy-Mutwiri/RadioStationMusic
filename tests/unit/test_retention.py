"""Retention planning (§36, ADR-07, milestone 1.9).

Milestone 1.9's exit test: the planner selects the right deletion candidates and
**never** selects queued, emergency, or unplayed files.

This is the most dangerous code in Phase 1 — it deletes things — so the planner was
deliberately written as a pure function over value objects. That makes it possible
to test adversarial cases (disk pressure while the queue is full of recent tracks)
exhaustively, without creating a single file.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradefix_radio.config.schema import RetentionSettings
from tradefix_radio.core.state_machine import TrackState
from tradefix_radio.storage.paths import FileRole
from tradefix_radio.storage.retention import (
    RetentionAction,
    RetentionCandidate,
    execute_plan,
    plan_retention,
)

NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
GB = 1_000_000_000
MB = 1_000_000


def settings(**overrides: object) -> RetentionSettings:
    base: dict[str, object] = {
        "enabled": True,
        "audio_retention_days": 14,
        "min_free_gb": 20.0,
        "keep_recent_masters": 0,
    }
    base.update(overrides)
    # The config invariant requires alert_free_gb >= min_free_gb (so the alert
    # fires before emergency sweeping). Derive it unless a test sets it, rather
    # than making every raised-floor test restate it.
    if "alert_free_gb" not in base:
        base["alert_free_gb"] = float(base["min_free_gb"]) + 15.0  # type: ignore[arg-type]
    return RetentionSettings.model_validate(base)


def candidate(
    file_id: int = 1,
    *,
    role: FileRole = FileRole.MASTER,
    track_state: TrackState = TrackState.PLAYED,
    played_days_ago: float | None = 30.0,
    size_mb: float = 20.0,
    retain_forever: bool = False,
    in_queue: bool = False,
    already_deleted: bool = False,
    protected_location: bool = False,
    created_days_ago: float = 40.0,
    provenance: str = "production_radio",
) -> RetentionCandidate:
    return RetentionCandidate(
        file_id=file_id,
        track_id=f"TF-20260101-{file_id:05d}",
        role=role,
        path=Path(f"/generated/TF-{file_id}.flac"),
        size_bytes=int(size_mb * MB),
        created_at=NOW - timedelta(days=created_days_ago),
        track_state=track_state,
        last_played_at=None if played_days_ago is None else NOW - timedelta(days=played_days_ago),
        retain_forever=retain_forever,
        in_queue=in_queue,
        already_deleted=already_deleted,
        protected_location=protected_location,
        provenance=provenance,
    )


# ---------------------------------------------------------------- hard guards


def test_queued_file_is_never_deleted() -> None:
    """§36: deleting a queued file would cause dead air."""
    plan = plan_retention([candidate(in_queue=True)], settings(), now=NOW)
    assert plan.deletions == []
    assert plan.decisions[0].action is RetentionAction.KEEP_QUEUED


def test_retain_forever_file_is_never_deleted() -> None:
    """The §33 Tier 2 emergency reserve must survive every sweep."""
    plan = plan_retention([candidate(retain_forever=True)], settings(), now=NOW)
    assert plan.deletions == []
    assert plan.decisions[0].action is RetentionAction.KEEP_PROTECTED


def test_file_in_the_emergency_tree_is_never_deleted() -> None:
    plan = plan_retention([candidate(protected_location=True)], settings(), now=NOW)
    assert plan.deletions == []
    assert plan.decisions[0].action is RetentionAction.KEEP_PROTECTED


def test_station_id_clip_is_never_deleted() -> None:
    """§31 identifiers are part of the station's identity, not disposable output."""
    plan = plan_retention(
        [candidate(role=FileRole.STATION_ID, played_days_ago=900.0)], settings(), now=NOW
    )
    assert plan.deletions == []
    assert plan.decisions[0].action is RetentionAction.KEEP_STATION_ID


@pytest.mark.parametrize(
    "state",
    [
        TrackState.PLANNED,
        TrackState.GENERATING,
        TrackState.GENERATED,
        TrackState.ANALYZING,
        TrackState.APPROVED,
        TrackState.MASTERING,
        TrackState.READY,
        TrackState.QUEUED,
        TrackState.PLAYING,
        TrackState.QUARANTINED,
    ],
)
def test_non_reclaimable_states_are_kept(state: TrackState) -> None:
    """Only aired or dead tracks may have their audio reclaimed."""
    plan = plan_retention([candidate(track_state=state)], settings(), now=NOW)
    assert plan.deletions == []


def test_already_reclaimed_file_is_not_selected_again() -> None:
    """Keeps the sweep idempotent if a previous run was interrupted."""
    plan = plan_retention([candidate(already_deleted=True)], settings(), now=NOW)
    assert plan.deletions == []
    assert plan.decisions[0].action is RetentionAction.KEEP_ALREADY_DELETED


def test_played_track_with_no_air_date_is_kept() -> None:
    """Contradictory data: refuse to infer an age rather than guess and delete."""
    plan = plan_retention([candidate(played_days_ago=None)], settings(), now=NOW)
    assert plan.deletions == []
    assert plan.decisions[0].action is RetentionAction.KEEP_NEVER_PLAYED


def test_guards_outrank_disk_pressure() -> None:
    """The dangerous case: a nearly full disk must not override the guards.

    Disk pressure is exactly the situation where a naive implementation starts
    deleting the queue, so this is asserted directly rather than inferred.
    """
    candidates = [
        candidate(1, in_queue=True, size_mb=500),
        candidate(2, retain_forever=True, size_mb=500),
        candidate(3, role=FileRole.STATION_ID, size_mb=500),
        candidate(4, track_state=TrackState.PLAYING, size_mb=500),
    ]
    plan = plan_retention(
        candidates, settings(min_free_gb=100.0), now=NOW, free_bytes=1 * GB
    )
    assert plan.deletions == []
    assert not plan.satisfies_pressure  # Honest about being unable to free space.


# ---------------------------------------------------------------- normal policy


def test_raw_output_is_reclaimed_first() -> None:
    """Raw provider output is redundant once a master exists."""
    plan = plan_retention(
        [candidate(role=FileRole.RAW_GENERATION, played_days_ago=0.1)], settings(), now=NOW
    )
    assert len(plan.deletions) == 1
    assert plan.deletions[0].action is RetentionAction.DELETE_RAW


@pytest.mark.parametrize(
    "state", [TrackState.REJECTED, TrackState.FAILED, TrackState.CANCELLED]
)
def test_tracks_that_will_never_air_are_reclaimed_regardless_of_age(
    state: TrackState,
) -> None:
    """Their fingerprints and blueprints live in other tables (§36)."""
    plan = plan_retention(
        [candidate(track_state=state, played_days_ago=0.01)], settings(), now=NOW
    )
    assert len(plan.deletions) == 1
    assert plan.deletions[0].action is RetentionAction.DELETE_REJECTED


def test_master_older_than_the_window_is_reclaimed() -> None:
    plan = plan_retention(
        [candidate(played_days_ago=30.0)], settings(audio_retention_days=14), now=NOW
    )
    assert len(plan.deletions) == 1
    assert plan.deletions[0].action is RetentionAction.DELETE_AGED_MASTER


def test_master_inside_the_window_is_kept() -> None:
    plan = plan_retention(
        [candidate(played_days_ago=3.0)], settings(audio_retention_days=14), now=NOW
    )
    assert plan.deletions == []
    assert plan.decisions[0].action is RetentionAction.KEEP_WITHIN_RETENTION


def test_retention_boundary_is_respected_exactly() -> None:
    """A file at exactly the cutoff must be kept, not deleted off-by-one."""
    inside = plan_retention(
        [candidate(played_days_ago=13.99)], settings(audio_retention_days=14), now=NOW
    )
    outside = plan_retention(
        [candidate(played_days_ago=14.01)], settings(audio_retention_days=14), now=NOW
    )
    assert inside.deletions == []
    assert len(outside.deletions) == 1


def test_disabled_retention_deletes_nothing() -> None:
    candidates = [candidate(i, played_days_ago=900.0) for i in range(1, 6)]
    plan = plan_retention(candidates, settings(enabled=False), now=NOW)
    assert plan.deletions == []
    assert len(plan.retained) == 5


# ---------------------------------------------------------------- archive


def test_recent_masters_are_kept_as_a_listening_archive() -> None:
    """§36's intent: keep a browsable archive even past the retention window."""
    candidates = [
        candidate(file_id=i, played_days_ago=float(100 + i)) for i in range(1, 11)
    ]
    plan = plan_retention(candidates, settings(keep_recent_masters=3), now=NOW)
    kept = [d for d in plan.decisions if d.action is RetentionAction.KEEP_RECENT_ARCHIVE]
    assert len(kept) == 3
    # The three most recently aired, not an arbitrary three.
    assert {d.candidate.file_id for d in kept} == {1, 2, 3}


def test_archive_ranking_falls_back_to_creation_date() -> None:
    """A never-aired master still has a creation date to rank by."""
    candidates = [
        candidate(1, played_days_ago=None, created_days_ago=1.0),
        candidate(2, played_days_ago=None, created_days_ago=100.0),
    ]
    plan = plan_retention(candidates, settings(keep_recent_masters=1), now=NOW)
    archived = [
        d for d in plan.decisions if d.action is RetentionAction.KEEP_RECENT_ARCHIVE
    ]
    assert len(archived) == 1
    assert archived[0].candidate.file_id == 1


def test_archive_does_not_protect_rejected_tracks() -> None:
    """The archive is for things that aired, not for failed candidates."""
    candidates = [candidate(1, track_state=TrackState.REJECTED, played_days_ago=0.1)]
    plan = plan_retention(candidates, settings(keep_recent_masters=10), now=NOW)
    assert len(plan.deletions) == 1


# ---------------------------------------------------------------- disk pressure


def test_pressure_pass_reclaims_inside_the_window_when_space_is_short() -> None:
    """Below the free-space floor, recent masters become fair game."""
    candidates = [candidate(i, played_days_ago=float(i), size_mb=5_000) for i in range(1, 11)]
    plan = plan_retention(
        candidates,
        settings(audio_retention_days=90, min_free_gb=30.0),
        now=NOW,
        free_bytes=1 * GB,
    )
    pressure = [d for d in plan.deletions if d.action is RetentionAction.DELETE_PRESSURE]
    assert pressure, "expected the pressure pass to engage"
    assert plan.satisfies_pressure


def test_pressure_pass_takes_the_oldest_first() -> None:
    """Reclaiming the newest track while keeping a six-week-old one is backwards."""
    candidates = [candidate(i, played_days_ago=float(i * 10), size_mb=15_000) for i in range(1, 5)]
    plan = plan_retention(
        candidates,
        settings(audio_retention_days=365, min_free_gb=30.0),
        now=NOW,
        free_bytes=1 * GB,
    )
    deleted_ids = [d.candidate.file_id for d in plan.deletions]
    # file_id 4 aired 40 days ago (oldest), file_id 1 aired 10 days ago (newest).
    assert deleted_ids[0] == 4
    assert deleted_ids == sorted(deleted_ids, reverse=True)


def test_pressure_pass_stops_once_the_floor_is_reached() -> None:
    """It must free enough, not everything."""
    candidates = [candidate(i, played_days_ago=float(i), size_mb=25_000) for i in range(1, 11)]
    plan = plan_retention(
        candidates,
        settings(audio_retention_days=365, min_free_gb=30.0),
        now=NOW,
        free_bytes=10 * GB,
    )
    assert plan.satisfies_pressure
    assert len(plan.deletions) < len(candidates)


def test_no_pressure_pass_when_free_space_is_unknown() -> None:
    """``free_bytes=0`` means "not measured"; it must not trigger mass deletion."""
    candidates = [candidate(i, played_days_ago=1.0) for i in range(1, 6)]
    plan = plan_retention(
        candidates, settings(audio_retention_days=365), now=NOW, free_bytes=0
    )
    assert plan.deletions == []


def test_no_pressure_pass_when_there_is_plenty_of_space() -> None:
    candidates = [candidate(i, played_days_ago=1.0) for i in range(1, 6)]
    plan = plan_retention(
        candidates,
        settings(audio_retention_days=365, min_free_gb=20.0),
        now=NOW,
        free_bytes=500 * GB,
    )
    assert plan.deletions == []


def test_aged_deletions_count_toward_relieving_pressure() -> None:
    """Pressure deletions should only cover the shortfall the age pass left."""
    aged = [candidate(i, played_days_ago=100.0, size_mb=20_000) for i in range(1, 4)]
    recent = [candidate(i + 10, played_days_ago=1.0, size_mb=20_000) for i in range(1, 4)]
    plan = plan_retention(
        aged + recent,
        settings(audio_retention_days=14, min_free_gb=30.0),
        now=NOW,
        free_bytes=1 * GB,
    )
    assert plan.satisfies_pressure
    assert not [d for d in plan.deletions if d.action is RetentionAction.DELETE_PRESSURE]


# ---------------------------------------------------------------- reporting


def test_every_candidate_gets_exactly_one_decision() -> None:
    """No candidate may be silently dropped from the plan."""
    candidates = [
        candidate(1, in_queue=True),
        candidate(2, role=FileRole.RAW_GENERATION),
        candidate(3, played_days_ago=100.0),
        candidate(4, played_days_ago=1.0),
        candidate(5, track_state=TrackState.REJECTED),
        candidate(6, retain_forever=True),
    ]
    plan = plan_retention(candidates, settings(), now=NOW)
    assert len(plan.decisions) == len(candidates)
    assert {d.candidate.file_id for d in plan.decisions} == {1, 2, 3, 4, 5, 6}


def test_every_decision_carries_a_human_readable_reason() -> None:
    """The sweep log must explain itself; §36 behaviour has to be auditable."""
    candidates = [
        candidate(1, in_queue=True),
        candidate(2, role=FileRole.RAW_GENERATION),
        candidate(3, played_days_ago=100.0),
    ]
    plan = plan_retention(candidates, settings(), now=NOW)
    for decision in plan.decisions:
        assert decision.reason
        assert len(decision.reason) > 10


def test_plan_summary_counts_actions() -> None:
    candidates = [candidate(1, role=FileRole.RAW_GENERATION), candidate(2, in_queue=True)]
    summary = plan_retention(candidates, settings(), now=NOW).summary()
    assert summary[RetentionAction.DELETE_RAW.value] == 1
    assert summary[RetentionAction.KEEP_QUEUED.value] == 1


def test_reclaimable_bytes_sums_only_deletions() -> None:
    candidates = [
        candidate(1, role=FileRole.RAW_GENERATION, size_mb=10),
        candidate(2, in_queue=True, size_mb=1_000),
    ]
    plan = plan_retention(candidates, settings(), now=NOW)
    assert plan.reclaimable_bytes == 10 * MB


def test_empty_candidate_list_is_handled() -> None:
    plan = plan_retention([], settings(), now=NOW)
    assert plan.decisions == []
    assert plan.reclaimable_bytes == 0


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        plan_retention([], settings(), now=datetime(2026, 10, 2, 12, 0, 0))


def test_action_delete_predicate_matches_naming() -> None:
    """The ``deletes`` helper must stay in sync with the enum's own names."""
    for action in RetentionAction:
        assert action.deletes == action.value.startswith("delete_")


# ---------------------------------------------------------------- execution


def test_execute_plan_deletes_real_files(tmp_path: Path) -> None:
    victim = tmp_path / "old.flac"
    victim.write_bytes(b"x" * 2048)
    plan = plan_retention(
        [
            RetentionCandidate(
                file_id=1,
                track_id="TF-1",
                role=FileRole.MASTER,
                path=victim,
                size_bytes=2048,
                created_at=NOW - timedelta(days=60),
                track_state=TrackState.PLAYED,
                last_played_at=NOW - timedelta(days=40),
                provenance="production_radio",
            )
        ],
        settings(),
        now=NOW,
    )
    result = execute_plan(plan)
    assert result.files_deleted == 1
    assert result.bytes_reclaimed == 2048
    assert not victim.exists()


def test_dry_run_deletes_nothing(tmp_path: Path) -> None:
    survivor = tmp_path / "old.flac"
    survivor.write_bytes(b"x" * 512)
    plan = plan_retention(
        [
            RetentionCandidate(
                file_id=1,
                track_id="TF-1",
                role=FileRole.MASTER,
                path=survivor,
                size_bytes=512,
                created_at=NOW - timedelta(days=60),
                track_state=TrackState.PLAYED,
                last_played_at=NOW - timedelta(days=40),
                provenance="production_radio",
            )
        ],
        settings(),
        now=NOW,
    )
    result = execute_plan(plan, dry_run=True)
    assert result.files_deleted == 1
    assert survivor.exists()


def test_missing_file_is_counted_separately_not_as_an_error(tmp_path: Path) -> None:
    """An absent file means the desired end state was already reached."""
    plan = plan_retention(
        [
            RetentionCandidate(
                file_id=1,
                track_id="TF-1",
                role=FileRole.MASTER,
                path=tmp_path / "never-existed.flac",
                size_bytes=1024,
                created_at=NOW - timedelta(days=60),
                track_state=TrackState.PLAYED,
                last_played_at=NOW - timedelta(days=40),
                provenance="production_radio",
            )
        ],
        settings(),
        now=NOW,
    )
    result = execute_plan(plan)
    assert result.files_missing == 1
    assert result.files_deleted == 0
    assert result.errors == []


def test_execute_plan_on_an_empty_plan_is_a_no_op() -> None:
    result = execute_plan(plan_retention([], settings(), now=NOW))
    assert result.files_deleted == 0
    assert result.bytes_reclaimed == 0


# ------------------------------------------------- provenance policy (B5)


def test_unknown_provenance_is_never_reclaimed() -> None:
    """The 8.68 GB legacy corpus, stated as a rule.

    Files that predate provenance cannot be proven disposable, and a retention engine
    that treats "I do not know what this is" as "delete it" is one bad migration away
    from erasing the station's catalogue. Ambiguity is conservative here, always.
    """
    plan = plan_retention(
        [candidate(1, provenance="unknown", created_days_ago=900.0,
                   played_days_ago=900.0)],
        settings(),
        now=NOW,
    )
    assert plan.deletions == []
    assert "provenance is unknown" in plan.decisions[0].reason


def test_a_simulation_render_is_reclaimed_quickly() -> None:
    """Very short retention: a simulation never aired to anyone."""
    plan = plan_retention(
        [candidate(1, role=FileRole.RAW_GENERATION, provenance="simulation",
                   created_days_ago=3.0)],
        settings(),
        now=NOW,
    )
    assert plan.deletions, "a three-day-old simulation render was kept"


def test_a_fresh_engineering_render_is_still_inside_its_allowance() -> None:
    """Short, but not zero: the point of engineering output is to be listened to."""
    plan = plan_retention(
        [candidate(1, role=FileRole.RAW_GENERATION, provenance="engineering_test",
                   created_days_ago=0.5)],
        settings(),
        now=NOW,
    )
    assert plan.deletions == []
    assert "allowance" in plan.decisions[0].reason


def test_the_tighter_of_role_and_provenance_wins() -> None:
    """A raw render of a production track is still a raw render.

    Provenance says 60 days, the raw role says 2. Keeping it for 60 because the *track*
    is precious would defeat the point: the master is what is precious, and the raw file
    is the input that produced it.
    """
    plan = plan_retention(
        [candidate(1, role=FileRole.RAW_GENERATION, provenance="production_radio",
                   created_days_ago=10.0)],
        settings(),
        now=NOW,
    )
    assert plan.deletions, "a 10-day-old raw file survived on its track's provenance"


def test_quarantine_outlives_rejected_output() -> None:
    """A broken render is the rarest artefact here; triage evidence is not."""
    kept = plan_retention(
        [candidate(1, role=FileRole.QUARANTINE, provenance="production_radio",
                   created_days_ago=10.0)],
        settings(),
        now=NOW,
    )
    assert kept.deletions == []
    gone = plan_retention(
        [candidate(2, role=FileRole.REJECTED_RAW, provenance="production_radio",
                   created_days_ago=10.0)],
        settings(),
        now=NOW,
    )
    assert gone.deletions, "week-old triage output was kept"
