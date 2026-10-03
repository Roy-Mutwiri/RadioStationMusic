"""Where generated audio lives, and for how long (§7.27).

Real generation produces substantially more disk than the mock did. §7.27 asks for four
locations with different lifetimes, and the lifetimes are the point — the directories are
just where the policy becomes visible.

====================  ==========================================================
``raw/``              what the model produced, before mastering
``masters/``          the approved broadcast master
``rejected/``         raw audio that failed the pipeline, kept briefly for triage
``quarantine/``       raw audio that was *broken*, kept longer because it is a bug
====================  ==========================================================

The retention rule that matters most is the one §7.27 states last and this module treats as
absolute: **never delete audio currently referenced by the queue.** Everything else is a
policy that can be tuned; that one is the difference between reclaiming disk and causing
dead air. It is enforced here rather than left to the caller, because the caller is a
retention sweep running on a timer at 4 a.m. and nobody will be watching.
"""

from __future__ import annotations

import enum
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable
    from collections.abc import Set as AbstractSet
    from datetime import datetime

_log = structlog.get_logger(__name__)

__all__ = [
    "GenerationStorage",
    "RetentionPolicy",
    "RetentionSweep",
    "TrackStage",
]


class TrackStage(str, enum.Enum):
    """Which of the four directories a file belongs in."""

    RAW = "raw"
    MASTER = "masters"
    REJECTED = "rejected"
    QUARANTINE = "quarantine"


@dataclass(frozen=True)
class RetentionPolicy:
    """How long each stage is kept.

    Defaults chosen for what each class of file is *for*, not for a uniform number:

    * **Raw** is an input that has already been consumed once the master exists. Two days
      is enough to re-master after a mastering bug without keeping every render forever.
    * **Rejected** exists for triage. A week covers "the station rejected a lot last night,
      what did it sound like" without accumulating indefinitely.
    * **Quarantine** is a bug report. Thirty days, because a broken render is the rarest and
      most valuable artefact here and deleting it loses the only evidence.
    * **Masters** are kept by the station's own retention (§36), not by this module. Setting
      a number here would create a second authority over the same files.
    """

    raw_days: float = 2.0
    rejected_days: float = 7.0
    quarantine_days: float = 30.0
    #: Stop deleting once free space is above this. A sweep is for reclaiming disk, and
    #: deleting evidence on a disk with 400 GB free buys nothing.
    free_space_floor_gb: float = 50.0

    def days_for(self, stage: TrackStage) -> float | None:
        return {
            TrackStage.RAW: self.raw_days,
            TrackStage.REJECTED: self.rejected_days,
            TrackStage.QUARANTINE: self.quarantine_days,
            TrackStage.MASTER: None,
        }[stage]


@dataclass
class RetentionSweep:
    """What a sweep did. Returned rather than logged-and-forgotten so it can be asserted."""

    examined: int = 0
    deleted: int = 0
    bytes_reclaimed: int = 0
    #: Files that were old enough but are still referenced by the queue.
    protected: list[str] = field(default_factory=list)
    skipped_stages: list[str] = field(default_factory=list)

    @property
    def mb_reclaimed(self) -> float:
        return self.bytes_reclaimed / 1e6


#: Audio extensions a sweep will consider. Anything else in these directories is left alone.
_AUDIO_SUFFIXES: Final = frozenset({".wav", ".flac", ".mp3", ".opus", ".ogg", ".aac"})


class GenerationStorage:
    """The four directories, and the rules about removing things from them."""

    def __init__(self, generated_dir: Path, *, policy: RetentionPolicy | None = None) -> None:
        self._root = Path(generated_dir)
        self._policy = policy or RetentionPolicy()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def policy(self) -> RetentionPolicy:
        return self._policy

    def directory(self, stage: TrackStage) -> Path:
        return self._root / stage.value

    def ensure(self) -> None:
        """Create the layout. Idempotent."""
        for stage in TrackStage:
            self.directory(stage).mkdir(parents=True, exist_ok=True)

    def path_for(self, track_id: str, stage: TrackStage, *, suffix: str = ".flac") -> Path:
        """Where this track's file for this stage belongs."""
        return self.directory(stage) / f"{track_id}{suffix}"

    def move_to(self, source: Path, track_id: str, stage: TrackStage) -> Path:
        """Move a file into a stage directory, returning its new path.

        Used when the pipeline's verdict is known: a rejected raw render moves to
        ``rejected/``, a broken one to ``quarantine/``. Moving rather than copying, because
        two copies of a 40 MB render is exactly the disk pressure §7.27 is about.
        """
        destination = self.directory(stage) / f"{track_id}{source.suffix}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() == destination.resolve():
            return destination
        shutil.move(str(source), str(destination))
        _log.info(
            "storage.moved", track_id=track_id, stage=stage.value, path=str(destination)
        )
        return destination

    def usage_bytes(self) -> dict[str, int]:
        """Bytes per stage, for the System page and the §7.27 report."""
        usage: dict[str, int] = {}
        for stage in TrackStage:
            directory = self.directory(stage)
            if not directory.is_dir():
                usage[stage.value] = 0
                continue
            total = 0
            for entry in directory.iterdir():
                if entry.is_file():
                    try:
                        total += entry.stat().st_size
                    except OSError:
                        continue
            usage[stage.value] = total
        return usage

    def sweep(
        self,
        *,
        now: datetime,
        protected_paths: AbstractSet[str] | Iterable[str] = (),
        free_space_gb: float | None = None,
        dry_run: bool = False,
    ) -> RetentionSweep:
        """Delete what is past its retention, and nothing that is in use.

        ``protected_paths`` is every audio path the queue currently references. It is a
        required-in-practice argument rather than an optional one: the caller holds the
        queue and this module does not, so the only way for the rule to be enforced is for
        the caller to supply the list. Passing nothing protects nothing, which is correct
        for a sweep over ``quarantine/`` and catastrophic for one over ``masters/`` — which
        is why masters have no retention here at all.
        """
        result = RetentionSweep()
        protected = {_normalise(path) for path in protected_paths}

        if (
            free_space_gb is not None
            and free_space_gb > self._policy.free_space_floor_gb
        ):
            # Nothing to buy. Deleting triage evidence on a half-empty disk is pure loss.
            result.skipped_stages.append(
                f"all stages: {free_space_gb:.0f} GB free is above the "
                f"{self._policy.free_space_floor_gb:.0f} GB floor"
            )
            return result

        for stage in TrackStage:
            days = self._policy.days_for(stage)
            if days is None:
                # Masters are the station's retention (§36), not this sweep's. Two
                # authorities deleting the same files is how a queued track vanishes.
                result.skipped_stages.append(f"{stage.value}: owned by §36 retention")
                continue

            directory = self.directory(stage)
            if not directory.is_dir():
                continue
            cutoff = now.timestamp() - days * 86_400.0

            for entry in sorted(directory.iterdir()):
                if not entry.is_file() or entry.suffix.lower() not in _AUDIO_SUFFIXES:
                    continue
                result.examined += 1
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                if stat.st_mtime >= cutoff:
                    continue
                if _normalise(entry) in protected:
                    # The rule that is not negotiable.
                    result.protected.append(str(entry))
                    _log.info(
                        "storage.retention_skipped_in_use",
                        path=str(entry),
                        detail="the queue still references this file",
                    )
                    continue
                if dry_run:
                    result.deleted += 1
                    result.bytes_reclaimed += stat.st_size
                    continue
                try:
                    entry.unlink()
                except OSError as error:
                    _log.warning(
                        "storage.retention_delete_failed", path=str(entry), error=str(error)
                    )
                    continue
                result.deleted += 1
                result.bytes_reclaimed += stat.st_size

        _log.info(
            "storage.sweep",
            examined=result.examined,
            deleted=result.deleted,
            mb=round(result.mb_reclaimed, 1),
            protected=len(result.protected),
            dry_run=dry_run,
        )
        return result


def _normalise(path: str | Path) -> str:
    """Compare paths by resolved, case-folded string.

    Case-folded because Windows paths differ in case for the same file, and a sweep that
    compared them literally would delete a queued track whose path the queue happened to
    spell with a different drive-letter case.
    """
    try:
        return str(Path(path).resolve()).casefold()
    except OSError:
        return str(path).casefold()
