"""The canonical playout format, and explicit conversion into it (ADR-06, milestone 4.6).

One format for the whole playout path: **48 kHz, stereo, float32**. Everything the mixer and the
sink touch is in it, and conversion happens at exactly one place — :func:`conform`, called when
audio enters the playout path from a file.

Why a single canonical rate at all. Without one, each component assumes whatever its input
happened to be, and the assumptions only disagree at the seam between two tracks from different
sources. A crossfade between a 44.1 kHz track and a 48 kHz one does not fail loudly: it produces
a seam at the wrong pitch for the length of the overlap, which sounds like a mastering problem
rather than a bug. :meth:`~tradefix_radio.audio.pcm.AudioBuffer.require_compatible` refuses the
mismatch outright, and this module is what makes that refusal something callers can satisfy.

Why 48 kHz specifically. It is the rate every audio interface, OBS and virtual cable runs at
natively, so the station's output needs no resampling on the way out — and a resample in the
*output* path would be the one place a glitch is unrecoverable. The provider is free to produce
44.1 kHz; the cost of converting is paid once, off the critical path, when the file is loaded.

The resampler is linear interpolation, and that is a stated compromise rather than an oversight.
See :func:`resample`.
"""

from __future__ import annotations

import numpy as np

from tradefix_radio.audio.pcm import SAMPLE_DTYPE, AudioBuffer

#: The station's internal and output rate. See the module docstring for why 48 kHz.
PLAYOUT_SAMPLE_RATE = 48_000

#: Stereo. Mono would halve the data and lose the §30 transitions' width; more than two
#: channels has nowhere to go — OBS and the virtual cable are stereo.
PLAYOUT_CHANNELS = 2


def resample(buffer: AudioBuffer, sample_rate: int) -> AudioBuffer:
    """Resample to ``sample_rate`` by linear interpolation.

    **A deliberate compromise, stated plainly.** Linear interpolation is not a good resampler:
    it attenuates the top octave and folds a little aliasing back down. A windowed-sinc
    resampler would be correct, and needs either SciPy or several hundred lines here.

    Accepted for now because of where it sits. The one conversion in the station's life is
    44.1 kHz provider output to the 48 kHz playout rate — a ratio of 1.088, where the
    interpolation error is small and sits above 20 kHz. The *output* path never resamples,
    which is the place a defect would be unrecoverable. Phase 6 pulls in librosa for analysis,
    and that is the point at which this should be replaced with
    ``librosa.resample``; it is called out in the Phase 4 report's limitations rather than left
    for someone to discover.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if buffer.sample_rate == sample_rate or buffer.is_empty:
        return AudioBuffer(buffer.samples, sample_rate) if buffer.is_empty else buffer

    ratio = sample_rate / buffer.sample_rate
    target_frames = max(1, round(buffer.frames * ratio))
    # Positions in the *source* timebase that the output frames land on.
    source_index = np.arange(target_frames, dtype=np.float64) / ratio
    original = np.arange(buffer.frames, dtype=np.float64)
    columns = [
        np.interp(source_index, original, buffer.samples[:, channel].astype(np.float64))
        for channel in range(buffer.channels)
    ]
    return AudioBuffer.owning(
        np.stack(columns, axis=1).astype(SAMPLE_DTYPE), sample_rate
    )


def conform(
    buffer: AudioBuffer,
    *,
    sample_rate: int = PLAYOUT_SAMPLE_RATE,
    channels: int = PLAYOUT_CHANNELS,
) -> AudioBuffer:
    """Bring audio into the canonical playout format.

    The single conversion boundary. Called when a track file is loaded for playback, so that
    everything downstream — mixer, crossfade, sink — can assume one rate and one channel count
    rather than checking.

    Idempotent: conforming already-canonical audio returns it unchanged, with no copy. That
    matters because the playout engine calls this on every track and most tracks are already
    right.
    """
    result = buffer
    if result.channels != channels:
        result = result.with_channels(channels)
    if result.sample_rate != sample_rate:
        result = resample(result, sample_rate)
    return result


def is_canonical(
    buffer: AudioBuffer,
    *,
    sample_rate: int = PLAYOUT_SAMPLE_RATE,
    channels: int = PLAYOUT_CHANNELS,
) -> bool:
    """Whether ``buffer`` is already in the playout format.

    Used in assertions and in the soak's accounting, so that "nothing silently assumed a
    different rate" is a property that gets checked rather than hoped for.
    """
    return buffer.sample_rate == sample_rate and buffer.channels == channels


__all__ = [
    "PLAYOUT_CHANNELS",
    "PLAYOUT_SAMPLE_RATE",
    "conform",
    "is_canonical",
    "resample",
]
