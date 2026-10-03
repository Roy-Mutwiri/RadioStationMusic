"""Mastering (§6.9–§6.11).

The goal is **consistent broadcast loudness, not maximum loudness**. §6.9 says so and the
distinction decides the whole design: peak-normalising every track to full scale would make a
quiet ambient piece and a breakout-session trap track equally loud, which destroys exactly the
dynamic that §1 asks the director to create.

The loudness target is a band, not a number
-------------------------------------------
A single -14 LUFS target applied to everything removes the audible difference between a quiet
market and a violent one — the station would *look* reactive on the dashboard and sound flat on
air. So the target slides with the blueprint's intended energy across a configured range, and
the brief explicitly permits this: *"If necessary, define target loudness bands based on
blueprint energy rather than a single exact number. Document the choice."*

The band is narrow on purpose. It is wide enough that a listener can tell a quiet track from a
loud one, and narrow enough that nobody reaches for the volume control between songs. Outside
that window the whole point of normalising is lost.

FFmpeg does the normalisation
-----------------------------
Two-pass `loudnorm`: the first pass measures, the second applies the correction with the
measured values supplied, which is what makes the result accurate rather than an estimate. The
brief prefers FFmpeg here and it is the right call — this is a solved problem with a reference
implementation, and reimplementing a true-peak limiter in numpy would be inventing a worse one.

Everything FFmpeg does **not** do well in one pass — boundary trimming, DC correction — happens
in numpy first, where the operation is exact and inspectable.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import json
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

import numpy as np
import structlog

from tradefix_radio.audio.io import read_audio, write_audio
from tradefix_radio.audio.pcm import AudioBuffer

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradefix_radio.config.schema import MasteringSettings
    from tradefix_radio.contracts.music import MusicBlueprintV1

_log = structlog.get_logger(__name__)

__all__ = [
    "MASTER_CHANNELS",
    "MASTER_SAMPLE_RATE",
    "MasteringOutcome",
    "MasteringResult",
    "ffmpeg_available",
    "master_track",
    "target_loudness_for",
]

#: Canonical master format (§6.11).
#:
#: 48 kHz stereo, matching the playout format exactly so the engine's `conform` is a no-op for
#: an approved track. A master at any other rate would be silently resampled on every play —
#: the "hidden resampling surprise" §6.11 asks to avoid — using the linear interpolator that
#: `audio/format.py` already documents as a compromise.
MASTER_SAMPLE_RATE: Final = 48_000
MASTER_CHANNELS: Final = 2

#: How far the loudness target moves either side of the configured centre, in LU.
#:
#: ±2 LU. The audible difference between a quiet and a loud track is preserved — 4 LU is
#: clearly perceptible — while the loudest and quietest tracks still sit inside a range no
#: listener reaches for the volume control over. Wider starts to defeat normalising at all.
ENERGY_LOUDNESS_SPREAD_LU: Final = 2.0

#: Tolerance on the achieved loudness before final QC calls it a failure, in LU.
#:
#: 1.5 LU. FFmpeg's two-pass loudnorm lands well inside this for ordinary material; the
#: allowance covers short or unusually dynamic tracks where the integrated measurement is
#: genuinely less stable, without being loose enough to pass a track that was not normalised.
LOUDNESS_TOLERANCE_LU: Final = 1.5

#: How close to the true-peak ceiling counts as "the limiter ran out of room", in dB.
#:
#: 0.5 dB. A master sitting this close to the ceiling cannot be made louder without either
#: breaching it or crushing the dynamics further, so an undershoot here is a property of the
#: material rather than a normalisation failure. See :attr:`MasteringResult.peak_constrained`.
_PEAK_CONSTRAINED_MARGIN_DB: Final = 0.5

#: Seconds of the loudnorm filter's own limiter lookahead. FFmpeg's default.
_LOUDNORM_TIMEOUT_SECONDS: Final = 180


class MasteringOutcome(str, enum.Enum):
    """What happened. Distinguished so the pipeline can react differently to each."""

    MASTERED = "mastered"
    #: Mastering is disabled by configuration; the raw file is used unchanged.
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class MasteringResult:
    """The outcome, with the numbers behind it."""

    outcome: MasteringOutcome
    output_path: Path | None
    target_lufs: float
    measured_lufs_before: float | None = None
    measured_lufs_after: float | None = None
    true_peak_dbtp: float | None = None
    true_peak_ceiling_dbtp: float | None = None
    gain_applied_db: float | None = None
    #: The master is quieter than its target *because the true-peak ceiling stopped it*.
    #:
    #: Recorded rather than hidden because it changes what the undershoot means. A track with
    #: a high crest factor cannot reach a loud target without the limiter doing progressively
    #: more damage, and §6.9's rule is consistent loudness, **not** maximum loudness — so the
    #: right response is to accept the quieter master, not to squeeze it. Final QC reads this
    #: flag; without it, every dynamic track would be rejected for a defect it does not have.
    peak_constrained: bool = False
    trimmed_start_seconds: float = 0.0
    trimmed_end_seconds: float = 0.0
    dc_removed: float = 0.0
    duration_before: float = 0.0
    duration_after: float = 0.0
    elapsed_seconds: float = 0.0
    detail: str = ""
    extras: dict[str, object] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.outcome is not MasteringOutcome.FAILED


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def target_loudness_for(
    settings: MasteringSettings, blueprint: MusicBlueprintV1 | None
) -> float:
    """The LUFS target for one track, from the configured centre and the intended energy.

    Energy 0 lands ``ENERGY_LOUDNESS_SPREAD_LU`` below the centre and energy 1 the same
    distance above, linearly. A quiet ambient piece is therefore mastered quieter *on purpose*
    and a high-energy track louder, while both sit within a band a listener will not adjust
    for.

    Returns the configured target unchanged when there is no blueprint — a station identity or
    a reserve track has no intended energy, and inventing one would be worse than neutral.
    """
    if blueprint is None:
        return float(settings.target_lufs)
    energy = float(blueprint.composition.energy)
    offset = (energy - 0.5) * 2.0 * ENERGY_LOUDNESS_SPREAD_LU
    return float(settings.target_lufs) + offset


def _trim_boundaries(
    buffer: AudioBuffer, threshold_dbfs: float, max_trim_seconds: float
) -> tuple[AudioBuffer, float, float]:
    """Remove leading and trailing silence, bounded.

    Bounded because a track that is *mostly* silent is a QC failure, not a trimming job —
    without the cap, a broken render would be trimmed down to a fraction of a second and
    presented as a valid short track.
    """
    samples = np.asarray(buffer.samples, dtype=np.float32)
    if samples.ndim == 1:
        samples = samples.reshape(-1, 1)
    if samples.shape[0] == 0:
        return buffer, 0.0, 0.0

    threshold = 10 ** (threshold_dbfs / 20.0)
    envelope = np.abs(samples).max(axis=1)
    loud = np.flatnonzero(envelope >= threshold)
    if loud.size == 0:
        return buffer, 0.0, 0.0

    rate = buffer.sample_rate
    max_frames = int(max_trim_seconds * rate)
    start = min(int(loud[0]), max_frames)
    end_silence = samples.shape[0] - 1 - int(loud[-1])
    end = min(end_silence, max_frames)
    if start == 0 and end == 0:
        return buffer, 0.0, 0.0

    trimmed = samples[start : samples.shape[0] - end]
    return (
        AudioBuffer(trimmed, rate),
        start / rate,
        end / rate,
    )


def _remove_dc(buffer: AudioBuffer, limit: float = 0.001) -> tuple[AudioBuffer, float]:
    """Subtract a constant offset, when there is one worth subtracting.

    Per channel, because a DC offset can differ between them. Below ``limit`` the offset is
    inaudible and removing it only burns a copy.
    """
    samples = np.asarray(buffer.samples, dtype=np.float32)
    if samples.ndim == 1:
        samples = samples.reshape(-1, 1)
    offsets = samples.mean(axis=0)
    worst = float(np.abs(offsets).max()) if offsets.size else 0.0
    if worst < limit:
        return buffer, 0.0
    return AudioBuffer((samples - offsets).astype(np.float32), buffer.sample_rate), worst


def _conform_to_master_format(buffer: AudioBuffer) -> AudioBuffer:
    """48 kHz stereo (§6.11), using the playout path's own conversion."""
    from tradefix_radio.audio.format import conform  # noqa: PLC0415

    return conform(buffer, sample_rate=MASTER_SAMPLE_RATE, channels=MASTER_CHANNELS)


def _run_ffmpeg(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed executable, argument list, no shell
        args, capture_output=True, text=True, timeout=timeout, check=False
    )


def _measure_loudnorm(source: Path) -> dict[str, float] | None:
    """First pass: measure, so the second pass can correct exactly.

    Two passes rather than one because single-pass loudnorm is a dynamic estimate that drifts
    on material whose loudness varies — which is most music. The brief asks for two-pass where
    accuracy justifies it, and final QC holds the result to ±1.5 LU, which one pass does not
    reliably meet.
    """
    completed = _run_ffmpeg(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(source),
            "-af", "loudnorm=print_format=json",
            "-f", "null", "-",
        ],
        timeout=_LOUDNORM_TIMEOUT_SECONDS,
    )
    text = completed.stderr or ""
    start = text.rfind("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        _log.warning("mastering.measure_failed", detail=text[-200:])
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    measured: dict[str, float] = {}
    for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        try:
            value = float(payload[key])
        except (KeyError, TypeError, ValueError):
            return None
        # loudnorm reports -inf for silence, which is true and unusable as a correction input.
        if not math.isfinite(value):
            return None
        measured[key] = value
    return measured


def _apply_loudnorm(
    source: Path,
    destination: Path,
    *,
    target_lufs: float,
    true_peak_dbtp: float,
    measured: dict[str, float],
    loudness_range: float,
) -> bool:
    """Second pass: apply the correction and write the canonical master."""
    loudnorm = (
        f"loudnorm=I={target_lufs:.2f}:TP={true_peak_dbtp:.2f}:LRA={loudness_range:.1f}"
        f":measured_I={measured['input_i']:.2f}"
        f":measured_TP={measured['input_tp']:.2f}"
        f":measured_LRA={measured['input_lra']:.2f}"
        f":measured_thresh={measured['input_thresh']:.2f}"
        f":offset={measured['target_offset']:.2f}"
        ":linear=true:print_format=summary"
    )
    completed = _run_ffmpeg(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-y", "-i", str(source),
            "-af", loudnorm,
            "-ar", str(MASTER_SAMPLE_RATE),
            "-ac", str(MASTER_CHANNELS),
            # 24-bit PCM: §6.11's canonical master. Lossless, and wide enough that the
            # limiter's output is not re-quantised into audibility.
            "-c:a", "pcm_s24le",
            str(destination),
        ],
        timeout=_LOUDNORM_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        _log.warning("mastering.loudnorm_failed", stderr=completed.stderr[-300:])
        return False
    return destination.is_file() and destination.stat().st_size > 0


def _measure_output(path: Path) -> tuple[float | None, float | None]:
    """Integrated loudness and true peak of the finished master, as (LUFS, dBTP).

    Measured from the file rather than from samples: true peak is an *inter-sample* quantity,
    and a sample-domain maximum systematically under-reports it. FFmpeg's ebur128 does the
    oversampling that makes the number mean what it says.

    Both numbers come from one pass because ebur128 reports them together, and because the
    pair is only meaningful read together — an undershoot is excusable at the ceiling and a
    defect below it, so measuring one without the other cannot answer the question.
    """
    completed = _run_ffmpeg(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
            "-af", "ebur128=peak=true",
            "-f", "null", "-",
        ],
        timeout=_LOUDNORM_TIMEOUT_SECONDS,
    )
    peak: float | None = None
    loudness: float | None = None
    in_summary = False
    for line in (completed.stderr or "").splitlines():
        stripped = line.strip()
        # FFmpeg prefixes the marker with the filter instance — "[Parsed_ebur128_0 @ ..]
        # Summary:" — so this matches the end of the line, not the start.
        if stripped.endswith("Summary:"):
            in_summary = True
        elif stripped.startswith("Peak:"):
            with contextlib.suppress(ValueError, IndexError):
                peak = float(stripped.split()[1])
        # `I:` appears on every progress line too; only the summary's copy is integrated over
        # the whole file, so it is read after the summary marker and not before.
        elif in_summary and stripped.startswith("I:"):
            with contextlib.suppress(ValueError, IndexError):
                loudness = float(stripped.split()[1])
    if loudness is not None and not math.isfinite(loudness):
        loudness = None
    return loudness, peak


def master_track(
    source: Path,
    destination: Path,
    *,
    settings: MasteringSettings,
    blueprint: MusicBlueprintV1 | None = None,
) -> MasteringResult:
    """Master one track. Synchronous; call it from a thread (see :func:`master_track_async`).

    The pre-processing happens in numpy because those operations are exact and inspectable
    there; the loudness work happens in FFmpeg because that is a solved problem with a
    reference implementation.
    """
    import time  # noqa: PLC0415

    started = time.perf_counter()
    target = target_loudness_for(settings, blueprint)

    if not settings.enabled:
        return MasteringResult(
            outcome=MasteringOutcome.SKIPPED,
            output_path=source,
            target_lufs=target,
            detail="mastering is disabled in configuration; the raw file is used as-is",
        )

    try:
        original = read_audio(source)
    except Exception as error:  # noqa: BLE001 - an unreadable source is a mastering failure
        return MasteringResult(
            outcome=MasteringOutcome.FAILED,
            output_path=None,
            target_lufs=target,
            detail=f"the source file could not be read: {error}",
            elapsed_seconds=time.perf_counter() - started,
        )

    duration_before = original.duration_seconds
    working = original
    trimmed_start = trimmed_end = 0.0
    if settings.trim_silence:
        working, trimmed_start, trimmed_end = _trim_boundaries(
            working, settings.silence_threshold_dbfs, settings.max_trim_seconds
        )
    working, dc_removed = _remove_dc(working)
    working = _conform_to_master_format(working)

    if not ffmpeg_available():
        # Without FFmpeg there is no defensible loudness normalisation. Writing the conformed
        # file and calling it mastered would be a lie, so this fails and says why — the
        # pipeline treats it as a mastering failure and `doctor` reports the missing tool.
        return MasteringResult(
            outcome=MasteringOutcome.FAILED,
            output_path=None,
            target_lufs=target,
            trimmed_start_seconds=trimmed_start,
            trimmed_end_seconds=trimmed_end,
            dc_removed=dc_removed,
            duration_before=duration_before,
            detail="FFmpeg is not installed; loudness normalisation cannot be performed",
            elapsed_seconds=time.perf_counter() - started,
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tradefix-master-") as scratch:
        staged = Path(scratch) / "staged.wav"
        write_audio(staged, working, subtype="FLOAT")

        measured = _measure_loudnorm(staged)
        if measured is None:
            return MasteringResult(
                outcome=MasteringOutcome.FAILED,
                output_path=None,
                target_lufs=target,
                trimmed_start_seconds=trimmed_start,
                trimmed_end_seconds=trimmed_end,
                dc_removed=dc_removed,
                duration_before=duration_before,
                detail=(
                    "loudness could not be measured; the track is probably silent or the file "
                    "is malformed"
                ),
                elapsed_seconds=time.perf_counter() - started,
            )

        applied = _apply_loudnorm(
            staged,
            destination,
            target_lufs=target,
            true_peak_dbtp=settings.true_peak_ceiling_dbtp,
            measured=measured,
            # The source's own range, clamped to something loudnorm accepts. Preserving the
            # track's dynamics rather than imposing a house range is the §6.9 rule: the job is
            # consistent loudness, not uniform dynamics.
            loudness_range=max(1.0, min(20.0, measured["input_lra"])),
        )
        if not applied:
            return MasteringResult(
                outcome=MasteringOutcome.FAILED,
                output_path=None,
                target_lufs=target,
                measured_lufs_before=measured["input_i"],
                trimmed_start_seconds=trimmed_start,
                trimmed_end_seconds=trimmed_end,
                dc_removed=dc_removed,
                duration_before=duration_before,
                detail="FFmpeg could not write the mastered file",
                elapsed_seconds=time.perf_counter() - started,
            )

    achieved, true_peak = _measure_output(destination)
    mastered = read_audio(destination)
    ceiling = float(settings.true_peak_ceiling_dbtp)

    # Why the achieved loudness may legitimately fall short of the target.
    #
    # `loudnorm` in linear mode scales by a constant, and falls back to its dynamic limiter
    # when that constant would breach the true-peak ceiling. On material with a high crest
    # factor the limiter reaches the ceiling before it reaches the target — the mock
    # provider's output measures -22.1 LUFS at -3.0 dBTP, so a -12.4 LUFS target would need
    # +9.7 dB and land the peak at +6.7 dBTP. The only ways further are breaching the ceiling
    # or crushing the dynamics, and §6.9 rules out both.
    peak_constrained = (
        achieved is not None
        and true_peak is not None
        and achieved < target - LOUDNESS_TOLERANCE_LU
        and true_peak >= ceiling - _PEAK_CONSTRAINED_MARGIN_DB
    )

    if achieved is None:
        detail = f"normalised towards {target:.1f} LUFS from {measured['input_i']:.1f} LUFS"
    elif peak_constrained:
        detail = (
            f"normalised to {achieved:.1f} LUFS from {measured['input_i']:.1f} LUFS; the "
            f"{target:.1f} LUFS target was not reached because the true-peak ceiling of "
            f"{ceiling:.1f} dBTP was hit first"
        )
    else:
        detail = (
            f"normalised to {achieved:.1f} LUFS from {measured['input_i']:.1f} LUFS "
            f"(target {target:.1f})"
        )

    result = MasteringResult(
        outcome=MasteringOutcome.MASTERED,
        output_path=destination,
        target_lufs=target,
        measured_lufs_before=measured["input_i"],
        measured_lufs_after=achieved,
        true_peak_dbtp=true_peak,
        true_peak_ceiling_dbtp=ceiling,
        # The gain that was actually applied, not the gain that was asked for. With the
        # limiter engaged those differ, and recording the request as though it were the
        # result would make the stored evidence disagree with the file.
        gain_applied_db=(
            None if achieved is None else achieved - measured["input_i"]
        ),
        peak_constrained=peak_constrained,
        trimmed_start_seconds=trimmed_start,
        trimmed_end_seconds=trimmed_end,
        dc_removed=dc_removed,
        duration_before=duration_before,
        duration_after=mastered.duration_seconds,
        elapsed_seconds=time.perf_counter() - started,
        detail=detail,
    )
    _log.info(
        "mastering.completed",
        target_lufs=round(target, 2),
        before_lufs=round(measured["input_i"], 2),
        after_lufs=None if achieved is None else round(achieved, 2),
        true_peak_dbtp=None if true_peak is None else round(true_peak, 2),
        peak_constrained=peak_constrained,
        trimmed=round(trimmed_start + trimmed_end, 2),
        seconds=round(result.elapsed_seconds, 2),
    )
    return result


async def master_track_async(
    source: Path,
    destination: Path,
    *,
    settings: MasteringSettings,
    blueprint: MusicBlueprintV1 | None = None,
) -> MasteringResult:
    """Master off the event loop.

    Mastering shells out to FFmpeg twice and reads the result; on the loop that would block
    every other task, including the playout pump. §26's rule that generation must not run in a
    request thread applies here for the same reason.
    """
    return await asyncio.to_thread(
        master_track, source, destination, settings=settings, blueprint=blueprint
    )
