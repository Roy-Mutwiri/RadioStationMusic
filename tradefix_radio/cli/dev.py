"""``tradefix dev`` — run the station and serve the Control Center.

§72's development mode in one command: a simulated market, the mock provider, a null sink, and
an HTTP API with the Control Center attached. No GPU, no broker, no OBS, no sound device.

The command deliberately refuses to run in production mode. Not because the code would break —
it would work — but because this runner builds a *simulated* feed and a *mock* provider by
construction, and a production deployment that got those silently would be broadcasting
nothing real while reporting that it was. §72 already makes production refuse a simulated
feed; this refuses one step earlier, with a sentence saying what to run instead.
"""

from __future__ import annotations

import argparse

from tradefix_radio.config.schema import AppSettings, RunMode
from tradefix_radio.market.simulation import Scenario

__all__ = ["command", "register"]


def register(subparsers: object) -> None:
    """Add the ``dev`` subcommand."""
    parser = subparsers.add_parser(  # type: ignore[attr-defined]
        "dev",
        help="run the station and serve the Control Center (development mode)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind")
    parser.add_argument("--port", type=int, default=8000, help="port to bind")
    parser.add_argument(
        "--scenario",
        default=Scenario.RANDOM_WALK.value,
        choices=[scenario.value for scenario in Scenario],
        help="market scenario the simulator starts on",
    )
    parser.add_argument("--seed", type=int, default=2026, help="RNG seed (reproducible)")


async def command(args: argparse.Namespace, settings: AppSettings) -> int:
    """Start everything and serve until interrupted."""
    from tradefix_radio.api.runner import frontend_dist, serve  # noqa: PLC0415

    if settings.mode is RunMode.PRODUCTION:
        print(
            "tradefix dev builds a simulated feed and the mock provider, which production "
            "must never run on.\nUse --mode development or --mode simulation."
        )
        return 2

    dist = frontend_dist()
    if not dist.is_dir():
        print(
            f"The Control Center has not been built ({dist} is missing).\n"
            "The API will still serve; run `npm install && npm run build` in frontend/ "
            "to serve the UI from this process,\nor `npm run dev` for the hot-reloading "
            "frontend against this API."
        )

    url = f"http://{args.host}:{args.port}"
    print(
        f"\n  TRADE FIX RADIO — THE MARKET COMPOSES THE RADIO\n"
        f"  Control Center : {url}\n"
        f"  Broadcast overlay: {url}/overlay/live\n"
        f"  API docs       : {url}/api/docs\n"
        f"  Mode           : {settings.mode.value}   Scenario: {args.scenario}\n"
    )
    await serve(
        settings,
        host=args.host,
        port=args.port,
        scenario=Scenario(args.scenario),
        seed=args.seed,
    )
    return 0
