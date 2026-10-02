"""Procedural audio synthesis (§62, §33).

§62 requires the mock provider to produce **real** audio, quickly. That matters more than it
sounds: a provider returning silence or white noise would let every downstream stage — QC,
fingerprinting, loudness, crossfades, the mixer — be written against input that cannot
exercise them. Silence has no tempo to detect, no spectrum to fingerprint and no seam to
hear.

So this is a small synthesiser, not a signal generator. It renders from a
:class:`~tradefix_radio.contracts.music.MusicBlueprintV1`'s own numbers, which has a useful
consequence: **the §1 chain becomes audible end to end**. A quiet market produces a 75 BPM
sparse pad; a breakout produces a 170 BPM track with a hard kick on every beat. If the
director's energy mapping breaks, the mock radio sounds wrong — a far better test than any
assertion.

What it is not: musical. The output is recognisably a drum machine and three oscillators. It
exists to be *valid, varied and measurable* audio, and ACE-Step replaces it in Phase 7.

Everything is vectorised NumPy. A 4-minute stereo track at 44.1 kHz is 21 million samples,
and a per-sample Python loop takes minutes — which would make §62's "quickly" false and the
soak test impractical.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from tradefix_radio.audio.pcm import SAMPLE_DTYPE, AudioBuffer

#: Internal working signal: mono, float64. Rendering happens in float64 and converts to
#: float32 once, at the end — intermediate sums and envelopes accumulate rounding, and a
#: float32 pad summed 8 times is measurably noisier than the same pad in float64.
Mono = npt.NDArray[np.float64]

#: Semitone offsets of the scale degrees used for melodic content, by mode family.
#:
#: Only two families, because the point is that a minor-key blueprint sounds different from a
#: major-key one — not to model modal harmony. ``keys.py`` knows about 84 keys; the
#: synthesiser needs to know whether to sound bright or dark.
_MAJOR_STEPS = (0, 2, 4, 5, 7, 9, 11)
_MINOR_STEPS = (0, 2, 3, 5, 7, 8, 10)

#: Pitch classes, for parsing a key name's tonic.
_PITCH_CLASS: dict[str, int] = {
    "c": 0, "c#": 1, "db": 1, "d": 2, "d#": 3, "eb": 3, "e": 4, "f": 5,
    "f#": 6, "gb": 6, "g": 7, "g#": 8, "ab": 8, "a": 9, "a#": 10, "bb": 10, "b": 11,
}

#: Stereo widening: how much delayed signal is blended into the right channel, and by how
#: long. Tuned for an inter-channel correlation near 0.9, which is where real records sit —
#: see :meth:`Synthesiser._to_stereo` for why a fully decorrelated image is wrong.
_WIDEN_MIX = 0.22
_WIDEN_DELAY_SECONDS = 0.006

#: Box-filter lengths, in samples at any rate. Both are moving averages: see
#: :func:`_moving_average` for why a crude filter is the right tool here.
#:
#: ``_HAT_HIGHPASS_TAPS`` sets where the hat's noise band starts (~2-3 kHz at 44.1 kHz).
#: ``_MIX_LOWPASS_TAPS`` tames the naive sawtooth's aliasing across the whole mix.
_HAT_HIGHPASS_TAPS = 8
_MIX_LOWPASS_TAPS = 3

#: Modes that read as dark, and modes that read as bright. Anything in neither list defaults
#: to dark — see :func:`parse_key`.
_DARK_MODES = frozenset(
    {"minor", "harmonic minor", "melodic minor", "dorian", "phrygian", "aeolian", "locrian"}
)
_BRIGHT_MODES = frozenset({"major", "ionian", "lydian", "mixolydian"})


def parse_key(name: str) -> tuple[int, bool]:
    """``"C# harmonic minor"`` → ``(1, True)`` — tonic pitch class and whether it is dark.

    Tolerant on purpose. A key name reaches here from the content library and from
    persisted blueprints, and an unparseable one must not stop a track from being
    generated: an unknown tonic defaults to A and an unknown mode to minor, which is the
    most common case in this library and inaudibly wrong at worst.
    """
    text = name.strip().lower()
    if not text:
        return 9, True
    tonic_text, _, mode_text = text.partition(" ")
    tonic = _PITCH_CLASS.get(tonic_text, 9)
    mode = mode_text.strip() or "minor"
    if mode in _DARK_MODES:
        return tonic, True
    if mode in _BRIGHT_MODES:
        return tonic, False
    # Unrecognised mode. Defaults to dark, which this library's keys mostly are — and which
    # is what the docstring promises. An earlier version fell through to bright, so an
    # unparseable mode silently flipped the track's character rather than guessing the
    # common case.
    return tonic, True


@dataclass(frozen=True)
class SynthesisSpec:
    """What to render. Derived from a blueprint, but independent of it.

    Deliberately not taking a ``MusicBlueprintV1``: the Tier 3 procedural generator (§33)
    needs this synthesiser and has no blueprint, and the unit tests need to sweep one
    parameter at a time.
    """

    duration_seconds: float
    bpm: int
    key: str = "A minor"
    #: 0–1. Drives layer count, brightness and overall density.
    energy: float = 0.5
    rhythm_density: float = 0.5
    bass_intensity: float = 0.5
    drum_intensity: float = 0.5
    melodic_complexity: float = 0.5
    sample_rate: int = 44_100
    channels: int = 2
    #: Section boundaries as fractions of the whole, so structure is audible.
    sections: tuple[str, ...] = ()
    seed: int = 0

    def __post_init__(self) -> None:
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        if not 40 <= self.bpm <= 220:
            raise ValueError(f"bpm {self.bpm} outside the 40-220 range")
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if self.channels not in (1, 2):
            raise ValueError("channels must be 1 or 2")


class Synthesiser:
    """Renders a :class:`SynthesisSpec` to PCM."""

    def __init__(self, spec: SynthesisSpec) -> None:
        self._spec = spec
        # Seeded per track, so the same blueprint renders identically. §64's endurance
        # replays and §22's duplicate detection both depend on that.
        self._rng = np.random.default_rng(spec.seed)
        self._frames = max(1, round(spec.duration_seconds * spec.sample_rate))
        self._time = np.arange(self._frames, dtype=np.float64) / spec.sample_rate
        tonic, dark = parse_key(spec.key)
        self._steps = _MINOR_STEPS if dark else _MAJOR_STEPS
        # Root around A2–A3 depending on key, which keeps bass audible without muddying.
        self._root_hz = 110.0 * (2.0 ** (tonic / 12.0))
        self._beat_seconds = 60.0 / spec.bpm

    # -- public ------------------------------------------------------------

    def render(self) -> AudioBuffer:
        """Render the whole track.

        Layers are summed, then the sum is scaled to a sensible peak. Scaling after summing
        rather than budgeting each layer is deliberate: a 4-layer mix and a 7-layer mix would
        otherwise differ in level by 5 dB, and §24's loudness window would reject the busy
        one while passing the sparse one — a QC failure caused entirely by arrangement.
        """
        spec = self._spec
        envelope = self._arrangement_envelope()

        mono: Mono = np.zeros(self._frames, dtype=np.float64)
        mono += self._pad() * (0.55 - 0.2 * spec.energy)
        mono += self._bass() * (0.35 + 0.45 * spec.bass_intensity)
        mono += self._kick() * (0.4 + 0.5 * spec.drum_intensity)
        mono += self._hats() * (0.08 + 0.2 * spec.rhythm_density)
        if spec.melodic_complexity > 0.25:
            mono += self._lead() * (0.18 + 0.3 * spec.melodic_complexity)
        if spec.energy > 0.55:
            mono += self._noise_sweep() * (0.05 + 0.12 * spec.energy)

        mono *= envelope
        # Gentle overall low-pass. The naive sawtooth aliases above a few kHz (see
        # :func:`_saw`), and without this the aliasing products dominate the top of the
        # spectrum. Brightness still rises with energy — the layers that are added at high
        # energy are the bright ones — but it now does so over a plausible range rather than
        # from one implausible value to another.
        mono = _moving_average(mono, _MIX_LOWPASS_TAPS)
        buffer = self._to_stereo(mono)
        # -3 dBFS leaves room for §25 mastering to work and keeps the §24 clip ratio at
        # zero, so a QC failure in the mock pipeline means a real defect.
        return buffer.normalised_to_peak(-3.0)

    # -- layers ------------------------------------------------------------

    def _pad(self) -> Mono:
        """Sustained chord. Two detuned saws per note, which is what makes it a pad."""
        chord = [self._steps[0], self._steps[2], self._steps[4]]
        if self._spec.energy > 0.7:
            chord.append(self._steps[4] + 12)
        out = np.zeros(self._frames, dtype=np.float64)
        for degree in chord:
            hz = self._root_hz * 2.0 ** (degree / 12.0) * 2.0
            detune = 1.0 + 0.004 * self._rng.uniform(-1.0, 1.0)
            out += _saw(self._time, hz) + _saw(self._time, hz * detune)
        out /= 2.0 * len(chord)
        # Slow filter movement, so a 4-minute pad is not 4 minutes of the same timbre.
        return out * (0.75 + 0.25 * np.sin(2.0 * math.pi * 0.05 * self._time))

    def _bass(self) -> Mono:
        """Sine bass on an eighth-note pattern, following the chord root."""
        pattern = self._rhythm_grid(
            subdivision=2, hit_probability=0.55 + 0.35 * self._spec.rhythm_density
        )
        hz = self._root_hz / 2.0
        tone = np.sin(2.0 * math.pi * hz * self._time)
        # A touch of second harmonic so the bass is audible on small speakers and has
        # something for a spectral fingerprint to hold onto.
        tone += 0.25 * np.sin(2.0 * math.pi * hz * 2.0 * self._time)
        gated: Mono = tone * self._gate(
            pattern, attack_ms=4.0, decay_ms=self._beat_seconds * 400.0
        )
        return gated

    def _kick(self) -> Mono:
        """Pitch-swept sine with a fast decay — a drum machine kick."""
        grid = self._rhythm_grid(subdivision=1, hit_probability=1.0)
        out = np.zeros(self._frames, dtype=np.float64)
        # One hit shape, rendered once and stamped at each grid position. The downward
        # pitch sweep is what makes it read as a kick rather than as a low blip.
        decay_frames = int(0.18 * self._spec.sample_rate)
        local = np.arange(decay_frames) / self._spec.sample_rate
        pitch = 120.0 * np.exp(-local * 28.0) + 45.0
        phase = 2.0 * math.pi * np.cumsum(pitch) / self._spec.sample_rate
        hit = np.sin(phase) * np.exp(-local * 16.0)
        for start in np.flatnonzero(grid):
            end = min(self._frames, start + decay_frames)
            out[start:end] += hit[: end - start]
        return out

    def _hats(self) -> Mono:
        """Filtered noise bursts on sixteenths. Carries the perceived tempo."""
        subdivision = 4 if self._spec.rhythm_density > 0.45 else 2
        grid = self._rhythm_grid(
            subdivision=subdivision, hit_probability=0.4 + 0.5 * self._spec.rhythm_density
        )
        noise = self._rng.standard_normal(self._frames)
        # High-passed by subtracting a short moving average, which puts the corner around
        # 2-3 kHz. The first version used ``np.diff``, a +6 dB/octave differentiator: it
        # pushed almost all the energy toward Nyquist and dragged the whole mix's spectral
        # centroid up to 9.5 kHz, where no real record sits. Worse, it made brightness
        # insensitive to everything else — a quiet track and a violent one measured within
        # 16 % of each other, so §1's energy mapping was inaudible in the spectrum.
        noise = noise - _moving_average(noise, _HAT_HIGHPASS_TAPS)
        gated: Mono = noise * self._gate(grid, attack_ms=0.5, decay_ms=55.0)
        return gated

    def _lead(self) -> Mono:
        """Square-wave melody over the scale, quantised to the beat grid."""
        steps_per_bar = 8
        bar_seconds = self._beat_seconds * 4
        total_steps = max(1, int(self._spec.duration_seconds / bar_seconds * steps_per_bar))
        degrees = self._rng.choice(len(self._steps), size=total_steps)
        octaves = self._rng.choice([0, 12], size=total_steps, p=[0.75, 0.25])

        out = np.zeros(self._frames, dtype=np.float64)
        step_frames = max(1, self._frames // total_steps)
        rests = self._rng.random(total_steps) > (0.45 + 0.4 * self._spec.melodic_complexity)
        for index in range(total_steps):
            if rests[index]:
                continue
            start = index * step_frames
            end = min(self._frames, start + step_frames)
            if end <= start:
                break
            hz = self._root_hz * 4.0 * 2.0 ** (
                (self._steps[int(degrees[index])] + int(octaves[index])) / 12.0
            )
            local = self._time[: end - start]
            note = np.sign(np.sin(2.0 * math.pi * hz * local)) * 0.5
            out[start:end] += note * np.exp(-local * 6.0)
        return out

    def _noise_sweep(self) -> Mono:
        """Riser into each section boundary. Only present at high energy."""
        out = np.zeros(self._frames, dtype=np.float64)
        if not self._spec.sections:
            return out
        noise = self._rng.standard_normal(self._frames) * 0.5
        sweep_frames = int(min(2.0, self._beat_seconds * 4) * self._spec.sample_rate)
        for boundary in self._section_boundaries()[1:]:
            start = max(0, boundary - sweep_frames)
            if boundary <= start:
                continue
            ramp = np.linspace(0.0, 1.0, boundary - start) ** 2
            out[start:boundary] += noise[start:boundary] * ramp
        return out

    # -- arrangement -------------------------------------------------------

    def _section_boundaries(self) -> list[int]:
        """Frame index where each section starts."""
        count = len(self._spec.sections) or 1
        return [int(self._frames * index / count) for index in range(count)]

    def _arrangement_envelope(self) -> Mono:
        """Per-section level, plus fades at both ends.

        Section levels make §8's structure audible — an intro is quieter than a hook — and
        the fades exist so the raw provider output has no DC step at its edges, which would
        read as a click and as a §24 clipping failure.
        """
        envelope = np.ones(self._frames, dtype=np.float64)
        boundaries = [*self._section_boundaries(), self._frames]
        for index, name in enumerate(self._spec.sections):
            start, end = boundaries[index], boundaries[index + 1]
            envelope[start:end] *= _section_level(name)

        fade = min(int(0.05 * self._spec.sample_rate), self._frames // 2)
        if fade > 0:
            envelope[:fade] *= np.linspace(0.0, 1.0, fade)
            envelope[-fade:] *= np.linspace(1.0, 0.0, fade)
        return envelope

    def _rhythm_grid(
        self, *, subdivision: int, hit_probability: float
    ) -> npt.NDArray[np.bool_]:
        """Boolean array marking hit frames, ``subdivision`` hits per beat."""
        grid = np.zeros(self._frames, dtype=bool)
        step = self._beat_seconds / subdivision
        count = max(1, int(self._spec.duration_seconds / step))
        hits = self._rng.random(count) < min(1.0, hit_probability)
        # Downbeats always sound, whatever the dice say. A pattern that can drop every
        # downbeat has no detectable tempo, and tempo is the one musical property §22's
        # fingerprinting and §30's beat-matched transitions both rely on.
        hits[:: subdivision * 4] = True
        indices = (np.flatnonzero(hits) * step * self._spec.sample_rate).astype(np.int64)
        grid[indices[indices < self._frames]] = True
        return grid

    def _gate(self, grid: npt.NDArray[np.bool_], *, attack_ms: float, decay_ms: float) -> Mono:
        """Per-hit amplitude envelope over a boolean grid.

        One envelope shape, stamped at each hit position with
        :func:`numpy.maximum` so overlapping tails take the louder value rather than summing
        past full scale.

        **Not** ``np.convolve``, which was the first implementation and made a 3½-minute
        track take minutes to render. ``np.convolve`` is direct, so it costs O(frames ×
        kernel): the bass gate's kernel is ~14 000 samples against 9.3 million frames, which
        is 1.3·10¹¹ multiply-adds. Stamping costs O(hits × kernel) — a few thousand hits, so
        four orders of magnitude less work for identical output. §62's "quickly" is a
        requirement, and the soak test depends on it.
        """
        rate = self._spec.sample_rate
        attack = max(1, int(attack_ms / 1000.0 * rate))
        # Cap the tail: a slow-tempo bass note asked for a 320 ms decay, and a kernel longer
        # than the gap between hits is inaudible beyond the next hit anyway.
        decay = max(1, min(int(decay_ms / 1000.0 * rate), int(0.5 * rate)))
        shape = np.concatenate(
            (
                np.linspace(0.0, 1.0, attack),
                np.exp(-np.arange(decay) / (decay / 4.0)),
            )
        )
        envelope = np.zeros(self._frames, dtype=np.float64)
        for start in np.flatnonzero(grid):
            end = min(self._frames, start + shape.shape[0])
            np.maximum(envelope[start:end], shape[: end - start], out=envelope[start:end])
        return envelope

    def _to_stereo(self, mono: Mono) -> AudioBuffer:
        """Widen to stereo by mixing in a little delayed signal on one side.

        A duplicated mono signal is *correct* but measures as perfectly correlated, which
        makes every stereo-width check downstream trivially pass.

        The amount matters. Putting the delayed copy on the right **wholesale** gave an
        inter-channel correlation of −0.004 — fully decorrelated, which no real record is:
        it sounds phasey, and summing to mono cancels most of the content. Real music sits
        around 0.9, so only :data:`_WIDEN_MIX` of the delayed signal is blended in. The
        result has genuine width to measure and still survives a mono fold-down, which is
        what an OBS listener on a phone speaker actually hears.
        """
        samples = np.asarray(mono, dtype=SAMPLE_DTYPE)
        if self._spec.channels == 1:
            return AudioBuffer.owning(samples.reshape(-1, 1), self._spec.sample_rate)
        delay = max(1, int(_WIDEN_DELAY_SECONDS * self._spec.sample_rate))
        delayed = np.concatenate((np.zeros(delay, dtype=SAMPLE_DTYPE), samples[:-delay]))
        right = (1.0 - _WIDEN_MIX) * samples + _WIDEN_MIX * delayed
        return AudioBuffer.owning(
            np.stack((samples, right.astype(SAMPLE_DTYPE)), axis=1),
            self._spec.sample_rate,
        )


def _section_level(name: str) -> float:
    """Relative level for a §8 section name. Unknown names play at full level."""
    lowered = name.lower()
    if "intro" in lowered or "outro" in lowered:
        return 0.6
    if "break" in lowered or "bridge" in lowered:
        return 0.75
    if "drop" in lowered or "hook" in lowered or "chorus" in lowered:
        return 1.0
    return 0.88


def _moving_average(signal: Mono, taps: int) -> Mono:
    """Box-filter low-pass, O(n) via a cumulative sum.

    A moving average is a crude filter — its stopband ripples — but it is the only one
    available without SciPy that runs in linear time over nine million samples, and "crude
    low-pass" is exactly what is wanted here. The first null sits at ``sample_rate / taps``.

    Padded at the front so the output stays the same length and nothing shifts in time; a
    shifted layer would smear the drum transients the §30 beat-matching relies on.
    """
    if taps <= 1:
        return signal
    padded = np.concatenate((np.zeros(taps - 1, dtype=np.float64), signal))
    cumulative = np.cumsum(padded, dtype=np.float64)
    averaged: Mono = (cumulative[taps - 1 :] - np.concatenate(
        ([0.0], cumulative[: signal.shape[0] - 1])
    )) / taps
    return averaged


def _saw(time: Mono, hz: float) -> Mono:
    """Band-limited-ish sawtooth.

    Not a true band-limited saw — the naive form aliases above roughly 5 kHz at 44.1 kHz.
    Acceptable here and worth stating: aliasing adds high-frequency content, which makes the
    mock output *harder* for QC and fingerprinting than a clean source would be, so nothing
    downstream is being flattered.
    """
    phase = time * hz
    wave: Mono = 2.0 * (phase - np.floor(phase + 0.5))
    return wave


def render(spec: SynthesisSpec) -> AudioBuffer:
    """Convenience wrapper: render ``spec`` to PCM."""
    return Synthesiser(spec).render()


__all__ = ["Mono", "SynthesisSpec", "Synthesiser", "parse_key", "render"]
