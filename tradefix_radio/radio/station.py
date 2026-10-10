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
from datetime import datetime
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from tradefix_radio.audio.analysis import EMBEDDING_VERSION
from tradefix_radio.audio.sinks import AudioSink
from tradefix_radio.config.schema import AppSettings
from tradefix_radio.contracts.enums import (
    MarketRegime,
    PlayoutTier,
    RunMode,
    StartupMode,
    StartupState,
    TrackProvenance,
    TransitionType,
)
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
    TrackRejected,
)
from tradefix_radio.contracts.lyrics import LyricsV1
from tradefix_radio.contracts.market import MarketStateV1
from tradefix_radio.contracts.music import MusicBlueprintV1
from tradefix_radio.contracts.queue import QueueLockLevel
from tradefix_radio.core.clock import Clock, SystemClock
from tradefix_radio.core.errors import (
    IllegalTransitionError,
    PersistenceError,
)
from tradefix_radio.core.job_states import TERMINAL_JOB_STATES
from tradefix_radio.core.state_machine import TrackState
from tradefix_radio.director.history import HistoryEntry, ProgrammingHistory
from tradefix_radio.director.memory import restore_director_state, save_director_state
from tradefix_radio.director.music_director import DirectorDecision, MusicDirector
from tradefix_radio.generation.manager import GenerationManager, GenerationOutcome
from tradefix_radio.lyrics.orchestrator import (
    LyricGenerationResult,
    LyricOrchestrator,
)
from tradefix_radio.persistence.database import Database
from tradefix_radio.persistence.models import Lyrics as LyricsRow
from tradefix_radio.persistence.repositories import (
    GenerationJobRepository,
    LyricsRepository,
    MemoryKeys,
    OriginalityRepository,
    PlayEventRepository,
    ProviderSubmissionsRepository,
    RadioMemoryRepository,
    TrackFileRepository,
    TrackRepository,
)
from tradefix_radio.persistence.repositories.queue import (
    PLAYING_POSITION,
    PersistedQueueSlot,
    QueueRepository,
)
from tradefix_radio.postprocess.pipeline import PipelineOutcome, PostProductionPipeline
from tradefix_radio.radio.buffer import BufferAssessment, BufferLevel, BufferMonitor
from tradefix_radio.radio.emergency import EmergencyManager
from tradefix_radio.radio.playout import AiredPlay, PlayingItem, PlayoutEngine
from tradefix_radio.radio.queue import QueueEntry, RadioQueue, ReadinessState
from tradefix_radio.radio.scheduler import Scheduler
from tradefix_radio.radio.startup import (
    StartupProgrammingPlanner,
    StartupProgress,
    StartupRequirements,
)
from tradefix_radio.radio.station_ids import StationIdCategory, StationIdLibrary
from tradefix_radio.runtime.coordinator import RuntimeCoordinator
from tradefix_radio.storage.paths import FileRole, StoragePaths
from tradefix_radio.storage.registrar import (
    FileRegistrar,
    RegisteredFile,
    StorageError,
)

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


#: Recent lyric rows loaded back into memory at start, for recovered generation jobs.
REHYDRATED_LYRICS = 500


def _lyrics_from_row(row: LyricsRow) -> LyricsV1:
    """Rebuild the lyric contract from its stored row.

    Only the fields the provider and the validator read. `lines` is not reconstructed:
    the section structure is recoverable from the text's own tags, and inventing line
    metadata that was never stored would be worse than omitting it.
    """
    return LyricsV1(
        track_id=row.track_id,
        text=row.text,
        primary_topic=row.primary_topic,
        secondary_topic=row.secondary_topic,
        format=row.lyric_format,
        perspective=row.perspective,
        tradefix_mentions=row.tradefix_mentions,
        educational_intensity=row.educational_intensity,
        concepts_used=tuple(row.concepts_used or ()),
    )


def _provenance_for(mode: RunMode) -> str:
    """The provenance class a run in this mode produces.

    Production is the only mode that makes broadcast radio. Development and simulation
    both drive the real architecture against a simulated market, and §72 is explicit that
    neither may be mistaken for the real thing — that applies to the novelty library as
    much as to the dashboard.
    """
    if mode is RunMode.PRODUCTION:
        return str(TrackProvenance.PRODUCTION_RADIO.value)
    if mode is RunMode.SIMULATION:
        return str(TrackProvenance.SIMULATION.value)
    return str(TrackProvenance.ENGINEERING_TEST.value)


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
#: Used **only** when no post-production pipeline is attached — the Phase 4 and Phase 5
#: configurations, where the states are walked so the §27 transition trail is complete and the
#: §46 page has something to show, rather than jumping to ``READY`` and leaving a gap.
#:
#: With a pipeline attached this constant is not used at all. §6.14 is explicit that generator
#: success is not a playable track, and a station that walked these states unconditionally
#: would be asserting that QC, originality and mastering had passed when none of them had run.
#: :meth:`RadioStation._run_post_production` advances the same states one at a time as each
#: stage *actually* completes.
READY_LIFECYCLE: tuple[TrackState, ...] = (
    TrackState.GENERATING,
    TrackState.GENERATED,
    TrackState.ANALYZING,
    TrackState.APPROVED,
    TrackState.MASTERING,
    TrackState.READY,
)

#: The prefix of :data:`READY_LIFECYCLE` that is true the moment audio exists on disk.
#:
#: Walked before post-production runs, because these two states say only "the generator
#: produced a file and we are now looking at it" — which is exactly what has happened.
GENERATED_LIFECYCLE: tuple[TrackState, ...] = (
    TrackState.GENERATING,
    TrackState.GENERATED,
    TrackState.ANALYZING,
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
    #: Tracks the generator produced successfully that post-production refused (§6.14).
    #:
    #: Counted apart from ``tracks_failed`` because the two mean different things to an
    #: operator: a generation failure is the model or the host misbehaving, while a
    #: post-production rejection is the station working exactly as designed.
    post_production_rejected: int = 0
    #: Vocal blueprints given validated lyrics, and those that could not be (B3).
    lyrics_composed: int = 0
    lyrics_failed: int = 0
    #: Vocals not even attempted because the buffer was at a suppressing level. Counted
    #: apart from failures: this is the station choosing, not the station struggling, and
    #: conflating them would make a healthy pressure response look like a defect.
    lyrics_suppressed: int = 0
    #: Rejections by reason, so the soak report can show *what* is being rejected.
    rejection_reasons: dict[str, int] = field(default_factory=dict)
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
        post_production: PostProductionPipeline | None = None,
        startup_mode: StartupMode = StartupMode.CONTROLLED_START,
    ) -> None:
        self._settings = settings
        self._database = database
        self._coordinator = coordinator
        self._director = director
        self._generation = generation
        self._clock = clock or SystemClock()
        self._audio_dir = audio_dir or settings.paths.generated_dir
        self._station_ids = station_ids
        #: Set when the active symbol changes, cleared when the identifier is queued.
        self._pending_market_switch: tuple[str, str] | None = None
        #: Composed lyrics by track id, for the generation request.
        #:
        #: Held in memory because `claim_and_generate` takes a *synchronous* callback and
        #: cannot await a database read. Populated when the lyric is composed and
        #: rehydrated for queued tracks on recovery, so a restart does not turn pending
        #: vocal tracks into instrumentals.
        self._composed_lyrics: dict[str, LyricsV1] = {}

        #: Composes and validates lyrics before anything reaches the provider (B3).
        #:
        #: Built here rather than inside the director because it needs the lyric history
        #: from the database, and a director that reached for a session would stop being
        #: testable against a list.
        self._lyrics = LyricOrchestrator(
            director.library,
            settings.lyrics,
            director.lyrics_director,
            director.selector,
        )

        #: Which provenance class the tracks this process plans belong to.
        #:
        #: Derived from the run mode rather than configured, because the honest answer is
        #: a property of how the station was started. A development or simulation run is
        #: not making radio anybody hears, and its output must not age the real library.
        self._provenance = _provenance_for(settings.mode)
        # Optional so the proven Phase 4 and Phase 5 configurations are untouched. When it is
        # absent the station behaves exactly as it did when those phases were accepted; when
        # it is present, §6.14's rule applies and nothing reaches READY without passing it.
        self._post_production = post_production

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

        # Fresh start programming (§FSP): ensure the station sounds fresh from track 1
        self._startup_mode = startup_mode
        self._startup = StartupProgrammingPlanner(
            StartupRequirements.from_settings(settings.radio),
            mode=startup_mode,
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
        #: Owns every audio file the station keeps (B5). Always present: a station that
        #: writes audio nobody recorded is the defect this replaces.
        self._storage = FileRegistrar(StoragePaths(settings.paths))

        #: When the item currently on air started. Stamped by `_on_track_started`.
        self._airing_started_at: datetime | None = None

        self._finished: asyncio.Queue[
            tuple[PlayingItem, bool, str, AiredPlay, datetime]
        ] = asyncio.Queue(
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
    def startup_progress(self) -> StartupProgress:
        """Current startup progress for Control Center display."""
        return self._startup.progress

    @property
    def startup_state(self) -> StartupState:
        """Current startup state."""
        return self._startup.state

    @property
    def is_priming(self) -> bool:
        """Whether the station is still in startup priming phase."""
        return self._startup.state in (
            StartupState.BOOTING,
            StartupState.MARKET_ACQUIRE,
            StartupState.GENERATOR_WARMING,
            StartupState.PRIMING,
        )

    @property
    def market(self) -> MarketStateV1 | None:
        return self._market

    @property
    def pending_market_switch(self) -> tuple[str, str] | None:
        """A market change noticed but not yet announced, as (previous, current)."""
        return self._pending_market_switch

    def set_market(self, state: MarketStateV1) -> None:
        """Feed the station a market state. The only input it takes from outside.

        A change of *symbol* is noted here rather than subscribed to as an event. The
        station takes one market input and the symbol arrives on it; reaching for a routing
        event would give it a second source for the same fact, and the two could disagree
        about which market the state in hand describes.
        """
        previous = self._market
        if previous is not None and previous.symbol != state.symbol:
            self._pending_market_switch = (previous.symbol, state.symbol)
        self._market = state

        # Notify startup planner when market is first acquired (§FSP)
        if previous is None and state is not None and self.is_priming:
            self._startup.on_market_acquired(state.symbol)

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
        """Start the station with fresh-start programming (§FSP).

        In CONTROLLED_START mode, playout is held until priming requirements are met.
        This ensures the listener hears fresh, never-before-played tracks from track 1.

        In LIVE_RECOVERY mode, playout starts immediately with emergency fallback
        while fresh tracks are generated - dead air is worse than temporary fallback.
        """
        self._startup.transition_to(StartupState.BOOTING)
        await self._coordinator.start()
        await self.recover()

        # Start generation and scheduling immediately - we need fresh tracks
        self._startup.transition_to(StartupState.MARKET_ACQUIRE)
        self._coordinator.spawn("generation-worker", self._generation_worker)
        self._coordinator.spawn("scheduler", self._scheduler_loop)
        self._coordinator.spawn("persistence", self._persistence_worker)

        # In live recovery, start playout immediately - dead air is worse than fallback
        if self._startup_mode is StartupMode.LIVE_RECOVERY:
            self._startup.transition_to(StartupState.ON_AIR)
            await self._playout.start()
            self._coordinator.spawn("playout", self._playout_loop)
            _log.info(
                "station.started_live_recovery",
                queue_depth=len(self._queue),
                ready_minutes=round(self._queue.ready_seconds() / 60, 1),
                detail="live recovery mode: playout started immediately",
            )
            return

        # Controlled start: wait for priming before playout
        # Generator warming will be signaled when ACE-Step is ready
        self._startup.transition_to(StartupState.GENERATOR_WARMING)

        # Note: Actual priming progress is tracked in _on_track_ready callbacks
        # The _startup_priming_loop monitors progress and starts playout when ready
        self._coordinator.spawn("startup-priming", self._startup_priming_loop)

        _log.info(
            "station.started_controlled",
            queue_depth=len(self._queue),
            ready_minutes=round(self._queue.ready_seconds() / 60, 1),
            reserve_minutes=round(self._emergency.reserve_minutes, 1),
            startup_mode=self._startup_mode.value,
            priming_enabled=self._startup.progress.fresh_tracks_required > 0,
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
            queue_repository = QueueRepository(session)
            slots = await queue_repository.load()
            # A slot dropped for want of a blueprint leaves its generation job pending,
            # and that job can never succeed — the provider is handed a blueprint and
            # there is nothing to hand it. Left alone it is claimed, fails, retries to
            # exhaustion and logs a traceback on every scheduling cycle, which is exactly
            # how it presented: a station that looked like its generator was broken while
            # it ground on five rows that could not possibly complete.
            #
            # Abandoned here rather than inside `classify_on_startup`, because this is
            # the only place that knows *which* slots were dropped. The job repository
            # would have to reach into the track schema to work it out, and would wrongly
            # condemn a job planned before its track row was written.
            orphaned = 0
            jobs = GenerationJobRepository(session)
            for track_id in queue_repository.dropped_track_ids:
                for job in await jobs.for_track(track_id):
                    if job.state in TERMINAL_JOB_STATES:
                        continue
                    if await jobs.mark_abandoned(
                        job.job_id,
                        now=self._clock.now(),
                        reason="the queue slot was dropped: no blueprint is stored",
                    ):
                        orphaned += 1
            if orphaned:
                _log.warning(
                    "station.orphaned_jobs_abandoned",
                    count=orphaned,
                    track_ids=queue_repository.dropped_track_ids[:10],
                )
            tracks = TrackRepository(session)
            history = await tracks.recent_history(limit=400)
            titles = await tracks.recent_titles(limit=500)
            # Continue the day's track-id numbering rather than restarting it; see
            # ``Scheduler.seed_sequence``.
            self._scheduler.seed_sequence(
                await tracks.next_daily_sequence(self._clock.now())
            )

        self._history = [_as_history_entry(entry) for entry in history]
        # Reversed to oldest-first. `recent_titles` returns newest-first, and the scheduler
        # grows this list by appending each title it plans and then reads `[-40:]` as "the
        # most recent forty" for §99's similarity rejection. Against a newest-first list that
        # slice returns the *oldest* forty — so the check compared each new title against
        # names from hundreds of tracks ago and never against the ones a listener had just
        # heard. Exact-collision checking uses the whole list and does not care about order.
        self._used_titles = list(reversed(titles))
        self._used_signatures = {entry.blueprint_signature for entry in history}
        self._played_track_ids = {entry.track_id for entry in history}

        entries = [_slot_to_entry(slot) for slot in slots if not slot.is_playing]
        playing_slot = next((slot for slot in slots if slot.is_playing), None)
        playing = None if playing_slot is None else _slot_to_entry(playing_slot)

        # A track that already aired must never return as new programming. It would be an
        # immediate, audible repeat, and §11's history would not catch it because the queue is
        # upstream of that check.
        # Also ask each slot's own track row. The history above is the last 400 airings by
        # ``last_played_at``, and a track whose later airings were never stamped there (a
        # reissued id collided with it) fell outside that window and came back on every
        # restart. Played is a fact about the track, not about its rank in a window.
        async with self._database.session() as session:
            tracks = TrackRepository(session)
            for entry in entries:
                if entry.track_id in self._played_track_ids:
                    continue
                track_row = await tracks.get(entry.track_id)
                if track_row is not None and track_row.state == TrackState.PLAYED.value:
                    self._played_track_ids.add(entry.track_id)
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

        # §75.1: Recover tracks that have master files from a previous session.
        # A PENDING entry with an existing master.wav is ready to play - the generation
        # completed but the station crashed before marking it READY. Without this check,
        # such tracks stay PENDING forever and the station falls back to procedural.
        async with self._database.session() as session:
            files_repo = TrackFileRepository(session)
            tracks_repo = TrackRepository(session)
            recovered_from_disk = 0
            for i, entry in enumerate(entries):
                if entry.readiness is not ReadinessState.PENDING:
                    continue
                master_path = await files_repo.live_path(entry.track_id, FileRole.MASTER)
                if master_path is None or not master_path.is_file():
                    continue
                # Get the track to read duration
                track = await tracks_repo.get(entry.track_id)
                if track is None:
                    continue
                entries[i] = replace(
                    entry,
                    readiness=ReadinessState.READY,
                    state=TrackState.READY,
                    audio_path=str(master_path),
                    duration_seconds=track.duration_seconds,
                )
                recovered_from_disk += 1
            if recovered_from_disk:
                _log.info(
                    "station.recovered_from_disk",
                    count=recovered_from_disk,
                    detail="tracks with existing master files marked ready",
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

        # Jobs that have nothing left to do must not be re-run. Start-up classification
        # requeues every job the dead process held, which is right for a track still being
        # made and wrong for two cases only this method can see: a track that already
        # aired (its slot was dropped above as resurrected) and a track whose restored slot
        # is READY with its audio on disk. Both were regenerated on every restart — minutes
        # of GPU time each, and the regenerated copy aired again as a repeat.
        finished = {e.track_id for e in resurrected} | {
            e.track_id for e in entries if e.readiness is ReadinessState.READY
        }
        if finished:
            async with self._database.session() as session:
                jobs = GenerationJobRepository(session)
                stale = 0
                for track_id in sorted(finished):
                    for job in await jobs.for_track(track_id):
                        if job.state in TERMINAL_JOB_STATES:
                            continue
                        if await jobs.mark_abandoned(
                            job.job_id,
                            now=self._clock.now(),
                            reason="the track already has its audio; nothing left to generate",
                        ):
                            stale += 1
            if stale:
                _log.info(
                    "station.finished_jobs_abandoned",
                    count=stale,
                    detail="jobs for tracks that already aired or are ready were not re-run",
                )

        # §FSP: a restored READY track has never aired — anything that had was dropped above —
        # so it is exactly the fresh programming controlled start waits for. Without crediting
        # it, the scheduler sees a full buffer and plans nothing, no generation ever completes,
        # priming never finishes, and a station holding ten minutes of fresh music stays silent.
        if self._startup_mode is StartupMode.CONTROLLED_START:
            credited = 0
            for entry in entries:
                if entry.readiness is not ReadinessState.READY:
                    continue
                blueprint = self._blueprint_for(entry.track_id)
                self._startup.on_fresh_track_ready(
                    track_id=entry.track_id,
                    duration_seconds=float(entry.duration_seconds or 0.0),
                    genre=blueprint.composition.genre if blueprint else "unknown",
                    persona_id=blueprint.persona_id if blueprint else None,
                )
                credited += 1
            if credited:
                _log.info(
                    "station.restored_tracks_credited_as_fresh",
                    count=credited,
                    detail="never-played tracks restored from the queue count toward priming",
                )
        self._stats.recovered_queue_entries = len(entries)

        # Rehydrate composed lyrics.
        #
        # Without this a restart silently converts every pending vocal track into an
        # instrumental: the words are safe in the database, but the synchronous callback
        # the generation manager uses can only read the in-memory map.
        #
        # Every recent lyric, not only those of restored slots. A recovered generation
        # job is not always behind a slot — a track dropped from the queue above (it had
        # already aired) or whose slot was lost still has a reclaimable job — and that job
        # ran with no words, the prompt builder refused to let the model invent its own,
        # and the "vocal" track came out instrumental. Loading the recent lyric rows costs
        # a few hundred small reads once per start and covers every job that can run.
        async with self._database.session() as session:
            lyrics_repository = LyricsRepository(session)
            for row in await lyrics_repository.recent(limit=REHYDRATED_LYRICS):
                self._composed_lyrics.setdefault(row.track_id, _lyrics_from_row(row))
            for entry in entries:
                if entry.track_id in self._composed_lyrics:
                    continue
                found = await lyrics_repository.get(entry.track_id)
                if found is not None:
                    self._composed_lyrics[entry.track_id] = _lyrics_from_row(found)

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

    async def _startup_priming_loop(self) -> None:
        """Monitor startup priming progress and start playout when ready (§FSP).

        In CONTROLLED_START mode, this loop:
        1. Waits for the generator to be ready
        2. Monitors fresh track generation progress
        3. Starts playout only when priming requirements are met

        This ensures the listener hears fresh music from track 1, never the same
        recognizable startup sound.
        """
        # Wait a moment for generator to initialize
        await self._clock.sleep(1.0)

        # Signal generator warming (ACE-Step should be loading)
        self._startup.on_generator_ready()

        # Monitor priming progress
        check_interval = 2.0  # Check every 2 seconds
        while not self._coordinator.should_stop:
            if self._startup.state is StartupState.READY_TO_AIR:
                # Priming complete! Start playout
                await self._playout.start()
                self._coordinator.spawn("playout", self._playout_loop)
                self._startup.transition_to(StartupState.ON_AIR)
                _log.info(
                    "station.priming_complete",
                    fresh_tracks=self._startup.progress.fresh_tracks_ready,
                    fresh_minutes=round(self._startup.progress.fresh_minutes_ready, 1),
                    time_to_ready=round(self._startup.progress.time_to_ready or 0, 1),
                    detail="listener-facing playout started with fresh programming",
                )
                return

            if self._startup.state is StartupState.ON_AIR:
                # Already on air (shouldn't happen in this loop but guard against it)
                return

            # Log priming progress
            progress = self._startup.progress
            _log.debug(
                "station.priming_progress",
                state=self._startup.state.value,
                fresh_tracks=f"{progress.fresh_tracks_ready}/{progress.fresh_tracks_required}",
                fresh_minutes=f"{progress.fresh_minutes_ready:.1f}/{progress.fresh_minutes_target:.1f}",
                progress_pct=round(progress.priming_progress_fraction * 100, 0),
            )

            await self._clock.sleep(check_interval)

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
        abandoned = await self._persist_tracks(planned)
        kept = [p for p in planned if p.blueprint.track_id not in abandoned]
        await self._publish_new_entries(kept)

        # No job for a track the lyric policy abandoned. Dropping the queue slot inside
        # `_persist_tracks` is not enough on its own: the job is planned *here*, after that
        # call, so the slot vanished and the GPU rendered the track anyway — an instrumental
        # nobody had asked for, under the one policy that exists to say "do not make this".
        for produced in kept:
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

    async def _persist_tracks(
        self, planned: Sequence[DirectorDecision]
    ) -> frozenset[str]:
        """Write the track row and its blueprint (§8, §37). Returns abandoned track ids.

        Not optional bookkeeping. Without it the queue persists slots whose blueprints are
        nowhere, so every one is dropped on restore — the first soak logged exactly that, and
        it meant queue recovery did not work at all despite the queue being saved.

        The return value exists because the lyric failure policy can decide a track should
        not be made at all, and the caller is the only place that can act on that: it plans
        the generation job, and it does so *after* this returns.
        """
        now = self._clock.now()
        provider = self._generation.describe_provider()
        abandoned: set[str] = set()
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
                    provenance=self._provenance,
                )
                if not await self._compose_lyrics(session, produced, now=now):
                    abandoned.add(produced.blueprint.track_id)
        return frozenset(abandoned)

    async def _compose_lyrics(
        self,
        session: AsyncSession,
        produced: DirectorDecision,
        *,
        now: datetime,
    ) -> bool:
        """Write validated lyrics for a vocal blueprint, or record why there are none.

        Returns whether the track should still be made. ``False`` only under the ``fail``
        lyric policy: every other path keeps the track, as an instrumental if it must.

        This is the orchestration B3 added, and its absence was invisible rather than
        loud: `AceStepPromptBuilder` sees a vocal blueprint with no stored lyric,
        correctly refuses to let the model invent words about markets, and silently
        downgrades the track to an instrumental. Every vocal blueprint the director
        produced became an instrumental, and the `lyrics` table was empty across the
        whole database.

        Composed here, with the track, rather than at generation time. Three reasons: the
        lyric is part of the creative decision and belongs beside the blueprint; §17
        validation costs nothing on the GPU and rejecting late would waste a generation;
        and the lyric is an originality input, so it has to exist before the candidate is
        compared against anything.
        """
        blueprint = produced.blueprint
        if blueprint.is_instrumental or not blueprint.lyrics.enabled:
            return True

        # §7.20 in the lyric direction: buffer pressure may change what the station *asks
        # for*, never what the audio must pass afterwards. A vocal track costs two
        # composition attempts, a validation pass, and — since the vocal profile — 2.2x the
        # GPU time. When the buffer is at the level the operator named, survival outranks
        # variety. Only for new requests: nothing already composed is thrown away, and a
        # recovered buffer restores vocals on its own.
        level = self.assess_buffer().level.value
        if level in self._settings.lyrics.suppress_vocals_at_buffer:
            self._stats.lyrics_suppressed += 1
            _log.info(
                "lyrics.suppressed_for_buffer",
                track_id=blueprint.track_id,
                buffer_level=level,
            )
            return True

        repository = LyricsRepository(session)
        try:
            result = self._lyrics.generate(
                blueprint,
                history=ProgrammingHistory(self._history),
                persona=self._director.library.personas.get(blueprint.persona_id or ""),
                previous_hashes=await repository.recent_hashes(),
                previous_shingles=await repository.recent_shingles(),
            )
        except Exception as error:  # noqa: BLE001 - a lyric must never stop scheduling
            # An orchestrator crash is a lyric failure, not a station failure. Left
            # unhandled it escaped `_persist_tracks` and took the whole schedule stage with
            # it: the test that found this measured 49 scheduling cycles, zero tracks
            # planned and a station living on procedural audio — a far worse outcome than
            # the instrumental the failure policy exists to choose.
            _log.error(
                "lyrics.orchestrator_failed",
                track_id=blueprint.track_id,
                error_type=type(error).__name__,
                error=str(error),
                exc_info=True,
            )
            result = LyricGenerationResult.crashed(blueprint, error)

        if result.usable and result.lyrics is not None:
            await repository.create(result.lyrics, now=now)
            self._composed_lyrics[blueprint.track_id] = result.lyrics
            self._stats.lyrics_composed += 1
            return True

        # No validated lyric. The provider must not be left to fill the gap, so the
        # choice is between an instrumental and abandoning the track, and it is
        # configuration rather than something decided here.
        self._stats.lyrics_failed += 1
        policy = self._settings.lyrics.on_lyric_failure
        _log.warning(
            "lyrics.unavailable",
            track_id=blueprint.track_id,
            mode=result.mode.value,
            failure=None if result.failure is None else result.failure.value,
            attempts=result.attempts,
            policy=policy,
            violations=(
                []
                if result.validation is None
                else [v.rule for v in result.validation.violations][:6]
            ),
        )
        if policy == "fail":
            # Drop the slot now rather than generating audio for a track that cannot be
            # realised as asked. The caller also needs to know, because it plans the
            # generation job after this returns — discarding the slot alone left the GPU
            # rendering a track whose whole point had just been abandoned.
            self._queue.discard(blueprint.track_id, force=True)
            return False
        # Instrumental fallback. The blueprint keeps its recorded intent — what it asked
        # for is part of the decision history — and the prompt builder already emits the
        # instrumental marker when no lyric is stored, so nothing further is needed to
        # make the audio match.
        return True

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
            lyrics_for=self._lyrics_for,
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
            #
            # `discard`, not `remove`: the slot may already be gone, because recovery drops
            # restored slots whose blueprint is missing while their jobs are still pending.
            # Raising there replaced the real generation failure with a `QueueError` from
            # inside the failure handler, on every cycle.
            with _reporting("drop permanently failed slot", track_id=outcome.track_id):
                self._queue.discard(outcome.track_id, force=True)
        return outcome

    async def _record_submission(self, outcome: GenerationOutcome) -> None:
        """Persist what the provider was actually sent for this attempt.

        §7.10 and §7.26 both want this, and the data had been assembled all along — the
        provider builds ``spec.as_metadata()`` under a docstring calling it "the record
        persisted with the track" and then hands it to a field nothing reads. The cost of
        that was a vocal track coming out instrumental with no way to tell, from the
        database alone, whether the lyric was never composed, never passed, or passed and
        ignored by the model.

        Failure here is logged and swallowed. This is a diagnostic; it must never be the
        reason a finished track does not reach air.
        """
        result = outcome.result
        if result is None:
            return
        try:
            async with self._database.session() as session:
                await ProviderSubmissionsRepository(session).record(
                    track_id=outcome.track_id,
                    provider=result.provider_name,
                    model_identifier=result.model_identifier,
                    detail=result.detail,
                    attempt=outcome.attempt,
                    now=self._clock.now(),
                )
        except Exception as error:  # noqa: BLE001 - diagnostics never block playout
            _log.warning(
                "station.submission_record_failed",
                track_id=outcome.track_id,
                error_type=type(error).__name__,
                error=str(error),
            )

    async def _register_raw(self, outcome: GenerationOutcome) -> RegisteredFile | None:
        """Take provider output into station ownership, or quarantine it.

        Returns ``None`` when the track cannot proceed, having already marked the slot
        unavailable. The caller treats that as "this track is over", which is the same
        thing the old unreadable-audio branch did — the difference is that the bytes are
        now kept as evidence under `quarantine/` instead of left in place looking like a
        normal render.
        """
        result = outcome.result
        if result is None:
            return None
        track_id = outcome.track_id
        now = self._clock.now()
        try:
            async with self._database.session() as session:
                return await self._storage.register(
                    repository=TrackFileRepository(session),
                    track_id=track_id,
                    role=FileRole.RAW_GENERATION,
                    source=Path(result.audio_path),
                    now=now,
                )
        except StorageError as error:
            _log.error(
                "station.raw_registration_failed",
                track_id=track_id,
                error=str(error),
                path=str(result.audio_path),
            )
            await self._quarantine(track_id, Path(result.audio_path), now=now)
            with _reporting("mark slot unavailable", track_id=track_id):
                self._queue.mark_unavailable(
                    track_id, reason="generated audio could not be taken into storage"
                )
            return None

    async def _quarantine(self, track_id: str, source: Path, *, now: datetime) -> None:
        """Move suspect audio somewhere it can be studied and never played.

        Best effort by design: quarantine is a diagnostic, and failing to file the
        evidence must not add a second failure on top of the one being recorded. A file
        that has already been consumed by the staging step simply is not there, which is
        why a missing source is not an error here.
        """
        if not await asyncio.to_thread(source.is_file):
            return
        try:
            async with self._database.session() as session:
                await self._storage.register(
                    repository=TrackFileRepository(session),
                    track_id=track_id,
                    role=FileRole.QUARANTINE,
                    source=source,
                    now=now,
                    verify_audio=False,
                )
        except (StorageError, Exception) as error:  # noqa: BLE001 - diagnostics never raise
            _log.warning(
                "station.quarantine_failed", track_id=track_id, error=str(error)
            )

    async def _accept_generated(self, outcome: GenerationOutcome) -> None:
        result = outcome.result
        track_id = outcome.track_id
        if result is None:
            return

        # Before anything can reject, rewrite or discard this track. What the model was
        # given is most worth having in exactly the cases that return early below.
        await self._record_submission(outcome)

        # Take ownership before anything downstream reads the file (B5).
        #
        # This both verifies the audio and records it. The two used to be separate: the
        # station called `read_info` to check the file decoded and then left it wherever
        # the provider had written it, with no row anywhere. 328 files and 8.68 GB
        # accumulated that way, none of them owned, which is why retention could not run
        # and why the library reported "Audio on disk" for tracks whose bytes it had never
        # heard of.
        registered = await self._register_raw(outcome)
        if registered is None:
            return
        measured = registered.duration_seconds or 0.0
        result = replace(result, audio_path=registered.path)
        outcome = replace(outcome, result=result)

        # §6.14: the generator producing a file is not the same thing as the station having a
        # playable track, and the distinction is enforced here rather than trusted. Without a
        # pipeline attached the old behaviour stands unchanged, which is what keeps the
        # accepted Phase 4 and Phase 5 configurations working exactly as they were proven.
        audio_path = result.audio_path
        novelty = 1.0
        if self._post_production is not None:
            approved = await self._run_post_production(outcome, measured)
            if approved is None:
                return
            audio_path, measured, novelty = approved

        try:
            self._queue.mark_ready(
                track_id,
                audio_path=str(audio_path),
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
        if self._post_production is None:
            await self._advance_track(track_id, READY_LIFECYCLE, reason="generated")
        else:
            # Every earlier state was advanced as its stage completed; only the final hop
            # remains, and it is taken here — after ``mark_ready`` succeeded — so a track is
            # never recorded as READY while the queue has refused it.
            await self._advance_track(
                track_id, (TrackState.READY,), reason="post-production approved"
            )

        # Notify startup planner about fresh track for §FSP priming
        # A newly generated track has never been played (play_count=0), so it qualifies as fresh
        if self.is_priming:
            blueprint = self._blueprint_for(track_id)
            self._startup.on_fresh_track_ready(
                track_id=track_id,
                duration_seconds=measured,
                genre=blueprint.composition.genre if blueprint else "unknown",
                persona_id=blueprint.persona_id if blueprint else None,
            )

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
                at=now, track_id=track_id, duration_seconds=measured, novelty_score=novelty
            )
        )

    async def _run_post_production(
        self, outcome: GenerationOutcome, measured: float
    ) -> tuple[Path, float, float] | None:
        """Run QC, originality and mastering; return the approved master or ``None`` (§6.14).

        Returns ``(master_path, duration_seconds, novelty_score)`` when the track is fit to
        broadcast. A ``None`` return means the track was rejected and the queue slot has
        already been removed, so the scheduler will replan into the gap.

        Runs on the generation worker's task, not the step loop and not playout. That is the
        right place for it: post-production is part of producing a track, it is the stage that
        should be occupied while a track is being validated, and ADR-11 gives playout its own
        task precisely so a long operation here cannot interrupt the broadcast.
        """
        from tradefix_radio.postprocess.pipeline import PipelineStage  # noqa: PLC0415

        assert self._post_production is not None
        result = outcome.result
        assert result is not None
        track_id = outcome.track_id

        await self._advance_track(track_id, GENERATED_LIFECYCLE, reason="generated")

        blueprint = self._blueprint_for(track_id)
        async with self._database.session() as session:
            originality = OriginalityRepository(session)
            library = await originality.load_library(exclude_track_id=track_id)
            lyrics_row = await session.get(LyricsRow, track_id)
            lyric_text = None if lyrics_row is None else lyrics_row.text
            tradefix_mentions = 0 if lyrics_row is None else lyrics_row.tradefix_mentions

        # The clock is held across the pipeline because this is a *leaf* of real work: it
        # awaits only threads and synchronous code, never the clock itself. Without the hold
        # an accelerated soak would charge ten seconds of real DSP against simulated time
        # racing ahead of it, and every generation after the first would look overdue.
        with self._clock.hold():
            pipeline_outcome = await self._post_production.process(
                track_id=track_id,
                source=result.audio_path,
                blueprint=blueprint,
                library=library,
                lyric_text=lyric_text,
                tradefix_mentions=tradefix_mentions,
                duplicate_hash_owner_lookup=self._duplicate_hash_owner,
                now=self._clock.now(),
            )

        with _reporting("record post-production evidence", track_id=track_id):
            await self._persist_pipeline_evidence(pipeline_outcome)

        if not pipeline_outcome.approved:
            reason = (
                pipeline_outcome.rejection_reason.value
                if pipeline_outcome.rejection_reason
                else "unknown"
            )
            self._stats.post_production_rejected += 1
            self._stats.rejection_reasons[reason] = (
                self._stats.rejection_reasons.get(reason, 0) + 1
            )
            _log.warning(
                "station.post_production_rejected",
                track_id=track_id,
                stage=pipeline_outcome.stage.value,
                reason=reason,
                detail=pipeline_outcome.detail[:400],
            )
            # Quarantine when the audio itself is broken and rejection when it is merely
            # unwanted. The distinction matters for retention: a quarantined file is kept for
            # inspection, a rejected one is an ordinary unused track.
            final_state = (
                TrackState.QUARANTINED
                if pipeline_outcome.stage
                in (PipelineStage.QC_REJECTED, PipelineStage.FINAL_QC_REJECTED)
                else TrackState.REJECTED
            )
            await self._advance_track(
                track_id,
                (final_state,),
                reason=f"{reason}: {pipeline_outcome.detail}"[:400],
            )
            # The slot can never be filled by this track. Dropping it keeps the queue's
            # projected duration honest and lets the scheduler plan a replacement, which is
            # what keeps a rejection from becoming dead air.
            #
            # ``force`` because §28's positional lock exists to stop the *scheduler* from
            # reprogramming an imminent slot for creative reasons — it was never meant to
            # compel the station to keep a track that cannot play. Without it, a rejection at
            # position 0 raised ``ProtectedItemError`` and the unplayable slot stayed at the
            # head of the queue, which is the dead air this phase exists to prevent. The
            # terminal-generation-failure path above reached the same conclusion already.
            with _reporting("drop rejected slot", track_id=track_id):
                self._queue.remove(track_id, force=True)
            await self._coordinator.publish(
                TrackRejected(
                    at=self._clock.now(),
                    track_id=track_id,
                    stage=pipeline_outcome.stage.value,
                    # The reason code first, then the sentence behind it. Both are kept
                    # because the code is what the Originality page groups by and the
                    # sentence is what an operator actually reads.
                    reasons=(reason, pipeline_outcome.detail[:400]),
                    novelty_score=pipeline_outcome.novelty_score,
                    closest_track_id=(
                        pipeline_outcome.similarity.closest.existing_track_id
                        if pipeline_outcome.similarity
                        and pipeline_outcome.similarity.closest
                        else None
                    ),
                )
            )
            return None

        await self._advance_track(
            track_id,
            (TrackState.APPROVED, TrackState.MASTERING),
            reason="post-production approved",
        )
        duration = (
            pipeline_outcome.features.duration_seconds
            if pipeline_outcome.features is not None
            else measured
        )

        # Take the master into ownership before the queue is told it exists (B5).
        #
        # The order is the point. `mark_ready` is what makes a track airable, and it must
        # not happen until there is a row saying which bytes will air. Registering after
        # would leave a window where the queue references audio the database does not
        # know about -- which is the state the whole station was in before B5.
        produced = pipeline_outcome.master_path
        if produced is None:
            # Mastering declined but post-production approved: the raw file airs. It is
            # already registered as RAW_GENERATION, so the queue still references owned
            # bytes and nothing further is needed.
            return Path(result.audio_path), duration, pipeline_outcome.novelty_score or 1.0

        try:
            async with self._database.session() as session:
                registered = await self._storage.register(
                    repository=TrackFileRepository(session),
                    track_id=track_id,
                    role=FileRole.MASTER,
                    source=Path(produced),
                    now=self._clock.now(),
                )
        except StorageError as error:
            # An unusable master is a post-production failure, not a storage footnote:
            # the one file the station was going to broadcast did not survive
            # verification.
            _log.error(
                "station.master_registration_failed",
                track_id=track_id,
                error=str(error),
                path=str(produced),
            )
            await self._quarantine(track_id, Path(produced), now=self._clock.now())
            with _reporting("mark slot unavailable", track_id=track_id):
                self._queue.mark_unavailable(
                    track_id, reason="master could not be taken into storage"
                )
            return None

        return (
            registered.path,
            registered.duration_seconds or duration,
            pipeline_outcome.novelty_score or 1.0,
        )

    async def _duplicate_hash_owner(self, canonical_hash: str) -> str | None:
        """Which track already owns this exact audio (§6.3).

        Passed into the pipeline as a callback rather than resolved up front because the
        lookup is an indexed point query over the whole table, and doing it eagerly for every
        candidate would load a hash the pipeline may never need.
        """
        async with self._database.session() as session:
            return await OriginalityRepository(session).canonical_hash_owner(canonical_hash)

    async def _persist_pipeline_evidence(self, outcome: PipelineOutcome) -> None:
        """Write every number behind the verdict, in one transaction (§6.1, §6.5).

        One transaction for the whole evidence trail: a partial write would read as though a
        stage had run and said nothing, which is worse than no record at all.
        """
        now = self._clock.now()
        async with self._database.session() as session:
            repository = OriginalityRepository(session)
            for qc_result in (outcome.raw_qc, outcome.final_qc):
                if qc_result is not None:
                    await repository.record_qc(
                        outcome.track_id,
                        qc_result,
                        elapsed_seconds=outcome.timings.get("qc", 0.0),
                        evaluated_at=now,
                    )
            if outcome.features is not None:
                await repository.record_features(
                    outcome.track_id, outcome.features, computed_at=now
                )
                blueprint = self._blueprint_for(outcome.track_id)
                await repository.record_fingerprint(
                    outcome.track_id,
                    features=outcome.features,
                    canonical_hash=outcome.canonical_hash or "",
                    file_sha256=outcome.file_hash or "",
                    fingerprint=outcome.fingerprint,
                    blueprint_signature=(
                        "" if blueprint is None else blueprint.signature()
                    ),
                    lyric_hash=(
                        None if outcome.lyrics is None else outcome.lyrics.content_hash
                    ),
                    embedding_version=EMBEDDING_VERSION,
                    computed_at=now,
                )
            if outcome.mastering is not None:
                await repository.record_mastering(
                    outcome.track_id, outcome.mastering, mastered_at=now
                )
            if outcome.similarity is not None:
                await repository.record_similarity(
                    outcome.track_id,
                    outcome.similarity,
                    evaluated_at=now,
                    resolution=outcome.review,
                )
            if outcome.lyrics is not None:
                await repository.record_lyric_fingerprint(outcome.lyrics, computed_at=now)

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
        # Stamped here rather than derived later from `now - played_seconds`. The two agree
        # for a clean airing and disagree for an interrupted one, and the start of a play is
        # a fact the station observed rather than one it should reconstruct.
        self._airing_started_at = self._clock.now()
        if item.is_station_id and item.station_id is not None:
            self._station_ids.note_played(item.station_id.key, now=self._clock.now())
            self._scheduler.note_station_id_played()

    async def _on_track_finished(
        self, item: PlayingItem, completed: bool, reason: str, aired: AiredPlay
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
                # Measured, not inferred. This was `duration if completed else 0.0`,
                # which reported a track cut at 2:58 of 3:00 as zero seconds of airtime.
                played_seconds=aired.played_seconds,
                completed=completed,
                end_reason=reason,
            )
        )
        # Station identities are airtime but not programming (§31 counts them separately),
        # so they are the one thing not recorded as a play. Everything else is -- including
        # Tier 2 reserve and Tier 3 procedural, which have no queue entry and no track row.
        # Excluding them would make "how much of the hour was real music" unanswerable,
        # which is exactly the question the emergency tiers exist to raise.
        if item.is_station_id:
            return

        started_at = self._airing_started_at or self._clock.now()
        try:
            self._finished.put_nowait((item, completed, reason, aired, started_at))
        except asyncio.QueueFull:
            # The database is far behind. Dropping the *record* is bad; dropping the *audio*
            # would be worse, so the broadcast wins and this is loud.
            _log.error(
                "station.persistence_backlog_full",
                track_id=item.track_id,
                detail="play record dropped; the broadcast continues",
            )

        if item.entry is None:
            # Emergency audio is recorded as a play above, but it is not programming: it
            # must not advance the station-id rotation, the diversity history, or the
            # "tracks aired" counter the scheduler paces itself against.
            return

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
            item, completed, reason, aired, started_at = await self._finished.get()
            try:
                await self._record_finished(
                    item, completed, reason, aired, started_at
                )
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
        self,
        item: PlayingItem,
        completed: bool,
        reason: str,
        aired: AiredPlay,
        started_at: datetime,
    ) -> None:
        now = self._clock.now()
        async with self._database.session() as session:
            # The airing record comes first, and it is written for every tier.
            #
            # `play_events` had a contract, a model, a table and five indexes and no writer
            # at all: 247 tracks played, zero rows. `tracks.play_count` and `last_played_at`
            # are denormalised separately, which is why rotation worked and why the gap was
            # invisible until something asked a question only these rows can answer -- how
            # much of the hour was real music, and whether §75's "never record an incomplete
            # airing as played" actually held.
            with contextlib.suppress(PersistenceError):
                await PlayEventRepository(session).record(
                    track_id=item.track_id,
                    tier=item.tier.value,
                    started_at=started_at,
                    ended_at=now,
                    played_seconds=aired.played_seconds,
                    completed=completed,
                    transition_in=aired.transition_in,
                    end_reason=reason[:48],
                    regime_at_play=(
                        None if self._market is None else self._market.regime.value
                    ),
                    symbol_at_play=None if self._market is None else self._market.symbol,
                )

        if item.entry is None:
            # Emergency audio: recorded as airtime, but it has no track row to advance.
            return

        await self._advance_track(item.track_id, PLAYING_LIFECYCLE, reason="airing")
        async with self._database.session() as session:
            tracks = TrackRepository(session)
            with contextlib.suppress(IllegalTransitionError, PersistenceError):
                await tracks.mark_played(
                    item.track_id,
                    now=now,
                    completed=completed,
                    reason=reason,
                )
            # Airing is what makes a track production history, and only here.
            #
            # A station generates far more candidates than it plays, so promoting on
            # approval or on queueing would fill the novelty library with music nobody
            # heard — the exact shape of the defect where 154 bench tracks aged real
            # output. The run mode gate matters just as much: a simulation run drives the
            # whole architecture and airs to a null sink, and §72 is explicit that it must
            # never be mistaken for the real thing.
            if self._provenance == TrackProvenance.PRODUCTION_RADIO.value:
                with contextlib.suppress(PersistenceError):
                    await tracks.promote_to_production(item.track_id, now=now)
    async def _maybe_request_station_id(self) -> None:
        # A market switch gets an identifier of its own, ahead of the rotation and
        # regardless of how long it has been since the last one. The listener has just had
        # the subject of the songs changed underneath them; saying so is the one case where
        # an identifier carries information rather than branding.
        #
        # Cleared whether or not a clip was found, so an unrecorded library does not leave
        # the station trying again on every cycle until the next switch.
        if self._pending_market_switch is not None:
            previous, current = self._pending_market_switch
            self._pending_market_switch = None
            record = self._station_ids.select(
                category=StationIdCategory.MARKET_SWITCH,
                regime=None if self._market is None else self._market.regime,
                session=None if self._market is None else self._market.session,
            )
            if record is not None and record.category is StationIdCategory.MARKET_SWITCH:
                await self._coordinator.publish(
                    StationIdRequested(
                        at=self._clock.now(),
                        category=record.category.value,
                        reason=f"active market changed {previous} -> {current}",
                        tracks_since_last=self._scheduler.tracks_since_station_id,
                    )
                )
                self._playout.queue_station_id(record)
                return

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

    def _lyrics_for(self, track_id: str) -> LyricsV1 | None:
        """The composed lyric for a track, for the generation request.

        The last link in the production lyric path, and the one that was missing.
        `GenerationRequest.lyrics` has always existed and `claim_and_generate` has always
        accepted a `lyrics_for` callback — nothing ever passed one, so the field was
        always `None`, the prompt builder saw a vocal blueprint with no words, and
        correctly refused to let the model invent its own. Every vocal track came out
        instrumental, which is exactly what the first real one did until this was wired.
        """
        return self._composed_lyrics.get(track_id)

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
