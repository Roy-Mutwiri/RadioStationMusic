"""Taking audio into station ownership (B5).

The ordering guarantee is the subject: bytes are verified and placed before a row claims
they exist, so the failure that cannot happen is a row promising audio that is not there.
That is the direction that matters, because a row is what the queue and the retention
planner trust — an orphaned *file* is recoverable and visible to `storage audit`, while an
orphaned *row* makes the station queue silence.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from tests.conftest import FIXED_NOW, make_blueprint, make_settings
from tradefix_radio.audio.io import write_audio
from tradefix_radio.audio.pcm import AudioBuffer
from tradefix_radio.persistence.database import Database
from tradefix_radio.persistence.repositories.files import TrackFileRepository
from tradefix_radio.storage.paths import FileRole, StoragePaths
from tradefix_radio.storage.registrar import FileRegistrar, StorageError, sha256_of

pytestmark = pytest.mark.asyncio

TRACK = "TF-20260101-00001"


def tone(path: Path, *, seconds: float = 1.0, rate: int = 8_000) -> Path:
    frames = int(rate * seconds)
    t = np.arange(frames, dtype=np.float32) / rate
    samples = (0.25 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_audio(path, AudioBuffer(samples.reshape(-1, 1), rate))
    return path


@pytest.fixture
async def database(tmp_path: Path):
    settings = make_settings(tmp_path)
    db = Database(settings.database)
    await db.connect()
    await db.create_all()
    try:
        yield db, settings
    finally:
        await db.disconnect()


async def _track(db: Database, track_id: str = TRACK) -> None:
    """A real track row, created the way the station creates one.

    Through `TrackRepository.create` with a real blueprint rather than a hand-built ORM
    row: the table has NOT NULL columns that a hand-built row quietly omits, and a test
    fixture that drifts from the production writer is a test about nothing.
    """
    from tradefix_radio.persistence.repositories.tracks import TrackRepository

    async with db.session() as session:
        await TrackRepository(session).create(
            make_blueprint(track_id, duration_seconds=30),
            now=FIXED_NOW,
            provider="mock",
            model_identifier="mock",
            provenance="engineering_test",
        )


# ----------------------------------------------------------------- the path


async def test_registering_raw_records_measured_metadata(database) -> None:
    """Gate A. The row describes the *file*, not the request that produced it."""
    db, settings = database
    await _track(db)
    registrar = FileRegistrar(StoragePaths(settings.paths))
    source = tone(tmp := Path(settings.paths.root_dir) / "incoming" / "out.wav")
    assert tmp.is_file()

    async with db.session() as session:
        registered = await registrar.register(
            repository=TrackFileRepository(session),
            track_id=TRACK,
            role=FileRole.RAW_GENERATION,
            source=source,
            now=FIXED_NOW,
        )

    assert registered.path.is_file()
    assert registered.sha256 == sha256_of(registered.path)
    assert registered.duration_seconds == pytest.approx(1.0, abs=0.05)
    assert registered.sample_rate == 8_000
    assert registered.channels == 1
    # Moved, not copied: the provider's temp file must not survive as a second copy.
    assert not source.is_file()

    async with db.read_session() as session:
        row = await TrackFileRepository(session).get(TRACK, FileRole.RAW_GENERATION)
    assert row is not None
    assert row.sha256 == registered.sha256
    assert row.size_bytes == registered.size_bytes
    assert row.deleted_at is None


async def test_a_copy_leaves_the_source_alone(database) -> None:
    """`move=False` for audio the station indexes but does not own."""
    db, settings = database
    await _track(db)
    registrar = FileRegistrar(StoragePaths(settings.paths))
    source = tone(Path(settings.paths.root_dir) / "curated" / "clip.wav")

    async with db.session() as session:
        await registrar.register(
            repository=TrackFileRepository(session),
            track_id=TRACK,
            role=FileRole.EMERGENCY,
            source=source,
            now=FIXED_NOW,
            move=False,
        )

    assert source.is_file(), "indexing an operator's file consumed it"


# -------------------------------------------------------------- the failures


async def test_a_missing_source_registers_nothing(database) -> None:
    """No row may exist for bytes that were never there."""
    db, settings = database
    await _track(db)
    registrar = FileRegistrar(StoragePaths(settings.paths))

    with pytest.raises(StorageError, match="missing"):
        async with db.session() as session:
            await registrar.register(
                repository=TrackFileRepository(session),
                track_id=TRACK,
                role=FileRole.RAW_GENERATION,
                source=Path(settings.paths.root_dir) / "nope.wav",
                now=FIXED_NOW,
            )

    async with db.read_session() as session:
        assert await TrackFileRepository(session).get(TRACK, FileRole.RAW_GENERATION) is None


async def test_undecodable_audio_never_reaches_its_final_name(database) -> None:
    """A file that is not audio must not occupy the path the queue would resolve.

    The staged copy is left behind deliberately — it is evidence — but under its
    `.partial` name, where nothing will mistake it for a track.
    """
    db, settings = database
    await _track(db)
    registrar = FileRegistrar(StoragePaths(settings.paths))
    source = Path(settings.paths.root_dir) / "incoming" / "broken.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"this is not a wav file")

    with pytest.raises(StorageError, match="did not decode"):
        async with db.session() as session:
            await registrar.register(
                repository=TrackFileRepository(session),
                track_id=TRACK,
                role=FileRole.RAW_GENERATION,
                source=source,
                now=FIXED_NOW,
            )

    paths = StoragePaths(settings.paths)
    final = paths.raw_audio(TRACK, FIXED_NOW, "wav")
    assert not final.is_file(), "undecodable bytes took the real filename"
    async with db.read_session() as session:
        assert await TrackFileRepository(session).get(TRACK, FileRole.RAW_GENERATION) is None


async def test_a_zero_length_render_is_refused(database) -> None:
    """Decodes, but there is no audio in it. §7.19's 'invalid output'."""
    db, settings = database
    await _track(db)
    registrar = FileRegistrar(StoragePaths(settings.paths))
    source = Path(settings.paths.root_dir) / "incoming" / "empty.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    write_audio(source, AudioBuffer(np.zeros((0, 1), dtype=np.float32), 8_000))

    with pytest.raises(StorageError):
        async with db.session() as session:
            await registrar.register(
                repository=TrackFileRepository(session),
                track_id=TRACK,
                role=FileRole.RAW_GENERATION,
                source=source,
                now=FIXED_NOW,
            )


async def test_a_row_failure_leaves_a_findable_file_not_a_false_claim(
    database,
) -> None:
    """DB insert fails after placement: an orphan file, never an orphan row.

    This is the compensation the ordering buys. The bytes are real and `storage audit`
    reports them as FILE_WITHOUT_ROW; the alternative ordering would leave the database
    asserting that audio exists, which the queue would believe and then fail to play.
    """
    db, settings = database
    await _track(db)
    registrar = FileRegistrar(StoragePaths(settings.paths))
    source = tone(Path(settings.paths.root_dir) / "incoming" / "out.wav")

    class ExplodingRepository(TrackFileRepository):
        async def add(self, **kwargs: object):  # type: ignore[override]
            raise RuntimeError("database is gone")

    with pytest.raises(RuntimeError, match="database is gone"):
        async with db.session() as session:
            await registrar.register(
                repository=ExplodingRepository(session),
                track_id=TRACK,
                role=FileRole.RAW_GENERATION,
                source=source,
                now=FIXED_NOW,
            )

    placed = StoragePaths(settings.paths).raw_audio(TRACK, FIXED_NOW, "wav")
    assert placed.is_file(), "the file vanished, losing the only copy of the render"
    async with db.read_session() as session:
        assert await TrackFileRepository(session).get(TRACK, FileRole.RAW_GENERATION) is None


async def test_staging_files_are_swept_only_once_they_are_cold(database) -> None:
    """A `.partial` younger than the threshold may belong to a write in flight."""
    _db, settings = database
    paths = StoragePaths(settings.paths)
    registrar = FileRegistrar(paths)
    paths.staging_dir.mkdir(parents=True, exist_ok=True)
    stale = paths.staging_dir / "old.partial"
    stale.write_bytes(b"x")
    mtime = stale.stat().st_mtime

    assert registrar.sweep_staging(older_than_seconds=3600, now_timestamp=mtime + 10) == 0
    assert stale.is_file()
    assert registrar.sweep_staging(older_than_seconds=60, now_timestamp=mtime + 3600) == 1
    assert not stale.is_file()
