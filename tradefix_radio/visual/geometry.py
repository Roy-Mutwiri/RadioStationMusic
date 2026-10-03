"""The frozen V1 blockout, loaded and resolved.

`visual/environment/TRADE_FIX_OFFICE_01.blockout.json` is the single source of spatial
truth (ADR-13). This module is the only code that reads it, and it turns the file into
three things behaviour needs:

* **anchors** — where a hand must arrive, and whether it can reach
* **gaze targets** — the semantic vocabulary resolved to real coordinates and angles
* **cameras** — transforms and metadata for the camera stub

Why resolve angles here rather than in the gaze director
--------------------------------------------------------
Because the alternative is per-camera tuned angles, and those are how eyes end up
looking *through* monitors. A gaze target is a point in the room; the angle to it is
geometry, not a parameter. Resolving it once, from the authoritative file, means "look
at the left monitor" means the same thing in all seven cameras and cannot drift.

The loader validates on load and raises. A blockout that disagrees with itself must
fail loudly at startup rather than produce a character reaching for a mug that is
400 mm beyond his arm — which is what the first version of this geometry actually
specified, and what `tests/unit/test_visual_geometry_freeze.py` now prevents.
"""

from __future__ import annotations

import functools
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from tradefix_radio.core.errors import ConfigurationError
from tradefix_radio.visual.contracts import GazeParticipation, GazeTarget

#: Repository-relative location of the authoritative blockout.
BLOCKOUT_RELATIVE: Final = Path("visual") / "environment" / "TRADE_FIX_OFFICE_01.blockout.json"

#: Maps the semantic gaze vocabulary onto the blockout's physical surface ids.
#:
#: ``MONITOR_LEFT`` is **the character's** left. He faces −Y, so his left is east (+X),
#: which is ``MON_3``. Getting this backwards would have him glance away from whatever
#: the behaviour intended, consistently, in every camera — a bug that looks like bad
#: animation rather than like a mapping error, so it is written down here rather than
#: inferred at each call site.
#:
#: ``MONITOR_LOWER`` is the one entry that is a compromise. The brief's gaze vocabulary
#: includes a lower screen; V1 geometry has no physically lower panel, and adding one
#: would change frozen geometry for a naming convenience. It therefore resolves to
#: ``MON_4`` — the station/system screen, down and to his left — which is the surface
#: the intent is actually reaching for.
#:
#: **Every target must map to a distinct surface.** An earlier draft had both
#: ``MONITOR_LOWER`` and a ``MONITOR_FAR_LEFT`` resolving to ``MON_4``, which silently
#: doubled that screen's gaze share: two enum members each drew their own base share
#: from the same physical panel. The duplicate was removed rather than reconciled, and
#: ``test_visual_gaze.py`` now asserts the mapping is injective.
_GAZE_SURFACE: Final[dict[GazeTarget, str]] = {
    GazeTarget.MONITOR_MAIN: "GAZE_MON_1",
    GazeTarget.MONITOR_LEFT: "GAZE_MON_3",
    GazeTarget.MONITOR_RIGHT: "GAZE_MON_2",
    GazeTarget.MONITOR_LOWER: "GAZE_MON_4",
    GazeTarget.MONITOR_FAR_RIGHT: "GAZE_MON_5",
    GazeTarget.PANEL_UPPER_LEFT: "GAZE_PANEL_W2",
    GazeTarget.PANEL_UPPER_RIGHT: "GAZE_PANEL_W1",
    GazeTarget.NOTEBOOK: "GAZE_NOTEBOOK",
    GazeTarget.MOUSE: "GAZE_MOUSE",
    GazeTarget.KEYBOARD: "GAZE_KEYBOARD",
    GazeTarget.COFFEE: "GAZE_MUG",
    GazeTarget.WINDOW: "GAZE_WINDOW",
    GazeTarget.MIDDLE_DISTANCE: "GAZE_MIDDLE_DISTANCE",
    GazeTarget.CAMERA: "GAZE_CAMERA",
}

Point = tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class Anchor:
    """Where a hand must arrive, and what it costs to get there."""

    anchor_id: str
    position: Point
    hand: str | None
    reach_mm: float
    tolerance_mm: float

    @property
    def is_two_handed(self) -> bool:
        return self.hand == "L+R"


@dataclass(frozen=True, slots=True)
class ResolvedGazeTarget:
    """A semantic gaze target with its geometry already worked out."""

    target: GazeTarget
    surface_id: str
    position: Point | None
    #: Degrees off neutral facing. Positive is toward his left (east).
    yaw_degrees: float
    #: Degrees from horizontal. Negative is down.
    pitch_degrees: float
    participation: GazeParticipation
    base_share: float
    is_screen: bool
    #: ``True`` for ``CAMERA``, whose position depends on the active camera.
    dynamic: bool

    @property
    def total_angle_degrees(self) -> float:
        """Angular distance from neutral. Drives transit time and head contribution."""
        return math.hypot(self.yaw_degrees, self.pitch_degrees)


@dataclass(frozen=True, slots=True)
class CameraTransform:
    """One of the seven frozen V1 compositions."""

    camera_id: str
    name: str
    position: Point
    target: Point
    focal_mm_35eq: float
    hfov_degrees: float
    distance_mm: float
    bezel_clearance_mm: float | None
    hour_share_cap: float | None


@dataclass(frozen=True, slots=True)
class Blockout:
    """The resolved, validated blockout.

    Immutable and cached. Behaviour reads it many times a second and it never changes
    at runtime — that is what "frozen" means operationally.
    """

    blockout_id: str
    room: tuple[float, float, float]
    eye: Point
    shoulder: Point
    seated_reach_mm: float
    contact_tolerance_mm: float
    desk_surface_z: float
    monitor_top_bezel_z: float
    monitor_centre_z: float
    default_gaze_pitch_degrees: float
    min_gaze_transit_ms: int
    forced_blink_above_degrees: float
    screen_share_target: float
    anchors: dict[str, Anchor]
    gaze: dict[GazeTarget, ResolvedGazeTarget]
    cameras: dict[str, CameraTransform]

    # -- queries behaviour actually makes ---------------------------------

    def anchor(self, anchor_id: str) -> Anchor:
        try:
            return self.anchors[anchor_id]
        except KeyError:
            raise ConfigurationError(
                f"unknown interaction anchor {anchor_id!r}; "
                f"known: {', '.join(sorted(self.anchors))}"
            ) from None

    def gaze_target(self, target: GazeTarget) -> ResolvedGazeTarget:
        try:
            return self.gaze[target]
        except KeyError:
            raise ConfigurationError(
                f"gaze target {target.value!r} does not resolve to blockout geometry"
            ) from None

    def camera(self, camera_id: str) -> CameraTransform:
        try:
            return self.cameras[camera_id]
        except KeyError:
            raise ConfigurationError(
                f"unknown camera {camera_id!r}; known: {', '.join(sorted(self.cameras))}"
            ) from None

    def angle_between(self, a: GazeTarget, b: GazeTarget) -> float:
        """Angular distance between two gaze targets, degrees.

        Used for transit timing and the forced blink. Computed in the yaw/pitch plane
        rather than as a true solid angle: the error is under two degrees across the
        whole target set, and the number is consumed as a *duration scale*, where that
        is far below perceptible.
        """
        first, second = self.gaze_target(a), self.gaze_target(b)
        return math.hypot(
            second.yaw_degrees - first.yaw_degrees,
            second.pitch_degrees - first.pitch_degrees,
        )

    def screen_targets(self) -> tuple[GazeTarget, ...]:
        screens = (t for t, g in self.gaze.items() if g.is_screen)
        return tuple(sorted(screens, key=lambda target: target.value))


# ------------------------------------------------------------------ loading


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _angles(position: Point, eye: Point) -> tuple[float, float]:
    """Yaw and pitch from the eye to a point, in the character's frame.

    Neutral facing is −Y. Yaw is positive toward +X (his left); pitch is positive up.
    """
    dx = position[0] - eye[0]
    dy = position[1] - eye[1]
    dz = position[2] - eye[2]
    yaw = math.degrees(math.atan2(dx, -dy))
    pitch = math.degrees(math.atan2(dz, math.hypot(dx, dy)))
    return yaw, pitch


def _point(raw: dict[str, Any]) -> Point:
    return float(raw["x"]), float(raw["y"]), float(raw["z"])


def load_blockout(path: Path | None = None) -> Blockout:
    """Read, resolve and validate the blockout.

    Raises :class:`ConfigurationError` on anything self-inconsistent. Loud failure at
    startup is the point: the alternative is a character silently reaching 400 mm past
    his own arm, which is exactly what the first draft of this file specified.
    """
    resolved = path or (_repo_root() / BLOCKOUT_RELATIVE)
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigurationError(
            f"blockout not found at {resolved}; the visual layer cannot resolve anchors "
            "or gaze targets without it"
        ) from None
    except json.JSONDecodeError as error:
        raise ConfigurationError(f"blockout at {resolved} is not valid JSON: {error}") from error

    character = raw["character"]
    heights = character["key_heights"]
    seat = character["seated_position"]
    eye: Point = (float(seat["x"]), float(seat["y"]), float(heights["eye"]))
    shoulder: Point = (float(seat["x"]), float(seat["y"]), float(heights["shoulder"]))

    rules = raw["anchor_rules"]
    reach = float(rules["seated_reach_mm"])
    tolerance = float(rules["contact_tolerance_mm"])

    anchors: dict[str, Anchor] = {}
    for entry in raw["anchors"]:
        anchor_position = _point(entry["position"])
        distance = math.dist(shoulder, anchor_position)
        if distance > reach:
            raise ConfigurationError(
                f"anchor {entry['id']} is {distance:.0f} mm from the seated shoulder, "
                f"beyond the {reach:.0f} mm reach; a hand cannot arrive there"
            )
        anchors[entry["id"]] = Anchor(
            anchor_id=entry["id"],
            position=anchor_position,
            hand=entry.get("hand"),
            reach_mm=distance,
            tolerance_mm=float(entry.get("tolerance_mm", tolerance)),
        )

    gaze_rules = raw["gaze_rules"]
    tiers = {
        tier["id"]: tier for tier in gaze_rules["participation_model"]["tiers"]
    }
    by_surface = {entry["id"]: entry for entry in raw["gaze_targets"]}

    gaze: dict[GazeTarget, ResolvedGazeTarget] = {}
    for target, surface_id in _GAZE_SURFACE.items():
        entry = by_surface.get(surface_id)
        if entry is None:
            raise ConfigurationError(
                f"gaze target {target.value!r} maps to {surface_id!r}, which the blockout "
                "does not define"
            )
        declared = entry.get("participation")
        if declared not in tiers:
            raise ConfigurationError(
                f"{surface_id} declares participation {declared!r}, which is not a known tier"
            )
        tier = tiers[declared]
        dynamic = not isinstance(entry["position"], dict)
        position: Point | None = None
        if dynamic:
            position, yaw, pitch = None, 0.0, 0.0
        else:
            position = _point(entry["position"])
            yaw, pitch = _angles(position, eye)
            if abs(yaw) > tier["max_abs_yaw"] + 0.5:
                raise ConfigurationError(
                    f"{surface_id} sits at yaw {yaw:+.1f}, beyond its declared "
                    f"{declared!r} tier ({tier['max_abs_yaw']})"
                )
            if abs(pitch) > tier["max_abs_pitch"] + 0.5:
                raise ConfigurationError(
                    f"{surface_id} sits at pitch {pitch:+.1f}, beyond its declared "
                    f"{declared!r} tier ({tier['max_abs_pitch']})"
                )
        gaze[target] = ResolvedGazeTarget(
            target=target,
            surface_id=surface_id,
            position=position,
            yaw_degrees=yaw,
            pitch_degrees=pitch,
            participation=GazeParticipation(declared),
            base_share=float(entry["base_share"]),
            is_screen=bool(entry.get("screen", False)),
            dynamic=dynamic,
        )

    cameras: dict[str, CameraTransform] = {}
    for entry in raw["cameras"]:
        clearance = entry.get("bezel_clearance_mm")
        cameras[entry["id"]] = CameraTransform(
            camera_id=entry["id"],
            name=entry["name"],
            position=_point(entry["position"]),
            target=_point(entry["target"]),
            focal_mm_35eq=float(entry["focal_mm_35eq"]),
            hfov_degrees=float(entry["hfov_degrees"]),
            distance_mm=float(entry["distance_mm"]),
            bezel_clearance_mm=float(clearance) if isinstance(clearance, (int, float)) else None,
            hour_share_cap=(
                float(entry["hour_share_cap"]) if "hour_share_cap" in entry else None
            ),
        )
    if len(cameras) != 7:
        raise ConfigurationError(
            f"expected the seven frozen V1 cameras, found {len(cameras)}: "
            f"{', '.join(sorted(cameras))}"
        )

    room = raw["room"]["size"]
    monitors = raw["monitors"]
    return Blockout(
        blockout_id=raw["id"],
        room=(float(room["x"]), float(room["y"]), float(room["z"])),
        eye=eye,
        shoulder=shoulder,
        seated_reach_mm=reach,
        contact_tolerance_mm=tolerance,
        desk_surface_z=float(raw["desk"]["surface_z"]),
        monitor_top_bezel_z=float(monitors["derived"]["top_bezel_z"]),
        monitor_centre_z=float(monitors["common"]["centre_z"]),
        default_gaze_pitch_degrees=float(gaze_rules["default_pitch_degrees"]),
        min_gaze_transit_ms=int(gaze_rules["min_transit_ms"]),
        forced_blink_above_degrees=float(gaze_rules["forced_blink_above_degrees"]),
        screen_share_target=float(gaze_rules["screen_share_target"]),
        anchors=anchors,
        gaze=gaze,
        cameras=cameras,
    )


@functools.lru_cache(maxsize=1)
def default_blockout() -> Blockout:
    """The repository's blockout, loaded once per process.

    Cached because it is read on every director tick and never changes at runtime.
    Tests that need a variant call :func:`load_blockout` directly.
    """
    return load_blockout()


__all__ = [
    "BLOCKOUT_RELATIVE",
    "Anchor",
    "Blockout",
    "CameraTransform",
    "Point",
    "ResolvedGazeTarget",
    "default_blockout",
    "load_blockout",
]
