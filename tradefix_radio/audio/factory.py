"""Choosing an audio sink from configuration.

The mirror of `generation/factory.py`, and it exists for the same reason: before this, the
dev runner constructed `NullSink` directly, so there was no path from
``audio.sink: sounddevice`` in a config file to actual sound coming out of the machine. The
setting was read by `doctor` and by nothing that played audio.

ADR-14 governs the format: **the playout format is the sink's format.** The engine conforms
every input at the boundary, so the sink is the authority on rate and channel count, and the
configured values are what the device is opened with rather than a canonical constant the
device may not support.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from tradefix_radio.audio.sinks import AudioSink, NullSink, SoundDeviceSink, WavFileSink

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradefix_radio.config.schema import AppSettings
    from tradefix_radio.core.clock import Clock

_log = structlog.get_logger(__name__)

__all__ = ["build_sink", "describe_sink"]


def build_sink(
    settings: AppSettings,
    *,
    clock: Clock,
    realtime: bool = True,
    output_path: Path | None = None,
) -> AudioSink:
    """The sink this configuration asks for.

    ``realtime`` only affects `NullSink`: a soak wants it to return instantly while a
    development run wants it to pace like a real device, and that difference belongs to the
    caller rather than to configuration.
    """
    audio = settings.audio
    name = audio.sink

    if name == "sounddevice":
        _log.info(
            "audio.sink_selected",
            sink=name,
            device=audio.device_name,
            host_api=audio.device_host_api,
            sample_rate=audio.sample_rate,
            channels=audio.channels,
        )
        return SoundDeviceSink(
            sample_rate=audio.sample_rate,
            channels=audio.channels,
            device_name=audio.device_name,
            host_api=audio.device_host_api,
            block_frames=audio.block_frames,
            buffer_blocks=audio.buffer_blocks,
            clock=clock,
        )

    if name == "wav_file":
        destination = output_path or (settings.paths.data_dir / "broadcast.wav")
        _log.info("audio.sink_selected", sink=name, path=str(destination))
        return WavFileSink(
            path=destination,
            sample_rate=audio.sample_rate,
            channels=audio.channels,
            clock=clock,
        )

    _log.info("audio.sink_selected", sink="null_sink", realtime=realtime)
    return NullSink(
        sample_rate=audio.sample_rate,
        channels=audio.channels,
        clock=clock,
        realtime=realtime,
    )


def describe_sink(sink: AudioSink) -> str:
    """One line naming what is actually playing, for the console banner.

    Reads the device the sink *resolved* rather than the one configured: on Windows the
    configured name matches several host APIs and the sink chooses between them, so the
    configured string is not reliably what is playing.
    """
    resolved = getattr(sink, "resolved_device", None)
    if resolved is not None:
        index, device_name, api = resolved
        return f"{sink.name} -> [{index}] {device_name} ({api})"
    return sink.name
