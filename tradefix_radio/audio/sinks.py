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
from typing import Any, Final, Protocol, runtime_checkable

import numpy as np
import soundfile as sf
import structlog

from tradefix_radio.audio.pcm import AudioBuffer
from tradefix_radio.core.clock import Clock, SystemClock
from tradefix_radio.core.errors import AudioSinkError

_log = structlog.get_logger(__name__)


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
        host_api: str | None = None,
        block_frames: int = 1_024,
        buffer_blocks: int = 8,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(sample_rate=sample_rate, channels=channels, clock=clock)
        self._device_name = device_name
        self._host_api = host_api
        self._block_frames = block_frames
        self._buffer_blocks = buffer_blocks
        self._stream: object | None = None
        #: ``(index, name, host api)`` actually opened, for logs and the §49 page.
        self._resolved: tuple[int, str, str] | None = None

    @property
    def resolved_device(self) -> tuple[int, str, str] | None:
        """Which device is in use, once open. ``None`` before open, or on the default."""
        return self._resolved

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

        device = _resolve_output_device(
            sounddevice, self._device_name, self._host_api, sink=self.name
        )
        self._resolved = device

        try:
            stream = sounddevice.OutputStream(
                samplerate=self._sample_rate,
                channels=self._channels,
                dtype="float32",
                blocksize=self._block_frames,
                latency=self._block_frames * self._buffer_blocks / self._sample_rate,
                device=None if device is None else device[0],
            )
            stream.start()
        except Exception as exc:
            raise AudioSinkError(
                f"cannot open audio device {self._device_name or 'default'!r}: {exc}",
                sink=self.name,
                device=self._device_name,
            ) from exc
        self._stream = stream
        if device is not None:
            # Logged because the host API was very likely chosen for the operator rather
            # than by them, and which one is in use changes latency and resampling.
            _log.info(
                "audio.device_opened",
                index=device[0],
                device=device[1],
                host_api=device[2],
                sample_rate=self._sample_rate,
                channels=self._channels,
            )

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


# ------------------------------------------------------------- device resolution

#: Host APIs preferred, best first, when one name matches under several of them.
#:
#: Every Windows device is enumerated once per host API, so a bare name is ambiguous on
#: essentially every Windows machine — including for the shipped production value
#: ``CABLE Input``. WASAPI first because it is the modern path, has the lowest latency, and
#: runs natively at 48 kHz, which is the station's canonical playout rate: choosing it
#: avoids a resample on every block.
_HOST_API_PREFERENCE: Final = ("WASAPI", "WDM-KS", "DirectSound", "MME")


def _resolve_output_device(
    sounddevice: Any, name: str | None, host_api: str | None, *, sink: str
) -> tuple[int, str, str] | None:
    """Turn a configured device name into a concrete PortAudio index.

    ``None`` means "use the system default", which is what an unset name means.

    Resolution happens here rather than being handed to `sounddevice` because its own name
    matching *raises* on an ambiguous name, and on Windows every name is ambiguous. Passing
    the name straight through made the station unopenable on a normal desktop, with an error
    that said the device had been found several times and nothing about what to do.

    A numeric string is accepted as an index, so an operator can paste a number straight
    from `tradefix audio devices` when a name is genuinely unhelpful.
    """
    if name is None or not str(name).strip():
        return None

    text = str(name).strip()
    if text.isdigit():
        index = int(text)
        try:
            info = sounddevice.query_devices(index)
        except Exception as exc:
            raise AudioSinkError(
                f"audio.device_name={text!r} is not a valid device index: {exc}",
                sink=sink,
                device=text,
            ) from exc
        if int(info.get("max_output_channels", 0)) <= 0:
            raise AudioSinkError(
                f"device [{index}] {info.get('name')!r} has no output channels",
                sink=sink,
                device=text,
            )
        api = sounddevice.query_hostapis(info["hostapi"])["name"]
        return index, str(info["name"]), str(api)

    needle = text.casefold()
    matches: list[tuple[int, str, str]] = []
    for index, info in enumerate(sounddevice.query_devices()):
        if int(info.get("max_output_channels", 0)) <= 0:
            continue
        device_name = str(info.get("name", ""))
        if needle not in device_name.casefold():
            continue
        api = str(sounddevice.query_hostapis(info["hostapi"])["name"])
        if host_api and host_api.casefold() not in api.casefold():
            continue
        matches.append((index, device_name, api))

    if not matches:
        qualifier = f" on host API {host_api!r}" if host_api else ""
        raise AudioSinkError(
            # The remedy, not just the fault. §72 expects simulation mode to run on a
            # machine with no sound card at all, and an operator who hits this needs to be
            # told the way out — `null_sink` — rather than left with a device list.
            f"no output device matching {text!r}{qualifier}. Set audio.device_name to one "
            f"of the devices below, or audio.sink to null_sink to run with no audio "
            f"output at all. Available:\n{describe_output_devices(sounddevice)}",
            sink=sink,
            device=text,
        )
    if len(matches) == 1:
        return matches[0]

    # Several matches. If they are one device under different host APIs — the normal Windows
    # case — preference order decides and the choice is logged. If the *names* differ, the
    # configuration is ambiguous about which hardware is wanted, and guessing there would be
    # the silent wrong-device selection worth refusing.
    if len({device_name.casefold() for _i, device_name, _a in matches}) > 1:
        listed = "\n".join(f"  [{i}] {n}  ({a})" for i, n, a in matches)
        raise AudioSinkError(
            f"{text!r} matches several different output devices; set audio.device_name to a "
            f"longer substring, or to an index:\n{listed}",
            sink=sink,
            device=text,
        )

    for preferred in _HOST_API_PREFERENCE:
        for candidate in matches:
            if preferred.casefold() in candidate[2].casefold():
                return candidate
    return matches[0]


def describe_output_devices(sounddevice: Any) -> str:
    """Every output device, numbered, with its host API. For errors and the CLI."""
    lines = []
    for index, info in enumerate(sounddevice.query_devices()):
        if int(info.get("max_output_channels", 0)) <= 0:
            continue
        api = sounddevice.query_hostapis(info["hostapi"])["name"]
        lines.append(
            f"  [{index:3d}] {str(info['name'])[:48]:50s} "
            f"{int(info['max_output_channels'])}ch  ({api})"
        )
    return "\n".join(lines) or "  (no output devices found)"
