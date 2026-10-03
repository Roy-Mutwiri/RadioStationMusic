"""Generated-audio storage and retention (§7.27, §7.30).

One rule in this file is absolute and the rest are policy: **never delete audio currently
referenced by the queue.** The tests are weighted accordingly — most of them exist to attack
that rule from a different angle, because a retention sweep runs unattended at 4 a.m. and the
failure mode is dead air.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tradefix_radio.core.clock import UTC
from tradefix_radio.generation.storage import (
    GenerationStorage,
    RetentionPolicy,
    TrackStage,
)

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def write(path: Path, *, age_days: float = 0.0, size: int = 1024) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    stamp = (NOW - timedelta(days=age_days)).timestamp()
    import os

    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def storage(tmp_path: Path) -> GenerationStorage:
    store = GenerationStorage(tmp_path / "generated")
    store.ensure()
    return store


# ------------------------------------------------------------------ layout


def test_the_four_directories_exist(storage: GenerationStorage) -> None:
    for stage in TrackStage:
        assert storage.directory(stage).is_dir()


def test_ensure_is_idempotent(storage: GenerationStorage) -> None:
    storage.ensure()
    storage.ensure()
    assert storage.directory(TrackStage.RAW).is_dir()


def test_moving_a_rejected_render_relocates_rather_than_copies(
    storage: GenerationStorage,
) -> None:
    """Two copies of a 40 MB render is exactly the disk pressure §7.27 is about."""
    source = write(storage.directory(TrackStage.RAW) / "TF-1.flac")
    moved = storage.move_to(source, "TF-1", TrackStage.REJECTED)
    assert moved.is_file()
    assert not source.exists()
    assert moved.parent == storage.directory(TrackStage.REJECTED)


def test_usage_is_reported_per_stage(storage: GenerationStorage) -> None:
    write(storage.directory(TrackStage.RAW) / "a.flac", size=2048)
    write(storage.directory(TrackStage.MASTER) / "b.wav", size=4096)
    usage = storage.usage_bytes()
    assert usage["raw"] == 2048
    assert usage["masters"] == 4096
    assert usage["quarantine"] == 0


# --------------------------------------------------------------- retention


def test_old_raw_audio_is_reclaimed(storage: GenerationStorage) -> None:
    write(storage.directory(TrackStage.RAW) / "old.flac", age_days=10, size=5000)
    result = storage.sweep(now=NOW, free_space_gb=1.0)
    assert result.deleted == 1
    assert result.bytes_reclaimed == 5000


def test_recent_raw_audio_is_kept(storage: GenerationStorage) -> None:
    """The paired negative: retention must not mean "delete everything"."""
    recent = write(storage.directory(TrackStage.RAW) / "new.flac", age_days=0.5)
    result = storage.sweep(now=NOW, free_space_gb=1.0)
    assert result.deleted == 0
    assert recent.is_file()


def test_queued_audio_is_never_deleted_however_old(storage: GenerationStorage) -> None:
    """§7.27's one absolute rule.

    The file is far past every retention window and the disk is nearly full — every signal
    says delete it. It is in the queue, so it stays. This is the test that matters.
    """
    queued = write(storage.directory(TrackStage.RAW) / "on-air.flac", age_days=999)
    result = storage.sweep(now=NOW, protected_paths={str(queued)}, free_space_gb=0.1)
    assert result.deleted == 0
    assert queued.is_file()
    assert str(queued) in result.protected


def test_protection_survives_a_differently_spelled_path(
    storage: GenerationStorage,
) -> None:
    """Windows spells the same path several ways, and the queue may hold any of them.

    A literal string comparison would fail to match `d:\\...` against `D:\\...` and delete a
    queued track. Comparing resolved and case-folded is what makes the rule hold in practice
    rather than only in the test that spells it identically.
    """
    queued = write(storage.directory(TrackStage.RAW) / "on-air.flac", age_days=999)
    oddly_spelled = str(queued).upper()
    result = storage.sweep(
        now=NOW, protected_paths={oddly_spelled}, free_space_gb=0.1
    )
    assert result.deleted == 0
    assert queued.is_file()


def test_masters_are_never_swept_here(storage: GenerationStorage) -> None:
    """§36's retention owns them. Two authorities deleting the same files is how a queued
    track vanishes while both sweeps look correct in isolation."""
    master = write(storage.directory(TrackStage.MASTER) / "ancient.wav", age_days=9999)
    result = storage.sweep(now=NOW, free_space_gb=0.1)
    assert master.is_file()
    assert any("masters" in note for note in result.skipped_stages)


def test_quarantine_is_kept_far_longer_than_rejected(
    storage: GenerationStorage,
) -> None:
    """A broken render is a bug report, and it is the rarest artefact here."""
    rejected = write(storage.directory(TrackStage.REJECTED) / "r.flac", age_days=10)
    quarantined = write(storage.directory(TrackStage.QUARANTINE) / "q.flac", age_days=10)
    storage.sweep(now=NOW, free_space_gb=0.1)
    assert not rejected.exists()
    assert quarantined.is_file()


def test_a_sweep_does_nothing_when_there_is_plenty_of_disk(
    storage: GenerationStorage,
) -> None:
    """Deleting triage evidence on a half-empty disk buys nothing."""
    old = write(storage.directory(TrackStage.RAW) / "old.flac", age_days=999)
    result = storage.sweep(now=NOW, free_space_gb=500.0)
    assert result.deleted == 0
    assert old.is_file()
    assert result.skipped_stages


def test_a_dry_run_reports_without_deleting(storage: GenerationStorage) -> None:
    old = write(storage.directory(TrackStage.RAW) / "old.flac", age_days=999, size=7000)
    result = storage.sweep(now=NOW, free_space_gb=0.1, dry_run=True)
    assert result.deleted == 1
    assert result.bytes_reclaimed == 7000
    assert old.is_file()


def test_non_audio_files_are_left_alone(storage: GenerationStorage) -> None:
    """A sweep over a directory should not remove whatever else is in it."""
    note = write(storage.directory(TrackStage.RAW) / "notes.txt", age_days=999)
    storage.sweep(now=NOW, free_space_gb=0.1)
    assert note.is_file()


def test_the_policy_is_configurable(tmp_path: Path) -> None:
    """Both knobs, together, because they interact.

    The floor gates the whole sweep before any retention window is consulted, so a short
    `raw_days` means nothing on a disk the policy considers roomy. Setting the floor above
    the reported free space is what puts the sweep under pressure.
    """
    aggressive = RetentionPolicy(raw_days=0.0, free_space_floor_gb=100.0)
    storage = GenerationStorage(tmp_path / "g", policy=aggressive)
    storage.ensure()
    write(storage.directory(TrackStage.RAW) / "fresh.flac", age_days=0.01)
    result = storage.sweep(now=NOW, free_space_gb=1.0)
    assert result.deleted == 1
