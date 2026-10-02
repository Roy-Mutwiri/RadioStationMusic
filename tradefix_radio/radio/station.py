"""RadioStation: the composition root (§72, §74, §75, milestone 4.9).

The one place that knows every subsystem. Everything else knows at most the queue.

**Stepped, not free-running.** The station exposes :meth:`step` — one unit of work across
scheduling, generation and playout — and :meth:`run` is a loop over it. That shape exists for
§64: a soak driving ``step`` against a virtual clock exercises *the real runtime* rather than a
parallel simulation, which is the difference between an endurance test and a reassuring fiction.
It also makes the whole station deterministic to test.

**Recovery is classification, not amnesia.** §75's temptation is to mark everything failed on
startup and begin fresh. That throws away generated audio sitting on disk, and worse, it can
resurrect a played track as new programming. :meth:`recover` sorts what it finds:

* a job that finished generating keeps its audio;
* a job whose worker vanished becomes retryable, immediately rather than after a lease period;
* a queue slot whose file is gone is marked unavailable rather than silently dropped;
* **a track that already aired never returns to the queue**, which is the one that would be
  audible to a listener and is checked explicitly.

**Persistence is periodic and on every significant change.** Writing on every block would make
the database the bottleneck; writing only at shutdown would lose everything to the crash §75 is
about. The compromise is: on track boundaries, and on a timer.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import structlog

from tradefix_radio.audio.io import read_info
from tradefix_radio.audio.sinks import AudioSink
from tradefix_radio.config.schema import AppSettings
from tradefix_radio.contracts.enums import MarketRegime, PlayoutTier, TransitionType
from tradefix_radio.contracts.events import (
    GenerationCompleted,
    GenerationFailed,
    GenerationJobPlanned,
    PlayoutFallbackEntered,
    PlayoutFallbackExited,
    RadioBufferCritical,
    RadioBufferLow,
    RadioBufferRecovered,
    StationIdRequested,
    TrackFinished,
    TrackQueued,
    TrackReady,
)
from tradefix_radio.contracts.market import MarketStateV1
from tradefix_radio.contracts.music import MusicBlueprintV1
from tradefix_radio.contracts.queue import QueueLockLevel
from tradefix_radio.core.clock import Clock, SystemClock
from tradefix_radio.core.errors import (
    AudioError,
    IllegalTransitionError,
    PersistenceError,
)
from tradefix_radio.core.state_machine import TrackState
from tradefix_radio.director.history import HistoryEntry, ProgrammingHistory
from tradefix_radio.director.memory import restore_director_state, save_director_state
from tradefix_radio.director.music_director import DirectorDecision, MusicDirector
from tradefix_radio.generation.manager import GenerationManager, GenerationOutcome
from tradefix_radio.persistence.database import Database
from tradefix_radio.persistence.repositories import (
    MemoryKeys,
    RadioMemoryRepository,
    TrackRepository,
)
from tradefix_radio.persistence.repositories.queue import (
    PLAYING_POSITION,
    PersistedQueueSlot,
    QueueRepository,
)
from tradefix_radio.radio.buffer import BufferAssessment, BufferLevel, BufferMonitor
from tradefix_radio.radio.emergency import EmergencyManager
from tradefix_radio.radio.playout import PlayingItem, PlayoutEngine
from tradefix_radio.radio.queue import QueueEntry, RadioQueue, ReadinessState
from tradefix_radio.radio.scheduler import Scheduler
from tradefix_radio.radio.station_ids import StationIdCategory, StationIdLibrary
from tradefix_radio.runtime.coordinator import RuntimeCoordinator

_log = structlog.get_logger(__name__)


def _entry_to_slot(entry: QueueEntry, position: int) -> PersistedQueueSlot:
    return PersistedQueueSlot(
        track_id=entry.track_id,
        position=position,
        blueprint=entry.blueprint,
        duration_seconds=entry.duration_seconds,
        enqueued_at=entry.inserted_at,
        state=entry.state.value,
        readiness=entry.readiness.value,
        lock_level=entry.lock_level.value,
        lock_reason=entry.lock_reason,
        tier=entry.tier.value,
        planned_regime=entry.planned_regime.value,
        planned_energy=entry.planned_energy,
        transition_in=entry.transition_in.value,
        transition_seconds=entry.transition_seconds,
        generation_progress=entry.generation_progress,
        audio_path=entry.audio_path,
        started_at=entry.started_at,
    )


def _slot_to_entry(slot: PersistedQueueSlot) -> QueueEntry:
    return QueueEntry(
        track_id=slot.track_id,
        blueprint=slot.blueprint,
        duration_seconds=slot.duration_seconds,
        inserted_at=slot.enqueued_at,
        state=TrackState(slot.state),
        readiness=ReadinessState(slot.readiness),
        lock_level=QueueLockLevel(slot.lock_level),
        lock_reason=slot.lock_reason,
        tier=PlayoutTier(slot.tier),
        planned_regime=MarketRegime(slot.planned_regime),
        planned_energy=slot.planned_energy,
        transition_in=TransitionType(slot.transition_in),
        transition_seconds=slot.transition_seconds,
        generation_progress=slot.generation_progress,
        audio_path=slot.audio_path,
        started_at=slot.started_at,
    )


def _as_history_entry(entry: object) -> HistoryEntry:
    """Convert a repository history row into the director's own entry type.

    Both satisfy ``ProgrammedTrack`` structurally, but the station holds a concrete list and
    appends freshly-aired tracks to it — so one type has to win, and it is the director's.
    """
    return HistoryEntry(
        track_id=entry.track_id,  # type: ignore[attr-defined]
        genre=entry.genre,  # type: ignore[attr-defined]
        secondary_genre=entry.secondary_genre,  # type: ignore[attr-defined]
        bpm=entry.bpm,  # type: ignore[attr-defined]
        musical_key=entry.musical_key,  # type: ignore[attr-defined]
        duration_seconds=entry.duration_seconds,  # type: ignore[attr-defined]
        is_instrumental=entry.is_instrumental,  # type: ignore[attr-defined]
        vocal_style=entry.vocal_style,  # type: ignore[attr-defined]
        primary_topic=entry.primary_topic,  # type: ignore[attr-defined]
        secondary_topic=entry.secondary_topic,  # type: ignore[attr-defined]
        persona_id=entry.persona_id,  # type: ignore[attr-defined]
        blueprint_signature=entry.blueprint_signature,  # type: ignore[attr-defined]
        energy_at_generation=entry.energy_at_generation,  # type: ignore[attr-defined]
        regime_at_generation=entry.regime_at_generation,  # type: ignore[attr-defined]
        created_at=entry.created_at,  # type: ignore[attr-defined]
        played_at=entry.played_at,  # type: ignore[attr-defined]
    )

#: Memory key for the station-identifier history (§96).
STATION_ID_STATE_KEY = "radio.station_id_state"
#: Memory key for the emergency manager's reserve position (§33, §96).
EMERGENCY_STATE_KEY = "radio.emergency_state"

#: How often the queue is written to the database, in simulated seconds.
#:
#: Track boundaries are the natural save point, but a long track means minutes between them —
#: so a timer covers the gap. Sixty seconds costs one small write per minute and bounds what a
#: crash can lose to one minute of queue churn.
PERSIST_INTERVAL_SECONDS = 60.0

#: How long the generation worker waits when there is nothing to claim.
#:
#: Short enough that a newly planned job starts promptly, long enough that an idle station is
#: not spinning. Slept on the injected clock, so it costs nothing under a virtual one.
GENERATION_IDLE_SECONDS = 5.0

#: Gap between scheduling passes.
#:
#: Frequent enough that a draining buffer is noticed within a few seconds, infrequent enough
#: that the director is not re-deciding constantly. Slept on the injected clock.
SCHEDULER_INTERVAL_SECONDS = 5.0

#: How many finished tracks may await persistence.
#:
#: Generous relative to the rate tracks finish at — one every few minutes — so the bound is
#: only reached if the database has genuinely stopped responding, which is worth an alert
#: rather than silent memory growth.
FINISHED_QUEUE_SIZE = 256

#: States a track passes through once its audio exists, up to ``READY``.
#:
#: The QC, originality and mastering work these states name is Phase 6's. Until then the
#: station walks them so the §27 transition trail is complete and the §46 page has something to
#: show — rather than jumping to ``READY`` and leaving a gap the real pipeline would have to
#: reconcile.
READY_LIFECYCLE: tuple[TrackState, ...] = (
    TrackState.GENERATING,
    TrackState.GENERATED,
    TrackState.ANALYZING,
    TrackState.APPROVED,
    TrackState.MASTERING,
    TrackState.READY,
)

#: States a track passes through when it goes on air.
PLAYING_LIFECYCLE: tuple[TrackState, ...] = (TrackState.QUEUED, TrackState.PLAYING)


@dataclass
class StationStats:
    """Everything the soak report needs that is not owned by a subsystem."""

    steps: int = 0
    generation_cycles: int = 0
    tracks_ready: int = 0
    tracks_failed: int = 0
    #: Generated audio that exists on disk but could not be put into the queue.
    ready_rejected: int = 0
    replans: int = 0
    persists: int = 0
    recovered_queue_entries: int = 0
    recovered_jobs: dict[str, int] = field(default_factory=dict)
    unavailable_on_restore: int = 0


@contextlib.contextmanager
def _reporting(action: str, **context: object) -> Iterator[None]:
    """Run a best-effort step, logging anything it raises instead of swallowing it.

    Used where the station must carry on regardless — a shutdown step, a queue update for a
    track that may already be gone — but where silence would be a bug in its own right. §86
    forbids suppressed errors, and ``contextlib.suppress(Exception)`` is exactly that: the
    first version of :meth:`RadioStation._accept_generated` wrapped ``mark_ready`` in one, and
    when every generated track silently failed to reach the queue the station reported an
    empty buffer with no error anywhere in the log. The bug took a gate test to find and the
    log said nothing at all.
    """
    try:
        yield
    except Exception as error:  # noqa: BLE001 - logged, counted by the caller, deliberate
        _log.error(
            "station.step_failed",
            action=action,
            error_type=type(error).__name__,
            error=str(error),
            exc_info=True,
            **context,
        )


class RadioStation:
    """Wires the subsystems and steps them. Owns no policy of its own."""

    def __init__(
        self,
        settings: AppSettings,
        *,
        database: Database,
        coordinator: RuntimeCoordinator,
        director: MusicDirector,
        generation: GenerationManager,
        sink: AudioSink,
        station_ids: StationIdLibrary,
        emergency: EmergencyManager | None = None,
        clock: Clock | None = None,
        audio_dir: Path | None = None,
        playout_block_seconds: float = 1.0,
    ) -> None:
        self._settings = settings
        self._database = database
        self._coordinator = coordinator
        self._director = director
        self._generation = generation
        self._clock = clock or SystemClock()
        self._audio_dir = audio_dir or settings.paths.generated_dir
        self._station_ids = station_ids

        self._queue = RadioQueue(
            locked_slots=settings.radio.locked_slots,
            semi_locked_slots=settings.radio.semi_locked_slots,
        )
        self._scheduler = Scheduler(
            settings, director=director, queue=self._queue, clock=self._clock
        )
        self._buffer = BufferMonitor(settings.radio)
        self._emergency = emergency or EmergencyManager()
        self._playout_block_seconds = playout_block_seconds
        self._playout = PlayoutEngine(
            sink=sink,
            queue=self._queue,
            emergency=self._emergency,
            clock=self._clock,
            block_seconds=playout_block_seconds,
            # Taken from the sink rather than from the module constants.
            #
            # The playout format *is* the output device's format — that is what "canonical"
            # means operationally, and the engine already conforms every input to it at the
            # boundary (§4.6). Hard-coding 48 kHz stereo here instead meant the engine
            # rejected any other sink outright, so an accelerated endurance run had to
            # synthesise, resample and mix full-rate stereo audio for every broadcast second
            # it simulated: a 24-hour run would have spent hours of CPU rendering audio
            # nobody listens to. The constants remain the default for a real deployment, via
            # the sinks themselves.
            sample_rate=sink.sample_rate,
            channels=sink.channels,
            on_track_started=self._on_track_started,
            on_track_finished=self._on_track_finished,
        )

        self._stats = StationStats()
        self._market: MarketStateV1 | None = None
        self._history: list[HistoryEntry] = []
        self._used_titles: list[str] = []
        self._used_signatures: set[str] = set()
        self._used_seeds: set[int] = set()
        self._played_track_ids: set[str] = set()
        self._last_persist_monotonic = 0.0
        self._last_buffer_level = BufferLevel.HEALTHY
        self._degraded_since: float | None = None
        self._last_tier = PlayoutTier.SCHEDULED
        self._tier_since = 0.0
        # Finished tracks waiting to be written. Bounded: an unbounded queue would turn a
        # database stall into unbounded memory growth over a week-long run.
        self._finished: asyncio.Queue[tuple[PlayingItem, bool, str]] = asyncio.Queue(
            maxsize=FINISHED_QUEUE_SIZE
        )

    # -- introspection -----------------------------------------------------

    @property
    def queue(self) -> RadioQueue:
        return self._queue

    @property
    def playout(self) -> PlayoutEngine:
        return self._playout

    @property
    def scheduler(self) -> Scheduler:
        return self._scheduler

    @property
    def emergency(self) -> EmergencyManager:
        return self._emergency

    @property
    def stats(self) -> StationStats:
        return self._stats

    @property
    def market(self) -> MarketStateV1 | None:
        return self._market

    def set_market(self, state: MarketStateV1) -> None:
        """Feed the station a market state. The only input it takes from outside."""
        self._market = state

    def assess_buffer(self) -> BufferAssessment:
        return self._buffer.assess(
            ready_seconds=self._queue.ready_seconds(),
            pending_seconds=self._queue.total_seconds() - self._queue.ready_seconds(),
            capacity=self._generation.capacity_snapshot(),
            monotonic_now=self._clock.monotonic(),
        )

    def history(self) -> ProgrammingHistory:
        return ProgrammingHistory(self._history)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        await self._coordinator.start()
        await self.recover()
        await self._playout.start()
        self._coordinator.spawn("playout", self._playout_loop)
        self._coordinator.spawn("generation-worker", self._generation_worker)
        self._coordinator.spawn("scheduler", self._scheduler_loop)
        self._coordinator.spawn("persistence", self._persistence_worker)
        _log.info(
            "station.started",
            queue_depth=len(self._queue),
            ready_minutes=round(self._queue.ready_seconds() / 60, 1),
            reserve_minutes=round(self._emergency.reserve_minutes, 1),
        )

    async def stop(self) -> None:
        """§74's graceful shutdown, in the order that loses the least.

        Generation first — an in-flight job may still finish, and a finished track is worth more
        than a fast exit. Then persist, so the queue on disk reflects reality. Then playout and
        the coordinator, because a subsystem shut down after the bus would publish into a closed
        bus during its own teardown.
        """
        with _reporting("shutdown generation"):
            await self._generation.shutdown(timeout_seconds=30.0)
        with _reporting("persist on shutdown"):
            await self.persist()
        with _reporting("stop playout"):
            await self._playout.stop()
        await self._coordinator.stop()
        _log.info(
            "station.stopped",
            tracks_played=self._playout.stats.tracks_completed,
            unintended_silence=round(
                self._playout.stats.unintended_silence_seconds, 3
            ),
        )

    # -- recovery (§75) ----------------------------------------------------

    async def recover(self) -> None:
        """Restore everything a crash could have interrupted.

        Order matters. Jobs are classified first so a reclaimed job is runnable before the
        scheduler looks at the buffer; the queue is restored next so the buffer figure is real;
        and the played-track guard runs last, over the restored queue, because that is where a
        resurrected track would appear.
        """
        self._stats.recovered_jobs = await self._generation.recover_on_startup()

        async with self._database.session() as session:
            slots = await QueueRepository(session).load()
            tracks = TrackRepository(session)
            history = await tracks.recent_history(limit=400)
            titles = await tracks.recent_titles(limit=500)
            # Continue the day's track-id numbering rather than restarting it; see
            # ``Scheduler.seed_sequence``.
            self._scheduler.seed_sequence(
                await tracks.next_daily_sequence(self._clock.now())
            )

        self._history = [_as_history_entry(entry) for entry in history]
        self._used_titles = list(titles)
        self._used_signatures = {entry.blueprint_signature for entry in history}
        self._played_track_ids = {entry.track_id for entry in history}

        entries = [_slot_to_entry(slot) for slot in slots if not slot.is_playing]
        playing_slot = next((slot for slot in slots if slot.is_playing), None)
        playing = None if playing_slot is None else _slot_to_entry(playing_slot)

        # A track that already aired must never return as new programming. It would be an
        # immediate, audible repeat, and §11's history would not catch it because the queue is
        # upstream of that check.
        resurrected = [e for e in entries if e.track_id in self._played_track_ids]
        if resurrected:
            _log.error(
                "station.resurrected_tracks_dropped",
                count=len(resurrected),
                track_ids=[e.track_id for e in resurrected][:10],
                detail="these already aired; restoring them would repeat them immediately",
            )
            entries = [e for e in entries if e.track_id not in self._played_track_ids]

        entries = [self._verify_audio(entry) for entry in entries]
        self._stats.unavailable_on_restore = sum(
            1 for e in entries if e.readiness is ReadinessState.UNAVAILABLE
        )

        # The track that was playing when the process died did **not** complete. It is not
        # restored to the playing slot — that would make the station resume mid-track with no
        # position — and it is not marked played either, because §75 forbids recording an
        # incomplete airing as a successful one.
        if playing is not None:
            _log.info(
                "station.interrupted_track_discarded",
                track_id=playing.track_id,
                detail="was playing at the crash; not marked played and not resumed",
            )

        self._queue.restore(entries)
        self._stats.recovered_queue_entries = len(entries)

        async with self._database.session() as session:
            memory = RadioMemoryRepository(session)
            await restore_director_state(memory, self._director.energy_planner)
            station_state = await memory.get(STATION_ID_STATE_KEY)
            if isinstance(station_state, dict):
                self._station_ids.restore_state(station_state)
            emergency_state = await memory.get(EMERGENCY_STATE_KEY)
            if isinstance(emergency_state, dict):
                self._emergency.restore_state(emergency_state)

        _log.info(
            "station.recovered",
            queue_entries=len(entries),
            unavailable=self._stats.unavailable_on_restore,
            resurrected_dropped=len(resurrected),
            jobs=self._stats.recovered_jobs,
            history=len(self._history),
        )

    def _verify_audio(self, entry: QueueEntry) -> QueueEntry:
        """Mark a slot unavailable if its file has gone (§36, disk fault).

        Checked on restore rather than trusted, because the retention sweeper runs between
        sessions and a slot pointing at a reclaimed file would reach the playout engine as a
        read error at the moment it went on air.
        """
        if entry.readiness is not ReadinessState.READY:
            return entry
        path = entry.audio_path
        if path and Path(path).is_file():
            return entry
        _log.error(
            "station.restored_audio_missing",
            track_id=entry.track_id,
            path=path,
        )
        return replace(
            entry,
            readiness=ReadinessState.UNAVAILABLE,
            lock_reason="audio file missing at restore",
        )

    # -- persistence -------------------------------------------------------

    async def persist(self) -> None:
        """Write the queue and creative memory. Cheap enough to call often."""
        now = self._clock.now()
        async with self._database.session() as session:
            await QueueRepository(session).save(self._persisted_slots())
            memory = RadioMemoryRepository(session)
            await save_director_state(memory, self._director.energy_planner, now=now)
            await memory.set_many(
                {
                    STATION_ID_STATE_KEY: self._station_ids.export_state(),
                    EMERGENCY_STATE_KEY: self._emergency.export_state(),
                    MemoryKeys.TRACK_SEQUENCE: len(self._history),
                },
                now=now,
            )
        self._stats.persists += 1
        self._last_persist_monotonic = self._clock.monotonic()

    def _persisted_slots(self) -> list[PersistedQueueSlot]:
        """The queue as flat rows. The conversion lives here because the domain types do."""
        slots: list[PersistedQueueSlot] = []
        if self._queue.playing is not None:
            slots.append(_entry_to_slot(self._queue.playing, PLAYING_POSITION))
        slots.extend(
            _entry_to_slot(entry, position)
            for position, entry in enumerate(self._queue)
        )
        return slots

    async def _persist_if_due(self) -> None:
        if (
            self._clock.monotonic() - self._last_persist_monotonic
            >= PERSIST_INTERVAL_SECONDS
        ):
            await self.persist()

    # -- the step ----------------------------------------------------------

    async def _playout_loop(self) -> None:
        """Pump the playout engine forever, and do nothing else.

        **Its own task, with nothing else in it.** An earlier version interleaved the pump with
        scheduling and maintenance in a single step, and the consequence was severe: while the
        task was doing database work the sink was not sleeping, so a virtual clock advanced past
        time that no audio covered. Measured, that was 290 seconds of audio written across
        3 600 seconds of broadcast — and because the silence counter only looked for
        *underruns*, it reported zero.

        Separated, the engine is always either writing a block or sleeping on the sink, which is
        what makes continuity a property of the design rather than of scheduling luck.
        """
        while not self._coordinator.should_stop:
            try:
                await self._playout.pump()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - the broadcast outlives any one block
                _log.error(
                    "station.playout_error",
                    error_type=type(error).__name__,
                    error=str(error),
                    exc_info=True,
                )
                # Sleep a block rather than spinning, so a persistent fault does not become a
                # busy loop that starves whatever might fix it.
                await self._clock.sleep(self._playout_block_seconds)

    async def _scheduler_loop(self) -> None:
        """Schedule and maintain on a timer, well away from the audio path.

        Does not hold the clock: scheduling publishes events and awaits other components, and
        a hold may only wrap a leaf of real work — see the note in :meth:`_generation_worker`.
        """
        while not self._coordinator.should_stop:
            await self.step()
            await self._clock.sleep(SCHEDULER_INTERVAL_SECONDS)

    async def step(self) -> None:
        """One scheduling and maintenance pass.

        Does **not** touch playout — that has its own task. Exposed so tests and the soak can
        drive scheduling deterministically instead of waiting on a timer.

        Each stage is individually guarded: a subsystem that raises must not take the rest of
        the cycle, let alone the broadcast, with it.
        """
        self._stats.steps += 1
        for stage, run in (
            ("schedule", self._run_scheduling),
            ("maintain", self._run_maintenance),
        ):
            try:
                await run()
            except Exception as error:  # noqa: BLE001 - the broadcast outranks every stage
                _log.error(
                    "station.stage_failed",
                    stage=stage,
                    error_type=type(error).__name__,
                    error=str(error),
                    exc_info=True,
                )

    async def run(self) -> None:
        """Start every loop and wait until the coordinator says stop."""
        await self.start()
        try:
            await self._coordinator.wait_for_stop()
        finally:
            await self.stop()

    # -- stages ------------------------------------------------------------

    async def _run_scheduling(self) -> None:
        state = self._market
        if state is None:
            return
        assessment = self.assess_buffer()
        await self._announce_buffer(assessment)

        decision = self._scheduler.decide(
            assessment=assessment,
            state=state,
            capacity=self._generation.capacity_snapshot(),
            provider_healthy=True,
        )

        if decision.replan:
            before = tuple(e.track_id for e in self._queue)
            replacements = self._scheduler.plan_tracks(
                decision,
                state=state,
                history=self.history(),
                buffer=assessment.to_contract(
                    ready_count=self._queue.snapshot().ready_count,
                    in_flight_count=len(self._queue) - self._queue.snapshot().ready_count,
                ),
                used_titles=tuple(self._used_titles),
                used_signatures=frozenset(self._used_signatures),
                used_seeds=frozenset(self._used_seeds),
                capacity_ratio=self._generation.capacity_snapshot().capacity_ratio,
            )
            self._scheduler.replan(replacements, state=state)
            await self._persist_tracks(replacements)
            after = tuple(e.track_id for e in self._queue)
            for track_id in self._scheduler.cancelled_track_ids(before, after):
                await self._generation.cancel_track(
                    track_id, reason="replanned after a market shift"
                )
            self._stats.replans += 1
            await self._publish_new_entries(replacements)
            return

        if not decision.wants_work:
            return

        planned = self._scheduler.plan_tracks(
            decision,
            state=state,
            history=self.history(),
            buffer=assessment.to_contract(
                ready_count=self._queue.snapshot().ready_count,
                in_flight_count=len(self._queue) - self._queue.snapshot().ready_count,
            ),
            used_titles=tuple(self._used_titles),
            used_signatures=frozenset(self._used_signatures),
            used_seeds=frozenset(self._used_seeds),
            capacity_ratio=self._generation.capacity_snapshot().capacity_ratio,
        )
        self._scheduler.enqueue(planned, state=state)
        await self._persist_tracks(planned)
        await self._publish_new_entries(planned)

        for produced in planned:
            job = await self._generation.plan_job(
                produced.blueprint, priority=decision.priority
            )
            await self._coordinator.publish(
                GenerationJobPlanned(
                    at=self._clock.now(),
                    job_id=job.job_id,
                    track_id=job.track_id,
                    priority=job.priority.value,
                    provider=job.provider,
                )
            )

    async def _persist_tracks(self, planned: Sequence[DirectorDecision]) -> None:
        """Write the track row and its blueprint (§8, §37).

        Not optional bookkeeping. Without it the queue persists slots whose blueprints are
        nowhere, so every one is dropped on restore — the first soak logged exactly that, and
        it meant queue recovery did not work at all despite the queue being saved.
        """
        now = self._clock.now()
        provider = self._generation.describe_provider()
        async with self._database.session() as session:
            tracks = TrackRepository(session)
            for produced in planned:
                if await tracks.get(produced.blueprint.track_id) is not None:
                    continue
                await tracks.create(
                    produced.blueprint,
                    now=now,
                    provider=self._settings.generation.provider,
                    model_identifier=provider,
                )

    async def _publish_new_entries(
        self, planned: Sequence[DirectorDecision]
    ) -> None:
        for produced in planned:
            blueprint = produced.blueprint
            self._used_titles.append(blueprint.title)
            self._used_signatures.add(blueprint.signature())
            self._used_seeds.add(blueprint.seed)
            position = self._queue.position_of(blueprint.track_id)
            if position is None:
                continue
            await self._coordinator.publish(
                TrackQueued(
                    at=self._clock.now(),
                    track_id=blueprint.track_id,
                    position=position,
                    lock_level=self._queue.snapshot().entries[position].lock_level.value,
                    queue_duration_seconds=self._queue.total_seconds(),
                )
            )

    async def _generation_worker(self) -> None:
        """Claim and run generation jobs, forever, in its own task (§26).

        A separate task rather than a step stage, because a generation call takes minutes and
        the playout pump has to keep writing throughout. The loop sleeps when there is nothing
        to claim, so an idle station costs nothing.
        """
        while not self._coordinator.should_stop:
            try:
                # Deliberately **not** inside ``clock.hold()``, though the real cost of
                # generating is an artefact of simulation and holding it looks right.
                #
                # A hold may only wrap a *leaf* of real work. This call is orchestration: it
                # awaits child tasks that themselves need virtual time to advance (the
                # provider's simulated latency), so holding here deadlocked the clock against
                # its own waiters until the 30-second escape hatch fired — on a 360-second
                # simulated run, eleven times, and the generation deadline expired each time.
                # The hold belongs where the real work is, which is inside the provider.
                outcome = await self._run_generation()
            except Exception as error:  # noqa: BLE001 - the worker must outlive one bad job
                _log.error(
                    "station.generation_worker_error",
                    error_type=type(error).__name__,
                    error=str(error),
                    exc_info=True,
                )
                outcome = None
            if outcome is None:
                # Nothing claimable. Sleeping on the injected clock keeps this cheap under a
                # virtual clock, where a busy loop would spin without time ever advancing.
                await self._clock.sleep(GENERATION_IDLE_SECONDS)

    async def _run_generation(self) -> GenerationOutcome | None:
        """Claim and run at most one job."""
        self._stats.generation_cycles += 1
        outcome = await self._generation.claim_and_generate(
            output_path_for=self._output_path_for,
            blueprint_for=self._blueprint_for,
        )
        if outcome is None:
            return None

        if outcome.succeeded and outcome.result is not None:
            await self._accept_generated(outcome)
            return outcome

        self._stats.tracks_failed += 1
        await self._coordinator.publish(
            GenerationFailed(
                at=self._clock.now(),
                job_id=outcome.job_id,
                track_id=outcome.track_id,
                kind=(outcome.failure_kind.value if outcome.failure_kind else "unknown"),
                detail=outcome.detail[:2000],
                attempt=outcome.attempt,
                will_retry=outcome.will_retry,
                retry_after_seconds=outcome.retry_after_seconds,
            )
        )
        if outcome.is_terminal_failure:
            # The slot can never be filled. Removing it keeps the queue's duration honest —
            # a permanent pending slot would inflate the buffer forever.
            with _reporting("drop permanently failed slot", track_id=outcome.track_id):
                self._queue.remove(outcome.track_id, force=True)
        return outcome

    async def _accept_generated(self, outcome: GenerationOutcome) -> None:
        result = outcome.result
        track_id = outcome.track_id
        if result is None:
            return
        try:
            measured = read_info(result.audio_path).duration_seconds
        except AudioError as error:
            _log.error(
                "station.generated_audio_unreadable",
                track_id=track_id,
                error=str(error),
            )
            with _reporting("mark slot unavailable", track_id=track_id):
                self._queue.mark_unavailable(track_id, reason="unreadable after generation")
            return

        try:
            self._queue.mark_ready(
                track_id,
                audio_path=str(result.audio_path),
                duration_seconds=measured,
            )
        except Exception as error:  # noqa: BLE001 - one bad slot must not stop the worker
            # Not suppressed, and not fatal either. The audio exists; what failed is the
            # bookkeeping that would put it on air, and that is worth a loud log and a counter
            # rather than either a crash or a shrug.
            self._stats.ready_rejected += 1
            _log.error(
                "station.mark_ready_failed",
                track_id=track_id,
                error_type=type(error).__name__,
                error=str(error),
                exc_info=True,
            )
            return
        self._stats.tracks_ready += 1
        self._buffer.record_delivery(self._clock.monotonic())
        await self._advance_track(track_id, READY_LIFECYCLE, reason="generated")

        now = self._clock.now()
        await self._coordinator.publish(
            GenerationCompleted(
                at=now,
                job_id=outcome.job_id,
                track_id=track_id,
                wall_seconds=outcome.wall_seconds,
                audio_seconds=measured,
                capacity_ratio=self._generation.capacity_snapshot().capacity_ratio,
            )
        )
        await self._coordinator.publish(
            TrackReady(
                at=now, track_id=track_id, duration_seconds=measured, novelty_score=1.0
            )
        )

    async def _advance_track(
        self, track_id: str, states: Sequence[TrackState], *, reason: str
    ) -> None:
        """Walk a track through ``states``, tolerating a state it has already left.

        The intermediate states are walked rather than skipped because §27 requires every
        transition to be persisted, and the §46 detail page reads that trail. The QC and
        mastering stages those states name are Phase 6's work — here the station passes
        through them with a reason that says so, rather than inventing a shortcut the real
        pipeline would then have to undo.
        """
        now = self._clock.now()
        async with self._database.session() as session:
            tracks = TrackRepository(session)
            for state in states:
                try:
                    await tracks.transition(track_id, state, now=now, reason=reason)
                except IllegalTransitionError:
                    # Already past this point — a retry, or a restart mid-lifecycle. Not an
                    # error: the track is where it needs to be or further along.
                    continue
                except PersistenceError as error:
                    _log.warning(
                        "station.track_transition_failed",
                        track_id=track_id,
                        state=state.value,
                        error=str(error),
                    )
                    return

    async def _run_maintenance(self) -> None:
        await self._generation.reclaim_expired_leases()
        await self._persist_if_due()
        await self._announce_tier()

    # -- callbacks ---------------------------------------------------------

    async def _on_track_started(self, item: PlayingItem) -> None:
        if item.is_station_id and item.station_id is not None:
            self._station_ids.note_played(item.station_id.key, now=self._clock.now())
            self._scheduler.note_station_id_played()

    async def _on_track_finished(
        self, item: PlayingItem, completed: bool, reason: str
    ) -> None:
        """Called by the playout engine on the audio path. **Does no I/O.**

        Everything here is in-memory or a bus publish. The database work a finished track
        implies — its state transitions, its play record, the queue snapshot — is handed to
        :meth:`_persistence_worker` instead.

        That split is not tidiness. An earlier version awaited three database round trips here,
        and because this runs inside the playout task, the sink was not sleeping while they
        happened: a virtual clock advanced over the gap and the station covered 78 % of its own
        broadcast with audio. Underruns were zero the whole time, because the engine always
        *had* audio — it just was not writing any.
        """
        await self._coordinator.publish(
            TrackFinished(
                at=self._clock.now(),
                track_id=item.track_id,
                played_seconds=item.duration_seconds if completed else 0.0,
                completed=completed,
                end_reason=reason,
            )
        )
        if item.entry is None:
            return

        try:
            self._finished.put_nowait((item, completed, reason))
        except asyncio.QueueFull:
            # The database is far behind. Dropping the *record* is bad; dropping the *audio*
            # would be worse, so the broadcast wins and this is loud.
            _log.error(
                "station.persistence_backlog_full",
                track_id=item.track_id,
                detail="play record dropped; the broadcast continues",
            )

        self._scheduler.note_track_aired()
        self._station_ids.note_track_aired()
        self._played_track_ids.add(item.track_id)
        self._history.insert(
            0,
            HistoryEntry(
                track_id=item.track_id,
                genre=item.entry.blueprint.composition.genre,
                secondary_genre=item.entry.blueprint.composition.secondary_genre,
                bpm=item.entry.blueprint.composition.bpm,
                musical_key=item.entry.blueprint.composition.key,
                duration_seconds=item.duration_seconds,
                is_instrumental=item.entry.blueprint.is_instrumental,
                vocal_style=item.entry.blueprint.vocal.style.value,
                primary_topic=item.entry.blueprint.lyrics.primary_topic,
                secondary_topic=item.entry.blueprint.lyrics.secondary_topic,
                persona_id=item.entry.blueprint.persona_id,
                blueprint_signature=item.entry.blueprint.signature(),
                energy_at_generation=item.entry.blueprint.market.energy,
                regime_at_generation=item.entry.planned_regime.value,
                created_at=item.entry.inserted_at,
                played_at=self._clock.now(),
            ),
        )
        del self._history[400:]

        if completed:
            await self._maybe_request_station_id()
        await self.persist()

    async def _persistence_worker(self) -> None:
        """Write what finished tracks imply, off the audio path.

        Everything in here is database work, and none of it is urgent to the millisecond — but
        all of it must happen, because §75's recovery reads it. Running it in its own task keeps
        the playout engine free to do nothing but write blocks.
        """
        while not self._coordinator.should_stop:
            item, completed, reason = await self._finished.get()
            try:
                await self._record_finished(item, completed, reason)
            except Exception as error:  # noqa: BLE001 - one bad record must not stop the rest
                _log.error(
                    "station.persistence_failed",
                    track_id=item.track_id,
                    error_type=type(error).__name__,
                    error=str(error),
                    exc_info=True,
                )
            finally:
                self._finished.task_done()

    async def _record_finished(
        self, item: PlayingItem, completed: bool, reason: str
    ) -> None:
        await self._advance_track(item.track_id, PLAYING_LIFECYCLE, reason="airing")
        async with self._database.session() as session:
            with contextlib.suppress(IllegalTransitionError, PersistenceError):
                await TrackRepository(session).mark_played(
                    item.track_id,
                    now=self._clock.now(),
                    completed=completed,
                    reason=reason,
                )
    async def _maybe_request_station_id(self) -> None:
        assessment = self.assess_buffer()
        if not self._scheduler.wants_station_id(assessment=assessment):
            return
        record = self._station_ids.select(
            category=StationIdCategory.GENERAL,
            regime=None if self._market is None else self._market.regime,
            session=None if self._market is None else self._market.session,
        )
        if record is None:
            return
        await self._coordinator.publish(
            StationIdRequested(
                at=self._clock.now(),
                category=record.category.value,
                reason="scheduled rotation",
                tracks_since_last=self._scheduler.tracks_since_station_id,
            )
        )
        self._playout.queue_station_id(record)

    # -- announcements -----------------------------------------------------

    async def _announce_buffer(self, assessment: BufferAssessment) -> None:
        level = assessment.level
        if level is self._last_buffer_level:
            return
        previous, self._last_buffer_level = self._last_buffer_level, level
        now = self._clock.now()
        snapshot = self._queue.snapshot()
        contract = assessment.to_contract(
            ready_count=snapshot.ready_count,
            in_flight_count=snapshot.depth - snapshot.ready_count,
        )

        if level is BufferLevel.HEALTHY and previous.is_degraded:
            degraded = (
                0.0
                if self._degraded_since is None
                else self._clock.monotonic() - self._degraded_since
            )
            self._degraded_since = None
            await self._coordinator.publish(
                RadioBufferRecovered(at=now, buffer=contract, degraded_seconds=degraded)
            )
            return

        if level.is_degraded and self._degraded_since is None:
            self._degraded_since = self._clock.monotonic()
        if level.is_urgent:
            await self._coordinator.publish(
                RadioBufferCritical(
                    at=now,
                    buffer=contract,
                    seconds_to_failure=assessment.seconds_to_failure,
                )
            )
        elif level is BufferLevel.LOW:
            await self._coordinator.publish(
                RadioBufferLow(at=now, buffer=contract, critical=False)
            )

    async def _announce_tier(self) -> None:
        tier = self._emergency.tier
        if tier is self._last_tier:
            return
        previous, self._last_tier = self._last_tier, tier
        now = self._clock.now()
        if tier is PlayoutTier.SCHEDULED:
            await self._coordinator.publish(
                PlayoutFallbackExited(
                    at=now,
                    tier=tier,
                    previous_tier=previous,
                    duration_seconds=max(0.0, self._clock.monotonic() - self._tier_since),
                )
            )
        else:
            await self._coordinator.publish(
                PlayoutFallbackEntered(
                    at=now,
                    tier=tier,
                    previous_tier=previous,
                    reason="scheduled programming unavailable",
                )
            )
        self._tier_since = self._clock.monotonic()

    # -- lookups -----------------------------------------------------------

    def _output_path_for(self, track_id: str) -> Path:
        return self._audio_dir / f"{track_id}.wav"

    def _blueprint_for(self, track_id: str) -> MusicBlueprintV1 | None:
        entry = self._queue.get(track_id)
        return None if entry is None else entry.blueprint


__all__ = [
    "EMERGENCY_STATE_KEY",
    "PERSIST_INTERVAL_SECONDS",
    "SCHEDULER_INTERVAL_SECONDS",
    "STATION_ID_STATE_KEY",
    "RadioStation",
    "StationStats",
]
