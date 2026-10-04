"""VISUAL DEMO MODE.

Two things worth asserting here, and they are both about what demo mode must *not* do.

It must not reach production. The whole safety argument for a mode that accepts button
presses from a web page is that the page can only reach a state source holding nothing
mutable — so `test_demo_mode_cannot_reach_production` reads this module's own imports
rather than taking the docstring's word for it.

It must not reset the character when the symbol changes. That is an explicit requirement,
and the natural wrong implementation — rebuilding the director for the new market — would
look fine in a screenshot and be obvious on a stream.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from tradefix_radio.core.clock import UTC, VirtualClock
from tradefix_radio.visual.camera import CAMERA_METADATA, CAMERA_NAMES
from tradefix_radio.visual.catalog import CATALOG
from tradefix_radio.visual.contracts import IntensityBand
from tradefix_radio.visual.demo import (
    CYCLE_SECONDS,
    DEMO_TIMELINE,
    PANEL_BAND_PROFILE,
    PANEL_BANDS,
    PANEL_BPM,
    PANEL_SYMBOLS,
    DemoStateSource,
)
from tradefix_radio.visual.service import RUNTIME_DIR, VisualRuntime, create_app

from datetime import datetime


def _source(seed: int = 7) -> tuple[DemoStateSource, VirtualClock]:
    clock = VirtualClock(datetime(2026, 10, 3, 22, 0, tzinfo=UTC))
    return DemoStateSource(clock=clock, seed=seed), clock


# ============================================================ the timeline


def test_the_timeline_matches_the_requested_show() -> None:
    labels = [(p.scenario.symbol, p.label) for p in DEMO_TIMELINE]
    assert labels == [
        ("XAUUSD", "QUIET"),
        ("XAUUSD", "TREND"),
        ("XAUUSD", "BREAKOUT"),
        ("XAUUSD", "EXTREME_VOLATILITY"),
        ("BTCUSD", "TREND"),
        ("BTCUSD", "BREAKOUT"),
    ]
    assert CYCLE_SECONDS == 360.0
    assert all(phase.seconds == 60.0 for phase in DEMO_TIMELINE)


def test_energy_and_tempo_rise_with_the_band() -> None:
    """The demo exists to show the market driving behaviour; the inputs must move."""
    rising = DEMO_TIMELINE[:4]
    assert [p.scenario.market_energy for p in rising] == sorted(
        p.scenario.market_energy for p in rising
    )
    assert [p.scenario.bpm for p in rising] == sorted(p.scenario.bpm for p in rising)
    assert [p.scenario.base_salience for p in rising] == sorted(
        p.scenario.base_salience for p in rising
    )


def test_the_show_walks_its_phases_in_order() -> None:
    source, clock = _source()
    seen = []
    for _ in range(6):
        seen.append(source.snapshot()["phase"])
        clock.advance_sync(60.0)
    assert seen == [p.label for p in DEMO_TIMELINE]


def test_the_symbol_switches_part_way_through() -> None:
    source, clock = _source()
    clock.advance_sync(30.0)
    assert source.state().active_symbol == "XAUUSD"
    clock.advance_sync(240.0)
    assert source.state().active_symbol == "BTCUSD"


def test_later_cycles_vary_rather_than_looping() -> None:
    source, clock = _source()
    first = source.snapshot()["market"]["energy"]
    clock.advance_sync(CYCLE_SECONDS)
    second = source.snapshot()["market"]["energy"]
    clock.advance_sync(CYCLE_SECONDS)
    third = source.snapshot()["market"]["energy"]
    assert len({first, second, third}) > 1, "the show is a loop"


def test_variation_is_deterministic_from_the_seed() -> None:
    """Same seed, same show. A demo nobody can reproduce cannot be reported on."""
    a, clock_a = _source(seed=11)
    b, clock_b = _source(seed=11)
    for _ in range(14):
        clock_a.advance_sync(45.0)
        clock_b.advance_sync(45.0)
        assert a.snapshot()["market"] == b.snapshot()["market"]
        assert a.snapshot()["music"] == b.snapshot()["music"]


def test_a_different_seed_gives_a_different_show() -> None:
    a, clock_a = _source(seed=11)
    b, clock_b = _source(seed=12)
    clock_a.advance_sync(CYCLE_SECONDS + 90)
    clock_b.advance_sync(CYCLE_SECONDS + 90)
    assert a.snapshot()["market"] != b.snapshot()["market"]


def test_variation_stays_inside_sane_bounds() -> None:
    """Jitter must not produce a 300 bpm track or a negative market."""
    source, clock = _source()
    for _ in range(300):
        clock.advance_sync(37.0)
        state = source.state()
        assert 5.0 <= state.market_energy <= 99.0
        assert 72 <= state.music_bpm <= 176
        assert 0.15 <= state.music_energy <= 0.98
        assert 0.0 <= state.reaction_salience <= 0.5


def test_the_cycle_boundary_does_not_jump_to_peak() -> None:
    """Every cycle opens on the quiet phase, so the build is always visible."""
    source, clock = _source()
    for cycle in range(5):
        clock.advance_sync(CYCLE_SECONDS if cycle else 1.0)
        assert source.snapshot()["phase"] in {"QUIET", "quiet".upper()}


# ============================================================ overrides


def test_a_band_override_moves_energy_and_tempo_with_it() -> None:
    """A button that changed only the band would barely alter the behaviour."""
    source, _ = _source()
    for label, (energy, velocity, bpm, music, salience) in PANEL_BAND_PROFILE.items():
        source.set_band(label)
        state = source.state()
        assert state.intensity_band is PANEL_BANDS[label]
        assert state.market_energy == energy
        assert state.market_energy_velocity == velocity
        assert state.music_bpm == bpm
        assert state.music_energy == music
        assert state.reaction_salience == salience


def test_overrides_are_sticky_until_resumed() -> None:
    """Held across phase boundaries, so an operator can study one condition.

    Checked inside the QUIET window rather than at an arbitrary time: at 200 s the
    timeline is naturally in EXTREME_VOLATILITY, and a test that resumed there would
    pass whether or not `resume` did anything.
    """
    source, clock = _source()
    clock.advance_sync(20.0)
    assert source.state().intensity_band is IntensityBand.B1_QUIET

    source.set_band("extreme")
    assert source.state().intensity_band is IntensityBand.B5_PEAK
    clock.advance_sync(25.0)
    assert source.state().intensity_band is IntensityBand.B5_PEAK, "override expired"
    assert source.overridden is True

    source.resume()
    assert source.state().intensity_band is IntensityBand.B1_QUIET
    assert source.overridden is False


def test_a_music_override_does_not_change_the_market() -> None:
    """Music is a weaker input than the market, and the panel must not conflate them."""
    source, _ = _source()
    before = source.state()
    source.set_music("high")
    after = source.state()
    assert after.music_bpm == PANEL_BPM["high"][0]
    assert after.market_energy == before.market_energy
    assert after.intensity_band is before.intensity_band


def test_a_symbol_override_changes_nothing_but_the_symbol() -> None:
    source, _ = _source()
    before = source.state()
    source.set_symbol("BTCUSD")
    after = source.state()
    assert after.active_symbol == "BTCUSD"
    assert after.intensity_band is before.intensity_band
    assert after.market_energy == before.market_energy
    assert after.music_bpm == before.music_bpm


def test_unknown_overrides_are_refused() -> None:
    source, _ = _source()
    for call, bad in (
        (source.set_band, "sideways"),
        (source.set_symbol, "DOGEUSD"),
        (source.set_music, "medium"),
    ):
        with pytest.raises(KeyError):
            call(bad)


def test_the_snapshot_carries_what_the_hud_shows() -> None:
    source, _ = _source()
    shot = source.snapshot()
    for key in ("cycle", "phase", "headline", "seconds_remaining", "elapsed",
                "symbol_changes", "overridden", "overrides", "market", "music",
                "timeline"):
        assert key in shot, key
    assert set(shot["market"]) >= {"symbol", "band", "energy", "energy_velocity",
                                   "direction", "salience"}
    assert set(shot["music"]) >= {"bpm", "energy", "genre"}


# ============================================================ isolation


def test_demo_mode_cannot_reach_production() -> None:
    """Demo mode is a value source. It must hold nothing it could mutate.

    Asserted by reading the imports rather than trusting the docstring, because the
    failure mode is somebody adding a convenient repository handle later.
    """
    source = (
        RUNTIME_DIR.parents[1] / "tradefix_radio" / "visual" / "demo.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "tradefix_radio.persistence",
        "tradefix_radio.radio",
        "tradefix_radio.generation",
        "tradefix_radio.core.events",
        "tradefix_radio.market",
        "tradefix_radio.audio",
        "tradefix_radio.api",
    ):
        assert forbidden not in source, f"demo.py imports {forbidden}"


def test_the_state_source_is_a_pure_function_of_the_clock() -> None:
    """Two sources on the same clock and seed must agree, forever."""
    a, clock = _source()
    b = DemoStateSource(clock=clock, seed=7)
    for _ in range(20):
        clock.advance_sync(23.0)
        left, right = a.state(), b.state()
        assert left.active_symbol == right.active_symbol
        assert left.intensity_band is right.intensity_band
        assert left.market_energy == right.market_energy


# ============================================================ the service


def _client() -> TestClient:
    return TestClient(create_app(VisualRuntime(seed=7, demo=DemoStateSource(seed=7))))


def test_the_runtime_reports_demo_mode() -> None:
    with _client() as client:
        assert client.get("/api/visual/state").json()["mode"] == "demo"
        assert client.get("/api/visual/demo").json()["phase"] == "QUIET"


def test_the_hud_blocks_are_all_present() -> None:
    """Each one backs a panel in the page; a missing block is a dash on screen."""
    with _client() as client:
        state = client.get("/api/visual/state").json()
    for block in ("character", "rhythm", "drive", "camera", "market", "music",
                  "anti_repeat", "demo", "quality", "target"):
        assert state[block] is not None, block
    assert set(state["camera"]) >= {
        "live", "name", "auto", "hold_target", "held_for", "last_reason",
        "cut_eligible", "seconds_to_eligible", "recent",
    }
    assert set(state["anti_repeat"]) >= {
        "recent_actions", "recent_action_count", "recent_cameras", "recent_camera_count",
    }


def test_the_camera_panel_names_every_shot() -> None:
    assert set(CAMERA_NAMES) == set(CAMERA_METADATA)
    assert CAMERA_NAMES["CAM_1"] == "HERO FRONT"
    assert CAMERA_NAMES["CAM_6"] == "HANDS / DESK"


def test_the_panel_endpoints_accept_what_the_buttons_send() -> None:
    with _client() as client:
        for label in PANEL_BANDS:
            assert client.post(f"/api/visual/demo/market/{label}").status_code == 200
        for symbol in PANEL_SYMBOLS:
            assert client.post(f"/api/visual/demo/symbol/{symbol}").status_code == 200
        for label in PANEL_BPM:
            assert client.post(f"/api/visual/demo/music/{label}").status_code == 200
        for camera_id in CAMERA_METADATA:
            assert client.post(f"/api/visual/camera/{camera_id}").status_code == 200
        assert client.post("/api/visual/demo/resume").status_code == 200


def test_the_panel_rejects_what_it_should_not_accept() -> None:
    with _client() as client:
        assert client.post("/api/visual/demo/market/sideways").status_code == 404
        assert client.post("/api/visual/demo/symbol/DOGE").status_code == 404
        assert client.post("/api/visual/demo/music/medium").status_code == 404
        assert client.post("/api/visual/camera/CAM_9").status_code == 404


def test_the_demo_endpoints_are_inert_outside_demo_mode() -> None:
    """A scenario-mode or live runtime must not accept demo overrides."""
    app = create_app(VisualRuntime(scenario="range", seed=7))
    with TestClient(app) as client:
        assert client.get("/api/visual/demo").status_code == 409
        assert client.post("/api/visual/demo/market/quiet").status_code == 409
        assert client.post("/api/visual/demo/symbol/BTCUSD").status_code == 409


def test_the_character_survives_a_symbol_change() -> None:
    """The explicit requirement: switching market must not reset the character.

    Checked on the counters rather than on the state name — a reset director would very
    likely land back in the same state, and `actions_performed` dropping to zero is the
    thing that cannot happen by accident.
    """
    with _client() as client:
        time.sleep(2.0)
        before = client.get("/api/visual/state").json()
        client.post("/api/visual/demo/symbol/BTCUSD")
        time.sleep(0.6)
        client.post("/api/visual/demo/symbol/XAUUSD")
        time.sleep(0.6)
        after = client.get("/api/visual/state").json()

    assert after["character"]["actions_performed"] >= (
        before["character"]["actions_performed"]
    )
    assert after["rhythm"]["time_in_phase"] >= before["rhythm"]["time_in_phase"]
    assert after["market"]["symbol"] == "XAUUSD"


def test_a_blocked_trigger_names_the_conflicting_lock() -> None:
    """`BLOCKED: <reason>` has to say which lock, not list every busy body part."""
    app = create_app(VisualRuntime(seed=7, demo=DemoStateSource(seed=7)))
    with TestClient(app) as client:
        # Take the coffee, then ask for the pen: the one-object rule must refuse it.
        assert client.post("/api/visual/trigger/pick_cup").status_code in (200, 409)
        blocked = None
        for action_id in ("acquire_pen", "reach_pen", "note_write_short"):
            response = client.post(f"/api/visual/trigger/{action_id}")
            if response.status_code == 409:
                blocked = response.json()
                break
        if blocked is None:
            pytest.skip("nothing was holding a lock at this moment")
        assert blocked["reason"]
        assert "reason" in blocked and blocked["triggered"] is False
        # The reason must be specific, not the whole held set.
        assert len(blocked["reason"]) < 120


# ============================================================ manual camera mode


def _warm(seed: int = 7) -> tuple[object, VirtualClock, object]:
    """A director with a camera live and its hold already running."""
    import random

    from tradefix_radio.visual.director import BehaviorDirector

    clock = VirtualClock(datetime(2026, 10, 3, 22, 0, tzinfo=UTC))
    source = DemoStateSource(clock=clock, seed=seed)
    director = BehaviorDirector(clock=clock, rng=random.Random(seed))
    for _ in range(200):
        clock.advance_sync(0.05)
        director.tick(source.state())
    return director, clock, source


def _run_until_cut(director, clock, source, seconds: float):
    for _ in range(int(seconds / 0.05)):
        clock.advance_sync(0.05)
        out = director.tick(source.state())
        if out.camera_cut is not None:
            return out.camera_cut
    return None


def test_a_manual_request_is_honoured_while_auto_is_off() -> None:
    """The bug this guards: turning `auto` off stopped the camera director ticking, so
    every operator request queued forever and manual mode locked whichever camera
    happened to be live rather than the one chosen."""
    director, clock, source = _warm()
    director.camera.auto = False
    director.camera_director.request("CAM_6")

    cut = _run_until_cut(director, clock, source, 120.0)
    assert cut is not None, "the manual request never landed"
    assert cut.camera_id == "CAM_6"
    assert director.camera.camera_id == "CAM_6"


def test_an_operator_cut_still_waits_for_the_absolute_floor() -> None:
    """`MINIMUM_HOLD_FLOOR` outranks every motivation, operator requests included."""
    from tradefix_radio.visual.camera_director import MINIMUM_HOLD_FLOOR

    director, clock, source = _warm()
    held_at_request = director.camera.hold_seconds(clock.monotonic())
    director.camera.auto = False
    director.camera_director.request("CAM_6")

    start = clock.monotonic()
    cut = _run_until_cut(director, clock, source, 120.0)
    assert cut is not None
    waited = clock.monotonic() - start
    assert waited + held_at_request >= MINIMUM_HOLD_FLOOR - 1e-6, (
        f"cut after {waited + held_at_request:.1f}s, below the {MINIMUM_HOLD_FLOOR}s floor"
    )


def test_an_operator_cut_is_exempt_from_the_per_camera_minimum() -> None:
    """Pacing, not safety — and the panel has to be usable for inspecting all seven.

    `CAM_1`'s own minimum is 50 s. An operator request must not have to wait it out,
    because `MOTIVATION_EARLIEST[OPERATOR] = 0.0` already declares the exemption.
    """
    from tradefix_radio.visual.camera_director import MINIMUM_HOLD_FLOOR

    director, clock, source = _warm()
    live = director.camera.camera_id
    minimum = CAMERA_METADATA[live].minimum_hold_seconds
    assert minimum > MINIMUM_HOLD_FLOOR, "pick a camera whose minimum exceeds the floor"

    held_at_request = director.camera.hold_seconds(clock.monotonic())
    director.camera.auto = False
    director.camera_director.request("CAM_6")
    start = clock.monotonic()
    cut = _run_until_cut(director, clock, source, 120.0)

    assert cut is not None
    total = (clock.monotonic() - start) + held_at_request
    assert total < minimum, (
        f"waited {total:.1f}s for {live}'s {minimum}s minimum — the operator exemption "
        "is not applied"
    )


def test_manual_mode_takes_no_cuts_of_its_own() -> None:
    """Locked means locked: no automatic cut until AUTO is restored."""
    director, clock, source = _warm()
    director.camera.auto = False
    locked = director.camera.camera_id
    assert _run_until_cut(director, clock, source, 900.0) is None
    assert director.camera.camera_id == locked


def test_restoring_auto_resumes_automatic_cuts() -> None:
    director, clock, source = _warm()
    director.camera.auto = False
    _run_until_cut(director, clock, source, 300.0)
    director.camera.auto = True
    assert _run_until_cut(director, clock, source, 900.0) is not None


def test_safety_vetoes_still_apply_to_an_operator_cut() -> None:
    """The exemption is narrow: it covers the per-camera minimum and nothing else."""
    from tradefix_radio.visual.camera_director import CutVeto

    safety = {veto for veto in CutVeto if veto.is_safety}
    assert CutVeto.BELOW_SAMPLED_HOLD not in safety, (
        "the per-camera minimum is classed as safety; the operator exemption would then "
        "be bypassing a safety veto"
    )
    assert CutVeto.BELOW_FLOOR not in safety  # a pacing floor, but absolute
    for veto in (CutVeto.MID_BLEND, CutVeto.OBJECT_ACQUISITION,
                 CutVeto.COMMITTED_CHAIN_INVISIBLE, CutVeto.REACTION_LOCKOUT):
        assert veto in safety


# ============================================================ the page


def test_the_control_panel_only_calls_real_action_ids() -> None:
    """A button wired to a typo is a button that always reports BLOCKED."""
    html = (RUNTIME_DIR / "index.html").read_text(encoding="utf-8")
    import re

    triggers = re.findall(r'data-trigger="([^"]+)"', html)
    assert len(triggers) >= 16, "the panel lost its buttons"
    for action_id in triggers:
        assert action_id in CATALOG, f"the panel triggers unknown action {action_id!r}"


def test_the_panel_covers_the_movements_asked_for() -> None:
    html = (RUNTIME_DIR / "index.html").read_text(encoding="utf-8")
    for label in ("BLINK", "TYPING", "MOUSE", "NOTE", "COFFEE", "HEADPHONES",
                  "POSTURE RESET", "MARKET REACTION",
                  "QUIET", "TREND", "BREAKOUT", "EXTREME",
                  "XAUUSD", "BTCUSD", "LOW BPM", "HIGH BPM", "AUTO"):
        assert label in html, label
    for index in range(1, 8):
        assert f'data-camera="CAM_{index}"' in html


def test_the_panel_is_opt_in_so_a_broadcast_source_cannot_show_it() -> None:
    source = (RUNTIME_DIR / "app.js").read_text(encoding="utf-8")
    assert "params.get('demo') === '1'" in source
    html = (RUNTIME_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="controls" class="off"' in html


def test_the_page_is_structurally_complete() -> None:
    html = (RUNTIME_DIR / "index.html").read_text(encoding="utf-8")
    for tag in ("<!doctype html>", "<html", "<head>", "</head>", "<body>", "</body>",
                "</html>"):
        assert tag in html, tag


def test_every_hud_element_the_script_writes_exists_in_the_page() -> None:
    """The HUD is the demo's explanation of itself; a missing id is a silent blank."""
    import re

    html = (RUNTIME_DIR / "index.html").read_text(encoding="utf-8")
    source = (RUNTIME_DIR / "app.js").read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([^"]+)"', html))
    written = set(re.findall(r"show\('([a-z]-[a-z0-9]+)'", source))
    written |= set(re.findall(r"el\('([a-z]-[a-z0-9]+)'\)", source))
    missing = sorted(written - ids)
    assert not missing, f"the script writes to ids the page does not have: {missing}"


def test_the_motion_map_covers_the_schedulable_catalogue() -> None:
    """Every action a viewer can see must move geometry, not just print its name.

    The point of the demo is to watch the behaviour system. An action that arrives as a
    HUD string and no movement is indistinguishable from one that did not fire.
    """
    import re

    source = (RUNTIME_DIR / "app.js").read_text(encoding="utf-8")
    block = source[source.index("const MOTION = {"):source.index("\n};", source.index(
        "const MOTION = {"))]
    mapped = set(re.findall(r"^  ([a-z_0-9]+):", block, re.MULTILINE))

    # Blinks are drawn by the lid channel rather than the motion map.
    blinks = {"blink", "double_blink", "slow_blink"}
    expected = set(CATALOG) - blinks
    missing = sorted(expected - mapped)
    assert not missing, f"actions with no visible representation: {missing}"


def test_reaches_resolve_against_frozen_anchors_only() -> None:
    """Interaction points must come from the blockout, never be typed in for the demo."""
    import re

    from tradefix_radio.visual.scene import build_scene

    source = (RUNTIME_DIR / "app.js").read_text(encoding="utf-8")
    block = source[source.index("const MOTION = {"):source.index("\n};", source.index(
        "const MOTION = {"))]
    referenced = set(re.findall(r"R\('[a-z_]+', '([A-Z_0-9]+)'", block))
    anchors = set(build_scene().anchors)
    body_points = set(re.findall(r"^  ([A-Z_]+):", source[
        source.index("const BODY_POINTS = {"):source.index("};", source.index(
            "const BODY_POINTS = {"))], re.MULTILINE))

    unknown = sorted(referenced - anchors - body_points)
    assert not unknown, f"the motion map reaches for unknown points: {unknown}"
    # And every desk interaction the brief names must be a frozen anchor.
    for required in ("ANCHOR_MOUSE", "ANCHOR_KEYBOARD_HOME_L", "ANCHOR_MUG_BODY",
                     "ANCHOR_NOTEBOOK", "ANCHOR_PEN", "ANCHOR_HP_CUP_L"):
        assert required in referenced, f"{required} is never reached for"
        assert required in anchors
