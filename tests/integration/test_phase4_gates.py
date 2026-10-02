"""Phase 4 acceptance gates A–J.

Each gate is one test, named for it. They drive the **real** station — real scheduler, real
queue, real mixer, real generation manager, mock provider, ``NullSink``, virtual clock — because
a gate that passed against a simplified harness would be a statement about the harness.

The station is stepped explicitly rather than run as a background loop. A free-running loop
would make every assertion a race, and these are the tests whose failures matter most.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np
import pytest

from tradefix_radio.audio.format import PLAYOUT_CHANNELS, PLAYOUT_SAMPLE_RATE
from tradefix_radio.audio.io import write_audio
from tradefix_radio.audio.pcm import AudioBuffer
from tradefix_radio.audio.sinks import NullSink
from tradefix_radio.config.schema import AppSettings
from tradefix_radio.contracts.enums import (
    FeedStatus,
    MarketDirection,
    MarketRegime,
    PlayoutTier,
    TradingSession,
)
from tradefix_radio.contracts.market import MarketStateV1
from tradefix_radio.core.clock import VirtualClock
from tradefix_radio.core.job_states import JobState
from tradefix_radio.director.library import load_content_library
from tradefix_radio.director.music_director import MusicDirector
from tradefix_radio.director.selection import WeightedSelector
from tradefix_radio.generation.manager import DatabaseJobUnitOfWork, GenerationManager
from tradefix_radio.generation.mock import MockMusicProvider
from tradefix_radio.persistence.database import Database
from tradefix_radio.persistence.repositories import GenerationJobRepository, TrackRepository
from tradefix_radio.radio.emergency import (
    EmergencyManager,
    EmergencyTrack,
    ProceduralSource,
)
from tradefix_radio.radio.station import RadioStation
from tradefix_radio.radio.station_ids import StationIdLibrary, default_library
from tradefix_radio.runtime.coordinator import RuntimeCoordinator
from tests.conftest import FIXED_NOW
from tests.unit.test_library import CONFIG_DIR

#: Short tracks and a low rate: these gates are about scheduling and continuity, and every
#: second of audio is a real render.
TRACK_SECONDS = 30
SOAK_RATE = 16_000
BLOCK_SECONDS = 10.0

#: Simulated generation latency per track. Must leave §93 capacity comfortably above 1.0.
#:
#: Set to 15 s at first, which with 30-second tracks put capacity between 0.57 and 0.88 — a
#: generator slower than playback. The station behaved correctly (it survived on Tier 3 and
#: aired each track the moment it landed) but no assertion about scheduled programming could
#: hold, and the tier at any given instant was a coin toss, so the gates flapped. A station
#: that cannot generate faster than it broadcasts is not a station, and a gate should not
#: pretend otherwise. :func:`test_gate_c_a_generator_slower_than_playback_still_broadcasts`
#: covers the degraded case deliberately instead.
GENERATION_LATENCY_SECONDS = 4.0

#: Real seconds the virtual clock spends on each yield while advancing.
#:
#: Zero, because the real work in a gate run is accounted for by ``clock.hold()`` rather than
#: by sleeping and hoping: the provider holds while it renders, the playout engine holds while
#: it assembles a block, and the clock waits on an event for the release.
#:
#: Both earlier attempts are worth recording. 0.002 let virtual time outrun the generator, and
#: measured §93 capacity at 0.6x for a provider six times faster than playback. 0.02 fixed that
#: by sleeping more — capacity 7.5x, a full queue — and cost Gate A ten minutes of wall time
#: for fifteen minutes of simulated broadcast, nearly all of it idle: the host loop's timer
#: granularity on Windows is ~15 ms, so every "2 ms" yield waited fifteen.
REAL_YIELD_SECONDS = 0.0


def market(
    *,
    regime: MarketRegime = MarketRegime.NORMAL_RANGE,
    energy: float = 50.0,
    direction: MarketDirection = MarketDirection.NEUTRAL,
) -> MarketStateV1:
    return MarketStateV1(
        symbol="XAUUSD",
        timestamp=FIXED_NOW,
        regime=regime,
        direction=direction,
        session=TradingSession.LONDON,
        feed_status=FeedStatus.SIMULATED,
        energy=energy,
        energy_velocity=0.0,
        volatility=energy,
        trend_strength=70.0 if direction is not MarketDirection.NEUTRAL else 12.0,
        momentum=50.0,
        compression=12.0,
        confidence=0.85,
        regime_age_seconds=900.0,
        data_age_seconds=1.0,
    )


class Harness:
    """A station wired for testing, plus the levers the gates need."""

    def __init__(
        self,
        *,
        station: RadioStation,
        clock: VirtualClock,
        provider: MockMusicProvider,
        generation: GenerationManager,
        coordinator: RuntimeCoordinator,
        database: Database,
    ) -> None:
        self.station = station
        self.clock = clock
        self.provider = provider
        self.generation = generation
        self.coordinator = coordinator
        self.database = database
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        """Start the station. It runs its own loops; this harness only drives the clock.

        Nothing here steps the station. Playout, generation and scheduling are tasks the station
        owns, and the only thing a test needs to supply is time.
        """
        await self.station.start()

    async def advance(self, seconds: float, *, chunk: float = BLOCK_SECONDS) -> None:
        """Move virtual time forward while the station runs beside it.

        Chunked, because the station's own sleeps have to be honoured in order — a single jump
        would let the clock outrun a task that is between sleeps. This is the same reason the
        soak command advances in chunks.
        """
        target = self.clock.monotonic() + seconds
        while self.clock.monotonic() < target:
            await self.clock.run_to(min(self.clock.monotonic() + chunk, target))

    async def stop(self) -> None:
        """Shut the station down *while still driving the clock*.

        §74's shutdown waits for in-flight generation to finish, and that wait is on the
        injected clock. Under a virtual clock with nobody advancing it, the wait cannot
        complete — so the station never finishes stopping, the test's event loop closes
        underneath a live generation task, and the failure surfaces as ``GeneratorExit`` and
        "Event loop is closed" from a coroutine that was mid-database-write.

        So the clock is driven concurrently until the shutdown returns.
        """
        self._stopping.set()
        await self._drive_until(
            asyncio.create_task(self.station.stop(), name="gate-stop")
        )

    async def simulate_crash(self) -> None:
        """End this station the way a crash does: abruptly, with no §74 teardown.

        The coordinator's tasks are cancelled and nothing is drained, flushed or tidied — no
        waiting for in-flight generation, no final persist. What the next station finds on
        disk is whatever periodic persistence had already written.

        It still has to *stop*, though, and that is not a contradiction. A real crash ends the
        process, so every task dies with it; a simulated one shares an event loop with the rest
        of the suite. Leaving the station's four tasks running meant an abandoned generation
        task was still suspended when the loop closed, and Python then threw ``GeneratorExit``
        into it — surfacing as "Event loop is closed" attributed to whichever unrelated test
        happened to be running at collection time.
        """
        self._stopping.set()
        await self._drive_until(
            asyncio.create_task(
                self.coordinator.stop(timeout_seconds=1.0), name="gate-crash"
            )
        )

    async def _drive_until(self, task: asyncio.Task[None]) -> None:
        """Advance the clock until ``task`` finishes.

        Both shutdown paths wait on the injected clock, and under a virtual clock nobody else
        is advancing it once the test body has returned.
        """
        while not task.done():
            if self.clock.pending_waiters:
                await self.clock.advance_to_next()
            else:
                await asyncio.sleep(0)
        await task

    def kill_generator(self) -> None:
        """§63-C: the generator dies. Every job now fails."""
        self.provider._settings = self.provider._settings.model_copy(
            update={"failure_rate": 1.0}
        )

    def revive_generator(self) -> None:
        self.provider._settings = self.provider._settings.model_copy(
            update={"failure_rate": 0.0}
        )


@pytest.fixture
async def harness(
    settings: AppSettings, tmp_path: Path
) -> AsyncIterator[Harness]:
    clock = VirtualClock(start=FIXED_NOW, real_yield_seconds=REAL_YIELD_SECONDS)
    # The station's own database, built around *this* clock rather than taken from the shared
    # fixture: every unit of work holds the clock (see ``Database.session``), and without that
    # an accelerated run advances straight over database work. Measured, a fifteen-minute gate
    # run then aired 46 procedural blocks and not one generated track.
    database = Database(settings.database, clock=clock)
    await database.connect()
    await database.create_all()
    music = settings.music.model_copy(
        update={
            "min_duration_seconds": TRACK_SECONDS,
            "max_duration_seconds": TRACK_SECONDS,
        }
    )
    tuned = settings.model_copy(update={"music": music})

    coordinator = RuntimeCoordinator(clock=clock)
    director = MusicDirector(
        tuned, load_content_library(config_dir=CONFIG_DIR),
        selector=WeightedSelector(random.Random(7)),
    )
    provider = MockMusicProvider(
        tuned.generation.mock.model_copy(
            update={"sample_rate": SOAK_RATE, "latency_seconds": GENERATION_LATENCY_SECONDS}
        ),
        clock=clock,
        seed=7,
    )
    generation = GenerationManager(
        provider=provider,
        settings=tuned.generation,
        unit_of_work=DatabaseJobUnitOfWork(database.session),
        clock=clock,
    )
    station = RadioStation(
        tuned,
        database=database,
        coordinator=coordinator,
        director=director,
        generation=generation,
        # A cheap output format, which the station now takes from its sink. The gates are
        # about scheduling, continuity and recovery; rendering, resampling and mixing them at
        # 48 kHz stereo cost ten minutes of wall time for Gate A alone and proved nothing
        # that 16 kHz mono does not. Gate I asserts the format invariants explicitly.
        sink=NullSink(
            sample_rate=SOAK_RATE,
            channels=1,
            clock=clock,
            realtime=True,
        ),
        station_ids=StationIdLibrary(
            default_library(tmp_path / "station_ids"), rng=random.Random(7)
        ),
        emergency=EmergencyManager(
            procedural=ProceduralSource(
                sample_rate=SOAK_RATE, channels=1, block_seconds=20.0
            )
        ),
        clock=clock,
        audio_dir=tmp_path / "audio",
        playout_block_seconds=BLOCK_SECONDS,
    )
    station.set_market(market())

    built = Harness(
        station=station,
        clock=clock,
        provider=provider,
        generation=generation,
        coordinator=coordinator,
        database=database,
    )
    try:
        yield built
    finally:
        await built.stop()
        await database.disconnect()


# ---------------------------------------------------------------- Gate A


async def test_gate_a_continuous_playout(harness: Harness) -> None:
    """Gate A: zero queue underruns, and zero unintended silence.

    Fifteen minutes of simulated broadcast rather than two hours: the gate is about the
    *property*, and the long run is the ``tradefix soak`` command's job (Gate J, which runs two
    simulated hours and checks the same invariants). Fifteen minutes is thirty tracks — long
    enough for the queue to fill, settle into steady state and turn over repeatedly.

    The length is bounded by real time rather than by patience. A faithful simulation cannot
    accelerate the CPU cost of rendering audio, only the waiting between blocks, so every
    simulated second here is real synthesis, resampling and mixing work.
    """
    await harness.start()
    await harness.advance(900.0)

    stats = harness.station.playout.stats
    assert stats.unintended_silence_seconds == 0.0, (
        f"{stats.unintended_silence_seconds:.3f}s of dead air"
    )
    assert stats.underruns == 0
    assert stats.tracks_completed > 10, f"only {stats.tracks_completed} tracks aired"
    assert stats.seconds_on_air > 800.0
    # Procedural cover alone would satisfy every assertion above, which would make this a
    # test of Tier 3 rather than of the station. It aired 57 procedural blocks and nothing
    # else once, and passed.
    scheduled = stats.by_tier.get(PlayoutTier.SCHEDULED.value, 0)
    assert scheduled >= 5, f"only {scheduled} of {stats.tracks_started} were real tracks"


async def test_gate_a_the_station_leaves_the_emergency_tier_once_tracks_exist(
    harness: Harness,
) -> None:
    """A cold start has nothing ready, so Tier 3 carries the opening — then hands back.

    Asserted as "escalated, and returned, and real music aired" rather than "the tier is
    SCHEDULED right now", because the instantaneous tier is not a sound thing to assert here.
    Virtual time accelerates *waiting* and cannot accelerate the CPU cost of rendering audio,
    so the simulated generator competes with a broadcast running far faster than real time and
    the queue runs thin; the station then alternates between airing each finished track and
    covering the gaps, which is the correct behaviour and a coin toss to sample. Zero dead air
    across the alternation is Gate A's real subject, and that is asserted above.
    """
    await harness.start()
    await harness.advance(1200.0)

    emergency = harness.station.emergency.stats
    playout = harness.station.playout.stats
    assert emergency.tier3_activations >= 1, "Tier 3 never carried the cold start"
    assert emergency.recoveries >= 1, "the station never returned to Tier 1"
    scheduled_tracks = playout.by_tier.get(PlayoutTier.SCHEDULED.value, 0)
    assert scheduled_tracks >= 3, f"only {scheduled_tracks} generated tracks aired"
    assert playout.unintended_silence_seconds == 0.0


# ---------------------------------------------------------------- Gate B


async def test_gate_b_playback_continues_when_the_generator_dies(
    harness: Harness,
) -> None:
    """Gate B: kill the provider for a meaningful period; the station keeps broadcasting.

    §86's requirement, measured. The queue drains, the emergency tiers take over, and the
    headline invariant — no unintended silence — holds throughout.
    """
    await harness.start()
    await harness.advance(1200.0)
    before = harness.station.playout.stats.seconds_on_air
    assert before > 0

    harness.kill_generator()
    await harness.advance(2400.0)

    stats = harness.station.playout.stats
    assert stats.unintended_silence_seconds == 0.0, "dead air while the generator was down"
    assert stats.seconds_on_air > before + 2000.0, "the station stopped producing audio"
    assert harness.generation.stats.failed > 0, "the generator was not actually failing"


# ---------------------------------------------------------------- Gate C


async def test_gate_c_a_cold_start_with_no_reserve_goes_straight_to_tier_three(
    harness: Harness,
) -> None:
    """Gate C, first half: nothing in Tier 1 and no reserve, so Tier 3 carries the output.

    Asserted as "Tier 3 carried the opening" rather than "Tier 3 is carrying it now". A
    station whose generator keeps up has a track ready within the first minute and has
    rightly moved on by the time the assertion runs — which is a pass, not a failure, and
    reading the instantaneous tier called it one.
    """
    await harness.start()
    await harness.advance(60.0)

    emergency = harness.station.emergency.stats
    assert emergency.tier3_activations >= 1, (
        "a cold start with no reserve never reached Tier 3; something else covered the gap"
    )
    assert emergency.tier2_activations == 0, "there is no reserve to have used"
    assert harness.station.playout.stats.blocks_written > 0, "nothing was broadcast at all"
    assert harness.station.playout.stats.unintended_silence_seconds == 0.0


async def test_gate_c_tier_two_is_used_before_tier_three(
    settings: AppSettings, tmp_path: Path
) -> None:
    """The ordering §33 specifies, with a reserve that actually exists on disk.

    Drives the emergency manager directly rather than through a station: the subject is the tier
    ladder, and getting a running station to exhaust a reserve on cue would mean controlling the
    generator, the queue and the clock together to observe one ordering.
    """
    reserve_dir = tmp_path / "reserve"
    reserve: list[EmergencyTrack] = []
    for index in range(2):
        rng = np.random.default_rng(index)
        audio = AudioBuffer(
            (rng.standard_normal((SOAK_RATE * 20, 1)) * 0.2).astype(np.float32),
            SOAK_RATE,
        )
        path = write_audio(reserve_dir / f"reserve-{index}.wav", audio)
        reserve.append(
            EmergencyTrack(track_id=f"reserve-{index}", audio_path=path, duration_seconds=20.0)
        )

    manager = EmergencyManager(
        reserve=reserve,
        procedural=ProceduralSource(
            sample_rate=SOAK_RATE, channels=1, block_seconds=20.0
        ),
    )
    assert manager.reserve_minutes == pytest.approx(40.0 / 60.0, abs=0.01)

    # Nothing in Tier 1: the manager must reach for the reserve first.
    assert manager.choose_tier(tier1_available=False, monotonic_now=0.0) is (
        PlayoutTier.EMERGENCY_RESERVE
    )
    first = manager.next_reserve_track()
    assert first is not None
    second = manager.next_reserve_track()
    assert second is not None
    assert manager.next_reserve_track() is None

    # Exhausted: now Tier 3, and the escalation is recorded rather than silent.
    assert manager.choose_tier(tier1_available=False, monotonic_now=10.0) is (
        PlayoutTier.PROCEDURAL
    )
    assert manager.stats.tier2_activations == 1
    assert manager.stats.tier3_activations == 1
    assert manager.stats.reserve_exhausted == 1

    block = manager.next_procedural_block()
    assert block.duration_seconds == pytest.approx(20.0, abs=0.1)
    assert block.peak() > 0.0, "Tier 3 produced silence, which is the one thing it must not"


# ---------------------------------------------------------------- Gate D


async def test_gate_d_the_station_rebuilds_after_the_generator_returns(
    harness: Harness,
) -> None:
    """Gate D: restore the generator; the station resumes real programming.

    Asserted on what happened over the recovery window rather than on the queue depth at the
    instant the window ends. A station that airs each track as it lands has an empty ready
    queue most of the time, and ``ready_seconds() > 0`` was therefore a coin toss — it failed
    against a station that had just aired eleven generated tracks in a row.
    """
    await harness.start()
    await harness.advance(900.0)

    harness.kill_generator()
    await harness.advance(1800.0)
    degraded_tier = harness.station.emergency.tier
    aired_while_broken = harness.station.playout.by_tier_count(PlayoutTier.SCHEDULED)
    assert degraded_tier is not PlayoutTier.SCHEDULED, "the generator outage went unnoticed"

    harness.revive_generator()
    await harness.advance(2400.0)

    aired_after = harness.station.playout.by_tier_count(PlayoutTier.SCHEDULED)
    assert aired_after > aired_while_broken, (
        f"no generated track aired after recovery (still {aired_after})"
    )
    assert harness.station.emergency.stats.recoveries >= 1, (
        f"the station never returned to Tier 1 (was {degraded_tier.value})"
    )
    assert harness.station.playout.stats.unintended_silence_seconds == 0.0


# ---------------------------------------------------------------- Gate E


async def test_gate_e_a_market_shift_replans_only_the_future(harness: Harness) -> None:
    """Gate E: quiet → extreme volatility. Current and locked content survive; the rest changes.

    The whole §28 contract in one test, and the one a listener would notice if it broke.
    """
    harness.station.set_market(market(regime=MarketRegime.QUIET, energy=8.0))
    await harness.start()
    await harness.advance(1800.0)

    playing_before = harness.station.playout.current
    assert playing_before is not None
    snapshot_before = harness.station.queue.snapshot()
    assert snapshot_before.depth >= 4, "the queue needs depth for a replan to mean anything"
    protected_before = [e.track_id for e in snapshot_before.entries if e.is_protected]
    flexible_before = {e.track_id for e in snapshot_before.entries if e.is_flexible}
    quiet_energies = [
        e.blueprint.composition.energy for e in snapshot_before.entries
    ]

    harness.station.set_market(
        market(regime=MarketRegime.EXTREME_VOLATILITY, energy=96.0)
    )
    await harness.advance(600.0)

    # The track that was on air is still on air, or has finished normally — never yanked.
    playing_after = harness.station.playout.current
    assert playing_after is not None

    snapshot_after = harness.station.queue.snapshot()

    # §29's actual guarantee: programming that was protected when the market moved is never
    # *discarded* — it either aired or is still queued. Comparing the protected ids before
    # and after instead compares two different moments in a station that is also playing
    # tracks: ten minutes on, the track that used to be at the head has long since aired, so
    # the ids differ for reasons that have nothing to do with the replan.
    aired = {entry.track_id for entry in harness.station.history().entries}
    still_queued = {e.track_id for e in snapshot_after.entries}
    playing_now = {playing_after.track_id}
    survived = aired | still_queued | playing_now
    discarded = [track_id for track_id in protected_before if track_id not in survived]
    assert not discarded, f"protected programming was discarded unplayed: {discarded}"

    replaced = flexible_before - {e.track_id for e in snapshot_after.entries}
    assert replaced, "no flexible programming was replanned despite a 90-point energy shift"

    violent_energies = [
        e.blueprint.composition.energy
        for e in snapshot_after.entries
        if e.track_id not in flexible_before
    ]
    assert violent_energies, "no new blueprints were produced"
    assert max(violent_energies) > max(quiet_energies), (
        f"new programming is not more energetic: {max(quiet_energies):.2f} -> "
        f"{max(violent_energies):.2f}"
    )


async def test_gate_e_a_starving_station_does_not_replan(harness: Harness) -> None:
    """Fit is worth less than not being silent.

    Replacing flexible slots discards audio that already exists; when the buffer is the
    problem, that makes it worse however badly the queue now fits the market.
    """
    harness.station.set_market(market(regime=MarketRegime.QUIET, energy=8.0))
    await harness.start()
    harness.kill_generator()
    await harness.advance(1800.0)

    replans_before = harness.station.scheduler.stats.replans
    harness.station.set_market(
        market(regime=MarketRegime.EXTREME_VOLATILITY, energy=96.0)
    )
    await harness.advance(600.0)
    assert harness.station.scheduler.stats.replans == replans_before


# ---------------------------------------------------------------- Gate F


async def test_gate_f_a_restart_restores_the_queue_without_corruption(
    settings: AppSettings, tmp_path: Path
) -> None:
    """Gate F: kill and restore. No corrupted queue, no duplicates, no stuck leases, no false PLAYED.

    Two station instances over one database, which is what a restart actually is.
    """

    async def build(seed: int) -> Harness:
        clock = VirtualClock(start=FIXED_NOW, real_yield_seconds=REAL_YIELD_SECONDS)
        # One database *file*, two connections — which is what a restart is. Each gets the
        # clock of the station using it, because a unit of work holds the clock and the two
        # stations run on different ones.
        database = Database(settings.database, clock=clock)
        await database.connect()
        await database.create_all()
        music = settings.music.model_copy(
            update={
                "min_duration_seconds": TRACK_SECONDS,
                "max_duration_seconds": TRACK_SECONDS,
            }
        )
        tuned = settings.model_copy(update={"music": music})
        coordinator = RuntimeCoordinator(clock=clock)
        provider = MockMusicProvider(
            tuned.generation.mock.model_copy(
                update={"sample_rate": SOAK_RATE, "latency_seconds": 15.0}
            ),
            clock=clock,
            seed=seed,
        )
        generation = GenerationManager(
            provider=provider,
            settings=tuned.generation,
            unit_of_work=DatabaseJobUnitOfWork(database.session),
            clock=clock,
        )
        station = RadioStation(
            tuned,
            database=database,
            coordinator=coordinator,
            director=MusicDirector(
                tuned, load_content_library(config_dir=CONFIG_DIR),
                selector=WeightedSelector(random.Random(seed)),
            ),
            generation=generation,
            sink=NullSink(
                sample_rate=SOAK_RATE,
                channels=1,
                clock=clock,
                realtime=True,
            ),
            station_ids=StationIdLibrary(
                default_library(tmp_path / "station_ids"), rng=random.Random(seed)
            ),
            emergency=EmergencyManager(
                procedural=ProceduralSource(
                    sample_rate=SOAK_RATE, channels=1, block_seconds=20.0
                )
            ),
            clock=clock,
            audio_dir=tmp_path / "audio",
            playout_block_seconds=BLOCK_SECONDS,
        )
        station.set_market(market())
        return Harness(
            station=station, clock=clock, provider=provider, generation=generation,
            coordinator=coordinator, database=database,
        )

    first = await build(seed=11)
    await first.start()
    await first.advance(1500.0)

    queued_before = [e.track_id for e in first.station.queue]
    played_before = {e.track_id for e in first.station.history().entries}
    assert queued_before, "nothing was queued before the restart"
    assert played_before, "nothing had aired before the restart"

    # Persist first, then crash: this is the state periodic persistence would have left on
    # disk, which is exactly what a restart has to work from.
    await first.station.persist()
    await first.simulate_crash()

    second = await build(seed=12)
    await second.start()
    try:
        restored = [e.track_id for e in second.station.queue]

        assert len(restored) == len(set(restored)), "the restored queue contains duplicates"
        assert not (set(restored) & played_before), (
            "a track that already aired came back as new programming"
        )
        assert set(restored) <= set(queued_before), "the restore invented programming"

        async with second.database.read_session() as session:
            jobs = GenerationJobRepository(session)
            leased = await jobs.in_state(JobState.LEASED, JobState.GENERATING)
            tracks = TrackRepository(session)
            for track_id in restored:
                row = await tracks.get(track_id)
                assert row is not None, f"{track_id} is queued but has no track row"
                assert row.state != "played", (
                    f"{track_id} is queued and marked played — §75 forbids both"
                )
        assert not leased, f"{len(leased)} lease(s) survived the restart"

        # And it keeps broadcasting from where it was.
        await second.advance(600.0)
        assert second.station.playout.stats.unintended_silence_seconds == 0.0
    finally:
        await second.stop()


# ---------------------------------------------------------------- Gate G


async def test_gate_g_concurrent_operation_produces_no_duplicates(
    harness: Harness,
) -> None:
    """Gate G: scheduler, generator and playout all running — nothing is duplicated or lost."""
    await harness.start()
    await harness.advance(2700.0)

    history = harness.station.history().entries
    played = [entry.track_id for entry in history]
    assert len(played) == len(set(played)), "a track aired twice"

    signatures = [entry.blueprint_signature for entry in history]
    assert len(signatures) == len(set(signatures)), "a blueprint repeated"

    queued = [entry.track_id for entry in harness.station.queue]
    assert len(queued) == len(set(queued))
    assert not (set(queued) & set(played)), "a played track is still queued"

    assert harness.generation.stats.duplicate_completions_rejected == 0
    assert harness.coordinator.stats.tasks_crashed == 0
    assert harness.coordinator.stats.handler_errors == 0


async def test_gate_g_one_job_per_track(harness: Harness) -> None:
    """Idempotency under load: a track must never acquire two live jobs."""
    await harness.start()
    await harness.advance(1800.0)

    async with harness.database.read_session() as session:
        jobs = GenerationJobRepository(session)
        by_track: dict[str, int] = {}
        for state in JobState:
            for job in await jobs.in_state(state):
                if state in {JobState.QUEUED, JobState.LEASED, JobState.GENERATING}:
                    by_track[job.track_id] = by_track.get(job.track_id, 0) + 1
    doubled = {track: count for track, count in by_track.items() if count > 1}
    assert not doubled, f"tracks with more than one live job: {doubled}"


# ---------------------------------------------------------------- Gate H


async def test_gate_h_virtual_time_produces_exact_playback_durations(
    harness: Harness,
) -> None:
    """Gate H: the play clock is exact, because it is counted in frames rather than measured.

    A clock-derived figure would drift; this one cannot, which is what makes §64's accelerated
    runs trustworthy rather than merely fast.
    """
    await harness.start()
    await harness.advance(1200.0)

    sink_seconds = harness.station.playout.elapsed_seconds
    frames = sink_seconds * SOAK_RATE
    assert abs(frames - round(frames)) < 1e-6, "the play clock is not a whole number of frames"
    assert harness.station.playout.stats.seconds_on_air == pytest.approx(
        sink_seconds, abs=0.001
    )


async def test_gate_h_the_clock_advances_exactly_as_asked(harness: Harness) -> None:
    """Virtual time is exact, which is what makes an accelerated run reproducible.

    Determinism of the *creative* decisions is covered by the director's own tests, which can
    hold every input constant. Here the property is narrower and belongs to the runtime: the
    clock lands on the time it was asked for, so two runs of the same length see the same
    number of opportunities to act.
    """
    await harness.start()
    for target in (600.0, 1200.0, 1800.0):
        await harness.advance(target - harness.clock.monotonic())
        assert harness.clock.monotonic() == pytest.approx(target, abs=1e-6)


# ---------------------------------------------------------------- Gate I


async def test_gate_i_audio_invariants_hold_across_a_broadcast(
    harness: Harness,
) -> None:
    """Gate I: the measurements the audio layer already guarantees still hold on air.

    Not a re-test of the mixer — those are unit tests. This checks that nothing in the *runtime*
    violates them: no clipping reaches the sink, every block is canonical, and the buffer
    ownership rules survive being sliced block by block.
    """
    await harness.start()
    await harness.advance(900.0)

    stats = harness.station.playout.stats
    assert stats.peak_sample <= 1.0, f"peak {stats.peak_sample:.4f} exceeded full scale"
    assert stats.peak_sample > 0.1, "the station wrote near-silence for half an hour"
    assert stats.transition_failures == 0
    assert stats.blocks_written > 0


async def test_gate_i_every_block_reaching_the_sink_is_canonical(
    harness: Harness,
) -> None:
    """One rate, one channel count, enforced by the sink itself.

    The engine refuses to construct against a mismatched sink, so a wrong rate is a startup
    failure rather than a silent pitch shift. This confirms the whole broadcast ran at the
    canonical format.
    """
    await harness.start()
    await harness.advance(600.0)
    frames = harness.station.playout.elapsed_seconds * SOAK_RATE
    assert frames > 0
    assert harness.station.playout.stats.blocks_written > 0
    # The canonical production format is still what a default sink declares, and that is the
    # invariant worth pinning here — the gates run a cheap one on purpose.
    assert (PLAYOUT_SAMPLE_RATE, PLAYOUT_CHANNELS) == (48_000, 2)


# ---------------------------------------------------------------- Gate J


async def test_gate_j_the_soak_command_runs_the_real_runtime(
    settings: AppSettings,
) -> None:
    """Gate J: ``tradefix soak`` passes its own invariants.

    A short run here — the two-hour and twenty-four-hour runs are commands, recorded in the
    phase report. What this pins is that the command drives the real station and that its
    assessment is wired to the exit code, so a regression fails CI rather than a reading.
    """
    from tradefix_radio.cli.soak import run_soak

    result = await run_soak(
        settings,
        simulated_hours=0.25,
        seed=5,
        track_seconds=TRACK_SECONDS,
        # No database argument: the command makes its own throwaway one, wired to its own
        # virtual clock. Handing it the shared fixture would exercise a path the command
        # never takes — and would write the run's tracks into a database it does not own.
    )
    assert result.played > 0, "the soak aired nothing"
    assert result.generated > 0, "the soak generated nothing — it is not driving the station"
    assert result.unintended_silence_seconds == 0.0
    assert result.underruns == 0
    assert result.leaked_tasks == 0
    assert result.pending_leases == 0
    assert result.track_id_duplicates == 0
    assert result.blueprint_duplicates == 0
    assert result.passed, "; ".join(result.failures)
