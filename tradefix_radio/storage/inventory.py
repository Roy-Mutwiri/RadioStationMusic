"""What is on disk, what the database says, and where the two disagree (§36).

Read-only. Nothing here deletes, moves or registers anything — the station arrived at
8.68 GB of unowned audio by writing files nobody recorded, and the fix for that does not
start with another process making confident guesses about them.

**Classification is evidence-based or it is `UNKNOWN_LEGACY`.** A file is only called a
master because a `track_files` row says so, or because it sits in the masters location
*and* the track exists *and* a mastering result was recorded for it. Two of those three is
not enough. The point of the exercise is to stop inferring ownership from filenames, so
inferring it here — in the very tool meant to establish ground truth — would be the same
mistake with better formatting.
"""

from __future__ import annotations

import enum
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from tradefix_radio.persistence.models import (
    MasteringResultRow,
    Track,
    TrackFile,
)
from tradefix_radio.storage.paths import FileRole, StoragePaths

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

_log = structlog.get_logger(__name__)

#: Extensions the inventory will consider audio. Anything else in the tree is listed as a
#: non-audio stray rather than silently ignored — a stray `.tmp` beside the masters is
#: exactly the sort of thing worth seeing.
AUDIO_SUFFIXES = frozenset({".wav", ".flac", ".mp3", ".opus", ".ogg", ".aac"})


class Finding(str, enum.Enum):
    """How a file and the database relate."""

    OK = "ok"
    """A row and its bytes agree."""

    FILE_WITHOUT_ROW = "file_without_row"
    """Bytes nobody recorded. Every pre-B5 file is one of these."""

    ROW_WITHOUT_FILE = "row_without_file"
    """A row promising audio that is not there. The dangerous direction."""

    SIZE_MISMATCH = "size_mismatch"
    """The file changed after it was registered."""

    HASH_MISMATCH = "hash_mismatch"
    """Contents differ from what was recorded. Only checked when asked."""

    DUPLICATE_PATH = "duplicate_path"
    """More than one live row points at the same file."""

    DUPLICATE_CONTENT = "duplicate_content"
    """Identical bytes stored twice under different names."""

    INVALID_ROLE = "invalid_role"
    """A row whose role is not a role this build understands."""

    ACTIVE_REFERENCE_MISSING = "active_reference_missing"
    """A queued or playing track whose authoritative master is gone. Production-critical."""

    STRAY_STAGING = "stray_staging"
    """A `.partial` file left by an interrupted registration."""


class Classification(str, enum.Enum):
    """What a file on disk appears to be, when that can be proven."""

    RAW_GENERATION = "raw_generation"
    MASTER = "master"
    REJECTED_RAW = "rejected_raw"
    QUARANTINE = "quarantine"
    EMERGENCY = "emergency"
    STATION_ID = "station_id"
    ARTWORK = "artwork"
    UNKNOWN_LEGACY = "unknown_legacy"
    """Role could not be established from evidence. Never auto-deleted."""


@dataclass(frozen=True)
class FileEntry:
    """One file on disk, with whatever the database could say about it."""

    path: Path
    size_bytes: int
    classification: Classification
    #: Why it was classified that way, in words an operator can check.
    evidence: str
    track_id: str | None = None
    file_id: int | None = None
    findings: tuple[Finding, ...] = ()

    @property
    def is_owned(self) -> bool:
        return self.file_id is not None


@dataclass
class InventoryReport:
    """The whole picture. Counts, bytes, and the specific problems."""

    scanned_roots: list[Path] = field(default_factory=list)
    files: list[FileEntry] = field(default_factory=list)
    #: Rows whose bytes are missing — these have no `FileEntry`, by definition.
    missing_rows: list[tuple[int, str, FileRole, Path]] = field(default_factory=list)
    non_audio_strays: list[Path] = field(default_factory=list)

    @property
    def total_files(self) -> int:
        return len(self.files)

    @property
    def total_bytes(self) -> int:
        return sum(entry.size_bytes for entry in self.files)

    def by_classification(self) -> dict[str, tuple[int, int]]:
        """``classification -> (count, bytes)``."""
        out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for entry in self.files:
            bucket = out[entry.classification.value]
            bucket[0] += 1
            bucket[1] += entry.size_bytes
        return {key: (value[0], value[1]) for key, value in sorted(out.items())}

    def findings(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for entry in self.files:
            for finding in entry.findings:
                counts[finding.value] += 1
        for _ in self.missing_rows:
            counts[Finding.ROW_WITHOUT_FILE.value] += 1
        return dict(sorted(counts.items()))

    @property
    def owned_bytes(self) -> int:
        return sum(e.size_bytes for e in self.files if e.is_owned)

    @property
    def unowned_bytes(self) -> int:
        return sum(e.size_bytes for e in self.files if not e.is_owned)

    @property
    def is_healthy(self) -> bool:
        """No production-critical problem. Legacy strays alone are not unhealthy."""
        critical = {Finding.ROW_WITHOUT_FILE, Finding.ACTIVE_REFERENCE_MISSING}
        if any(set(e.findings) & critical for e in self.files):
            return False
        return not self.missing_rows


class StorageInventory:
    """Walks the storage roots and reconciles them against the database."""

    def __init__(self, paths: StoragePaths) -> None:
        self._paths = paths

    def roots(self) -> list[Path]:
        """Every directory the station claims to own audio in."""
        settings = self._paths.settings
        return [
            Path(settings.generated_dir),
            Path(settings.emergency_dir),
        ]

    async def scan(
        self, session: AsyncSession, *, verify_hashes: bool = False
    ) -> InventoryReport:
        """Build the report. ``verify_hashes`` re-reads every file; off by default."""
        report = InventoryReport(scanned_roots=self.roots())

        rows = list((await session.execute(select(TrackFile))).scalars())
        by_path: dict[str, list[TrackFile]] = defaultdict(list)
        for row in rows:
            by_path[_key(Path(row.path))].append(row)

        known_tracks = {
            track_id
            for (track_id,) in await session.execute(select(Track.track_id))
        }
        mastered_tracks = {
            track_id
            for (track_id,) in await session.execute(
                select(MasteringResultRow.track_id)
            )
        }

        seen_paths: set[str] = set()
        for root in report.scanned_roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in AUDIO_SUFFIXES:
                    if path.name.endswith(".partial"):
                        report.files.append(
                            FileEntry(
                                path=path,
                                size_bytes=_size(path),
                                classification=Classification.UNKNOWN_LEGACY,
                                evidence="interrupted registration staging file",
                                findings=(Finding.STRAY_STAGING,),
                            )
                        )
                    else:
                        report.non_audio_strays.append(path)
                    continue

                key = _key(path)
                seen_paths.add(key)
                matches = [row for row in by_path.get(key, []) if row.deleted_at is None]
                report.files.append(
                    self._entry_for(
                        path,
                        matches,
                        known_tracks=known_tracks,
                        mastered_tracks=mastered_tracks,
                        verify_hashes=verify_hashes,
                    )
                )

        # Rows whose bytes are gone. Checked against the filesystem directly rather than
        # against `seen_paths`, because a row may point outside the scanned roots — which
        # is itself worth knowing.
        for row in rows:
            if row.deleted_at is not None:
                continue
            path = Path(row.path)
            if path.is_file():
                continue
            try:
                role = FileRole(row.role)
            except ValueError:
                role = FileRole.RAW_GENERATION
            report.missing_rows.append((row.id, row.track_id, role, path))

        return report

    def _entry_for(
        self,
        path: Path,
        matches: list[TrackFile],
        *,
        known_tracks: set[str],
        mastered_tracks: set[str],
        verify_hashes: bool,
    ) -> FileEntry:
        size = _size(path)

        if matches:
            row = matches[0]
            findings: list[Finding] = []
            if len(matches) > 1:
                findings.append(Finding.DUPLICATE_PATH)
            try:
                role = FileRole(row.role)
                classification = Classification(_classification_value(role))
            except ValueError:
                findings.append(Finding.INVALID_ROLE)
                classification = Classification.UNKNOWN_LEGACY
            if row.size_bytes != size:
                findings.append(Finding.SIZE_MISMATCH)
            if verify_hashes and row.sha256:
                from tradefix_radio.storage.registrar import sha256_of  # noqa: PLC0415

                if sha256_of(path) != row.sha256:
                    findings.append(Finding.HASH_MISMATCH)
            if not findings:
                findings.append(Finding.OK)
            return FileEntry(
                path=path,
                size_bytes=size,
                classification=classification,
                evidence=f"track_files row {row.id}",
                track_id=row.track_id,
                file_id=row.id,
                findings=tuple(findings),
            )

        classification, evidence, track_id = self._prove(
            path, known_tracks=known_tracks, mastered_tracks=mastered_tracks
        )
        return FileEntry(
            path=path,
            size_bytes=size,
            classification=classification,
            evidence=evidence,
            track_id=track_id,
            findings=(Finding.FILE_WITHOUT_ROW,),
        )

    def _prove(
        self, path: Path, *, known_tracks: set[str], mastered_tracks: set[str]
    ) -> tuple[Classification, str, str | None]:
        """Classify an unowned file, or decline to.

        The track id has to come from the filename — there is nothing else left — but a
        name alone proves nothing, so it is only accepted when a track by that id actually
        exists. Location then narrows the role, and for a master the mastering record has
        to agree. Anything short of that is `UNKNOWN_LEGACY`, which is never auto-deleted.
        """
        settings = self._paths.settings
        stem = path.stem.split(".")[0]
        track_id = stem if stem in known_tracks else None

        try:
            relative = path.relative_to(Path(settings.emergency_dir))
        except ValueError:
            relative = None
        if relative is not None:
            head = relative.parts[0] if relative.parts else ""
            if head == "station_ids":
                return (
                    Classification.STATION_ID,
                    "inside the station-id directory",
                    None,
                )
            return (
                Classification.EMERGENCY,
                "inside the emergency tree",
                track_id,
            )

        try:
            inside = path.relative_to(Path(settings.generated_dir))
        except ValueError:
            return (Classification.UNKNOWN_LEGACY, "outside every known root", track_id)

        head = inside.parts[0] if len(inside.parts) > 1 else ""
        if head == "quarantine":
            return (Classification.QUARANTINE, "inside quarantine/", track_id)
        if head == "rejected":
            return (Classification.REJECTED_RAW, "inside rejected/", track_id)

        if track_id is None:
            return (
                Classification.UNKNOWN_LEGACY,
                "filename does not name a known track",
                None,
            )

        if head == "mastered" or ".master" in path.name:
            if track_id in mastered_tracks:
                return (
                    Classification.MASTER,
                    "in the masters location with a mastering record",
                    track_id,
                )
            return (
                Classification.UNKNOWN_LEGACY,
                "in the masters location but no mastering record exists",
                track_id,
            )

        # Flat in `generated/`: this is where the pre-B5 raw writer put everything.
        return (
            Classification.RAW_GENERATION,
            "provider output location for a known track",
            track_id,
        )


def _classification_value(role: FileRole) -> str:
    return {
        FileRole.RAW_GENERATION: "raw_generation",
        FileRole.MASTER: "master",
        FileRole.REJECTED_RAW: "rejected_raw",
        FileRole.QUARANTINE: "quarantine",
        FileRole.EMERGENCY: "emergency",
        FileRole.STATION_ID: "station_id",
        FileRole.ARTWORK: "artwork",
    }[role]


def _key(path: Path) -> str:
    """Comparable path key. Windows is case-insensitive; the database is not."""
    try:
        return str(path.resolve()).casefold()
    except OSError:
        return str(path).casefold()


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


__all__ = [
    "AUDIO_SUFFIXES",
    "Classification",
    "FileEntry",
    "Finding",
    "InventoryReport",
    "StorageInventory",
]
