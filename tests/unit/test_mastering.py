"""Mastering and the canonical master format (§6.9–§6.11, §6.19).

The thesis under test is §6.9's: **consistent broadcast loudness, not maximum loudness**. Most
of these assertions exist to stop the easy wrong implementation — peak-normalise everything to
full scale — from passing, because it would produce a station where a quiet ambient piece and
a breakout trap track hit the listener identically hard, destroying the dynamic §1 asks the
director to create.
"""

from __future__ import annotations

import pytest

from tests.audio_fixtures import dc_offset, musical, quiet_ambient, silence
from tests.conftest import make_blueprint
from tradefix_radio.audio.analysis import extract_features, librosa_available
from tradefix_radio.audio.io import read_audio, read_info, write_audio
from tradefix_radio.audio.mastering import (
    ENERGY_LOUDNESS_SPREAD_LU,
    LOUDNESS_TOLERANCE_LU,
    MASTER_CHANNELS,
    MASTER_SAMPLE_RATE,
    MasteringOutcome,
    ffmpeg_available,
    master_track,
    target_loudness_for,
)
from tradefix_radio.audio.pcm import AudioBuffer
from tradefix_radio.config.schema import AppSettings

needs_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available(), reason="FFmpeg is required for loudness normalisation"
)


def _write(tmp_path, buffer: AudioBuffer, name: str = "source.wav"):
    path = tmp_path / name
    write_audio(path, buffer)
    return path


# ------------------------------------------------------- the loudness band


def test_the_target_slides_with_blueprint_energy(settings: AppSettings) -> None:
    """§6.9 permits energy-based bands if documented. This is the documented behaviour."""
    quiet = target_loudness_for(settings.mastering, make_blueprint("TF-Q", composition_energy=0.0))
    loud = target_loudness_for(settings.mastering, make_blueprint("TF-L", composition_energy=1.0))
    assert loud > quiet
    assert loud - quiet == pytest.approx(2 * ENERGY_LOUDNESS_SPREAD_LU, abs=0.01)


def test_the_band_is_narrow_enough_to_stay_a_band(settings: AppSettings) -> None:
    """A band, not a free-for-all.

    Without a ceiling on the spread, "energy-based targets" becomes "no normalisation", and a
    listener reaches for the volume control between every track — which is the thing loudness
    normalisation exists to prevent.
    """
    assert ENERGY_LOUDNESS_SPREAD_LU <= 3.0


def test_no_blueprint_means_the_configured_target_unchanged(settings: AppSettings) -> None:
    """A station identity has no intended energy, and inventing one would be worse."""
    assert target_loudness_for(settings.mastering, None) == pytest.approx(
        settings.mastering.target_lufs
    )


# --------------------------------------------------------------- the output


@needs_ffmpeg
def test_a_master_is_written_in_the_canonical_format(tmp_path, settings: AppSettings) -> None:
    """§6.11: 48 kHz stereo 24-bit, matching playout so nothing is resampled on air."""
    source = _write(tmp_path, musical(seconds=5.0))
    result = master_track(
        source, tmp_path / "master.wav", settings=settings.mastering,
        blueprint=make_blueprint("TF-M"),
    )
    assert result.outcome is MasteringOutcome.MASTERED
    assert result.output_path is not None
    info = read_info(result.output_path)
    assert info.sample_rate == MASTER_SAMPLE_RATE
    assert info.channels == MASTER_CHANNELS
    assert "24" in info.subtype or info.subtype.upper().endswith("24")


@needs_ffmpeg
def test_mastering_hits_its_target_or_explains_why_not(
    tmp_path, settings: AppSettings
) -> None:
    """Either within tolerance, or peak-constrained with the evidence to prove it.

    Written as a disjunction on purpose rather than as a flat tolerance assertion, because a
    flat assertion is not true of all material and papering over that would mean either a
    flaky test or a tolerance widened until it meant nothing. High-crest audio genuinely
    cannot reach a loud target under a true-peak ceiling; what *is* always required is that
    the undershoot be explained by a measured ceiling hit rather than left unaccounted.
    """
    source = _write(tmp_path, musical(seconds=6.0))
    blueprint = make_blueprint("TF-T", composition_energy=0.7)
    result = master_track(
        source, tmp_path / "m.wav", settings=settings.mastering, blueprint=blueprint
    )
    assert result.outcome is MasteringOutcome.MASTERED
    assert result.measured_lufs_after is not None
    drift = result.measured_lufs_after - result.target_lufs
    if abs(drift) > LOUDNESS_TOLERANCE_LU:
        assert result.peak_constrained, f"unexplained {drift:+.1f} LU drift"
        assert result.true_peak_dbtp is not None
        assert result.true_peak_ceiling_dbtp is not None
        assert result.true_peak_dbtp >= result.true_peak_ceiling_dbtp - 0.5


@needs_ffmpeg
def test_a_master_never_exceeds_the_true_peak_ceiling(
    tmp_path, settings: AppSettings
) -> None:
    """The one limit that is not negotiable: a breach is audible distortion downstream."""
    source = _write(tmp_path, musical(seconds=5.0, amplitude=0.9))
    result = master_track(
        source, tmp_path / "m.wav", settings=settings.mastering,
        blueprint=make_blueprint("TF-P", composition_energy=0.95),
    )
    assert result.true_peak_dbtp is not None
    # 0.1 dB of slack for ebur128's own rounding, which reports to one decimal.
    assert result.true_peak_dbtp <= settings.mastering.true_peak_ceiling_dbtp + 0.1


@needs_ffmpeg
def test_quiet_material_stays_quieter_than_loud_material(
    tmp_path, settings: AppSettings
) -> None:
    """The §6.9 thesis, as an end-to-end assertion.

    This is the test a peak-normalising implementation fails. Two tracks that differ in
    intended energy must still differ after mastering — not by their original gulf, but
    audibly. A single fixed target, or peak normalisation, would flatten them together.
    """
    quiet_source = _write(tmp_path, quiet_ambient(seconds=6.0), "quiet.wav")
    loud_source = _write(tmp_path, musical(seconds=6.0, amplitude=0.7), "loud.wav")

    quiet = master_track(
        quiet_source, tmp_path / "quiet-m.wav", settings=settings.mastering,
        blueprint=make_blueprint("TF-Q", genre="lofi", energy=10.0, composition_energy=0.1, bpm=70),
    )
    loud = master_track(
        loud_source, tmp_path / "loud-m.wav", settings=settings.mastering,
        blueprint=make_blueprint("TF-L", genre="uk_drill", energy=95.0, composition_energy=0.95, bpm=142),
    )
    assert quiet.target_lufs < loud.target_lufs
    assert quiet.measured_lufs_after is not None
    assert loud.measured_lufs_after is not None
    assert quiet.measured_lufs_after < loud.measured_lufs_after


@needs_ffmpeg
def test_dc_offset_is_removed_before_normalising(tmp_path, settings: AppSettings) -> None:
    source = _write(tmp_path, dc_offset(seconds=4.0, offset=0.2))
    result = master_track(source, tmp_path / "m.wav", settings=settings.mastering)
    assert result.dc_removed == pytest.approx(0.2, abs=0.02)
    assert result.output_path is not None
    assert abs(read_audio(result.output_path).dc_offset()) < 0.01


@needs_ffmpeg
def test_silence_fails_mastering_rather_than_producing_a_file(
    tmp_path, settings: AppSettings
) -> None:
    """Loudness cannot be measured, so there is nothing to normalise to.

    Failing loudly beats writing a silent "master": the latter would reach final QC looking
    like a real file and consume a queue slot before anything noticed.
    """
    source = _write(tmp_path, silence(seconds=4.0))
    result = master_track(source, tmp_path / "m.wav", settings=settings.mastering)
    assert result.outcome is MasteringOutcome.FAILED
    assert result.output_path is None
    assert result.detail


@needs_ffmpeg
@pytest.mark.skipif(
    not librosa_available(), reason="librosa is required for feature extraction"
)
def test_the_applied_gain_is_what_happened_not_what_was_asked(
    tmp_path, settings: AppSettings
) -> None:
    """Recorded evidence must agree with the file it describes.

    ``gain_applied_db`` was originally the *requested* gain. With the limiter engaged the two
    differ, and storing the request as though it were the result makes the mastering record
    disagree with the audio it documents.
    """
    source = _write(tmp_path, musical(seconds=5.0))
    result = master_track(
        source, tmp_path / "m.wav", settings=settings.mastering,
        blueprint=make_blueprint("TF-G", composition_energy=0.9),
    )
    assert result.gain_applied_db is not None
    assert result.measured_lufs_before is not None
    assert result.measured_lufs_after is not None
    assert result.gain_applied_db == pytest.approx(
        result.measured_lufs_after - result.measured_lufs_before, abs=0.05
    )
    measured = extract_features(read_audio(result.output_path)).integrated_lufs
    assert measured is not None
    assert measured == pytest.approx(result.measured_lufs_after, abs=0.6)


def test_mastering_can_be_disabled_without_pretending_it_ran(
    tmp_path, settings: AppSettings
) -> None:
    source = _write(tmp_path, musical(seconds=2.0))
    disabled = settings.mastering.model_copy(update={"enabled": False})
    result = master_track(source, tmp_path / "m.wav", settings=disabled)
    assert result.outcome is MasteringOutcome.SKIPPED
    assert result.output_path == source
    assert "disabled" in result.detail
