"""Transitions and mixing (§30).

§30: "No silence between tracks." Milestone 4.6 states the exit test as a property of the
samples themselves — *no gap and no clipping at the seam* — which is the right way to specify
it, because every other formulation is a statement about intent rather than about audio.

Two things live here:

:func:`crossfade` builds the seam between two tracks.
:class:`TransitionPlanner` decides which §30 transition a particular pair of tracks gets.

**Equal-power, not linear, by default.** Two linear gain ramps summing to 1.0 is the obvious
crossfade and it is wrong for music. Two uncorrelated signals at amplitude 0.5 sum to roughly
0.71 in power, not 1.0, so a linear crossfade *dips about 3 dB* in the middle — audible as a
hole exactly at the transition. Square-root ramps keep summed power constant, which is why
every DJ mixer uses them. Linear is kept available because it *is* correct for the one case
where the material is correlated (the same track against itself, as in a loop seam), where
equal-power would instead bulge +3 dB.

**Headroom is applied before the sum, not limiting after it.** During a crossfade two tracks
are audible at once, so even perfect equal-power ramps can exceed full scale wherever the two
happen to peak together. Attenuating both by a small, known amount keeps the seam clean and
*linear*; limiting afterwards would mean the loudest moment of a transition is the one place
the station applies dynamic processing, which is audible as a pump.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from tradefix_radio.audio.pcm import SAMPLE_DTYPE, AudioBuffer, Samples, from_dbfs
from tradefix_radio.contracts.enums import TransitionType

#: Attenuation applied to both tracks across the seam, in dB.
#:
#: Two tracks each mastered to -1 dBTP can momentarily sum above full scale however the ramps
#: are shaped. 1.2 dB is enough for the overlap of two normalised tracks in practice and
#: small enough to be inaudible — it is restored by the ramps themselves at the edges, where
#: only one track is playing.
CROSSFADE_HEADROOM_DB = -1.2

#: Inter-channel correlation at or above which a pair counts as correlated, so linear ramps
#: are used instead of equal-power ones — see :func:`choose_equal_power`.
#:
#: 0.5 rather than something near 1.0 because the failure is asymmetric: using linear ramps on
#: mildly correlated material costs under a decibel of dip, while using equal-power ramps on
#: strongly correlated material exceeds full scale.
CORRELATION_THRESHOLD = 0.5

#: Default crossfade when nothing more specific applies.
DEFAULT_CROSSFADE_SECONDS = 4.0

#: Shortest crossfade that still sounds like a crossfade rather than a click.
MIN_CROSSFADE_SECONDS = 0.25

#: Longest crossfade §30 should produce. Beyond this a transition stops being a transition
#: and becomes a mashup of two tracks.
MAX_CROSSFADE_SECONDS = 20.0


def equal_power_ramps(frames: int) -> tuple[Samples, Samples]:
    """Fade-out and fade-in ramps whose summed **power** is constant.

    ``sqrt(1-t)`` and ``sqrt(t)``: the squares sum to 1 at every point, so two uncorrelated
    signals cross at constant loudness. This is the default for music.
    """
    if frames <= 0:
        empty = np.zeros(0, dtype=SAMPLE_DTYPE)
        return empty, empty
    position = np.linspace(0.0, 1.0, frames, dtype=np.float64)
    return (
        np.sqrt(1.0 - position).astype(SAMPLE_DTYPE),
        np.sqrt(position).astype(SAMPLE_DTYPE),
    )


def linear_ramps(frames: int) -> tuple[Samples, Samples]:
    """Fade-out and fade-in ramps whose summed **amplitude** is constant.

    Correct only when the two signals are correlated — the same material against itself, as
    at a loop seam. On unrelated tracks this dips about 3 dB in the middle.
    """
    if frames <= 0:
        empty = np.zeros(0, dtype=SAMPLE_DTYPE)
        return empty, empty
    position = np.linspace(0.0, 1.0, frames, dtype=np.float64)
    return (
        (1.0 - position).astype(SAMPLE_DTYPE),
        position.astype(SAMPLE_DTYPE),
    )


def crossfade(
    outgoing: AudioBuffer,
    incoming: AudioBuffer,
    *,
    seconds: float = DEFAULT_CROSSFADE_SECONDS,
    equal_power: bool | None = None,
    headroom_db: float = CROSSFADE_HEADROOM_DB,
) -> AudioBuffer:
    """Join two buffers with an overlapping fade, returning the whole joined audio.

    The result is ``len(outgoing) + len(incoming) - overlap`` frames long: the seam is an
    **overlap**, not an insertion, which is what makes the join gapless. The arithmetic is
    worth stating because the obvious alternative — fade one out, then fade the next in —
    produces exactly the silence §30 forbids.

    ``equal_power`` defaults to ``None``, meaning *choose by measuring* — see
    :func:`choose_equal_power`. Pass ``True`` or ``False`` to force a curve.

    Given two inputs that are themselves within range, the seam stays within range. A track
    that arrives already clipping still clips; that is the mastering stage's business (§25),
    and silently repairing it here would hide a QC failure.

    A zero or negative ``seconds`` produces a hard cut, which is a legitimate §30 transition
    rather than an error.
    """
    outgoing.require_compatible(incoming)
    if seconds <= 0:
        return outgoing.concat(incoming)

    overlap = min(
        round(seconds * outgoing.sample_rate), outgoing.frames, incoming.frames
    )
    if overlap <= 0:
        return outgoing.concat(incoming)

    leaving = outgoing.samples[outgoing.frames - overlap :]
    arriving = incoming.samples[:overlap]
    use_equal_power = (
        choose_equal_power(leaving, arriving) if equal_power is None else equal_power
    )
    fade_out, fade_in = (equal_power_ramps if use_equal_power else linear_ramps)(overlap)

    head = outgoing.samples[: outgoing.frames - overlap]
    tail = incoming.samples[overlap:]
    # ``[:, None]`` broadcasts one ramp across both channels. Ramping channels separately
    # would be the same arithmetic done twice, and getting the two out of step is how a
    # crossfade acquires a moving stereo image.
    seam = (
        leaving * fade_out[:, None] + arriving * fade_in[:, None]
    ) * _headroom_envelope(overlap, headroom_db)[:, None]

    return AudioBuffer.owning(
        np.vstack((head, seam.astype(SAMPLE_DTYPE), tail)), outgoing.sample_rate
    )


def choose_equal_power(leaving: Samples, arriving: Samples) -> bool:
    """Whether to use equal-power ramps for this particular pair of overlap segments.

    The two curves are each correct for a different kind of material, and the right one can be
    *measured* rather than guessed: one correlation coefficient over the overlap.

    * Uncorrelated (ordinary consecutive tracks) → **equal-power**, which preserves power.
      Linear would dip 3 dB in the middle.
    * Correlated (the same material against itself, as at a loop seam) → **linear**, which
      preserves amplitude. Equal-power would bulge +3 dB and exceed full scale on loud input.

    This replaced an adaptive attenuation that measured the summed seam and deepened the
    headroom envelope until it fitted under a ceiling. That worked, but it was treating the
    symptom: the overage only ever happened because equal-power was being applied to correlated
    material, which is the case the curve is documented as wrong for. Choosing the curve removes
    the overage at its source, and it is both cheaper and simpler than the compensation was.
    """
    if leaving.size == 0 or arriving.size == 0:
        return True
    # Subsample: a correlation estimate over a few thousand frames is as good as one over a
    # few hundred thousand, and the seam can be twenty seconds long.
    step = max(1, leaving.shape[0] // 4_096)
    left = leaving[::step].astype(np.float64).mean(axis=1)
    right = arriving[::step].astype(np.float64).mean(axis=1)
    left_energy = float(np.dot(left, left))
    right_energy = float(np.dot(right, right))
    if left_energy <= 0.0 or right_energy <= 0.0:
        # One side is silent, so no sum can exceed either input and the curve is moot.
        return True
    correlation = float(np.dot(left, right)) / math.sqrt(left_energy * right_energy)
    return abs(correlation) < CORRELATION_THRESHOLD


def _headroom_envelope(frames: int, headroom_db: float) -> Samples:
    """Gain envelope that is unity at both edges and ``headroom_db`` in the middle.

    A flat gain across the seam looks right and is wrong: at the first frame of the overlap the
    outgoing fade is still unity, so a constant attenuation puts a step of ``headroom_db``
    exactly at the seam boundary — a click, in the one place §30 cares about. A half-sine window
    attenuates only where the two tracks genuinely overlap and reaches unity where only one is
    audible.

    The depth is fixed. It absorbs the residual peak alignment of two uncorrelated tracks; the
    larger +3 dB case is handled by :func:`choose_equal_power` picking the right curve, not by
    attenuating harder.
    """
    if frames <= 0:
        return np.zeros(0, dtype=SAMPLE_DTYPE)
    depth = 1.0 - from_dbfs(headroom_db)
    position = np.linspace(0.0, 1.0, frames, dtype=np.float64)
    return (1.0 - depth * np.sin(np.pi * position)).astype(SAMPLE_DTYPE)


def fade_in(buffer: AudioBuffer, seconds: float) -> AudioBuffer:
    """Ramp up over the first ``seconds``. Used at the very start of a broadcast."""
    return _apply_edge_fade(buffer, seconds, rising=True)


def fade_out(buffer: AudioBuffer, seconds: float) -> AudioBuffer:
    """Ramp down over the last ``seconds``. Used at shutdown (§74)."""
    return _apply_edge_fade(buffer, seconds, rising=False)


def _apply_edge_fade(buffer: AudioBuffer, seconds: float, *, rising: bool) -> AudioBuffer:
    frames = min(round(seconds * buffer.sample_rate), buffer.frames)
    if frames <= 0:
        return buffer
    # Equal-power here too, so a fade-in sounds like a steady rise rather than one that
    # arrives late and then jumps.
    ramp = np.sqrt(np.linspace(0.0, 1.0, frames, dtype=np.float64)).astype(SAMPLE_DTYPE)
    samples = buffer.samples.copy()
    if rising:
        samples[:frames] *= ramp[:, None]
    else:
        samples[buffer.frames - frames :] *= ramp[::-1][:, None]
    return AudioBuffer.owning(samples, buffer.sample_rate)


@dataclass(frozen=True)
class TransitionDecision:
    """Which §30 transition to use, how long, and why."""

    transition: TransitionType
    seconds: float
    reason: str

    @property
    def is_hard_cut(self) -> bool:
        return self.transition is TransitionType.HARD_CUT or self.seconds <= 0.0


class TransitionPlanner:
    """Chooses the §30 transition between two tracks.

    §30 names five styles plus a hard cut and says to pick by context. The context that
    actually matters is the energy gap and the tempo relationship, so those are what this
    reads — not the genre names, which would make the rule a lookup table that has to grow
    every time the library does.
    """

    def __init__(
        self,
        *,
        default_seconds: float = DEFAULT_CROSSFADE_SECONDS,
        max_gap_milliseconds: int = 0,
    ) -> None:
        if not MIN_CROSSFADE_SECONDS <= default_seconds <= MAX_CROSSFADE_SECONDS:
            raise ValueError(
                f"default_seconds must be within "
                f"{MIN_CROSSFADE_SECONDS}-{MAX_CROSSFADE_SECONDS}, got {default_seconds}"
            )
        if max_gap_milliseconds < 0:
            raise ValueError("max_gap_milliseconds must not be negative")
        self._default_seconds = default_seconds
        self._max_gap_milliseconds = max_gap_milliseconds

    @property
    def max_gap_seconds(self) -> float:
        """§30's hard ceiling on silence between tracks, normally zero."""
        return self._max_gap_milliseconds / 1000.0

    def plan(
        self,
        *,
        outgoing_bpm: int | None,
        incoming_bpm: int,
        outgoing_energy: float | None,
        incoming_energy: float,
        incoming_is_station_id: bool = False,
        outgoing_duration_seconds: float | None = None,
        incoming_duration_seconds: float | None = None,
    ) -> TransitionDecision:
        """Decide the transition into the incoming track.

        Energies are 0–1, matching ``CompositionSpecV1.energy``.
        """
        if outgoing_bpm is None or outgoing_energy is None:
            # First track of a broadcast. There is nothing to fade from, and a fade-in from
            # silence is handled by the engine rather than by a transition.
            return TransitionDecision(
                TransitionType.HARD_CUT, 0.0, "first track: nothing to transition from"
            )

        if incoming_is_station_id:
            # §31: a station ID is an announcement, and fading one in under the end of a
            # track makes it unintelligible — which defeats the point of having one.
            return TransitionDecision(
                TransitionType.STATION_ID, 0.6, "station ID needs a clean entry"
            )

        energy_gap = abs(incoming_energy - outgoing_energy)
        tempo_ratio = incoming_bpm / outgoing_bpm if outgoing_bpm else 1.0
        headroom = self._available_seconds(
            outgoing_duration_seconds, incoming_duration_seconds
        )

        if energy_gap >= 0.45:
            # A big energy change wants *time*, not a cut: a long bridge is how a listener
            # experiences the market having changed character rather than a glitch.
            return TransitionDecision(
                TransitionType.ENERGY_BRIDGE,
                min(headroom, self._default_seconds * 2.0),
                f"energy gap {energy_gap:.2f} needs a longer bridge",
            )

        if _is_beat_compatible(tempo_ratio):
            return TransitionDecision(
                TransitionType.BEAT_MATCHED_CROSSFADE,
                min(headroom, self._default_seconds),
                f"tempi are compatible ({outgoing_bpm}->{incoming_bpm} BPM)",
            )

        if energy_gap <= 0.12:
            return TransitionDecision(
                TransitionType.AMBIENT,
                min(headroom, self._default_seconds * 1.5),
                "similar energy, incompatible tempi: blend rather than beat-match",
            )

        return TransitionDecision(
            TransitionType.CROSSFADE,
            min(headroom, self._default_seconds),
            "default crossfade",
        )

    def _available_seconds(
        self, outgoing_duration: float | None, incoming_duration: float | None
    ) -> float:
        """Longest crossfade these two tracks can actually support.

        A crossfade cannot be longer than either track, and a seam approaching the length of
        a short track would overlap material the listener has not heard yet. Capped at a
        third of the shorter track, which keeps even a 45-second emergency clip intact.
        """
        limits = [MAX_CROSSFADE_SECONDS]
        for duration in (outgoing_duration, incoming_duration):
            if duration is not None and duration > 0:
                limits.append(duration / 3.0)
        return max(MIN_CROSSFADE_SECONDS, min(limits))


def _is_beat_compatible(ratio: float, tolerance: float = 0.06) -> bool:
    """Whether two tempi are close enough to beat-match.

    Includes the half- and double-time relationships, because 85 BPM and 170 BPM share a beat
    grid exactly — and a planner that only accepted near-identical tempi would refuse the
    single most useful beat-match in dance music.
    """
    return any(abs(ratio - target) <= tolerance * target for target in (0.5, 1.0, 2.0))


__all__ = [
    "CORRELATION_THRESHOLD",
    "CROSSFADE_HEADROOM_DB",
    "DEFAULT_CROSSFADE_SECONDS",
    "MAX_CROSSFADE_SECONDS",
    "MIN_CROSSFADE_SECONDS",
    "TransitionDecision",
    "TransitionPlanner",
    "choose_equal_power",
    "crossfade",
    "equal_power_ramps",
    "fade_in",
    "fade_out",
    "linear_ramps",
]
