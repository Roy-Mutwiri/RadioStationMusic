"""Generation job repository — where §70's guarantees actually live (milestone 4.2).

§70 is about one thing: a crashed worker must not be able to lock a job forever. The answer is
a **lease** — an owner plus an expiry, written to the database — and the whole mechanism works
because nothing has to *detect* the crash. Detection is the part that fails.

Three correctness properties are enforced here rather than in the manager, because only the
database can enforce them across processes:

**Two workers cannot claim one job.** The claim is a conditional ``UPDATE ... WHERE state IN
(...) AND job_id = ...``, and the loser sees zero affected rows. A SELECT-then-UPDATE would
leave a window between the two statements wide enough for both workers to pass the check,
which on a 4-minute generation job is a wasted GPU-minute and a duplicate track.

**An expired lease cannot block a job.** :meth:`reclaim_expired` is a plain query over
``lease_expires_at``, so the only thing needed to recover a dead worker's job is for the clock
to keep ticking.

**Completion happens once.** Every state change goes through
:func:`~tradefix_radio.core.job_states.assert_job_transition` and is conditional on the
expected current state, so a late second completion from a worker that was presumed dead
affects zero rows and is reported as such rather than being applied.

The repository takes an injected clock for every time-dependent operation. Lease and retry
behaviour is most of this module, and testing it against wall time would mean either sleeping
for real or not testing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import and_, func, or_, select, update

from tradefix_radio.contracts.enums import GenerationPriority
from tradefix_radio.core.job_states import (
    CLAIMABLE_JOB_STATES,
    LEASED_JOB_STATES,
    JobFailureKind,
    JobState,
    assert_job_transition,
)
from tradefix_radio.persistence.models import GenerationJob
from tradefix_radio.persistence.repositories.base import Repository, affected_rows

_log = structlog.get_logger(__name__)

#: Priority order for claiming, lowest value claimed first.
_PRIORITY_ORDER: dict[GenerationPriority, int] = {
    GenerationPriority.CRITICAL: 0,
    GenerationPriority.HIGH: 1,
    GenerationPriority.NORMAL: 2,
    GenerationPriority.EXPERIMENTAL: 3,
}


@dataclass(frozen=True)
class JobRecord:
    """A generation job, detached from the session.

    Returned instead of the ORM row so a caller can hold a job across session boundaries —
    which the manager does constantly, since a generation call outlives any sane transaction.
    """

    job_id: str
    track_id: str
    provider: str
    priority: GenerationPriority
    state: JobState
    attempt: int
    max_attempts: int
    timeout_seconds: float
    lease_owner: str | None
    lease_expires_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    generation_seconds: float | None
    error_kind: JobFailureKind | None
    error_message: str | None
    request_payload: dict[str, Any] | None

    @property
    def attempts_remaining(self) -> int:
        return max(0, self.max_attempts - self.attempt)

    @property
    def can_retry(self) -> bool:
        return self.attempts_remaining > 0

    def lease_expired_at(self, now: datetime) -> bool:
        return self.lease_expires_at is not None and self.lease_expires_at <= now


def _to_record(row: GenerationJob) -> JobRecord:
    return JobRecord(
        job_id=row.job_id,
        track_id=row.track_id,
        provider=row.provider,
        priority=GenerationPriority(row.priority),
        state=JobState(row.state),
        attempt=row.attempt,
        max_attempts=row.max_attempts,
        timeout_seconds=row.timeout_seconds,
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        generation_seconds=row.generation_seconds,
        error_kind=JobFailureKind(row.error_kind) if row.error_kind else None,
        error_message=row.error_message,
        request_payload=row.request_payload,
    )


class GenerationJobRepository(Repository):
    """Persistent generation jobs with leases (§70)."""

    # -- creation ----------------------------------------------------------

    async def create(
        self,
        *,
        job_id: str,
        track_id: str,
        provider: str,
        priority: GenerationPriority,
        now: datetime,
        timeout_seconds: float,
        max_attempts: int,
        request_payload: dict[str, Any] | None = None,
        state: JobState = JobState.QUEUED,
    ) -> JobRecord:
        """Insert a job. ``job_id`` must be unique; collisions raise."""
        row = GenerationJob(
            job_id=job_id,
            track_id=track_id,
            provider=provider,
            priority=priority.value,
            state=state.value,
            attempt=1,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
            created_at=now,
            request_payload=request_payload,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_record(row)

    # -- lookup ------------------------------------------------------------

    async def get(self, job_id: str) -> JobRecord | None:
        row = await self._session.get(GenerationJob, job_id)
        return None if row is None else _to_record(row)

    async def for_track(self, track_id: str) -> list[JobRecord]:
        """Every job ever created for a track, oldest first.

        Plural because a track legitimately has several: a timeout, a retry, a success. A
        single-job assumption is what makes retry bookkeeping go wrong.
        """
        result = await self._session.execute(
            select(GenerationJob)
            .where(GenerationJob.track_id == track_id)
            .order_by(GenerationJob.created_at, GenerationJob.job_id)
        )
        return [_to_record(row) for row in result.scalars()]

    async def active_for_track(self, track_id: str) -> JobRecord | None:
        """The job currently doing work for a track, if any.

        Used for idempotency: asking to generate a track that already has live work must not
        create a second job competing for the same output path.
        """
        live = [
            JobState.PLANNED,
            JobState.QUEUED,
            JobState.LEASED,
            JobState.GENERATING,
            JobState.RETRY_PENDING,
        ]
        result = await self._session.execute(
            select(GenerationJob)
            .where(
                GenerationJob.track_id == track_id,
                GenerationJob.state.in_([s.value for s in live]),
            )
            .order_by(GenerationJob.created_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return None if row is None else _to_record(row)

    async def count_by_state(self) -> dict[JobState, int]:
        result = await self._session.execute(
            select(GenerationJob.state, func.count()).group_by(GenerationJob.state)
        )
        return {JobState(state): count for state, count in result.all()}

    async def in_state(self, *states: JobState, limit: int | None = None) -> list[JobRecord]:
        query = (
            select(GenerationJob)
            .where(GenerationJob.state.in_([s.value for s in states]))
            .order_by(GenerationJob.created_at)
        )
        if limit is not None:
            query = query.limit(limit)
        result = await self._session.execute(query)
        return [_to_record(row) for row in result.scalars()]

    async def leased_count(self) -> int:
        """How many jobs a worker currently holds. The concurrency limit's denominator."""
        result = await self._session.execute(
            select(func.count())
            .select_from(GenerationJob)
            .where(GenerationJob.state.in_([s.value for s in LEASED_JOB_STATES]))
        )
        return int(result.scalar_one())

    # -- claiming (§70) ----------------------------------------------------

    async def next_claimable(self, now: datetime) -> JobRecord | None:
        """The job a worker should take next, by priority then age.

        ``RETRY_PENDING`` is claimable only once its backoff has elapsed. The backoff deadline
        lives in ``lease_expires_at``, which is less an abuse of the column than a recognition
        that both mean "the time after which this job is available again" — and reusing it keeps
        the eligibility filter to one indexed predicate.

        Priority ordering happens in Python rather than SQL. The column stores the enum's
        string value, so ``ORDER BY priority`` would sort alphabetically and put ``critical``
        after ``experimental``. Safe to do in memory because the candidate set is small by
        construction: a backlog of thousands of *pending* generation jobs would mean the station
        is already failing in a way no ordering fixes.
        """
        result = await self._session.execute(
            select(GenerationJob).where(
                GenerationJob.state.in_([s.value for s in CLAIMABLE_JOB_STATES]),
                or_(
                    GenerationJob.state == JobState.QUEUED.value,
                    and_(
                        GenerationJob.state == JobState.RETRY_PENDING.value,
                        or_(
                            GenerationJob.lease_expires_at.is_(None),
                            GenerationJob.lease_expires_at <= now,
                        ),
                    ),
                ),
            )
        )
        candidates = [_to_record(row) for row in result.scalars()]
        if not candidates:
            return None
        candidates.sort(
            key=lambda job: (
                _PRIORITY_ORDER.get(job.priority, 99),
                job.created_at,
                job.job_id,
            )
        )
        return candidates[0]

    async def claim(
        self,
        job_id: str,
        *,
        owner: str,
        now: datetime,
        lease_seconds: float,
    ) -> JobRecord | None:
        """Take the lease on ``job_id``. ``None`` if another worker got there first.

        One conditional UPDATE. A SELECT-then-UPDATE leaves a window in which two workers both
        pass the check, and on a four-minute generation that costs a GPU-minute and produces a
        duplicate track — the exact failure §70 names.
        """
        expires = now + timedelta(seconds=lease_seconds)
        result = await self._session.execute(
            update(GenerationJob)
            .where(
                GenerationJob.job_id == job_id,
                GenerationJob.state.in_([s.value for s in CLAIMABLE_JOB_STATES]),
            )
            .values(
                state=JobState.LEASED.value,
                lease_owner=owner,
                lease_expires_at=expires,
            )
        )
        if affected_rows(result) == 0:
            return None
        await self._session.flush()
        self._session.expire_all()
        claimed = await self.get(job_id)
        _log.debug(
            "generation_job.claimed", job_id=job_id, owner=owner, expires_at=expires
        )
        return claimed

    async def renew_lease(
        self, job_id: str, *, owner: str, now: datetime, lease_seconds: float
    ) -> bool:
        """Extend a lease the caller still holds.

        Conditional on ownership: a worker whose lease already expired and was reclaimed must
        not be able to take it back, because another worker may already be generating.
        """
        result = await self._session.execute(
            update(GenerationJob)
            .where(
                GenerationJob.job_id == job_id,
                GenerationJob.lease_owner == owner,
                GenerationJob.state.in_([s.value for s in LEASED_JOB_STATES]),
            )
            .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
        )
        return affected_rows(result) > 0

    async def expired_leases(self, now: datetime) -> list[JobRecord]:
        result = await self._session.execute(
            select(GenerationJob).where(
                GenerationJob.state.in_([s.value for s in LEASED_JOB_STATES]),
                GenerationJob.lease_expires_at.is_not(None),
                GenerationJob.lease_expires_at <= now,
            )
        )
        return [_to_record(row) for row in result.scalars()]

    # -- state changes -----------------------------------------------------

    async def _transition(
        self,
        job_id: str,
        to_state: JobState,
        *,
        expected: frozenset[JobState],
        values: dict[str, Any],
        reason: str,
        owner: str | None = None,
    ) -> JobRecord | None:
        """Conditional state change. ``None`` when the job was not in an expected state.

        Returning ``None`` rather than raising is deliberate: "someone else already moved
        this" is a normal race in a leased-work system, and the caller's correct response is
        to drop the result, not to crash. The *illegal* case — an edge that does not exist in
        the state machine — still raises, because that is a programming error.
        """
        current = await self.get(job_id)
        if current is None:
            return None
        if current.state not in expected:
            _log.debug(
                "generation_job.transition_skipped",
                job_id=job_id,
                current=current.state.value,
                attempted=to_state.value,
                reason=reason,
            )
            return None
        assert_job_transition(current.state, to_state)

        conditions = [
            GenerationJob.job_id == job_id,
            GenerationJob.state.in_([s.value for s in expected]),
        ]
        if owner is not None:
            conditions.append(GenerationJob.lease_owner == owner)

        result = await self._session.execute(
            update(GenerationJob)
            .where(*conditions)
            .values(state=to_state.value, **values)
        )
        if affected_rows(result) == 0:
            return None
        await self._session.flush()
        self._session.expire_all()
        return await self.get(job_id)

    async def mark_generating(
        self, job_id: str, *, owner: str, now: datetime
    ) -> JobRecord | None:
        return await self._transition(
            job_id,
            JobState.GENERATING,
            expected=frozenset({JobState.LEASED}),
            values={"started_at": now},
            reason="generation started",
            owner=owner,
        )

    async def mark_generated(
        self,
        job_id: str,
        *,
        owner: str,
        now: datetime,
        generation_seconds: float,
        peak_vram_bytes: int | None = None,
    ) -> JobRecord | None:
        """Record success. ``None`` if the job was no longer this worker's to complete.

        That is the duplicate-completion guard: a worker whose lease expired mid-generation
        finds its completion rejected, because by then another worker may have produced the
        audio that is already queued.
        """
        return await self._transition(
            job_id,
            JobState.GENERATED,
            # ``GENERATING`` only. A job still merely ``LEASED`` has not started, so a
            # completion for it is by definition stale — and the state machine has no
            # ``LEASED -> GENERATED`` edge, so including it here raised an
            # ``IllegalTransitionError`` where the correct answer is a quiet rejection.
            expected=frozenset({JobState.GENERATING}),
            values={
                "finished_at": now,
                "generation_seconds": generation_seconds,
                "peak_vram_bytes": peak_vram_bytes,
                "lease_owner": None,
                "lease_expires_at": None,
            },
            reason="generation finished",
            owner=owner,
        )

    async def mark_retry_pending(
        self,
        job_id: str,
        *,
        now: datetime,
        retry_after_seconds: float,
        kind: JobFailureKind,
        message: str,
        owner: str | None = None,
    ) -> JobRecord | None:
        """Fail an attempt but keep the job, incrementing ``attempt``.

        The backoff deadline goes in ``lease_expires_at`` so that a job waiting one out is
        invisible to the claim query — which is what makes backoff survive a restart. Holding
        it in a worker's memory would lose it on exactly the restart §70 exists for.
        """
        current = await self.get(job_id)
        if current is None:
            return None
        return await self._transition(
            job_id,
            JobState.RETRY_PENDING,
            expected=frozenset({JobState.LEASED, JobState.GENERATING}),
            values={
                "attempt": current.attempt + 1,
                "error_kind": kind.value,
                "error_message": message[:2000],
                "lease_owner": None,
                "lease_expires_at": now + timedelta(seconds=retry_after_seconds),
            },
            reason=f"retry pending after {kind.value}",
            owner=owner,
        )

    async def requeue(self, job_id: str) -> JobRecord | None:
        """Move a job whose backoff has elapsed back into the claimable pool."""
        return await self._transition(
            job_id,
            JobState.QUEUED,
            expected=frozenset({JobState.RETRY_PENDING}),
            values={"lease_owner": None, "lease_expires_at": None},
            reason="backoff elapsed",
        )

    async def mark_failed(
        self,
        job_id: str,
        *,
        now: datetime,
        kind: JobFailureKind,
        message: str,
        owner: str | None = None,
    ) -> JobRecord | None:
        """Terminal failure: attempts exhausted, or a non-retryable kind."""
        return await self._transition(
            job_id,
            JobState.FAILED,
            expected=frozenset(
                {JobState.LEASED, JobState.GENERATING, JobState.RETRY_PENDING}
            ),
            values={
                "finished_at": now,
                "error_kind": kind.value,
                "error_message": message[:2000],
                "lease_owner": None,
                "lease_expires_at": None,
            },
            reason=f"failed: {kind.value}",
            owner=owner,
        )

    async def mark_cancelled(
        self, job_id: str, *, now: datetime, reason: str
    ) -> JobRecord | None:
        """§28's replan and an operator skip both land here."""
        return await self._transition(
            job_id,
            JobState.CANCELLED,
            expected=frozenset(
                {
                    JobState.PLANNED,
                    JobState.QUEUED,
                    JobState.LEASED,
                    JobState.GENERATING,
                    JobState.RETRY_PENDING,
                }
            ),
            values={
                "finished_at": now,
                "error_kind": JobFailureKind.CANCELLED.value,
                "error_message": reason[:2000],
                "lease_owner": None,
                "lease_expires_at": None,
            },
            reason="cancelled",
        )

    async def mark_abandoned(
        self, job_id: str, *, now: datetime, reason: str
    ) -> JobRecord | None:
        """Its worker vanished and nobody reclaimed the work in time.

        Distinct from ``FAILED`` because the operator response differs: a rising abandonment
        count is about worker health, while a rising failure count is about the model.
        """
        return await self._transition(
            job_id,
            JobState.ABANDONED,
            expected=frozenset(
                {JobState.LEASED, JobState.GENERATING, JobState.RETRY_PENDING}
            ),
            values={
                "finished_at": now,
                "error_kind": JobFailureKind.LEASE_EXPIRED.value,
                "error_message": reason[:2000],
                "lease_owner": None,
                "lease_expires_at": None,
            },
            reason="abandoned",
        )

    async def mark_queued(self, job_id: str) -> JobRecord | None:
        return await self._transition(
            job_id,
            JobState.QUEUED,
            expected=frozenset({JobState.PLANNED}),
            values={},
            reason="admitted",
        )

    # -- recovery (§75) ----------------------------------------------------

    async def reclaim_expired(
        self, now: datetime, *, retry_after_seconds: float
    ) -> list[JobRecord]:
        """Return expired-lease jobs to the pool, or abandon them if out of attempts.

        The whole of §70's crash story. Nothing detects the crash — the lease simply runs out,
        and this runs on a timer.

        A reclaimed job goes to ``RETRY_PENDING`` rather than straight to ``QUEUED`` so a
        worker that is crash-looping cannot spin on the same job: each reclaim costs it an
        attempt and a backoff.
        """
        reclaimed: list[JobRecord] = []
        for job in await self.expired_leases(now):
            if job.can_retry:
                updated = await self.mark_retry_pending(
                    job.job_id,
                    now=now,
                    retry_after_seconds=retry_after_seconds,
                    kind=JobFailureKind.LEASE_EXPIRED,
                    message=(
                        f"lease held by {job.lease_owner!r} expired at "
                        f"{job.lease_expires_at}"
                    ),
                )
            else:
                updated = await self.mark_abandoned(
                    job.job_id,
                    now=now,
                    reason=(
                        f"lease held by {job.lease_owner!r} expired with no attempts left"
                    ),
                )
            if updated is not None:
                reclaimed.append(updated)
                _log.warning(
                    "generation_job.lease_reclaimed",
                    job_id=job.job_id,
                    track_id=job.track_id,
                    previous_owner=job.lease_owner,
                    attempt=job.attempt,
                    new_state=updated.state.value,
                )
        return reclaimed

    async def release_backoffs(self, now: datetime) -> list[JobRecord]:
        """Move every ``RETRY_PENDING`` job whose backoff has elapsed back to ``QUEUED``."""
        result = await self._session.execute(
            select(GenerationJob).where(
                GenerationJob.state == JobState.RETRY_PENDING.value,
                or_(
                    GenerationJob.lease_expires_at.is_(None),
                    GenerationJob.lease_expires_at <= now,
                ),
            )
        )
        # Read every id out **before** requeuing any of them.
        #
        # ``requeue`` flushes, and a flush expires the loaded instances — so reading
        # ``row.job_id`` on the *next* iteration triggers a lazy reload, and a lazy reload from
        # a plain attribute access has no greenlet to run in. SQLAlchemy raises
        # ``MissingGreenlet`` and, because this runs inside the generation worker's claim
        # cycle, it fails before any job can be claimed: the station then stops generating
        # entirely while the scheduler keeps planning work nobody picks up.
        #
        # Only visible once a provider failure has put something into backoff, which is why a
        # soak with a healthy generator never saw it and Gate D did — 829 of them in one run.
        job_ids = [row.job_id for row in result.scalars().all()]
        released: list[JobRecord] = []
        for job_id in job_ids:
            updated = await self.requeue(job_id)
            if updated is not None:
                released.append(updated)
        return released

    async def classify_on_startup(self, now: datetime) -> dict[str, int]:
        """Decide what to do with jobs left behind by a crash (§75, milestone 4.9).

        Deliberately **not** "mark everything failed". That is the easy answer and it throws
        away work: a job that finished generating before the crash has audio on disk, and
        regenerating it costs a GPU-minute for nothing.

        * ``GENERATED`` — left alone. The audio exists.
        * ``LEASED`` / ``GENERATING`` — their owner is gone by definition, since a fresh
          process holds no leases. Expired ones are reclaimed; unexpired ones have their lease
          cleared immediately rather than waiting it out, because waiting would stall the queue
          for up to the lease duration for no reason.
        * ``RETRY_PENDING`` — left alone. Its backoff is in the database and still valid.
        * ``PLANNED`` / ``QUEUED`` — left alone. Nothing was started.

        Jobs whose track has no blueprint are **not** handled here, though they were in a
        first attempt. A job is planned before its track row is written in some callers,
        so "no blueprint yet" is a normal transient state rather than an orphan — and
        deciding otherwise would make this repository reach into the track schema to
        answer a question the station already knows the answer to. See
        `RadioStation.recover`, which abandons jobs for the specific slots it dropped.
        """
        counts = {"reclaimed": 0, "abandoned": 0, "cleared": 0}
        for job in await self.in_state(*LEASED_JOB_STATES):
            if job.can_retry:
                updated = await self.mark_retry_pending(
                    job.job_id,
                    now=now,
                    # Zero backoff: the previous owner cannot possibly still be working, so
                    # there is nothing to wait for.
                    retry_after_seconds=0.0,
                    kind=JobFailureKind.LEASE_EXPIRED,
                    message="worker process did not survive; lease cleared at startup",
                )
                if updated is not None:
                    counts["reclaimed"] += 1
                    counts["cleared"] += 1
            else:
                updated = await self.mark_abandoned(
                    job.job_id,
                    now=now,
                    reason="worker process did not survive and no attempts remain",
                )
                if updated is not None:
                    counts["abandoned"] += 1
        if any(counts.values()):
            _log.info("generation_job.startup_classification", **counts)
        return counts


__all__ = ["GenerationJobRepository", "JobRecord"]
