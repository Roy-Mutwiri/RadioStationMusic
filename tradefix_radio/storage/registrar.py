"""Registering station-owned audio: the bytes and the row, together (§36, §37, ADR-07).

The one place a file becomes *owned*. Everything the station keeps now arrives through
:meth:`FileRegistrar.register`, which stages, verifies, hashes, moves and records in a
fixed order, so that the database is the authority on what exists rather than a cache of
what someone remembered to write down.

**Why this exists.** Before B5 the station wrote raw audio to ``generated/<id>.wav`` and
masters to ``generated/mastered/`` directly, and `TrackFileRepository` had no production
caller at all: 328 files, 8.68 GB, zero rows. Retention could not run, because it could not
tell a master from a discarded render, and the library reported "Audio on disk" for every
track by counting rows that were never written.

**The ordering, and why it is this way round.** File operations and database writes fail
independently, so one of them has to go second and be the one that can be retried. The
order here is:

1. write (or move) into a ``.partial`` staging name beside the destination;
2. verify the bytes decode, and measure them — not the request, the file;
3. hash;
4. atomically rename into place;
5. insert the row.

A crash before (4) leaves a ``.partial`` file, which is identifiable and sweepable and
never looks like real audio. A crash between (4) and (5) leaves a real file with no row —
an orphan, which ``tradefix storage audit`` reports and which is recoverable because the
path encodes the track and role. The reverse failure, a row claiming bytes that are not
there, is the one this order makes impossible, and it is the one that matters: a row is
what the queue and the retention planner trust.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from tradefix_radio.core.errors import AudioError
from tradefix_radio.storage.paths import FileRole, StoragePaths

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

    from tradefix_radio.persistence.repositories.files import TrackFileRepository

_log = structlog.get_logger(__name__)

#: Read size for hashing. Large enough that a 50 MB master is a handful of reads, small
#: enough not to hold a whole file in memory on a machine already running a GPU model.
_HASH_CHUNK = 1 << 20


class StorageError(AudioError):
    """A file could not be taken into station ownership."""


@dataclass(frozen=True)
class RegisteredFile:
    """What was recorded, returned so the caller never re-derives the path."""

    track_id: str
    role: FileRole
    path: Path
    size_bytes: int
    sha256: str
    duration_seconds: float | None
    sample_rate: int | None
    channels: int | None

    @property
    def megabytes(self) -> float:
        return self.size_bytes / 1e6


def sha256_of(path: Path) -> str:
    """Hash a file in chunks.

    Computed once, at registration. §36's integrity audit re-reads it deliberately and
    rarely; doing it on every playback would add a full file read to the one path that
    must never stall.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class FileRegistrar:
    """Takes ownership of audio: verifies it, moves it into place, records it."""

    def __init__(self, paths: StoragePaths) -> None:
        self._paths = paths

    @property
    def paths(self) -> StoragePaths:
        return self._paths

    def _place_and_measure(
        self,
        *,
        track_id: str,
        role: FileRole,
        source: Path,
        now: datetime,
        move: bool,
        verify_audio: bool,
        destination: Path | None,
    ) -> RegisteredFile:
        """The blocking half of registration. Runs in a worker thread.

        Returns a `RegisteredFile` describing what is now on disk; the caller writes the
        row. Split out so that the ordering guarantee in this module's docstring is
        readable in one place and so the I/O never touches the event loop.
        """
        if not source.is_file():
            raise StorageError(f"{role.value} source for {track_id} is missing: {source}")

        target = Path(destination) if destination is not None else self._paths.path_for(
            role, track_id, now, extension=source.suffix.lstrip(".")
        )
        self._paths.ensure_parent(target)
        staging = self._paths.staging(track_id, f".{role.value}{source.suffix}")
        self._paths.ensure_parent(staging)

        # 1. Land the bytes in staging.
        try:
            _atomic_place(source, staging, move=move)
        except OSError as error:
            raise StorageError(
                f"could not stage {source} for {track_id}: {error}"
            ) from error

        # 2. Read them back. Verification happens on the staged copy, so a file that does
        # not decode never occupies its final name.
        duration = sample_rate = channels = None
        if verify_audio and role.is_audio:
            try:
                from tradefix_radio.audio.io import read_info  # noqa: PLC0415

                info = read_info(staging)
                duration = float(info.duration_seconds)
                sample_rate = int(info.sample_rate)
                channels = int(info.channels)
            except (AudioError, OSError) as error:
                # The staged file stays put under its `.partial` name. It is evidence and
                # it cannot be mistaken for playable audio.
                raise StorageError(
                    f"{role.value} for {track_id} did not decode: {error}"
                ) from error
            if duration <= 0.0:
                raise StorageError(
                    f"{role.value} for {track_id} decoded to {duration}s of audio"
                )

        # 3. Measure.
        try:
            digest = sha256_of(staging)
            size_bytes = staging.stat().st_size
        except OSError as error:
            raise StorageError(f"could not measure {staging}: {error}") from error

        # 4. Into place. `Path.replace` is atomic within a filesystem, which is why
        # staging sits beside the destination rather than in a system temp directory.
        try:
            staging.replace(target)
        except OSError as error:
            raise StorageError(f"could not place {target}: {error}") from error

        return RegisteredFile(
            track_id=track_id,
            role=role,
            path=target,
            size_bytes=size_bytes,
            sha256=digest,
            duration_seconds=duration,
            sample_rate=sample_rate,
            channels=channels,
        )

    async def register(
        self,
        *,
        repository: TrackFileRepository,
        track_id: str,
        role: FileRole,
        source: Path,
        now: datetime,
        move: bool = True,
        verify_audio: bool = True,
        retain_forever: bool = False,
        destination: Path | None = None,
    ) -> RegisteredFile:
        """Bring one file under station ownership.

        ``move`` copies instead when false, for a source the station does not own — a
        curated emergency reserve clip should not vanish from the operator's directory
        because the station indexed it.

        Raises :class:`StorageError` rather than returning a sentinel. A caller that
        cannot register its output has not produced a usable track, and letting it
        continue is how an unregistered file reaches the queue.
        """
        source = Path(source)
        # Steps 1-4 are filesystem work: a move, a decode, a hash of up to ~50 MB and a
        # rename. Run off the event loop, because the loop that would otherwise be blocked
        # is the one pacing the audio sink, and §86 does not accept "the broadcast
        # stuttered while we checksummed a file".
        placed = await asyncio.to_thread(
            self._place_and_measure,
            track_id=track_id,
            role=role,
            source=source,
            now=now,
            move=move,
            verify_audio=verify_audio,
            destination=destination,
        )
        target = placed.path
        duration = placed.duration_seconds
        sample_rate = placed.sample_rate
        channels = placed.channels
        digest = placed.sha256
        size_bytes = placed.size_bytes

        # 5. The row. If this raises, the caller's transaction rolls back and the file is
        # an orphan that `storage audit` will find — recoverable, and visible. The reverse
        # order would leave a row promising audio that is not there, which the queue would
        # believe.
        try:
            await repository.add(
                track_id=track_id,
                role=role,
                path=target,
                size_bytes=size_bytes,
                file_format=target.suffix.lstrip(".").lower(),
                now=now,
                sample_rate=sample_rate,
                channels=channels,
                sha256=digest,
                retain_forever=retain_forever,
            )
        except Exception:
            _log.error(
                "storage.row_failed_after_place",
                track_id=track_id,
                role=role.value,
                path=str(target),
                detail="bytes are on disk with no row; `tradefix storage audit` will report it",
            )
            raise

        _log.info(
            "storage.registered",
            track_id=track_id,
            role=role.value,
            path=str(target),
            mb=round(size_bytes / 1e6, 2),
            seconds=None if duration is None else round(duration, 2),
        )
        return RegisteredFile(
            track_id=track_id,
            role=role,
            path=target,
            size_bytes=size_bytes,
            sha256=digest,
            duration_seconds=duration,
            sample_rate=sample_rate,
            channels=channels,
        )

    def sweep_staging(self, *, older_than_seconds: float, now_timestamp: float) -> int:
        """Remove abandoned `.partial` files. Returns how many went.

        Only files old enough to be certain nothing is mid-write: a staging file younger
        than the threshold may belong to a generation in flight on another task.
        """
        directory = self._paths.staging_dir
        if not directory.is_dir():
            return 0
        removed = 0
        for path in directory.glob("*.partial"):
            try:
                if now_timestamp - path.stat().st_mtime < older_than_seconds:
                    continue
                path.unlink()
                removed += 1
            except OSError:
                _log.warning("storage.staging_unremovable", path=str(path))
        return removed


def _atomic_place(source: Path, staging: Path, *, move: bool) -> None:
    """Get ``source`` to ``staging``, preferring a rename and falling back to a copy."""
    import shutil  # noqa: PLC0415 - only needed on the cross-filesystem path

    if not move:
        shutil.copy2(source, staging)
        return
    try:
        source.replace(staging)
    except OSError:
        # Different filesystem: ACE-Step writes to its own cache directory, which may not
        # be the station's disk. Copy then remove — not atomic, but the only option across
        # devices, and the `.partial` name still means a crash cannot leave something that
        # looks like a finished file.
        shutil.copy2(source, staging)
        source.unlink(missing_ok=True)


__all__ = ["FileRegistrar", "RegisteredFile", "StorageError", "sha256_of"]
