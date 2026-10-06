"""The playout engine (§30, §31, §33, milestone 4.6).

Its entire job: **play valid audio continuously**.

What it deliberately does not know: ACE-Step, the music director, blueprints, market regimes,
genres. It reads a queue of files and writes blocks to a sink. §86 requires that a model crash
cannot stop the radio, and the cheapest way to guarantee that is for the audio path to hold no
reference to a model — not to catch its exceptions carefully.

Three design points.

**Generation never happens here.** Not even indirectly. The engine asks the queue what is ready;
if nothing is, it asks the emergency manager. A render on this path would stall block delivery
and underrun the sink, which is the one failure the engine exists to prevent.

**The sink paces playback, not the engine.** ``write`` returns when the sink is ready for more.
An engine that slept for the block duration itself would drift against the device's real clock —
the classic cause of a slow underrun over hours — and would make §64's accelerated runs
impossible without a second code path.

**Silence is measured, not assumed away.** Every gap between writes is accounted for and
reported as :attr:`PlayoutStats.unintended_silence_seconds`. A station that merely *tries* not
to produce dead air has no way to prove it did not, and the soak's headline invariant is exactly
that number being zero.
"""

from __future__ import annotations

import asyncio
import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from tradefix_radio.audio.ducking import DuckingConfig, DuckingController
from tradefix_radio.audio.format import PLAYOUT_CHANNELS, PLAYOUT_SAMPLE_RATE, conform
from tradefix_radio.audio.io import read_audio
from tradefix_radio.audio.mixer import TransitionDecision, TransitionPlanner, crossfade
from tradefix_radio.audio.pcm import AudioBuffer
from tradefix_radio.audio.sinks import AudioSink
from tradefix_radio.contracts.enums import PlayoutTier, TransitionType
from tradefix_radio.core.clock import Clock, SystemClock
from tradefix_radio.core.errors import AudioError, AudioSinkError
from tradefix_radio.radio.emergency import EmergencyManager
from tradefix_radio.radio.queue import QueueEntry, RadioQueue
from tradefix_radio.radio.station_ids import StationIdRecord

_log = structlog.get_logger(__name__)

#: Audio handed to the sink per write, in seconds.
#:
#: Not samples: the figure that matters is how long the engine goes between decisions, and
#: that is a time. One second is long enough that an accelerated run does not spend all its
#: steps in the sink, and short enough that an operator skip takes effect without a wait.
DEFAULT_BLOCK_SECONDS = 1.0

#: Consecutive sink failures after which the engine stops trying to reopen it.
#:
#: Reopening is worth attempting — a USB interface that dropped out often comes back — but an
#: unbounded retry against a device that is gone produces a tight loop of failures and no audio,
#: which is worse than failing loudly.
MAX_SINK_REOPEN_ATTEMPTS = 5


class PlayoutState(str, enum.Enum):
    """What the engine is doing. Reported to §41, used by nothing internally."""

    STOPPED = "stopped"
    STARTING = "starting"
    PLAYING = "playing"
    PAUSED = "paused"
    DRAINING = "draining"
    FAILED = "failed"


@dataclass(frozen=True)
class PlayingItem:
    """Whatever is currently on air, whichever tier produced it.

    One type for all three tiers so the engine's loop has no per-tier branching beyond
    acquiring the audio. A Tier 3 block has no track id in the database and no blueprint, which
    is why those are optional here rather than the engine carrying three parallel notions of
    "the current thing".
    """

    track_id: str
    audio: AudioBuffer
    tier: PlayoutTier
    entry: QueueEntry | None = None
    station_id: StationIdRecord | None = None

    @property
    def duration_seconds(self) -> float:
        return self.audio.duration_seconds

    @property
    def is_station_id(self) -> bool:
        return self.station_id is not None


@dataclass
class PlayoutStats:
    """What actually happened on air. The soak report reads this directly."""

    #: Music only. Station identities have their own counter — see ``station_ids_played``.
    tracks_started: int = 0
    tracks_completed: int = 0
    tracks_skipped: int = 0
    station_ids_played: int = 0
    transitions: int = 0
    transition_failures: int = 0
    blocks_written: int = 0
    seconds_on_air: float = 0.0
    #: The headline invariant: any period where the engine wanted audio and had none.
    unintended_silence_seconds: float = 0.0
    #: Intentional startup hold (§FSP): time spent waiting for fresh tracks in controlled
    #: start mode. NOT counted as unintended silence — this is deliberate priming.
    startup_hold_seconds: float = 0.0
    underruns: int = 0
    sink_errors: int = 0
    sink_reopens: int = 0
    peak_sample: float = 0.0
    #: Outward callbacks that raised. Counted rather than fatal — see ``_notify``.
    hook_errors: int = 0
    #: Tracks started per tier. Music only — station identities are not tracks.
    by_tier: dict[str, int] = field(default_factory=dict)

    def record_tier(self, tier: PlayoutTier) -> None:
        self.by_tier[tier.value] = self.by_tier.get(tier.value, 0) + 1


@dataclass(frozen=True)
class AiredPlay:
    """What an item measured on its way off air.

    Separate from :class:`PlayingItem` because these are facts about *this airing* rather
    than about the thing aired, and separate from the hook's positional arguments so that
    adding another measurement later does not change the hook's arity again.

    ``played_seconds`` is counted from frames the sink actually accepted, not from the
    item's nominal duration and not from wall-clock timestamps. That distinction is the
    whole reason this type exists: a track skipped at 2:58 of 3:00 played 178 seconds, and
    the bus event that predates this recorded it as 0.0.
    """

    #: Audio actually written for this item.
    played_seconds: float
    #: The §30 transition that brought it in; ``cold_open`` when nothing preceded it.
    transition_in: str


#: Called when a track finishes. The engine's only outward dependency beyond the sink, and a
#: callback rather than a reference so it cannot acquire a second one.
TrackFinishedHook = Callable[[PlayingItem, bool, str, AiredPlay], Awaitable[None]]
TrackStartedHook = Callable[[PlayingItem], Awaitable[None]]


class PlayoutEngine:
    """Plays audio continuously, whatever else is broken."""

    def __init__(
        self,
        *,
        sink: AudioSink,
        queue: RadioQueue,
        emergency: EmergencyManager,
        clock: Clock | None = None,
        transitions: TransitionPlanner | None = None,
        block_seconds: float = DEFAULT_BLOCK_SECONDS,
        sample_rate: int = PLAYOUT_SAMPLE_RATE,
        channels: int = PLAYOUT_CHANNELS,
        on_track_started: TrackStartedHook | None = None,
        on_track_finished: TrackFinishedHook | None = None,
    ) -> None:
        if sink.sample_rate != sample_rate:
            raise ValueError(
                f"sink runs at {sink.sample_rate} Hz but the playout format is {sample_rate} Hz; "
                "conversion belongs at the input boundary, not between the engine and the sink"
            )
        self._sink = sink
        self._queue = queue
        self._emergency = emergency
        self._clock = clock or SystemClock()
        self._transitions = transitions or TransitionPlanner()
        self._block_seconds = block_seconds
        self._sample_rate = sample_rate
        self._channels = channels
        self._on_started = on_track_started
        self._on_finished = on_track_finished

        self._state = PlayoutState.STOPPED
        self._current: PlayingItem | None = None
        # The item that just finished. Needed because ``_finish`` clears ``_current`` before
        # the next ``_begin`` runs, so reading ``_current`` for "what preceded this" always saw
        # ``None`` — and the §30 transition planner was never consulted once. The soak reported
        # 20 tracks and 0 transitions, which is what made it visible.
        self._previous: PlayingItem | None = None
        self._position_frames = 0
        #: The §30 transition that brought the current item in. See `AiredPlay`.
        self._transition_in = "cold_open"
        self._pending_station_id: StationIdRecord | None = None
        self._stats = PlayoutStats()
        self._skip_requested = False
        self._stop_requested = False
        self._paused = False
        self._state_before_pause: PlayoutState | None = None
        self._sink_failures = 0
        self._ducking = DuckingController()

    # -- introspection -----------------------------------------------------

    @property
    def ducking(self) -> DuckingController:
        """Voice-activated ducking controller."""
        return self._ducking

    @property
    def state(self) -> PlayoutState:
        return self._state

    def by_tier_count(self, tier: PlayoutTier) -> int:
        """Tracks started from ``tier``. Music only — identities are not tracks."""
        return self._stats.by_tier.get(tier.value, 0)

    @property
    def stats(self) -> PlayoutStats:
        return self._stats

    @property
    def current(self) -> PlayingItem | None:
        return self._current

    @property
    def previous(self) -> PlayingItem | None:
        """The item that finished most recently. What the next transition is planned from."""
        return self._previous

    @property
    def tier(self) -> PlayoutTier:
        return self._emergency.tier

    @property
    def position_seconds(self) -> float:
        """How far into the current item playback has reached."""
        return self._position_frames / self._sample_rate

    @property
    def elapsed_seconds(self) -> float:
        """Total audio written since the engine started. The station's play clock.

        Counted in frames rather than measured in wall time, so it stays exact under a virtual
        clock and does not drift under a real one.
        """
        return self._sink.frames_written / self._sample_rate

    # -- control -----------------------------------------------------------

    def request_skip(self) -> None:
        """§44's operator skip. Takes effect at the next block boundary."""
        self._skip_requested = True

    def request_stop(self) -> None:
        self._stop_requested = True

    def pause(self) -> bool:
        """Pause playback. Returns True if paused, False if already paused or stopped."""
        if self._paused or self._state == PlayoutState.STOPPED:
            return False
        self._paused = True
        self._state_before_pause = self._state
        self._state = PlayoutState.PAUSED
        _log.info("playout.paused")
        return True

    def resume(self) -> bool:
        """Resume playback. Returns True if resumed, False if not paused."""
        if not self._paused:
            return False
        self._paused = False
        self._state = self._state_before_pause or PlayoutState.PLAYING
        self._state_before_pause = None
        _log.info("playout.resumed")
        return True

    @property
    def is_paused(self) -> bool:
        """Whether playback is currently paused."""
        return self._paused

    def queue_station_id(self, record: StationIdRecord) -> None:
        """Air an identifier after the current item (§31)."""
        self._pending_station_id = record

    # -- the loop ----------------------------------------------------------

    async def start(self) -> None:
        await self._sink.open()
        self._state = PlayoutState.STARTING
        self._stop_requested = False
        _log.info(
            "playout.started",
            sink=self._sink.name,
            sample_rate=self._sample_rate,
            block_seconds=self._block_seconds,
        )

    async def stop(self) -> None:
        self._state = PlayoutState.DRAINING
        await self._sink.close()
        self._state = PlayoutState.STOPPED
        _log.info(
            "playout.stopped",
            seconds_on_air=round(self._stats.seconds_on_air, 1),
            tracks=self._stats.tracks_completed,
            unintended_silence=round(self._stats.unintended_silence_seconds, 3),
        )

    async def run(self) -> None:
        """Play until stopped. The station's main loop."""
        await self.start()
        try:
            while not self._stop_requested:
                await self.pump()
        finally:
            await self.stop()

    async def pump(self) -> None:
        """Advance playback by one block. The whole engine, in one method.

        Separated from :meth:`run` so a test or the soak driver can step the station
        deterministically rather than racing a background loop.

        The body runs inside ``clock.hold()``. On a real clock that is a no-op. On a virtual
        one it stops the driver advancing time while this method is between sleeps, which is
        what keeps an accelerated run's audio coverage honest — see
        :meth:`~tradefix_radio.core.clock.VirtualClock.hold`.
        """
        with self._clock.hold():
            await self._pump_once()

    async def _pump_once(self) -> None:
        # If paused, just sleep without advancing playback
        if self._paused:
            await self._clock.sleep(self._block_seconds)
            return

        if self._current is None:
            acquired = await self._acquire_next()
            if acquired is None:
                # Nothing from any tier. This is the failure the whole subsystem exists to
                # prevent, so it is counted rather than slept through — and the engine still
                # yields, because spinning would starve whatever is trying to fix it.
                self._stats.underruns += 1
                self._stats.unintended_silence_seconds += self._block_seconds
                _log.error(
                    "playout.underrun",
                    detail="no audio available from any tier",
                    tier=self._emergency.tier.value,
                    seconds=self._block_seconds,
                )
                await self._clock.sleep(self._block_seconds)
                return
            await self._begin(acquired)

        await self._write_next_block()

    # -- acquisition -------------------------------------------------------

    async def _acquire_next(self) -> PlayingItem | None:
        """Get the next thing to play, escalating tiers as needed (§33)."""
        pending = self._pending_station_id
        if pending is not None:
            self._pending_station_id = None
            item = self._load_station_id(pending)
            if item is not None:
                return item

        tier = self._emergency.choose_tier(
            tier1_available=self._tier1_ready(),
            monotonic_now=self._clock.monotonic(),
        )

        if tier is PlayoutTier.SCHEDULED:
            item = self._take_from_queue()
            if item is not None:
                return item
            # The queue said it had something and then did not. Re-ask the emergency manager
            # rather than returning nothing, or a race between the scheduler and playout would
            # show up as dead air.
            tier = self._emergency.choose_tier(
                tier1_available=False,
                monotonic_now=self._clock.monotonic(),
                reason="queue head became unplayable between the check and the read",
            )

        if tier is PlayoutTier.EMERGENCY_RESERVE:
            reserved = self._emergency.next_reserve_track()
            if reserved is not None:
                track, audio = reserved
                return PlayingItem(
                    track_id=track.track_id,
                    audio=audio,
                    tier=PlayoutTier.EMERGENCY_RESERVE,
                )
            tier = self._emergency.choose_tier(
                tier1_available=False,
                monotonic_now=self._clock.monotonic(),
                reason="emergency reserve exhausted",
            )

        if tier is PlayoutTier.PROCEDURAL:
            block = self._emergency.next_procedural_block()
            # Include session ID in track name for forensic identification
            # Each startup has a unique session, so procedural-abc123-000001 is distinct
            # from procedural-def456-000001 in a different session
            session_id = self._emergency.procedural_session_id
            block_num = self._emergency.stats.tier3_blocks_played
            return PlayingItem(
                track_id=f"procedural-{session_id}-{block_num:06d}",
                audio=conform(block, sample_rate=self._sample_rate, channels=self._channels),
                tier=PlayoutTier.PROCEDURAL,
            )
        return None

    def _tier1_ready(self) -> bool:
        return any(entry.is_playable for entry in self._queue)

    def _take_from_queue(self) -> PlayingItem | None:
        """Pop the next playable slot and load its audio.

        A slot whose file has gone missing is marked ``UNAVAILABLE`` and skipped *loudly* —
        §36's retention sweeper and a disk fault both produce this, and silently stepping over
        it would make a storage problem look like a short queue.
        """
        for _ in range(len(self._queue) + 1):
            entry = self._queue.begin_playing(self._clock.now())
            if entry is None:
                return None
            if entry.audio_path is None:
                self._queue.finish_playing()
                self._stats.tracks_skipped += 1
                continue
            try:
                audio = conform(
                    read_audio(Path(entry.audio_path)),
                    sample_rate=self._sample_rate,
                    channels=self._channels,
                )
            except AudioError as error:
                self._queue.finish_playing()
                self._stats.tracks_skipped += 1
                _log.error(
                    "playout.track_unreadable",
                    track_id=entry.track_id,
                    path=entry.audio_path,
                    error=str(error),
                )
                continue
            return PlayingItem(
                track_id=entry.track_id,
                audio=audio,
                tier=entry.tier,
                entry=entry,
            )
        return None

    def _load_station_id(self, record: StationIdRecord) -> PlayingItem | None:
        try:
            audio = conform(
                read_audio(record.audio_path),
                sample_rate=self._sample_rate,
                channels=self._channels,
            )
        except AudioError as error:
            # A missing identifier is cosmetic. Falling through to music is the right response;
            # stalling the station over branding would not be.
            _log.warning(
                "playout.station_id_unreadable",
                key=record.key,
                path=str(record.audio_path),
                error=str(error),
            )
            return None
        return PlayingItem(
            track_id=f"station-id-{record.key}",
            audio=audio,
            tier=PlayoutTier.SCHEDULED,
            station_id=record,
        )

    # -- playback ----------------------------------------------------------

    async def _begin(self, item: PlayingItem) -> None:
        """Start an item, applying the §30 transition from whatever preceded it."""
        previous = self._previous
        self._current = item
        self._position_frames = 0
        # Overwritten by `_apply_transition` when something preceded this item. The default
        # is the honest answer for the first item after a start or a recovery: nothing was
        # faded out of, so no transition was planned.
        self._transition_in = "cold_open"
        self._state = PlayoutState.PLAYING
        if item.is_station_id:
            # Counted separately, and deliberately not as a track. A station identity is
            # airtime that is not programming, so folding it into ``tracks_started`` would
            # overstate how much music the station aired — by a sixth, at the default
            # ``station_id_every_n_tracks`` — in §50's panel, in the soak report and in any
            # gate that reasons about tracks per hour.
            self._stats.station_ids_played += 1
        else:
            self._stats.tracks_started += 1
            # Counted here, inside the non-identity branch, so ``by_tier`` answers "how many
            # *tracks* came from each tier" rather than "how many items aired". The gates and
            # the soak both need the former, and deriving it by subtracting Tier 3 blocks from
            # the completed count went negative the moment a procedural block was still on air
            # when the figures were read.
            self._stats.record_tier(item.tier)

        if previous is not None:
            self._apply_transition(previous, item)

        _log.info(
            "playout.track_started",
            track_id=item.track_id,
            tier=item.tier.value,
            duration_seconds=round(item.duration_seconds, 1),
            station_id=item.is_station_id,
        )
        if self._on_started is not None:
            await self._notify(self._on_started(item), hook="on_track_started")

    def _apply_transition(self, outgoing: PlayingItem, incoming: PlayingItem) -> None:
        """Decide the §30 transition and record it.

        The *decision* is recorded here; the crossfade itself is applied by
        :meth:`crossfade_into`, which the caller uses when it has both buffers. Splitting them
        keeps the engine's loop able to play a track it is streaming rather than holding two
        whole tracks in memory to overlap them.
        """
        try:
            decision = self._transitions.plan(
                outgoing_bpm=_bpm_of(outgoing),
                incoming_bpm=_bpm_of(incoming) or 120,
                outgoing_energy=_energy_of(outgoing),
                incoming_energy=_energy_of(incoming) or 0.5,
                incoming_is_station_id=incoming.is_station_id,
                outgoing_duration_seconds=outgoing.duration_seconds,
                incoming_duration_seconds=incoming.duration_seconds,
            )
            self._stats.transitions += 1
            self._transition_in = decision.transition.value
            _log.debug(
                "playout.transition",
                from_track=outgoing.track_id,
                to_track=incoming.track_id,
                transition=decision.transition.value,
                seconds=round(decision.seconds, 2),
            )
        except Exception as error:  # noqa: BLE001 - a bad transition must not stop playback
            self._stats.transition_failures += 1
            _log.error(
                "playout.transition_failed",
                from_track=outgoing.track_id,
                to_track=incoming.track_id,
                error=str(error),
                detail="falling through to a hard cut",
            )
            # Record what actually happened, not what was asked for. The planner failed, so
            # the audio is a hard cut, and the play record has to say so.
            self._transition_in = "hard_cut"

    def crossfade_into(
        self, outgoing: AudioBuffer, incoming: AudioBuffer, decision: TransitionDecision
    ) -> AudioBuffer:
        """Join two buffers using a planned transition (§30)."""
        if decision.is_hard_cut:
            return outgoing.concat(incoming)
        return crossfade(outgoing, incoming, seconds=decision.seconds)

    async def _write_next_block(self) -> None:
        """Write one block of the current item, finishing it if that was the last."""
        item = self._current
        if item is None:
            return

        block_frames = max(1, int(self._block_seconds * self._sample_rate))
        remaining = item.audio.frames - self._position_frames
        if remaining <= 0 or self._skip_requested:
            await self._finish(completed=remaining <= 0)
            return

        take = min(block_frames, remaining)
        block = item.audio.slice_frames(
            self._position_frames, self._position_frames + take
        )
        written = await self._write(block)
        if written:
            self._position_frames += take
            self._stats.blocks_written += 1
            self._stats.seconds_on_air += block.duration_seconds
            self._stats.peak_sample = max(self._stats.peak_sample, block.peak())
        if self._position_frames >= item.audio.frames:
            await self._finish(completed=True)

    async def _write(self, block: AudioBuffer) -> bool:
        """Write to the sink, attempting one reopen on failure.

        A sink failure is recoverable surprisingly often — a USB interface that dropped out, a
        pipe that was restarted — so one reopen is worth trying. Unbounded retrying is not: a
        device that is genuinely gone would produce a tight loop of failures and no audio, which
        is worse than failing loudly.
        """
        # Apply voice ducking if enabled
        if self._ducking.enabled and self._ducking.gain < 0.999:
            block = block.scaled(self._ducking.gain)

        try:
            await self._sink.write(block)
        except AudioSinkError as error:
            self._stats.sink_errors += 1
            self._sink_failures += 1
            self._stats.unintended_silence_seconds += block.duration_seconds
            _log.error(
                "playout.sink_write_failed",
                sink=self._sink.name,
                error=str(error),
                consecutive=self._sink_failures,
            )
            if self._sink_failures > MAX_SINK_REOPEN_ATTEMPTS:
                self._state = PlayoutState.FAILED
                return False
            await self._reopen_sink()
            return False
        else:
            self._sink_failures = 0
            return True

    async def _reopen_sink(self) -> None:
        try:
            await self._sink.close()
            await self._sink.open()
            self._stats.sink_reopens += 1
            _log.warning("playout.sink_reopened", sink=self._sink.name)
        except AudioSinkError as error:
            _log.error(
                "playout.sink_reopen_failed", sink=self._sink.name, error=str(error)
            )

    async def _finish(self, *, completed: bool) -> None:
        item = self._current
        if item is None:
            return
        reason = "completed" if completed else "skipped"
        # Measured *before* the reset below, which is where this number used to be lost. The
        # frame counter is the only honest source for a partial airing: the item's duration
        # describes the file, and a wall-clock difference describes the scheduler.
        aired = AiredPlay(
            played_seconds=self._position_frames / self._sample_rate,
            transition_in=self._transition_in,
        )
        self._previous = item
        self._current = None
        self._position_frames = 0
        self._skip_requested = False

        if not item.is_station_id:
            # See :meth:`_begin`: identities are not tracks.
            if completed:
                self._stats.tracks_completed += 1
            else:
                self._stats.tracks_skipped += 1

        if item.entry is not None:
            self._queue.finish_playing()

        _log.info(
            "playout.track_finished",
            track_id=item.track_id,
            completed=completed,
            reason=reason,
            tier=item.tier.value,
            played_seconds=round(aired.played_seconds, 2),
            transition_in=aired.transition_in,
        )
        if self._on_finished is not None:
            await self._notify(
                self._on_finished(item, completed, reason, aired),
                hook="on_track_finished",
            )

    async def _notify(self, call: Awaitable[None], *, hook: str) -> None:
        """Await an outward callback, surviving whatever it does.

        These hooks run on the audio path. A station-side listener that raises — a bounded
        queue that is full, a database write that times out — would otherwise propagate out
        of :meth:`pump` and kill the playout task, turning a bookkeeping failure into dead
        air. §86 is explicit that one component's crash must not stop the radio, and this is
        the one place where honouring that is not optional.

        Not suppressed: logged with a traceback and counted.
        """
        try:
            await call
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - logged and counted, see above
            self._stats.hook_errors += 1
            _log.error(
                "playout.hook_failed",
                hook=hook,
                error_type=type(error).__name__,
                error=str(error),
                exc_info=True,
            )


def _bpm_of(item: PlayingItem) -> int | None:
    if item.entry is None:
        return None
    return item.entry.blueprint.composition.bpm


def _energy_of(item: PlayingItem) -> float | None:
    if item.entry is None:
        return None
    return item.entry.blueprint.composition.energy


__all__ = [
    "DEFAULT_BLOCK_SECONDS",
    "MAX_SINK_REOPEN_ATTEMPTS",
    "AiredPlay",
    "PlayingItem",
    "PlayoutEngine",
    "PlayoutState",
    "PlayoutStats",
    "TransitionType",
]
