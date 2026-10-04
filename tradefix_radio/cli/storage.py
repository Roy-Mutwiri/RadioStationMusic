"""`tradefix storage` — what the station owns, and what it could reclaim (§36, B5).

Four commands, all of which can be run on a live station:

* ``audit``  — reconcile the filesystem against ``track_files``. Read-only.
* ``stats``  — bytes by role and provenance, and what is reclaimable.
* ``sweep --dry-run`` — the retention plan, with a reason per file.
* ``sweep``  — the same plan, executed. Requires ``--yes``.

Dry-run is the default for ``sweep`` in everything but name: without ``--yes`` the command
refuses, prints the plan, and exits non-zero. A deletion tool whose dangerous mode is one
forgotten flag away is a deletion tool that will eventually be run by accident.
"""

from __future__ import annotations

import argparse
import shutil
from typing import TYPE_CHECKING

from tradefix_radio.storage.inventory import StorageInventory
from tradefix_radio.storage.paths import StoragePaths

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradefix_radio.config.schema import AppSettings
    from tradefix_radio.persistence.database import Database

__all__ = ["command", "register"]


def register(subparsers: object) -> None:
    """Add the ``storage`` subcommand."""
    parser = subparsers.add_parser(  # type: ignore[attr-defined]
        "storage", help="inspect and reclaim station audio storage"
    )
    actions = parser.add_subparsers(dest="storage_action")

    audit = actions.add_parser(
        "audit", help="reconcile files against the database (read-only)"
    )
    audit.add_argument(
        "--verify-hashes",
        action="store_true",
        help="re-read every file and compare checksums (slow)",
    )
    audit.add_argument(
        "--list-unknown",
        action="store_true",
        help="list files whose role could not be established",
    )

    actions.add_parser("stats", help="bytes by role and provenance")

    sweep = actions.add_parser("sweep", help="reclaim eligible audio")
    sweep.add_argument(
        "--dry-run",
        action="store_true",
        help="show the plan and delete nothing (the default behaviour without --yes)",
    )
    sweep.add_argument(
        "--yes",
        action="store_true",
        help="actually delete. Without this the command is a dry run and exits 1.",
    )
    sweep.add_argument("--limit", type=int, default=40, help="rows to print")


async def command(args: argparse.Namespace, settings: AppSettings) -> int:
    action = getattr(args, "storage_action", None)
    if action == "audit":
        return await _audit(settings, args)
    if action == "stats":
        return await _stats(settings)
    if action == "sweep":
        return await _sweep(settings, args)
    print("usage: tradefix storage {audit|stats|sweep}")
    return 2


async def _open(settings: AppSettings) -> Database:
    from tradefix_radio.persistence.database import Database  # noqa: PLC0415

    database = Database(settings.database)
    await database.connect()
    return database


async def _audit(settings: AppSettings, args: argparse.Namespace) -> int:
    database = await _open(settings)
    inventory = StorageInventory(StoragePaths(settings.paths))
    try:
        async with database.read_session() as session:
            report = await inventory.scan(session, verify_hashes=args.verify_hashes)
    finally:
        await database.disconnect()

    print("STORAGE AUDIT")
    for root in report.scanned_roots:
        print(f"  root  {root}")
    print()
    print(f"  files            {report.total_files}")
    print(f"  bytes            {report.total_bytes / 1e9:.2f} GB")
    print(f"  owned (has row)  {report.owned_bytes / 1e9:.2f} GB")
    print(f"  unowned          {report.unowned_bytes / 1e9:.2f} GB")
    print()
    print("  by classification")
    for name, (count, byts) in report.by_classification().items():
        print(f"    {name:16} {count:5} files  {byts / 1e9:7.2f} GB")

    findings = report.findings()
    print()
    print("  findings")
    if not findings:
        print("    none")
    for name, count in findings.items():
        print(f"    {name:26} {count}")

    if report.missing_rows:
        print()
        print("  ROW_WITHOUT_FILE — the database promises audio that is not there:")
        for file_id, track_id, role, path in report.missing_rows[:20]:
            print(f"    [{file_id}] {track_id} {role.value}: {path}")

    if args.list_unknown:
        unknown = [
            entry
            for entry in report.files
            if entry.classification.value == "unknown_legacy"
        ]
        print()
        print(f"  UNKNOWN_LEGACY ({len(unknown)}) — never auto-deleted:")
        for entry in unknown[:60]:
            print(f"    {entry.path.name:44} {entry.size_bytes / 1e6:8.1f} MB  {entry.evidence}")

    if report.non_audio_strays:
        print()
        print(f"  non-audio files in audio roots: {len(report.non_audio_strays)}")

    print()
    print(f"  verdict: {'healthy' if report.is_healthy else 'ATTENTION REQUIRED'}")
    # A legacy corpus is not a failure; a row promising absent audio is.
    return 0 if report.is_healthy else 1


async def _stats(settings: AppSettings) -> int:
    from sqlalchemy import func, select  # noqa: PLC0415

    from tradefix_radio.persistence.models import Track, TrackFile  # noqa: PLC0415

    database = await _open(settings)
    try:
        async with database.read_session() as session:
            by_role = (
                await session.execute(
                    select(
                        TrackFile.role,
                        func.count(),
                        func.coalesce(func.sum(TrackFile.size_bytes), 0),
                    )
                    .where(TrackFile.deleted_at.is_(None))
                    .group_by(TrackFile.role)
                )
            ).all()
            by_provenance = (
                await session.execute(
                    select(
                        Track.provenance,
                        func.count(),
                        func.coalesce(func.sum(TrackFile.size_bytes), 0),
                    )
                    .join(TrackFile, TrackFile.track_id == Track.track_id)
                    .where(TrackFile.deleted_at.is_(None))
                    .group_by(Track.provenance)
                )
            ).all()
            reclaimed = (
                await session.execute(
                    select(
                        func.count(),
                        func.coalesce(func.sum(TrackFile.size_bytes), 0),
                    ).where(TrackFile.deleted_at.is_not(None))
                )
            ).one()
    finally:
        await database.disconnect()

    print("STORAGE STATS (from track_files; run `storage audit` for the filesystem)")
    print()
    print("  live audio by role")
    if not by_role:
        print("    no registered files")
    for role, count, byts in by_role:
        print(f"    {role:16} {count:5} files  {byts / 1e9:7.2f} GB")
    print()
    print("  live audio by provenance")
    for provenance, count, byts in by_provenance:
        print(f"    {provenance or 'unknown':16} {count:5} files  {byts / 1e9:7.2f} GB")
    print()
    print(f"  reclaimed already   {reclaimed[0]} rows  {reclaimed[1] / 1e9:.2f} GB")

    usage = shutil.disk_usage(settings.paths.generated_dir)
    print()
    print(f"  disk free           {usage.free / 1e9:.1f} GB of {usage.total / 1e9:.1f} GB")
    print(f"  retention floor     {settings.retention.min_free_gb:.1f} GB")
    return 0


async def _sweep(settings: AppSettings, args: argparse.Namespace) -> int:
    from datetime import datetime  # noqa: PLC0415

    from tradefix_radio.core.clock import UTC  # noqa: PLC0415
    from tradefix_radio.persistence.repositories.files import (  # noqa: PLC0415
        TrackFileRepository,
    )
    from tradefix_radio.storage.retention import (  # noqa: PLC0415
        execute_plan,
        plan_retention,
    )

    dry_run = args.dry_run or not args.yes
    database = await _open(settings)
    try:
        async with database.read_session() as session:
            candidates = await TrackFileRepository(session).retention_candidates()
    finally:
        await database.disconnect()

    free_bytes = shutil.disk_usage(settings.paths.generated_dir).free
    plan = plan_retention(
        candidates, settings.retention, now=datetime.now(UTC), free_bytes=free_bytes
    )

    print(f"RETENTION {'DRY RUN' if dry_run else 'SWEEP'}")
    print(f"  candidates        {len(candidates)}")
    print(f"  would delete      {len(plan.deletions)}")
    print(f"  reclaimable       {plan.reclaimable_bytes / 1e9:.2f} GB")
    print(f"  free now          {free_bytes / 1e9:.1f} GB")
    print()
    if plan.deletions:
        print("  eligible files, and why:")
        for decision in plan.deletions[: args.limit]:
            candidate = decision.candidate
            print(
                f"    {candidate.track_id:22} {candidate.role.value:14} "
                f"{candidate.size_bytes / 1e6:8.1f} MB  {decision.reason}"
            )
        if len(plan.deletions) > args.limit:
            print(f"    ... and {len(plan.deletions) - args.limit} more")
    else:
        print("  nothing is eligible.")

    if dry_run:
        print()
        print("  nothing was deleted. Pass --yes to execute this plan.")
        # Non-zero without --yes so a script cannot mistake a dry run for a sweep.
        return 0 if args.dry_run else 1

    result = execute_plan(plan)
    database = await _open(settings)
    try:
        async with database.session() as session:
            await TrackFileRepository(session).mark_reclaimed(
                [d.candidate.file_id for d in plan.deletions if d.candidate.file_id],
                now=datetime.now(UTC),
            )
    finally:
        await database.disconnect()

    print()
    print(f"  deleted           {result.files_deleted}")
    print(f"  already gone      {result.files_missing}")
    print(f"  reclaimed         {result.bytes_reclaimed / 1e9:.2f} GB")
    for error in result.errors[:10]:
        print(f"  error: {error}")
    return 0 if not result.errors else 1
