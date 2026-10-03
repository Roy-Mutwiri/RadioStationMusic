"""Control Center API (Phase 5).

Every test here runs against a **real station** — real queue, real playout engine, real
generation manager, real buffer monitor — because the API's entire job is to report runtime
state faithfully, and a mocked runtime would make these tests a description of the mock.

The station is driven on a virtual clock so a test can advance time deliberately rather than
sleeping, exactly as the Phase 4 gates do.

Two properties get the most attention, because they are the ones a dashboard gets wrong:

**Nothing is fabricated.** A capability that does not exist returns 409 with the phase that
delivers it, not 200 with zeroes. A stale feed withholds its price rather than showing an old
one. A GPU that is not present reports ``null``, never 0 °C.

**The UI cannot corrupt the runtime.** The read endpoints are pure reads, and the three
mutating ones map to methods Phase 4 already shipped and tested.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FIXED_NOW
from tests.unit.test_library import CONFIG_DIR
from tradefix_radio.api.app import create_app
from tradefix_radio.api.capabilities import (
    Capability,
    CapabilityState,
    detect_capabilities,
)
from tradefix_radio.api.snapshot import RuntimeView, build_live_state
from tradefix_radio.audio.sinks import NullSink
from tradefix_radio.config.schema import AppSettings
from tradefix_radio.contracts.enums import (
    FeedStatus,
    MarketDirection,
    MarketRegime,
    TradingSession,
)
from tradefix_radio.contracts.market import MarketStateV1
from tradefix_radio.contracts.queue import QueueLockLevel
from tradefix_radio.core.clock import VirtualClock
from tradefix_radio.director.library import load_content_library
from tradefix_radio.director.music_director import MusicDirector
from tradefix_radio.director.selection import WeightedSelector
from tradefix_radio.generation.manager import DatabaseJobUnitOfWork, GenerationManager
from tradefix_radio.generation.mock import MockMusicProvider
from tradefix_radio.persistence.database import Database
from tradefix_radio.radio.emergency import EmergencyManager, ProceduralSource
from tradefix_radio.radio.station import RadioStation
from tradefix_radio.radio.station_ids import StationIdLibrary, default_library
from tradefix_radio.runtime.coordinator import RuntimeCoordinator

pytestmark = pytest.mark.asyncio

#: Cheap audio: these tests are about the API's shape, not about rendering.
RATE = 8_000
TRACK_SECONDS = 30


def market_state(
    *,
    regime: MarketRegime = MarketRegime.BULLISH_BREAKOUT,
    energy: float = 84.0,
    price: float | None = None,
    data_age: float = 1.0,
    feed_status: FeedStatus = FeedStatus.SIMULATED,
) -> MarketStateV1:
    """A market state for the API to report.

    ``price`` defaults to ``None`` because :class:`MarketStateV1` **refuses** to carry one on a
    simulated feed — §21 is enforced in the Phase 1 contract, not merely in the UI, so a
    simulated price cannot be constructed let alone displayed. The price-bearing tests
    therefore use a live feed, which is the only configuration where the question arises.
    """
    return MarketStateV1(
        symbol="XAUUSD",
        timestamp=FIXED_NOW,
        regime=regime,
        direction=MarketDirection.BULLISH,
        session=TradingSession.LONDON,
        feed_status=feed_status,
        energy=energy,
        energy_velocity=2.5,
        volatility=energy,
        trend_strength=88.0,
        momentum=76.0,
        compression=12.0,
        confidence=0.89,
        regime_age_seconds=900.0,
        data_age_seconds=data_age,
        price=price,
    )


class _StubFeed:
    """A feed whose scenario can be switched, standing in for the simulator.

    Only the two members the API touches. A real `SimulatedFeed` would work and would also
    generate bars on a clock nobody is advancing, which is noise in a test about routing.
    """

    def __init__(self) -> None:
        self.scenario: str | None = None
        self.is_simulated = True

    def set_scenario(self, scenario: object) -> None:
        self.scenario = getattr(scenario, "value", str(scenario))


class _StubMarketService:
    """Holds a state the test sets directly, so market shape is controllable."""

    def __init__(self, state: MarketStateV1 | None) -> None:
        self.current_state = state
        self.feed = _StubFeed()
        self.feed_status = FeedStatus.SIMULATED
        self.bars_processed = 120


@pytest.fixture
async def station_fixture(
    settings: AppSettings, tmp_path: Path
) -> AsyncIterator[tuple[RadioStation, GenerationManager, Database, VirtualClock]]:
    clock = VirtualClock(start=FIXED_NOW, real_yield_seconds=0.0)
    music = settings.music.model_copy(
        update={
            "min_duration_seconds": TRACK_SECONDS,
            "max_duration_seconds": TRACK_SECONDS,
        }
    )
    tuned = settings.model_copy(update={"music": music})

    database = Database(settings.database, clock=clock)
    await database.connect()
    await database.create_all()

    provider = MockMusicProvider(
        tuned.generation.mock.model_copy(
            update={"sample_rate": RATE, "latency_seconds": 2.0}
        ),
        clock=clock,
        seed=5,
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
        coordinator=RuntimeCoordinator(clock=clock),
        director=MusicDirector(
            tuned,
            load_content_library(config_dir=CONFIG_DIR),
            selector=WeightedSelector(random.Random(5)),
        ),
        generation=generation,
        sink=NullSink(sample_rate=RATE, channels=1, clock=clock, realtime=True),
        station_ids=StationIdLibrary(
            default_library(tmp_path / "station_ids"), rng=random.Random(5)
        ),
        emergency=EmergencyManager(
            procedural=ProceduralSource(sample_rate=RATE, channels=1, block_seconds=10.0)
        ),
        clock=clock,
        audio_dir=tmp_path / "audio",
        playout_block_seconds=1.0,
    )
    station.set_market(market_state())
    await station.start()
    try:
        yield station, generation, database, clock
    finally:
        # Shut down *while driving the clock*. §74's teardown waits for in-flight generation,
        # and that wait is on the injected clock — under a virtual one with nobody advancing
        # it, the station never finishes stopping and the test hangs rather than failing.
        await _drive_until(clock, asyncio.create_task(station.stop(), name="api-test-stop"))
        await database.disconnect()


async def _drive_until(clock: VirtualClock, task: asyncio.Task[None]) -> None:
    """Advance virtual time until ``task`` completes."""
    while not task.done():
        if clock.pending_waiters:
            await clock.advance_to_next()
        else:
            await asyncio.sleep(0)
    await task


@pytest.fixture
def view(
    station_fixture: tuple[RadioStation, GenerationManager, Database, VirtualClock],
    settings: AppSettings,
) -> RuntimeView:
    station, generation, database, _clock = station_fixture
    return RuntimeView(
        settings=settings,
        station=station,
        market_service=_StubMarketService(market_state()),
        generation=generation,
        database=database,
        capabilities=detect_capabilities(
            has_station=True,
            has_market_feed=True,
            has_generation=True,
            simulation_allowed=True,
            gpu_present=False,
            provider_name="mock",
        ),
    )


@pytest.fixture
def client(view: RuntimeView) -> Iterator[TestClient]:
    with TestClient(create_app(view, serve_frontend=False)) as test_client:
        yield test_client


# ----------------------------------------------------------------- shape


async def test_a_health_does_not_touch_the_station(client: TestClient) -> None:
    """A liveness probe must stay up when the station is in trouble."""
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_b_status_returns_one_coherent_frame(client: TestClient) -> None:
    response = client.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    for key in ("status", "market", "queue", "buffer", "emergency", "health", "capabilities"):
        assert key in body, key


async def test_c_the_websocket_frame_matches_the_rest_frame(
    client: TestClient, view: RuntimeView
) -> None:
    """One builder, so a page cannot see two different stations.

    If these ever diverge, a dashboard that loads over REST and then upgrades to the socket
    would visibly change its mind about the station a second after opening.
    """
    rest = client.get("/api/status").json()
    with client.websocket_connect("/ws") as socket:
        first = socket.receive_json()
    assert first["type"] == "state"
    assert set(first["payload"]) == set(rest)


async def test_d_the_socket_sends_state_before_anything_is_asked_of_it(
    client: TestClient,
) -> None:
    """An operator opening the dashboard should see the station, not a spinner."""
    with client.websocket_connect("/ws") as socket:
        message = socket.receive_json()
    assert message["type"] == "state"
    assert message["payload"]["status"]["station_name"] == "TRADE FIX RADIO"


# ----------------------------------------------------------------- honesty


async def test_e_a_stale_feed_withholds_the_price(
    view: RuntimeView, client: TestClient
) -> None:
    """§21: a price may only be shown from a current state.

    Withheld rather than flagged. A number on screen is read as current however it is
    styled, so the only safe representation of an old price is no price.
    """
    view.market_service.current_state = market_state(  # type: ignore[union-attr]
        price=4012.5, data_age=120.0, feed_status=FeedStatus.LIVE
    )
    body = client.get("/api/market/current").json()
    assert body["is_stale"] is True
    assert body["price"] is None
    assert body["price_change"] is None


async def test_f_a_fresh_live_feed_reports_its_price(
    view: RuntimeView, client: TestClient
) -> None:
    view.market_service.current_state = market_state(  # type: ignore[union-attr]
        price=4012.5, data_age=1.0, feed_status=FeedStatus.LIVE
    )
    body = client.get("/api/market/current").json()
    assert body["is_stale"] is False
    assert body["price"] == pytest.approx(4012.5)


async def test_f2_a_simulated_feed_can_never_carry_a_price(client: TestClient) -> None:
    """§21 enforced one layer deeper than the UI.

    :class:`MarketStateV1` rejects a price on a simulated feed outright, so the dashboard
    cannot show a fabricated one even if every layer above it tried to. Worth pinning here
    because it is the strongest form the rule takes anywhere in the system.
    """
    with pytest.raises(ValueError, match="price must be None"):
        market_state(price=4012.5, feed_status=FeedStatus.SIMULATED)
    assert client.get("/api/market/current").json()["price"] is None


async def test_g_a_simulated_feed_is_always_marked(client: TestClient) -> None:
    """§72: simulation must never be visually indistinguishable from live."""
    assert client.get("/api/market/current").json()["is_simulated"] is True


async def test_h_unbuilt_subsystems_refuse_rather_than_return_zeroes(
    client: TestClient,
) -> None:
    """§86. A 200 with empty arrays would render as "0 rejections" for a subsystem that does
    not exist — which is the fabrication the rule forbids."""
    for path in ("/api/originality/summary", "/api/obs/status"):
        response = client.get(path)
        assert response.status_code == 409, path
        detail = response.json()["detail"]
        assert detail["state"] == "planned"
        assert detail["arrives_in_phase"] in (6, 8)
        assert detail["detail"]


async def test_i_capabilities_name_the_phase_that_delivers_them(
    client: TestClient,
) -> None:
    reports = {row["capability"]: row for row in client.get("/api/capabilities").json()}
    assert reports["originality"]["arrives_in_phase"] == 6
    assert reports["obs"]["arrives_in_phase"] == 8
    assert reports["watchdog"]["arrives_in_phase"] == 9
    assert reports["playout"]["state"] == "ready"


async def test_j_an_absent_gpu_reports_null_not_zero(client: TestClient) -> None:
    """A 0 °C GPU reads as a working sensor on a very cold card."""
    body = client.get("/api/system/resources").json()
    assert body["gpu_name"] is None
    assert body["gpu_temperature_c"] is None
    assert body["vram_total_mb"] is None
    # The host figures are real and must be present.
    assert body["process_memory_mb"] > 0
    assert body["active_tasks"] >= 1


# ----------------------------------------------------------------- runtime truth


async def test_k_the_queue_reports_real_lock_levels(
    client: TestClient,
    station_fixture: tuple[RadioStation, GenerationManager, Database, VirtualClock],
) -> None:
    """§28's layering, as the UI sees it. The labels are the ones the brief asks for."""
    _station, _generation, _database, clock = station_fixture
    await clock.run_to(clock.monotonic() + 30.0)

    items = client.get("/api/radio/queue").json()
    assert items, "the scheduler planned nothing"
    assert items[0]["lock_label"] == "HARD"
    assert items[0]["is_protected"] is True
    assert {item["lock_label"] for item in items} <= {"HARD", "SOFT", "FLEXIBLE", "PINNED"}
    # "Starts in" accumulates down the queue, so the UI never computes it twice.
    offsets = [item["starts_in_seconds"] for item in items]
    assert offsets == sorted(offsets)


async def test_l_the_buffer_reports_a_trend_an_operator_can_act_on(
    client: TestClient,
) -> None:
    body = client.get("/api/radio/buffer").json()
    assert body["level"] in {"healthy", "low", "critical", "empty"}
    assert body["trajectory"] in {"filling", "holding", "draining", "stalled"}
    assert body["reason"], "a trajectory with no stated reason is not actionable"
    # Minutes per hour, not the monitor's internal minutes-per-minute.
    assert isinstance(body["trend_minutes_per_hour"], (int, float))


async def test_m_time_to_failure_is_absent_unless_the_buffer_is_shrinking(
    client: TestClient,
) -> None:
    """A countdown that is always on screen stops being read."""
    body = client.get("/api/radio/buffer").json()
    if body["trajectory"] in {"filling", "holding"}:
        assert body["seconds_to_failure"] is None


async def test_n_the_emergency_tier_is_always_exposed(client: TestClient) -> None:
    body = client.get("/api/radio/emergency").json()
    assert body["tier_label"] in {"NORMAL", "TIER 2 RESERVE", "TIER 3 PROCEDURAL"}
    assert isinstance(body["is_degraded"], bool)


async def test_o_now_playing_comes_from_the_playout_engine(
    client: TestClient,
    station_fixture: tuple[RadioStation, GenerationManager, Database, VirtualClock],
) -> None:
    """Gate B: the dashboard's position is the engine's frame count, not a timestamp."""
    station, _generation, _database, clock = station_fixture
    await clock.run_to(clock.monotonic() + 20.0)

    body = client.get("/api/radio/current").json()
    assert body is not None
    engine_item = station.playout.current
    assert engine_item is not None
    assert body["track_id"] == engine_item.track_id
    assert body["elapsed_seconds"] == pytest.approx(
        station.playout.position_seconds, abs=0.01
    )
    assert 0.0 <= body["progress"] <= 1.0
    assert body["origin"]


async def test_p_why_this_track_uses_stored_rationale_only(
    client: TestClient,
    station_fixture: tuple[RadioStation, GenerationManager, Database, VirtualClock],
) -> None:
    """The panel shows what the director recorded, never generated reasoning."""
    station, _generation, _database, clock = station_fixture
    # Run until a *scheduled* track is on air. A cold start opens on Tier 3, which has no
    # blueprint and therefore no recorded reasoning — a correct answer, and not the one this
    # test is about.
    for _ in range(40):
        await clock.run_to(clock.monotonic() + 15.0)
        current = station.playout.current
        if current is not None and current.entry is not None:
            break
    else:
        pytest.skip("no scheduled track reached the air within the window")

    body = client.get("/api/radio/current").json()
    assert body is not None
    reason = body.get("reason")
    if reason is None:  # an emergency tier has no blueprint, which is a valid answer
        assert body["tier"] != "scheduled"
        return
    assert reason["factors"], "the director recorded no rationale"
    assert all(isinstance(line, str) for line in reason["factors"])
    assert reason["market_regime"]
    assert 0.0 <= reason["market_energy"] <= 100.0


async def test_q_health_rows_explain_themselves(client: TestClient) -> None:
    """The brief's standard: no log-reading to understand a basic problem."""
    rows = client.get("/api/status").json()["health"]
    assert rows
    for row in rows:
        assert row["detail"], f"{row['name']} has a state with no explanation"
        assert row["state"] in {
            "healthy",
            "degraded",
            "critical",
            "recovering",
            "offline",
        }
    assert {row["name"] for row in rows} >= {"playout", "generator", "queue", "emergency"}


# ----------------------------------------------------------------- controls


async def test_r_skip_asks_the_engine_rather_than_touching_audio(
    client: TestClient,
    station_fixture: tuple[RadioStation, GenerationManager, Database, VirtualClock],
) -> None:
    station, _generation, _database, clock = station_fixture
    await clock.run_to(clock.monotonic() + 15.0)
    playing = station.playout.current
    assert playing is not None

    response = client.post("/api/radio/skip")
    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert playing.track_id in body["message"]

    # The engine finishes it at the next block boundary; the audio path is untouched.
    await clock.run_to(clock.monotonic() + 3.0)
    assert station.playout.stats.unintended_silence_seconds == 0.0


async def test_s_locking_pins_a_slot_against_a_replan(
    client: TestClient,
    station_fixture: tuple[RadioStation, GenerationManager, Database, VirtualClock],
) -> None:
    station, _generation, _database, clock = station_fixture
    await clock.run_to(clock.monotonic() + 30.0)
    target = next(
        (e for e in station.queue if e.lock_level is QueueLockLevel.REPLACEABLE), None
    )
    assert target is not None, "no flexible slot to pin"

    body = client.post(f"/api/radio/queue/{target.track_id}/lock").json()
    assert body["applied"] is True
    assert body["lock"] == "operator_pinned"
    assert station.queue.get(target.track_id).lock_level is QueueLockLevel.OPERATOR_PINNED


async def test_t_unlocking_returns_the_slot_to_its_positional_lock(
    client: TestClient,
    station_fixture: tuple[RadioStation, GenerationManager, Database, VirtualClock],
) -> None:
    """§28 computes lock level from position; forcing REPLACEABLE would contradict it."""
    station, _generation, _database, clock = station_fixture
    await clock.run_to(clock.monotonic() + 30.0)
    head = station.queue.snapshot().entries[0]
    client.post(f"/api/radio/queue/{head.track_id}/lock")

    body = client.post(f"/api/radio/queue/{head.track_id}/unlock").json()
    assert body["applied"] is True
    # The head is hard-locked by position, so unpinning restores that rather than freeing it.
    assert body["lock"] == "locked"
    assert station.queue.get(head.track_id).lock_level is QueueLockLevel.LOCKED


async def test_u_unlocking_something_that_is_not_pinned_says_so(
    client: TestClient,
    station_fixture: tuple[RadioStation, GenerationManager, Database, VirtualClock],
) -> None:
    station, _generation, _database, clock = station_fixture
    await clock.run_to(clock.monotonic() + 30.0)
    head = station.queue.snapshot().entries[0]

    body = client.post(f"/api/radio/queue/{head.track_id}/unlock").json()
    assert body["applied"] is False
    assert "position" in body["message"]


async def test_v_controls_on_an_unknown_track_are_404(client: TestClient) -> None:
    assert client.post("/api/radio/queue/NOPE/lock").status_code == 404
    assert client.post("/api/radio/queue/NOPE/unlock").status_code == 404


# ----------------------------------------------------------------- simulation


async def test_w_the_simulator_scenario_can_be_switched(
    client: TestClient, view: RuntimeView
) -> None:
    """Gate D: a control the operator presses reaches the real feed."""
    response = client.post("/api/simulation/regime", json={"scenario": "violent_breakout"})
    assert response.status_code == 200
    assert response.json()["applied"] is True
    assert view.market_service.feed.scenario == "violent_breakout"  # type: ignore[union-attr]


async def test_x_an_unknown_scenario_is_rejected(client: TestClient) -> None:
    response = client.post("/api/simulation/regime", json={"scenario": "to_the_moon"})
    assert response.status_code == 422


async def test_y_simulation_is_refused_when_the_mode_forbids_it(
    view: RuntimeView, settings: AppSettings
) -> None:
    """§72: production has no simulated feed, so there is nothing to switch."""
    view.capabilities = detect_capabilities(
        has_station=True,
        has_market_feed=True,
        has_generation=True,
        simulation_allowed=False,
        gpu_present=False,
        provider_name="mock",
    )
    with TestClient(create_app(view, serve_frontend=False)) as client:
        response = client.post("/api/simulation/regime", json={"scenario": "flat"})
        assert response.status_code == 403
        assert "development and simulation" in response.json()["detail"]
        assert (
            view.capabilities[Capability.SIMULATION].state is CapabilityState.UNAVAILABLE
        )


# ----------------------------------------------------------------- database-backed


async def test_z_the_library_paginates(client: TestClient) -> None:
    body = client.get("/api/library/tracks?limit=5").json()
    assert set(body) == {"items", "total", "offset", "limit"}
    assert body["limit"] == 5
    assert len(body["items"]) <= 5


async def test_z2_an_unknown_track_is_404(client: TestClient) -> None:
    assert client.get("/api/library/tracks/NOPE").status_code == 404


async def test_z3_analytics_counts_rather_than_estimates(client: TestClient) -> None:
    """An empty database yields zeros, which the UI renders as an empty state."""
    body = client.get("/api/analytics?window=24h").json()
    assert body["window"] == "24h"
    assert body["tracks_generated"] >= 0
    assert isinstance(body["genre_distribution"], list)


async def test_z4_the_job_monitor_lists_real_jobs(
    client: TestClient,
    station_fixture: tuple[RadioStation, GenerationManager, Database, VirtualClock],
) -> None:
    _station, _generation, _database, clock = station_fixture
    await clock.run_to(clock.monotonic() + 30.0)

    jobs = client.get("/api/generation/jobs?limit=10").json()
    assert jobs, "the station planned no generation jobs"
    for job in jobs:
        assert job["job_id"] and job["track_id"]
        assert job["attempt"] >= 1
        assert job["age_seconds"] >= 0.0
    counts = client.get("/api/generation/counts").json()
    assert counts["total"] >= len(jobs)


async def test_z5_an_unknown_job_state_is_rejected(client: TestClient) -> None:
    assert client.get("/api/generation/jobs?state=banana").status_code == 422


# ----------------------------------------------------------------- degraded


async def test_z6_the_api_runs_without_a_station_and_says_so(
    settings: AppSettings,
) -> None:
    """The frontend's `vite dev` loop points at an API with no runtime attached.

    It must answer rather than crash, and it must be obvious that nothing is broadcasting —
    an empty dashboard that looks like a healthy quiet station would be the worst outcome.
    """
    view = RuntimeView(
        settings=settings,
        capabilities=detect_capabilities(
            has_station=False,
            has_market_feed=False,
            has_generation=False,
            simulation_allowed=True,
            gpu_present=False,
            provider_name="mock",
        ),
    )
    with TestClient(create_app(view, serve_frontend=False)) as client:
        status_body = client.get("/api/status").json()
        assert status_body["status"]["is_broadcasting"] is False
        assert status_body["now_playing"] is None
        assert status_body["queue"] == []
        assert client.get("/api/radio/current").status_code == 503
        assert client.get("/api/radio/queue").status_code == 503
        health = status_body["health"]
        assert health[0]["state"] == "offline"


async def test_z7_building_a_frame_twice_is_stable(view: RuntimeView) -> None:
    """A snapshot is a pure read, so two in a row agree on everything structural."""
    first = build_live_state(view)
    second = build_live_state(view)
    assert first.status.is_broadcasting == second.status.is_broadcasting
    assert [item.track_id for item in first.queue] == [
        item.track_id for item in second.queue
    ]
    assert first.emergency == second.emergency


# ----------------------------------------------------------------- static hosting


async def test_z8_the_app_serves_the_built_frontend_when_present(
    view: RuntimeView,
) -> None:
    """A deployment is one process: the API serves the built UI from the same origin.

    Worth its own test because every other test here constructs the app with
    ``serve_frontend=False`` — and the first time the real thing ran with the frontend built,
    it failed at import. FastAPI cannot derive a response model from a union of two Response
    types, so the catch-all route has to opt out of one.
    """
    from tradefix_radio.api.app import _FRONTEND_DIST

    app = create_app(view, serve_frontend=True)
    with TestClient(app) as client:
        # The API must keep answering regardless of whether the UI is mounted.
        assert client.get("/api/health").status_code == 200
        if _FRONTEND_DIST.is_dir():
            # The SPA fallback serves index.html for a client-side route...
            page = client.get("/radio")
            assert page.status_code == 200
            assert "text/html" in page.headers["content-type"]
            # ...but never for an API path, which must still 404 honestly.
            assert client.get("/api/nope").status_code == 404
