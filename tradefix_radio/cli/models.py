"""Model inventory and installation (§7.28).

    tradefix models status
    tradefix models install ace-step

§7.28's rule shapes both: *"Do not auto-download tens of GB silently. Provide explicit setup
command/instructions."* `status` only ever reads the disk. `install` performs the download —
that is what it is for — but states the size first, and refuses to proceed without
confirmation unless `--yes` is given.

The download itself is delegated to ACE-Step's own tooling rather than reimplemented here.
The station should not own a second opinion about where a checkpoint lives or what it is
called; getting that subtly wrong produces a 6 GB download into a directory the model never
reads from.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from typing import TYPE_CHECKING

from tradefix_radio.generation.ace_step.install import (
    APPROX_DOWNLOAD_GB,
    InstallationReport,
    inspect_installation,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradefix_radio.config.schema import AppSettings

__all__ = ["command", "register"]


def _render(report: InstallationReport) -> str:
    lines = ["", "  ACE-Step installation", ""]
    lines.append(f"    Directory:     {report.install_dir or '— not found'}")
    lines.append(f"    Valid checkout: {'yes' if report.install_valid else 'no'}")
    lines.append(f"    Environment:   {report.venv_python or '— not synced'}")
    if report.checkpoint_dirs:
        lines.append("    Checkpoints:")
        for path in report.checkpoint_dirs:
            lines.append(f"      {path}")
    lines.append("")
    lines.append("  Models")
    if not report.models:
        lines.append("    (none requested)")
    for model in report.models:
        if model.present:
            size = f"{model.size_gb:.2f} GB" if model.size_bytes else "size unknown"
            lines.append(f"    [present] {model.name:28s} {size}")
            lines.append(f"              {model.path}")
        else:
            lines.append(f"    [MISSING] {model.name}")
    lines.append("")
    for note in report.notes:
        lines.append(f"    note: {note}")
    lines.append(f"  {report.summary()}")
    if not report.ready:
        lines.append("")
        lines.append("  To fix:")
        for line in report.remediation().splitlines():
            lines.append(f"    {line}")
    lines.append("")
    return "\n".join(lines)


async def _status(settings: AppSettings) -> int:
    ace = settings.generation.ace_step
    report = await asyncio.to_thread(
        inspect_installation,
        worker_directory=ace.worker_directory,
        dit_model=ace.dit_model,
        lm_model=ace.lm_model,
    )
    print(_render(report))
    return 0 if report.ready else 1


async def _install(args: argparse.Namespace, settings: AppSettings) -> int:
    """Download the checkpoints, after saying how large they are.

    Delegates to ACE-Step's own loader inside its own interpreter. The station's 3.10 venv
    cannot import ACE-Step at all, so there is no alternative that does not reimplement the
    download — and reimplementing it is how a checkpoint ends up somewhere the model does
    not look.
    """
    ace = settings.generation.ace_step
    report = await asyncio.to_thread(
        inspect_installation,
        worker_directory=ace.worker_directory,
        dit_model=ace.dit_model,
        lm_model=ace.lm_model,
    )

    if report.install_dir is None or not report.install_valid:
        print(_render(report))
        print("  Cannot install: ACE-Step itself is not present. See the instructions above.")
        return 1
    if report.venv_python is None:
        print("  Cannot install: dependencies are not synced.")
        print(f"  Run `uv sync` in {report.install_dir} first.")
        return 1
    if report.ready:
        print(_render(report))
        print("  Nothing to do: every requested model is already on disk.")
        return 0

    print()
    print(f"  About to download: {', '.join(report.missing)}")
    print(f"  Approximate size:  {APPROX_DOWNLOAD_GB:.0f} GB")
    print(f"  Destination:       {report.install_dir / 'checkpoints'}")
    print()
    if not args.yes:
        # §7.28's point. A multi-gigabyte download on someone else's connection is their
        # decision, and a prompt is the cheapest possible way to let them make it.
        print("  Re-run with --yes to proceed. Nothing has been downloaded.")
        return 1

    if not sys.stdin.isatty() and not args.yes:  # pragma: no cover - defensive
        print("  Refusing to download non-interactively without --yes.")
        return 1

    uv = shutil.which("uv")
    if uv is None:
        print("  `uv` is not on PATH; it is how ACE-Step runs in its own environment.")
        print("  Install it: https://astral.sh/uv")
        return 1

    # ACE-Step downloads on first initialisation, so the honest way to trigger a download is
    # to start the service and initialise. Done in the foreground with output shown, because
    # a silent six-gigabyte wait is indistinguishable from a hang.
    print("  Starting ACE-Step to trigger its own checkpoint download. This will take a")
    print("  while, and progress is ACE-Step's own output.")
    print()
    process = await asyncio.create_subprocess_exec(
        uv,
        "run",
        "python",
        "-c",
        (
            "from acestep.handler import AceStepHandler;"
            f"h=AceStepHandler();h.initialize_service(project_root='.',"
            f"config_path='{ace.dit_model}',device='cpu')"
        ),
        cwd=str(report.install_dir),
        env={**os.environ},
    )
    code = await process.wait()
    if code != 0:
        print()
        print(f"  Download exited {code}. Start the service manually to see the full error:")
        print(f"    cd {report.install_dir} && uv run acestep-api")
        return 1

    after = await asyncio.to_thread(
        inspect_installation,
        worker_directory=ace.worker_directory,
        dit_model=ace.dit_model,
        lm_model=ace.lm_model,
    )
    print(_render(after))
    return 0 if after.ready else 1


async def command(args: argparse.Namespace, settings: AppSettings) -> int:
    action = getattr(args, "models_action", "status")
    if action == "install":
        return await _install(args, settings)
    return await _status(settings)


def register(subparsers: object) -> None:
    """Add the ``models`` subcommand."""
    parser = subparsers.add_parser(  # type: ignore[attr-defined]
        "models",
        help="inspect or install generation model checkpoints (§7.28)",
    )
    actions = parser.add_subparsers(dest="models_action")

    actions.add_parser("status", help="report what is installed. Downloads nothing.")

    install = actions.add_parser(
        "install", help="download missing checkpoints (several GB)"
    )
    install.add_argument(
        "target",
        nargs="?",
        default="ace-step",
        choices=["ace-step"],
        help="which model set to install",
    )
    install.add_argument(
        "--yes",
        action="store_true",
        help="confirm the download. Without it, the size is reported and nothing happens.",
    )
