"""Filesystem layout and disk retention (§36, ADR-07).

The central idea, from ADR-07: **metadata is permanent, bytes are expendable.**
Audio files are addressed only through ``track_files`` rows, so retention can
reclaim gigabytes while every duplicate-prevention check keeps working forever.
"""

from tradefix_radio.storage.paths import FileRole, StoragePaths, validate_track_id
from tradefix_radio.storage.retention import (
    RetentionAction,
    RetentionCandidate,
    RetentionDecision,
    RetentionPlan,
    SweepResult,
    execute_plan,
    plan_retention,
)

__all__ = [
    "FileRole",
    "RetentionAction",
    "RetentionCandidate",
    "RetentionDecision",
    "RetentionPlan",
    "StoragePaths",
    "SweepResult",
    "execute_plan",
    "plan_retention",
    "validate_track_id",
]
