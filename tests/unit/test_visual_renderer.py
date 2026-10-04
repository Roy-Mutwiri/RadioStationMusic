"""The placeholder runtime: scene, projection, protocol and service.

The interesting test here is `test_cam_1_projection_matches_the_camera_plan`. The
placeholder renderer exists to verify that the seven frozen camera transforms produce the
compositions `CAMERA_PLAN.md` claims — and that claim can be checked without a GPU, by
running the renderer's own projection algorithm in Python and comparing frame positions.
If `CAM_1`'s bezel line does not land near 30 % of frame height, either the plan or the
geometry is wrong, and no amount of looking at a screenshot would tell you which.

The rest covers the wire protocol, the quality controller and the service routes. What it
cannot cover is pixels: that needs a browser, and `V8_REPORT.md` records the gap.
"""

from __future__ import annotations

import itertools
import json
import math
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tradefix_radio.core.clock import UTC
from tradefix_radio.visual.camera import CAMERA_METADATA
from tradefix_radio.visual.contracts import (
    FeedTrust,
    GazeTarget,
    IntensityBand,
    RendererTelemetryV1,
    RhythmPolicyV1,
    StationMode,
    VisualStateV1,
)
from tradefix_radio.visual.geometry import default_blockout
from tradefix_radio.visual.renderer import (
    PROTOCOL_VERSION,
    QUALITY_PROFILES,
    RESYNC_LAG_THRESHOLD,
    TARGET_FPS,
    TARGET_HEIGHT,
    TARGET_WIDTH,
    TELEMETRY_TIMEOUT_SECONDS,
    QualityController,
    camera_command,
    decode_telemetry,
    encode,
    resync_commands,
    rhythm_command,
    screen_command,
)
from tradefix_radio.visual.scene import (
    BOX_FACES,
    CHART_BARS,
    PALETTE,
    UNIFORMS_PER_QUAD,
    build_scene,
    frame_workload,
    layer_depths,
)
from tradefix_radio.visual.service import (
    RUNTIME_DIR,
    TELEMETRY_HISTORY,
    VisualRuntime,
    create_app,
    runtime_url,
)

REPO = Path(__file__).resolve().parents[2]


def _state(
    *,
    band: IntensityBand = IntensityBand.B2_STEADY,
    trust: FeedTrust = FeedTrust.LIVE,
) -> VisualStateV1:
    stale = trust is FeedTrust.STALE
    return VisualStateV1(
        at=datetime(2026, 10, 3, 22, 0, tzinfo=UTC),
        source_age_seconds=0.4,
        feed_trust=trust,
        active_symbol="XAUUSD" if not stale else "NO_ACTIVE_MARKET",
        market_regime=None if stale else "normal_range",
        intensity_band=IntensityBand.B0_DORMANT if stale else band,
        market_energy=None if stale else 48.0,
        music_bpm=110,
        music_energy=0.5,
        station_mode=StationMode.NORMAL,
        broadcasting=True,
        behavior_energy=0.5,
    )


# ============================================================ the scene


def test_the_scene_is_derived_from_the_frozen_blockout() -> None:
    scene = build_scene()
    geometry = default_blockout()
    assert scene.blockout_id == geometry.blockout_id
    assert scene.room == geometry.room
    assert len(scene.cameras) == 7
    assert set(scene.anchors) == set(geometry.anchors)


def test_every_scene_colour_is_a_palette_key() -> None:
    """The placeholder must not drift from `tailwind.config.js`."""
    scene = build_scene()
    for box in scene.boxes:
        assert box.colour in PALETTE, box.box_id
    for quad in scene.quads:
        assert quad.colour in PALETTE, quad.quad_id
    for joint in scene.joints:
        assert joint.colour in PALETTE, joint.joint_id


def test_the_character_is_a_parented_hierarchy() -> None:
    """The brief: preserve a believable joint hierarchy, not independent sprites."""
    scene = build_scene()
    joints = {joint.joint_id: joint for joint in scene.joints}
    # The brief's own example chain must exist, parented end to end.
    chain = ["hand_l", "forearm_l", "shoulder_l", "torso", "pelvis"]
    for child, parent in itertools.pairwise(chain):
        assert joints[child].parent == parent, f"{child} is not parented to {parent}"
    assert joints["pelvis"].parent is None


def test_no_joint_parent_is_dangling_or_cyclic() -> None:
    scene = build_scene()
    joints = {joint.joint_id: joint for joint in scene.joints}
    for joint in scene.joints:
        seen = set()
        node = joint
        while node.parent is not None:
            assert node.parent in joints, f"{node.joint_id} -> unknown {node.parent}"
            assert node.parent not in seen, f"cycle at {node.joint_id}"
            seen.add(node.parent)
            node = joints[node.parent]


def test_face_joints_are_parented_to_the_head() -> None:
    """Eyes and lids must move with the head, or gaze detaches from the face."""
    scene = build_scene()
    joints = {joint.joint_id: joint for joint in scene.joints}
    assert joints["eye_l"].parent == "head"
    assert joints["eye_r"].parent == "head"
    assert joints["lid_l"].parent == "eye_l"
    assert joints["headphones"].parent == "head"


def test_the_live_surfaces_are_the_monitors() -> None:
    scene = build_scene()
    live = {quad.quad_id for quad in scene.quads if quad.live}
    assert live == {"MON_1", "MON_2", "MON_3", "MON_4", "MON_5", "PANEL_W1", "PANEL_W2"}


def test_parallax_is_derived_from_depth_not_authored() -> None:
    """`ASSET_MANIFEST.md` §2: eyeballed parallax is how a 2.5D scene reads as cards."""
    depths = layer_depths("CAM_1")
    # The window is far; the mug is near. Near must travel more.
    assert depths["MUG"] > depths["WINDOW"]
    # And nothing travels more than a small fraction of the frame.
    assert max(depths.values()) < 0.05


def test_parallax_scales_with_the_cameras_amplitude() -> None:
    wide = layer_depths("CAM_5")   # amplitude 0.010
    hands = layer_depths("CAM_6")  # amplitude 0.002
    assert wide["DESK"] > hands["DESK"]


def test_the_scene_serialises_to_json() -> None:
    payload = build_scene().to_json()
    assert json.loads(json.dumps(payload))
    assert "PLACEHOLDER" in payload["note"]
    assert payload["units"] == "millimetres"


# ============================================================ the projection


def _look_at(
    eye: tuple[float, float, float], target: tuple[float, float, float]
) -> tuple[list[float], list[float], list[float], tuple[float, float, float]]:
    """The renderer's own right-handed look-at, re-implemented.

    `up` is +Z because the blockout is right-handed with Z up. Using +Y — the reflex for
    anyone coming from screen-space work — rolls every camera onto its side, which is the
    classic way a correct camera position still yields a wrong picture.
    """
    def subtract(a, b):
        return [a[i] - b[i] for i in range(3)]

    def normalise(v):
        length = math.hypot(*v) or 1.0
        return [component / length for component in v]

    def cross(a, b):
        return [
            a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0],
        ]

    def dot(a, b):
        return sum(a[i] * b[i] for i in range(3))

    forward = normalise(subtract(eye, target))
    up = [0.0, 0.0, 1.0]
    if abs(dot(forward, up)) > 0.999:
        up = [0.0, 1.0, 0.0]
    right = normalise(cross(up, forward))
    true_up = cross(forward, right)
    translation = (-dot(right, eye), -dot(true_up, eye), -dot(forward, eye))
    return right, true_up, forward, translation


def _frame_fraction(point: tuple[float, float, float], camera) -> float | None:
    """Where a world point lands vertically in frame: 0 at the bottom, 1 at the top."""
    _right, up, forward, translation = _look_at(camera.position, camera.target)
    view_y = sum(up[i] * point[i] for i in range(3)) + translation[1]
    view_z = sum(forward[i] * point[i] for i in range(3)) + translation[2]
    w = -view_z
    if w <= 0:
        return None
    focal = 1.0 / math.tan(math.radians(camera.vfov) / 2.0)
    return (focal * view_y / w + 1.0) / 2.0


def test_cam_1_projection_matches_the_camera_plan() -> None:
    """The verification the placeholder renderer exists to provide.

    `CAMERA_PLAN.md` §3 claims specific frame positions for the hero front composition.
    Projecting the blockout points through the renderer's own matrix checks that claim
    without a GPU — and tells you *which* side is wrong if it fails.
    """
    scene = build_scene()
    camera = next(c for c in scene.cameras if c.camera_id == "CAM_1")
    geometry = default_blockout()

    claims = (
        ("monitor top bezel", (3200.0, 2350.0, geometry.monitor_top_bezel_z), 0.30),
        ("shoulders", (3200.0, 3000.0, 1080.0), 0.21),
        ("chin", (3200.0, 3000.0, 1215.0), 0.37),
        ("eye line", (3200.0, 3000.0, 1295.0), 0.46),
        ("crown", (3200.0, 3000.0, 1455.0), 0.65),
    )
    for label, point, claimed in claims:
        projected = _frame_fraction(point, camera)
        assert projected is not None, f"{label} is behind the camera"
        assert projected == pytest.approx(claimed, abs=0.03), (
            f"{label}: plan says {claimed * 100:.0f} %, projection gives "
            f"{projected * 100:.1f} %"
        )


def test_the_character_is_in_frame_from_every_face_camera() -> None:
    """A camera transform that frames an empty wall is a transform bug."""
    scene = build_scene()
    geometry = default_blockout()
    eye_point = geometry.eye
    for camera in scene.cameras:
        if camera.camera_id == "CAM_6":
            continue  # desk-surface shot; the face is out of frame by design
        fraction = _frame_fraction(eye_point, camera)
        assert fraction is not None, f"{camera.camera_id}: the eye is behind the camera"
        assert 0.0 < fraction < 1.0, (
            f"{camera.camera_id}: the eye line lands at {fraction * 100:.0f} % of frame"
        )


def test_cam_6_frames_the_desk_surface() -> None:
    scene = build_scene()
    camera = next(c for c in scene.cameras if c.camera_id == "CAM_6")
    keyboard = (3200.0, 2740.0, 757.0)
    fraction = _frame_fraction(keyboard, camera)
    assert fraction is not None
    assert 0.0 < fraction < 1.0


def test_the_vertical_fov_follows_from_the_horizontal() -> None:
    scene = build_scene()
    for camera in scene.cameras:
        expected = 2 * math.degrees(
            math.atan(math.tan(math.radians(camera.hfov / 2)) * 9 / 16)
        )
        assert camera.vfov == pytest.approx(expected, abs=0.05), camera.camera_id


# ============================================================ the protocol


def test_every_command_carries_the_protocol_version() -> None:
    policy = RhythmPolicyV1(bpm=120.0, nod_probability=0.4)
    commands = [
        *resync_commands(
            sequence=1,
            character_state=__import__(
                "tradefix_radio.visual.contracts", fromlist=["CharacterState"]
            ).CharacterState.IDLE_FOCUS,
            gaze=GazeTarget.MONITOR_MAIN,
            camera_id="CAM_7",
            running=(),
            fatigue_phase=0.2,
            policy=policy,
        ),
        rhythm_command(policy, sequence=2),
        screen_command(_state(), sequence=3),
        camera_command("CAM_1", sequence=4, transition="cut", parallax=0.008),
    ]
    for command in commands:
        assert command["v"] == PROTOCOL_VERSION, command["kind"]
        assert "kind" in command


def test_the_screen_command_carries_the_honesty_flags() -> None:
    """The renderer must not have to infer them."""
    live = screen_command(_state(), sequence=1)["honesty"]
    assert live["charts_advance"] is True
    assert live["stale_marker"] is None
    assert live["price_displayable"] is False

    stale = screen_command(_state(trust=FeedTrust.STALE), sequence=2)["honesty"]
    assert stale["charts_advance"] is False
    assert stale["stale_marker"] == "NO FEED"


def test_the_rhythm_command_restates_its_ceilings() -> None:
    """Restated on every update so a dropped message cannot raise a limit."""
    command = rhythm_command(RhythmPolicyV1(bpm=174.0, nod_probability=0.9), sequence=1)
    assert command["max_nod_degrees"] <= 1.1
    assert command["max_consecutive_beats"] <= 16
    assert command["mandatory_gap_seconds"] >= 1.0


def test_commands_encode_compactly() -> None:
    payload = encode([camera_command("CAM_1", sequence=1, transition="cut", parallax=0.01)])
    assert ", " not in payload  # compact separators
    assert json.loads(payload)[0]["camera_id"] == "CAM_1"


def test_resync_carries_the_render_target() -> None:
    from tradefix_radio.visual.contracts import CharacterState

    commands = resync_commands(
        sequence=1,
        character_state=CharacterState.ANALYZING,
        gaze=GazeTarget.MONITOR_MAIN,
        camera_id="CAM_3",
        running=("chart_inspect",),
        fatigue_phase=0.5,
        policy=RhythmPolicyV1(bpm=None),
    )
    target = commands[0]["target"]
    assert target == {"width": TARGET_WIDTH, "height": TARGET_HEIGHT, "fps": TARGET_FPS}


# ============================================================ quality


def _telemetry(**overrides) -> RendererTelemetryV1:
    payload = {
        "at": datetime(2026, 10, 3, 22, 0, tzinfo=UTC),
        "frames_rendered": 1800,
        "fps_mean": 30.0,
        "fps_p05": 29.5,
        "frame_time_p95_ms": 34.0,
        "dropped_frames": 0,
        "gl_memory_mb": 48.0,
        "quality_profile": "balanced",
        "last_command_sequence": 100,
    }
    payload.update(overrides)
    return RendererTelemetryV1.model_validate(payload)


def test_quality_profiles_shed_in_order() -> None:
    assert set(QUALITY_PROFILES) == {"low", "balanced", "high"}
    assert QUALITY_PROFILES["low"]["resident_camera_stacks"] < (
        QUALITY_PROFILES["high"]["resident_camera_stacks"]
    )


def test_a_healthy_renderer_keeps_its_profile() -> None:
    controller = QualityController()
    assert controller.observe(_telemetry(), 10.0) is None
    assert controller.profile == "balanced"


def test_gl_memory_over_the_ceiling_sheds_immediately() -> None:
    """Music generation outranks the picture. ADR-10 §4."""
    controller = QualityController()
    shed = controller.observe(_telemetry(gl_memory_mb=999.0), 10.0)
    assert shed == "low"


def test_sustained_low_fps_sheds_after_the_window() -> None:
    controller = QualityController()
    assert controller.observe(_telemetry(fps_p05=10.0), 0.0) is None
    assert controller.observe(_telemetry(fps_p05=10.0), 10.0) is None
    assert controller.observe(_telemetry(fps_p05=10.0), 40.0) == "low"


def test_a_recovered_renderer_stops_shedding() -> None:
    controller = QualityController()
    controller.observe(_telemetry(fps_p05=10.0), 0.0)
    controller.observe(_telemetry(fps_p05=29.9), 10.0)
    assert controller.observe(_telemetry(fps_p05=10.0), 20.0) is None


def test_renderer_liveness_times_out() -> None:
    """The director keeps running regardless — that is ADR-11's whole point."""
    controller = QualityController()
    controller.observe(_telemetry(), 100.0)
    assert controller.renderer_alive(100.0 + TELEMETRY_TIMEOUT_SECONDS - 1)
    assert not controller.renderer_alive(100.0 + TELEMETRY_TIMEOUT_SECONDS + 1)


def test_command_lag_triggers_a_resync() -> None:
    controller = QualityController()
    assert controller.needs_resync(_telemetry(last_command_sequence=10), 10 + RESYNC_LAG_THRESHOLD + 1)
    assert not controller.needs_resync(_telemetry(last_command_sequence=10), 12)


def test_telemetry_decodes_from_json() -> None:
    telemetry = decode_telemetry(json.dumps(_telemetry().to_json_dict()))
    assert telemetry.fps_p05 == 29.5


# ============================================================ the runtime files


def test_the_runtime_files_exist() -> None:
    assert (RUNTIME_DIR / "index.html").is_file()
    assert (RUNTIME_DIR / "app.js").is_file()


def test_the_runtime_javascript_parses() -> None:
    """Checked with node, because a syntax error would be a blank browser source."""
    node = subprocess.run(
        ["node", "--check", str(RUNTIME_DIR / "app.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    if node.returncode == 127 or "not recognized" in (node.stderr or ""):
        pytest.skip("node is unavailable")
    assert node.returncode == 0, node.stderr


def test_the_placeholder_is_labelled_unmissably() -> None:
    """This page must never be mistaken for the product.

    The wording moved when proof art landed — `PLACEHOLDER` became `TEMP_PROOF`, because
    the page now shows a recognisable trader in a recognisable office and "placeholder"
    understated what it was claiming not to be. The guarantee is unchanged and asserted
    on both the markup and the mode-dependent strings `app.js` sets: whatever is on
    screen, the page says what it is.
    """
    html = (RUNTIME_DIR / "index.html").read_text(encoding="utf-8")
    assert "TEMP_PROOF" in html
    assert "NOT APPROVED ARTWORK" in html
    assert 'id="stamp"' in html
    assert 'id="banner"' in html

    source = (RUNTIME_DIR / "app.js").read_text(encoding="utf-8")
    # Every art mode must carry its own honest badge; none may be unlabelled.
    for mode, claim in (
        ("proof", "NOT APPROVED ARTWORK"),
        ("blockout", "NOT ARTWORK"),
        ("final", "procedural fallback"),
    ):
        assert f"{mode}: [" in source, mode
        assert claim in source, f"{mode} has no honest badge"


def test_the_runtime_targets_1920x1080_and_caps_frames() -> None:
    source = (RUNTIME_DIR / "app.js").read_text(encoding="utf-8")
    assert "TARGET_W = 1920" in source
    assert "TARGET_H = 1080" in source
    assert "requestAnimationFrame" in source
    # 30 fps default, overridable for the benchmark.
    assert "Number(params.get('fps')) || 30" in source


def test_the_runtime_uses_easing_not_linear_motion() -> None:
    """Linear motion is the robotic tell; instant gaze is the same defect."""
    source = (RUNTIME_DIR / "app.js").read_text(encoding="utf-8")
    for curve in ("inOut", "attention", "settle", "drift"):
        assert f"{curve}:" in source, curve
    assert "Math.max(180, command.transit_ms)" in source, "the gaze floor is not enforced"


def test_the_runtime_asks_for_a_low_power_context() -> None:
    """ACE-Step has priority on this GPU."""
    source = (RUNTIME_DIR / "app.js").read_text(encoding="utf-8")
    assert "'low-power'" in source


# ============================================================ the service


def test_the_service_serves_the_runtime_and_the_scene() -> None:
    app = create_app(VisualRuntime(scenario="range", seed=3))
    with TestClient(app) as client:
        assert client.get("/api/visual/health").json()["protocol"] == PROTOCOL_VERSION
        scene = client.get("/api/visual/scene").json()
        assert scene["blockout"] == "TRADE_FIX_OFFICE_01"
        assert client.get("/").status_code == 200
        assert client.get("/app.js").status_code == 200


def test_the_socket_opens_and_resyncs() -> None:
    """The failure this guards: `from __future__ import annotations` plus a
    function-local `WebSocket` import left FastAPI unable to resolve the handler's
    annotation, and the route closed every connection with a 403 and no traceback."""
    app = create_app(VisualRuntime(scenario="range", seed=3))
    with TestClient(app) as client, client.websocket_connect("/ws") as socket:
        commands = json.loads(socket.receive_text())
        kinds = [command["kind"] for command in commands]
        assert "RESYNC" in kinds


def test_the_control_endpoints_respect_the_rules() -> None:
    app = create_app(VisualRuntime(scenario="range", seed=3))
    with TestClient(app) as client:
        assert client.post("/api/visual/camera/CAM_9").status_code == 404
        assert client.post("/api/visual/camera/CAM_3").json()["queued_behind_vetoes"]
        assert client.post("/api/visual/camera/auto/false").json()["auto"] is False
        assert client.post("/api/visual/force_idle/true").json()["force_idle"] is True
        assert client.post("/api/visual/trigger/not_an_action").status_code == 404


def test_the_snapshot_exposes_what_the_control_page_reads() -> None:
    app = create_app(VisualRuntime(scenario="breakout", seed=3))
    with TestClient(app) as client:
        state = client.get("/api/visual/state").json()
    for key in ("character", "rhythm", "drive", "camera", "bridge", "quality", "target"):
        assert key in state, key
    assert state["mode"] == "simulated"
    assert state["camera"]["live"] in CAMERA_METADATA


def test_the_service_never_exposes_a_station_mutation() -> None:
    """The control surface must not be able to reach the radio."""
    app = create_app(VisualRuntime(scenario="range", seed=3))
    paths = [getattr(route, "path", "") for route in app.routes]
    for path in paths:
        assert "station" not in path.lower() or path.startswith("/api/visual/")
    source = (REPO / "tradefix_radio" / "visual" / "service.py").read_text(encoding="utf-8")
    for forbidden in ("tradefix_radio.persistence", "tradefix_radio.radio",
                      "tradefix_radio.generation", "tradefix_radio.core.events"):
        assert forbidden not in source, forbidden


def test_the_benchmark_is_honest_about_having_no_samples() -> None:
    runtime = VisualRuntime(scenario="range", seed=3)
    assert runtime.benchmark()["samples"] == 0


def test_the_benchmark_summarises_what_the_renderer_reported() -> None:
    """Exercised here because the real path needs a GPU this session cannot reach."""
    runtime = VisualRuntime(scenario="range", seed=3)
    for fps in (28.0, 30.0, 31.0, 29.0, 30.0):
        runtime.ingest_telemetry(json.dumps(_telemetry(fps_mean=fps).to_json_dict()))

    summary = runtime.benchmark()
    assert summary["samples"] == 5
    assert summary["fps_mean"]["min"] == 28.0  # type: ignore[index]
    assert summary["fps_mean"]["max"] == 31.0  # type: ignore[index]
    assert summary["fps_mean"]["mean"] == pytest.approx(29.6)  # type: ignore[index]
    assert summary["target"]["fps"] == TARGET_FPS  # type: ignore[index]


def test_a_rejected_telemetry_frame_is_not_counted() -> None:
    runtime = VisualRuntime(scenario="range", seed=3)
    runtime.ingest_telemetry("not json at all")
    runtime.ingest_telemetry(json.dumps({"frames_rendered": "nonsense"}))
    assert runtime.benchmark()["samples"] == 0


def test_retained_telemetry_is_bounded() -> None:
    """This process runs for weeks; an unbounded list is a leak with a nice name."""
    runtime = VisualRuntime(scenario="range", seed=3)
    frame = json.dumps(_telemetry().to_json_dict())
    for _ in range(TELEMETRY_HISTORY + 50):
        runtime.ingest_telemetry(frame)
    assert runtime.benchmark()["samples"] == TELEMETRY_HISTORY


def test_the_benchmark_endpoint_is_served() -> None:
    app = create_app(VisualRuntime(scenario="range", seed=3))
    with TestClient(app) as client:
        assert client.get("/api/visual/benchmark").json()["samples"] == 0


def test_the_derived_workload_matches_the_scene() -> None:
    """A workload budget, not a GPU measurement — and it must not drift from the scene."""
    scene = build_scene()
    workload = frame_workload()
    breakdown = workload["breakdown"]
    assert breakdown["boxes"] == len(scene.boxes) * BOX_FACES  # type: ignore[index]
    assert breakdown["panels"] == len(scene.quads)  # type: ignore[index]
    assert breakdown["character_joints"] == len(scene.joints)  # type: ignore[index]
    assert workload["draw_calls_per_second"]["60"] == (  # type: ignore[index]
        workload["draw_calls_per_frame"] * 60  # type: ignore[operator]
    )


def test_the_workload_constants_match_the_shader_loop() -> None:
    """If `app.js`'s draw loop changes shape, the derived budget goes stale silently."""
    source = (RUNTIME_DIR / "app.js").read_text(encoding="utf-8")
    assert "const bars = 26;" in source, f"CHART_BARS is {CHART_BARS} but app.js disagrees"
    assert source.count("{ c: [") == BOX_FACES, "_drawBoxes no longer draws five faces"
    assert source.count("gl.uniform") >= UNIFORMS_PER_QUAD


def test_the_runtime_url_is_not_the_stations_port() -> None:
    assert "8090" in runtime_url()
    assert "8080" not in runtime_url()
