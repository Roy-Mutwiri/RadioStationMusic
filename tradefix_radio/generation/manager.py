"""GenerationManager — leases, retries, timeouts, cancellation (§70, §93, §94, milestone 4.2).

The component that turns "a provider that can make one track" into "a station that keeps
making tracks for a week". Everything difficult about that is failure handling, so that is what
this module mostly is.

**Where the guarantees live.** The ones that must hold across processes are enforced by
:class:`~tradefix_radio.persistence.repositories.jobs.GenerationJobRepository` in SQL —
one worker per job, no permanently-blocked job, one completion per job. This class owns the
ones that are about *behaviour*: how long to wait, how many times to try, when to stop being
adventurous, and how to shut down without losing work.

**A generation failure must never reach playout.** §86 states it and §26 depends on it. The
mechanism is that this class returns outcomes rather than raising: every provider exception is
classified (:class:`~tradefix_radio.core.job_states.JobFailureKind`), recorded, and turned into
a retry or a terminal failure. The scheduler reads the queue; it never awaits a provider.

**Timeouts are enforced here, not trusted to the provider.** A provider that hangs is the
failure mode that matters, and a hung provider by definition does not honour its own deadline.
``asyncio.wait_for`` around the call is the only thing that actually bounds it.

**Everything time-dependent takes the injected clock.** Lease expiry, retry backoff and the
timeout all run on it, so §64's accelerated runs exercise the real logic and the tests need no
sleeping.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

import structlog

from tradefix_radio.config.schema import GenerationSettings
from tradefix_radio.contracts.enums import GenerationPriority
from tradefix_radio.contracts.lyrics import LyricsV1
from tradefix_radio.contracts.music import MusicBlueprintV1
from tradefix_radio.core.clock import Clock, SystemClock
from tradefix_radio.core.errors import (
    AudioError,
    GenerationCancelledError,
    GenerationError,
    GenerationTimeoutError,
    GpuResourceError,
    ProviderUnavailableError,
    QualityControlError,
)
from tradefix_radio.core.job_states import JobFailureKind, JobState
from tradefix_radio.generation.capacity import CapacitySnapshot, CapacityTracker
from tradefix_radio.generation.provider import (
    GenerationRequest,
    GenerationResult,
    MusicGenerationProvider,
)
from tradefix_radio.persistence.repositories.jobs import GenerationJobRepository, JobRecord

_log = structlog.get_logger(__name__)

_T = TypeVar("_T")

#: One repository operation, to be run inside a transaction.
JobWork = Callable[[GenerationJobRepository], Awaitable[_T]]


class JobUnitOfWork(Protocol):
    """Opens a session, hands a repository to the callback, and commits on success.

    The manager is given this rather than a session. A generation call takes minutes, and holding
    a database transaction open across it would pin a connection and block the scheduler — so
    every state change is its own short transaction instead.

    A Protocol with a generic ``__call__`` rather than a ``Callable`` alias, so the callback's
    return type survives the round trip. As ``Callable[[JobWork], Awaitable[object]]`` every call
    site got back ``object``, which defeats the type checker at exactly the places where "did
    this transition actually apply?" is the question being asked.
    """

    def __call__(self, work: JobWork[_T]) -> Awaitable[_T]: ...


#: Fraction of its lease a worker lets elapse before renewing.
#:
#: Renewing at the last moment loses the race on any scheduling hiccup; renewing constantly is
#: pointless write traffic. Two-thirds leaves a full third of the lease as margin.
LEASE_RENEW_FRACTION = 0.66

#: Hard ceiling on retry backoff, however many attempts have failed.
#:
#: Without it, five attempts at a 2x multiplier from 5 seconds reaches 80 seconds and a sixth
#: would reach 160 — longer than the §26 minimum buffer can absorb, so the backoff itself would
#: cause the starvation it is meant to avoid.
MAX_BACKOFF_SECONDS = 120.0

#: Extra backoff multiplier for failures where an immediate retry makes things worse.
RESOURCE_BACKOFF_MULTIPLIER = 4.0


@dataclass(frozen=True)
class GenerationOutcome:
    """What happened to one job. Returned, never raised.

    A generation failure that propagated as an exception would reach whatever awaited it, and
    in a single-process deployment that is the playout loop. §86 forbids a model crash stopping
    the radio, so the type system is used to make that structurally impossible: there is no
    exception to catch because none is thrown.
    """

    job_id: str
    track_id: str
    state: JobState
    result: GenerationResult | None = None
    failure_kind: JobFailureKind | None = None
    detail: str = ""
    attempt: int = 1
    wall_seconds: float = 0.0
    retry_after_seconds: float | None = None

    @property
    def succeeded(self) -> bool:
        return self.state is JobState.GENERATED and self.result is not None

    @property
    def will_retry(self) -> bool:
        return self.state is JobState.RETRY_PENDING

    @property
    def is_terminal_failure(self) -> bool:
        return self.state in {JobState.FAILED, JobState.ABANDONED}


@dataclass
class ManagerStats:
    """Counters for the §101 report and the §56 metrics."""

    planned: int = 0
    started: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    abandoned: int = 0
    timeouts: int = 0
    retries: int = 0
    duplicate_completions_rejected: int = 0
    lease_reclaims: int = 0
    by_failure_kind: dict[str, int] = field(default_factory=dict)

    def record_failure_kind(self, kind: JobFailureKind) -> None:
        self.by_failure_kind[kind.value] = self.by_failure_kind.get(kind.value, 0) + 1


def classify_failure(error: BaseException) -> JobFailureKind:
    """Map an exception to a §70 failure kind.

    Explicit rather than inferred from the message: a substring match on an error string is a
    test that passes until a library rewords its exception. Anything unrecognised becomes
    ``UNKNOWN`` and is logged loudly, because an unrecognised kind means this table is missing a
    case — not that the failure is unimportant.
    """
    if isinstance(error, GenerationCancelledError):
        return JobFailureKind.CANCELLED
    if isinstance(error, GenerationTimeoutError | asyncio.TimeoutError):
        return JobFailureKind.TIMEOUT
    if isinstance(error, ProviderUnavailableError):
        return JobFailureKind.PROVIDER_UNAVAILABLE
    if isinstance(error, GpuResourceError):
        return JobFailureKind.RESOURCE_EXHAUSTED
    if isinstance(error, QualityControlError):
        return JobFailureKind.QUALITY_REJECTED
    if isinstance(error, AudioError):
        # Could not write the file: out of disk, or a bad path (§36).
        return JobFailureKind.RESOURCE_EXHAUSTED
    if isinstance(error, ValueError):
        # A blueprint the provider cannot realise. Retrying is pointless — the same request
        # fails identically and burns an attempt a transient failure would have used.
        return JobFailureKind.INVALID_REQUEST
    if isinstance(error, GenerationError):
        return JobFailureKind.PROVIDER_ERROR
    return JobFailureKind.UNKNOWN


class GenerationManager:
    """Runs generation jobs against a provider, surviving every way they fail."""

    def __init__(
        self,
        *,
        provider: MusicGenerationProvider,
        settings: GenerationSettings,
        unit_of_work: JobUnitOfWork,
        clock: Clock | None = None,
        capacity: CapacityTracker | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._provider = provider
        self._settings = settings
        self._unit_of_work = unit_of_work
        self._clock = clock or SystemClock()
        self._capacity = capacity or CapacityTracker(settings.capacity_window_jobs)
        # Identity for the lease. Includes the process id so two workers on one machine are
        # distinguishable in the audit trail — "which worker died" is the first question asked
        # when leases start expiring.
        self._worker_id = worker_id or f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._stats = ManagerStats()
        self._running: dict[str, asyncio.Task[GenerationOutcome]] = {}
        self._cancelled: set[str] = set()
        self._shutting_down = False

    # -- introspection -----------------------------------------------------

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def stats(self) -> ManagerStats:
        return self._stats

    @property
    def in_flight(self) -> int:
        return len(self._running)

    @property
    def is_shutting_down(self) -> bool:
        return self._shutting_down

    def capacity_snapshot(self) -> CapacitySnapshot:
        return self._capacity.snapshot()

    def describe_provider(self) -> str:
        return self._provider.describe().model_identifier

    @property
    def provider(self) -> MusicGenerationProvider:
        """The provider itself.

        Exposed so §7.24 can report ACE-Step's model state, VRAM and latency percentiles —
        facts only the provider holds. Read-only by convention: the manager still owns the
        job lifecycle, and a caller that generated through this would bypass leases, retries
        and capacity tracking entirely.
        """
        return self._provider

    async def provider_health(self) -> bool:
        """Whether the provider could work right now (§18, §34). Never raises."""
        try:
            return (await self._provider.healthcheck()).healthy
        except Exception as error:  # noqa: BLE001 - a healthcheck must never propagate
            _log.warning(
                "generation.healthcheck_failed",
                error=str(error),
                detail="treating the provider as unhealthy",
            )
            return False

    # -- planning ----------------------------------------------------------

    async def plan_job(
        self,
        blueprint: MusicBlueprintV1,
        *,
        priority: GenerationPriority | None = None,
        timeout_seconds: float | None = None,
    ) -> JobRecord:
        """Create a persistent job for ``blueprint``, or return the live one.

        **Idempotent per track.** Asking twice for the same track returns the existing job
        rather than creating a second. Two jobs for one track would both write the same output
        path, so whichever finished second would corrupt the file the first had already
        registered — and the queue would be holding a path whose contents changed underneath
        it.
        """

        async def work(repository: GenerationJobRepository) -> JobRecord:
            existing = await repository.active_for_track(blueprint.track_id)
            if existing is not None:
                _log.debug(
                    "generation.job_already_planned",
                    track_id=blueprint.track_id,
                    job_id=existing.job_id,
                    state=existing.state.value,
                )
                return existing
            return await repository.create(
                job_id=f"job-{uuid.uuid4().hex[:20]}",
                track_id=blueprint.track_id,
                provider=self._provider.describe().name,
                priority=priority or blueprint.priority,
                now=self._clock.now(),
                timeout_seconds=timeout_seconds or self._default_timeout(blueprint),
                max_attempts=self._settings.max_attempts,
                request_payload={
                    "title": blueprint.title,
                    "genre": blueprint.composition.genre,
                    "bpm": blueprint.composition.bpm,
                    "duration_seconds": blueprint.composition.duration_seconds,
                    "seed": blueprint.seed,
                },
            )

        job = await self._run(work)
        if job.attempt == 1 and job.state is JobState.QUEUED:
            self._stats.planned += 1
        return job

    def _default_timeout(self, blueprint: MusicBlueprintV1) -> float:
        """A deadline proportional to what was asked for, with a floor.

        A flat timeout is wrong in both directions: generous enough for a six-minute track, it
        lets a 45-second one hang for minutes; tight enough for the short one, it kills every
        long one. The multiplier is deliberately loose because the consequence of a premature
        timeout — a wasted attempt and a retry — is worse than waiting.
        """
        audio = float(blueprint.composition.duration_seconds)
        snapshot = self._capacity.snapshot()
        expected = snapshot.seconds_to_generate(audio)
        return max(60.0, min(self._settings.ace_step.timeout_seconds, expected * 4.0))

    # -- the work ----------------------------------------------------------

    async def claim_and_generate(
        self,
        *,
        output_path_for: Callable[[str], Path],
        lyrics_for: Callable[[str], LyricsV1 | None] | None = None,
        blueprint_for: Callable[[str], MusicBlueprintV1 | None] | None = None,
    ) -> GenerationOutcome | None:
        """Claim one job and run it. ``None`` when there is nothing to do.

        The concurrency limit is checked against **this manager's** in-flight count, not the
        database's leased count. They differ, and the local number is the right one: the limit
        exists to stop one process from oversubscribing its own GPU (§89), and another worker's
        lease is that worker's business.
        """
        if self._shutting_down:
            return None
        if self.in_flight >= self._settings.max_concurrent_jobs:
            return None

        job = await self._claim_next()
        if job is None:
            return None

        blueprint = None if blueprint_for is None else blueprint_for(job.track_id)
        if blueprint is None:
            # The job outlived its blueprint. Nothing can be generated, and leaving it claimed
            # would make it expire and retry forever.
            await self._run(
                lambda repo: repo.mark_failed(
                    job.job_id,
                    now=self._clock.now(),
                    kind=JobFailureKind.INVALID_REQUEST,
                    message="no blueprint available for this track",
                    owner=self._worker_id,
                )
            )
            self._stats.failed += 1
            self._stats.record_failure_kind(JobFailureKind.INVALID_REQUEST)
            return GenerationOutcome(
                job_id=job.job_id,
                track_id=job.track_id,
                state=JobState.FAILED,
                failure_kind=JobFailureKind.INVALID_REQUEST,
                detail="no blueprint available for this track",
                attempt=job.attempt,
            )

        request = GenerationRequest(
            blueprint=blueprint,
            output_path=output_path_for(job.track_id),
            lyrics=None if lyrics_for is None else lyrics_for(job.track_id),
            timeout_seconds=job.timeout_seconds,
            attempt=job.attempt,
        )
        # The coroutine is built first and closed explicitly if it cannot be scheduled.
        #
        # ``create_task`` raises if the loop is already closing, and a coroutine object that
        # was created but never awaited is a warning Python raises at collection time, from
        # whatever happens to be running then. During a deliberately abrupt shutdown — Gate F
        # kills a station without the orderly teardown §74 performs — that surfaced as
        # ``GeneratorExit`` and "Event loop is closed" attributed to an unrelated test.
        coroutine = self._run_job(job, request)
        try:
            task: asyncio.Task[GenerationOutcome] = asyncio.create_task(
                coroutine, name=f"generation:{job.job_id}"
            )
        except RuntimeError:
            coroutine.close()
            raise
        self._running[job.job_id] = task
        try:
            return await task
        except asyncio.CancelledError:
            # Cancelling *this* coroutine does not cancel the task it is awaiting, so without
            # this the generation task is orphaned: still running, nothing holding it, and
            # still renewing a lease on work nobody is waiting for. It shows up as "Task was
            # destroyed but it is pending" at interpreter exit, and as a job that stays
            # GENERATING long after shutdown.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise
        finally:
            self._running.pop(job.job_id, None)

    async def _claim_next(self) -> JobRecord | None:
        async def work(repository: GenerationJobRepository) -> JobRecord | None:
            now = self._clock.now()
            # Release elapsed backoffs first, or a station whose only pending work is a
            # RETRY_PENDING job would sit idle until something else happened to poke it.
            await repository.release_backoffs(now)
            candidate = await repository.next_claimable(now)
            if candidate is None:
                return None
            return await repository.claim(
                candidate.job_id,
                owner=self._worker_id,
                now=now,
                lease_seconds=self._settings.job_lease_seconds,
            )

        return await self._run(work)

    async def _run_job(
        self, job: JobRecord, request: GenerationRequest
    ) -> GenerationOutcome:
        """Generate, with a timeout, a lease renewal and full failure classification."""
        started_monotonic = self._clock.monotonic()
        await self._run(
            lambda repo: repo.mark_generating(
                job.job_id, owner=self._worker_id, now=self._clock.now()
            )
        )
        self._stats.started += 1
        _log.info(
            "generation.started",
            job_id=job.job_id,
            track_id=job.track_id,
            attempt=job.attempt,
            timeout_seconds=job.timeout_seconds,
        )

        renewer = asyncio.create_task(
            self._renew_lease_until_done(job.job_id), name=f"lease:{job.job_id}"
        )
        try:
            result = await self._generate_with_timeout(request)
        except asyncio.CancelledError:
            # Must propagate. Treating cancellation as a failure would record a shutdown as a
            # generation error *and* attempt a database write while the task is unwinding,
            # which fails against a session that is already closing. The job stays leased and
            # §70's reclaim recovers it — no second code path needed.
            raise
        except Exception as error:  # noqa: BLE001 - classified, recorded, never re-raised
            return await self._handle_failure(
                job, error, self._clock.monotonic() - started_monotonic
            )
        finally:
            renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewer

        return await self._handle_success(
            job, result, self._clock.monotonic() - started_monotonic
        )

    async def _generate_with_timeout(
        self, request: GenerationRequest
    ) -> GenerationResult:
        """Run the provider under a hard deadline.

        ``asyncio.wait_for`` rather than trusting the provider's own timeout: the failure that
        matters is a provider that *hangs*, and a hung provider is by definition not honouring
        its own deadline. Without this the job would sit in ``GENERATING`` until its lease
        expired — recoverable, but only after the whole lease duration, which the buffer may
        not have.
        """
        try:
            return await asyncio.wait_for(
                self._provider.generate(request), timeout=request.timeout_seconds
            )
        except asyncio.TimeoutError as error:
            # Ask the provider to stop, so a hung job is not left consuming VRAM behind the
            # scenes while the next attempt starts.
            with contextlib.suppress(Exception):
                await self._provider.cancel(request.track_id)
            raise GenerationTimeoutError(
                f"generation exceeded {request.timeout_seconds:.0f}s",
                track_id=request.track_id,
            ) from error

    async def _renew_lease_until_done(self, job_id: str) -> None:
        """Keep the lease alive while real work continues.

        Without renewal, every job longer than the lease would be reclaimed mid-generation and
        regenerated — so the lease duration would silently become a cap on track length. With
        it, an *absent* worker is still detected within one lease period, because a dead process
        renews nothing.
        """
        interval = max(1.0, self._settings.job_lease_seconds * LEASE_RENEW_FRACTION)
        while True:
            await self._clock.sleep(interval)
            renewed = await self._run(
                lambda repo: repo.renew_lease(
                    job_id,
                    owner=self._worker_id,
                    now=self._clock.now(),
                    lease_seconds=self._settings.job_lease_seconds,
                )
            )
            if not renewed:
                # Someone reclaimed it. Stop renewing; the completion attempt will be rejected
                # by the repository, which is the duplicate-completion guard working.
                _log.warning(
                    "generation.lease_lost",
                    job_id=job_id,
                    worker=self._worker_id,
                    detail="lease was reclaimed while generating; completion will be dropped",
                )
                return

    async def _handle_success(
        self, job: JobRecord, result: GenerationResult, wall_seconds: float
    ) -> GenerationOutcome:
        updated = await self._run(
            lambda repo: repo.mark_generated(
                job.job_id,
                owner=self._worker_id,
                now=self._clock.now(),
                generation_seconds=wall_seconds,
                peak_vram_bytes=result.peak_vram_bytes,
            )
        )
        if updated is None:
            # The lease was reclaimed while this worker was generating, so another worker owns
            # the job now. Dropping the result is correct: the queue may already hold audio
            # from the other attempt, and registering this one would make two tracks share a
            # path.
            self._stats.duplicate_completions_rejected += 1
            _log.warning(
                "generation.duplicate_completion_rejected",
                job_id=job.job_id,
                track_id=job.track_id,
                worker=self._worker_id,
            )
            return GenerationOutcome(
                job_id=job.job_id,
                track_id=job.track_id,
                state=JobState.ABANDONED,
                failure_kind=JobFailureKind.LEASE_EXPIRED,
                detail="lease was reclaimed before completion could be recorded",
                attempt=job.attempt,
                wall_seconds=wall_seconds,
            )

        self._capacity.record_success(
            audio_seconds=result.duration_seconds, wall_seconds=wall_seconds
        )
        self._stats.completed += 1
        _log.info(
            "generation.completed",
            job_id=job.job_id,
            track_id=job.track_id,
            wall_seconds=round(wall_seconds, 2),
            audio_seconds=round(result.duration_seconds, 1),
            ratio=round(result.duration_seconds / max(wall_seconds, 1e-6), 2),
        )
        return GenerationOutcome(
            job_id=job.job_id,
            track_id=job.track_id,
            state=JobState.GENERATED,
            result=result,
            attempt=job.attempt,
            wall_seconds=wall_seconds,
        )

    async def _handle_failure(
        self, job: JobRecord, error: BaseException, wall_seconds: float
    ) -> GenerationOutcome:
        kind = classify_failure(error)
        message = f"{type(error).__name__}: {error}"
        self._capacity.record_failure(
            wall_seconds=wall_seconds, timeout=kind is JobFailureKind.TIMEOUT
        )
        self._stats.record_failure_kind(kind)
        if kind is JobFailureKind.TIMEOUT:
            self._stats.timeouts += 1
        if kind is JobFailureKind.UNKNOWN:
            _log.error(
                "generation.unclassified_failure",
                job_id=job.job_id,
                error_type=type(error).__name__,
                error=str(error),
                detail="classify_failure has no case for this exception type",
            )

        if kind is JobFailureKind.CANCELLED:
            await self._run(
                lambda repo: repo.mark_cancelled(
                    job.job_id, now=self._clock.now(), reason=message
                )
            )
            self._stats.cancelled += 1
            return GenerationOutcome(
                job_id=job.job_id,
                track_id=job.track_id,
                state=JobState.CANCELLED,
                failure_kind=kind,
                detail=message,
                attempt=job.attempt,
                wall_seconds=wall_seconds,
            )

        retryable = kind.is_retryable and job.attempt < job.max_attempts
        if not retryable:
            await self._run(
                lambda repo: repo.mark_failed(
                    job.job_id,
                    now=self._clock.now(),
                    kind=kind,
                    message=message,
                    owner=self._worker_id,
                )
            )
            self._stats.failed += 1
            _log.warning(
                "generation.failed",
                job_id=job.job_id,
                track_id=job.track_id,
                kind=kind.value,
                attempt=job.attempt,
                max_attempts=job.max_attempts,
                retryable=kind.is_retryable,
                error=message,
            )
            return GenerationOutcome(
                job_id=job.job_id,
                track_id=job.track_id,
                state=JobState.FAILED,
                failure_kind=kind,
                detail=message,
                attempt=job.attempt,
                wall_seconds=wall_seconds,
            )

        backoff = self.backoff_seconds(job.attempt, kind)
        await self._run(
            lambda repo: repo.mark_retry_pending(
                job.job_id,
                now=self._clock.now(),
                retry_after_seconds=backoff,
                kind=kind,
                message=message,
                owner=self._worker_id,
            )
        )
        self._capacity.record_retry()
        self._stats.retries += 1
        _log.info(
            "generation.retry_scheduled",
            job_id=job.job_id,
            track_id=job.track_id,
            kind=kind.value,
            attempt=job.attempt,
            retry_after_seconds=round(backoff, 1),
        )
        return GenerationOutcome(
            job_id=job.job_id,
            track_id=job.track_id,
            state=JobState.RETRY_PENDING,
            failure_kind=kind,
            detail=message,
            attempt=job.attempt,
            wall_seconds=wall_seconds,
            retry_after_seconds=backoff,
        )

    def backoff_seconds(self, attempt: int, kind: JobFailureKind) -> float:
        """Exponential backoff for ``attempt``, bounded.

        Bounded in both directions. The ceiling exists because an unbounded backoff causes the
        starvation it is meant to prevent — five attempts at 2x from 5 seconds already reaches
        80, and the §26 minimum buffer is finite. The resource multiplier exists because
        hammering a provider that is out of VRAM delays its recovery.
        """
        base = self._settings.retry_backoff_seconds
        multiplier = self._settings.retry_backoff_multiplier ** max(0, attempt - 1)
        delay = base * multiplier
        if kind.needs_long_backoff:
            delay *= RESOURCE_BACKOFF_MULTIPLIER
        return min(MAX_BACKOFF_SECONDS, delay)

    # -- cancellation ------------------------------------------------------

    async def cancel_track(self, track_id: str, *, reason: str) -> bool:
        """Cancel whatever work exists for a track (§28's replan, operator skip).

        Both halves matter: the provider is asked to stop so a GPU is not held for audio nobody
        wants, and the job row is marked cancelled so no worker picks it up again.
        """
        self._cancelled.add(track_id)
        with contextlib.suppress(Exception):
            await self._provider.cancel(track_id)

        async def work(repository: GenerationJobRepository) -> bool:
            job = await repository.active_for_track(track_id)
            if job is None:
                return False
            updated = await repository.mark_cancelled(
                job.job_id, now=self._clock.now(), reason=reason
            )
            return updated is not None

        cancelled = await self._run(work)
        if cancelled:
            self._stats.cancelled += 1
            _log.info("generation.cancelled", track_id=track_id, reason=reason)
        return cancelled

    # -- maintenance (§70, §75) --------------------------------------------

    async def reclaim_expired_leases(self) -> list[JobRecord]:
        """Return dead workers' jobs to the pool. Run on a timer.

        This is the whole of §70's crash story: nothing detects the crash, the lease simply
        runs out.
        """
        records = await self._run(
            lambda repo: repo.reclaim_expired(
                self._clock.now(),
                retry_after_seconds=self._settings.retry_backoff_seconds,
            )
        )
        self._stats.lease_reclaims += len(records)
        for job in records:
            if job.state is JobState.ABANDONED:
                self._stats.abandoned += 1
        return records

    async def recover_on_startup(self) -> dict[str, int]:
        """Classify jobs left behind by a crash (§75, milestone 4.9).

        Not "mark everything failed" — see
        :meth:`~tradefix_radio.persistence.repositories.jobs.GenerationJobRepository.classify_on_startup`
        for why that would throw away completed work.
        """
        return await self._run(lambda repo: repo.classify_on_startup(self._clock.now()))

    # -- shutdown (§74) ----------------------------------------------------

    async def shutdown(self, *, timeout_seconds: float = 30.0) -> None:
        """Stop accepting work and let in-flight jobs finish if they can.

        Finishing beats cancelling: a job three minutes into a four-minute generation is nearly
        a track, and throwing it away costs the buffer. Jobs that do not finish in time are left
        leased — their leases expire and they are reclaimed, which is exactly the mechanism
        §70 already provides and needs no second code path.
        """
        self._shutting_down = True
        if not self._running:
            return
        _log.info(
            "generation.shutdown_waiting",
            in_flight=len(self._running),
            timeout_seconds=timeout_seconds,
        )
        pending = list(self._running.values())
        done, still_running = await asyncio.wait(pending, timeout=timeout_seconds)
        for task in still_running:
            task.cancel()
        if still_running:
            await asyncio.gather(*still_running, return_exceptions=True)
            _log.warning(
                "generation.shutdown_incomplete",
                abandoned=len(still_running),
                detail="leases will expire and the jobs will be reclaimed",
            )
        _log.info("generation.shutdown_complete", finished=len(done))

    # -- plumbing ----------------------------------------------------------

    async def _run(self, work: JobWork[_T]) -> _T:
        return await self._unit_of_work(work)


class DatabaseJobUnitOfWork:
    """A :class:`JobUnitOfWork` over a database's session factory.

    A class rather than a closure because the protocol's ``__call__`` is generic in the
    callback's return type, and a nested ``async def`` cannot express that — it would have to
    annotate one concrete type and every call site would get it back.

    Lives here rather than in persistence so the manager has no opinion about how sessions are
    produced, which is what lets the tests drive it with an in-memory fake.
    """

    __slots__ = ("_session_factory",)

    def __init__(self, session_factory: Callable[[], AbstractAsyncContextManager[Any]]) -> None:
        self._session_factory = session_factory

    async def __call__(self, work: JobWork[_T]) -> _T:
        async with self._session_factory() as session:
            return await work(GenerationJobRepository(session))


__all__ = [
    "LEASE_RENEW_FRACTION",
    "MAX_BACKOFF_SECONDS",
    "DatabaseJobUnitOfWork",
    "GenerationManager",
    "GenerationOutcome",
    "JobUnitOfWork",
    "JobWork",
    "ManagerStats",
    "classify_failure",
]
