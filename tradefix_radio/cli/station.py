"""`tradefix station start` — run the real station with real audio output.

The difference from `tradefix dev`: this one is meant to be *listened to*. It builds the
sink from configuration rather than assuming a headless run, prints a banner saying exactly
what will play where, and refuses to start quietly when something an operator needs is
missing.

`dev` remains the headless Control Center runner. Both share the same `ControlCenterRunner`,
so there is one composition root and not two that drift.

TEST MODE
---------
`--test-mode` lowers the buffer targets so a human can hear the first track within a couple
of minutes instead of waiting for a 45-minute buffer to fill at 1.17x realtime. It is a
**command-line override that announces itself**, not a changed default: the shipped
production targets are untouched, the banner says TEST MODE in capitals, and the setting
appears in the run's own log. A quiet override of a safety-adjacent target is how a test
configuration ends up in production.

It changes *buffer* targets only. It does not touch QC, originality or mastering thresholds
— §7.20's rule that operational urgency may change scheduling, never audio integrity.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import TYPE_CHECKING

from tradefix_radio.contracts.enums import RunMode, StartupMode
from tradefix_radio.market.simulation import Scenario

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradefix_radio.config.schema import AppSettings

__all__ = ["command", "register"]

#: Buffer targets for an interactive listening test.
#:
#: At the measured 1.17x realtime generation capacity, the production 45-minute target takes
#: around 38 minutes of uninterrupted generation to reach from empty. That is a reasonable
#: broadcast posture and a poor way to spend the first ten minutes of a human test, so the
#: test posture is: start playing as soon as there is one track, aim for twelve minutes.
TEST_MODE_MINIMUM_MINUTES = 1.5
TEST_MODE_TARGET_MINUTES = 12.0
TEST_MODE_MAXIMUM_MINUTES = 25.0


def register(subparsers: object) -> None:
    """Add the ``station`` subcommand."""
    parser = subparsers.add_parser(  # type: ignore[attr-defined]
        "station",
        help="run the station with real audio output and the Control Center",
    )
    actions = parser.add_subparsers(dest="station_action")
    start = actions.add_parser("start", help="start broadcasting")

    start.add_argument("--host", default="127.0.0.1", help="interface to bind")
    start.add_argument("--port", type=int, default=8000, help="port to bind")
    start.add_argument(
        "--scenario",
        default=Scenario.RANDOM_WALK.value,
        choices=[scenario.value for scenario in Scenario],
        help="market scenario the simulator starts on",
    )
    start.add_argument("--seed", type=int, default=2026, help="RNG seed (reproducible)")
    start.add_argument(
        "--device",
        default=None,
        help=(
            "output device name substring or index, overriding audio.device_name. "
            "Run `tradefix audio devices` to list them."
        ),
    )
    start.add_argument(
        "--host-api",
        default=None,
        help="pin the host API when a device name matches several (e.g. WASAPI, MME)",
    )
    start.add_argument(
        "--sink",
        default=None,
        choices=["sounddevice", "null_sink", "wav_file"],
        help="override audio.sink for this run",
    )
    start.add_argument(
        "--test-mode",
        action="store_true",
        help=(
            "lower buffer targets so playback starts within minutes. Announced in the "
            "banner and the log; production defaults are not modified."
        ),
    )
    start.add_argument(
        "--provider",
        default=None,
        choices=("mock", "ace_step"),
        help=(
            "override generation.provider for this run. Development mode ships `mock` so "
            "the stack runs without a GPU; a real listening test needs `ace_step`."
        ),
    )
    start.add_argument(
        "--open",
        action="store_true",
        help="open the Control Center in the default browser once it is serving",
    )
    # Fresh start programming (§FSP)
    startup_group = start.add_mutually_exclusive_group()
    startup_group.add_argument(
        "--wait-for-fresh",
        action="store_true",
        default=True,
        help=(
            "wait for fresh tracks before audible playout (default). "
            "Ensures the listener hears never-before-played music from track 1."
        ),
    )
    startup_group.add_argument(
        "--immediate",
        action="store_true",
        help=(
            "start playout immediately with emergency fallback if needed. "
            "Use for live recovery where dead air is worse than temporary fallback."
        ),
    )


def _apply_overrides(settings: AppSettings, args: argparse.Namespace) -> AppSettings:
    """Build the effective settings for this run, leaving the files alone."""
    audio_updates: dict[str, object] = {}
    if args.sink:
        audio_updates["sink"] = args.sink
    if args.device:
        audio_updates["device_name"] = args.device
    if getattr(args, "host_api", None):
        audio_updates["device_host_api"] = args.host_api
    if audio_updates:
        settings = settings.model_copy(
            update={"audio": settings.audio.model_copy(update=audio_updates)}
        )

    if getattr(args, "provider", None):
        settings = settings.model_copy(
            update={
                "generation": settings.generation.model_copy(
                    update={"provider": args.provider}
                )
            }
        )

    if args.test_mode:
        # Buffer targets only. Nothing here reaches a QC, originality or mastering
        # threshold, and nothing here touches the lock rules or the retry policy — test
        # mode shortens the wait before playback starts, and that is the whole of what it
        # is allowed to do. `test_mode` itself travels so the Control Center can label the
        # run rather than leaving a 12-minute target looking like a misconfiguration.
        settings = settings.model_copy(
            update={
                "test_mode": True,
                "radio": settings.radio.model_copy(
                    update={
                        "minimum_buffer_minutes": TEST_MODE_MINIMUM_MINUTES,
                        "target_buffer_minutes": TEST_MODE_TARGET_MINUTES,
                        "maximum_buffer_minutes": TEST_MODE_MAXIMUM_MINUTES,
                    }
                ),
            }
        )
    return settings


def _banner(settings: AppSettings, args: argparse.Namespace, url: str) -> str:
    audio = settings.audio
    radio = settings.radio
    lines = [
        "",
        "  ==========================================================",
        "   TRADE FIX RADIO",
        "  ==========================================================",
        f"   mode          {settings.mode.value}",
        f"   market        {settings.market.feed} / scenario {args.scenario}",
        f"   generator     {settings.generation.provider}",
    ]
    if settings.generation.provider == "ace_step":
        ace = settings.generation.ace_step
        lines.append(f"                 {ace.dit_model}, profile {ace.profile}")
        lines.append(f"                 service {ace.base_url}")
    lines.extend(
        [
            f"   audio         {audio.sink}",
        ]
    )
    if audio.sink == "sounddevice":
        api = f" [{audio.device_host_api}]" if audio.device_host_api else ""
        lines.append(f"                 device {audio.device_name!r}{api}")
        lines.append(f"                 {audio.sample_rate} Hz, {audio.channels} ch")
    lines.append(
        f"   buffer        min {radio.minimum_buffer_minutes:.1f} / "
        f"target {radio.target_buffer_minutes:.0f} / "
        f"max {radio.maximum_buffer_minutes:.0f} min"
    )
    if args.test_mode:
        lines.extend(
            [
                "",
                "   *** TEST MODE: buffer targets lowered for interactive testing. ***",
                "   *** Shipped production defaults are NOT modified.              ***",
            ]
        )
    if getattr(args, "immediate", False):
        lines.extend(
            [
                "",
                "   *** LIVE RECOVERY MODE: playout starts immediately with fallback. ***",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "   *** FRESH START: waiting for fresh tracks before audible playout. ***",
            ]
        )
    lines.extend(
        [
            "",
            f"   Control Center  {url}",
            "   Ctrl-C to stop",
            "  ==========================================================",
            "",
        ]
    )
    return "\n".join(lines)


async def command(args: argparse.Namespace, settings: AppSettings) -> int:
    """Start the station and serve until interrupted."""
    from tradefix_radio.api.runner import frontend_dist, serve  # noqa: PLC0415

    if getattr(args, "station_action", None) not in (None, "start"):
        print("usage: tradefix station start")
        return 2

    settings = _apply_overrides(settings, args)

    if settings.mode is RunMode.PRODUCTION:
        # The runner builds a simulated feed. Production must reach this through its own
        # entry point rather than through a command whose market is a simulator.
        print(
            "tradefix station start builds a simulated market feed, which production must "
            "never run on.\nUse --mode development or --mode simulation."
        )
        return 2

    dist = frontend_dist()
    if not dist.is_dir():
        print(
            "The Control Center has not been built.\n"
            "  cd frontend && npm install && npm run build"
        )
        return 2

    url = f"http://{args.host}:{args.port}"
    print(_banner(settings, args, url))

    if args.open:
        import webbrowser  # noqa: PLC0415

        # Delayed, so the browser does not race the server to the port.
        async def _open_later() -> None:
            await asyncio.sleep(2.0)
            webbrowser.open(url)

        asyncio.create_task(_open_later())  # noqa: RUF006 - fire and forget by design

    # Determine startup mode from CLI flags
    startup_mode = (
        StartupMode.LIVE_RECOVERY if getattr(args, "immediate", False)
        else StartupMode.CONTROLLED_START
    )

    await serve(
        settings,
        host=args.host,
        port=args.port,
        scenario=Scenario(args.scenario),
        seed=args.seed,
        startup_mode=startup_mode,
    )
    return 0
