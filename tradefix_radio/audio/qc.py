"""Audio quality control (§6.1).

Every check returns a name, a status, the measured value, the threshold it was judged against
and a sentence a human can act on. §6.1 forbids collapsing this into one boolean, and the
reason is operational rather than aesthetic: "QC failed" tells an operator to read the logs,
while "integrated loudness -41.2 LUFS against a -40.0 floor" tells them the generator produced
something inaudible.

Two design rules do most of the work here.

**Thresholds consider intent.** §6.1 is explicit that a quiet ambient track must not fail for
being quiet. The blueprint states the energy the director asked for, so the loudness floor and
the dynamics expectations scale with it. A check that ignores intent either rejects good
ambient music or passes broken dance music; there is no single threshold that does both.

**WARN is a real outcome, not a soft FAIL.** A track can be unusual without being broken —
heavy low end, a long intro, wide stereo. Those are recorded, surfaced on the Originality page,
and do not block the broadcast. Only defects that make a track genuinely unfit to air FAIL.

What this module does not do
----------------------------
It does not judge whether the music is *good*. Nothing here can, and a check that claimed to
would be the sort of fabricated authority §86 rules out. It judges whether the file is sound,
audible, correctly formed and consistent with what was asked for.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import structlog

from tradefix_radio.audio.analysis import AudioFeatures

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradefix_radio.config.schema import QualityControlSettings
    from tradefix_radio.contracts.music import MusicBlueprintV1

_log = structlog.get_logger(__name__)

__all__ = ["QcCheck", "QcStage", "QcStatus", "TrackQcResult", "run_audio_qc"]


class QcStatus(str, enum.Enum):
    """Outcome of one check, or of the whole result."""

    PASS = "pass"  # noqa: S105 - a QC verdict, not a credential
    WARN = "warn"
    FAIL = "fail"

    @property
    def blocks_broadcast(self) -> bool:
        return self is QcStatus.FAIL


class QcStage(str, enum.Enum):
    """Which pass produced the result.

    The same checks run twice (§6.10): once on the generator's raw output and again on the
    mastered file. Recording which pass a result came from is what makes "mastering introduced
    clipping" a distinguishable finding rather than a confusing duplicate row.
    """

    RAW = "raw"
    MASTERED = "mastered"


@dataclass(frozen=True)
class QcCheck:
    """One measurement, judged."""

    name: str
    status: QcStatus
    #: ``None`` when the check could not be performed — never a stand-in number.
    value: float | None
    unit: str
    threshold: str
    reason: str

    @property
    def failed(self) -> bool:
        return self.status is QcStatus.FAIL


@dataclass(frozen=True)
class TrackQcResult:
    """Every check for one track, plus the overall verdict."""

    track_id: str
    stage: QcStage
    status: QcStatus
    checks: tuple[QcCheck, ...]
    features: AudioFeatures | None = None
    extras: dict[str, object] = field(default_factory=dict)

    @property
    def failures(self) -> tuple[QcCheck, ...]:
        return tuple(check for check in self.checks if check.status is QcStatus.FAIL)

    @property
    def warnings(self) -> tuple[QcCheck, ...]:
        return tuple(check for check in self.checks if check.status is QcStatus.WARN)

    @property
    def passed(self) -> bool:
        return self.status is not QcStatus.FAIL

    def summary(self) -> str:
        """One line for a log or a UI row."""
        if self.passed and not self.warnings:
            return f"{len(self.checks)} checks passed"
        if self.passed:
            return f"{len(self.warnings)} warning(s): " + "; ".join(
                check.reason for check in self.warnings[:3]
            )
        return "; ".join(check.reason for check in self.failures[:3])


# ----------------------------------------------------------------- thresholds

#: Loudness floor relaxation for deliberately quiet material, in LU.
#:
#: A blueprint at energy 0.1 is asking for something barely there. Judging it against the same
#: floor as a breakout-session trap track would reject exactly the music §1 asks the director
#: to make when the market is asleep.
_QUIET_LOUDNESS_ALLOWANCE: Final = 8.0

#: Crest factor below which a track is suspiciously flat — the signature of heavy limiting or
#: a stuck generator, not of a musical choice.
_MIN_CREST_FACTOR: Final = 1.8

#: Stereo correlation below which the channels are fighting. -0.2 is generous: wide stereo is
#: a legitimate effect, sustained anti-phase is a defect you only hear on a phone speaker.
_MIN_STEREO_CORRELATION: Final = -0.2

#: Mono fold-down loss that indicates real phase cancellation rather than width, in dB.
_MAX_MONO_LOSS_DB: Final = -6.0

#: Longest acceptable gap inside a track, in seconds. Long enough for a real breakdown,
#: short enough that a dropout is caught.
_MAX_INTERNAL_SILENCE: Final = 6.0

#: Boundary silence tolerated before it is worth trimming, in seconds.
_MAX_BOUNDARY_SILENCE: Final = 3.0

#: Fraction of spectral energy below 120 Hz above which the low end is overloaded.
_MAX_LOW_ENERGY_RATIO: Final = 0.75

#: Upper band boundary for the top-end check, in Hz. Must match `analysis._band_ratios`.
_HIGH_BAND_HZ: Final = 8_000.0

#: How much headroom the Nyquist limit needs above the band before the check means anything.
#:
#: 1.1. At exactly 2x the band there is no representable content at all; a little above it
#: there is a sliver, measured through an anti-alias rolloff that attenuates it to nothing.
#: Either way the number says more about the sample rate than about the track.
_NYQUIST_MARGIN: Final = 1.1

#: Fraction of energy above 8 kHz below which the top end is *noted*, not rejected.
#:
#: Tuned against measurement rather than intuition. The first value rejected every track in
#: the fixture set including the clean one, because music built from low sine partials
#: genuinely has almost nothing above 8 kHz - and so does plenty of real dark, warm music.
#: Rejecting on that is precisely the over-simple assumption the brief warns about.
_LOW_HIGH_ENERGY_RATIO: Final = 0.0005

#: Fraction below which the top end has genuinely *collapsed*, which is a decode or render
#: fault rather than a mix choice. Essentially zero: no real signal path produces this.
_COLLAPSED_HIGH_ENERGY_RATIO: Final = 1e-9

#: Discontinuities tolerated before the file is treated as corrupt rather than percussive.
_MAX_DISCONTINUITIES: Final = 32


def _check(
    name: str,
    *,
    ok: bool,
    warn: bool = False,
    value: float | None,
    unit: str,
    threshold: str,
    reason: str,
) -> QcCheck:
    status = QcStatus.PASS if ok else (QcStatus.WARN if warn else QcStatus.FAIL)
    return QcCheck(
        name=name, status=status, value=value, unit=unit, threshold=threshold, reason=reason
    )


def _intended_energy(blueprint: MusicBlueprintV1 | None) -> float:
    """The 0–1 intensity the director asked for, or a neutral 0.5."""
    if blueprint is None:
        return 0.5
    return float(blueprint.composition.energy)


def run_audio_qc(
    *,
    track_id: str,
    features: AudioFeatures,
    settings: QualityControlSettings,
    blueprint: MusicBlueprintV1 | None = None,
    stage: QcStage = QcStage.RAW,
    expected_duration_seconds: float | None = None,
) -> TrackQcResult:
    """Judge one analysed track.

    Takes `AudioFeatures` rather than a file so that QC is a pure function of measurements —
    testable against fabricated features, and incapable of disagreeing with the similarity
    engine about what the audio contains.
    """
    checks: list[QcCheck] = []
    energy = _intended_energy(blueprint)

    # -- structural validity ------------------------------------------------

    checks.append(
        _check(
            "sample_rate",
            ok=features.sample_rate > 0,
            value=float(features.sample_rate),
            unit="Hz",
            threshold="> 0",
            reason=(
                f"{features.sample_rate} Hz"
                if features.sample_rate > 0
                else "the file reports no sample rate; it is not decodable audio"
            ),
        )
    )
    checks.append(
        _check(
            "channel_count",
            ok=features.channels in (1, 2),
            value=float(features.channels),
            unit="channels",
            threshold="1 or 2",
            reason=(
                f"{features.channels} channel(s)"
                if features.channels in (1, 2)
                else f"{features.channels} channels; the playout path handles mono and stereo only"
            ),
        )
    )

    duration_ok = features.duration_seconds >= settings.min_duration_seconds
    checks.append(
        _check(
            "duration",
            ok=duration_ok,
            value=features.duration_seconds,
            unit="s",
            threshold=f">= {settings.min_duration_seconds:.0f}s",
            reason=(
                f"{features.duration_seconds:.1f}s"
                if duration_ok
                else (
                    f"{features.duration_seconds:.1f}s is below the "
                    f"{settings.min_duration_seconds:.0f}s minimum; a short file usually "
                    "means generation was truncated"
                )
            ),
        )
    )

    if expected_duration_seconds and expected_duration_seconds > 0:
        deviation = (
            abs(features.duration_seconds - expected_duration_seconds)
            / expected_duration_seconds
        )
        within = deviation <= settings.max_duration_deviation
        checks.append(
            _check(
                "duration_deviation",
                ok=within,
                warn=not within and deviation <= settings.max_duration_deviation * 2,
                value=deviation,
                unit="fraction",
                threshold=f"<= {settings.max_duration_deviation:.0%}",
                reason=(
                    f"{deviation:.0%} from the requested {expected_duration_seconds:.0f}s"
                    if within
                    else (
                        f"delivered {features.duration_seconds:.0f}s against a requested "
                        f"{expected_duration_seconds:.0f}s ({deviation:.0%} out)"
                    )
                ),
            )
        )

    # -- numerical sanity ---------------------------------------------------
    #
    # NaN and Inf are checked via the peak, which `extract_features` computes over finite
    # samples only — so a non-finite peak means every sample was non-finite.
    finite_peak = math.isfinite(features.peak)
    checks.append(
        _check(
            "finite_samples",
            ok=finite_peak and features.peak >= 0.0,
            value=features.peak if finite_peak else None,
            unit="amplitude",
            threshold="all samples finite",
            reason=(
                "no NaN or Inf samples"
                if finite_peak
                else "the file contains NaN or Inf samples and cannot be broadcast"
            ),
        )
    )

    non_empty = features.duration_seconds > 0 and features.peak > 0
    checks.append(
        _check(
            "non_empty",
            ok=non_empty,
            value=features.peak,
            unit="peak",
            threshold="> 0",
            reason="contains audio" if non_empty else "the file is digitally silent end to end",
        )
    )

    # -- level --------------------------------------------------------------

    checks.append(
        _check(
            "peak_level",
            ok=features.peak <= 1.0,
            value=features.peak,
            unit="amplitude",
            threshold="<= 1.0",
            reason=(
                f"peak {features.peak:.3f}"
                if features.peak <= 1.0
                else f"peak {features.peak:.3f} is above full scale; samples are already clipped"
            ),
        )
    )

    clipping_ok = features.clipped_sample_ratio <= settings.max_clipped_sample_ratio
    checks.append(
        _check(
            "clipping",
            ok=clipping_ok,
            warn=(
                not clipping_ok
                and features.clipped_sample_ratio <= settings.max_clipped_sample_ratio * 5
            ),
            value=features.clipped_sample_ratio,
            unit="fraction",
            threshold=f"<= {settings.max_clipped_sample_ratio:.3%}",
            reason=(
                f"{features.clipped_sample_ratio:.4%} of samples at full scale"
                if clipping_ok
                else f"{features.clipped_sample_ratio:.2%} of samples are clipped"
            ),
        )
    )

    dc_ok = abs(features.dc_offset) <= settings.max_dc_offset
    checks.append(
        _check(
            "dc_offset",
            ok=dc_ok,
            warn=not dc_ok and abs(features.dc_offset) <= settings.max_dc_offset * 3,
            value=features.dc_offset,
            unit="amplitude",
            threshold=f"|offset| <= {settings.max_dc_offset}",
            reason=(
                f"DC offset {features.dc_offset:+.4f}"
                if dc_ok
                else (
                    f"DC offset {features.dc_offset:+.3f} wastes headroom and can thump on "
                    "transitions"
                )
            ),
        )
    )

    checks.append(
        _check(
            "rms",
            ok=features.rms > 0,
            value=features.rms,
            unit="amplitude",
            threshold="> 0",
            reason=f"RMS {features.rms:.4f}" if features.rms > 0 else "RMS is zero",
        )
    )

    # Crest factor only means something once there is signal to measure.
    if features.rms > 1e-6:
        crest_ok = features.crest_factor >= _MIN_CREST_FACTOR
        checks.append(
            _check(
                "crest_factor",
                ok=crest_ok,
                # A warning, never a rejection. Low crest is characteristic of heavily
                # limited music, which is a style, not a fault — but it is worth seeing.
                warn=not crest_ok,
                value=features.crest_factor,
                unit="ratio",
                threshold=f">= {_MIN_CREST_FACTOR}",
                reason=(
                    f"crest factor {features.crest_factor:.2f}"
                    if crest_ok
                    else f"crest factor {features.crest_factor:.2f} is unusually flat for music"
                ),
            )
        )

    # -- loudness -----------------------------------------------------------

    if features.integrated_lufs is None:
        checks.append(
            QcCheck(
                name="integrated_loudness",
                # A missing meter is an environment gap and warns; a meter that ran and found
                # nothing to measure is a dead file and fails. Collapsing the two sent an
                # operator to install software they already had, while a silent track passed.
                status=(
                    QcStatus.FAIL if features.loudness_meter_available else QcStatus.WARN
                ),
                value=None,
                unit="LUFS",
                threshold=f"{settings.min_loudness_lufs} to {settings.max_loudness_lufs}",
                reason=(
                    "loudness is below the meter's measurement floor; the track is "
                    "effectively inaudible"
                    if features.loudness_meter_available
                    else "no loudness meter is installed; loudness was not measured"
                ),
            )
        )
    else:
        # The floor moves with intent. A blueprint at energy 0.1 is asking for something
        # barely there, and holding it to the same floor as a breakout track would reject
        # exactly the music §1 wants when the market is quiet.
        floor = settings.min_loudness_lufs - _QUIET_LOUDNESS_ALLOWANCE * (1.0 - energy)
        too_quiet = features.integrated_lufs < floor
        too_loud = features.integrated_lufs > settings.max_loudness_lufs
        ok = not (too_quiet or too_loud)
        checks.append(
            _check(
                "integrated_loudness",
                ok=ok,
                # Pre-mastering loudness is an input to mastering, not a verdict: the raw
                # pass warns, the mastered pass fails.
                warn=not ok and stage is QcStage.RAW,
                value=features.integrated_lufs,
                unit="LUFS",
                threshold=f"{floor:.1f} to {settings.max_loudness_lufs:.1f}",
                reason=(
                    f"{features.integrated_lufs:.1f} LUFS"
                    if ok
                    else (
                        f"{features.integrated_lufs:.1f} LUFS is "
                        + ("below" if too_quiet else "above")
                        + f" the {floor:.1f} to {settings.max_loudness_lufs:.1f} range"
                        + (
                            f" expected for a track of intended energy {energy:.2f}"
                            if too_quiet
                            else ""
                        )
                    )
                ),
            )
        )

    # -- silence and structure ---------------------------------------------

    silence_ok = features.silence_ratio <= settings.max_silence_ratio
    checks.append(
        _check(
            "silence_ratio",
            ok=silence_ok,
            value=features.silence_ratio,
            unit="fraction",
            threshold=f"<= {settings.max_silence_ratio:.0%}",
            reason=(
                f"{features.silence_ratio:.0%} of the track is below the silence floor"
                if silence_ok
                else f"{features.silence_ratio:.0%} of the track is silent"
            ),
        )
    )

    gap_ok = features.longest_internal_silence_seconds <= _MAX_INTERNAL_SILENCE
    checks.append(
        _check(
            "internal_silence",
            ok=gap_ok,
            value=features.longest_internal_silence_seconds,
            unit="s",
            threshold=f"<= {_MAX_INTERNAL_SILENCE}s",
            reason=(
                f"longest internal gap {features.longest_internal_silence_seconds:.1f}s"
                if gap_ok
                else (
                    f"a {features.longest_internal_silence_seconds:.1f}s gap mid-track reads as "
                    "a dropout on air"
                )
            ),
        )
    )

    for name, measured in (
        ("leading_silence", features.leading_silence_seconds),
        ("trailing_silence", features.trailing_silence_seconds),
    ):
        within = measured <= _MAX_BOUNDARY_SILENCE
        checks.append(
            _check(
                name,
                ok=within,
                # Boundary silence is trimmed by mastering, so the raw pass only notes it.
                warn=not within and stage is QcStage.RAW,
                value=measured,
                unit="s",
                threshold=f"<= {_MAX_BOUNDARY_SILENCE}s",
                reason=(
                    f"{measured:.1f}s"
                    if within
                    else f"{measured:.1f}s of silence at the {name.split('_')[0]}"
                ),
            )
        )

    disc_ok = features.discontinuity_count <= _MAX_DISCONTINUITIES
    checks.append(
        _check(
            "discontinuities",
            ok=disc_ok,
            value=float(features.discontinuity_count),
            unit="count",
            threshold=f"<= {_MAX_DISCONTINUITIES}",
            reason=(
                f"{features.discontinuity_count} large sample steps"
                if disc_ok
                else (
                    f"{features.discontinuity_count} impossible sample steps; the file is "
                    "probably corrupt"
                )
            ),
        )
    )

    # -- stereo -------------------------------------------------------------

    if features.channels == 2:
        correlation_ok = features.stereo_correlation >= _MIN_STEREO_CORRELATION
        checks.append(
            _check(
                "stereo_correlation",
                ok=correlation_ok,
                value=features.stereo_correlation,
                unit="correlation",
                threshold=f">= {_MIN_STEREO_CORRELATION}",
                reason=(
                    f"channels correlate {features.stereo_correlation:+.2f}"
                    if correlation_ok
                    else (
                        f"channels are anti-phase ({features.stereo_correlation:+.2f}); "
                        "the track will nearly vanish on a mono speaker"
                    )
                ),
            )
        )
        mono_ok = features.mono_compatibility_db >= _MAX_MONO_LOSS_DB
        checks.append(
            _check(
                "mono_compatibility",
                ok=mono_ok,
                warn=(
                    not mono_ok
                    and features.mono_compatibility_db >= _MAX_MONO_LOSS_DB * 2
                ),
                value=features.mono_compatibility_db,
                unit="dB",
                threshold=f">= {_MAX_MONO_LOSS_DB} dB",
                reason=(
                    f"mono fold-down loses {abs(features.mono_compatibility_db):.1f} dB"
                    if mono_ok
                    else (
                        f"mono fold-down loses {abs(features.mono_compatibility_db):.1f} dB to "
                        "phase cancellation"
                    )
                ),
            )
        )

    # -- spectrum -----------------------------------------------------------

    low_ok = features.low_energy_ratio <= _MAX_LOW_ENERGY_RATIO
    checks.append(
        _check(
            "low_frequency_balance",
            ok=low_ok,
            warn=not low_ok,
            value=features.low_energy_ratio,
            unit="fraction",
            threshold=f"<= {_MAX_LOW_ENERGY_RATIO:.0%} below 120 Hz",
            reason=(
                f"{features.low_energy_ratio:.0%} of energy below 120 Hz"
                if low_ok
                else (
                    f"{features.low_energy_ratio:.0%} of energy is below 120 Hz; "
                    "the low end dominates"
                )
            ),
        )
    )

    # Only meaningful once there is signal at all — a silent file has no spectrum to collapse.
    #
    # And only meaningful when 8 kHz is a frequency the file can actually represent. A 16 kHz
    # recording has its Nyquist limit at exactly 8 kHz, so it contains no energy above that
    # band *by construction* — sampling theory, not a fault. Measuring anyway reported "the
    # top end has collapsed, which means a decode or render fault" for every track in a
    # 16 kHz run: 355 of 355 rejected, the station carried entirely by its emergency tiers,
    # and the stated reason was a decode fault that had not occurred.
    if features.peak > 0:
        nyquist = features.sample_rate / 2.0
        if nyquist <= _HIGH_BAND_HZ * _NYQUIST_MARGIN:
            checks.append(
                _check(
                    "high_frequency_content",
                    ok=True,
                    value=None,
                    unit="fraction",
                    threshold=f">= {_LOW_HIGH_ENERGY_RATIO:.4%} above 8 kHz",
                    reason=(
                        f"not applicable: at {features.sample_rate} Hz the Nyquist limit is "
                        f"{nyquist:.0f} Hz, so there is no representable energy above "
                        f"{_HIGH_BAND_HZ / 1000:.0f} kHz to measure"
                    ),
                )
            )
        else:
            collapsed = features.high_energy_ratio < _COLLAPSED_HIGH_ENERGY_RATIO
            dark = features.high_energy_ratio < _LOW_HIGH_ENERGY_RATIO
            checks.append(
                _check(
                    "high_frequency_content",
                    ok=not dark,
                    # Dark is a mix; collapsed is a fault. Only the latter blocks the broadcast.
                    warn=dark and not collapsed,
                    value=features.high_energy_ratio,
                    unit="fraction",
                    threshold=f">= {_LOW_HIGH_ENERGY_RATIO:.4%} above 8 kHz",
                    reason=(
                        f"{features.high_energy_ratio:.3%} of energy above 8 kHz"
                        if not dark
                        else (
                            "no energy above 8 kHz at all; the top end has collapsed, which "
                            "means a decode or render fault"
                            if collapsed
                            else (
                                f"only {features.high_energy_ratio:.4%} of energy above "
                                "8 kHz; a dark mix, noted but not rejected"
                            )
                        )
                    ),
                )
            )

    checks.append(
        _check(
            "spectral_bandwidth",
            ok=features.spectral_bandwidth > 0,
            warn=features.spectral_bandwidth <= 0,
            value=features.spectral_bandwidth,
            unit="Hz",
            threshold="> 0",
            reason=(
                f"bandwidth {features.spectral_bandwidth:.0f} Hz"
                if features.spectral_bandwidth > 0
                else "no spectral spread; the signal is a constant or a single tone"
            ),
        )
    )

    status = QcStatus.PASS
    if any(check.status is QcStatus.FAIL for check in checks):
        status = QcStatus.FAIL
    elif any(check.status is QcStatus.WARN for check in checks):
        status = QcStatus.WARN

    result = TrackQcResult(
        track_id=track_id,
        stage=stage,
        status=status,
        checks=tuple(checks),
        features=features,
    )
    _log.info(
        "qc.completed",
        track_id=track_id,
        stage=stage.value,
        status=status.value,
        checks=len(checks),
        failures=[check.name for check in result.failures],
        warnings=[check.name for check in result.warnings],
    )
    return result
