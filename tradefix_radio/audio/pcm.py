"""PCM audio as a value object (ADR-06).

Everything downstream of generation — QC, mastering, mixing, playout — works on the same
representation: **float32, shaped ``(frames, channels)``, nominal range [-1, 1]**.

Three decisions worth stating, because each rules out a class of bug that is painful to
find once audio is flowing:

**Float32, not int16.** Intermediate mixing in int16 clips on every sum and quantises on
every gain change; a crossfade is a sum with two gain ramps, so int16 would introduce
audible artefacts at exactly the seam §30 cares about. Conversion to the device's integer
format happens once, in the sink.

**Frames-major, not channels-major.** ``buffer[1000:2000]`` is a slice of *time*, which is
what every caller actually wants — a crossfade region, a fade-in window, a block for the
sink. Channels-major would make the common operation a strided copy.

**Always 2-D, even for mono.** A silent ``(frames,)`` shape is the single most common source
of broadcasting bugs in audio code: ``mono + stereo`` succeeds, produces the wrong shape,
and the error surfaces three functions away. :class:`AudioBuffer` normalises on
construction, so there is exactly one shape in the system.

The range is *nominal*, not enforced. Generation and mastering legitimately produce
out-of-range samples; the whole point of :meth:`AudioBuffer.peak` and the §24 QC rules is to
measure that rather than to have silently clamped it earlier.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

#: The sample dtype used everywhere in-process.
SAMPLE_DTYPE = np.float32

#: Amplitude at or above which a sample counts as clipped (§24).
#:
#: Slightly below 1.0 because a converter that clamps at exactly 1.0 produces a run of
#: identical maximum samples, and that run is the audible artefact. Catching 0.999 catches
#: the flat top; waiting for 1.0 catches only the samples that were already destroyed.
CLIP_THRESHOLD = 0.999

#: Amplitude below which a frame counts as silent, as dBFS (§24's silence ratio).
SILENCE_DBFS = -60.0

#: Floor returned instead of -inf for a fully silent measurement.
#:
#: -inf is mathematically right and ruinous in practice: it propagates through every
#: average, comparison and JSON serialisation downstream. -200 dBFS is ~70 dB below the
#: quietest meaningful signal in a 32-bit float, so nothing real is misreported.
MIN_DBFS = -200.0

Samples = npt.NDArray[np.float32]


def to_dbfs(amplitude: float) -> float:
    """Linear amplitude to dBFS, with a floor instead of -inf."""
    if amplitude <= 0.0:
        return MIN_DBFS
    return max(MIN_DBFS, 20.0 * math.log10(amplitude))


def from_dbfs(dbfs: float) -> float:
    """dBFS to linear amplitude."""
    return float(10.0 ** (dbfs / 20.0))


@dataclass(frozen=True)
class AudioBuffer:
    """Immutable PCM audio with its sample rate.

    "Immutable" is by convention on the samples array. NumPy has no cheap deep freeze, and
    copying on construction would dominate the playout budget — a 4-minute stereo track is
    75 MB, and the engine builds buffers constantly.

    Instead, buffers produced by this class's **own** operations are marked read-only, via
    :meth:`owning`. That is where the real risk lives: a slice handed to the sink while the
    mixer still holds the parent, where an in-place write would corrupt audio that is already
    playing. Those failures are now loud.

    A buffer constructed from a **caller's** array leaves that array alone. An earlier version
    tried to detect ownership with ``owndata and base is None`` and froze the caller's array
    too, because a freshly allocated ``np.zeros`` is indistinguishable from one allocated
    here — so merely constructing a buffer made the caller's own array unwritable, somewhere
    else entirely. Ownership is something the caller knows and NumPy does not, so it is now
    stated rather than guessed.
    """

    samples: Samples
    sample_rate: int

    @classmethod
    def owning(cls, samples: Samples, sample_rate: int) -> AudioBuffer:
        """Wrap an array this buffer may take ownership of, marking it read-only.

        For results of computation, never for an array the caller still uses.
        """
        buffer = cls(samples, sample_rate)
        if buffer.samples.flags.owndata:
            buffer.samples.flags.writeable = False
        return buffer

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError(f"sample_rate must be positive, got {self.sample_rate}")

        data = np.asarray(self.samples)
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        elif data.ndim != 2:
            raise ValueError(
                f"samples must be 1-D (mono) or 2-D (frames, channels), got shape "
                f"{data.shape}"
            )
        if data.shape[1] not in (1, 2):
            raise ValueError(
                f"samples must have 1 or 2 channels, got {data.shape[1]}. A "
                "channels-major array of shape (channels, frames) is the usual cause — "
                "this class is frames-major"
            )
        if data.dtype != SAMPLE_DTYPE:
            data = data.astype(SAMPLE_DTYPE, copy=False)
        if not data.flags.c_contiguous:
            data = np.ascontiguousarray(data)
        object.__setattr__(self, "samples", data)

    # -- shape -------------------------------------------------------------

    @property
    def frames(self) -> int:
        return int(self.samples.shape[0])

    @property
    def channels(self) -> int:
        return int(self.samples.shape[1])

    @property
    def duration_seconds(self) -> float:
        return self.frames / self.sample_rate

    @property
    def is_empty(self) -> bool:
        return self.frames == 0

    def __len__(self) -> int:
        return self.frames

    # -- construction ------------------------------------------------------

    @classmethod
    def silence(cls, *, seconds: float, sample_rate: int, channels: int = 2) -> AudioBuffer:
        if seconds < 0:
            raise ValueError("seconds must not be negative")
        frames = round(seconds * sample_rate)
        return cls.owning(np.zeros((frames, channels), dtype=SAMPLE_DTYPE), sample_rate)

    @classmethod
    def from_mono(cls, mono: Samples, sample_rate: int, *, channels: int = 2) -> AudioBuffer:
        """Duplicate a mono signal across ``channels``.

        A deliberate duplicate rather than a broadcast view: the result is written to by
        mixing and by effects, and a view over one channel's memory would alias them.
        """
        flat = np.asarray(mono, dtype=SAMPLE_DTYPE).reshape(-1, 1)
        if channels == 1:
            return cls.owning(flat, sample_rate)
        return cls.owning(np.repeat(flat, channels, axis=1), sample_rate)

    # -- slicing and shaping ----------------------------------------------

    def slice_frames(self, start: int, end: int | None = None) -> AudioBuffer:
        """A frame range. Clamped to the buffer, so callers need no bounds arithmetic."""
        stop = self.frames if end is None else end
        start = max(0, min(start, self.frames))
        stop = max(start, min(stop, self.frames))
        return AudioBuffer(self.samples[start:stop], self.sample_rate)

    def slice_seconds(self, start: float, end: float | None = None) -> AudioBuffer:
        return self.slice_frames(
            round(start * self.sample_rate),
            None if end is None else round(end * self.sample_rate),
        )

    def with_channels(self, channels: int) -> AudioBuffer:
        """Up-mix mono to stereo or down-mix stereo to mono.

        Down-mixing averages rather than summing: summing two correlated channels doubles
        the amplitude and clips material that was perfectly in range.
        """
        if channels == self.channels:
            return self
        if channels == 1:
            return AudioBuffer.owning(
                self.samples.mean(axis=1, keepdims=True), self.sample_rate
            )
        if channels == 2:
            return AudioBuffer.owning(np.repeat(self.samples, 2, axis=1), self.sample_rate)
        raise ValueError(f"unsupported channel count {channels}")

    def padded_to(self, frames: int) -> AudioBuffer:
        """Zero-pad to ``frames``; truncate if already longer."""
        if frames == self.frames:
            return self
        if frames < self.frames:
            return self.slice_frames(0, frames)
        pad = np.zeros((frames - self.frames, self.channels), dtype=SAMPLE_DTYPE)
        return AudioBuffer.owning(np.vstack((self.samples, pad)), self.sample_rate)

    def concat(self, other: AudioBuffer) -> AudioBuffer:
        self.require_compatible(other)
        return AudioBuffer.owning(
            np.vstack((self.samples, other.samples)), self.sample_rate
        )

    # -- gain --------------------------------------------------------------

    def scaled(self, gain: float) -> AudioBuffer:
        return AudioBuffer.owning(self.samples * SAMPLE_DTYPE(gain), self.sample_rate)

    def normalised_to_peak(self, target_dbfs: float) -> AudioBuffer:
        """Scale so the peak sits at ``target_dbfs``. Silence is returned unchanged."""
        peak = self.peak()
        if peak <= 0.0:
            return self
        return self.scaled(from_dbfs(target_dbfs) / peak)

    def clipped(self) -> AudioBuffer:
        """Hard-limit into [-1, 1]. The last step before an integer sink, never before."""
        return AudioBuffer.owning(np.clip(self.samples, -1.0, 1.0), self.sample_rate)

    # -- measurement (§24) -------------------------------------------------

    def peak(self) -> float:
        if self.is_empty:
            return 0.0
        return float(np.max(np.abs(self.samples)))

    def peak_dbfs(self) -> float:
        return to_dbfs(self.peak())

    def rms(self) -> float:
        if self.is_empty:
            return 0.0
        return float(np.sqrt(np.mean(np.square(self.samples, dtype=np.float64))))

    def rms_dbfs(self) -> float:
        return to_dbfs(self.rms())

    def dc_offset(self) -> float:
        """Largest per-channel mean (§24).

        Per channel, not overall: equal and opposite offsets on two channels average to
        zero and would report a clean signal while both channels are in fact offset.
        """
        if self.is_empty:
            return 0.0
        return float(np.max(np.abs(np.mean(self.samples, axis=0, dtype=np.float64))))

    def clipped_sample_ratio(self, threshold: float = CLIP_THRESHOLD) -> float:
        """Fraction of individual samples at or above the clip threshold (§24)."""
        if self.is_empty:
            return 0.0
        return float(np.mean(np.abs(self.samples) >= threshold))

    def silence_ratio(
        self, *, threshold_dbfs: float = SILENCE_DBFS, window_ms: float = 50.0
    ) -> float:
        """Fraction of the buffer that is silent (§24).

        Measured over short windows rather than per sample, because every waveform crosses
        zero twice a cycle: a per-sample test reports a loud 100 Hz tone as roughly 3 %
        silent, and the figure then varies with frequency rather than with silence.
        """
        if self.is_empty:
            return 0.0
        window = max(1, round(window_ms / 1000.0 * self.sample_rate))
        mono = np.abs(self.samples).max(axis=1)
        usable = (mono.shape[0] // window) * window
        if usable == 0:
            return float(mono.max() < from_dbfs(threshold_dbfs))
        blocks = mono[:usable].reshape(-1, window).max(axis=1)
        return float(np.mean(blocks < from_dbfs(threshold_dbfs)))

    def leading_silence_seconds(
        self, *, threshold_dbfs: float = SILENCE_DBFS
    ) -> float:
        """How much silence precedes the first audible sample (§25 trimming)."""
        return self._edge_silence(threshold_dbfs, reverse=False)

    def trailing_silence_seconds(
        self, *, threshold_dbfs: float = SILENCE_DBFS
    ) -> float:
        return self._edge_silence(threshold_dbfs, reverse=True)

    def _edge_silence(self, threshold_dbfs: float, *, reverse: bool) -> float:
        if self.is_empty:
            return 0.0
        mono = np.abs(self.samples).max(axis=1)
        audible = np.flatnonzero(mono >= from_dbfs(threshold_dbfs))
        if audible.size == 0:
            return self.duration_seconds
        index = (mono.shape[0] - 1 - int(audible[-1])) if reverse else int(audible[0])
        return index / self.sample_rate

    # -- internals ---------------------------------------------------------

    def require_compatible(self, other: AudioBuffer) -> None:
        """Raise unless ``other`` can be combined with this buffer directly."""
        if other.sample_rate != self.sample_rate:
            raise ValueError(
                f"sample rate mismatch: {self.sample_rate} vs {other.sample_rate}. "
                "Resample before combining; silently reinterpreting one rate as another "
                "changes pitch and duration"
            )
        if other.channels != self.channels:
            raise ValueError(
                f"channel mismatch: {self.channels} vs {other.channels}; use "
                "with_channels() to agree first"
            )


__all__ = [
    "CLIP_THRESHOLD",
    "MIN_DBFS",
    "SAMPLE_DTYPE",
    "SILENCE_DBFS",
    "AudioBuffer",
    "Samples",
    "from_dbfs",
    "to_dbfs",
]
