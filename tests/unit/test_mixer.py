"""Transitions and crossfades (§30, milestone 4.6).

Milestone 4.6's exit test, quoted: "crossfade output has **no gap and no clipping** at the seam
(assert on the PCM)". That phrasing is the right one, and these tests honour it — the
assertions are on samples, not on intent.

Most measurements use **steady-state** signals rather than rendered music. Measured on music,
a loudness profile across a seam moves because the music moves, and a 2 dB reading proves
nothing. On two uncorrelated signals of constant level the only thing that can change the
measurement is the crossfade itself, which is what makes the numbers mean something.
"""

from __future__ import annotations

import numpy as np
import pytest

from tradefix_radio.audio.mixer import (
    CROSSFADE_HEADROOM_DB,
    DEFAULT_CROSSFADE_SECONDS,
    MAX_CROSSFADE_SECONDS,
    MIN_CROSSFADE_SECONDS,
    TransitionPlanner,
    choose_equal_power,
    crossfade,
    equal_power_ramps,
    fade_in,
    fade_out,
    linear_ramps,
)
from tradefix_radio.audio.pcm import SAMPLE_DTYPE, AudioBuffer, to_dbfs
from tradefix_radio.contracts.enums import TransitionType

SR = 44_100


def steady(seconds: float, *, amplitude: float = 0.2, seed: int = 0) -> AudioBuffer:
    """Uncorrelated noise at a constant level — a measurement signal, not music."""
    rng = np.random.default_rng(seed)
    frames = round(seconds * SR)
    return AudioBuffer(
        (rng.standard_normal((frames, 2)) * amplitude).astype(SAMPLE_DTYPE), SR
    )


def constant(seconds: float, value: float = 0.5) -> AudioBuffer:
    frames = round(seconds * SR)
    return AudioBuffer(np.full((frames, 2), value, dtype=SAMPLE_DTYPE), SR)


def window_dbfs(buffer: AudioBuffer, start: int, frames: int) -> float:
    segment = buffer.samples[start : start + frames]
    if segment.size == 0:
        return -200.0
    return to_dbfs(float(np.sqrt(np.mean(np.square(segment, dtype=np.float64)))))


# ---------------------------------------------------------------- ramps


def test_equal_power_ramps_keep_summed_power_constant() -> None:
    """``sqrt(1-t)`` and ``sqrt(t)``: the squares sum to 1 everywhere."""
    out, into = equal_power_ramps(1000)
    power = out.astype(np.float64) ** 2 + into.astype(np.float64) ** 2
    assert np.allclose(power, 1.0, atol=1e-6)


def test_linear_ramps_keep_summed_amplitude_constant() -> None:
    out, into = linear_ramps(1000)
    assert np.allclose(out.astype(np.float64) + into.astype(np.float64), 1.0, atol=1e-6)


@pytest.mark.parametrize("builder", [equal_power_ramps, linear_ramps])
def test_ramps_start_and_end_at_unity(builder: object) -> None:
    """Otherwise the seam has a step at its boundary, which is a click."""
    out, into = builder(500)  # type: ignore[operator]
    assert out[0] == pytest.approx(1.0)
    assert out[-1] == pytest.approx(0.0, abs=1e-6)
    assert into[0] == pytest.approx(0.0, abs=1e-6)
    assert into[-1] == pytest.approx(1.0)


@pytest.mark.parametrize("builder", [equal_power_ramps, linear_ramps])
def test_zero_length_ramps_are_empty_not_an_error(builder: object) -> None:
    out, into = builder(0)  # type: ignore[operator]
    assert out.size == 0 and into.size == 0


# ---------------------------------------------------------------- no gap


def test_the_crossfade_overlaps_rather_than_inserting() -> None:
    """The arithmetic that makes the join gapless.

    Fade one out, *then* fade the next in, and the result is longer than the inputs and has
    the silence §30 forbids in the middle. An overlap is shorter than the sum by exactly the
    overlap length — which is the property worth pinning, because it is what distinguishes the
    two implementations.
    """
    first, second = steady(5.0, seed=1), steady(5.0, seed=2)
    joined = crossfade(first, second, seconds=2.0)
    assert joined.frames == first.frames + second.frames - round(2.0 * SR)


def test_there_is_no_silence_anywhere_in_the_seam() -> None:
    """§30 states it as "no silence between tracks", so that is what is measured."""
    first, second = steady(4.0, seed=1), steady(4.0, seed=2)
    joined = crossfade(first, second, seconds=1.5)
    overlap = round(1.5 * SR)
    seam_start = first.frames - overlap
    window = SR // 100
    quietest = min(
        window_dbfs(joined, position, window)
        for position in range(seam_start, seam_start + overlap - window, window)
    )
    assert quietest > -30.0, f"seam dropped to {quietest:.1f} dBFS"


def test_the_seam_has_no_discontinuity_at_its_boundaries() -> None:
    """A step in gain at the seam edge is a click, and it is in the worst possible place.

    A real defect, caught here: the headroom attenuation was applied flat across the overlap,
    so at the first frame — where the outgoing fade is still unity — the signal stepped down by
    the full headroom amount. The fix is a half-sine envelope that reaches unity at both edges.
    """
    first, second = constant(2.0, 0.5), constant(2.0, 0.5)
    joined = crossfade(first, second, seconds=1.0)
    overlap = round(1.0 * SR)
    # The bound is 1 % of the signal (-40 dB) rather than zero. An equal-power ramp uses
    # ``sqrt``, whose derivative is infinite at zero, so the incoming track's first sample
    # necessarily jumps to ``sqrt(1/overlap)`` of its level — about 0.5 % here, and inaudible.
    # What this rules out is the 1.2 dB step a flat headroom gain produced.
    for boundary in (first.frames - overlap, first.frames):
        around = joined.samples[boundary - 3 : boundary + 3, 0].astype(np.float64)
        step = float(np.max(np.abs(np.diff(around))))
        assert step < 0.005, f"gain step of {step:.4f} at frame {boundary}"


def test_a_zero_length_crossfade_is_a_clean_concatenation() -> None:
    """A hard cut is a legitimate §30 transition, not an error."""
    first, second = steady(1.0, seed=1), steady(1.0, seed=2)
    joined = crossfade(first, second, seconds=0.0)
    assert joined.frames == first.frames + second.frames


def test_a_crossfade_longer_than_the_tracks_is_clamped() -> None:
    """Emergency Tier 2 clips can be 45 seconds; a 20-second fade must not consume one."""
    first, second = steady(1.0, seed=1), steady(1.0, seed=2)
    joined = crossfade(first, second, seconds=30.0)
    assert joined.frames == first.frames  # fully overlapped, nothing lost or invented


# ---------------------------------------------------------------- no clipping


def test_the_crossfade_introduces_no_clipping() -> None:
    """Two tracks are audible at once, so the seam is where a sum can exceed full scale."""
    first = constant(2.0, 0.9)
    second = constant(2.0, -0.9)
    joined = crossfade(first, second, seconds=1.0)
    assert joined.peak() <= 1.0
    assert joined.clipped_sample_ratio() == 0.0


def test_two_loud_correlated_tracks_stay_in_range() -> None:
    """The worst case for a sum: identical material, both near full scale.

    Equal-power ramps on correlated material bulge +3 dB, which the 1.2 dB headroom cannot
    absorb — forcing them here peaks at 1.179. Left to choose, the crossfade measures the
    correlation and uses linear ramps, which preserve amplitude exactly.
    """
    hot = constant(2.0, 0.95)
    assert crossfade(hot, hot, seconds=1.0, equal_power=True).peak() > 1.0
    joined = crossfade(hot, hot, seconds=1.0)
    assert joined.peak() <= 1.0, f"peak {joined.peak():.3f}"
    assert joined.clipped_sample_ratio() == 0.0


def test_the_seam_stays_in_range_for_loud_in_range_input() -> None:
    """Swept across amplitudes and both correlation cases.

    Hostile-but-legal input: tracks mastered right up against full scale. Input that already
    clips is excluded on purpose — repairing that here would hide a §24 QC failure.
    """
    for amplitude in (0.3, 0.7, 0.9):
        for same in (False, True):
            first = steady(3.0, amplitude=amplitude, seed=1).normalised_to_peak(-0.5)
            second = (
                first if same else steady(3.0, amplitude=amplitude, seed=2)
                .normalised_to_peak(-0.5)
            )
            overlap = round(1.5 * SR)
            joined = crossfade(first, second, seconds=1.5)
            seam = joined.slice_frames(first.frames - overlap, first.frames)
            assert seam.peak() <= 1.0, (
                f"amplitude {amplitude}, same={same}: seam peak {seam.peak():.3f}"
            )
            assert seam.clipped_sample_ratio() == 0.0


def test_the_curve_is_chosen_by_measuring_correlation() -> None:
    """The mechanism, tested directly rather than only through its consequences."""
    rng = np.random.default_rng(3)
    signal = (rng.standard_normal((20_000, 2)) * 0.3).astype(SAMPLE_DTYPE)
    other = (rng.standard_normal((20_000, 2)) * 0.3).astype(SAMPLE_DTYPE)
    assert choose_equal_power(signal, other) is True
    assert choose_equal_power(signal, signal) is False
    assert choose_equal_power(signal, -signal) is False


def test_a_silent_side_does_not_confuse_the_curve_choice() -> None:
    """A correlation coefficient needs energy on both sides; zero would divide by zero."""
    rng = np.random.default_rng(4)
    signal = (rng.standard_normal((5_000, 2)) * 0.3).astype(SAMPLE_DTYPE)
    silence = np.zeros((5_000, 2), dtype=SAMPLE_DTYPE)
    assert choose_equal_power(signal, silence) is True
    assert choose_equal_power(silence, silence) is True


def test_a_quiet_seam_is_attenuated_only_by_the_configured_headroom() -> None:
    """The headroom is fixed, so a quiet transition is not dragged down by a loud one."""
    quiet = constant(2.0, 0.1)
    # Forced equal-power: this is about the envelope depth, not the curve choice.
    joined = crossfade(quiet, quiet, seconds=1.0, equal_power=True)
    overlap = round(1.0 * SR)
    centre_frame = quiet.frames - overlap // 2
    centre = float(abs(joined.samples[centre_frame, 0]))
    # Correlated material, equal-power: 2 * 0.1 * 0.707 = 0.141, times the 1.2 dB headroom.
    assert centre == pytest.approx(0.141 * 0.871, abs=0.005)


def test_no_nan_or_infinity_reaches_the_output() -> None:
    joined = crossfade(steady(2.0, seed=1), steady(2.0, seed=2), seconds=1.0)
    assert not np.isnan(joined.samples).any()
    assert not np.isinf(joined.samples).any()


# ---------------------------------------------------------------- loudness


def test_an_equal_power_crossfade_holds_its_level_across_the_seam() -> None:
    """The reason equal-power is the default, measured.

    Deviation is compared against the deliberate headroom rather than against zero: the
    crossfade is power-preserving, and the ~1.2 dB dip in the middle is the headroom doing its
    job, not a flaw in the ramps.
    """
    first, second = steady(6.0, seed=1), steady(6.0, seed=2)
    joined = crossfade(first, second, seconds=3.0)
    overlap = round(3.0 * SR)
    seam = first.frames - overlap
    window = SR // 10

    base = window_dbfs(joined, seam - 10 * window, window)
    worst = max(
        abs(window_dbfs(joined, position, window) - base)
        for position in range(seam, seam + overlap - window, window)
    )
    # The headroom accounts for most of it; the residual is the ramps' own error.
    assert worst <= abs(CROSSFADE_HEADROOM_DB) + 0.4, f"deviation {worst:.2f} dB"


def test_a_linear_crossfade_dips_about_three_decibels() -> None:
    """The textbook result, and the reason linear is not the default.

    Two uncorrelated signals at amplitude 0.5 sum to ~0.71 in power, not 1.0. Asserting the
    dip exists — rather than only that equal-power is better — is what keeps the docstring's
    claim honest.
    """
    first, second = steady(6.0, seed=1), steady(6.0, seed=2)
    joined = crossfade(first, second, seconds=3.0, equal_power=False)
    overlap = round(3.0 * SR)
    seam = first.frames - overlap
    window = SR // 10

    base = window_dbfs(joined, seam - 10 * window, window)
    middle = window_dbfs(joined, seam + overlap // 2 - window // 2, window)
    dip = base - middle - abs(CROSSFADE_HEADROOM_DB)
    assert 2.0 < dip < 4.5, f"linear dip was {dip:.2f} dB, expected about 3"


def test_equal_power_beats_linear_on_uncorrelated_material() -> None:
    first, second = steady(6.0, seed=1), steady(6.0, seed=2)
    overlap = round(3.0 * SR)
    seam = first.frames - overlap
    window = SR // 10

    def mid_level(equal_power: bool) -> float:
        joined = crossfade(first, second, seconds=3.0, equal_power=equal_power)
        return window_dbfs(joined, seam + overlap // 2 - window // 2, window)

    assert mid_level(equal_power=True) > mid_level(equal_power=False) + 2.0


# ---------------------------------------------------------------- validation


def test_a_sample_rate_mismatch_is_refused() -> None:
    other = AudioBuffer(np.zeros((48_000, 2), dtype=SAMPLE_DTYPE), 48_000)
    with pytest.raises(ValueError, match="sample rate mismatch"):
        crossfade(steady(1.0), other)


# ---------------------------------------------------------------- edge fades


def test_fade_in_starts_silent_and_reaches_full_level() -> None:
    """Used at the start of a broadcast, where there is nothing to fade from."""
    faded = fade_in(constant(2.0, 0.5), 1.0)
    assert abs(float(faded.samples[0, 0])) < 0.01
    assert float(faded.samples[round(1.0 * SR) + 100, 0]) == pytest.approx(0.5, abs=0.01)


def test_fade_out_ends_silent(tmp_path: object) -> None:
    """§74's graceful shutdown: the station stops, it does not cut."""
    faded = fade_out(constant(2.0, 0.5), 1.0)
    assert abs(float(faded.samples[-1, 0])) < 0.01
    assert float(faded.samples[0, 0]) == pytest.approx(0.5, abs=0.01)


def test_a_zero_length_fade_is_a_no_op() -> None:
    buffer = constant(1.0, 0.5)
    assert fade_in(buffer, 0.0) is buffer
    assert fade_out(buffer, 0.0) is buffer


def test_a_fade_longer_than_the_buffer_is_clamped() -> None:
    faded = fade_in(constant(0.5, 0.5), 10.0)
    assert faded.frames == round(0.5 * SR)
    assert abs(float(faded.samples[0, 0])) < 0.01


# ---------------------------------------------------------------- TransitionPlanner


def planner(**overrides: object) -> TransitionPlanner:
    return TransitionPlanner(**overrides)  # type: ignore[arg-type]


def test_the_first_track_of_a_broadcast_has_nothing_to_fade_from() -> None:
    decision = planner().plan(
        outgoing_bpm=None, incoming_bpm=120,
        outgoing_energy=None, incoming_energy=0.5,
    )
    assert decision.transition is TransitionType.HARD_CUT
    assert decision.is_hard_cut
    assert "first track" in decision.reason


def test_a_station_id_gets_a_clean_entry() -> None:
    """§31: fading an announcement in under a track makes it unintelligible."""
    decision = planner().plan(
        outgoing_bpm=120, incoming_bpm=120,
        outgoing_energy=0.5, incoming_energy=0.5,
        incoming_is_station_id=True,
    )
    assert decision.transition is TransitionType.STATION_ID
    assert decision.seconds < 1.0


def test_a_large_energy_change_gets_a_longer_bridge() -> None:
    """§29: a genuine market shift should be something a listener experiences as a change."""
    decision = planner().plan(
        outgoing_bpm=120, incoming_bpm=121,
        outgoing_energy=0.15, incoming_energy=0.9,
    )
    assert decision.transition is TransitionType.ENERGY_BRIDGE
    assert decision.seconds > DEFAULT_CROSSFADE_SECONDS


def test_compatible_tempi_are_beat_matched() -> None:
    decision = planner().plan(
        outgoing_bpm=124, incoming_bpm=126,
        outgoing_energy=0.5, incoming_energy=0.55,
    )
    assert decision.transition is TransitionType.BEAT_MATCHED_CROSSFADE


def test_half_and_double_time_count_as_beat_compatible() -> None:
    """85 and 170 BPM share a beat grid exactly — the most useful beat-match there is."""
    for outgoing, incoming in ((85, 170), (170, 85), (70, 140)):
        decision = planner().plan(
            outgoing_bpm=outgoing, incoming_bpm=incoming,
            outgoing_energy=0.5, incoming_energy=0.52,
        )
        assert decision.transition is TransitionType.BEAT_MATCHED_CROSSFADE, (
            f"{outgoing} -> {incoming}"
        )


def test_similar_energy_with_incompatible_tempi_blends() -> None:
    decision = planner().plan(
        outgoing_bpm=96, incoming_bpm=137,
        outgoing_energy=0.5, incoming_energy=0.52,
    )
    assert decision.transition is TransitionType.AMBIENT


def test_an_ordinary_pair_gets_an_ordinary_crossfade() -> None:
    decision = planner().plan(
        outgoing_bpm=96, incoming_bpm=137,
        outgoing_energy=0.4, incoming_energy=0.72,
    )
    assert decision.transition is TransitionType.CROSSFADE
    assert decision.seconds == pytest.approx(DEFAULT_CROSSFADE_SECONDS)


def test_a_short_track_limits_the_crossfade_length() -> None:
    """A seam approaching the length of a short track overlaps unheard material."""
    decision = planner().plan(
        outgoing_bpm=120, incoming_bpm=121,
        outgoing_energy=0.1, incoming_energy=0.95,
        incoming_duration_seconds=6.0,
    )
    assert decision.seconds <= 2.0


def test_every_planned_transition_is_usable_by_the_crossfade() -> None:
    """A planner that produced a length the mixer rejects would stall playout.

    Inputs are normalised first. Gaussian noise at amplitude 0.2 reaches 5.5 sigma somewhere in
    1.3 million samples, so the raw test signal peaked at 1.12 before any crossfade touched
    it — the failure was the fixture, not the mixer, and asserting on it would have been
    asserting that the mixer repairs clipped input, which it deliberately does not.
    """
    first = steady(30.0, seed=1).normalised_to_peak(-1.0)
    second = steady(30.0, seed=2).normalised_to_peak(-1.0)
    for outgoing_bpm, incoming_bpm, outgoing_energy, incoming_energy in (
        (70, 175, 0.05, 0.95),
        (124, 126, 0.5, 0.5),
        (96, 137, 0.5, 0.52),
        (120, 120, 0.4, 0.7),
    ):
        decision = planner().plan(
            outgoing_bpm=outgoing_bpm, incoming_bpm=incoming_bpm,
            outgoing_energy=outgoing_energy, incoming_energy=incoming_energy,
            outgoing_duration_seconds=30.0, incoming_duration_seconds=30.0,
        )
        assert MIN_CROSSFADE_SECONDS <= decision.seconds <= MAX_CROSSFADE_SECONDS
        joined = crossfade(first, second, seconds=decision.seconds)
        assert joined.peak() <= 1.0
        assert joined.frames > max(first.frames, second.frames)


def test_the_default_gap_ceiling_is_zero() -> None:
    """§30 forbids silence between tracks, and the configured default says so."""
    assert planner().max_gap_seconds == 0.0


def test_an_out_of_range_default_length_is_rejected() -> None:
    with pytest.raises(ValueError, match="default_seconds must be within"):
        planner(default_seconds=0.01)
    with pytest.raises(ValueError, match="default_seconds must be within"):
        planner(default_seconds=120.0)


def test_a_negative_gap_ceiling_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_gap_milliseconds"):
        planner(max_gap_milliseconds=-1)
