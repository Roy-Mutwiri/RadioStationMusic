"""Audio output sinks (ADR-06).

The playout engine produces blocks of PCM and hands them to an :class:`AudioSink`. That seam
is the reason the same engine can drive a sound card, a file, an ffmpeg pipe, or nothing —
and the reason §64's accelerated endurance runs are possible at all.

``NullSink`` is not a test stub. §72's simulation mode and §64's seven-day run both need a
station that broadcasts to nowhere, and §64 needs it *faster than real time*. Both fall out of
the sink deciding how long a block takes: a real device takes exactly as long as the audio
lasts, and a :class:`~tradefix_radio.core.clock.VirtualClock` takes none.

**Sinks pace playback; the engine does not.** This is the central design point. A sink's
``write`` returns when it is ready for more audio, which for a device means "when there is
room in the ring buffer" and for ``NullSink`` means "when the virtual clock has advanced by
one block". An engine that slept for the block duration itself would drift against the
device's real clock — the classic cause of a slow underrun over hours — and would make
accelerated runs impossible without a second code path.

Every sink converts to its own output format at the boundary and **clips there**, never
earlier (see :mod:`tradefix_radio.audio.pcm`).
"""

from __future__ import annotations

import asyncio
import contextlib
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np
import soundfile as sf

from tradefix_radio.audio.pcm import AudioBuffer
from tradefix_radio.core.clock import Clock, SystemClock
from tradefix_radio.core.errors import AudioSinkError


@runtime_checkable
class AudioSink(Protocol):
    """Where the station's audio goes."""

    @property
    def sample_rate(self) -> int: ...

    @property
    def channels(self) -> int: ...

    @property
    def name(self) -> str: ...

    @property
    def frames_written(self) -> int:
        """Total frames accepted since opening. The station's own play clock."""
        ...

    async def open(self) -> None:
        """Acquire the device or file. Idempotent."""
        ...

    async def write(self, buffer: AudioBuffer) -> None:
        """Accept one block, returning when ready for more.

        Raises :class:`~tradefix_radio.core.errors.AudioSinkError` if output has failed in a
        way the engine must handle (§57 alerts on silence).
        """
        ...

    async def close(self) -> None:
        """Release the device or finalise the file. Idempotent."""
        ...


class BaseSink(ABC):
    """Shared bookkeeping: format checking, frame counting, open/close idempotency."""

    def __init__(self, *, sample_rate: int, channels: int, clock: Clock | None = None) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if channels not in (1, 2):
            raise ValueError("channels must be 1 or 2")
        self._sample_rate = sample_rate
        self._channels = channels
        self._clock = clock or SystemClock()
        self._frames_written = 0
        self._is_open = False

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def frames_written(self) -> int:
        return self._frames_written

    @property
    def seconds_written(self) -> float:
        return self._frames_written / self._sample_rate

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    @abstractmethod
    def name(self) -> str: ...

    async def open(self) -> None:
        if self._is_open:
            return
        await self._do_open()
        self._is_open = True

    async def close(self) -> None:
        if not self._is_open:
            return
        # Cleared first so a failing close cannot be retried into an inconsistent state —
        # the device is gone either way, and a second close must not raise again.
        self._is_open = False
        await self._do_close()

    async def write(self, buffer: AudioBuffer) -> None:
        if not self._is_open:
            raise AudioSinkError(f"{self.name} sink is not open", sink=self.name)
        if buffer.is_empty:
            return
        if buffer.sample_rate != self._sample_rate:
            raise AudioSinkError(
                f"{self.name} sink expects {self._sample_rate} Hz, got "
                f"{buffer.sample_rate} Hz. Resample before writing; reinterpreting the rate "
                "changes pitch and makes the play clock wrong",
                sink=self.name,
            )
        if buffer.channels != self._channels:
            buffer = buffer.with_channels(self._channels)
        await self._do_write(buffer)
        self._frames_written += buffer.frames

    # -- subclass hooks ----------------------------------------------------

    async def _do_open(self) -> None:
        return None

    async def _do_close(self) -> None:
        return None

    @abstractmethod
    async def _do_write(self, buffer: AudioBuffer) -> None: ...


class NullSink(BaseSink):
    """Discards audio, paced by the injected clock.

    The workhorse of §64 and §72. With a :class:`~tradefix_radio.core.clock.SystemClock` it
    behaves like a perfect device — a block of audio takes exactly its own duration — and the
    station runs in real time with no sound card. With a ``VirtualClock`` the same code runs
    a simulated week in minutes, and the play clock stays exact because it is derived from
    frames written rather than from wall time.

    ``realtime=False`` disables pacing entirely, for unit tests that only care about what was
    written.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 44_100,
        channels: int = 2,
        clock: Clock | None = None,
        realtime: bool = True,
    ) -> None:
        super().__init__(sample_rate=sample_rate, channels=channels, clock=clock)
        self._realtime = realtime
        self._peak = 0.0

    @property
    def name(self) -> str:
        return "null_sink"

    @property
    def peak(self) -> float:
        """Loudest sample seen. Lets a silent-output bug fail a run that writes nothing."""
        return self._peak

    async def _do_write(self, buffer: AudioBuffer) -> None:
        self._peak = max(self._peak, buffer.peak())
        if self._realtime:
            await self._clock.sleep(buffer.duration_seconds)


class WavFileSink(BaseSink):
    """Streams the broadcast to one growing audio file.

    For listening to what a soak run actually produced — which is the only way to catch the
    class of defect that no assertion describes, such as a crossfade that measures clean and
    sounds wrong.

    Written incrementally through an open handle rather than accumulated and saved at the end:
    a 2-hour run is ~1.3 GB of float32, and the file has to survive the run being killed.
    """

    def __init__(
        self,
        path: Path,
        *,
        sample_rate: int = 44_100,
        channels: int = 2,
        clock: Clock | None = None,
        realtime: bool = False,
        subtype: str = "PCM_16",
    ) -> None:
        super().__init__(sample_rate=sample_rate, channels=channels, clock=clock)
        self._path = path
        self._realtime = realtime
        self._subtype = subtype
        self._handle: sf.SoundFile | None = None

    @property
    def name(self) -> str:
        return "wav_file"

    @property
    def path(self) -> Path:
        return self._path

    async def _do_open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._handle = sf.SoundFile(
                str(self._path),
                mode="w",
                samplerate=self._sample_rate,
                channels=self._channels,
                subtype=self._subtype,
            )
        except (sf.LibsndfileError, RuntimeError, OSError) as exc:
            raise AudioSinkError(
                f"cannot open {self._path} for writing: {exc}", sink=self.name
            ) from exc

    async def _do_write(self, buffer: AudioBuffer) -> None:
        if self._handle is None:
            raise AudioSinkError("wav_file sink has no open handle", sink=self.name)
        try:
            self._handle.write(buffer.clipped().samples)
        except (sf.LibsndfileError, RuntimeError, OSError) as exc:
            raise AudioSinkError(
                f"cannot write to {self._path}: {exc}", sink=self.name
            ) from exc
        if self._realtime:
            await self._clock.sleep(buffer.duration_seconds)

    async def _do_close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


class SoundDeviceSink(BaseSink):
    """Real audio output through PortAudio (§51's VB-CABLE path).

    Imported lazily, and ``sounddevice`` is an optional dependency: §72's simulation mode is
    supposed to run on a machine with no sound card at all, and a hard import would make the
    whole package unimportable there.

    Pacing comes from PortAudio's blocking write, which returns when the ring buffer has room.
    That is the device's clock rather than ours, which is the point — a station that paced
    itself would drift and underrun after hours.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 44_100,
        channels: int = 2,
        device_name: str | None = None,
        block_frames: int = 1_024,
        buffer_blocks: int = 8,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(sample_rate=sample_rate, channels=channels, clock=clock)
        self._device_name = device_name
        self._block_frames = block_frames
        self._buffer_blocks = buffer_blocks
        self._stream: object | None = None

    @property
    def name(self) -> str:
        return "sounddevice"

    async def _do_open(self) -> None:
        try:
            import sounddevice  # noqa: PLC0415 - optional dependency, imported on use
        except Exception as exc:
            raise AudioSinkError(
                "the 'sounddevice' package is required for device output; install the "
                "'audio' extra (pip install -e .[audio]) or set audio.sink to null_sink",
                sink=self.name,
            ) from exc

        try:
            stream = sounddevice.OutputStream(
                samplerate=self._sample_rate,
                channels=self._channels,
                dtype="float32",
                blocksize=self._block_frames,
                latency=self._block_frames * self._buffer_blocks / self._sample_rate,
                device=self._device_name,
            )
            stream.start()
        except Exception as exc:
            raise AudioSinkError(
                f"cannot open audio device {self._device_name or 'default'!r}: {exc}",
                sink=self.name,
                device=self._device_name,
            ) from exc
        self._stream = stream

    async def _do_write(self, buffer: AudioBuffer) -> None:
        stream = self._stream
        if stream is None:
            raise AudioSinkError("sounddevice sink has no open stream", sink=self.name)
        data = buffer.clipped().samples
        try:
            # Blocking write, off the event loop: PortAudio's write blocks until the ring
            # buffer has room, and doing that on the loop would stall every other task —
            # including the generation worker in a single-process deployment.
            await asyncio.to_thread(stream.write, data)  # type: ignore[attr-defined]
        except Exception as exc:
            raise AudioSinkError(
                f"audio device write failed: {exc}", sink=self.name
            ) from exc

    async def _do_close(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.stop()  # type: ignore[attr-defined]
            stream.close()  # type: ignore[attr-defined]
        except Exception as exc:
            raise AudioSinkError(f"cannot close audio device: {exc}", sink=self.name) from exc


class FFmpegPipeSink(BaseSink):
    """Pipes raw PCM to an ffmpeg process, for encoded streaming.

    Deferred to its extension point: §88 lists direct RTMP streaming as out of scope for V1,
    and §51 routes audio to OBS through a virtual cable instead. This exists so that path is
    an adapter rather than a redesign, and it is exercised against ``-f null`` in the tests.
    """

    def __init__(
        self,
        arguments: list[str],
        *,
        sample_rate: int = 44_100,
        channels: int = 2,
        clock: Clock | None = None,
        executable: str = "ffmpeg",
    ) -> None:
        super().__init__(sample_rate=sample_rate, channels=channels, clock=clock)
        self._arguments = arguments
        self._executable = executable
        self._process: asyncio.subprocess.Process | None = None

    @property
    def name(self) -> str:
        return "ffmpeg_pipe"

    @property
    def returncode(self) -> int | None:
        return None if self._process is None else self._process.returncode

    async def _do_open(self) -> None:
        # asyncio's subprocess rather than subprocess.Popen: spawning a process blocks for
        # milliseconds, and ``drain()`` on the pipe gives real backpressure. The blocking
        # version needed a thread hop per block to avoid stalling the loop, which is a lot
        # of machinery to work around using the wrong API.
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._executable,
                "-hide_banner",
                "-loglevel", "error",
                "-f", "f32le",
                "-ar", str(self._sample_rate),
                "-ac", str(self._channels),
                "-i", "pipe:0",
                *self._arguments,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            raise AudioSinkError(
                f"cannot start {self._executable}: {exc}", sink=self.name
            ) from exc

    async def _do_write(self, buffer: AudioBuffer) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise AudioSinkError("ffmpeg sink has no open pipe", sink=self.name)
        if process.returncode is not None:
            raise AudioSinkError(
                f"ffmpeg exited with code {process.returncode}: "
                f"{await self._read_stderr(process)}",
                sink=self.name,
            )
        payload = np.ascontiguousarray(buffer.clipped().samples).tobytes()
        try:
            process.stdin.write(payload)
            # Backpressure: returns once the OS pipe has room, which paces the station to
            # the encoder's real throughput rather than to a guess about it.
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise AudioSinkError(
                f"ffmpeg pipe closed: {exc} ({await self._read_stderr(process)})",
                sink=self.name,
            ) from exc

    async def _do_close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            # Closing stdin is how ffmpeg is told the stream ended; it then flushes and
            # exits. An already-closed pipe is the state being aimed for either way.
            with contextlib.suppress(OSError, BrokenPipeError):
                process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            process.kill()
            with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
                await asyncio.wait_for(process.wait(), timeout=5.0)

    @staticmethod
    async def _read_stderr(process: asyncio.subprocess.Process) -> str:
        """ffmpeg's own complaint, which is the only useful part of an encoder failure."""
        if process.stderr is None:
            return "no stderr captured"
        try:
            data = await asyncio.wait_for(process.stderr.read(4096), timeout=1.0)
        except (asyncio.TimeoutError, OSError):
            return "stderr unavailable"
        return data.decode(errors="replace").strip() or "no error output"


__all__ = [
    "AudioSink",
    "BaseSink",
    "FFmpegPipeSink",
    "NullSink",
    "SoundDeviceSink",
    "WavFileSink",
]
