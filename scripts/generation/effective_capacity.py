"""Effective **approved** capacity (§93), split by profile and by vocal/instrumental.

Raw throughput is the wrong number. A generator that renders four times faster than real
time and has three quarters of its output rejected by Phase 6 is not a station that can stay
on air — it is a station that goes silent slightly later. The figure that matters is:

    seconds of audio that reached a playable state
    -------------------------------------------------------------
    seconds of GPU time spent getting there, rejections included

Anything above 1.0 means the station generates faster than it broadcasts. The split by
profile exists because B3 introduced a `vocal` profile that costs ~2.2x the GPU time of
`balanced`, and the blended figure is the only one that answers "can the station run".

Usage:  python scripts/generation/effective_capacity.py [--db PATH] [--since ISO8601]
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

#: States from which a track can actually be broadcast, or already has been.
PLAYABLE = ("ready", "playing", "played", "archived")


@dataclass
class Bucket:
    """One row of the report."""

    label: str
    approved_audio_seconds: float = 0.0
    gpu_seconds: float = 0.0
    approved: int = 0
    rejected: int = 0
    #: GPU time spent on attempts that produced nothing playable.
    wasted_seconds: float = 0.0
    durations: list[float] = field(default_factory=list)

    @property
    def capacity(self) -> float:
        if self.gpu_seconds <= 0:
            return 0.0
        return self.approved_audio_seconds / self.gpu_seconds

    @property
    def approval_rate(self) -> float:
        total = self.approved + self.rejected
        return 0.0 if total == 0 else self.approved / total


def collect(connection: sqlite3.Connection, since: str | None) -> dict[str, Bucket]:
    connection.row_factory = sqlite3.Row
    clause = "" if since is None else "AND t.created_at >= :since"
    rows = connection.execute(
        f"""
        SELECT
            t.track_id,
            t.state,
            t.duration_seconds,
            t.is_instrumental,
            s.profile          AS profile,
            s.instrumental     AS submitted_instrumental,
            SUM(COALESCE(j.generation_seconds, 0.0)) AS gpu_seconds
        FROM tracks t
        LEFT JOIN generation_jobs j ON j.track_id = t.track_id
        LEFT JOIN provider_submissions s ON s.id = (
            SELECT id FROM provider_submissions
            WHERE track_id = t.track_id ORDER BY id DESC LIMIT 1
        )
        WHERE j.generation_seconds IS NOT NULL {clause}
        GROUP BY t.track_id
        """,
        {"since": since} if since else {},
    ).fetchall()

    buckets: dict[str, Bucket] = defaultdict(lambda: Bucket(label=""))

    def add(label: str, row: sqlite3.Row, *, playable: bool) -> None:
        bucket = buckets[label]
        bucket.label = label
        gpu = float(row["gpu_seconds"] or 0.0)
        bucket.gpu_seconds += gpu
        if playable:
            duration = float(row["duration_seconds"] or 0.0)
            bucket.approved_audio_seconds += duration
            bucket.approved += 1
            bucket.durations.append(duration)
        else:
            bucket.rejected += 1
            bucket.wasted_seconds += gpu

    for row in rows:
        playable = (row["state"] or "").lower() in PLAYABLE
        # `submitted_instrumental` is what the provider was actually asked for, which is the
        # honest split: a vocal blueprint downgraded to an instrumental cost instrumental
        # money, not vocal money, and filing it under "vocal" would overstate vocal cost.
        submitted = row["submitted_instrumental"]
        voiced = (
            "unknown"
            if submitted is None
            else ("instrumental" if submitted else "vocal")
        )
        profile = row["profile"] or "unrecorded"

        add("ALL", row, playable=playable)
        add(f"voice:{voiced}", row, playable=playable)
        add(f"profile:{profile}", row, playable=playable)

    return dict(buckets)


def report(buckets: dict[str, Bucket]) -> str:
    order = sorted(
        buckets.values(),
        key=lambda b: (b.label != "ALL", b.label),
    )
    lines = [
        "| bucket | approved | rejected | approval | audio (s) | GPU (s) | wasted (s) | capacity |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for bucket in order:
        lines.append(
            f"| {bucket.label} | {bucket.approved} | {bucket.rejected} | "
            f"{bucket.approval_rate:.0%} | {bucket.approved_audio_seconds:.0f} | "
            f"{bucket.gpu_seconds:.0f} | {bucket.wasted_seconds:.0f} | "
            f"**{bucket.capacity:.2f}x** |"
        )

    overall = buckets.get("ALL")
    if overall is not None:
        lines.append("")
        verdict = (
            "above 1.0x — the station generates faster than it broadcasts"
            if overall.capacity > 1.0
            else "BELOW 1.0x — the station cannot sustain its own schedule"
        )
        lines.append(f"Effective approved capacity: **{overall.capacity:.2f}x** ({verdict}).")
        if overall.gpu_seconds > 0:
            share = overall.wasted_seconds / overall.gpu_seconds
            lines.append(
                f"{share:.0%} of GPU time went to tracks that never became playable."
            )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/tradefix.db")
    parser.add_argument(
        "--since",
        default=None,
        help="only count tracks created at or after this timestamp (ISO 8601)",
    )
    args = parser.parse_args()

    connection = sqlite3.connect(args.db)
    try:
        buckets = collect(connection, args.since)
    finally:
        connection.close()

    if not buckets:
        print("No generated tracks with recorded GPU time in range.")
        return
    print(report(buckets))


if __name__ == "__main__":
    main()
