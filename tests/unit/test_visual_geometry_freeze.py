"""The V1 geometry freeze, as a permanent regression suite.

`docs/visual/GEOMETRY_FREEZE.md` declares the V1 blockout frozen. This module is what
makes that declaration enforceable rather than aspirational: it re-derives every
geometric claim the blockout records and fails if any of them drifts.

It began life as `scripts/visual/validate_blockout.py`, a one-off check for milestone
V1.8. Its first run rejected three interaction anchors as beyond seated reach, two gaze
targets as physically behind the character, a monitor height that would have hidden his
face from every front camera, and an arithmetic error in the default gaze pitch. That
record is the argument for keeping it: the figures in these files are load-bearing and
are not self-evidently right.

The script remains for interactive use and prints a readable table. This module is the
version that runs in CI, and the two must not disagree — `test_script_and_suite_agree`
asserts they read the same file.
"""

from __future__ import annotations

import json
import math
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

BLOCKOUT_PATH = Path(__file__).resolve().parents[2] / "visual" / "environment" / (
    "TRADE_FIX_OFFICE_01.blockout.json"
)

#: Limb lengths from CHARACTER_BIBLE §7. Seated reach is their sum.
UPPER_ARM_MM = 365
FOREARM_MM = 270
HAND_MM = 195


@pytest.fixture(scope="module")
def blockout() -> dict[str, Any]:
    return json.loads(BLOCKOUT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def eye(blockout: dict[str, Any]) -> tuple[float, float, float]:
    seat = blockout["character"]["seated_position"]
    return float(seat["x"]), float(seat["y"]), float(blockout["character"]["key_heights"]["eye"])


@pytest.fixture(scope="module")
def shoulder(blockout: dict[str, Any]) -> tuple[float, float, float]:
    seat = blockout["character"]["seated_position"]
    heights = blockout["character"]["key_heights"]
    return float(seat["x"]), float(seat["y"]), float(heights["shoulder"])


# --------------------------------------------------------------- monitor geometry


def test_monitor_bezel_heights_follow_from_panel_size(blockout: dict[str, Any]) -> None:
    """The recorded bezel heights must be the panel geometry, not a separate claim."""
    centre = blockout["monitors"]["common"]["centre_z"]
    height = blockout["monitors"]["panels"][0]["panel"]["h"]
    derived = blockout["monitors"]["derived"]
    assert derived["top_bezel_z"] == centre + height / 2
    assert derived["bottom_bezel_z"] == centre - height / 2


def test_top_bezel_sits_at_the_eye_line(blockout: dict[str, Any]) -> None:
    """Top of screen level with the eye: correct ergonomics, and the camera constraint.

    This is the relationship that forces every front camera to be elevated. If it ever
    drifts upward the hero compositions stop existing, because a monitor ends up where
    his face should be — which is exactly what the first draft did at a 1250 mm centre.
    """
    eye_z = blockout["character"]["key_heights"]["eye"]
    top = blockout["monitors"]["derived"]["top_bezel_z"]
    assert abs(eye_z - top) <= 10, f"eye {eye_z} vs top bezel {top}: must be level within 10 mm"


def test_desk_monitor_panels_do_not_overlap(blockout: dict[str, Any]) -> None:
    """Yaw-foreshortened x extents must be disjoint, and fit on the desk."""
    spans = []
    for panel in blockout["monitors"]["panels"]:
        if panel.get("mounted"):
            continue
        width = panel["panel"]["w"] * math.cos(math.radians(abs(panel.get("yaw_degrees", 0))))
        centre = panel["centre"]["x"]
        spans.append((centre - width / 2, centre + width / 2, panel["id"]))
    spans.sort()
    for (_, left_end, left_id), (right_start, _, right_id) in pairwise(spans):
        assert left_end <= right_start, f"{left_id} overlaps {right_id}"

    low, high = spans[0][0], max(end for _, end, _ in spans)
    recorded = blockout["monitors"]["array_span_x"]
    assert recorded == [pytest.approx(low, abs=1), pytest.approx(high, abs=1)]
    desk = blockout["desk"]["extent"]["x"]
    assert desk[0] <= low and high <= desk[1], "monitor array overflows the desk"


# --------------------------------------------------------------- camera geometry


def _bezel_clearance(
    camera: dict[str, Any], eye: tuple[float, float, float], plane: dict[str, Any]
) -> float | None:
    """Height of the camera-to-eye sight line above the bezel plane, or None if N/A."""
    position = camera["position"]
    y0, z0 = float(position["y"]), float(position["z"])
    y1, z1 = eye[1], eye[2]
    plane_y, plane_z = float(plane["y"]), float(plane["z"])
    if not min(y0, y1) < plane_y < max(y0, y1):
        return None
    ratio = (plane_y - y0) / (y1 - y0)
    return z0 + ratio * (z1 - z0) - plane_z


def test_face_framing_cameras_clear_the_bezel(
    blockout: dict[str, Any], eye: tuple[float, float, float]
) -> None:
    """A front camera whose sight line to the eye is blocked is not a shot.

    `CAM_6` is exempt by rule: it frames the desk surface, and its target is below the
    bezel plane deliberately.
    """
    plane = blockout["validation"]["bezel_plane"]
    recorded = blockout["validation"]["computed_clearances_mm"]
    checked = 0
    for camera in blockout["cameras"]:
        camera_id = camera["id"]
        if camera["position"]["y"] >= blockout["desk"]["extent"]["y"][0]:
            continue  # north or east of the array; nothing in front of the face
        if camera_id == "CAM_6":
            assert recorded[camera_id] == "exempt - desk surface shot"
            continue
        clearance = _bezel_clearance(camera, eye, plane)
        assert clearance is not None, f"{camera_id}: bezel plane not between camera and eye"
        assert clearance > 0, f"{camera_id} sight line is blocked by a monitor ({clearance:+.1f})"
        assert recorded[camera_id] == pytest.approx(clearance, abs=1.0), (
            f"{camera_id} records {recorded[camera_id]} but geometry gives {clearance:.1f}"
        )
        checked += 1
    assert checked >= 3, "expected at least CAM_1, CAM_4 and CAM_7 to be checked"


def test_cameras_lie_inside_the_room(blockout: dict[str, Any]) -> None:
    size = blockout["room"]["size"]
    for camera in blockout["cameras"]:
        position = camera["position"]
        for axis in ("x", "y", "z"):
            assert 0 < position[axis] < size[axis], f"{camera['id']} outside the room on {axis}"


def test_camera_frame_widths_match_their_lenses(blockout: dict[str, Any]) -> None:
    """Recorded frame width must be the lens and distance, not an independent number.

    35 mm-equivalent focal length on a 36 mm-wide frame: half-width / focal = tan(hFOV/2).
    """
    for camera in blockout["cameras"]:
        frame = camera.get("frame_at_subject")
        if not frame or "w" not in frame:
            continue
        expected_hfov = 2 * math.degrees(math.atan(18.0 / camera["focal_mm_35eq"]))
        assert camera["hfov_degrees"] == pytest.approx(expected_hfov, abs=0.3), camera["id"]
        width = 2 * camera["distance_mm"] * math.tan(math.radians(camera["hfov_degrees"] / 2))
        assert frame["w"] == pytest.approx(width, rel=0.02), (
            f"{camera['id']} records width {frame['w']} but lens and distance give {width:.0f}"
        )
        assert frame["h"] == pytest.approx(frame["w"] * 9 / 16, rel=0.02), camera["id"]


def test_camera_distance_matches_position_and_target(blockout: dict[str, Any]) -> None:
    for camera in blockout["cameras"]:
        position, target = camera["position"], camera["target"]
        distance = math.dist(
            (position["x"], position["y"], position["z"]),
            (target["x"], target["y"], target["z"]),
        )
        assert camera["distance_mm"] == pytest.approx(distance, abs=2.0), camera["id"]


def test_cam_6_frames_every_interactive_prop(blockout: dict[str, Any]) -> None:
    """The hands shot exists to prove contact, so it must contain the things touched.

    It did not, in the first draft: the anchors moved to satisfy seated reach and the
    frame was left behind, cutting off both the notebook and the mug.
    """
    camera = next(c for c in blockout["cameras"] if c["id"] == "CAM_6")
    low, high = camera["frame_at_subject"]["x_range"]
    anchored = {anchor["id"] for anchor in blockout["anchors"] if anchor.get("hand")}
    for obj in blockout["desk_objects"]:
        if not obj.get("reachable", True):
            continue
        if f"ANCHOR_{obj['id']}" not in anchored and obj["id"] not in {
            "NOTEBOOK", "PEN", "MOUSE", "KEYBOARD", "MUG",
        }:
            continue
        x = obj["position"]["x"]
        assert low <= x <= high, f"CAM_6 frame {low}-{high} excludes {obj['id']} at x{x}"


# --------------------------------------------------------------- reach and anchors


def test_every_anchor_is_within_seated_reach(
    blockout: dict[str, Any], shoulder: tuple[float, float, float]
) -> None:
    """The constraint that decides the desk layout.

    A seated man reaches about 830 mm. The desk is 3600 mm wide, so its outer thirds
    cannot be touched from the chair — which is why the notebook, pen, mouse and stream
    pad sit where they do rather than where composition would have put them.
    """
    limit = blockout["anchor_rules"]["seated_reach_mm"]
    assert limit == UPPER_ARM_MM + FOREARM_MM + HAND_MM
    for anchor in blockout["anchors"]:
        position = anchor["position"]
        distance = math.dist(shoulder, (position["x"], position["y"], position["z"]))
        assert distance <= limit, f"{anchor['id']} at {distance:.0f} mm exceeds {limit} mm"


def test_unreachable_objects_carry_no_anchor(blockout: dict[str, Any]) -> None:
    """Decor must be declared decor, so a motion can never target it."""
    anchor_ids = {anchor["id"] for anchor in blockout["anchors"]}
    for obj in blockout["desk_objects"]:
        if obj.get("reachable", True):
            continue
        assert f"ANCHOR_{obj['id']}" not in anchor_ids, (
            f"{obj['id']} is marked unreachable but has an anchor"
        )


def test_reserved_hand_envelope_holds_only_what_it_permits(blockout: dict[str, Any]) -> None:
    permitted = {"KEYBOARD", "MOUSE", "MUG"}
    envelope = blockout["reserved_volumes"][0]["extent"]
    for obj in blockout["desk_objects"]:
        position = obj["position"]
        inside = (
            envelope["x"][0] <= position["x"] <= envelope["x"][1]
            and envelope["y"][0] <= position["y"] <= envelope["y"][1]
        )
        if inside:
            assert obj["id"] in permitted, f"{obj['id']} intrudes into the hand envelope"


# --------------------------------------------------------------- gaze


def _gaze_angles(
    target: dict[str, Any], eye: tuple[float, float, float]
) -> tuple[float, float]:
    position = target["position"]
    dx = position["x"] - eye[0]
    dy = position["y"] - eye[1]
    dz = position["z"] - eye[2]
    yaw = math.degrees(math.atan2(dx, -dy))  # neutral facing is -Y
    pitch = math.degrees(math.atan2(dz, math.hypot(dx, dy)))
    return yaw, pitch


def test_every_gaze_target_declares_sufficient_participation(
    blockout: dict[str, Any], eye: tuple[float, float, float]
) -> None:
    """Check the declaration against the geometry, not the angle against a flat limit.

    A large angle is not a fault; an angle reached with the wrong body parts is. The
    first draft used flat limits and rejected three targets a real trader plainly looks
    at — his notebook, his keyboard, and out of the window.
    """
    tiers = {
        tier["id"]: tier
        for tier in blockout["gaze_rules"]["participation_model"]["tiers"]
    }
    for target in blockout["gaze_targets"]:
        if not isinstance(target["position"], dict):
            continue  # GAZE_CAMERA resolves at runtime
        declared = target["participation"]
        assert declared in tiers, f"{target['id']} declares unknown tier {declared!r}"
        tier = tiers[declared]
        yaw, pitch = _gaze_angles(target, eye)
        assert abs(yaw) <= tier["max_abs_yaw"], (
            f"{target['id']} at yaw {yaw:+.1f} exceeds tier {declared!r} "
            f"({tier['max_abs_yaw']})"
        )
        assert abs(pitch) <= tier["max_abs_pitch"], (
            f"{target['id']} at pitch {pitch:+.1f} exceeds tier {declared!r} "
            f"({tier['max_abs_pitch']})"
        )


def test_gaze_shares_sum_to_one_and_favour_screens(blockout: dict[str, Any]) -> None:
    """89 % on screens is what makes the remaining 11 % read correctly."""
    targets = blockout["gaze_targets"]
    total = math.fsum(target["base_share"] for target in targets)
    screens = math.fsum(t["base_share"] for t in targets if t.get("screen"))
    assert total == pytest.approx(1.0, abs=0.001)
    assert screens == pytest.approx(blockout["gaze_rules"]["screen_share_target"], abs=0.001)


def test_camera_gaze_is_rationed(blockout: dict[str, Any]) -> None:
    """A rare glance acknowledges the viewer. A frequent one makes him a presenter."""
    camera_gaze = next(t for t in blockout["gaze_targets"] if t["id"] == "GAZE_CAMERA")
    assert camera_gaze["base_share"] <= 0.01


def test_default_gaze_pitch_is_the_real_angle(
    blockout: dict[str, Any], eye: tuple[float, float, float]
) -> None:
    """Recorded as -6.8 in the first draft. The geometry gives -14.2."""
    main = next(t for t in blockout["gaze_targets"] if t["id"] == "GAZE_MON_1")
    _, pitch = _gaze_angles(main, eye)
    assert blockout["gaze_rules"]["default_pitch_degrees"] == pytest.approx(pitch, abs=0.3)


def test_gaze_targets_resolve_to_real_geometry(blockout: dict[str, Any]) -> None:
    """A gaze target must sit on the thing it names, or the eyes look past it."""
    panels = {panel["id"]: panel["centre"] for panel in blockout["monitors"]["panels"]}
    for target in blockout["gaze_targets"]:
        suffix = target["id"].removeprefix("GAZE_")
        if suffix not in panels:
            continue
        centre = panels[suffix]
        for axis in ("x", "y", "z"):
            assert target["position"][axis] == centre[axis], (
                f"{target['id']} does not sit on {suffix}"
            )


# --------------------------------------------------------------- the two scripts agree


def test_script_and_suite_agree(blockout: dict[str, Any]) -> None:
    """The interactive script must read the same file this suite does."""
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "visual" / "validate_blockout.py"
    ).read_text(encoding="utf-8")
    assert "TRADE_FIX_OFFICE_01.blockout.json" in script
    assert blockout["id"] == "TRADE_FIX_OFFICE_01"
    assert blockout["authority"].startswith("AUTHORITATIVE")
