"""Audio feature extraction (§6.2).

One component, used by three callers that must agree: QC measures against it, the similarity
engine compares it, and the Originality page displays it. Computing features twice in two
places is how two surfaces end up disagreeing about whether a track is a duplicate.

**What is stored is compact by design.** A 20-band MFCC over a four-minute track is roughly
20 × 10 000 floats; a hundred tracks of that is a gigabyte of matrix nobody can query. §6.2
says so explicitly, and the Phase 1 schema already anticipated it — `audio_fingerprints` holds
means and standard deviations, not frames. Those summaries are what similarity actually
compares, so nothing is lost for the purpose they serve.

**librosa is optional at import, required in production.** It is the right tool and ADR-01 pins
Python 3.10 for its wheels, but a development machine without it should degrade to a smaller
feature set with a stated reason rather than refuse to start. `tradefix doctor` reports which
level is active, and production enforces the full one.

The one performance trap, measured
--------------------------------
librosa's numba kernels compile on first call. Measured on this machine against 30 seconds of
audio: **MFCC 29 s and beat tracking 15 s on the first call, then 0.08 s each** — 375× and 352×
real time once warm. That is a one-off process cost, but paid lazily it lands on the station's
first real track, where forty-five seconds of unexpected CPU could starve the buffer. So
:func:`warm_up` exists and the pipeline calls it at construction.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np
import structlog

from tradefix_radio.audio.pcm import AudioBuffer

_log = structlog.get_logger(__name__)

__all__ = [
    "ANALYSIS_SAMPLE_RATE",
    "EMBEDDING_VERSION",
    "AudioFeatures",
    "analysis_backend",
    "extract_features",
    "librosa_available",
    "loudness_meter_available",
    "warm_up",
]

#: Rate every analysis runs at, regardless of the file's own.
#:
#: 22 050 Hz, which is librosa's own default and halves the work against a 48 kHz master for
#: no loss that matters here: every feature used for similarity lives well below 11 kHz, and
#: the spectral measures that reach higher are summarised to a single number anyway.
#:
#: Fixed rather than derived, because two tracks analysed at different rates produce MFCCs
#: that are not comparable — and silently incomparable features are worse than none.
ANALYSIS_SAMPLE_RATE: Final = 22_050

#: MFCC bands kept. 20 is the usual compromise: enough to characterise timbre, few enough that
#: the mean/std summary stays a 40-float vector.
MFCC_BANDS: Final = 20

#: Layout version of :meth:`AudioFeatures.embedding`.
#:
#: Stored beside every persisted embedding. Two embeddings built by different versions are not
#: comparable — a cosine between them is a number with no meaning — and the alternative to
#: recording this is a library that silently mixes scales after an upgrade. Version 1 was the
#: raw concatenation; version 2 centres chroma and drops the MFCC log-energy term.
EMBEDDING_VERSION: Final = 2

#: Reference frequency for key estimation — A4, the tuning standard the chroma bins assume.
_PITCH_CLASSES: Final = (
    "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B",
)

#: Krumhansl–Schmuckler profiles, the standard correlation templates for key finding.
#:
#: Used rather than "loudest chroma bin wins", which mistakes a bass-heavy IV chord for the
#: tonic constantly. These weight the whole pitch-class distribution against what a major or
#: minor key actually implies.
_MAJOR_PROFILE: Final = np.array(
    [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
)
_MINOR_PROFILE: Final = np.array(
    [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
)


def loudness_meter_available() -> bool:
    """Whether an ITU-R BS.1770 meter can be used in this process."""
    try:  # pragma: no cover - trivially environment-dependent
        import pyloudnorm  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def librosa_available() -> bool:
    """Whether the full feature set can be computed."""
    try:  # pragma: no cover - trivially environment-dependent
        import librosa  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def analysis_backend() -> str:
    """``"librosa"`` or ``"numpy"`` — what `doctor` and the report quote."""
    return "librosa" if librosa_available() else "numpy"


_warmed = False


def warm_up() -> float:
    """Compile librosa's numba kernels now rather than on the first real track.

    Returns the seconds spent, so the caller can log a figure rather than a guess.

    Worth the explicitness: measured cold, MFCC took 29 s and beat tracking 15 s for 30
    seconds of audio. Paid lazily that lands on the first track the station generates, which
    is exactly when the buffer is emptiest.
    """
    global _warmed
    if _warmed or not librosa_available():
        return 0.0
    import time  # noqa: PLC0415

    started = time.perf_counter()
    # Two seconds of noise is enough to trigger every kernel the real path uses.
    rng = np.random.default_rng(0)
    probe = rng.standard_normal(ANALYSIS_SAMPLE_RATE * 2).astype(np.float32) * 0.1
    try:
        _compute_librosa_features(probe, ANALYSIS_SAMPLE_RATE)
    except Exception as error:  # noqa: BLE001 - warming is best-effort by definition
        _log.warning(
            "analysis.warm_up_failed",
            error_type=type(error).__name__,
            error=str(error),
            detail="the first real analysis will pay the JIT cost instead",
        )
        return 0.0
    _warmed = True
    elapsed = time.perf_counter() - started
    _log.info("analysis.warmed", seconds=round(elapsed, 2), backend=analysis_backend())
    return elapsed


@dataclass(frozen=True)
class AudioFeatures:
    """Everything measured from one audio file, in a form small enough to store.

    Every vector here is a *summary*: twelve chroma means, twenty MFCC means, and so on. The
    per-frame matrices they come from are discarded deliberately (§6.2).
    """

    duration_seconds: float
    sample_rate: int
    channels: int

    # -- level and dynamics
    peak: float
    rms: float
    crest_factor: float
    dc_offset: float
    #: ITU-R BS.1770 integrated loudness.
    #:
    #: ``None`` for two unrelated reasons, which is why :attr:`loudness_meter_available`
    #: exists alongside it: either no meter is installed, or the meter ran and the material
    #: measured below its floor. The first is a gap in the environment and the second is a
    #: defect in the track, and reporting both as "not measured" told an operator to install
    #: software when the real answer was that the file was inaudible.
    integrated_lufs: float | None = None
    loudness_range: float | None = None
    #: Whether a loudness meter was available to this process at all.
    loudness_meter_available: bool = False

    # -- spectral summaries
    spectral_centroid: float = 0.0
    spectral_bandwidth: float = 0.0
    spectral_rolloff: float = 0.0
    zero_crossing_rate: float = 0.0
    #: Energy fraction below 120 Hz and above 8 kHz — the two ends QC cares about.
    low_energy_ratio: float = 0.0
    high_energy_ratio: float = 0.0

    # -- stereo
    stereo_correlation: float = 1.0
    #: Loss in level when folded to mono, in dB. Large values mean phase cancellation.
    mono_compatibility_db: float = 0.0

    # -- musical
    tempo: float | None = None
    beat_strength: float | None = None
    musical_key: str | None = None
    key_confidence: float | None = None

    # -- comparison vectors
    chroma_mean: tuple[float, ...] = ()
    chroma_std: tuple[float, ...] = ()
    mfcc_mean: tuple[float, ...] = ()
    mfcc_std: tuple[float, ...] = ()
    #: Coarse RMS envelope, for shape comparison independent of timbre.
    rms_profile: tuple[float, ...] = ()

    # -- structure
    leading_silence_seconds: float = 0.0
    trailing_silence_seconds: float = 0.0
    longest_internal_silence_seconds: float = 0.0
    silence_ratio: float = 0.0
    clipped_sample_ratio: float = 0.0
    #: Samples where consecutive values jump impossibly far — a decode fault signature.
    discontinuity_count: int = 0

    backend: str = "numpy"
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def is_mono(self) -> bool:
        return self.channels == 1

    def embedding(self) -> np.ndarray:
        """The vector similarity actually compares.

        Chroma and timbre, each reduced to a *shape* and unit-normalised separately, then
        concatenated with equal weight and normalised again. Cosine over the result therefore
        spans a usable range instead of clustering near 1.

        Why each step is there, because the naive version was measured and did not work
        ---------------------------------------------------------------------------------
        Concatenating the raw means and taking a cosine produced 0.93–0.95 between four tracks
        with different genres, tempos and keys, and an MFCC cosine of 0.998 between *every*
        pair. Two separate causes, both structural rather than incidental:

        * **Chroma is non-negative.** The cosine of two arbitrary vectors in the positive
          orthant has a floor far above zero — unrelated tracks measured 0.86–0.91 — so the
          interesting range was compressed into the top tenth of the scale. Subtracting the
          mean makes the vector a zero-sum profile of *which pitch classes stand out*, and the
          cosine becomes a correlation spanning the full −1…1.
        * **MFCC[0] is log-energy, not timbre.** Its magnitude is an order above the
          coefficients that describe spectral shape, so after any whole-vector scaling it
          dominated the dot product and every track looked alike. It is dropped: overall level
          is already carried by ``rms`` and ``integrated_lufs``, and judging two tracks similar
          because they are equally loud is precisely the error to avoid.

        Each block is unit-normalised *before* concatenation so the 12 chroma bins and the 19
        timbre coefficients contribute equally regardless of their natural magnitudes —
        normalising only at the end would hand the comparison to whichever block happened to
        have the larger norm.
        """
        parts: list[np.ndarray] = []

        chroma = np.asarray(self.chroma_mean, dtype=np.float64)
        if chroma.size:
            centred = chroma - chroma.mean()
            norm = float(np.linalg.norm(centred))
            # A perfectly flat chroma — white noise, or silence — has no profile to compare.
            # Contributing zeros is honest; contributing noise amplified to unit length is not.
            parts.append(centred / norm if norm > 0 else centred)

        mfcc = np.asarray(self.mfcc_mean, dtype=np.float64)
        if mfcc.size > 1:
            shape = mfcc[1:]
            norm = float(np.linalg.norm(shape))
            parts.append(shape / norm if norm > 0 else shape)

        if not parts:
            return np.zeros(1)
        vector = np.concatenate(parts)
        total = float(np.linalg.norm(vector))
        return np.asarray(vector / total if total > 0 else vector)


def _to_mono(samples: np.ndarray) -> np.ndarray:
    if samples.ndim == 2 and samples.shape[1] > 1:
        return np.asarray(samples.mean(axis=1))
    return np.asarray(samples.reshape(-1))


def _resample_linear(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Linear resampling to the analysis rate.

    Adequate here and nowhere else: these samples feed statistical summaries, not the
    broadcast. The playout path's own `conform` carries the same caveat.
    """
    if source_rate == target_rate or samples.size == 0:
        return samples
    duration = samples.size / source_rate
    target_length = max(1, round(duration * target_rate))
    source_positions = np.linspace(0.0, samples.size - 1, num=samples.size)
    target_positions = np.linspace(0.0, samples.size - 1, num=target_length)
    resampled = np.interp(target_positions, source_positions, samples)
    return np.asarray(resampled, dtype=np.float32)


def _silence_runs(
    mono: np.ndarray, rate: int, threshold: float
) -> tuple[float, float, float, float]:
    """Leading, trailing, longest internal silence, and the overall silent fraction.

    Measured on a short-window RMS rather than on raw samples: a sine wave crosses zero
    constantly, and a per-sample threshold would call every waveform half-silent.

    ``signal`` is a rectified per-instant envelope across channels, not a mono fold — see the
    note in :func:`extract_features`.
    """
    if mono.size == 0:
        return 0.0, 0.0, 0.0, 1.0
    window = max(1, rate // 100)  # 10 ms
    usable = (mono.size // window) * window
    if usable == 0:
        return 0.0, 0.0, 0.0, 1.0
    frames = mono[:usable].reshape(-1, window)
    envelope = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1))
    quiet = envelope < threshold
    frame_seconds = window / rate

    if quiet.all():
        total = quiet.size * frame_seconds
        return total, total, total, 1.0

    first_loud = int(np.argmax(~quiet))
    last_loud = int(quiet.size - 1 - np.argmax(~quiet[::-1]))
    leading = first_loud * frame_seconds
    trailing = (quiet.size - 1 - last_loud) * frame_seconds

    longest = 0
    current = 0
    for value in quiet[first_loud : last_loud + 1]:
        current = current + 1 if value else 0
        longest = max(longest, current)

    return leading, trailing, longest * frame_seconds, float(quiet.mean())


def _discontinuities(mono: np.ndarray) -> int:
    """Count sample-to-sample jumps that no real signal produces.

    A decode fault, a truncated write or a corrupted frame shows up as a step far larger than
    anything the surrounding signal is doing. Two conditions must both hold: the step is a
    statistical outlier *and* it is large relative to the track's own peak.

    Why the floor is relative to peak rather than an absolute 0.5
    -------------------------------------------------------------
    It used to be ``max(limit, 0.5)`` — an absolute amplitude. That encodes "no real signal
    moves 0.5 between consecutive samples", and at 48 kHz that is simply false: a sine of
    amplitude *A* at frequency *f* has a maximum step of ``A·2πf/fs``, so a loud component at
    just 5 kHz reaches 0.58. Hi-hats and transients live well above 5 kHz.

    Measured on real ACE-Step output, where this first showed up as a rejection:

    ========================================  ==========  ===============  =============
    material                                  max |Δ|     hits at 0.5      hits at peak
    ========================================  ==========  ===============  =============
    ACE-Step master, UK drill 142 BPM         0.666       50  (FAIL)       0
    ACE-Step master, DnB 174 BPM              0.607       18               0
    ACE-Step master, trap 148 BPM             0.713        1               0
    `clicking` fixture, 64 injected clicks    1.248       114              113
    `musical` fixture, clean                  0.057        0               0
    `noise` fixture, broadband                1.243        0               0
    ========================================  ==========  ===============  =============

    The absolute floor rejected a perfectly good master as "probably corrupt" while the
    fixtures that motivated it were all built from low sine partials and never exercised the
    case. Scaling with peak separates real music from real clicks with a wide margin and has
    a physical reading: exceeding the track's own peak in one sample step requires full-scale
    content above ``fs/2π`` (7.6 kHz at 48 kHz), which mastered music does not have, while a
    click — a sample flipped to the opposite rail — gives a step of about twice the peak.
    """
    if mono.size < 3:
        return 0
    samples = mono.astype(np.float64)
    deltas = np.abs(np.diff(samples))
    if deltas.size == 0:
        return 0
    # Nine standard deviations above the mean step. Chosen high on purpose: percussion is full
    # of genuinely large steps, and a false positive here quarantines a good track.
    limit = deltas.mean() + 9.0 * deltas.std()
    if not math.isfinite(limit) or limit <= 0:
        return 0
    peak = float(np.abs(samples).max())
    if peak <= 0:
        return 0
    return int((deltas > max(limit, peak * _DISCONTINUITY_PEAK_FACTOR)).sum())


#: Minimum sample step, as a multiple of the track's peak, before it can count as a fault.
#:
#: 1.0. Measured rather than chosen: real ACE-Step masters reach 0.71-0.76 of their peak in a
#: single step, and the click fixture reaches 1.27. Anything in that gap separates them; 1.0
#: sits in the middle and has a physical reading of its own (see `_discontinuities`).
_DISCONTINUITY_PEAK_FACTOR: Final = 1.0

#: Floor for the reported mono fold-down loss, in dB.
#:
#: −120 dB. Total cancellation is mathematically −∞, which no column can store and no UI can
#: render; −120 dB is below the noise floor of any format the station handles, so clamping
#: there loses nothing real while keeping the value finite and orderable.
_MONO_LOSS_FLOOR_DB: Final = -120.0


def _stereo_metrics(samples: np.ndarray) -> tuple[float, float]:
    """Inter-channel correlation, and the level lost when folded to mono.

    Mono compatibility matters because a stream can be heard on a phone speaker. Two channels
    that are out of phase sum to near-silence there while sounding fine on headphones, which
    is the kind of defect that only surfaces after it has aired.
    """
    if samples.ndim != 2 or samples.shape[1] < 2:
        return 1.0, 0.0
    left = samples[:, 0].astype(np.float64)
    right = samples[:, 1].astype(np.float64)
    if left.std() < 1e-9 or right.std() < 1e-9:
        # One silent channel is not "uncorrelated", it is a different defect, and the
        # correlation of a constant is undefined. Report full correlation and let the
        # silence checks speak.
        return 1.0, 0.0
    correlation = float(np.corrcoef(left, right)[0, 1])
    stereo_rms = float(np.sqrt((left**2 + right**2).mean() / 2))
    mono_rms = float(np.sqrt((((left + right) / 2) ** 2).mean()))

    if stereo_rms <= 1e-9:
        # Nothing on either channel. There is no fold-down loss to speak of, and the silence
        # checks are the ones with something to say.
        loss_db = 0.0
    elif mono_rms <= 1e-12:
        # Total cancellation: loud in stereo, gone in mono. The ratio's logarithm is −∞, and
        # the obvious guard — leaving `loss_db` at its 0.0 initialiser — reported the *worst*
        # possible case as "perfectly mono-compatible", which is how a track that vanishes on
        # a phone speaker passed the mono check while the correlation check caught it by
        # accident. Reporting the floor instead keeps the number's sign and meaning intact.
        loss_db = _MONO_LOSS_FLOOR_DB
    else:
        loss_db = max(_MONO_LOSS_FLOOR_DB, 20.0 * math.log10(mono_rms / stereo_rms))
    return (correlation if math.isfinite(correlation) else 1.0), loss_db


def _band_ratios(mono: np.ndarray, rate: int) -> tuple[float, float]:
    """Fraction of spectral energy below 120 Hz and above 8 kHz."""
    if mono.size < 256:
        return 0.0, 0.0
    spectrum = np.abs(np.fft.rfft(mono * np.hanning(mono.size)))
    power = spectrum**2
    total = float(power.sum())
    if total <= 0:
        return 0.0, 0.0
    freqs = np.fft.rfftfreq(mono.size, d=1.0 / rate)
    low = float(power[freqs < 120.0].sum()) / total
    high = float(power[freqs > 8_000.0].sum()) / total
    return low, high


def _numpy_spectral(mono: np.ndarray, rate: int) -> dict[str, float]:
    """Centroid, bandwidth, rolloff and zero-crossing rate without librosa.

    A single whole-signal FFT rather than a framed one. Coarser than librosa's per-frame
    statistics, and honest about it: these feed QC thresholds and a fallback similarity
    comparison, not a musicological claim.
    """
    if mono.size < 256:
        return {"centroid": 0.0, "bandwidth": 0.0, "rolloff": 0.0, "zcr": 0.0}
    windowed = mono * np.hanning(mono.size)
    magnitude = np.abs(np.fft.rfft(windowed))
    freqs = np.fft.rfftfreq(mono.size, d=1.0 / rate)
    total = float(magnitude.sum())
    if total <= 0:
        return {"centroid": 0.0, "bandwidth": 0.0, "rolloff": 0.0, "zcr": 0.0}
    centroid = float((freqs * magnitude).sum() / total)
    bandwidth = float(np.sqrt(((freqs - centroid) ** 2 * magnitude).sum() / total))
    cumulative = np.cumsum(magnitude)
    rolloff_index = int(np.searchsorted(cumulative, 0.85 * cumulative[-1]).item())
    rolloff = float(freqs[min(rolloff_index, freqs.size - 1)])
    zcr = float((np.diff(np.signbit(mono)) != 0).mean())
    return {"centroid": centroid, "bandwidth": bandwidth, "rolloff": rolloff, "zcr": zcr}


def _estimate_key(chroma_mean: np.ndarray) -> tuple[str | None, float]:
    """Krumhansl–Schmuckler key estimate from a chroma profile."""
    if chroma_mean.size != 12 or not np.any(chroma_mean):
        return None, 0.0
    profile = chroma_mean - chroma_mean.mean()
    if not np.any(profile):
        return None, 0.0

    best_score = -2.0
    best_name: str | None = None
    runner_up = -2.0
    for tonic in range(12):
        for template, suffix in ((_MAJOR_PROFILE, "major"), (_MINOR_PROFILE, "minor")):
            rotated = np.roll(template, tonic)
            centred = rotated - rotated.mean()
            denominator = np.linalg.norm(profile) * np.linalg.norm(centred)
            if denominator <= 0:
                continue
            score = float(profile @ centred / denominator)
            if score > best_score:
                runner_up = best_score
                best_score = score
                best_name = f"{_PITCH_CLASSES[tonic]} {suffix}"
            elif score > runner_up:
                runner_up = score
    # Confidence as the margin over the runner-up, not the raw correlation: a track that
    # correlates 0.9 with two different keys is not a confident estimate.
    confidence = max(0.0, min(1.0, (best_score - runner_up))) if best_name else 0.0
    return best_name, confidence


def _compute_librosa_features(mono: np.ndarray, rate: int) -> dict[str, Any]:
    """The full feature set. Raises if librosa is unavailable."""
    import librosa  # noqa: PLC0415

    # librosa warns "Trying to estimate tuning from empty frequency set" for signals with no
    # detectable pitch — silence, noise, a single sine. Those are expected inputs here, since
    # QC analyses broken audio on purpose, and the suite treats warnings as errors. Silenced
    # at the one call that raises it rather than globally.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="Trying to estimate tuning", category=UserWarning
        )
        chroma = librosa.feature.chroma_stft(y=mono, sr=rate)
    mfcc = librosa.feature.mfcc(y=mono, sr=rate, n_mfcc=MFCC_BANDS)
    centroid = librosa.feature.spectral_centroid(y=mono, sr=rate)
    bandwidth = librosa.feature.spectral_bandwidth(y=mono, sr=rate)
    rolloff = librosa.feature.spectral_rolloff(y=mono, sr=rate)
    zcr = librosa.feature.zero_crossing_rate(y=mono)
    onset = librosa.onset.onset_strength(y=mono, sr=rate)
    tempo_value, beats = librosa.beat.beat_track(y=mono, sr=rate, onset_envelope=onset)
    tempo = float(np.atleast_1d(tempo_value)[0])

    return {
        "chroma_mean": chroma.mean(axis=1),
        "chroma_std": chroma.std(axis=1),
        "mfcc_mean": mfcc.mean(axis=1),
        "mfcc_std": mfcc.std(axis=1),
        "centroid": float(centroid.mean()),
        "bandwidth": float(bandwidth.mean()),
        "rolloff": float(rolloff.mean()),
        "zcr": float(zcr.mean()),
        "tempo": tempo,
        # Mean onset strength at detected beats: how strongly the track pulses, which
        # distinguishes a four-on-the-floor track from an ambient pad at the same tempo.
        "beat_strength": float(onset[beats].mean()) if len(beats) else 0.0,
    }


def _chroma_numpy(mono: np.ndarray, rate: int) -> np.ndarray:
    """A twelve-bin pitch-class profile without librosa.

    Folds FFT magnitude into pitch classes by frequency. Cruder than a constant-Q transform —
    it has no octave weighting and inherits the FFT's uneven resolution at low frequencies —
    but it produces a usable key estimate and a comparable vector, which is what the fallback
    is for.
    """
    if mono.size < 2048:
        return np.zeros(12)
    spectrum = np.abs(np.fft.rfft(mono * np.hanning(mono.size)))
    freqs = np.fft.rfftfreq(mono.size, d=1.0 / rate)
    bins = np.zeros(12)
    usable = (freqs > 27.5) & (freqs < 4186.0)  # A0 to C8
    if not usable.any():
        return bins
    # MIDI note number, then pitch class.
    midi = 69 + 12 * np.log2(freqs[usable] / 440.0)
    classes = np.mod(np.round(midi).astype(int), 12)
    np.add.at(bins, classes, spectrum[usable])
    total = bins.sum()
    return bins / total if total > 0 else bins


def extract_features(
    buffer: AudioBuffer,
    *,
    silence_threshold: float = 10 ** (-60.0 / 20.0),
    measure_loudness: bool = True,
) -> AudioFeatures:
    """Measure one buffer. The only entry point; everything else is private.

    ``silence_threshold`` is an amplitude, defaulting to -60 dBFS — quiet enough that an
    ambient track's noise floor is not called silence, loud enough to catch a real gap.
    """
    samples = np.asarray(buffer.samples, dtype=np.float32)
    if samples.ndim == 1:
        samples = samples.reshape(-1, 1)
    channels = samples.shape[1]
    rate = buffer.sample_rate
    duration = buffer.duration_seconds

    mono_native = _to_mono(samples)
    # If the fold cancels but the channels carry signal, analyse one channel instead.
    #
    # Anti-phase stereo sums to silence, and a spectrum taken from that sum reports a
    # collapsed top end — which reads as a decode fault when the real defect is phase. One
    # channel is a faithful view of what the track contains; the stereo checks diagnose the
    # phase problem separately, which is where that finding belongs.
    if samples.ndim == 2 and samples.shape[1] > 1:
        fold_peak = float(np.abs(mono_native).max()) if mono_native.size else 0.0
        channel_peak = float(np.abs(samples[:, 0]).max()) if samples.size else 0.0
        if channel_peak > 1e-6 and fold_peak < channel_peak * 0.05:
            mono_native = np.asarray(samples[:, 0], dtype=np.float32)

    # Level is measured across **all channels**, not on the mono fold.
    #
    # This distinction is not academic. Two channels in anti-phase average to exactly zero, so
    # a fold-down-based peak reports a perfectly loud track as digitally silent — which is what
    # the first version did, mislabelling a stereo defect as "the file is silent end to end"
    # and sending an operator after the wrong fault. The fold is still used below for spectral
    # and structural analysis, where summing to mono is the right thing to do.
    flat = samples.reshape(-1)
    finite = flat[np.isfinite(flat)]

    peak = float(np.abs(finite).max()) if finite.size else 0.0
    rms = float(np.sqrt((finite.astype(np.float64) ** 2).mean())) if finite.size else 0.0
    crest = (peak / rms) if rms > 1e-12 else 0.0
    dc_offset = float(finite.mean()) if finite.size else 0.0
    # At or above full scale. Not "> 1.0": a sample sitting exactly at 1.0 is already the
    # signature of something that clipped before it reached us.
    clipped = float((np.abs(finite) >= 0.999).mean()) if finite.size else 0.0

    # Loudest channel at each instant, for the same reason: anti-phase content is present
    # audio, not silence.
    envelope_signal = (
        np.abs(samples).max(axis=1) if samples.ndim == 2 else np.abs(samples.reshape(-1))
    )
    leading, trailing, longest_gap, silence_ratio = _silence_runs(
        envelope_signal, rate, silence_threshold
    )
    correlation, mono_loss = _stereo_metrics(samples)
    low_ratio, high_ratio = _band_ratios(mono_native, rate)
    discontinuities = _discontinuities(mono_native)

    # A coarse envelope, 32 points regardless of length, so two tracks of different durations
    # still compare shape to shape.
    profile: tuple[float, ...] = ()
    if mono_native.size >= 32:
        chunks = np.array_split(mono_native, 32)
        profile = tuple(float(np.sqrt((c.astype(np.float64) ** 2).mean())) for c in chunks)

    analysis_signal = _resample_linear(mono_native, rate, ANALYSIS_SAMPLE_RATE)
    analysis_signal = np.nan_to_num(analysis_signal, nan=0.0, posinf=0.0, neginf=0.0)

    backend = "numpy"
    chroma_mean = chroma_std = np.zeros(12)
    mfcc_mean = mfcc_std = np.zeros(0)
    tempo: float | None = None
    beat_strength: float | None = None

    if librosa_available() and analysis_signal.size >= ANALYSIS_SAMPLE_RATE // 4:
        try:
            computed = _compute_librosa_features(analysis_signal, ANALYSIS_SAMPLE_RATE)
            backend = "librosa"
            chroma_mean = computed["chroma_mean"]
            chroma_std = computed["chroma_std"]
            mfcc_mean = computed["mfcc_mean"]
            mfcc_std = computed["mfcc_std"]
            spectral = {
                "centroid": computed["centroid"],
                "bandwidth": computed["bandwidth"],
                "rolloff": computed["rolloff"],
                "zcr": computed["zcr"],
            }
            tempo = computed["tempo"]
            beat_strength = computed["beat_strength"]
        except Exception as error:  # noqa: BLE001 - fall back rather than fail the track
            # A feature-extraction failure must not reject a track that may be perfectly
            # good. It degrades the comparison, which the stored backend records.
            _log.warning(
                "analysis.librosa_failed",
                error_type=type(error).__name__,
                error=str(error),
                detail="falling back to the numpy feature set for this track",
            )
            spectral = _numpy_spectral(analysis_signal, ANALYSIS_SAMPLE_RATE)
            chroma_mean = _chroma_numpy(analysis_signal, ANALYSIS_SAMPLE_RATE)
    else:
        spectral = _numpy_spectral(analysis_signal, ANALYSIS_SAMPLE_RATE)
        chroma_mean = _chroma_numpy(analysis_signal, ANALYSIS_SAMPLE_RATE)

    key_name, key_confidence = _estimate_key(np.asarray(chroma_mean, dtype=np.float64))

    lufs: float | None = None
    loudness_range: float | None = None
    meter_available = loudness_meter_available()
    if measure_loudness:
        lufs, loudness_range = _measure_loudness(samples, rate)

    return AudioFeatures(
        duration_seconds=duration,
        sample_rate=rate,
        channels=channels,
        peak=peak,
        rms=rms,
        crest_factor=crest,
        dc_offset=dc_offset,
        integrated_lufs=lufs,
        loudness_range=loudness_range,
        loudness_meter_available=meter_available and measure_loudness,
        spectral_centroid=spectral["centroid"],
        spectral_bandwidth=spectral["bandwidth"],
        spectral_rolloff=spectral["rolloff"],
        zero_crossing_rate=spectral["zcr"],
        low_energy_ratio=low_ratio,
        high_energy_ratio=high_ratio,
        stereo_correlation=correlation,
        mono_compatibility_db=mono_loss,
        tempo=tempo,
        beat_strength=beat_strength,
        musical_key=key_name,
        key_confidence=key_confidence,
        chroma_mean=tuple(float(v) for v in np.asarray(chroma_mean).ravel()),
        chroma_std=tuple(float(v) for v in np.asarray(chroma_std).ravel()),
        mfcc_mean=tuple(float(v) for v in np.asarray(mfcc_mean).ravel()),
        mfcc_std=tuple(float(v) for v in np.asarray(mfcc_std).ravel()),
        rms_profile=profile,
        leading_silence_seconds=leading,
        trailing_silence_seconds=trailing,
        longest_internal_silence_seconds=longest_gap,
        silence_ratio=silence_ratio,
        clipped_sample_ratio=clipped,
        discontinuity_count=discontinuities,
        backend=backend,
    )


def _measure_loudness(samples: np.ndarray, rate: int) -> tuple[float | None, float | None]:
    """ITU-R BS.1770 integrated loudness, in process.

    pyloudnorm rather than shelling to FFmpeg: QC measures loudness for every candidate, and a
    subprocess per track would dominate the pipeline's cost for a number that takes
    milliseconds to compute in memory. FFmpeg still does the *normalisation* in mastering,
    where its two-pass loudnorm is the better tool.

    Returns ``(None, None)`` rather than a guess when no meter is installed.
    """
    try:  # pragma: no cover - environment-dependent
        import pyloudnorm  # noqa: PLC0415
    except ImportError:
        return None, None

    # BS.1770 needs a meaningful window; shorter material has no defined integrated loudness.
    if samples.shape[0] < rate * 0.5:
        return None, None
    try:
        meter = pyloudnorm.Meter(rate)
        block = samples if samples.ndim == 2 else samples.reshape(-1, 1)
        value = float(meter.integrated_loudness(block.astype(np.float64)))
    except Exception as error:  # noqa: BLE001 - a meter failure is not a track defect
        _log.debug("analysis.loudness_failed", error=str(error))
        return None, None
    if not math.isfinite(value):
        # Digital silence measures as -inf, which is correct and unusable as a number.
        return None, None
    return value, None
