"""Filesystem layout helpers.

All audio paths are derived here rather than composed at call sites, so the
retention sweeper (§36) and the §46 library never disagree with the writer about
where a file is.

Layout::

    generated/
      2026/10/02/
        TF-20261002-00017.raw.wav        provider output, short-lived
        TF-20261002-00017.master.flac    mastered, the thing that airs
    artwork/
      2026/10/02/
        TF-20261002-00017.png
    emergency/
      reserve/TF-20261001-00904.master.flac
      station_ids/id_energy_up_01.flac

Date-partitioned because a single flat directory with 100 000 files is slow to
list on Windows and painful to inspect by hand, and because the retention sweep
can then skip whole day directories by name without stat-ing their contents.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from pathlib import Path

from tradefix_radio.config.schema import PathSettings

#: Track ids are used in filenames, so they must be path-safe. Validated rather
#: than sanitised: a track id that needs sanitising is a bug upstream, and
#: silently rewriting it would break the link between file and database row.
_SAFE_TRACK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class FileRole(str, Enum):
    """What a file is for. Mirrors ``track_files.role``."""

    RAW = "raw"
    """Unprocessed provider output. Deletable as soon as a master exists."""

    MASTER = "master"
    """The mastered file that actually airs."""

    ARTWORK = "artwork"
    """Procedurally generated cover art (§54)."""

    STATION_ID = "station_id"
    """A station identifier clip (§31)."""


def validate_track_id(track_id: str) -> str:
    """Reject a track id that cannot safely appear in a path."""
    if not _SAFE_TRACK_ID.match(track_id):
        raise ValueError(
            f"track_id {track_id!r} is not path-safe; expected 1-64 characters from "
            "[A-Za-z0-9_-]"
        )
    return track_id


def _date_partition(when: datetime) -> Path:
    if when.tzinfo is None:
        raise ValueError("partition datetime must be timezone-aware")
    return Path(f"{when:%Y}") / f"{when:%m}" / f"{when:%d}"


class StoragePaths:
    """Resolves every file location from validated :class:`PathSettings`."""

    def __init__(self, paths: PathSettings) -> None:
        self._paths = paths

    @property
    def settings(self) -> PathSettings:
        return self._paths

    # -- generated audio ---------------------------------------------------

    def raw_audio(self, track_id: str, when: datetime, extension: str = "wav") -> Path:
        validate_track_id(track_id)
        directory = self._paths.generated_dir / _date_partition(when)
        return directory / f"{track_id}.raw.{extension.lstrip('.')}"

    def master_audio(self, track_id: str, when: datetime, extension: str = "flac") -> Path:
        validate_track_id(track_id)
        directory = self._paths.generated_dir / _date_partition(when)
        return directory / f"{track_id}.master.{extension.lstrip('.')}"

    def artwork(self, track_id: str, when: datetime) -> Path:
        validate_track_id(track_id)
        return self._paths.artwork_dir / _date_partition(when) / f"{track_id}.png"

    # -- emergency reserve -------------------------------------------------

    @property
    def emergency_reserve_dir(self) -> Path:
        """Tier 2 audio (§33). Never swept by retention."""
        return self._paths.emergency_dir / "reserve"

    @property
    def station_id_dir(self) -> Path:
        """Station identifier clips (§31). Never swept by retention."""
        return self._paths.emergency_dir / "station_ids"

    def emergency_reserve(self, track_id: str, extension: str = "flac") -> Path:
        validate_track_id(track_id)
        return self.emergency_reserve_dir / f"{track_id}.master.{extension.lstrip('.')}"

    def station_id(self, key: str, extension: str = "flac") -> Path:
        validate_track_id(key)
        return self.station_id_dir / f"{key}.{extension.lstrip('.')}"

    # -- reports -----------------------------------------------------------

    def report(self, name: str) -> Path:
        validate_track_id(name.replace(".", "-"))
        return self._paths.report_dir / name

    # -- helpers -----------------------------------------------------------

    def ensure_parent(self, path: Path) -> Path:
        """Create a file's parent directory. Returns the path for chaining."""
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def is_protected(self, path: Path) -> bool:
        """Whether ``path`` lies in a directory retention must never touch (§36).

        Checked by containment rather than by naming convention so that a file
        placed in the emergency tree is protected regardless of how it was named.
        """
        try:
            resolved = path.resolve()
        except OSError:
            return True  # Cannot resolve it, so refuse to consider deleting it.
        for protected in (self._paths.emergency_dir,):
            try:
                resolved.relative_to(protected.resolve())
            except ValueError:
                continue
            return True
        return False


__all__ = ["FileRole", "StoragePaths", "validate_track_id"]
