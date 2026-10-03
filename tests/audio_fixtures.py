"""Deterministic audio fixtures for the Phase 6 tests (§6.18).

Every generator here is a pure function of its arguments and a seed. That matters more than it
sounds: a QC threshold test that is right 95% of the time is worse than no test, because the
5% arrives as a CI failure on an unrelated change and gets re-run until it passes.

The defects are **constructed, not hoped for**. ``clipped`` does not render loud music and
trust that it clips; it synthesises audio and then hard-limits a known fraction of samples to
±1.0, so the assertion can name the figure the check should report. The same discipline runs
through all of them — each generator produces exactly the pathology its name claims, at a
magnitude the caller chose.

Why these are not WAV files in a directory
------------------------------------------
§6.18 asks for fixtures, and committing a few megabytes of binary would work. Generating them
instead keeps the repository small, lets a test ask for "six seconds of this defect at that
severity" without a new asset, and makes the *definition* of each defect readable — which is
the thing a reviewer actually needs to check.
"""

from __future__ import annotations

from typing import Final

import numpy as np

from tradefix_radio.audio.pcm import AudioBuffer

__all__ = [
    "FIXTURE_RATE",
    "clicking",
    "clipped",
    "dc_offset",
    "gapped",
    "mono_cancelling",
    "musical",
    "near_duplicate",
    "noise",
    "quiet_ambient",
    "silence",
    "very_short",
]

#: Rate every fixture is generated at.
#:
#: 44 100 — a generator's natural output rate, not the 48 kHz master rate, so the fixtures
#: exercise the resampling the real pipeline performs rather than skipping past it.
FIXTURE_RATE: Final = 44_100

#: Pitch classes of an A-minor triad plus a bass root, in Hz.
_A_MINOR: Final = (110.0, 220.0, 261.63, 329.63)


def _time_axis(seconds: float, rate: int = FIXTURE_RATE) -> np.ndarray:
    return np.arange(int(seconds * rate), dtype=np.float64) / rate


def _stereo(mono: np.ndarray, width: float = 0.2) -> np.ndarray:
    """Widen a mono signal into two correlated channels.

    Correlated rather than identical, because a perfectly identical pair has a stereo
    correlation of exactly 1.0 and would never exercise the width checks. The offset is a
    small delay, which is what actual stereo width is.
    """
    if width <= 0:
        return np.stack([mono, mono], axis=1).astype(np.float32)
    delay = max(1, int(width * 50))
    right = np.concatenate([np.zeros(delay), mono[:-delay]]) if delay < mono.size else mono
    return np.stack([mono, right], axis=1).astype(np.float32)


def musical(
    seconds: float = 6.0,
    *,
    seed: int = 0,
    rate: int = FIXTURE_RATE,
    bpm: float = 120.0,
    amplitude: float = 0.4,
    root_hz: float = 110.0,
) -> AudioBuffer:
    """Clean, broadcast-plausible audio that must pass every QC check.

    Chord tones plus a pulsing envelope at the requested tempo and a little noise. It is not
    pleasant and does not need to be; it needs harmonic content for chroma, broadband content
    for MFCCs, and a periodic envelope for beat tracking, which a single sine has none of.
    """
    rng = np.random.default_rng(seed)
    t = _time_axis(seconds, rate)
    ratios = np.array(_A_MINOR) / _A_MINOR[0]
    signal = np.zeros_like(t)
    for index, ratio in enumerate(ratios):
        phase = rng.uniform(0.0, 2.0 * np.pi)
        signal += np.sin(2.0 * np.pi * root_hz * ratio * t + phase) / (index + 1.5)

    # A percussive envelope at the requested tempo. Beat tracking needs onsets, and a steady
    # drone gives it nothing to lock onto — a fixture that cannot be tempo-tracked would make
    # every tempo assertion vacuous.
    beat_period = 60.0 / bpm
    phase_in_beat = np.mod(t, beat_period) / beat_period
    envelope = 0.35 + 0.65 * np.exp(-6.0 * phase_in_beat)
    signal *= envelope
    signal += rng.normal(0.0, 0.015, size=t.size)

    peak = float(np.abs(signal).max()) or 1.0
    return AudioBuffer(_stereo(signal / peak * amplitude), rate)


def quiet_ambient(seconds: float = 6.0, *, seed: int = 1, rate: int = FIXTURE_RATE) -> AudioBuffer:
    """Deliberately quiet, deliberately valid.

    §6.1 names this case directly — *"a quiet ambient track should not fail simply because RMS
    is low"* — so there has to be a fixture that is genuinely quiet and genuinely fine, or the
    energy-aware loudness floor is untested and could be removed without anything going red.
    """
    rng = np.random.default_rng(seed)
    t = _time_axis(seconds, rate)
    signal = (
        np.sin(2.0 * np.pi * 220.0 * t)
        + 0.6 * np.sin(2.0 * np.pi * 329.63 * t)
        + 0.3 * np.sin(2.0 * np.pi * 440.0 * t)
    )
    # A slow swell rather than a beat: ambient music has no onsets, which is itself worth
    # having in the corpus, since the beat tracker must not fail the track for lacking them.
    signal *= 0.5 + 0.5 * np.sin(2.0 * np.pi * 0.1 * t)
    signal += rng.normal(0.0, 0.002, size=t.size)
    peak = float(np.abs(signal).max()) or 1.0
    return AudioBuffer(_stereo(signal / peak * 0.03), rate)


def silence(seconds: float = 6.0, *, rate: int = FIXTURE_RATE) -> AudioBuffer:
    """Digital silence. The unambiguous failure."""
    return AudioBuffer(np.zeros((int(seconds * rate), 2), dtype=np.float32), rate)


def noise(seconds: float = 6.0, *, seed: int = 2, rate: int = FIXTURE_RATE) -> AudioBuffer:
    """Broadband noise — audio with no musical content at all."""
    rng = np.random.default_rng(seed)
    samples = rng.normal(0.0, 0.2, size=(int(seconds * rate), 2)).astype(np.float32)
    return AudioBuffer(np.clip(samples, -1.0, 1.0), rate)


def clipped(
    seconds: float = 6.0,
    *,
    ratio: float = 0.08,
    seed: int = 3,
    rate: int = FIXTURE_RATE,
) -> AudioBuffer:
    """Audio with exactly ``ratio`` of its samples driven to full scale.

    Constructed rather than produced by overdriving, so a test can assert on the figure the
    clipping check reports instead of asserting that it is merely "high".
    """
    buffer = musical(seconds, seed=seed, rate=rate, amplitude=0.6)
    samples = np.array(buffer.samples, copy=True)
    rng = np.random.default_rng(seed + 100)
    total = samples.shape[0]
    count = int(total * ratio)
    indices = rng.choice(total, size=count, replace=False)
    samples[indices, :] = np.where(samples[indices, :] >= 0, 1.0, -1.0)
    return AudioBuffer(samples, rate)


def dc_offset(
    seconds: float = 6.0, *, offset: float = 0.2, seed: int = 4, rate: int = FIXTURE_RATE
) -> AudioBuffer:
    """Valid audio with a constant offset added to every sample."""
    buffer = musical(seconds, seed=seed, rate=rate, amplitude=0.3)
    return AudioBuffer(
        np.clip(np.asarray(buffer.samples) + offset, -1.0, 1.0).astype(np.float32), rate
    )


def gapped(
    seconds: float = 12.0,
    *,
    gap_start: float = 4.0,
    gap_seconds: float = 7.0,
    seed: int = 5,
    rate: int = FIXTURE_RATE,
) -> AudioBuffer:
    """Audio with a long silent stretch in the middle.

    The default gap exceeds the internal-silence limit, which is the point: a renderer that
    drops out mid-track produces exactly this, and it is inaudible in a waveform thumbnail.
    """
    buffer = musical(seconds, seed=seed, rate=rate)
    samples = np.array(buffer.samples, copy=True)
    start = int(gap_start * rate)
    end = min(samples.shape[0], start + int(gap_seconds * rate))
    samples[start:end, :] = 0.0
    return AudioBuffer(samples, rate)


def clicking(
    seconds: float = 6.0,
    *,
    clicks: int = 64,
    seed: int = 6,
    rate: int = FIXTURE_RATE,
) -> AudioBuffer:
    """Audio with sample-level discontinuities — the sound of a bad concatenation."""
    buffer = musical(seconds, seed=seed, rate=rate, amplitude=0.3)
    samples = np.array(buffer.samples, copy=True)
    rng = np.random.default_rng(seed + 200)
    positions = rng.choice(samples.shape[0] - 2, size=clicks, replace=False)
    for position in positions:
        # A full-scale sign flip across one sample: the largest discontinuity possible, and
        # exactly what a truncated buffer boundary produces.
        samples[position + 1, :] = -np.sign(samples[position, :]) * 0.98
    return AudioBuffer(samples, rate)


def mono_cancelling(seconds: float = 6.0, *, seed: int = 7, rate: int = FIXTURE_RATE) -> AudioBuffer:
    """Two channels in anti-phase: loud in stereo, silent folded to mono.

    The fixture that caught a real defect — level measured on the mono fold reported this as
    digitally silent, which is the opposite of true.
    """
    buffer = musical(seconds, seed=seed, rate=rate, amplitude=0.4)
    mono = np.asarray(buffer.samples)[:, 0]
    return AudioBuffer(np.stack([mono, -mono], axis=1).astype(np.float32), rate)


def very_short(seconds: float = 0.4, *, seed: int = 8, rate: int = FIXTURE_RATE) -> AudioBuffer:
    """Far below any plausible track length."""
    return musical(seconds, seed=seed, rate=rate)


def near_duplicate(
    source: AudioBuffer, *, gain_db: float = -0.5, noise_level: float = 0.0004, seed: int = 9
) -> AudioBuffer:
    """A re-render of ``source``: same music, slightly different numbers.

    Models what a generator does when handed the same blueprint twice — not a byte-identical
    file, which the hash catches trivially, but the case the *similarity* engine has to catch
    and the hash cannot. Keeping both cases distinct is what stops a test from proving the
    hash works and claiming it proved the engine does.
    """
    rng = np.random.default_rng(seed)
    samples = np.asarray(source.samples, dtype=np.float32) * (10 ** (gain_db / 20.0))
    samples = samples + rng.normal(0.0, noise_level, size=samples.shape).astype(np.float32)
    return AudioBuffer(np.clip(samples, -1.0, 1.0).astype(np.float32), source.sample_rate)
