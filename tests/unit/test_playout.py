"""Playout engine (§4.6).

The brief's instruction for this module is "the audio path must remain boring and reliable",
and these tests are written to that. They check that the engine keeps writing: through a
missing file, through an unreadable file, through a sink that fails mid-broadcast, through an
empty queue. What they deliberately do **not** do is re-test the mixer or the crossfade curves,
which have their own suites — §4.6 also says not to rewrite the proven lower layer, and testing
it twice from above is the first step toward doing so.

Everything runs on a :class:`VirtualClock` through an explicit ``pump()`` rather than the
engine's own loop, so each assertion is about a known number of blocks rather than about
whatever a background task managed in the meantime.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from tests.conftest import FIXED_NOW, make_blueprint
from tradefix_radio.audio.format import PLAYOUT_CHANNELS, PLAYOUT_SAMPLE_RATE, is_canonical
from tradefix_radio.audio.io import write_audio
from tradefix_radio.audio.pcm import AudioBuffer
from tradefix_radio.audio.sinks import BaseSink, NullSink
from tradefix_radio.contracts.enums import PlayoutTier
from tradefix_radio.contracts.queue import QueueLockLevel
from tradefix_radio.core.clock import VirtualClock
from tradefix_radio.core.errors import AudioSinkError
from tradefix_radio.radio.emergency import EmergencyManager, ProceduralSource
from tradefix_radio.radio.playout import PlayoutEngine, PlayoutState
from tradefix_radio.contracts.enums import MarketRegime
from tradefix_radio.radio.queue import RadioQueue, build_entry

pytestmark = pytest.mark.asyncio

#: Small numbers throughout: every second here is a real render and a real resample.
RATE = 8_000
BLOCK = 1.0
TRACK = 3.0


class RecordingSink(BaseSink):
    """A sink that keeps what it was given, and can be told to fail.

    Subclasses :class:`BaseSink` rather than reimplementing the interface so the open/closed
    bookkeeping under test is the real one.
    """

    def __init__(self, clock: VirtualClock, *, fail_after: int | None = None) -> None:
        super().__init__(
            sample_rate=PLAYOUT_SAMPLE_RATE, channels=PLAYOUT_CHANNELS, clock=clock
        )
        self.blocks: list[AudioBuffer] = []
        self.opens = 0
        self.fail_after = fail_after
        self.failures = 0

    @property
    def name(self) -> str:
        return "recording_sink"

    async def _do_open(self) -> None:
        self.opens += 1

    async def _do_close(self) -> None:
        pass

    async def _do_write(self, buffer: AudioBuffer) -> None:
        if self.fail_after is not None and len(self.blocks) >= self.fail_after:
            self.failures += 1
            raise AudioSinkError("the device went away", sink=self.name)
        self.blocks.append(buffer)

    @property
    def seconds_written(self) -> float:
        return sum(b.duration_seconds for b in self.blocks)


def tone(path: Path, *, seconds: float = TRACK, level: float = 0.3) -> Path:
    """A real audio file at a non-canonical rate, so ``conform`` is exercised too."""
    frames = int(RATE * seconds)
    t = np.arange(frames, dtype=np.float32) / RATE
    samples = (level * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
    write_audio(path, AudioBuffer(samples.reshape(-1, 1), RATE))
    return path


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock(start=FIXED_NOW)


@pytest.fixture
def queue() -> RadioQueue:
    return RadioQueue()


@pytest.fixture
def emergency() -> EmergencyManager:
    return EmergencyManager(
        procedural=ProceduralSource(
            sample_rate=PLAYOUT_SAMPLE_RATE, block_seconds=2.0, seed=1
        )
    )


@pytest.fixture
def sink(clock: VirtualClock) -> RecordingSink:
    return RecordingSink(clock)


@pytest.fixture
def engine(
    sink: RecordingSink,
    queue: RadioQueue,
    emergency: EmergencyManager,
    clock: VirtualClock,
) -> PlayoutEngine:
    return PlayoutEngine(
        sink=sink,
        queue=queue,
        emergency=emergency,
        clock=clock,
        block_seconds=BLOCK,
    )


def enqueue_pending(
    queue: RadioQueue, track_id: str, *, now: datetime = FIXED_NOW
) -> None:
    """Put a slot in the queue with no audio yet — what the scheduler produces."""
    queue.append(
        build_entry(
            # The blueprint asks for 30 s; the file on disk is 3 s. That divergence is
            # realistic — §24's duration-deviation check exists because providers do not
            # deliver what was asked for — and the queue takes its figure from the measured
            # audio, not from the request.
            make_blueprint(track_id, duration_seconds=30),
            now=now,
            regime=MarketRegime.NORMAL_RANGE,
            energy=50.0,
        )
    )


def enqueue_ready(
    queue: RadioQueue, track_id: str, path: Path, *, now: datetime = FIXED_NOW
) -> None:
    """Put a playable track in the queue, the way the station does."""
    enqueue_pending(queue, track_id, now=now)
    queue.mark_ready(track_id, audio_path=str(path), duration_seconds=TRACK)


class Deck:
    """An engine with the parts a test needs to drive it.

    Bundled rather than passed around because the engine cannot be pumped without a clock
    driver: it sleeps on the injected clock in two places — the sink's pacing and the
    underrun back-off — and a virtual clock that nobody advances turns either one into a
    deadlock. That is a property of the test, not of the engine, so it is handled here once.
    """

    def __init__(
        self, engine: PlayoutEngine, clock: VirtualClock, sink: RecordingSink
    ) -> None:
        self.engine = engine
        self.clock = clock
        self.sink = sink

    async def pump(self, times: int = 1) -> None:
        for _ in range(times):
            task = asyncio.create_task(self.engine.pump())
            while not task.done():
                if self.clock.pending_waiters:
                    await self.clock.advance_to_next()
                else:
                    await asyncio.sleep(0)
            await task

    async def pump_until(
        self, predicate: Callable[[], bool], *, limit: int = 40
    ) -> None:
        """Pump until something is true, rather than a fixed number of times.

        A 3-second track at 1-second blocks is three pumps, so a hard-coded count silently
        becomes "and then the next track finished too" the moment either figure changes.
        """
        for _ in range(limit):
            if predicate():
                return
            await self.pump()
        raise AssertionError(f"condition never held within {limit} pumps")


@pytest.fixture
def deck(
    engine: PlayoutEngine, clock: VirtualClock, sink: RecordingSink
) -> Deck:
    return Deck(engine, clock, sink)


# -- construction ----------------------------------------------------------


async def test_a_a_sink_at_the_wrong_rate_is_rejected(
    queue: RadioQueue, emergency: EmergencyManager, clock: VirtualClock
) -> None:
    """§4.6's canonical format, enforced at the boundary. A mismatch here would be a
    broadcast playing at the wrong speed, which no later assertion would catch."""
    with pytest.raises(ValueError, match="playout format"):
        PlayoutEngine(
            sink=NullSink(sample_rate=44_100, channels=PLAYOUT_CHANNELS, clock=clock),
            queue=queue,
            emergency=emergency,
            clock=clock,
        )


async def test_b_a_new_engine_is_stopped(engine: PlayoutEngine) -> None:
    assert engine.state is PlayoutState.STOPPED
    assert engine.current is None
    assert engine.stats.blocks_written == 0


# -- normal playout --------------------------------------------------------


async def test_c_pumping_plays_the_queued_track(
    engine: PlayoutEngine, deck: Deck, queue: RadioQueue, sink: RecordingSink, tmp_path: Path
) -> None:
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    await engine.start()
    await deck.pump()
    assert engine.current is not None
    assert engine.current.track_id == "t1"
    assert sink.blocks, "nothing reached the sink"


async def test_d_every_block_reaching_the_sink_is_canonical(
    engine: PlayoutEngine, deck: Deck, queue: RadioQueue, sink: RecordingSink, tmp_path: Path
) -> None:
    """The track on disk is mono at 8 kHz. Conversion happens once, at the input
    boundary, so the sink only ever sees 48 kHz stereo float32."""
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    await engine.start()
    await deck.pump(4)
    assert sink.blocks
    for block in sink.blocks:
        assert is_canonical(block), (block.sample_rate, block.channels)
        assert block.samples.dtype == np.float32


async def test_e_a_track_finishes_and_the_next_one_starts(
    engine: PlayoutEngine, deck: Deck, queue: RadioQueue, tmp_path: Path
) -> None:
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    enqueue_ready(queue, "t2", tone(tmp_path / "t2.flac"))
    await engine.start()
    await deck.pump_until(lambda: engine.stats.tracks_completed >= 1)
    await deck.pump()
    assert engine.current is not None
    assert engine.current.track_id == "t2"


async def test_f_seconds_on_air_matches_what_the_sink_received(
    engine: PlayoutEngine, deck: Deck, queue: RadioQueue, sink: RecordingSink, tmp_path: Path
) -> None:
    """The honest continuity metric. Counting underruns alone once reported zero silence
    across a broadcast that was missing 2.8 % of its audio."""
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    enqueue_ready(queue, "t2", tone(tmp_path / "t2.flac"))
    await engine.start()
    await deck.pump(5)
    assert engine.stats.seconds_on_air == pytest.approx(sink.seconds_written, rel=0.01)


async def test_g_position_advances_within_a_track(
    engine: PlayoutEngine, deck: Deck, queue: RadioQueue, tmp_path: Path
) -> None:
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    await engine.start()
    await deck.pump()
    first = engine.position_seconds
    await deck.pump()
    assert engine.position_seconds > first


async def test_h_the_tier_of_each_block_is_recorded(
    engine: PlayoutEngine, deck: Deck, queue: RadioQueue, tmp_path: Path
) -> None:
    """§50 reports time by tier, and an operator cannot tell a healthy hour from an
    emergency one without it."""
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    await engine.start()
    await deck.pump(2)
    assert engine.stats.by_tier.get(PlayoutTier.SCHEDULED.value, 0) >= 1


# -- survival --------------------------------------------------------------


async def test_i_an_empty_queue_falls_back_rather_than_going_silent(
    engine: PlayoutEngine, deck: Deck, sink: RecordingSink
) -> None:
    """The promise. Nothing to play is not a reason to stop writing."""
    await engine.start()
    await deck.pump(3)
    assert sink.blocks, "the engine produced no audio at all"
    assert engine.tier is PlayoutTier.PROCEDURAL
    assert engine.stats.unintended_silence_seconds == 0.0


async def test_j_a_slot_with_no_audio_path_is_skipped_loudly(
    engine: PlayoutEngine, deck: Deck, queue: RadioQueue, tmp_path: Path
) -> None:
    """A pending slot that somehow reached the head. Skipping is right; skipping
    *silently* would make a generation failure look like a short queue."""
    enqueue_pending(queue, "pending")
    enqueue_ready(queue, "ready", tone(tmp_path / "r.flac"))
    await engine.start()
    await deck.pump()
    assert engine.current is not None
    assert engine.current.track_id == "ready"


async def test_k_an_unreadable_file_is_skipped_and_counted(
    engine: PlayoutEngine, deck: Deck, queue: RadioQueue, tmp_path: Path
) -> None:
    """§36: retention and disk faults both produce this. The engine must step over it
    and say so, not stall on the head of the queue."""
    broken = tmp_path / "broken.flac"
    broken.write_bytes(b"this is not audio")
    enqueue_ready(queue, "broken", broken)
    enqueue_ready(queue, "good", tone(tmp_path / "good.flac"))

    await engine.start()
    await deck.pump()
    assert engine.stats.tracks_skipped == 1
    assert engine.current is not None
    assert engine.current.track_id == "good"


async def test_l_a_vanished_file_does_not_stop_the_broadcast(
    engine: PlayoutEngine, deck: Deck, queue: RadioQueue, sink: RecordingSink, tmp_path: Path
) -> None:
    """The file existed when the slot was marked ready and is gone now."""
    path = tone(tmp_path / "doomed.flac")
    enqueue_ready(queue, "doomed", path)
    path.unlink()

    await engine.start()
    await deck.pump(2)
    assert sink.blocks
    assert engine.stats.unintended_silence_seconds == 0.0


async def test_m_a_failing_sink_is_reopened_once_per_block(
    queue: RadioQueue, emergency: EmergencyManager, clock: VirtualClock, tmp_path: Path
) -> None:
    """A USB device or a virtual cable disappearing. One reopen attempt per block, so a
    permanently dead device neither hangs the loop nor spins on it."""
    sink = RecordingSink(clock, fail_after=2)
    engine = PlayoutEngine(
        sink=sink, queue=queue, emergency=emergency, clock=clock, block_seconds=BLOCK
    )
    deck = Deck(engine, clock, sink)
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    await engine.start()
    await deck.pump(4)
    assert sink.failures >= 1
    assert engine.stats.sink_errors >= 1
    assert engine.stats.sink_reopens >= 1


async def test_n_a_sink_failure_is_counted_as_lost_audio(
    queue: RadioQueue, emergency: EmergencyManager, clock: VirtualClock, tmp_path: Path
) -> None:
    """Audio the engine produced but the device never played is still dead air to a
    listener, and reporting it as on-air would make the coverage metric a lie."""
    sink = RecordingSink(clock, fail_after=0)
    engine = PlayoutEngine(
        sink=sink, queue=queue, emergency=emergency, clock=clock, block_seconds=BLOCK
    )
    deck = Deck(engine, clock, sink)
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    await engine.start()
    await deck.pump(3)
    assert sink.seconds_written == 0.0
    assert engine.stats.unintended_silence_seconds > 0.0


# -- control ---------------------------------------------------------------


async def test_o_skip_ends_the_current_track_early(
    engine: PlayoutEngine, deck: Deck, queue: RadioQueue, tmp_path: Path
) -> None:
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    enqueue_ready(queue, "t2", tone(tmp_path / "t2.flac"))
    await engine.start()
    await deck.pump()
    engine.request_skip()
    await deck.pump_until(lambda: engine.current is not None and engine.current.track_id == "t2")
    assert engine.current is not None
    assert engine.current.track_id == "t2"


async def test_p_a_station_id_is_played_before_the_next_track(
    engine: PlayoutEngine, deck: Deck, queue: RadioQueue, tmp_path: Path
) -> None:
    from tradefix_radio.radio.station_ids import StationIdCategory, StationIdRecord

    ident = StationIdRecord(
        key="brand",
        category=StationIdCategory.BRANDING,
        audio_path=tone(tmp_path / "id.flac", seconds=1.0),
        duration_seconds=1.0,
    )
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    await engine.start()
    engine.queue_station_id(ident)

    # Asserted as a sequence rather than on ``current``: the identity is one second long at
    # one-second blocks, so it is begun *and* finished inside a single pump, and sampling
    # ``current`` afterwards would be sampling whatever came next.
    await deck.pump_until(lambda: engine.stats.station_ids_played >= 1)
    assert engine.stats.station_ids_played == 1
    assert engine.stats.tracks_completed == 0, "a track aired before the identity"

    await deck.pump_until(lambda: engine.current is not None)
    assert engine.current is not None
    assert engine.current.track_id == "t1"
    assert not engine.current.is_station_id


async def test_q_stopping_leaves_the_engine_stopped(
    engine: PlayoutEngine, deck: Deck, queue: RadioQueue, tmp_path: Path
) -> None:
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    await engine.start()
    await deck.pump()
    await engine.stop()
    assert engine.state is PlayoutState.STOPPED


# -- queue integration -----------------------------------------------------


async def test_r_the_playing_track_leaves_the_queue_but_is_still_reachable(
    engine: PlayoutEngine, deck: Deck, queue: RadioQueue, tmp_path: Path
) -> None:
    """§4.4's invariant, from the playout side: the current track cannot disappear."""
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    await engine.start()
    await deck.pump()
    assert len(queue) == 0
    assert queue.playing is not None
    assert queue.playing.track_id == "t1"


async def test_s_the_head_of_the_queue_is_hard_locked_while_it_waits(
    engine: PlayoutEngine, deck: Deck, queue: RadioQueue, tmp_path: Path
) -> None:
    """Lock level is a function of position (§28), so the engine does not have to
    maintain it — but the property has to hold or a replan could take the next track."""
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    enqueue_ready(queue, "t2", tone(tmp_path / "t2.flac"))
    enqueue_ready(queue, "t3", tone(tmp_path / "t3.flac"))
    await engine.start()
    await deck.pump()
    assert queue.snapshot().entries[0].lock_level is QueueLockLevel.LOCKED


async def test_t_a_finished_track_is_not_handed_back(
    engine: PlayoutEngine, deck: Deck, queue: RadioQueue, tmp_path: Path
) -> None:
    """§4.9: a played track must not return as new programming."""
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    await engine.start()
    await deck.pump_until(lambda: engine.stats.tracks_completed >= 1)
    assert all(entry.track_id != "t1" for entry in queue)
    assert queue.playing is None or queue.playing.track_id != "t1"


# -- hooks -----------------------------------------------------------------


async def test_u_the_start_and_finish_hooks_fire_once_per_track(
    sink: RecordingSink,
    queue: RadioQueue,
    emergency: EmergencyManager,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    started: list[str] = []
    finished: list[tuple[str, bool]] = []

    async def on_started(item) -> None:
        started.append(item.track_id)

    async def on_finished(item, completed: bool, reason: str, aired) -> None:
        finished.append((item.track_id, completed))

    engine = PlayoutEngine(
        sink=sink,
        queue=queue,
        emergency=emergency,
        clock=clock,
        block_seconds=BLOCK,
        on_track_started=on_started,
        on_track_finished=on_finished,
    )
    deck = Deck(engine, clock, sink)
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    await engine.start()
    await deck.pump_until(lambda: bool(finished))
    assert started.count("t1") == 1
    assert ("t1", True) in finished


async def test_v_a_failing_hook_does_not_stop_the_broadcast(
    sink: RecordingSink,
    queue: RadioQueue,
    emergency: EmergencyManager,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    """§86: one subscriber's bug must not stop the radio. The hooks run on the audio
    path, which makes this the least forgiving place for that rule."""

    async def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("the metadata writer is broken")

    engine = PlayoutEngine(
        sink=sink,
        queue=queue,
        emergency=emergency,
        clock=clock,
        block_seconds=BLOCK,
        on_track_started=explode,
        on_track_finished=explode,
    )
    deck = Deck(engine, clock, sink)
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    await engine.start()
    await deck.pump(5)
    assert sink.blocks
    assert engine.stats.unintended_silence_seconds == 0.0
    assert engine.stats.hook_errors >= 1, "the failure was swallowed rather than counted"


# -- timing ----------------------------------------------------------------


async def test_w_virtual_time_advances_by_exactly_one_block_per_pump(
    queue: RadioQueue,
    emergency: EmergencyManager,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    """§4.6's determinism requirement. If a pump did not cost exactly one block of
    clock time, every endurance figure derived from it would be wrong."""
    paced_sink = NullSink(
        sample_rate=PLAYOUT_SAMPLE_RATE,
        channels=PLAYOUT_CHANNELS,
        clock=clock,
        realtime=True,
    )
    paced = PlayoutEngine(
        sink=paced_sink,
        queue=queue,
        emergency=emergency,
        clock=clock,
        block_seconds=BLOCK,
    )
    paced_deck = Deck(paced, clock, sink)
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac", seconds=10.0))
    await paced.start()
    before = clock.monotonic()
    await paced_deck.pump(5)
    assert clock.monotonic() - before == pytest.approx(5 * BLOCK)


async def test_x_a_pump_holds_the_clock_while_it_works(
    engine: PlayoutEngine, deck: Deck, queue: RadioQueue, clock: VirtualClock, tmp_path: Path
) -> None:
    """The hold is what stops a driver advancing over a block the engine was about to
    write — measured at 2.8 % of a broadcast's audio before it existed."""
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    await engine.start()
    assert clock.pending_holds == 0
    await deck.pump()
    assert clock.pending_holds == 0, "the pump leaked a clock hold"


# ------------------------------------------------------- the airing record


async def test_a_completed_airing_reports_the_audio_it_actually_wrote(
    sink: RecordingSink,
    queue: RadioQueue,
    emergency: EmergencyManager,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    """`AiredPlay.played_seconds` comes from the frame counter, not the file's length."""
    records: list[tuple[str, bool, object]] = []

    async def on_finished(item, completed: bool, reason: str, aired) -> None:
        records.append((item.track_id, completed, aired))

    engine = PlayoutEngine(
        sink=sink,
        queue=queue,
        emergency=emergency,
        clock=clock,
        block_seconds=BLOCK,
        on_track_finished=on_finished,
    )
    deck = Deck(engine, clock, sink)
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    await engine.start()
    await deck.pump_until(lambda: bool(records))

    _, completed, aired = records[0]
    assert completed is True
    assert aired.played_seconds == pytest.approx(TRACK, abs=BLOCK)
    # Nothing preceded it, so no transition was planned and none may be claimed.
    assert aired.transition_in == "cold_open"


async def test_a_skipped_airing_reports_the_part_that_played(
    sink: RecordingSink,
    queue: RadioQueue,
    emergency: EmergencyManager,
    clock: VirtualClock,
    tmp_path: Path,
) -> None:
    """The case the record exists for, and the one the old shortcut got wrong.

    `TrackFinished` used to publish `duration if completed else 0.0`, so a track cut
    three-quarters of the way through reported zero seconds of airtime. Any tier accounting
    built on that would have understated real music and overstated the emergency tiers —
    the opposite of the truth, and in the direction that hides a problem.
    """
    records: list[object] = []

    async def on_finished(item, completed: bool, reason: str, aired) -> None:
        records.append(aired)

    engine = PlayoutEngine(
        sink=sink,
        queue=queue,
        emergency=emergency,
        clock=clock,
        block_seconds=BLOCK,
        on_track_finished=on_finished,
    )
    deck = Deck(engine, clock, sink)
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    await engine.start()

    # One block through, then cut it short. Three would finish a three-second track at
    # one-second blocks, and the test would be asserting about a completed airing.
    await deck.pump(1)
    engine.request_skip()
    await deck.pump_until(lambda: bool(records))

    aired = records[0]
    assert aired.played_seconds > 0.0, "a skipped track reported no airtime at all"
    assert aired.played_seconds < TRACK, (
        "a skipped track reported the whole file as played"
    )


async def test_z1_mute_zeroes_the_output_without_stopping_the_clock(
    engine: PlayoutEngine, deck: Deck, queue: RadioQueue, sink: RecordingSink, tmp_path: Path
) -> None:
    """Operator mute: the same blocks keep flowing, at zero gain, so position and play
    records are exactly what they would have been. Unmuting resumes mid-track."""
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac", seconds=12.0))
    await engine.start()
    await deck.pump()
    await deck.pump()
    assert engine.current is not None
    loud = len(sink.blocks)
    assert sink.blocks[-1].peak() > 0.0

    engine.set_muted(True)
    assert engine.muted is True
    await deck.pump()
    await deck.pump()
    assert len(sink.blocks) == loud + 2, "blocks keep flowing while muted"
    assert sink.blocks[-1].peak() == 0.0
    assert engine.current.track_id == "t1"
    assert engine.stats.seconds_on_air > 0.0

    engine.set_muted(False)
    await deck.pump()
    assert sink.blocks[-1].peak() > 0.0


async def test_z2_previous_replays_the_last_track(
    engine: PlayoutEngine, deck: Deck, queue: RadioQueue, tmp_path: Path
) -> None:
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    enqueue_ready(queue, "t2", tone(tmp_path / "t2.flac"))
    enqueue_ready(queue, "t3", tone(tmp_path / "t3.flac"))
    await engine.start()
    await deck.pump()
    engine.request_skip()
    await deck.pump_until(lambda: engine.current is not None and engine.current.track_id == "t2")

    assert engine.request_previous() == "t1"
    await deck.pump_until(lambda: engine.current is not None and engine.current.track_id == "t1")
    assert engine.current is not None
    assert engine.current.entry is None, "a replay does not re-enter the queue"
    # The scheduler's plan is untouched: t3 is still next after the replay.
    assert [entry.track_id for entry in queue] == ["t3"]


async def test_z3_previous_restarts_the_current_track_when_nothing_came_before(
    engine: PlayoutEngine, deck: Deck, queue: RadioQueue, tmp_path: Path
) -> None:
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac"))
    await engine.start()
    await deck.pump()
    await deck.pump()
    assert engine.current is not None
    assert engine.request_previous() == "t1"
    await deck.pump_until(lambda: engine.stats.tracks_skipped == 1)
    await deck.pump()
    assert engine.current is not None
    assert engine.current.track_id == "t1"
    assert engine.stats.tracks_started == 2


async def test_z4_previous_with_nothing_playing_is_a_no_op(engine: PlayoutEngine) -> None:
    assert engine.request_previous() is None


async def test_z5_volume_scales_the_output_and_mute_overrides_it(
    engine: PlayoutEngine, deck: Deck, queue: RadioQueue, sink: RecordingSink, tmp_path: Path
) -> None:
    enqueue_ready(queue, "t1", tone(tmp_path / "t1.flac", seconds=12.0))
    await engine.start()
    await deck.pump()
    await deck.pump()
    full = sink.blocks[-1].peak()
    assert full > 0.0

    assert engine.set_volume(0.5) == 0.5
    await deck.pump()
    assert abs(sink.blocks[-1].peak() - full * 0.5) < 1e-3

    assert engine.set_volume(7.0) == 1.0, "clamped to unity"
    assert engine.set_volume(-1.0) == 0.0, "clamped to silence"
    engine.set_volume(0.25)
    engine.set_muted(True)
    await deck.pump()
    assert sink.blocks[-1].peak() == 0.0, "mute wins over volume"
    engine.set_muted(False)
    await deck.pump()
    assert abs(sink.blocks[-1].peak() - full * 0.25) < 1e-3, "unmuting returns to the level"
    assert engine.current is not None and engine.current.track_id == "t1"
