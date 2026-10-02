"""Procedural synthesis (§62, §33, milestone 4.1).

Milestone 4.1's exit test: "output is valid audio of requested duration, passes the Phase-6 QC
rules, differs per seed."

The QC half is tested here against the §24 thresholds the configuration actually ships with,
rather than against invented numbers — the whole point of §62's "real audio" requirement is
that the mock's output survives the same pipeline a model's output will.

The §1 half is the interesting part: a quiet market and a violent one must produce measurably
different audio, because that is the end of the chain that starts at XAUUSD. Those tests
measure spectral centroid and detected onset density rather than inspecting parameters, so
they would still catch a synthesiser that accepted the blueprint and ignored it.
"""

from __future__ import annotations

import numpy as np
import pytest

from tradefix_radio.audio.pcm import AudioBuffer
from tradefix_radio.audio.synthesis import SynthesisSpec, Synthesiser, parse_key, render
from tradefix_radio.config.schema import QualityControlSettings

SHORT = 8.0


def spec(**overrides: object) -> SynthesisSpec:
    base: dict[str, object] = {"duration_seconds": SHORT, "bpm": 120, "seed": 11}
    base.update(overrides)
    return SynthesisSpec(**base)  # type: ignore[arg-type]


def onset_rate(buffer: AudioBuffer) -> float:
    """Detected onsets per second, from the amplitude envelope.

    A crude detector on purpose: it must measure the audio rather than trust the spec, so a
    synthesiser that accepted ``bpm`` and ignored it would fail the tests that use this.

    The threshold is a fraction of the **largest** rise, not a percentile. A percentile
    threshold is self-defeating: asking for the 97th percentile returns 3 % of the windows by
    construction, so every input measured as exactly 3 onsets per second and the two tests
    using it could never fail.
    """
    mono = np.abs(buffer.samples).max(axis=1)
    window = max(1, buffer.sample_rate // 200)
    usable = (mono.shape[0] // window) * window
    if usable == 0:
        return 0.0
    envelope = mono[:usable].reshape(-1, window).max(axis=1)
    rise = np.diff(envelope, prepend=envelope[0])
    peak_rise = float(rise.max())
    if peak_rise <= 0:
        return 0.0
    # Local maxima above a fixed fraction of the strongest rise, so one onset is counted
    # once rather than once per window of its attack.
    strong = rise >= peak_rise * 0.25
    onsets = int(np.sum(strong & ~np.concatenate(([False], strong[:-1]))))
    return onsets / buffer.duration_seconds


def spectral_centroid(buffer: AudioBuffer) -> float:
    """Centre of mass of the spectrum, in Hz. The usual proxy for brightness."""
    mono = buffer.with_channels(1).samples[:, 0].astype(np.float64)
    spectrum = np.abs(np.fft.rfft(mono * np.hanning(mono.shape[0])))
    freqs = np.fft.rfftfreq(mono.shape[0], 1.0 / buffer.sample_rate)
    total = float(spectrum.sum())
    return float((spectrum * freqs).sum() / total) if total > 0 else 0.0


# ---------------------------------------------------------------- key parsing


@pytest.mark.parametrize(
    ("name", "tonic", "dark"),
    [
        ("C major", 0, False),
        ("A minor", 9, True),
        ("F# minor", 6, True),
        ("Bb major", 10, False),
        ("C# harmonic minor", 1, True),
        ("D dorian", 2, True),
        ("Ab mixolydian", 8, False),
    ],
)
def test_keys_parse_to_a_tonic_and_a_brightness(name: str, tonic: int, dark: bool) -> None:
    assert parse_key(name) == (tonic, dark)


def test_an_unparseable_key_defaults_rather_than_raising() -> None:
    """A key name reaches here from persisted blueprints, and one typo must not block a track.

    Defaulting is the right call because the cost is a track in the wrong key — inaudible
    against a synthesiser this crude — while raising would mean the queue loses a slot.
    """
    assert parse_key("") == (9, True)
    assert parse_key("Zz quasi-lydian") == (9, True)


# ---------------------------------------------------------------- validity


def test_rendered_audio_has_the_requested_duration() -> None:
    for seconds in (5.0, 30.0, 120.0):
        assert render(spec(duration_seconds=seconds)).duration_seconds == pytest.approx(
            seconds, abs=0.01
        )


def test_rendered_audio_is_stereo_float_and_in_range() -> None:
    buffer = render(spec())
    assert buffer.channels == 2
    assert buffer.samples.dtype == np.float32
    assert buffer.peak() <= 1.0
    assert not np.isnan(buffer.samples).any()
    assert not np.isinf(buffer.samples).any()


def test_mono_rendering_is_available() -> None:
    assert render(spec(channels=1)).channels == 1


def test_a_spec_with_an_impossible_duration_is_rejected() -> None:
    with pytest.raises(ValueError, match="duration_seconds must be positive"):
        spec(duration_seconds=0.0)


def test_a_spec_with_an_out_of_contract_bpm_is_rejected() -> None:
    """The §8 contract allows 40-220; a synthesiser accepting more would mask a director bug."""
    with pytest.raises(ValueError, match="outside the 40-220 range"):
        spec(bpm=300)


# ---------------------------------------------------------------- §24 QC


@pytest.mark.parametrize("energy", [0.05, 0.3, 0.6, 0.95])
def test_rendered_audio_passes_the_shipped_qc_thresholds(energy: float) -> None:
    """Milestone 4.1: the mock's output must survive the real §24 rules.

    Loudness is checked as RMS rather than LUFS — LUFS needs the Phase 6 analysis stack — and
    the §24 window is wide enough that the distinction does not matter for a pass/fail here.
    """
    qc = QualityControlSettings()
    buffer = render(
        spec(
            duration_seconds=60.0,
            energy=energy,
            rhythm_density=energy,
            bass_intensity=energy,
            drum_intensity=energy,
            melodic_complexity=energy,
        )
    )
    assert buffer.duration_seconds >= qc.min_duration_seconds
    assert buffer.silence_ratio() <= qc.max_silence_ratio
    assert buffer.clipped_sample_ratio() <= qc.max_clipped_sample_ratio
    assert buffer.dc_offset() <= qc.max_dc_offset
    assert qc.min_loudness_lufs <= buffer.rms_dbfs() <= qc.max_loudness_lufs


def test_rendered_audio_has_no_leading_or_trailing_silence_to_speak_of() -> None:
    """§25 trims edges; a synthesiser that padded its output would waste a quarter of a track."""
    buffer = render(spec(duration_seconds=20.0))
    assert buffer.leading_silence_seconds() < 0.2
    assert buffer.trailing_silence_seconds() < 0.2


def test_the_arrangement_fades_both_edges_to_avoid_a_click() -> None:
    """A hard edge is a DC step, which reads as a click and as a §24 clipping failure."""
    buffer = render(spec(duration_seconds=20.0))
    rate = buffer.sample_rate
    assert buffer.slice_frames(0, rate // 1000).peak() < buffer.peak() * 0.5
    assert buffer.slice_frames(buffer.frames - rate // 1000).peak() < buffer.peak() * 0.5


# ---------------------------------------------------------------- determinism


def test_the_same_seed_renders_identical_audio() -> None:
    """§64's replays and §22's duplicate detection both depend on this."""
    first, second = render(spec(seed=7)), render(spec(seed=7))
    assert np.array_equal(first.samples, second.samples)


def test_different_seeds_render_different_audio() -> None:
    first, second = render(spec(seed=7)), render(spec(seed=8))
    assert not np.array_equal(first.samples, second.samples)


def test_seeds_produce_broadly_distinct_output_in_bulk() -> None:
    """Milestone 4.1's "differs per seed", measured rather than asserted on one pair."""
    rendered = [render(spec(duration_seconds=4.0, seed=seed)) for seed in range(12)]
    checksums = {float(np.sum(np.abs(buffer.samples), dtype=np.float64)) for buffer in rendered}
    assert len(checksums) == len(rendered)


# ---------------------------------------------------------------- §1 audibility


def test_tempo_is_audible_in_the_rendered_audio() -> None:
    """Not "the spec said 175" — the detected onset density has to rise with BPM.

    This is what would catch a synthesiser that accepted ``bpm`` and ignored it, which no
    parameter assertion can detect.

    The margin is 1.4x for a 2.5x tempo change, not 2.5x, because onsets merge: at 175 BPM a
    kick's 180 ms tail overlaps the next one, so any envelope-based detector undercounts the
    fast case. Asserting the full ratio would be asserting a property of the detector.
    """
    slow = onset_rate(render(spec(duration_seconds=20.0, bpm=70, rhythm_density=0.8)))
    fast = onset_rate(render(spec(duration_seconds=20.0, bpm=175, rhythm_density=0.8)))
    assert fast > slow * 1.4, f"{slow:.1f} vs {fast:.1f} onsets/s"


def test_rhythm_density_is_audible() -> None:
    """Measured as brightness, not as onset count.

    The hats carry rhythmic density and live above 2 kHz, while the amplitude envelope any
    onset detector reads is dominated by the kick — which is present on every beat at any
    density. So onset count barely moves (3.8 to 4.2) while the spectrum moves a lot.
    """
    sparse = spectral_centroid(render(spec(duration_seconds=20.0, rhythm_density=0.1)))
    dense = spectral_centroid(render(spec(duration_seconds=20.0, rhythm_density=0.95)))
    assert dense > sparse * 1.25, f"{sparse:.0f} Hz vs {dense:.0f} Hz"


def test_energy_changes_the_spectrum() -> None:
    """§1: a quiet market and a violent one must not sound the same.

    Brightness rather than level, because level is normalised to a fixed peak — so a
    synthesiser that only changed loudness would fail this.
    """
    quiet = spectral_centroid(
        render(spec(duration_seconds=20.0, energy=0.05, rhythm_density=0.15,
                    drum_intensity=0.15, melodic_complexity=0.1))
    )
    violent = spectral_centroid(
        render(spec(duration_seconds=20.0, energy=0.95, rhythm_density=0.95,
                    drum_intensity=0.95, melodic_complexity=0.9))
    )
    assert violent > quiet * 1.25, f"{quiet:.0f} Hz vs {violent:.0f} Hz"


def test_the_spectrum_sits_where_music_sits() -> None:
    """A sanity bound on brightness, which caught a real defect.

    The first version high-passed the hats with ``np.diff`` — a differentiator — which put the
    whole mix's centroid at 9.5 kHz. Nothing downstream broke, but the mock stopped resembling
    music in the one dimension Phase 6's fingerprinting cares most about, *and* it compressed
    the quiet-versus-violent difference to 16 %, making §1 inaudible in the spectrum.
    """
    for energy in (0.1, 0.5, 0.9):
        centroid = spectral_centroid(
            render(spec(duration_seconds=15.0, energy=energy, rhythm_density=energy))
        )
        assert 1_000 < centroid < 8_000, f"energy {energy}: centroid {centroid:.0f} Hz"


def test_a_dark_key_and_a_bright_key_differ() -> None:
    minor = render(spec(duration_seconds=10.0, key="A minor"))
    major = render(spec(duration_seconds=10.0, key="A major"))
    assert not np.array_equal(minor.samples, major.samples)


def test_the_tonic_sets_the_pitch() -> None:
    """Two keys a tritone apart must not render identically."""
    a_minor = render(spec(duration_seconds=10.0, key="A minor"))
    eb_minor = render(spec(duration_seconds=10.0, key="Eb minor"))
    assert spectral_centroid(a_minor) != pytest.approx(spectral_centroid(eb_minor), rel=0.01)


def test_structure_is_audible_as_level_changes() -> None:
    """§8's structure should be something a listener experiences, not metadata."""
    buffer = render(
        spec(duration_seconds=40.0, sections=("intro", "verse", "hook", "outro"))
    )
    quarter = buffer.frames // 4
    levels = [
        buffer.slice_frames(index * quarter, (index + 1) * quarter).rms()
        for index in range(4)
    ]
    # The intro and outro are deliberately quieter than the hook.
    assert levels[2] > levels[0]
    assert levels[2] > levels[3]


def test_a_section_list_is_optional() -> None:
    assert render(spec(sections=())).duration_seconds == pytest.approx(SHORT, abs=0.01)


# ---------------------------------------------------------------- stereo


def test_the_stereo_image_is_wide_but_mono_compatible() -> None:
    """A fully decorrelated image measures as "wide" and cancels on a mono fold-down.

    Which is what an OBS listener on a phone speaker hears, so it is the case that matters.
    An earlier version put the delayed signal on the right wholesale and reached a correlation
    of -0.004 — no real record does that.
    """
    buffer = render(spec(duration_seconds=10.0))
    correlation = float(np.corrcoef(buffer.samples[:, 0], buffer.samples[:, 1])[0, 1])
    assert 0.85 < correlation < 0.999, f"inter-channel correlation {correlation:.3f}"
    folded = buffer.with_channels(1)
    assert folded.rms_dbfs() > buffer.rms_dbfs() - 1.0


# ---------------------------------------------------------------- performance


def test_rendering_is_much_faster_than_real_time() -> None:
    """§62 says "quickly", and the soak test depends on it.

    The first implementation used ``np.convolve`` for the per-hit envelopes, which is direct
    and therefore O(frames x kernel) — a 3.5-minute track took minutes. Stamping the envelope
    at each hit is the same output for four orders of magnitude less work. Asserted loosely so
    the test reports a regression rather than machine speed.
    """
    import time

    started = time.perf_counter()
    buffer = render(spec(duration_seconds=120.0, energy=0.9, rhythm_density=0.9))
    elapsed = time.perf_counter() - started
    assert elapsed < buffer.duration_seconds / 10, (
        f"rendered {buffer.duration_seconds:.0f}s of audio in {elapsed:.1f}s"
    )


def test_a_very_short_render_still_produces_audio() -> None:
    """Tier 3 procedural audio asks for short blocks; none of the layers may divide by zero."""
    buffer = Synthesiser(spec(duration_seconds=0.25)).render()
    assert buffer.frames > 0
    assert buffer.peak() > 0.0
