"""`tradefix audio devices` — what the station can play through.

Exists because selecting an output device was, until this command, a matter of guessing a
substring and finding out at launch whether it matched one device, several, or none. On
Windows it matches several essentially always, since every device is enumerated once per
host API.

Prints what `audio.device_name` and `audio.device_host_api` should be set to.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradefix_radio.config.schema import AppSettings

__all__ = ["command", "register"]


def register(subparsers: object) -> None:
    """Add the ``audio`` subcommand."""
    parser = subparsers.add_parser(  # type: ignore[attr-defined]
        "audio", help="inspect audio output devices"
    )
    actions = parser.add_subparsers(dest="audio_action")
    actions.add_parser("devices", help="list output devices and show which is configured")


async def command(_args: argparse.Namespace, settings: AppSettings) -> int:
    try:
        import sounddevice  # noqa: PLC0415 - optional dependency
    except ImportError:
        print(
            "  The 'sounddevice' package is not installed.\n"
            "  Install the audio extra:  pip install -e .[audio]\n"
            "  Without it the station can only use null_sink or wav_file."
        )
        return 1

    from tradefix_radio.audio.sinks import (  # noqa: PLC0415
        _resolve_output_device,
        describe_output_devices,
    )

    audio = settings.audio
    print()
    print("  Output devices")
    print(describe_output_devices(sounddevice))
    print()

    default_index = sounddevice.default.device[1]
    if default_index is not None and default_index >= 0:
        info = sounddevice.query_devices(default_index)
        print(f"  Windows default : [{default_index}] {info['name']}")

    print(f"  audio.sink      : {audio.sink}")
    print(f"  audio.device_name    : {audio.device_name!r}")
    print(f"  audio.device_host_api: {audio.device_host_api!r}")

    # The question an operator actually has: what will the station open?
    try:
        resolved = _resolve_output_device(
            sounddevice, audio.device_name, audio.device_host_api, sink="sounddevice"
        )
    except Exception as error:  # noqa: BLE001 - the message is the output here
        print()
        print("  The configured device does NOT resolve:")
        for line in str(error).splitlines():
            print(f"    {line}")
        return 1

    print()
    if resolved is None:
        print("  Resolves to     : the system default device")
    else:
        index, name, api = resolved
        print(f"  Resolves to     : [{index}] {name}  ({api})")
    print()
    return 0
