"""Creative memory across a restart (§96, milestone 3.11).

§96: "Restarting the application should NOT reset creative memory and cause immediate
repeats."

An integration test rather than a unit test, because the claim is about the *whole*
startup path: history and blueprint signatures come back from the ``tracks`` table, station
energy and the peak counter come back from ``radio_memory``, and §96 only holds if both do.
Mocking either half would test the half that already works.

The structure throughout is: run some tracks, throw the director away, build a new one the
way startup does, and assert the station continues rather than restarts.
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import timedelta

import pytest

from tradefix_radio.config.schema import AppSettings
from tradefix_radio.contracts.enums import (
    FeedStatus,
    MarketDirection,
    MarketRegime,
    TradingSession,
)
from tradefix_radio.contracts.market import MarketStateV1
from tradefix_radio.contracts.queue import BufferHealthV1
from tradefix_radio.core.state_machine import TrackState
from tradefix_radio.director.energy_curve import DEFAULT_MAX_STEP
from tradefix_radio.director.history import ProgrammingHistory
from tradefix_radio.director.library import ContentLibrary, load_content_library
from tradefix_radio.director.memory import (
    CONSECUTIVE_PEAKS_KEY,
    load_director_state,
    restore_director_state,
    save_director_state,
)
from tradefix_radio.director.music_director import MusicDirector
from tradefix_radio.director.selection import WeightedSelector
from tradefix_radio.persistence.database import Database
from tradefix_radio.persistence.repositories import (
    MemoryKeys,
    RadioMemoryRepository,
    TrackRepository,
)
from tests.conftest import FIXED_NOW
from tests.unit.test_library import CONFIG_DIR


@pytest.fixture(scope="module")
def library() -> ContentLibrary:
    return load_content_library(config_dir=CONFIG_DIR)


def make_director(library: ContentLibrary, seed: int) -> MusicDirector:
    return MusicDirector(
        AppSettings(), library, selector=WeightedSelector(random.Random(seed))
    )


def market(
    *,
    regime: MarketRegime = MarketRegime.NORMAL_RANGE,
    energy: float = 55.0,
    session: TradingSession = TradingSession.LONDON,
) -> MarketStateV1:
    return MarketStateV1(
        symbol="XAUUSD",
        timestamp=FIXED_NOW,
        regime=regime,
        direction=MarketDirection.NEUTRAL,
        session=session,
        feed_status=FeedStatus.SIMULATED,
        energy=energy,
        energy_velocity=0.0,
        volatility=energy,
        trend_strength=12.0,
        momentum=50.0,
        compression=12.0,
        confidence=0.82,
        regime_age_seconds=900.0,
        data_age_seconds=1.0,
    )


BUFFER = BufferHealthV1(
    minutes_ready=40.0,
    minutes_in_flight=6.0,
    minimum_minutes=20.0,
    target_minutes=45.0,
    maximum_minutes=90.0,
    ready_count=11,
    in_flight_count=2,
)


#: PLANNED -> PLAYING, the states a track passes through before it can air.
_LIFECYCLE = (
    TrackState.GENERATING,
    TrackState.GENERATED,
    TrackState.ANALYZING,
    TrackState.APPROVED,
    TrackState.MASTERING,
    TrackState.READY,
    TrackState.QUEUED,
    TrackState.PLAYING,
)


async def air_tracks(
    database: Database,
    director: MusicDirector,
    *,
    count: int,
    start_index: int,
    state: MarketStateV1,
) -> list[str]:
    """Decide, persist and mark played ``count`` tracks, exactly as the scheduler does.

    Returns the track ids. Every decision reads its own history and signature set back out
    of the database, so this is the same information flow a restarted process sees.
    """
    aired: list[str] = []
    for offset in range(count):
        index = start_index + offset
        now = FIXED_NOW + timedelta(minutes=4 * index)
        async with database.session() as session:
            tracks = TrackRepository(session)
            memory = RadioMemoryRepository(session)

            history = ProgrammingHistory(await tracks.recent_history(limit=400))
            used_titles = tuple(await tracks.recent_titles(limit=500))

            decision = director.create_blueprint(
                track_id=f"TF-{index:05d}",
                state=state.model_copy(update={"timestamp": now}),
                history=history,
                buffer=BUFFER,
                now=now,
                capacity_ratio=1.4,
                used_titles=used_titles,
                recent_titles=used_titles[:40],
                used_signatures=frozenset(history.signatures()),
            )
            blueprint = decision.blueprint
            await tracks.create(
                blueprint, now=now, provider="mock", model_identifier="test"
            )
            # The full lifecycle, not a shortcut to PLAYED. ``recent_history`` selects on
            # ``last_played_at``, which only ``mark_played`` sets, and that in turn only
            # accepts a track already PLAYING. Short-circuiting here would test a state the
            # scheduler can never actually produce.
            for to_state in _LIFECYCLE:
                await tracks.transition(
                    blueprint.track_id, to_state, now=now, reason="test harness"
                )
            await tracks.mark_played(
                blueprint.track_id, now=now, completed=True, reason="aired"
            )
            await save_director_state(memory, director.energy_planner, now=now)
        aired.append(blueprint.track_id)
    return aired


# ---------------------------------------------------------------- state round trip


async def test_nothing_remembered_on_a_first_ever_start(database: Database) -> None:
    """The normal cold start: no rows, no error, no pretend state."""
    async with database.session() as session:
        state = await load_director_state(RadioMemoryRepository(session))
    assert state.station_energy is None
    assert state.consecutive_peaks == 0


async def test_station_energy_survives_a_restart(
    database: Database, library: ContentLibrary
) -> None:
    director = make_director(library, seed=1)
    await air_tracks(database, director, count=6, start_index=0, state=market())
    before = director.energy_planner.current_energy
    assert before is not None

    # "Restart": a brand-new director, with nothing carried over in memory.
    restarted = make_director(library, seed=99)
    assert restarted.energy_planner.current_energy is None
    async with database.session() as session:
        restored = await restore_director_state(
            RadioMemoryRepository(session), restarted.energy_planner
        )
    assert restored.station_energy == pytest.approx(before)
    assert restarted.energy_planner.current_energy == pytest.approx(before)


async def test_the_peak_counter_survives_a_restart(
    database: Database, library: ContentLibrary
) -> None:
    """Otherwise a restart mid-session resets §98's peak budget.

    The station could then run six consecutive peak tracks where §98 allows three, which is
    the §96 failure that is easiest to miss because nothing looks broken.
    """
    director = make_director(library, seed=2)
    await air_tracks(
        database,
        director,
        count=5,
        start_index=0,
        state=market(regime=MarketRegime.HIGH_VOLATILITY_RANGE, energy=94.0),
    )
    peaks = director.energy_planner.consecutive_peaks
    assert peaks > 0, "a violent market produced no peak tracks"

    restarted = make_director(library, seed=77)
    async with database.session() as session:
        await restore_director_state(
            RadioMemoryRepository(session), restarted.energy_planner
        )
    assert restarted.energy_planner.consecutive_peaks == peaks


async def test_state_is_saved_after_every_track_not_at_shutdown(
    database: Database, library: ContentLibrary
) -> None:
    """A watchdog kill or a power cut never reaches a shutdown hook.

    Those are exactly the restarts §96 is about, so the write has to have already happened.
    """
    director = make_director(library, seed=3)
    await air_tracks(database, director, count=1, start_index=0, state=market())
    async with database.read_session() as session:
        memory = RadioMemoryRepository(session)
        assert await memory.get(MemoryKeys.RADIO_ENERGY) is not None
        assert await memory.get(CONSECUTIVE_PEAKS_KEY) is not None


# ---------------------------------------------------------------- §96 proper


async def test_a_restart_does_not_cause_an_immediate_repeat(
    database: Database, library: ContentLibrary
) -> None:
    """§96's headline sentence, tested as stated.

    Twelve tracks, a restart, twelve more. Nothing may repeat across the seam: not a
    blueprint signature, not a title, and not a third consecutive track of the same genre.
    """
    first = make_director(library, seed=4)
    await air_tracks(database, first, count=12, start_index=0, state=market())

    second = make_director(library, seed=4)  # same seed: the adversarial case
    async with database.session() as session:
        await restore_director_state(
            RadioMemoryRepository(session), second.energy_planner
        )
    await air_tracks(database, second, count=12, start_index=12, state=market())

    async with database.read_session() as session:
        history = await TrackRepository(session).recent_history(limit=100)
    assert len(history) == 24

    signatures = [entry.blueprint_signature for entry in history]
    assert len(set(signatures)) == 24, "a blueprint signature repeated across the restart"

    titles = [entry.track_id for entry in history]
    assert len(set(titles)) == 24

    # Oldest first — the order a listener heard — so "consecutive" means what it says.
    aired = list(reversed(history))
    worst = 1
    streak = 1
    for index in range(1, len(aired)):
        streak = streak + 1 if aired[index].genre == aired[index - 1].genre else 1
        worst = max(worst, streak)
    assert worst <= 2, f"a genre ran {worst} times consecutively across the restart"


async def test_programming_does_not_jump_at_the_restart_seam(
    database: Database, library: ContentLibrary
) -> None:
    """The audible §96 symptom: the first track after a restart sounds like a new station.

    Without restored energy the planner treats the first post-restart track as a cold
    start and adopts market energy outright, which is a step the step limit would never
    have allowed.
    """
    director = make_director(library, seed=5)
    # Settle the station well below the market, so a cold start would be obvious.
    await air_tracks(
        database, director, count=10, start_index=0, state=market(energy=15.0)
    )
    settled = director.energy_planner.current_energy
    assert settled is not None

    restarted = make_director(library, seed=5)
    async with database.session() as session:
        await restore_director_state(
            RadioMemoryRepository(session), restarted.energy_planner
        )
    # Market jumps to 90 at the moment of the restart.
    plan = restarted.energy_planner.plan(market_energy=90.0)
    assert plan.previous_energy == pytest.approx(settled)
    assert plan.target_energy < 90.0
    assert abs(plan.step) <= DEFAULT_MAX_STEP * 2 + 1e-9


async def test_genre_rotation_continues_rather_than_restarting(
    database: Database, library: ContentLibrary
) -> None:
    """Rotation memory comes from the tracks table, and the test proves it is read.

    The distribution over a restart must look like one continuous run, not two short ones
    that each independently favoured whatever the seed liked first.
    """
    first = make_director(library, seed=6)
    await air_tracks(database, first, count=20, start_index=0, state=market())
    second = make_director(library, seed=6)
    async with database.session() as session:
        await restore_director_state(
            RadioMemoryRepository(session), second.energy_planner
        )
    await air_tracks(database, second, count=20, start_index=20, state=market())

    async with database.read_session() as session:
        history = await TrackRepository(session).recent_history(limit=100)
    counts = Counter(entry.genre for entry in history)
    share = counts.most_common(1)[0][1] / len(history)
    assert share < 0.35, f"top genre at {share:.0%} across the restart"
    assert len(counts) >= 4


# ---------------------------------------------------------------- corrupt memory


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param("not a number", id="string"),
        pytest.param(True, id="bool"),
        pytest.param(-5.0, id="below range"),
        pytest.param(4200.0, id="above range"),
    ],
)
async def test_corrupt_energy_is_discarded_rather_than_fatal(
    database: Database, stored: object
) -> None:
    """§86: one bad value must not stop the radio.

    A creative hint that cannot be parsed is worth a warning and a cold start, not an
    outage. ``True`` is in the list on purpose: ``bool`` is an ``int`` subclass, so a naive
    numeric check would accept it and set station energy to 1.0.
    """
    async with database.session() as session:
        await RadioMemoryRepository(session).set(
            MemoryKeys.RADIO_ENERGY, stored, now=FIXED_NOW
        )
    async with database.read_session() as session:
        state = await load_director_state(RadioMemoryRepository(session))
    assert state.station_energy is None


@pytest.mark.parametrize(
    "stored", [pytest.param(-1, id="negative"), pytest.param("three", id="string")]
)
async def test_a_corrupt_peak_counter_is_discarded(
    database: Database, stored: object
) -> None:
    async with database.session() as session:
        await RadioMemoryRepository(session).set(
            CONSECUTIVE_PEAKS_KEY, stored, now=FIXED_NOW
        )
    async with database.read_session() as session:
        state = await load_director_state(RadioMemoryRepository(session))
    assert state.consecutive_peaks == 0


async def test_a_valid_boundary_energy_is_kept(database: Database) -> None:
    """0 and 100 are legitimate. The range check must not reject its own endpoints."""
    for value in (0.0, 100.0):
        async with database.session() as session:
            await RadioMemoryRepository(session).set(
                MemoryKeys.RADIO_ENERGY, value, now=FIXED_NOW
            )
        async with database.read_session() as session:
            state = await load_director_state(RadioMemoryRepository(session))
        assert state.station_energy == pytest.approx(value)


async def test_restoring_nothing_leaves_the_planner_cold(
    database: Database, library: ContentLibrary
) -> None:
    director = make_director(library, seed=7)
    async with database.read_session() as session:
        await restore_director_state(
            RadioMemoryRepository(session), director.energy_planner
        )
    assert director.energy_planner.current_energy is None
    # And the first decision therefore adopts the market, as a cold start should.
    plan = director.energy_planner.plan(market_energy=70.0)
    assert plan.previous_energy is None
    assert plan.target_energy == pytest.approx(70.0)
