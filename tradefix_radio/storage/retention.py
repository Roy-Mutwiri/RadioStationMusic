"""Disk retention planning (§36, ADR-07).

ADR-07 establishes why this is a Phase 1 concern: at ~17 tracks/hour and ~37 MB
per WAV, unmanaged generation fills a 193 GB volume in under two weeks — inside
the "days or weeks without human intervention" target. Retention is therefore a
correctness requirement.

The design separates **planning** from **execution**:

:func:`plan_retention`
    A pure function over :class:`RetentionCandidate` values. No I/O, no database,
    no clock. Every §36 rule is expressible as a predicate, so the dangerous part
    — deciding what to delete — is exhaustively testable without creating files.

:class:`RetentionExecutor`
    Performs the deletions the plan describes, marks rows, and reports what
    happened. Dumb on purpose: it makes no decisions.

What is **never** deleted, enforced as explicit guards rather than emergent
behaviour:

* anything whose track is not in a terminal, already-aired state;
* anything referenced by the live queue;
* anything marked ``retain_forever`` (the Tier 2 emergency reserve, §33);
* station identifier clips;
* the ``keep_recent_masters`` most recent masters, which form a listening archive;
* anything already reclaimed.

Metadata, blueprints, lyrics, fingerprints and seeds are *never* candidates —
they live in different tables and the planner cannot see them. That is the
structural guarantee behind §36's "preserve hash / fingerprint / embedding /
metadata / blueprint / lyrics".
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import structlog

from tradefix_radio.config.schema import RetentionSettings
from tradefix_radio.contracts.enums import TrackProvenance
from tradefix_radio.core.state_machine import TrackState
from tradefix_radio.storage.paths import FileRole

_log = structlog.get_logger(__name__)

#: States in which a track's audio may be reclaimed. Everything else is either
#: still needed for broadcast or still being worked on.
RECLAIMABLE_TRACK_STATES: frozenset[TrackState] = frozenset(
    {TrackState.PLAYED, TrackState.REJECTED, TrackState.FAILED, TrackState.CANCELLED}
)


class RetentionAction(str, enum.Enum):
    """Why a candidate was or was not selected. Surfaced in the sweep report."""

    DELETE_AGED_MASTER = "delete_aged_master"
    DELETE_RAW = "delete_raw"
    DELETE_REJECTED = "delete_rejected"
    DELETE_PRESSURE = "delete_pressure"

    KEEP_NOT_TERMINAL = "keep_not_terminal"
    KEEP_QUEUED = "keep_queued"
    KEEP_PROTECTED = "keep_protected"
    KEEP_STATION_ID = "keep_station_id"
    KEEP_RECENT_ARCHIVE = "keep_recent_archive"
    KEEP_WITHIN_RETENTION = "keep_within_retention"
    KEEP_ALREADY_DELETED = "keep_already_deleted"
    KEEP_NEVER_PLAYED = "keep_never_played"

    @property
    def deletes(self) -> bool:
        return self.value.startswith("delete_")


@dataclass(frozen=True)
class RetentionCandidate:
    """One file the sweeper could consider, with everything needed to judge it.

    A flat value object rather than an ORM row so the planner has no database
    dependency and tests can construct adversarial cases directly.
    """

    file_id: int
    track_id: str
    role: FileRole
    path: Path
    size_bytes: int
    created_at: datetime
    track_state: TrackState
    #: ``None`` when the track never aired.
    last_played_at: datetime | None = None
    #: Tier 2 reserve and anything an operator pinned.
    retain_forever: bool = False
    #: Present in the live queue right now.
    in_queue: bool = False
    #: Already reclaimed; the row survives but the bytes are gone.
    already_deleted: bool = False
    #: Path lies under a protected directory (emergency tree).
    protected_location: bool = False
    #: Provenance of the owning track (B5). ``unknown`` is never auto-deleted.
    provenance: str = "unknown"


@dataclass(frozen=True)
class RetentionDecision:
    """The planner's verdict on one candidate."""

    candidate: RetentionCandidate
    action: RetentionAction
    reason: str

    @property
    def deletes(self) -> bool:
        return self.action.deletes


@dataclass
class RetentionPlan:
    """A complete sweep plan."""

    decisions: list[RetentionDecision] = field(default_factory=list)
    free_bytes_before: int = 0
    required_bytes: int = 0

    @property
    def deletions(self) -> list[RetentionDecision]:
        return [d for d in self.decisions if d.deletes]

    @property
    def retained(self) -> list[RetentionDecision]:
        return [d for d in self.decisions if not d.deletes]

    @property
    def reclaimable_bytes(self) -> int:
        return sum(d.candidate.size_bytes for d in self.deletions)

    @property
    def satisfies_pressure(self) -> bool:
        """Whether executing this plan reaches the configured free-space floor."""
        if self.required_bytes <= 0:
            return True
        return self.free_bytes_before + self.reclaimable_bytes >= self.required_bytes

    def summary(self) -> dict[str, int]:
        """Counts per action, for the sweep log line and the §49 page."""
        counts: dict[str, int] = {}
        for decision in self.decisions:
            counts[decision.action.value] = counts.get(decision.action.value, 0) + 1
        return counts


def _is_protected(candidate: RetentionCandidate) -> RetentionDecision | None:
    """Hard guards, evaluated before any age or pressure logic.

    Ordering matters: these must be checked first so that no amount of disk
    pressure can argue its way past them. §36 is explicit that currently queued
    and emergency tracks are never deleted, and disk pressure is exactly the
    situation in which a naive implementation would start deleting them.
    """
    if candidate.already_deleted:
        return RetentionDecision(
            candidate, RetentionAction.KEEP_ALREADY_DELETED, "bytes already reclaimed"
        )
    if candidate.role is FileRole.STATION_ID:
        return RetentionDecision(
            candidate,
            RetentionAction.KEEP_STATION_ID,
            "station identifier clips are part of the station's identity (§31)",
        )
    if candidate.retain_forever or candidate.protected_location:
        return RetentionDecision(
            candidate,
            RetentionAction.KEEP_PROTECTED,
            "marked retain_forever or stored in the emergency tree (§33)",
        )
    if candidate.in_queue:
        return RetentionDecision(
            candidate,
            RetentionAction.KEEP_QUEUED,
            "referenced by the live queue; deleting it would cause silence",
        )
    if candidate.track_state not in RECLAIMABLE_TRACK_STATES:
        return RetentionDecision(
            candidate,
            RetentionAction.KEEP_NOT_TERMINAL,
            f"track is {candidate.track_state.value}; audio may still be needed",
        )
    return None


def _allowance_days(
    candidate: RetentionCandidate, settings: RetentionSettings
) -> float | None:
    """How many days this file is kept, or ``None`` when no policy names it.

    The *tighter* of the provenance and role allowances. They answer different
    questions — "how precious is the track" and "how redundant is this particular file" —
    and a raw render of a production track is still a raw render.
    """
    role_days = settings.role_retention_days.get(candidate.role.value)
    provenance_days = settings.provenance_retention_days.get(candidate.provenance)

    if (
        candidate.role is FileRole.MASTER
        and candidate.provenance == TrackProvenance.PRODUCTION_RADIO.value
    ):
        # A production master is the listening archive, and the archive already has an
        # authority: `keep_recent_masters` plus `audio_retention_days` below. Adding a
        # second number here would be the two-authorities mistake B5 exists to remove,
        # so the provenance allowance deliberately abstains for this one combination.
        return role_days

    present = [value for value in (role_days, provenance_days) if value is not None]
    return min(present) if present else None


def plan_retention(
    candidates: list[RetentionCandidate],
    settings: RetentionSettings,
    *,
    now: datetime,
    free_bytes: int = 0,
) -> RetentionPlan:
    """Decide which files may be reclaimed.

    Pure: no I/O, no global state, deterministic for a given input. The three
    passes are deliberately ordered cheapest-and-safest first.

    Parameters
    ----------
    candidates:
        Every file the sweeper is aware of.
    settings:
        Retention policy.
    now:
        Reference time, timezone-aware. Injected so the accelerated endurance
        clock (§64) can exercise multi-week retention in seconds.
    free_bytes:
        Current free space, used only to decide whether the extra
        disk-pressure pass runs.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    required_bytes = int(settings.min_free_gb * 1_000_000_000)
    plan = RetentionPlan(free_bytes_before=free_bytes, required_bytes=required_bytes)

    if not settings.enabled:
        plan.decisions = [
            RetentionDecision(
                candidate,
                RetentionAction.KEEP_WITHIN_RETENTION,
                "retention is disabled in configuration",
            )
            for candidate in candidates
        ]
        return plan

    cutoff = now - timedelta(days=settings.audio_retention_days)

    # The listening archive: the N most recent masters are kept regardless of age.
    # Ranked by air date, falling back to creation date for tracks that never
    # aired, so the archive reflects what the station actually broadcast.
    masters = [c for c in candidates if c.role is FileRole.MASTER]
    masters_by_recency = sorted(
        masters,
        key=lambda c: (c.last_played_at or c.created_at),
        reverse=True,
    )
    archive_ids = {c.file_id for c in masters_by_recency[: settings.keep_recent_masters]}

    deferred: list[RetentionCandidate] = []

    for candidate in candidates:
        guard = _is_protected(candidate)
        if guard is not None:
            plan.decisions.append(guard)
            continue

        # Provenance decides how long anything is kept, and an unprovable provenance
        # decides nothing at all (B5). A legacy asset from before provenance existed is
        # ambiguous rather than expendable, and the station has 8.68 GB of exactly that.
        if candidate.provenance == TrackProvenance.UNKNOWN.value:
            plan.decisions.append(
                RetentionDecision(
                    candidate,
                    RetentionAction.KEEP_WITHIN_RETENTION,
                    "provenance is unknown; conservative policy keeps it",
                )
            )
            continue

        allowance = _allowance_days(candidate, settings)
        if allowance is not None:
            age_days = (now - candidate.created_at).total_seconds() / 86_400.0
            if age_days < allowance:
                plan.decisions.append(
                    RetentionDecision(
                        candidate,
                        RetentionAction.KEEP_WITHIN_RETENTION,
                        f"{age_days:.1f}d old, inside the {allowance:.1f}d allowance for "
                        f"{candidate.provenance}/{candidate.role.value}",
                    )
                )
                continue

        # Raw provider output is redundant the moment a master exists, and is the
        # cheapest, safest space to reclaim. Taken first for that reason.
        if candidate.role is FileRole.RAW_GENERATION:
            plan.decisions.append(
                RetentionDecision(
                    candidate,
                    RetentionAction.DELETE_RAW,
                    "raw provider output is superseded by the master",
                )
            )
            continue

        # Rejected and failed candidates never air. Their fingerprints and
        # blueprints are retained in other tables, so the §48 explanation and all
        # future duplicate checks survive the audio going away.
        if candidate.track_state in {
            TrackState.REJECTED,
            TrackState.FAILED,
            TrackState.CANCELLED,
        }:
            plan.decisions.append(
                RetentionDecision(
                    candidate,
                    RetentionAction.DELETE_REJECTED,
                    f"track is {candidate.track_state.value} and will never air; "
                    "fingerprint and blueprint are retained separately",
                )
            )
            continue

        if candidate.file_id in archive_ids:
            plan.decisions.append(
                RetentionDecision(
                    candidate,
                    RetentionAction.KEEP_RECENT_ARCHIVE,
                    f"within the {settings.keep_recent_masters} most recent masters",
                )
            )
            continue

        # A PLAYED track with no air date is contradictory; treat the absence as a
        # reason to keep rather than guessing an age from creation time.
        reference = candidate.last_played_at
        if reference is None:
            plan.decisions.append(
                RetentionDecision(
                    candidate,
                    RetentionAction.KEEP_NEVER_PLAYED,
                    "no recorded air date; refusing to infer an age",
                )
            )
            continue

        if reference < cutoff:
            plan.decisions.append(
                RetentionDecision(
                    candidate,
                    RetentionAction.DELETE_AGED_MASTER,
                    f"last aired {(now - reference).days}d ago, beyond the "
                    f"{settings.audio_retention_days}d retention window",
                )
            )
            continue

        deferred.append(candidate)

    # Third pass: only if we are still below the free-space floor do we reach into
    # masters that are inside the retention window. Oldest air date first.
    projected_free = free_bytes + plan.reclaimable_bytes
    under_pressure = free_bytes > 0 and projected_free < required_bytes

    for candidate in sorted(deferred, key=lambda c: c.last_played_at or c.created_at):
        if under_pressure and projected_free < required_bytes:
            plan.decisions.append(
                RetentionDecision(
                    candidate,
                    RetentionAction.DELETE_PRESSURE,
                    "inside the retention window but reclaimed to restore the "
                    f"{settings.min_free_gb:.0f} GB free-space floor",
                )
            )
            projected_free += candidate.size_bytes
            continue
        plan.decisions.append(
            RetentionDecision(
                candidate,
                RetentionAction.KEEP_WITHIN_RETENTION,
                f"aired within the {settings.audio_retention_days}d retention window",
            )
        )

    return plan


@dataclass
class SweepResult:
    """What a sweep actually accomplished."""

    files_deleted: int = 0
    bytes_reclaimed: int = 0
    files_missing: int = 0
    files_failed: int = 0
    errors: list[str] = field(default_factory=list)


def execute_plan(plan: RetentionPlan, *, dry_run: bool = False) -> SweepResult:
    """Delete the files a plan selected.

    Makes no decisions. A file that is already gone counts as ``files_missing``
    rather than an error: the row should still be marked reclaimed, because the
    desired end state has been reached by other means (an operator cleaned up, a
    previous sweep was interrupted after unlinking but before committing).
    """
    result = SweepResult()
    for decision in plan.deletions:
        candidate = decision.candidate
        if dry_run:
            result.files_deleted += 1
            result.bytes_reclaimed += candidate.size_bytes
            continue
        try:
            candidate.path.unlink()
        except FileNotFoundError:
            result.files_missing += 1
            _log.info(
                "retention.file_already_absent",
                track_id=candidate.track_id,
                path=str(candidate.path),
            )
        except OSError as exc:
            result.files_failed += 1
            message = f"{candidate.path}: {exc.strerror or exc}"
            result.errors.append(message)
            _log.warning(
                "retention.delete_failed",
                track_id=candidate.track_id,
                path=str(candidate.path),
                error=str(exc),
            )
        else:
            result.files_deleted += 1
            result.bytes_reclaimed += candidate.size_bytes
    _log.info(
        "retention.sweep_complete",
        dry_run=dry_run,
        files_deleted=result.files_deleted,
        bytes_reclaimed=result.bytes_reclaimed,
        files_missing=result.files_missing,
        files_failed=result.files_failed,
        **plan.summary(),
    )
    return result


__all__ = [
    "RECLAIMABLE_TRACK_STATES",
    "RetentionAction",
    "RetentionCandidate",
    "RetentionDecision",
    "RetentionPlan",
    "SweepResult",
    "execute_plan",
    "plan_retention",
]
