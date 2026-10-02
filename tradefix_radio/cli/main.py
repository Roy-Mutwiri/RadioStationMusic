"""Command-line entry point (§78, §79).

Commands available in Phase 1::

    tradefix doctor      environment and dependency report (§79)
    tradefix init        create the directory tree and apply migrations
    tradefix migrate     apply database migrations
    tradefix config      show the resolved configuration, secrets masked
    tradefix version     version and interpreter
    tradefix market-sim  run a market scenario and print the engine's reading (§7, §47)

Later phases add ``report-director``, ``soak``, ``benchmark``, ``dev``,
``simulation`` and ``production`` (§78).

``argparse`` rather than a CLI framework: this is the one component that must run
on a half-broken installation to tell the operator what is broken. Adding a
dependency to the diagnostic tool is how you get "doctor itself crashes".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any

from tradefix_radio import __version__
from tradefix_radio.config.loader import ensure_directories, load_settings
from tradefix_radio.config.schema import AppSettings
from tradefix_radio.contracts.enums import HealthStatus, RunMode
from tradefix_radio.contracts.health import ComponentHealthV1, SystemHealthV1
from tradefix_radio.core.clock import Clock, SystemClock
from tradefix_radio.core.errors import ConfigurationError, PersistenceError, RadioError
from tradefix_radio.monitoring import checks as env_checks
from tradefix_radio.monitoring.health import (
    HealthCheck,
    HealthRegistry,
    healthy,
    unhealthy,
)
from tradefix_radio.monitoring.logging import configure_logging

#: ANSI colours, used only when stdout is a TTY so piped output stays clean.
_COLOURS = {
    HealthStatus.HEALTHY: "\033[32m",
    HealthStatus.DEGRADED: "\033[33m",
    HealthStatus.CRITICAL: "\033[31m",
    HealthStatus.RECOVERING: "\033[36m",
    HealthStatus.UNKNOWN: "\033[90m",
}
_RESET = "\033[0m"

#: §79's table labels, in the order the brief lists them.
_LABELS = {
    "python": "Python",
    "node": "Node",
    "ffmpeg": "FFmpeg",
    "config": "Configuration",
    "directories": "Directories",
    "database": "Database",
    "gpu": "GPU",
    "ace_step_environment": "ACE-Step toolchain",
    "generation_provider": "Generation provider",
    "market_feed": "Market Feed",
    "obs": "OBS WebSocket",
    "audio_device": "Audio Device",
    "disk_space": "Disk Space",
}

_STATUS_TEXT = {
    HealthStatus.HEALTHY: "OK",
    HealthStatus.DEGRADED: "WARN",
    HealthStatus.CRITICAL: "FAIL",
    HealthStatus.RECOVERING: "RECOVERING",
    HealthStatus.UNKNOWN: "UNKNOWN",
}


def _use_colour() -> bool:
    return sys.stdout.isatty() and sys.platform != "emscripten"


def _paint(text: str, status: HealthStatus) -> str:
    if not _use_colour():
        return text
    return f"{_COLOURS[status]}{text}{_RESET}"


# ---------------------------------------------------------------- doctor


async def _check_database(clock: Clock, settings: AppSettings) -> ComponentHealthV1:
    """Connect, then verify migrations have been applied.

    Connectivity alone is not enough: an empty but reachable database passes a
    ``SELECT 1`` and then fails on the first real query. §73 exists to catch
    exactly this class of problem before playback starts.
    """
    from tradefix_radio.persistence.database import Database

    database = Database(settings.database)
    try:
        await database.connect()
    except PersistenceError as exc:
        return unhealthy(
            "database",
            HealthStatus.CRITICAL,
            exc.message,
            clock=clock,
            remediation=(
                "For SQLite, check that the data directory exists and is writable "
                "(`tradefix init`). For PostgreSQL, verify the server is running and "
                "TRADEFIX_DATABASE__URL is correct."
            ),
        )
    try:
        latency = await database.ping()
        tables = set(await database.table_names())
        if "alembic_version" not in tables:
            return unhealthy(
                "database",
                HealthStatus.CRITICAL,
                "reachable but no schema has been applied",
                clock=clock,
                remediation="Run `tradefix migrate` to create the schema.",
                latency_ms=latency,
            )
        missing = {"tracks", "queue_items", "generation_jobs"} - tables
        if missing:
            return unhealthy(
                "database",
                HealthStatus.CRITICAL,
                f"schema is incomplete; missing {', '.join(sorted(missing))}",
                clock=clock,
                remediation="Run `tradefix migrate` to bring the schema up to date.",
                latency_ms=latency,
            )
        return healthy(
            "database",
            clock=clock,
            detail=f"{len(tables)} tables, {latency:.1f} ms round trip",
            latency_ms=latency,
            measurements={"table_count": float(len(tables))},
        )
    finally:
        await database.disconnect()


async def _check_config(clock: Clock, settings: AppSettings) -> ComponentHealthV1:
    """Configuration already validated by the time we get here.

    Reported as a check anyway so the §79 table shows the resolved mode and the
    operator can see at a glance which overlay is active — the most common source
    of "why is it using the mock provider?".
    """
    return healthy(
        "config",
        clock=clock,
        detail=(
            f"mode={settings.mode.value}, feed={settings.market.feed}, "
            f"provider={settings.generation.provider}, sink={settings.audio.sink}"
        ),
    )


def _build_registry(settings: AppSettings, clock: Clock) -> HealthRegistry:
    """Register every §79 check with its mode-appropriate required flag."""
    registry = HealthRegistry(clock=clock, timeout_seconds=20.0)

    def add(name: str, check: HealthCheck) -> None:
        registry.register(name, check, required=env_checks.is_required(name, settings))

    add("python", lambda: env_checks.check_python(clock))
    add("node", lambda: env_checks.check_node(clock))
    add("ffmpeg", lambda: env_checks.check_ffmpeg(clock))
    add("config", lambda: _check_config(clock, settings))
    add("directories", lambda: env_checks.check_directories(clock, settings))
    add("database", lambda: _check_database(clock, settings))
    add("gpu", lambda: env_checks.check_gpu(clock, settings))
    add("ace_step_environment", lambda: env_checks.check_ace_step_environment(clock))
    add("generation_provider", lambda: env_checks.check_generation_provider(clock, settings))
    add("market_feed", lambda: env_checks.check_market_feed(clock, settings))
    add("obs", lambda: env_checks.check_obs(clock, settings))
    add("audio_device", lambda: env_checks.check_audio_device(clock, settings))
    add("disk_space", lambda: env_checks.check_disk_space(clock, settings))
    return registry


def _render_doctor(health: SystemHealthV1) -> str:
    """Render the §79 table plus remediation for anything not healthy."""
    lines: list[str] = []
    width = max(len(label) for label in _LABELS.values()) + 2

    ordered = [
        component
        for name in _LABELS
        if (component := health.component(name)) is not None
    ]
    ordered += [c for c in health.components if c.name not in _LABELS]

    for component in ordered:
        label = _LABELS.get(component.name, component.name)
        status = _STATUS_TEXT[component.status]
        optional = "" if component.required else " (optional)"
        line = f"  {label:<{width}} {_paint(status, component.status):<14}"
        if component.detail:
            line += f" {component.detail}"
        lines.append(line + optional)

    problems = [c for c in ordered if c.status is not HealthStatus.HEALTHY]
    if problems:
        lines.append("")
        lines.append("  Remediation")
        lines.append("  " + "-" * 60)
        for component in problems:
            label = _LABELS.get(component.name, component.name)
            marker = "FAIL" if component.is_blocking else "WARN"
            lines.append(f"  [{marker}] {label}: {component.detail}")
            if component.remediation:
                for chunk in _wrap(component.remediation, 72):
                    lines.append(f"         {chunk}")
            lines.append("")

    lines.append("")
    overall = _paint(_STATUS_TEXT[health.status], health.status)
    lines.append(f"  Overall: {overall}")
    if health.blocking_failures:
        lines.append(
            "  Blocking failures: " + ", ".join(sorted(health.blocking_failures))
        )
        lines.append("  The station will not start until these are resolved.")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    """Minimal greedy wrapper. Preserves explicit newlines in remediation text."""
    out: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split():
            if current and len(current) + 1 + len(word) > width:
                out.append(current)
                current = word
            else:
                current = f"{current} {word}".strip()
        if current:
            out.append(current)
    return out


async def _cmd_doctor(args: argparse.Namespace) -> int:
    settings = _load(args)
    clock = SystemClock()
    registry = _build_registry(settings, clock)
    health = await registry.check_all()

    if args.json:
        print(json.dumps(health.to_json_dict(), indent=2, sort_keys=True))
    else:
        print()
        print(f"  TRADE FIX RADIO - doctor   (v{__version__}, mode={settings.mode.value})")
        print()
        print(_render_doctor(health))
        print()

    # Non-zero only for blocking failures. A warning on an optional dependency
    # must not break a CI pipeline that runs doctor as a smoke test.
    return 0 if health.can_start else 1


# ---------------------------------------------------------------- init / migrate


def _run_alembic(settings: AppSettings, *argv: str) -> int:
    """Invoke Alembic in-process against the configured database.

    In-process rather than as a subprocess so the migration runs with the same
    interpreter, the same resolved settings, and the same database URL the station
    will use — the alternative reliably ends with migrations applied to one file
    and the app reading another.

    The Config is built **programmatically** rather than read from ``alembic.ini``.
    Two reasons: the migration environment ships inside the package, so locating it
    from ``paths.root_dir`` (which is a relocatable *data* root) would break for any
    operator who puts data on a separate volume; and an installed, non-editable
    package has no ``alembic.ini`` on disk at all. ``alembic.ini`` remains for
    developers running the ``alembic`` CLI directly to author revisions.
    """
    from alembic import command
    from alembic.config import Config

    from tradefix_radio.persistence.database import MIGRATIONS_DIR, sync_database_url

    if not MIGRATIONS_DIR.is_dir():
        print(
            f"  FAIL  migration environment not found at {MIGRATIONS_DIR}",
            file=sys.stderr,
        )
        return 1

    # `stdout` must be passed explicitly: Alembic's Config captures `sys.stdout` as
    # a default argument evaluated at import time, so commands that print (current,
    # history) would write to whatever stream existed when alembic was first
    # imported — a closed stream under pytest capture, and the wrong stream whenever
    # output is redirected.
    config = Config(stdout=sys.stdout)
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", sync_database_url(settings.database.url))
    # env.py resolves the URL from settings by default; pass it explicitly so this
    # process's already-validated configuration is authoritative.
    config.cmd_opts = argparse.Namespace(x=[f"url={settings.database.url}"])
    action = argv[0]
    if action == "upgrade":
        command.upgrade(config, argv[1] if len(argv) > 1 else "head")
    elif action == "downgrade":
        command.downgrade(config, argv[1])
    elif action == "current":
        command.current(config, verbose=True)
    elif action == "history":
        command.history(config, verbose=False)
    else:
        print(f"  FAIL  unknown migration action {action!r}", file=sys.stderr)
        return 1
    return 0


async def _cmd_init(args: argparse.Namespace) -> int:
    settings = _load(args)
    created = ensure_directories(settings)
    print()
    print("  Directory tree")
    for directory in settings.paths.all_directories():
        marker = "created" if directory in created else "exists "
        print(f"    {marker}  {directory}")
    print()
    print("  Applying migrations")
    code = _run_alembic(settings, "upgrade", "head")
    if code != 0:
        return code
    print("    schema is up to date")
    print()
    print("  Next: run `tradefix doctor` to verify the environment.")
    print()
    return 0


async def _cmd_migrate(args: argparse.Namespace) -> int:
    settings = _load(args)
    if args.action == "current":
        return _run_alembic(settings, "current")
    if args.action == "history":
        return _run_alembic(settings, "history")
    if args.action == "down":
        if not args.revision:
            print("  FAIL  `migrate down` requires --revision", file=sys.stderr)
            return 1
        return _run_alembic(settings, "downgrade", args.revision)
    return _run_alembic(settings, "upgrade", args.revision or "head")


# ---------------------------------------------------------------- config


async def _cmd_config(args: argparse.Namespace) -> int:
    settings = _load(args)
    payload = settings.masked_dump()
    if args.section:
        if args.section not in payload:
            available = ", ".join(sorted(k for k in payload if isinstance(payload[k], dict)))
            print(
                f"  FAIL  unknown section {args.section!r}; available: {available}",
                file=sys.stderr,
            )
            return 1
        payload = {args.section: payload[args.section]}
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


async def _cmd_market_sim(args: argparse.Namespace) -> int:
    from tradefix_radio.cli import market_sim

    return await market_sim.command(args, _load(args))


async def _cmd_version(_args: argparse.Namespace) -> int:
    print(f"tradefix-radio {__version__}")
    print(f"python {sys.version.split()[0]} ({sys.executable})")
    return 0


# ---------------------------------------------------------------- wiring


def _load(args: argparse.Namespace) -> AppSettings:
    """Load settings, converting a configuration error into a clean exit."""
    mode = RunMode(args.mode) if getattr(args, "mode", None) else None
    try:
        return load_settings(config_file=args.config, mode=mode)
    except ConfigurationError as exc:
        print(f"\n  CONFIGURATION ERROR\n\n  {exc.message}\n", file=sys.stderr)
        raise SystemExit(2) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tradefix",
        description="Trade Fix Radio - autonomous market-reactive AI music station",
    )
    parser.add_argument("--version", action="version", version=f"tradefix-radio {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="FILE",
        help="operator YAML layered above the shipped defaults",
    )
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in RunMode],
        default=None,
        help="override the run mode (§72)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="environment and dependency report (§79)")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    doctor.set_defaults(handler=_cmd_doctor)

    init = sub.add_parser("init", help="create directories and apply migrations")
    init.set_defaults(handler=_cmd_init)

    migrate = sub.add_parser("migrate", help="apply database migrations")
    migrate.add_argument(
        "action",
        nargs="?",
        default="up",
        choices=["up", "down", "current", "history"],
        help="migration action (default: up)",
    )
    migrate.add_argument("--revision", default=None, help="target revision")
    migrate.set_defaults(handler=_cmd_migrate)

    config_cmd = sub.add_parser("config", help="print resolved configuration, secrets masked")
    config_cmd.add_argument("--section", default=None, help="limit output to one section")
    config_cmd.set_defaults(handler=_cmd_config)

    from tradefix_radio.cli import market_sim

    market_sim.register(sub)
    sub.choices["market-sim"].set_defaults(handler=_cmd_market_sim)

    version = sub.add_parser("version", help="print version information")
    version.set_defaults(handler=_cmd_version)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``tradefix`` script."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Logging is configured before any command runs, but kept quiet: the CLI's
    # output is its own report, and interleaved INFO lines would obscure it.
    try:
        mode = RunMode(args.mode) if args.mode else None
        settings = load_settings(config_file=args.config, mode=mode)
        configure_logging(
            settings.logging.model_copy(update={"level": "WARNING", "file_enabled": False}),
            service="cli",
        )
    except ConfigurationError:
        # The command handler re-loads and reports this properly; carry on so that
        # `tradefix config` can still explain what is wrong.
        pass

    handler: Callable[[argparse.Namespace], Coroutine[Any, Any, int]] = args.handler
    try:
        return asyncio.run(handler(args))
    except SystemExit as exc:
        return int(exc.code or 0)
    except KeyboardInterrupt:
        print("\n  interrupted", file=sys.stderr)
        return 130
    except RadioError as exc:
        print(f"\n  ERROR  {exc}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
