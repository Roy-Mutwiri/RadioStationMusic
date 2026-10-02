"""Audio sinks (ADR-06, milestone 4.6).

Milestone 4.6's second half: "``NullSink`` run advances a virtual clock deterministically."
That is the property §64's accelerated endurance runs rest on, so it is tested directly rather
than inferred from a soak run finishing.

The shared-behaviour tests run against every sink through one parametrised fixture, because
the bugs that matter here are the ones a sink gets wrong *individually* — a missing frame
count, a non-idempotent close, a silently accepted sample-rate mismatch.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from tradefix_radio.audio.io import read_audio, read_info
from tradefix_radio.audio.pcm import SAMPLE_DTYPE, AudioBuffer
from tradefix_radio.audio.sinks import (
    AudioSink,
    BaseSink,
    FFmpegPipeSink,
    NullSink,
    SoundDeviceSink,
    WavFileSink,
)
from tradefix_radio.core.clock import VirtualClock
from tradefix_radio.core.errors import AudioSinkError

SR = 44_100
BLOCK_SECONDS = 0.25


def block(seconds: float = BLOCK_SECONDS, *, amplitude: float = 0.5,
          channels: int = 2, sample_rate: int = SR) -> AudioBuffer:
    frames = round(seconds * sample_rate)
    time = np.arange(frames, dtype=np.float64) / sample_rate
    mono = (np.sin(2 * np.pi * 220.0 * time) * amplitude).astype(SAMPLE_DTYPE)
    return AudioBuffer.from_mono(mono, sample_rate, channels=channels)


SinkFactory = Callable[[Path], BaseSink]

#: Every sink that can run without external hardware.
_FACTORIES: dict[str, SinkFactory] = {
    "null": lambda _path: NullSink(realtime=False),
    "wav": lambda path: WavFileSink(path / "out.wav"),
}


@pytest.fixture(params=sorted(_FACTORIES))
def sink(request: pytest.FixtureRequest, tmp_path: Path) -> BaseSink:
    return _FACTORIES[request.param](tmp_path)


# ---------------------------------------------------------------- shared behaviour


def test_every_sink_satisfies_the_protocol(sink: BaseSink) -> None:
    assert isinstance(sink, AudioSink)
    assert sink.name


async def test_writing_before_opening_is_an_error(sink: BaseSink) -> None:
    """Silently accepting audio into a closed sink would look like a working station."""
    with pytest.raises(AudioSinkError, match="not open"):
        await sink.write(block())


async def test_frames_written_tracks_the_play_clock(sink: BaseSink) -> None:
    """The station's own notion of elapsed broadcast, used by §41 and §75."""
    await sink.open()
    for _ in range(4):
        await sink.write(block())
    await sink.close()
    assert sink.frames_written == 4 * round(BLOCK_SECONDS * SR)
    assert sink.seconds_written == pytest.approx(4 * BLOCK_SECONDS, abs=1e-6)


async def test_open_and_close_are_idempotent(sink: BaseSink) -> None:
    """§74's graceful shutdown and §75's recovery can both reach close twice."""
    await sink.open()
    await sink.open()
    assert sink.is_open
    await sink.write(block())
    await sink.close()
    await sink.close()
    assert not sink.is_open


async def test_an_empty_block_is_accepted_and_changes_nothing(sink: BaseSink) -> None:
    await sink.open()
    await sink.write(AudioBuffer.silence(seconds=0.0, sample_rate=SR))
    assert sink.frames_written == 0
    await sink.close()


async def test_a_sample_rate_mismatch_is_refused(sink: BaseSink) -> None:
    """Reinterpreting one rate as another changes pitch and makes the play clock wrong."""
    await sink.open()
    with pytest.raises(AudioSinkError, match="expects 44100 Hz"):
        await sink.write(block(sample_rate=48_000))
    await sink.close()


async def test_a_mono_block_is_up_mixed_rather_than_refused(sink: BaseSink) -> None:
    """Station IDs and Tier 3 audio are naturally mono; refusing them would be useless."""
    await sink.open()
    await sink.write(block(channels=1))
    assert sink.frames_written == round(BLOCK_SECONDS * SR)
    await sink.close()


# ---------------------------------------------------------------- construction


@pytest.mark.parametrize("rate", [0, -1])
def test_an_invalid_sample_rate_is_rejected(rate: int) -> None:
    with pytest.raises(ValueError, match="sample_rate must be positive"):
        NullSink(sample_rate=rate)


@pytest.mark.parametrize("channels", [0, 3, 8])
def test_an_unsupported_channel_count_is_rejected(channels: int) -> None:
    with pytest.raises(ValueError, match="channels must be 1 or 2"):
        NullSink(channels=channels)


# ---------------------------------------------------------------- NullSink


async def test_the_null_sink_advances_a_virtual_clock_by_the_audio_duration() -> None:
    """Milestone 4.6's exit test, and the foundation of §64's accelerated runs.

    The sink paces playback, not the engine. Under a virtual clock that pacing costs no wall
    time, which is the whole reason a seven-day endurance run is possible — and the play clock
    stays exact because it is counted in frames rather than measured in seconds.
    """
    clock = VirtualClock()
    sink = NullSink(clock=clock, realtime=True)
    await sink.open()

    audio = block(2.0)
    writing = asyncio.create_task(sink.write(audio))
    await asyncio.sleep(0)
    assert not writing.done(), "a realtime sink must not accept a block instantly"

    await clock.advance(2.0)
    await writing
    assert clock.monotonic() == pytest.approx(2.0)
    assert sink.seconds_written == pytest.approx(2.0)
    await sink.close()


async def test_the_null_sink_paces_a_long_run_exactly() -> None:
    """Ten blocks must advance the clock by exactly ten blocks — no drift, no rounding."""
    clock = VirtualClock()
    sink = NullSink(clock=clock, realtime=True)
    await sink.open()

    async def feed() -> None:
        for _ in range(10):
            await sink.write(block(0.5))

    task = asyncio.create_task(feed())
    await asyncio.sleep(0)
    await clock.run_for(5.0)
    await task
    assert clock.monotonic() == pytest.approx(5.0)
    assert sink.seconds_written == pytest.approx(5.0)
    await sink.close()


async def test_the_null_sink_can_skip_pacing_entirely() -> None:
    """For unit tests that only care about what was written, not when."""
    clock = VirtualClock()
    sink = NullSink(clock=clock, realtime=False)
    await sink.open()
    for _ in range(100):
        await sink.write(block())
    assert clock.monotonic() == 0.0
    assert sink.seconds_written == pytest.approx(100 * BLOCK_SECONDS)
    await sink.close()


async def test_the_null_sink_reports_the_loudest_sample_it_saw() -> None:
    """So a run that broadcast silence for two hours fails rather than passing quietly."""
    sink = NullSink(realtime=False)
    await sink.open()
    await sink.write(block(amplitude=0.4))
    await sink.write(block(amplitude=0.9))
    assert sink.peak == pytest.approx(0.9, abs=0.01)
    await sink.close()


async def test_a_silent_run_is_visible_in_the_null_sinks_peak() -> None:
    sink = NullSink(realtime=False)
    await sink.open()
    await sink.write(AudioBuffer.silence(seconds=1.0, sample_rate=SR))
    assert sink.peak == 0.0
    await sink.close()


# ---------------------------------------------------------------- WavFileSink


async def test_the_wav_sink_writes_what_it_was_given(tmp_path: Path) -> None:
    sink = WavFileSink(tmp_path / "out.wav", subtype="FLOAT")
    await sink.open()
    audio = block(1.0, amplitude=0.5)
    await sink.write(audio)
    await sink.close()

    restored = read_audio(tmp_path / "out.wav")
    assert restored.frames == audio.frames
    assert np.max(np.abs(restored.samples - audio.samples)) < 1e-6


async def test_the_wav_sink_appends_block_by_block(tmp_path: Path) -> None:
    """A 2-hour run is ~1.3 GB; accumulating it in memory and saving at the end is not an option."""
    sink = WavFileSink(tmp_path / "out.wav")
    await sink.open()
    for _ in range(8):
        await sink.write(block(0.5))
    await sink.close()
    assert read_info(tmp_path / "out.wav").duration_seconds == pytest.approx(4.0, abs=0.01)


async def test_the_wav_sink_creates_missing_directories(tmp_path: Path) -> None:
    sink = WavFileSink(tmp_path / "deep" / "nested" / "out.wav")
    await sink.open()
    await sink.write(block())
    await sink.close()
    assert (tmp_path / "deep" / "nested" / "out.wav").is_file()


async def test_the_wav_sink_reports_an_unwritable_path(tmp_path: Path) -> None:
    """A directory where a file should be. The message has to name the path."""
    target = tmp_path / "out.wav"
    target.mkdir()
    sink = WavFileSink(target)
    with pytest.raises(AudioSinkError, match="out.wav"):
        await sink.open()


async def test_the_wav_sink_can_pace_in_real_time(tmp_path: Path) -> None:
    clock = VirtualClock()
    sink = WavFileSink(tmp_path / "out.wav", clock=clock, realtime=True)
    await sink.open()
    writing = asyncio.create_task(sink.write(block(1.0)))
    await asyncio.sleep(0)
    await clock.advance(1.0)
    await writing
    assert clock.monotonic() == pytest.approx(1.0)
    await sink.close()


# ---------------------------------------------------------------- SoundDeviceSink


async def test_the_device_sink_explains_how_to_fix_a_missing_dependency() -> None:
    """§72's simulation mode is supposed to run on a machine with no sound card at all.

    Whether ``sounddevice`` is installed or whether a device exists, the failure must name the
    remedy — the alternative is a PortAudio error that means nothing to an operator.
    """
    sink = SoundDeviceSink(device_name="__no_such_device__")
    with pytest.raises(AudioSinkError) as caught:
        await sink.open()
    message = str(caught.value)
    assert "null_sink" in message or "cannot open audio device" in message


def test_the_device_sink_is_constructible_without_hardware() -> None:
    """Construction must not touch PortAudio, or importing the package would need a device."""
    sink = SoundDeviceSink(device_name="CABLE Input", block_frames=512, buffer_blocks=4)
    assert sink.name == "sounddevice"
    assert not sink.is_open


# ---------------------------------------------------------------- FFmpegPipeSink

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


def test_the_ffmpeg_sink_is_constructible_without_ffmpeg() -> None:
    sink = FFmpegPipeSink(["-f", "null", "-"])
    assert sink.name == "ffmpeg_pipe"
    assert sink.returncode is None


async def test_a_missing_ffmpeg_executable_is_reported_clearly() -> None:
    sink = FFmpegPipeSink(["-f", "null", "-"], executable="__no_such_ffmpeg__")
    with pytest.raises(AudioSinkError, match="cannot start __no_such_ffmpeg__"):
        await sink.open()


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg is not on PATH")
async def test_the_ffmpeg_sink_streams_to_a_file(tmp_path: Path) -> None:
    """The §88 extension point, exercised rather than merely declared."""
    output = tmp_path / "encoded.wav"
    sink = FFmpegPipeSink(["-y", str(output)])
    await sink.open()
    for _ in range(4):
        await sink.write(block(0.5))
    await sink.close()
    assert output.is_file()
    assert read_info(output).duration_seconds == pytest.approx(2.0, abs=0.1)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg is not on PATH")
async def test_the_ffmpeg_sink_discards_to_null(tmp_path: Path) -> None:
    sink = FFmpegPipeSink(["-f", "null", "-"])
    await sink.open()
    await sink.write(block(1.0))
    await sink.close()
    assert sink.frames_written == round(1.0 * SR)
