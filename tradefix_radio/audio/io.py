"""Audio file read and write (§19, §25, §36).

A thin layer over ``soundfile``, existing for three reasons rather than as a wrapper for its
own sake:

**One place that knows about subtypes.** FLAC cannot store float samples, WAV can, and the
right integer depth differs between a working master and a raw provider dump. Spreading that
knowledge across callers guarantees one of them writes a 16-bit master eventually.

**Writes are atomic.** A master is written to a temporary file in the same directory and
renamed. Without that, a crash mid-write (§75) leaves a truncated file that is *present* —
so the retention sweeper keeps it, the queue believes the track is ready, and playout hits a
read error at the moment it is on air. Atomic rename makes "the file exists" mean "the file
is complete".

**Failures name the file.** ``soundfile`` raises ``RuntimeError`` with libsndfile's message
and no path. At 3 a.m. on track 4 000 the path is the only part that matters.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np
import soundfile as sf

from tradefix_radio.audio.pcm import SAMPLE_DTYPE, AudioBuffer
from tradefix_radio.core.errors import AudioError

#: Subtype per container. 24-bit for anything that airs: 16-bit is audibly adequate but
#: leaves no headroom for the §25 mastering chain to work in, and the storage difference is
#: irrelevant next to the retention policy that deletes these files anyway.
_DEFAULT_SUBTYPE: Final[dict[str, str]] = {
    "FLAC": "PCM_24",
    "WAV": "FLOAT",
    "OGG": "VORBIS",
    "AIFF": "PCM_24",
}

#: Extension to libsndfile container name.
_FORMAT_BY_SUFFIX: Final[dict[str, str]] = {
    ".flac": "FLAC",
    ".wav": "WAV",
    ".ogg": "OGG",
    ".aiff": "AIFF",
    ".aif": "AIFF",
}


@dataclass(frozen=True)
class AudioFileInfo:
    """Metadata read without decoding the samples."""

    path: Path
    frames: int
    sample_rate: int
    channels: int
    format: str
    subtype: str

    @property
    def duration_seconds(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0


def format_for(path: Path) -> str:
    suffix = path.suffix.lower()
    try:
        return _FORMAT_BY_SUFFIX[suffix]
    except KeyError as exc:
        raise AudioError(
            f"unsupported audio extension {suffix!r} for {path}; "
            f"supported: {', '.join(sorted(_FORMAT_BY_SUFFIX))}"
        ) from exc


def read_audio(path: Path) -> AudioBuffer:
    """Read a whole file as float32 PCM."""
    try:
        data, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    except (sf.LibsndfileError, RuntimeError, OSError) as exc:
        raise AudioError(f"cannot read audio file {path}: {exc}") from exc
    return AudioBuffer(np.asarray(data, dtype=SAMPLE_DTYPE), int(sample_rate))


def read_info(path: Path) -> AudioFileInfo:
    """Read metadata only.

    Used by QC and by the retention sweeper, both of which need the duration of thousands
    of files and none of their samples.
    """
    try:
        info = sf.info(str(path))
    except (sf.LibsndfileError, RuntimeError, OSError) as exc:
        raise AudioError(f"cannot read audio metadata from {path}: {exc}") from exc
    return AudioFileInfo(
        path=path,
        frames=int(info.frames),
        sample_rate=int(info.samplerate),
        channels=int(info.channels),
        format=str(info.format),
        subtype=str(info.subtype),
    )


def write_audio(
    path: Path,
    buffer: AudioBuffer,
    *,
    subtype: str | None = None,
    atomic: bool = True,
) -> Path:
    """Write ``buffer`` to ``path``, creating parent directories.

    ``atomic`` writes to a sibling temporary file and renames. Keep it on for anything the
    rest of the system will look for; the only reason to turn it off is a sink streaming
    incrementally to a file it owns, where there is no "complete" state to protect.

    Samples are **clipped** on the way out. An integer subtype wraps on overflow rather than
    saturating, which turns a 1.02 peak into full-scale noise of the opposite sign — a far
    worse artefact than the limiting, and one that sounds like a decoder bug rather than
    like loud audio.
    """
    container = format_for(path)
    chosen = subtype or _DEFAULT_SUBTYPE.get(container, "PCM_24")
    if not sf.check_format(container, chosen):
        raise AudioError(
            f"{container} does not support subtype {chosen!r} (writing {path})"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    data = buffer.clipped().samples
    target = path.with_name(f".{path.name}.partial") if atomic else path

    try:
        sf.write(
            str(target),
            data,
            buffer.sample_rate,
            subtype=chosen,
            format=container,
        )
        if atomic:
            target.replace(path)
    except (sf.LibsndfileError, RuntimeError, OSError) as exc:
        if atomic:
            # Leaving a stray .partial behind would accumulate one per failure and never
            # be reclaimed, since the sweeper only knows about registered track files.
            target.unlink(missing_ok=True)
        raise AudioError(f"cannot write audio file {path}: {exc}") from exc
    return path


__all__ = [
    "AudioFileInfo",
    "format_for",
    "read_audio",
    "read_info",
    "write_audio",
]
