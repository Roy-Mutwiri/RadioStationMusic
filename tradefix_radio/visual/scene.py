"""A renderer-ready scene, derived from the frozen blockout.

The placeholder renderer's job is **verification, not beauty**: camera transforms, layer
ordering, parallax, the state bridge, action events, the browser runtime, OBS
compatibility. So the scene it draws is the blockout itself — real room geometry in
millimetres, projected by a real camera matrix.

That choice is what makes the placeholder worth building before any art exists. A
renderer fed hand-placed 2D sprites would verify that WebGL works. A renderer fed the
blockout verifies that **the seven camera transforms produce the compositions
`CAMERA_PLAN.md` claims they do** — and if `CAM_1`'s bezel line does not land at 30 % of
frame height on screen, the arithmetic in the freeze suite is wrong about something.

Character layer groups carry a **parent**, so the renderer can test transform parenting
(torso → upper arm → forearm → hand) against the joint hierarchy the final rig will use.
Animating those as independent screen-space sprites would verify nothing about the rig.

Everything here is geometry and depth. No colours beyond a palette index, no art, no
pixels — the asset manifest owns what the painted layers must provide, and this owns
where they go.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Final

from tradefix_radio.visual.assets import parallax_from_depth
from tradefix_radio.visual.camera import CAMERA_METADATA
from tradefix_radio.visual.geometry import (
    GEOMETRY_CONTRACT,
    Blockout,
    Point,
    default_blockout,
)
from tradefix_radio.visual.geometry import (
    vec as _vec,
)

#: Palette keys the runtime resolves to the Trade Fix tokens. Named rather than
#: hex-coded so the placeholder cannot drift from `tailwind.config.js`.
PALETTE: Final[dict[str, str]] = {
    "ink_950": "#07080a",
    "ink_900": "#0c0e12",
    "ink_800": "#151922",
    "ink_700": "#232935",
    "ink_600": "#2f3744",
    "ink_500": "#444d5d",
    "ink_400": "#6b7383",
    "ink_300": "#9aa1ad",
    "ink_100": "#e6e8ec",
    "gold_500": "#c9a227",
    "gold_400": "#d9b949",
    "gold_300": "#e7cd74",
    "blue": "#4a7fc2",
    "green": "#4f9d69",
    "red": "#c2504a",
}


@dataclass(frozen=True, slots=True)
class SceneBox:
    """An axis-aligned box in room coordinates. Drawn as a wireframe or filled."""

    box_id: str
    minimum: Point
    maximum: Point
    colour: str
    #: Compositing order within its depth band. Lower draws first.
    order: int
    filled: bool = False
    opacity: float = 1.0
    label: str | None = None

    @property
    def centre(self) -> Point:
        return tuple(  # type: ignore[return-value]
            (a + b) / 2.0 for a, b in zip(self.minimum, self.maximum, strict=True)
        )


@dataclass(frozen=True, slots=True)
class SceneQuad:
    """A flat rectangle in room space, given by its centre, size and orientation.

    Monitors, wall panels and the window are quads rather than boxes because they are
    surfaces the renderer draws content onto.
    """

    quad_id: str
    centre: Point
    width: float
    height: float
    #: Rotation about Z, degrees. 0 faces north (+Y).
    yaw: float
    #: Rotation about X, degrees. Negative tilts the top away.
    tilt: float
    colour: str
    order: int
    role: str
    #: Set for live screen surfaces the runtime draws generated chart content onto.
    live: bool = False


@dataclass(frozen=True, slots=True)
class SceneJoint:
    """One node of the placeholder character's transform hierarchy.

    `parent` is the point of this: the renderer composes world transforms down the chain,
    which is what verifies transform parenting before any rig exists.
    """

    joint_id: str
    parent: str | None
    #: Rest position in room coordinates.
    position: Point
    #: Placeholder shape: `ellipse` or `rect`.
    shape: str
    #: Shape size in millimetres, in the joint's local frame.
    size: tuple[float, float]
    colour: str
    order: int
    #: Degrees of freedom the runtime may animate, for the debug overlay.
    dof: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SceneCamera:
    """A frozen V1 camera, with everything the renderer needs to build a matrix."""

    camera_id: str
    name: str
    position: Point
    target: Point
    #: Vertical field of view, degrees — what a perspective matrix wants.
    vfov: float
    hfov: float
    focal_mm_35eq: float
    parallax_amplitude: float
    allows_push_in: bool


@dataclass
class SceneDescription:
    """Everything the placeholder renderer draws, in room coordinates."""

    blockout_id: str
    room: Point
    boxes: list[SceneBox] = field(default_factory=list)
    quads: list[SceneQuad] = field(default_factory=list)
    joints: list[SceneJoint] = field(default_factory=list)
    cameras: list[SceneCamera] = field(default_factory=list)
    anchors: dict[str, Point] = field(default_factory=dict)
    gaze_targets: dict[str, Point] = field(default_factory=dict)
    palette: dict[str, str] = field(default_factory=lambda: dict(PALETTE))

    def to_json(self) -> dict[str, Any]:
        return {
            "blockout": self.blockout_id,
            "units": "millimetres",
            "note": (
                "PLACEHOLDER GEOMETRY. Derived from the frozen V1 blockout so the camera "
                "transforms are genuinely verified. Replaced wholesale by painted layer "
                "stacks; nothing in the runtime depends on these shapes."
            ),
            "geometry_contract": GEOMETRY_CONTRACT,
            "room": _vec(self.room),
            "palette": self.palette,
            "boxes": [
                {
                    "id": box.box_id,
                    "min": _vec(box.minimum),
                    "max": _vec(box.maximum),
                    "colour": box.colour,
                    "order": box.order,
                    "filled": box.filled,
                    "opacity": box.opacity,
                    "label": box.label,
                }
                for box in sorted(self.boxes, key=lambda b: b.order)
            ],
            "quads": [
                {
                    "id": quad.quad_id,
                    "centre": _vec(quad.centre),
                    "width": quad.width,
                    "height": quad.height,
                    "yaw": quad.yaw,
                    "tilt": quad.tilt,
                    "colour": quad.colour,
                    "order": quad.order,
                    "role": quad.role,
                    "live": quad.live,
                }
                for quad in sorted(self.quads, key=lambda q: q.order)
            ],
            "joints": [
                {
                    "id": joint.joint_id,
                    "parent": joint.parent,
                    "position": _vec(joint.position),
                    "shape": joint.shape,
                    "size": _vec(joint.size),
                    "colour": joint.colour,
                    "order": joint.order,
                    "dof": list(joint.dof),
                }
                for joint in sorted(self.joints, key=lambda j: j.order)
            ],
            "cameras": [
                {
                    "id": camera.camera_id,
                    "name": camera.name,
                    "position": _vec(camera.position),
                    "target": _vec(camera.target),
                    "vfov": round(camera.vfov, 4),
                    "hfov": round(camera.hfov, 4),
                    "focal_mm_35eq": camera.focal_mm_35eq,
                    "parallax_amplitude": camera.parallax_amplitude,
                    "allows_push_in": camera.allows_push_in,
                }
                for camera in self.cameras
            ],
            "anchors": {
                name: _vec(point)
                for name, point in sorted(self.anchors.items())
            },
            "gaze_targets": {
                name: _vec(point)
                for name, point in sorted(self.gaze_targets.items())
            },
            "parallax_reference_mm": 2248.0,
        }


def _vfov_from_hfov(hfov_degrees: float) -> float:
    """Vertical FOV for a 16:9 frame. The renderer's matrix wants vertical."""
    half = math.radians(hfov_degrees / 2.0)
    return 2.0 * math.degrees(math.atan(math.tan(half) * 9.0 / 16.0))


def build_scene(blockout: Blockout | None = None) -> SceneDescription:
    """Derive the placeholder scene from the frozen blockout."""
    geometry = blockout or default_blockout()
    room_x, room_y, room_z = geometry.room
    desk_z = geometry.desk_surface_z
    eye_x, eye_y, eye_z = geometry.eye
    shoulder_z = geometry.shoulder[2]

    scene = SceneDescription(blockout_id=geometry.blockout_id, room=geometry.room)

    # -- the room shell. Drawn as a wireframe so the camera framing is legible.
    scene.boxes.append(
        SceneBox("ROOM", (0.0, 0.0, 0.0), (room_x, room_y, room_z), "ink_700", order=0,
                 label="TRADE_FIX_OFFICE_01")
    )
    # -- the window wall: a filled quad at y = room_y, standing in for the city.
    scene.quads.append(
        SceneQuad("WINDOW", (3200.0, room_y, 1550.0), 4800.0, 2300.0, 0.0, 0.0,
                  "blue", order=1, role="window")
    )
    # -- desk
    scene.boxes.append(
        SceneBox("DESK", (1400.0, 2200.0, desk_z - 40.0), (5000.0, 3000.0, desk_z),
                 "ink_800", order=20, filled=True, label="desk")
    )
    # -- chair
    scene.boxes.append(
        SceneBox("CHAIR", (2960.0, 3140.0, 0.0), (3440.0, 3460.0, 1180.0),
                 "ink_800", order=12, label="chair")
    )
    # -- west shelving and east cabinet, for spatial reference in the wide shot
    scene.boxes.append(
        SceneBox("SHELVES", (0.0, 900.0, 0.0), (400.0, 4200.0, 2100.0),
                 "ink_800", order=5, label="shelves")
    )
    scene.boxes.append(
        SceneBox("CABINET", (5700.0, 2600.0, 0.0), (6350.0, 3100.0, 900.0),
                 "ink_800", order=5, label="coffee zone")
    )

    # -- monitors. The live surfaces the runtime draws generated charts onto.
    monitor_specs = (
        ("MON_1", 3200.0, 598.0, 336.0, 0.0, "active market"),
        ("MON_2", 2500.0, 598.0, 336.0, 0.0, "secondary timeframe"),
        ("MON_3", 3900.0, 598.0, 336.0, 0.0, "watchlist"),
        ("MON_4", 4560.0, 531.0, 299.0, 22.0, "station"),
        ("MON_5", 1840.0, 531.0, 299.0, -22.0, "session"),
    )
    for index, (name, x, width, height, yaw, role) in enumerate(monitor_specs):
        scene.quads.append(
            SceneQuad(
                name, (x, 2350.0, geometry.monitor_centre_z), width, height, yaw, -8.0,
                "ink_950", order=30 + index, role=role, live=True,
            )
        )
    for name, y in (("PANEL_W1", 1900.0), ("PANEL_W2", 2600.0)):
        scene.quads.append(
            SceneQuad(name, (60.0, y, 1700.0), 708.0, 398.0, 90.0, 0.0,
                      "ink_950", order=6, role="wall panel", live=True)
        )

    # -- desk props, as small boxes at their anchor positions
    props = (
        ("KEYBOARD", 3200.0, 2740.0, 320.0, 120.0, 20.0),
        ("MOUSE", 2720.0, 2560.0, 70.0, 110.0, 35.0),
        ("MUG", 3760.0, 2620.0, 90.0, 90.0, 110.0),
        ("NOTEBOOK", 2480.0, 2810.0, 148.0, 210.0, 12.0),
        ("STREAM_PAD", 3900.0, 2860.0, 120.0, 80.0, 18.0),
    )
    for index, (name, x, y, width, depth, height) in enumerate(props):
        scene.boxes.append(
            SceneBox(
                name,
                (x - width / 2, y - depth / 2, desk_z),
                (x + width / 2, y + depth / 2, desk_z + height),
                "ink_600", order=40 + index, filled=True, label=name.lower(),
            )
        )

    # -- the placeholder character, as a transform hierarchy.
    #
    # Seven joints, parented. The brief's example chain is torso -> shoulder -> forearm
    # -> hand, and that is exactly what is built, so the renderer's world-transform
    # composition is exercised rather than assumed.
    scene.joints.extend(
        (
            SceneJoint("pelvis", None, (eye_x, eye_y, 500.0), "rect", (380.0, 260.0),
                       "ink_600", order=50, dof=("trans_y", "rot_z")),
            SceneJoint("torso", "pelvis", (eye_x, eye_y, shoulder_z - 120.0), "rect",
                       (400.0, 560.0), "ink_500", order=51,
                       dof=("rot_z", "trans_y")),
            SceneJoint("shoulder_l", "torso", (eye_x + 200.0, eye_y, shoulder_z),
                       "rect", (90.0, 90.0), "ink_500", order=52, dof=("rot_z",)),
            SceneJoint("forearm_l", "shoulder_l", (eye_x + 300.0, eye_y - 180.0, 760.0),
                       "rect", (80.0, 300.0), "ink_500", order=53, dof=("rot_z",)),
            SceneJoint("hand_l", "forearm_l", (3080.0, 2740.0, 790.0), "ellipse",
                       (110.0, 90.0), "ink_400", order=54, dof=("rot_z",)),
            SceneJoint("shoulder_r", "torso", (eye_x - 200.0, eye_y, shoulder_z),
                       "rect", (90.0, 90.0), "ink_500", order=52, dof=("rot_z",)),
            SceneJoint("forearm_r", "shoulder_r", (eye_x - 300.0, eye_y - 180.0, 760.0),
                       "rect", (80.0, 300.0), "ink_500", order=53, dof=("rot_z",)),
            SceneJoint("hand_r", "forearm_r", (2720.0, 2560.0, 790.0), "ellipse",
                       (110.0, 90.0), "ink_400", order=54, dof=("rot_z",)),
            SceneJoint("neck", "torso", (eye_x, eye_y, shoulder_z + 90.0), "rect",
                       (110.0, 150.0), "ink_500", order=55, dof=("rot_z",)),
            SceneJoint("head", "neck", (eye_x, eye_y, eye_z + 80.0), "ellipse",
                       (200.0, 260.0), "ink_400", order=56,
                       dof=("rot_z", "yaw", "trans_x", "trans_y")),
            SceneJoint("eye_l", "head", (eye_x + 42.0, eye_y - 95.0, eye_z), "ellipse",
                       (34.0, 20.0), "ink_100", order=58, dof=("pitch", "yaw")),
            SceneJoint("eye_r", "head", (eye_x - 42.0, eye_y - 95.0, eye_z), "ellipse",
                       (34.0, 20.0), "ink_100", order=58, dof=("pitch", "yaw")),
            SceneJoint("lid_l", "eye_l", (eye_x + 42.0, eye_y - 97.0, eye_z), "rect",
                       (38.0, 22.0), "ink_400", order=59, dof=("close",)),
            SceneJoint("lid_r", "eye_r", (eye_x - 42.0, eye_y - 97.0, eye_z), "rect",
                       (38.0, 22.0), "ink_400", order=59, dof=("close",)),
            SceneJoint("headphones", "head", (eye_x, eye_y, eye_z + 150.0), "rect",
                       (240.0, 60.0), "gold_500", order=57, dof=("rot_z",)),
        )
    )

    for camera_id, transform in sorted(geometry.cameras.items()):
        meta = CAMERA_METADATA[camera_id]
        scene.cameras.append(
            SceneCamera(
                camera_id=camera_id,
                name=transform.name,
                position=transform.position,
                target=transform.target,
                vfov=_vfov_from_hfov(transform.hfov_degrees),
                hfov=transform.hfov_degrees,
                focal_mm_35eq=transform.focal_mm_35eq,
                parallax_amplitude=meta.parallax_amplitude,
                allows_push_in=meta.allows_push_in,
            )
        )

    scene.anchors = {
        anchor_id: anchor.position for anchor_id, anchor in geometry.anchors.items()
    }
    scene.gaze_targets = {
        target.value: resolved.position
        for target, resolved in geometry.gaze.items()
        if resolved.position is not None
    }
    return scene


def layer_depths(camera_id: str, blockout: Blockout | None = None) -> dict[str, float]:
    """Parallax amplitude per scene element for one camera.

    Derived from distance, never eyeballed — `ASSET_MANIFEST.md` §2's rule, applied to
    the placeholder so the runtime's parallax path is exercised with real numbers.
    """
    geometry = blockout or default_blockout()
    camera = geometry.camera(camera_id)
    amplitude = CAMERA_METADATA[camera_id].parallax_amplitude
    scene = build_scene(geometry)

    out: dict[str, float] = {}
    for box in scene.boxes:
        distance = max(1.0, _distance(camera.position, box.centre))
        out[box.box_id] = parallax_from_depth(distance, camera_amplitude=amplitude)
    for quad in scene.quads:
        distance = max(1.0, _distance(camera.position, quad.centre))
        out[quad.quad_id] = parallax_from_depth(distance, camera_amplitude=amplitude)
    return out


def _distance(a: Point, b: Point) -> float:
    return math.dist(a, b)


# --------------------------------------------------------------------- the frame cost

#: `app.js` `_drawQuads` draws this many marks per live surface.
CHART_BARS: Final = 26
#: `app.js` `_drawBoxes` draws five of six faces; the sixth is never camera-facing.
BOX_FACES: Final = 5
#: `app.js` `_drawRoomShell` — floor, back wall, two side walls. Without them the office
#: reads as furniture floating in black rather than as a room with depth.
ROOM_SHELL_QUADS: Final = 4
#: `app.js` `_drawCharacter` draws the limb segments between joints, so an arm follows its
#: own hand. Four arm segments plus spine, neck and head.
LIMB_SEGMENTS: Final = 7
#: centre, size, basisX, basisY, parallax, colour, shape, edge.
UNIFORMS_PER_QUAD: Final = 8
VERTICES_PER_QUAD: Final = 6


def frame_workload() -> dict[str, object]:
    """How much draw work one frame asks for, counted from the scene.

    This is a workload budget, **not** a GPU measurement: it says how many calls the
    renderer issues, not how long the GPU takes to serve them. The distinction matters,
    because the only honest source for GPU time is a browser with a GPU process, and
    `tradefix visual benchmark` is the command that gets it.

    Mirrors `app.js`'s three draw passes. If that file's loop structure changes, this
    count goes stale — `test_visual_renderer.py` pins the constants it depends on.
    """
    scene = build_scene()

    box_calls = len(scene.boxes) * BOX_FACES
    panel_calls = len(scene.quads)
    chart_calls = sum(CHART_BARS for quad in scene.quads if quad.live)
    joint_calls = len(scene.joints)  # lids drop out while open; worst case counted
    calls = (
        ROOM_SHELL_QUADS
        + box_calls
        + panel_calls
        + chart_calls
        + joint_calls
        + LIMB_SEGMENTS
    )

    return {
        "draw_calls_per_frame": calls,
        "breakdown": {
            "room_shell": ROOM_SHELL_QUADS,
            "boxes": box_calls,
            "panels": panel_calls,
            "chart_marks": chart_calls,
            "character_joints": joint_calls,
            "limb_segments": LIMB_SEGMENTS,
        },
        "uniform_updates_per_frame": calls * UNIFORMS_PER_QUAD,
        "vertices_per_frame": calls * VERTICES_PER_QUAD,
        "draw_calls_per_second": {"30": calls * 30, "60": calls * 60},
        "textures": 0,
        "render_targets": 1,
        "shader_programs": 1,
        "note": (
            "Every primitive is one six-vertex screen-space quad shaded by an SDF branch, "
            "so the cost is dominated by uniform upload and per-call overhead rather than "
            f"by fill or texture bandwidth. The character is {joint_calls} of these calls; "
            f"the chart marks are {chart_calls}, which is where a reduction comes from "
            "first."
        ),
    }


__all__ = [
    "BOX_FACES",
    "CHART_BARS",
    "LIMB_SEGMENTS",
    "PALETTE",
    "ROOM_SHELL_QUADS",
    "UNIFORMS_PER_QUAD",
    "VERTICES_PER_QUAD",
    "SceneBox",
    "SceneCamera",
    "SceneDescription",
    "SceneJoint",
    "SceneQuad",
    "build_scene",
    "frame_workload",
    "layer_depths",
]
