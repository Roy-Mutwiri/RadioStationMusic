"""GenerationManager against a real database (§70, §93, milestone 4.2).

The twelve scenarios the milestone names, in order, are the section headings below. They run
against SQLite rather than a fake repository on purpose: every guarantee that matters here —
one worker per job, no permanently-blocked job, one completion — is enforced by a conditional
``UPDATE``, and a fake repository would be testing the fake's interpretation of those semantics
rather than the database's.

Everything time-dependent runs on a :class:`~tradefix_radio.core.clock.VirtualClock`. Lease
expiry and retry backoff are most of this subsystem, and testing them against wall time would
mean either sleeping for real or not testing them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from tradefix_radio.config.schema import GenerationSettings, MockProviderSettings
from tradefix_radio.contracts.enums import GenerationPriority
from tradefix_radio.contracts.music import MusicBlueprintV1
from tradefix_radio.core.clock import VirtualClock
from tradefix_radio.core.errors import (
    GenerationError,
    GpuResourceError,
    ProviderUnavailableError,
)
from tradefix_radio.core.job_states import JobFailureKind, JobState
from tradefix_radio.generation import (
    GenerationManager,
    GenerationRequest,
    GenerationResult,
    MockMusicProvider,
)
from tradefix_radio.generation.manager import DatabaseJobUnitOfWork, classify_failure
from tradefix_radio.generation.provider import (
    ProgressCallback,
    ProviderDescription,
    ProviderHealth,
)
from tradefix_radio.persistence.database import Database
from tradefix_radio.persistence.repositories import GenerationJobRepository
from tests.conftest import FIXED_NOW, make_blueprint

#: Short enough that the whole rendering cost of a test file stays sane.
TRACK_SECONDS = 30
BEHAVIOUR_RATE = 16_000


# ---------------------------------------------------------------- doubles


class ScriptedProvider:
    """A provider whose every call is dictated by a script.

    The mock provider is for testing *audio*; this is for testing the manager's reaction to
    outcomes it cannot otherwise produce on demand — a hang, a specific exception type, a
    success only on the third attempt.
    """

    def __init__(
        self,
        *,
        outcomes: list[BaseException | None] | None = None,
        hang_forever: bool = False,
        clock: VirtualClock | None = None,
        healthy: bool = True,
    ) -> None:
        self._outcomes = outcomes or []
        self._hang = hang_forever
        self._clock = clock
        self._healthy = healthy
        self.calls = 0
        self.cancelled: list[str] = []
        self.started = asyncio.Event()

    def describe(self) -> ProviderDescription:
        return ProviderDescription(
            name="scripted",
            model_identifier="scripted/1",
            supports_vocals=False,
            is_realtime_costly=False,
        )

    async def healthcheck(self) -> ProviderHealth:
        return ProviderHealth.ok() if self._healthy else ProviderHealth.down("scripted down")

    async def cancel(self, track_id: str) -> bool:
        self.cancelled.append(track_id)
        return True

    async def generate(
        self,
        request: GenerationRequest,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> GenerationResult:
        self.calls += 1
        self.started.set()
        if self._hang:
            # Sleeps on the virtual clock, so the manager's timeout is what ends this.
            assert self._clock is not None
            await self._clock.sleep(10_000.0)
        outcome = self._outcomes.pop(0) if self._outcomes else None
        if outcome is not None:
            raise outcome
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(b"audio")
        return GenerationResult(
            track_id=request.track_id,
            audio_path=request.output_path,
            duration_seconds=float(request.blueprint.composition.duration_seconds),
            sample_rate=BEHAVIOUR_RATE,
            channels=2,
            generation_seconds=1.0,
            model_identifier="scripted/1",
            provider_name="scripted",
        )


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def clock() -> VirtualClock:
    """A virtual clock that leaves room for real database I/O.

    ``real_yield_seconds`` is non-zero because every job state change is an aiosqlite round
    trip, which runs on a thread and therefore needs wall time rather than merely an event-loop
    turn. With a pure zero-yield clock the lease renewer renewed once in 150 virtual seconds —
    its write never finished — which under a soak would look like leases expiring under live
    generation. See :class:`~tradefix_radio.core.clock.VirtualClock`.
    """
    return VirtualClock(start=FIXED_NOW, real_yield_seconds=0.002)


@pytest.fixture
def generation_settings() -> GenerationSettings:
    return GenerationSettings(
        max_attempts=3,
        retry_backoff_seconds=5.0,
        retry_backoff_multiplier=2.0,
        job_lease_seconds=60.0,
        max_concurrent_jobs=2,
        mock=MockProviderSettings(latency_seconds=0.0, sample_rate=BEHAVIOUR_RATE),
    )


@pytest.fixture
def unit_of_work(database: Database) -> DatabaseJobUnitOfWork:
    return DatabaseJobUnitOfWork(database.session)


def build_manager(
    *,
    provider: object,
    settings: GenerationSettings,
    unit_of_work: DatabaseJobUnitOfWork,
    clock: VirtualClock,
    worker_id: str = "worker-a",
) -> GenerationManager:
    return GenerationManager(
        provider=provider,  # type: ignore[arg-type]
        settings=settings,
        unit_of_work=unit_of_work,
        clock=clock,
        worker_id=worker_id,
    )


def blueprint(index: int = 1) -> MusicBlueprintV1:
    return make_blueprint(
        track_id=f"TF-{index:05d}", duration_seconds=TRACK_SECONDS
    )


def paths(tmp_path: Path) -> Callable[[str], Path]:
    return lambda track_id: tmp_path / f"{track_id}.wav"


def lookup(*blueprints: MusicBlueprintV1) -> Callable[[str], MusicBlueprintV1 | None]:
    table = {bp.track_id: bp for bp in blueprints}
    return table.get


async def jobs_of(database: Database, track_id: str) -> list[object]:
    async with database.read_session() as session:
        return list(await GenerationJobRepository(session).for_track(track_id))


async def job_state(database: Database, job_id: str) -> JobState | None:
    async with database.read_session() as session:
        record = await GenerationJobRepository(session).get(job_id)
    return None if record is None else record.state


# ---------------------------------------------------------------- 1. normal generation


async def test_a_job_runs_from_planned_to_generated(
    database: Database,
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider()
    manager = build_manager(
        provider=provider, settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    job = await manager.plan_job(plan)
    assert job.state is JobState.QUEUED

    outcome = await manager.claim_and_generate(
        output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
    )
    assert outcome is not None
    assert outcome.succeeded
    assert outcome.result is not None
    assert outcome.result.audio_path.is_file()
    assert await job_state(database, job.job_id) is JobState.GENERATED
    assert manager.stats.completed == 1


async def test_capacity_is_measured_from_completed_jobs(
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    """§93: the ratio the scheduler reads has to come from real outcomes."""
    manager = build_manager(
        provider=ScriptedProvider(), settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    assert manager.capacity_snapshot().samples == 0
    plan = blueprint()
    await manager.plan_job(plan)
    await manager.claim_and_generate(
        output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
    )
    snapshot = manager.capacity_snapshot()
    assert snapshot.samples == 1
    assert snapshot.capacity_ratio > 0


async def test_claiming_with_nothing_queued_returns_nothing(
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    manager = build_manager(
        provider=ScriptedProvider(), settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    assert (
        await manager.claim_and_generate(
            output_path_for=paths(tmp_path), blueprint_for=lookup()
        )
        is None
    )


# ---------------------------------------------------------------- 2. timeout


async def test_a_hanging_provider_is_cut_off_at_its_deadline(
    database: Database,
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    """The failure that matters. A hung provider does not honour its own deadline.

    Without the manager's ``wait_for`` the job would sit in ``GENERATING`` until its lease
    expired — recoverable, but only after the whole lease duration, which the buffer may not
    have.
    """
    provider = ScriptedProvider(hang_forever=True, clock=clock)
    manager = build_manager(
        provider=provider, settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    job = await manager.plan_job(plan, timeout_seconds=30.0)

    task = asyncio.create_task(
        manager.claim_and_generate(
            output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
        )
    )
    await provider.started.wait()
    await clock.run_for(45.0)
    outcome = await task

    assert outcome is not None
    assert outcome.failure_kind is JobFailureKind.TIMEOUT
    assert outcome.will_retry, "a timeout is retryable; the next attempt may be luckier"
    assert await job_state(database, job.job_id) is JobState.RETRY_PENDING
    assert manager.stats.timeouts == 1
    assert provider.cancelled == [plan.track_id], (
        "a timed-out provider must be told to stop, or it keeps holding VRAM"
    )


# ---------------------------------------------------------------- 3. provider exception


@pytest.mark.parametrize(
    ("error", "kind", "retryable"),
    [
        (GenerationError("model blew up"), JobFailureKind.PROVIDER_ERROR, True),
        (ProviderUnavailableError("socket closed"), JobFailureKind.PROVIDER_UNAVAILABLE, True),
        (GpuResourceError("out of VRAM"), JobFailureKind.RESOURCE_EXHAUSTED, True),
        (ValueError("blueprint cannot be realised"), JobFailureKind.INVALID_REQUEST, False),
    ],
)
async def test_provider_exceptions_are_classified_not_propagated(
    database: Database,
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
    error: Exception,
    kind: JobFailureKind,
    retryable: bool,
) -> None:
    """§86: a generation failure must never reach playout.

    The mechanism is that the manager returns outcomes rather than raising — there is no
    exception for a caller to fail to catch.
    """
    provider = ScriptedProvider(outcomes=[error])
    manager = build_manager(
        provider=provider, settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    job = await manager.plan_job(plan)

    outcome = await manager.claim_and_generate(
        output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
    )
    assert outcome is not None
    assert outcome.failure_kind is kind
    assert outcome.will_retry is retryable
    expected = JobState.RETRY_PENDING if retryable else JobState.FAILED
    assert await job_state(database, job.job_id) is expected


def test_an_unrecognised_exception_is_classified_as_unknown() -> None:
    """Unknown means "this table is missing a case", not "unimportant"."""
    assert classify_failure(KeyError("surprise")) is JobFailureKind.UNKNOWN
    assert JobFailureKind.UNKNOWN.is_retryable


def test_resource_failures_ask_for_a_longer_backoff() -> None:
    """Hammering a provider that is out of VRAM delays its recovery."""
    assert JobFailureKind.RESOURCE_EXHAUSTED.needs_long_backoff
    assert JobFailureKind.PROVIDER_UNAVAILABLE.needs_long_backoff
    assert not JobFailureKind.PROVIDER_ERROR.needs_long_backoff


# ---------------------------------------------------------------- 4. cancellation


async def test_cancelling_a_track_stops_its_job(
    database: Database,
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
) -> None:
    """§28's replan and §44's operator skip both land here."""
    provider = ScriptedProvider()
    manager = build_manager(
        provider=provider, settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    job = await manager.plan_job(plan)

    assert await manager.cancel_track(plan.track_id, reason="regime changed") is True
    assert await job_state(database, job.job_id) is JobState.CANCELLED
    assert provider.cancelled == [plan.track_id]


async def test_cancelling_an_unknown_track_is_not_an_error(
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
) -> None:
    manager = build_manager(
        provider=ScriptedProvider(), settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    assert await manager.cancel_track("TF-99999", reason="nothing there") is False


async def test_a_cancelled_job_is_not_claimed_again(
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    manager = build_manager(
        provider=ScriptedProvider(), settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    await manager.plan_job(plan)
    await manager.cancel_track(plan.track_id, reason="replanned away")
    assert (
        await manager.claim_and_generate(
            output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
        )
        is None
    )


# ---------------------------------------------------------------- 5. expired lease


async def test_an_expired_lease_returns_the_job_to_the_pool(
    database: Database,
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
) -> None:
    """§70's whole point: nothing has to *detect* the crash."""
    manager = build_manager(
        provider=ScriptedProvider(), settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    job = await manager.plan_job(plan)

    async with database.session() as session:
        claimed = await GenerationJobRepository(session).claim(
            job.job_id, owner="dead-worker", now=clock.now(), lease_seconds=60.0
        )
    assert claimed is not None

    clock.advance_sync(120.0)
    reclaimed = await manager.reclaim_expired_leases()
    assert [r.job_id for r in reclaimed] == [job.job_id]
    assert await job_state(database, job.job_id) is JobState.RETRY_PENDING


async def test_an_expired_lease_cannot_block_a_job_forever(
    database: Database,
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    """The named prohibition. After reclaim, another worker must be able to run it."""
    manager = build_manager(
        provider=ScriptedProvider(), settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    job = await manager.plan_job(plan)
    async with database.session() as session:
        await GenerationJobRepository(session).claim(
            job.job_id, owner="dead-worker", now=clock.now(), lease_seconds=60.0
        )

    clock.advance_sync(200.0)
    await manager.reclaim_expired_leases()
    # A reclaimed job lands in RETRY_PENDING with a backoff, so a crash-looping worker cannot
    # spin on it. The backoff has to elapse before anyone may claim it again.
    assert (
        await manager.claim_and_generate(
            output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
        )
        is None
    )
    clock.advance_sync(generation_settings.retry_backoff_seconds + 1.0)
    outcome = await manager.claim_and_generate(
        output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
    )
    assert outcome is not None
    assert outcome.succeeded


async def test_a_lease_is_renewed_while_real_work_continues(
    database: Database,
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    """Without renewal the lease duration becomes a silent cap on track length."""
    provider = ScriptedProvider(hang_forever=True, clock=clock)
    manager = build_manager(
        provider=provider, settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    job = await manager.plan_job(plan, timeout_seconds=600.0)
    task = asyncio.create_task(
        manager.claim_and_generate(
            output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
        )
    )
    await provider.started.wait()

    # Well past the 60-second lease, but the renewer keeps it alive. Advanced in short steps
    # rather than one jump: the renewer does a database round trip between sleeps, and while
    # it is doing so it is runnable rather than sleeping — so a single large advance can
    # outrun it.
    for _ in range(15):
        await clock.run_for(10.0)
        await asyncio.sleep(0)

    async with database.read_session() as session:
        record = await GenerationJobRepository(session).get(job.job_id)
    assert record is not None
    assert record.state is JobState.GENERATING
    assert record.lease_expires_at is not None
    assert record.lease_expires_at > clock.now(), (
        f"lease expiry {record.lease_expires_at} is not beyond now {clock.now()} "
        f"(t={clock.monotonic():.1f}s, state={record.state.value}); the lease duration has "
        "silently become a cap on track length"
    )

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------- 6. worker crash


async def test_a_crashed_workers_job_is_recovered_at_startup(
    database: Database,
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
) -> None:
    """A fresh process holds no leases, so any leased job is by definition orphaned.

    Its lease is cleared immediately rather than waited out: waiting would stall the queue for
    up to a full lease period for no reason, because the previous owner cannot possibly still
    be working.
    """
    manager = build_manager(
        provider=ScriptedProvider(), settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    job = await manager.plan_job(plan)
    async with database.session() as session:
        await GenerationJobRepository(session).claim(
            job.job_id, owner="crashed-worker", now=clock.now(), lease_seconds=3600.0
        )

    counts = await manager.recover_on_startup()
    assert counts["reclaimed"] == 1
    async with database.read_session() as session:
        record = await GenerationJobRepository(session).get(job.job_id)
    assert record is not None
    assert record.state is JobState.RETRY_PENDING
    assert record.lease_owner is None


async def test_startup_recovery_leaves_completed_work_alone(
    database: Database,
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    """Not "mark everything failed" — that throws away a GPU-minute of finished audio."""
    manager = build_manager(
        provider=ScriptedProvider(), settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    job = await manager.plan_job(plan)
    await manager.claim_and_generate(
        output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
    )
    assert await job_state(database, job.job_id) is JobState.GENERATED

    await manager.recover_on_startup()
    assert await job_state(database, job.job_id) is JobState.GENERATED


async def test_startup_recovery_leaves_a_pending_backoff_alone(
    database: Database,
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    """The backoff is in the database and is still valid across a restart."""
    provider = ScriptedProvider(outcomes=[GenerationError("boom")])
    manager = build_manager(
        provider=provider, settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    job = await manager.plan_job(plan)
    await manager.claim_and_generate(
        output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
    )
    assert await job_state(database, job.job_id) is JobState.RETRY_PENDING

    counts = await manager.recover_on_startup()
    assert counts["reclaimed"] == 0
    assert await job_state(database, job.job_id) is JobState.RETRY_PENDING


# ---------------------------------------------------------------- 7. retry success


async def test_a_job_succeeds_on_a_later_attempt(
    database: Database,
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(outcomes=[GenerationError("first failed"), None])
    manager = build_manager(
        provider=provider, settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    job = await manager.plan_job(plan)

    first = await manager.claim_and_generate(
        output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
    )
    assert first is not None
    assert first.will_retry

    # The backoff must actually hold the job back, or it is decorative.
    assert (
        await manager.claim_and_generate(
            output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
        )
        is None
    )

    await clock.advance(first.retry_after_seconds or 0.0)
    second = await manager.claim_and_generate(
        output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
    )
    assert second is not None
    assert second.succeeded
    assert second.attempt == 2
    assert await job_state(database, job.job_id) is JobState.GENERATED


async def test_backoff_grows_with_each_attempt_and_is_bounded(
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
) -> None:
    """Bounded in both directions — an unbounded backoff causes the starvation it prevents."""
    from tradefix_radio.generation.manager import MAX_BACKOFF_SECONDS

    manager = build_manager(
        provider=ScriptedProvider(), settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    delays = [
        manager.backoff_seconds(attempt, JobFailureKind.PROVIDER_ERROR)
        for attempt in range(1, 5)
    ]
    assert delays == sorted(delays)
    assert delays[0] < delays[-1]
    assert all(delay <= MAX_BACKOFF_SECONDS for delay in delays)
    assert manager.backoff_seconds(20, JobFailureKind.PROVIDER_ERROR) == MAX_BACKOFF_SECONDS


async def test_a_resource_failure_backs_off_harder(
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
) -> None:
    manager = build_manager(
        provider=ScriptedProvider(), settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    ordinary = manager.backoff_seconds(1, JobFailureKind.PROVIDER_ERROR)
    scarce = manager.backoff_seconds(1, JobFailureKind.RESOURCE_EXHAUSTED)
    assert scarce > ordinary


# ---------------------------------------------------------------- 8. exhausted retries


async def test_retries_are_finite(
    database: Database,
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    """The named prohibition: no infinite retries."""
    provider = ScriptedProvider(outcomes=[GenerationError("always fails")] * 10)
    manager = build_manager(
        provider=provider, settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    job = await manager.plan_job(plan)

    attempts = 0
    for _ in range(10):
        outcome = await manager.claim_and_generate(
            output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
        )
        if outcome is None:
            await clock.advance(200.0)
            continue
        attempts += 1
        if outcome.is_terminal_failure:
            break

    assert attempts == generation_settings.max_attempts
    assert await job_state(database, job.job_id) is JobState.FAILED
    assert manager.stats.failed == 1


async def test_a_non_retryable_failure_stops_immediately(
    database: Database,
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    """Retrying an unrealisable blueprint burns the budget a transient failure needs."""
    provider = ScriptedProvider(outcomes=[ValueError("bad blueprint")])
    manager = build_manager(
        provider=provider, settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    job = await manager.plan_job(plan)
    outcome = await manager.claim_and_generate(
        output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
    )
    assert outcome is not None
    assert outcome.is_terminal_failure
    assert provider.calls == 1
    assert await job_state(database, job.job_id) is JobState.FAILED


# ---------------------------------------------------------------- 9. duplicate completion


async def test_a_completion_after_the_lease_was_reclaimed_is_rejected(
    database: Database,
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    """The named prohibition: no duplicate completion events.

    A worker presumed dead comes back with a finished track. By then another worker may already
    have produced audio for the same path, so accepting the late result would leave two tracks
    sharing a file.
    """
    manager = build_manager(
        provider=ScriptedProvider(), settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    job = await manager.plan_job(plan)

    async with database.session() as session:
        repository = GenerationJobRepository(session)
        await repository.claim(
            job.job_id, owner=manager.worker_id, now=clock.now(), lease_seconds=60.0
        )
        await repository.mark_generating(
            job.job_id, owner=manager.worker_id, now=clock.now()
        )

    # Another worker reclaims it after the lease runs out.
    clock.advance_sync(120.0)
    async with database.session() as session:
        repository = GenerationJobRepository(session)
        await repository.reclaim_expired(clock.now(), retry_after_seconds=0.0)
        await repository.release_backoffs(clock.now())
        await repository.claim(
            job.job_id, owner="worker-b", now=clock.now(), lease_seconds=60.0
        )

    # The original worker's completion must be refused.
    async with database.session() as session:
        late = await GenerationJobRepository(session).mark_generated(
            job.job_id,
            owner=manager.worker_id,
            now=clock.now(),
            generation_seconds=30.0,
        )
    assert late is None
    assert await job_state(database, job.job_id) is JobState.LEASED


async def test_a_completed_job_cannot_complete_twice(
    database: Database,
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    manager = build_manager(
        provider=ScriptedProvider(), settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    job = await manager.plan_job(plan)
    await manager.claim_and_generate(
        output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
    )
    async with database.session() as session:
        again = await GenerationJobRepository(session).mark_generated(
            job.job_id,
            owner=manager.worker_id,
            now=clock.now(),
            generation_seconds=1.0,
        )
    assert again is None


# ---------------------------------------------------------------- 10. concurrent leases


async def test_only_one_worker_can_claim_a_job(
    database: Database,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
) -> None:
    """The named prohibition, and the reason the claim is a conditional UPDATE.

    A SELECT-then-UPDATE leaves a window both workers pass, and on a four-minute generation
    that costs a GPU-minute and produces a duplicate track.
    """
    unit_of_work = DatabaseJobUnitOfWork(database.session)
    manager = build_manager(
        provider=ScriptedProvider(),
        settings=generation_settings,
        unit_of_work=unit_of_work,
        clock=clock,
    )
    plan = blueprint()
    job = await manager.plan_job(plan)

    results = []
    for owner in ("worker-a", "worker-b", "worker-c"):
        async with database.session() as session:
            results.append(
                await GenerationJobRepository(session).claim(
                    job.job_id, owner=owner, now=clock.now(), lease_seconds=60.0
                )
            )
    winners = [record for record in results if record is not None]
    assert len(winners) == 1
    assert winners[0].lease_owner == "worker-a"


async def test_two_managers_do_not_run_the_same_job(
    database: Database,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    unit_of_work = DatabaseJobUnitOfWork(database.session)
    provider_a = ScriptedProvider()
    provider_b = ScriptedProvider()
    first = build_manager(
        provider=provider_a, settings=generation_settings, unit_of_work=unit_of_work,
        clock=clock, worker_id="worker-a",
    )
    second = build_manager(
        provider=provider_b, settings=generation_settings, unit_of_work=unit_of_work,
        clock=clock, worker_id="worker-b",
    )
    plan = blueprint()
    await first.plan_job(plan)

    outcomes = await asyncio.gather(
        first.claim_and_generate(
            output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
        ),
        second.claim_and_generate(
            output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
        ),
    )
    produced = [o for o in outcomes if o is not None]
    assert len(produced) == 1
    assert provider_a.calls + provider_b.calls == 1


async def test_planning_the_same_track_twice_reuses_the_job(
    database: Database,
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
) -> None:
    """Idempotency. Two jobs for one track would both write the same output path."""
    manager = build_manager(
        provider=ScriptedProvider(), settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    first = await manager.plan_job(plan)
    second = await manager.plan_job(plan)
    assert first.job_id == second.job_id
    assert len(await jobs_of(database, plan.track_id)) == 1


async def test_the_concurrency_limit_is_respected(
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    """§89: one process must not oversubscribe its own GPU."""
    provider = ScriptedProvider(hang_forever=True, clock=clock)
    limited = generation_settings.model_copy(update={"max_concurrent_jobs": 1})
    manager = build_manager(
        provider=provider, settings=limited, unit_of_work=unit_of_work, clock=clock
    )
    first_plan, second_plan = blueprint(1), blueprint(2)
    await manager.plan_job(first_plan)
    await manager.plan_job(second_plan)

    running = asyncio.create_task(
        manager.claim_and_generate(
            output_path_for=paths(tmp_path),
            blueprint_for=lookup(first_plan, second_plan),
        )
    )
    await provider.started.wait()
    assert manager.in_flight == 1
    assert (
        await manager.claim_and_generate(
            output_path_for=paths(tmp_path),
            blueprint_for=lookup(first_plan, second_plan),
        )
        is None
    )
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)


# ---------------------------------------------------------------- 11. shutdown


async def test_shutdown_stops_accepting_new_work(
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    manager = build_manager(
        provider=ScriptedProvider(), settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    await manager.plan_job(plan)
    await manager.shutdown()
    assert manager.is_shutting_down
    assert (
        await manager.claim_and_generate(
            output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
        )
        is None
    )


async def test_shutdown_leaves_an_unfinished_job_recoverable(
    database: Database,
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    """§74: a job cancelled at shutdown is left leased, and §70's reclaim handles it.

    Deliberately not a second code path. The lease mechanism already recovers abandoned work,
    and inventing a shutdown-specific cleanup would be a second thing that can be wrong.
    """
    provider = ScriptedProvider(hang_forever=True, clock=clock)
    manager = build_manager(
        provider=provider, settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    job = await manager.plan_job(plan, timeout_seconds=9_000.0)
    task = asyncio.create_task(
        manager.claim_and_generate(
            output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
        )
    )
    await provider.started.wait()

    await manager.shutdown(timeout_seconds=0.01)
    await asyncio.gather(task, return_exceptions=True)

    assert await job_state(database, job.job_id) in {
        JobState.GENERATING,
        JobState.LEASED,
    }
    clock.advance_sync(600.0)
    reclaimed = await manager.reclaim_expired_leases()
    assert [r.job_id for r in reclaimed] == [job.job_id]


async def test_shutdown_with_nothing_running_is_instant(
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
) -> None:
    manager = build_manager(
        provider=ScriptedProvider(), settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    await manager.shutdown()
    assert manager.in_flight == 0


# ---------------------------------------------------------------- 12. restart


async def test_a_restart_finds_and_classifies_unfinished_jobs(
    database: Database,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    """Milestone 4.2's last scenario, end to end.

    Four jobs in four states survive a "process restart" — a brand new manager over the same
    database — and each must be classified by what it actually was, not swept into one bucket.
    """
    unit_of_work = DatabaseJobUnitOfWork(database.session)
    done, leased, failing, queued = (blueprint(i) for i in range(1, 5))
    everything = lookup(done, leased, failing, queued)
    first = build_manager(
        provider=ScriptedProvider(), settings=generation_settings, unit_of_work=unit_of_work,
        clock=clock, worker_id="worker-before",
    )

    # Order matters, and getting it wrong is instructive. Claims go by priority then **age**,
    # so a manager asked to run "the failing job" actually takes the oldest claimable one. An
    # earlier version of this test planned the queued job first and then watched the failing
    # manager claim *it* — and because that manager's blueprint lookup only knew its own track,
    # the wrong job died of INVALID_REQUEST. The product was right; the test was wrong. Each
    # manager below is given the complete lookup so a mis-claim cannot masquerade as a failure.
    done_job = await first.plan_job(done)
    await first.claim_and_generate(
        output_path_for=paths(tmp_path), blueprint_for=everything
    )
    clock.advance_sync(1.0)

    leased_job = await first.plan_job(leased)
    async with database.session() as session:
        await GenerationJobRepository(session).claim(
            leased_job.job_id, owner="worker-before", now=clock.now(), lease_seconds=600.0
        )
    clock.advance_sync(1.0)

    failing_manager = build_manager(
        provider=ScriptedProvider(outcomes=[GenerationError("boom")]),
        settings=generation_settings, unit_of_work=unit_of_work, clock=clock,
        worker_id="worker-before",
    )
    failing_job = await failing_manager.plan_job(failing)
    failed_outcome = await failing_manager.claim_and_generate(
        output_path_for=paths(tmp_path), blueprint_for=everything
    )
    assert failed_outcome is not None
    assert failed_outcome.track_id == failing.track_id, (
        "the failing manager claimed a different job than intended"
    )
    clock.advance_sync(1.0)

    queued_job = await first.plan_job(queued)

    # Restart: a new manager, a new worker identity, the same database.
    restarted = build_manager(
        provider=ScriptedProvider(), settings=generation_settings, unit_of_work=unit_of_work,
        clock=clock, worker_id="worker-after",
    )
    await restarted.recover_on_startup()

    assert await job_state(database, done_job.job_id) is JobState.GENERATED
    assert await job_state(database, leased_job.job_id) is JobState.RETRY_PENDING
    assert await job_state(database, queued_job.job_id) is JobState.QUEUED
    assert await job_state(database, failing_job.job_id) is JobState.RETRY_PENDING

    # And the orphaned job is immediately runnable, not held for a lease period.
    outcome = await restarted.claim_and_generate(
        output_path_for=paths(tmp_path), blueprint_for=everything
    )
    assert outcome is not None
    assert outcome.succeeded


# ---------------------------------------------------------------- priority (§94)


async def test_jobs_are_claimed_by_priority_then_age(
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    """When the buffer is in trouble, survival work must outrank experimentation.

    Ordering happens in Python because the column stores the enum's string value, so
    ``ORDER BY priority`` would sort alphabetically and put ``critical`` after ``experimental``.
    """
    manager = build_manager(
        provider=ScriptedProvider(), settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plans = {
        GenerationPriority.EXPERIMENTAL: blueprint(1),
        GenerationPriority.NORMAL: blueprint(2),
        GenerationPriority.CRITICAL: blueprint(3),
        GenerationPriority.HIGH: blueprint(4),
    }
    for priority, plan in plans.items():
        await manager.plan_job(plan, priority=priority)
        clock.advance_sync(1.0)

    claimed_order = []
    for _ in range(4):
        outcome = await manager.claim_and_generate(
            output_path_for=paths(tmp_path), blueprint_for=lookup(*plans.values())
        )
        assert outcome is not None
        claimed_order.append(outcome.track_id)

    assert claimed_order == [
        plans[GenerationPriority.CRITICAL].track_id,
        plans[GenerationPriority.HIGH].track_id,
        plans[GenerationPriority.NORMAL].track_id,
        plans[GenerationPriority.EXPERIMENTAL].track_id,
    ]


async def test_equal_priority_is_claimed_oldest_first(
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    manager = build_manager(
        provider=ScriptedProvider(), settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plans = [blueprint(index) for index in range(1, 4)]
    for plan in plans:
        await manager.plan_job(plan, priority=GenerationPriority.NORMAL)
        clock.advance_sync(10.0)

    order = []
    for _ in range(3):
        outcome = await manager.claim_and_generate(
            output_path_for=paths(tmp_path), blueprint_for=lookup(*plans)
        )
        assert outcome is not None
        order.append(outcome.track_id)
    assert order == [plan.track_id for plan in plans]


# ---------------------------------------------------------------- misc


async def test_a_missing_blueprint_fails_the_job_rather_than_looping(
    database: Database,
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    """A job that outlived its blueprint would otherwise expire and retry forever."""
    manager = build_manager(
        provider=ScriptedProvider(), settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    job = await manager.plan_job(plan)
    outcome = await manager.claim_and_generate(
        output_path_for=paths(tmp_path), blueprint_for=lambda _track_id: None
    )
    assert outcome is not None
    assert outcome.failure_kind is JobFailureKind.INVALID_REQUEST
    assert await job_state(database, job.job_id) is JobState.FAILED


async def test_the_real_mock_provider_works_through_the_manager(
    database: Database,
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    """End to end with the §62 provider, so the two halves are known to fit."""
    provider = MockMusicProvider(generation_settings.mock, clock=clock)
    manager = build_manager(
        provider=provider, settings=generation_settings, unit_of_work=unit_of_work, clock=clock
    )
    plan = blueprint()
    job = await manager.plan_job(plan)
    outcome = await manager.claim_and_generate(
        output_path_for=paths(tmp_path), blueprint_for=lookup(plan)
    )
    assert outcome is not None
    assert outcome.succeeded
    assert outcome.result is not None
    assert outcome.result.duration_seconds == pytest.approx(TRACK_SECONDS, abs=0.5)
    assert await job_state(database, job.job_id) is JobState.GENERATED


async def test_provider_health_is_reported_without_raising(
    unit_of_work: DatabaseJobUnitOfWork,
    generation_settings: GenerationSettings,
    clock: VirtualClock,
) -> None:
    """§18/§34: a healthcheck that raised would itself be a failure mode."""
    healthy = build_manager(
        provider=ScriptedProvider(healthy=True), settings=generation_settings,
        unit_of_work=unit_of_work, clock=clock,
    )
    assert await healthy.provider_health() is True

    sick = build_manager(
        provider=ScriptedProvider(healthy=False), settings=generation_settings,
        unit_of_work=unit_of_work, clock=clock,
    )
    assert await sick.provider_health() is False
