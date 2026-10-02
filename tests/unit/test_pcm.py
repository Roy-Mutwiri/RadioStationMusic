"""AudioBuffer and audio file I/O (ADR-06, §24, §25).

The representation every later stage depends on, so the tests here are mostly about the
invariants that make the rest of the audio code safe to write: one shape, one dtype, one
scale, and measurement functions that report what they claim to.

The §24 measurement tests use **constructed** signals with known properties rather than
rendered audio. A silence ratio measured on real music is a number nobody can check; a
silence ratio measured on a signal that is half silence by construction is a test.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from tradefix_radio.audio.io import (
    format_for,
    read_audio,
    read_info,
    write_audio,
)
from tradefix_radio.audio.pcm import (
    CLIP_THRESHOLD,
    MIN_DBFS,
    SAMPLE_DTYPE,
    AudioBuffer,
    from_dbfs,
    to_dbfs,
)
from tradefix_radio.core.errors import AudioError

SR = 44_100


def tone(seconds: float = 1.0, hz: float = 440.0, amplitude: float = 0.5,
         channels: int = 2, sample_rate: int = SR) -> AudioBuffer:
    time = np.arange(round(seconds * sample_rate), dtype=np.float64) / sample_rate
    mono = (np.sin(2 * math.pi * hz * time) * amplitude).astype(SAMPLE_DTYPE)
    return AudioBuffer.from_mono(mono, sample_rate, channels=channels)


# ---------------------------------------------------------------- dBFS


def test_dbfs_round_trips() -> None:
    for dbfs in (-60.0, -20.0, -6.0, -1.0, 0.0):
        assert to_dbfs(from_dbfs(dbfs)) == pytest.approx(dbfs, abs=1e-9)


def test_full_scale_is_zero_dbfs() -> None:
    assert to_dbfs(1.0) == pytest.approx(0.0)
    assert from_dbfs(0.0) == pytest.approx(1.0)


def test_silence_returns_a_floor_not_negative_infinity() -> None:
    """-inf is mathematically right and ruinous in practice.

    It propagates through every average, comparison and JSON serialisation downstream, so a
    single silent track would turn a §101 report's mean loudness into ``-Infinity``.
    """
    assert to_dbfs(0.0) == MIN_DBFS
    assert math.isfinite(to_dbfs(0.0))
    assert to_dbfs(-0.5) == MIN_DBFS


# ---------------------------------------------------------------- shape invariants


def test_mono_input_is_normalised_to_two_dimensions() -> None:
    """A 1-D array is the commonest source of broadcasting bugs in audio code."""
    buffer = AudioBuffer(np.zeros(1000, dtype=SAMPLE_DTYPE), SR)
    assert buffer.samples.ndim == 2
    assert buffer.channels == 1
    assert buffer.frames == 1000


def test_integer_input_is_converted_to_float32() -> None:
    buffer = AudioBuffer(np.zeros((100, 2), dtype=np.int16), SR)
    assert buffer.samples.dtype == SAMPLE_DTYPE


def test_three_dimensional_input_is_rejected() -> None:
    with pytest.raises(ValueError, match="1-D .* or 2-D"):
        AudioBuffer(np.zeros((10, 2, 2), dtype=SAMPLE_DTYPE), SR)


def test_a_channels_major_array_is_rejected_with_a_useful_message() -> None:
    """``(channels, frames)`` is the single likeliest mistake, so it names itself."""
    with pytest.raises(ValueError, match="frames-major"):
        AudioBuffer(np.zeros((2, 44_100), dtype=SAMPLE_DTYPE), SR)


def test_a_zero_sample_rate_is_rejected() -> None:
    with pytest.raises(ValueError, match="sample_rate must be positive"):
        AudioBuffer(np.zeros((10, 2), dtype=SAMPLE_DTYPE), 0)


def test_a_computed_buffer_is_frozen_against_accidental_writes() -> None:
    """An in-place write to audio something else is playing must fail, not corrupt it.

    The real risk is a slice handed to the sink while the mixer still holds the parent.
    """
    buffer = AudioBuffer.silence(seconds=0.1, sample_rate=SR)
    with pytest.raises(ValueError, match="read-only|assignment destination"):
        buffer.samples[0, 0] = 1.0


def test_every_derived_buffer_is_frozen() -> None:
    """One frozen constructor is not enough; every operation has to use it."""
    source = AudioBuffer.silence(seconds=0.2, sample_rate=SR)
    derived = [
        source.scaled(0.5),
        source.clipped(),
        source.padded_to(source.frames * 2),
        source.concat(source),
        source.with_channels(1),
        AudioBuffer.from_mono(np.zeros(100, dtype=SAMPLE_DTYPE), SR),
    ]
    for buffer in derived:
        assert not buffer.samples.flags.writeable, buffer.samples.shape


def test_a_callers_array_is_left_writeable() -> None:
    """Constructing a buffer must not reach back and freeze the caller's own array.

    A real defect, found by this test. Ownership was inferred from
    ``owndata and base is None``, which a freshly allocated ``np.zeros`` also satisfies — so
    merely wrapping a caller's array made it unwritable somewhere else entirely. NumPy cannot
    distinguish the two cases, so ownership is now stated by the caller instead of guessed.
    """
    caller_owned = np.zeros((100, 2), dtype=SAMPLE_DTYPE)
    AudioBuffer(caller_owned, SR)
    caller_owned[0, 0] = 1.0  # must not raise
    assert caller_owned[0, 0] == 1.0


def test_duration_is_derived_from_frames_and_rate() -> None:
    buffer = AudioBuffer.silence(seconds=2.5, sample_rate=48_000)
    assert buffer.sample_rate == 48_000
    assert buffer.duration_seconds == pytest.approx(2.5)
    assert len(buffer) == buffer.frames


def test_an_empty_buffer_is_valid_and_measures_as_zero() -> None:
    """Zero-length audio turns up at every edge: a zero-second slice, an empty concat."""
    empty = AudioBuffer.silence(seconds=0.0, sample_rate=SR)
    assert empty.is_empty
    assert empty.duration_seconds == 0.0
    assert empty.peak() == 0.0
    assert empty.rms() == 0.0
    assert empty.dc_offset() == 0.0
    assert empty.clipped_sample_ratio() == 0.0
    assert empty.silence_ratio() == 0.0


# ---------------------------------------------------------------- slicing


def test_slicing_by_seconds_matches_slicing_by_frames() -> None:
    buffer = tone(2.0)
    assert np.array_equal(
        buffer.slice_seconds(0.5, 1.5).samples,
        buffer.slice_frames(round(0.5 * SR), round(1.5 * SR)).samples,
    )


def test_slices_are_clamped_rather_than_raising() -> None:
    """Callers do bounds arithmetic in a dozen places; doing it once here is safer."""
    buffer = tone(1.0)
    assert buffer.slice_frames(-500, 10_000_000).frames == buffer.frames
    assert buffer.slice_frames(5000, 1000).is_empty


def test_padding_extends_with_silence_and_truncates_when_shorter() -> None:
    buffer = tone(1.0)
    longer = buffer.padded_to(buffer.frames + SR)
    assert longer.frames == buffer.frames + SR
    assert longer.slice_frames(buffer.frames).peak() == 0.0
    assert buffer.padded_to(100).frames == 100


def test_concat_joins_exactly() -> None:
    first, second = tone(1.0, hz=220.0), tone(0.5, hz=440.0)
    joined = first.concat(second)
    assert joined.frames == first.frames + second.frames


def test_concat_rejects_a_sample_rate_mismatch() -> None:
    """Reinterpreting one rate as another changes pitch and duration silently."""
    with pytest.raises(ValueError, match="sample rate mismatch"):
        tone(1.0, sample_rate=44_100).concat(tone(1.0, sample_rate=48_000))


def test_concat_rejects_a_channel_mismatch() -> None:
    with pytest.raises(ValueError, match="channel mismatch"):
        tone(1.0, channels=2).concat(tone(1.0, channels=1))


# ---------------------------------------------------------------- channels


def test_mono_to_stereo_duplicates() -> None:
    mono = tone(0.5, channels=1)
    stereo = mono.with_channels(2)
    assert stereo.channels == 2
    assert np.array_equal(stereo.samples[:, 0], stereo.samples[:, 1])


def test_stereo_to_mono_averages_rather_than_sums() -> None:
    """Summing two correlated channels doubles the amplitude and clips valid material."""
    stereo = AudioBuffer(np.full((100, 2), 0.6, dtype=SAMPLE_DTYPE), SR)
    assert stereo.with_channels(1).peak() == pytest.approx(0.6)


def test_requesting_the_current_channel_count_is_a_no_op() -> None:
    stereo = tone(0.1)
    assert stereo.with_channels(2) is stereo


def test_an_unsupported_channel_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported channel count"):
        tone(0.1).with_channels(5)


# ---------------------------------------------------------------- gain


def test_scaling_is_linear_in_amplitude() -> None:
    buffer = tone(0.2, amplitude=0.4)
    assert buffer.scaled(0.5).peak() == pytest.approx(0.2, abs=1e-6)


def test_normalising_hits_the_requested_peak() -> None:
    for target in (-12.0, -6.0, -1.0, 0.0):
        result = tone(0.2, amplitude=0.1).normalised_to_peak(target)
        assert result.peak_dbfs() == pytest.approx(target, abs=0.01)


def test_normalising_silence_leaves_it_silent() -> None:
    """Dividing by a zero peak would produce NaN, which then poisons every later stage."""
    silence = AudioBuffer.silence(seconds=0.5, sample_rate=SR)
    result = silence.normalised_to_peak(-3.0)
    assert result.peak() == 0.0
    assert not np.isnan(result.samples).any()


def test_clipping_bounds_the_range_without_touching_what_is_inside() -> None:
    buffer = AudioBuffer(
        np.array([[-2.0, -0.5], [0.5, 2.0]], dtype=SAMPLE_DTYPE), SR
    )
    clipped = buffer.clipped()
    assert clipped.peak() == pytest.approx(1.0)
    assert clipped.samples[0, 1] == pytest.approx(-0.5)
    assert clipped.samples[1, 0] == pytest.approx(0.5)


# ---------------------------------------------------------------- §24 measurement


def test_peak_and_rms_of_a_known_sine() -> None:
    """A sine's RMS is its amplitude over root two. If that is wrong, nothing else is right."""
    buffer = tone(1.0, amplitude=0.5)
    assert buffer.peak() == pytest.approx(0.5, abs=1e-3)
    assert buffer.rms() == pytest.approx(0.5 / math.sqrt(2), abs=1e-3)


def test_dc_offset_is_measured_per_channel() -> None:
    """Equal and opposite offsets average to zero overall and are still both wrong."""
    samples = np.zeros((1000, 2), dtype=SAMPLE_DTYPE)
    samples[:, 0] = 0.3
    samples[:, 1] = -0.3
    assert AudioBuffer(samples, SR).dc_offset() == pytest.approx(0.3, abs=1e-6)


def test_dc_offset_of_a_centred_signal_is_near_zero() -> None:
    assert tone(1.0).dc_offset() < 0.001


def test_clipped_ratio_counts_samples_at_the_threshold() -> None:
    samples = np.zeros((1000, 1), dtype=SAMPLE_DTYPE)
    samples[:100] = 1.0
    assert AudioBuffer(samples, SR).clipped_sample_ratio() == pytest.approx(0.1)


def test_the_clip_threshold_catches_a_flat_top_below_full_scale() -> None:
    """A converter that clamps *just* under 1.0 still produces the audible flat top."""
    samples = np.full((100, 1), CLIP_THRESHOLD, dtype=SAMPLE_DTYPE)
    assert AudioBuffer(samples, SR).clipped_sample_ratio() == pytest.approx(1.0)


def test_a_clean_signal_reports_no_clipping() -> None:
    assert tone(1.0, amplitude=0.9).clipped_sample_ratio() == 0.0


def test_silence_ratio_is_measured_over_windows_not_samples() -> None:
    """The defect a per-sample test has: every waveform crosses zero twice a cycle.

    A per-sample silence test reports a loud 100 Hz tone as a few per cent silent, and the
    figure then varies with frequency rather than with silence — so §24's threshold would be
    rejecting low-frequency content rather than quiet tracks.
    """
    loud = tone(1.0, hz=100.0, amplitude=0.8)
    assert loud.silence_ratio() == 0.0


def test_silence_ratio_of_a_half_silent_signal_is_a_half() -> None:
    buffer = tone(1.0).concat(AudioBuffer.silence(seconds=1.0, sample_rate=SR))
    assert buffer.silence_ratio() == pytest.approx(0.5, abs=0.02)


def test_silence_ratio_of_pure_silence_is_one() -> None:
    assert AudioBuffer.silence(seconds=1.0, sample_rate=SR).silence_ratio() == 1.0


def test_edge_silence_is_measured_at_each_end() -> None:
    """§25 trims leading and trailing silence; it has to know how much there is."""
    buffer = (
        AudioBuffer.silence(seconds=0.5, sample_rate=SR)
        .concat(tone(1.0))
        .concat(AudioBuffer.silence(seconds=0.25, sample_rate=SR))
    )
    assert buffer.leading_silence_seconds() == pytest.approx(0.5, abs=0.01)
    assert buffer.trailing_silence_seconds() == pytest.approx(0.25, abs=0.01)


def test_fully_silent_audio_reports_its_whole_length_as_edge_silence() -> None:
    silence = AudioBuffer.silence(seconds=2.0, sample_rate=SR)
    assert silence.leading_silence_seconds() == pytest.approx(2.0)
    assert silence.trailing_silence_seconds() == pytest.approx(2.0)


# ---------------------------------------------------------------- file I/O


def test_format_is_chosen_from_the_extension() -> None:
    assert format_for(Path("a.flac")) == "FLAC"
    assert format_for(Path("a.WAV")) == "WAV"


def test_an_unknown_extension_names_what_is_supported(tmp_path: Path) -> None:
    with pytest.raises(AudioError, match="unsupported audio extension"):
        format_for(tmp_path / "track.mp3")


@pytest.mark.parametrize("suffix", [".wav", ".flac"])
def test_audio_round_trips_through_a_file(tmp_path: Path, suffix: str) -> None:
    original = tone(1.0, amplitude=0.5)
    path = write_audio(tmp_path / f"track{suffix}", original)
    restored = read_audio(path)
    assert restored.sample_rate == original.sample_rate
    assert restored.channels == original.channels
    assert restored.frames == original.frames
    # FLAC is 24-bit integer, so exact equality is not available — but 24 bits is ~140 dB
    # of resolution, and anything worse than this would be a format bug.
    assert np.max(np.abs(restored.samples - original.samples)) < 1e-4


def test_writing_creates_missing_parent_directories(tmp_path: Path) -> None:
    path = write_audio(tmp_path / "a" / "b" / "c" / "track.wav", tone(0.1))
    assert path.is_file()


def test_info_reads_metadata_without_decoding(tmp_path: Path) -> None:
    path = write_audio(tmp_path / "track.flac", tone(2.0))
    info = read_info(path)
    assert info.duration_seconds == pytest.approx(2.0, abs=0.001)
    assert info.channels == 2
    assert info.sample_rate == SR
    assert info.format == "FLAC"


def test_an_atomic_write_leaves_no_partial_file(tmp_path: Path) -> None:
    """A truncated file that *exists* is worse than none: the queue believes it is ready."""
    write_audio(tmp_path / "track.flac", tone(0.5))
    assert not list(tmp_path.glob(".*partial"))
    assert (tmp_path / "track.flac").is_file()


def test_writing_clips_rather_than_wrapping(tmp_path: Path) -> None:
    """An integer subtype *wraps* on overflow, turning a loud peak into inverted noise."""
    hot = AudioBuffer(np.full((1000, 2), 1.4, dtype=SAMPLE_DTYPE), SR)
    restored = read_audio(write_audio(tmp_path / "hot.flac", hot))
    assert restored.peak() <= 1.0
    # Wrapping would put samples of the opposite sign in there.
    assert float(np.min(restored.samples)) > 0.0


def test_reading_a_missing_file_names_the_path(tmp_path: Path) -> None:
    """libsndfile's own error carries its message and not the path."""
    missing = tmp_path / "nope.flac"
    with pytest.raises(AudioError, match=str(missing.name)):
        read_audio(missing)


def test_reading_a_non_audio_file_names_the_path(tmp_path: Path) -> None:
    path = tmp_path / "fake.wav"
    path.write_bytes(b"this is not audio")
    with pytest.raises(AudioError, match="fake.wav"):
        read_audio(path)


def test_an_impossible_subtype_is_rejected_before_writing(tmp_path: Path) -> None:
    """FLAC cannot store float samples. Finding that out at write time is the point."""
    with pytest.raises(AudioError, match="does not support subtype"):
        write_audio(tmp_path / "track.flac", tone(0.1), subtype="FLOAT")
