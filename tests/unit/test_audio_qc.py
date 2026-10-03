"""Audio QC and feature extraction (§6.1, §6.2, §6.19).

The organising principle of these tests is §6.1's instruction not to collapse validation into
one Boolean. Almost every test therefore asserts on a *named check* rather than on the overall
verdict: `result.status is FAIL` would pass just as well if the wrong check failed, and a
rejection attributed to the wrong cause is a bug that reaches an operator as a wrong answer.
"""

from __future__ import annotations

import numpy as np
import pytest

from tests.audio_fixtures import (
    clicking,
    clipped,
    dc_offset,
    gapped,
    mono_cancelling,
    musical,
    noise,
    quiet_ambient,
    silence,
    very_short,
)
from tests.conftest import make_blueprint, make_settings
from tradefix_radio.audio.analysis import extract_features, librosa_available
from tradefix_radio.audio.pcm import AudioBuffer
from tradefix_radio.audio.qc import QcStage, QcStatus, run_audio_qc
from tradefix_radio.config.schema import AppSettings

pytestmark = pytest.mark.skipif(
    not librosa_available(), reason="librosa is required for feature extraction"
)


@pytest.fixture
def qc_settings(tmp_path, clean_environ) -> AppSettings:
    """Production settings with only the minimum-duration floor lowered.

    The fixtures are seconds long because feature extraction over minutes of audio would make
    this file take minutes to run. Every *other* threshold is left exactly as configured —
    lowering a threshold to make a test pass is precisely what the brief forbids, so the one
    relaxation here is the one that is an artefact of fixture length rather than of audio
    quality, and the duration check keeps its real floor in its own test below.
    """
    return make_settings(
        tmp_path, clean_environ, qc={"min_duration_seconds": 1.0}
    )


def _qc(buffer: AudioBuffer, settings: AppSettings, **kwargs: object):
    features = extract_features(buffer)
    return run_audio_qc(
        track_id="TF-TEST",
        features=features,
        settings=settings.qc,
        stage=QcStage.RAW,
        **kwargs,  # type: ignore[arg-type]
    )


def _check(result, name: str):
    found = next((check for check in result.checks if check.name == name), None)
    assert found is not None, f"no check named {name!r} in {[c.name for c in result.checks]}"
    return found


# ------------------------------------------------------------------ passing


def test_clean_music_passes_every_check(qc_settings: AppSettings) -> None:
    result = _qc(musical(), qc_settings)
    failures = [check.name for check in result.failures]
    assert failures == [], f"clean audio failed: {failures}"
    assert result.passed


def test_every_check_reports_its_own_status_value_and_threshold(
    qc_settings: AppSettings,
) -> None:
    """§6.1: each check carries name, status, value, threshold and reason.

    Asserted structurally rather than by spot-checking one field, because the requirement is
    about the *shape* of every check — a single check added later without a threshold would
    otherwise slip through.
    """
    result = _qc(musical(), qc_settings)
    assert len(result.checks) >= 15
    for check in result.checks:
        assert check.name
        assert isinstance(check.status, QcStatus)
        assert check.threshold, f"{check.name} states no threshold"
        assert check.reason, f"{check.name} gives no reason"


def test_quiet_ambient_is_not_failed_for_being_quiet(qc_settings: AppSettings) -> None:
    """§6.1, verbatim: a quiet ambient track must not fail simply because RMS is low."""
    blueprint = make_blueprint("TF-AMBIENT", genre="lofi", energy=8.0, composition_energy=0.08, bpm=70)
    result = _qc(quiet_ambient(), qc_settings, blueprint=blueprint)
    loudness = _check(result, "integrated_loudness")
    assert loudness.status is not QcStatus.FAIL, loudness.reason
    assert _check(result, "silence_ratio").status is not QcStatus.FAIL


def test_the_quiet_allowance_is_bounded(qc_settings: AppSettings) -> None:
    """The relaxation has a limit, and a low-energy blueprint cannot wave anything through.

    Paired with the test above, which is the point. On its own, "quiet ambient passes" is
    satisfied by deleting the loudness check entirely; this one fails if anyone does. The
    audio here is two orders of magnitude below the ambient fixture — inaudible rather than
    quiet — and it must fail *despite* a blueprint asking for the quietest possible track.

    Note what is **not** asserted: that quiet audio fails when the blueprint asked for a loud
    track. At the raw stage that would be wrong — mastering exists precisely to set level, and
    rejecting a track mastering was about to fix would reject most of what the generator makes.
    Loudness against intent is checked after mastering, where it is a real question.
    """
    blueprint = make_blueprint("TF-WHISPER", genre="lofi", energy=1.0, composition_energy=0.01, bpm=60)
    inaudible = AudioBuffer(
        (np.asarray(quiet_ambient().samples) * 0.003).astype(np.float32),
        quiet_ambient().sample_rate,
    )
    result = _qc(inaudible, qc_settings, blueprint=blueprint)
    assert _check(result, "integrated_loudness").status is QcStatus.FAIL


# ------------------------------------------------------------------ failures


def test_silence_fails_and_says_so(qc_settings: AppSettings) -> None:
    """Silence fails on the checks that are *about* silence.

    Asserting the specific checks rather than the overall verdict is the §6.1 discipline: a
    silent file failing because its duration was wrong would satisfy a Boolean assertion and
    tell an operator the wrong thing. Note that ``peak_level`` correctly passes — it is a
    ceiling check, and zero is under every ceiling.
    """
    result = _qc(silence(), qc_settings)
    assert result.status is QcStatus.FAIL
    assert _check(result, "non_empty").status is QcStatus.FAIL
    assert _check(result, "rms").status is QcStatus.FAIL
    assert _check(result, "silence_ratio").status is QcStatus.FAIL
    assert _check(result, "peak_level").status is not QcStatus.FAIL
    assert "silent" in result.summary().lower()


def test_clipping_is_detected_at_the_injected_ratio(qc_settings: AppSettings) -> None:
    result = _qc(clipped(ratio=0.08), qc_settings)
    check = _check(result, "clipping")
    assert check.status is QcStatus.FAIL
    assert check.value is not None
    assert check.value == pytest.approx(0.08, abs=0.02)


def test_dc_offset_is_detected(qc_settings: AppSettings) -> None:
    check = _check(_qc(dc_offset(offset=0.2), qc_settings), "dc_offset")
    assert check.status is QcStatus.FAIL
    assert check.value is not None
    assert check.value == pytest.approx(0.2, abs=0.02)


def test_a_long_internal_gap_fails(qc_settings: AppSettings) -> None:
    check = _check(_qc(gapped(gap_seconds=7.0), qc_settings), "internal_silence")
    assert check.status is QcStatus.FAIL
    assert check.value is not None
    assert check.value >= 6.0


def test_discontinuities_are_counted(qc_settings: AppSettings) -> None:
    check = _check(_qc(clicking(clicks=64), qc_settings), "discontinuities")
    assert check.status is QcStatus.FAIL
    assert check.value is not None
    assert check.value >= 32


def test_a_very_short_track_fails_duration(settings: AppSettings) -> None:
    check = _check(_qc(very_short(), settings), "duration")
    assert check.status is QcStatus.FAIL


def test_anti_phase_stereo_is_not_reported_as_silent(qc_settings: AppSettings) -> None:
    """The regression this file exists for.

    Measuring level on the mono fold reported a perfectly loud anti-phase track as digitally
    silent. The track *is* defective — it collapses to nothing in mono — but it must be failed
    for mono incompatibility, with the real level reported, not for a silence that is not
    there. A wrong diagnosis sends an operator looking for a dead renderer.
    """
    result = _qc(mono_cancelling(), qc_settings)
    peak = _check(result, "peak_level")
    assert peak.status is not QcStatus.FAIL
    assert peak.value is not None
    assert peak.value == pytest.approx(0.4, abs=0.05)
    assert _check(result, "mono_compatibility").status is QcStatus.FAIL


def test_noise_is_not_mistaken_for_a_broken_file(qc_settings: AppSettings) -> None:
    """Noise is poor music and valid audio. QC judges the second, not the first.

    §6.1 draws the line here: deciding that a track is unmusical is the director's business
    and a listener's. QC's job is whether the file is broken, and a file that is merely ugly
    must not be rejected by it — otherwise the pipeline quietly becomes a taste filter nobody
    specified.
    """
    result = _qc(noise(), qc_settings)
    assert _check(result, "peak_level").status is not QcStatus.FAIL
    assert _check(result, "integrated_loudness").status is not QcStatus.FAIL
    assert _check(result, "internal_silence").status is not QcStatus.FAIL


# ------------------------------------------------------------------ features


def test_features_are_summaries_not_frame_matrices() -> None:
    """§6.2: do not store gigantic per-frame matrices.

    Enforced by a size assertion rather than by convention, because the natural way to add a
    feature later is to keep the frames "just in case", and nothing else would notice until
    the database was gigabytes.
    """
    features = extract_features(musical(seconds=30.0))
    assert len(features.chroma_mean) == 12
    assert len(features.mfcc_mean) == 20
    assert len(features.rms_profile) <= 128
    total = (
        len(features.chroma_mean)
        + len(features.chroma_std)
        + len(features.mfcc_mean)
        + len(features.mfcc_std)
        + len(features.rms_profile)
    )
    assert total < 256, f"{total} floats stored per track is not a summary"


def test_tempo_is_recovered_from_the_fixture_tempo() -> None:
    features = extract_features(musical(seconds=12.0, bpm=120.0))
    assert features.tempo is not None
    # Half- and double-time are correct answers a beat tracker may legitimately give, so the
    # assertion allows them rather than pretending tempo estimation is unambiguous.
    assert any(
        abs(features.tempo - candidate) < 8.0 for candidate in (60.0, 120.0, 240.0)
    ), f"tempo {features.tempo} is not 120 or an octave of it"


def test_level_is_measured_across_channels_not_on_the_mono_fold() -> None:
    features = extract_features(mono_cancelling())
    assert features.peak == pytest.approx(0.4, abs=0.05)
    assert features.rms > 0.01
    assert features.mono_compatibility_db < -6.0


def test_extraction_is_deterministic() -> None:
    """Same input, same numbers. Everything downstream assumes it."""
    first = extract_features(musical(seed=42))
    second = extract_features(musical(seed=42))
    assert first.chroma_mean == second.chroma_mean
    assert first.mfcc_mean == second.mfcc_mean
    assert first.rms == second.rms


def test_a_low_rate_file_is_not_failed_for_lacking_what_it_cannot_contain(
    qc_settings: AppSettings,
) -> None:
    """The regression that rejected an entire soak run.

    A 16 kHz file's Nyquist limit is exactly 8 kHz, so it has no energy above 8 kHz by
    construction. The top-end check measured anyway and concluded "a decode or render
    fault" — failing 355 of 355 tracks in a 16 kHz soak while reporting a cause that had not
    happened. The check must decline to measure what the sample rate cannot represent.
    """
    source = musical(seconds=6.0, rate=16_000)
    result = _qc(source, qc_settings)
    check = _check(result, "high_frequency_content")
    assert check.status is QcStatus.PASS
    # No value, because nothing was measured. A number here would be a fabricated metric.
    assert check.value is None
    assert "Nyquist" in check.reason
    assert [c.name for c in result.failures] == []


def test_a_full_rate_file_is_still_held_to_the_top_end_check(
    qc_settings: AppSettings,
) -> None:
    """The paired negative: the exemption is about sample rate, not a blanket amnesty.

    At 44.1 kHz the band is representable, so a genuinely collapsed top end must still fail.
    """
    import scipy.signal as signal

    source = musical(seconds=6.0, rate=44_100)
    samples = np.asarray(source.samples)
    # An aggressive low-pass at 4 kHz: content that *could* exist above 8 kHz and does not.
    sos = signal.butter(10, 4_000, btype="low", fs=44_100, output="sos")
    filtered = signal.sosfilt(sos, samples, axis=0).astype(np.float32)
    result = _qc(AudioBuffer(filtered, 44_100), qc_settings)
    check = _check(result, "high_frequency_content")
    assert check.status is not QcStatus.PASS
    assert check.value is not None


def test_bright_loud_music_is_not_called_corrupt(qc_settings: AppSettings) -> None:
    """The regression real ACE-Step output exposed.

    The discontinuity floor was an absolute 0.5 amplitude step. At 48 kHz a sine of
    amplitude A at frequency f steps by ``A·2πf/fs`` between samples, so a loud component at
    5 kHz reaches 0.58 — and hi-hats live well above 5 kHz. A real, clean, mastered UK drill
    track counted 50 "impossible sample steps" and was rejected as *probably corrupt*.

    The fixtures that justified the old floor were all built from low sine partials and
    could never reach it, so nothing caught this until real audio arrived. This fixture is
    deliberately bright and loud: high-frequency content at near-full scale, no faults.
    """
    rate = 48_000
    t = np.arange(int(rate * 4.0)) / rate
    rng = np.random.default_rng(5)
    # A bright mix: dominant 8 kHz and 11 kHz partials over a bass note, near full scale.
    # The high partials are weighted heavily on purpose — the per-sample step of a sine is
    # proportional to its frequency, so it is the top end that produces the large steps the
    # old absolute floor mistook for corruption.
    signal = (
        0.30 * np.sin(2 * np.pi * 110.0 * t)
        + 0.70 * np.sin(2 * np.pi * 8_000.0 * t)
        + 0.35 * np.sin(2 * np.pi * 11_000.0 * t)
        + rng.normal(0.0, 0.01, t.size)
    )
    signal = signal / np.abs(signal).max() * 0.89
    bright = AudioBuffer(np.stack([signal, signal], axis=1).astype(np.float32), rate)

    features = extract_features(bright)
    # The physics this guards: a single step here legitimately exceeds the old 0.5 floor.
    samples = np.asarray(bright.samples, dtype=np.float64).mean(axis=1)
    assert np.abs(np.diff(samples)).max() > 0.5, "fixture is not bright enough to be a test"

    assert features.discontinuity_count == 0, (
        f"{features.discontinuity_count} false discontinuities in clean bright audio"
    )
    result = _qc(bright, qc_settings)
    assert _check(result, "discontinuities").status is QcStatus.PASS


def test_real_clicks_are_still_caught_after_the_fix(qc_settings: AppSettings) -> None:
    """The paired positive. The floor was raised, not removed.

    Without this, the fix above could be achieved by deleting the check — which would let a
    genuinely corrupt render reach air.
    """
    result = _qc(clicking(clicks=64), qc_settings)
    check = _check(result, "discontinuities")
    assert check.status is QcStatus.FAIL
    assert check.value is not None
    assert check.value >= 32
