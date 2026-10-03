"""The art asset contract: what painted plates must provide.

Formalised **now**, while the plates do not exist, for one reason: the brief's tenth
acceptance criterion is that final artwork can later be inserted without redesigning the
runtime. That is only achievable if the runtime was built against a declared contract
rather than against whatever the first delivered PSD happened to look like.

So this module declares, for every asset the system will need:

* its canonical pixel dimensions and the camera it belongs to
* its layer stack, in compositing order, each layer with a **depth from the blockout**
* the masks that must exist as separate channels
* the pivot and anchor it is positioned by
* which lighting layers must be delivered unbaked

Depth comes from geometry, not from the eye
-------------------------------------------
Every environment layer carries a depth in millimetres read off
`TRADE_FIX_OFFICE_01.blockout.json`, and parallax amplitude is derived from it. The brief
is explicit — *"Do not invent arbitrary parallax amounts by eye later"* — and the reason
is that eyeballed parallax is the single most common way a 2.5D scene reads as a stack of
cards rather than as a room.

Masks are specified before painting, deliberately
-------------------------------------------------
A flattened image cannot animate. Discovering that after nine character plates have been
painted is expensive and entirely avoidable, so :data:`CHARACTER_MASKS` and
:data:`ENVIRONMENT_MASKS` are part of the contract rather than a note for later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from tradefix_radio.visual.geometry import Blockout, default_blockout

#: Output the renderer targets, and the one it must scale to gracefully.
CANONICAL_OUTPUT: Final = (1920, 1080)
ALTERNATE_OUTPUT: Final = (2560, 1440)

#: Plates are delivered at this multiple of output size, so a slow push-in has real
#: pixels to move into rather than upscaling into softness.
PLATE_OVERSAMPLE: Final = 1.5

#: Contract version. Bumped when the required fields change, so a delivered manifest can
#: be checked against the runtime that will consume it.
CONTRACT_VERSION: Final = "1.0.0"


# ============================================================ vocabularies


@dataclass(frozen=True, slots=True)
class LayerSpec:
    """One painted layer inside a plate.

    ``depth_mm`` is the distance from the camera to the thing this layer depicts, taken
    from the blockout. ``parallax`` is derived from it rather than authored:
    :func:`parallax_from_depth` turns depth into a travel fraction, so two layers at the
    same real distance always move together.
    """

    layer_id: str
    order: int
    description: str
    depth_mm: float
    #: Layers that must ship with a separate alpha the runtime can deform independently.
    deformable: bool = False
    #: Composited at runtime rather than painted in — lighting and atmospherics.
    runtime_composite: bool = False
    #: Masks this layer must carry as separate channels.
    masks: tuple[str, ...] = ()

    def parallax(self, *, camera_amplitude: float) -> float:
        return parallax_from_depth(self.depth_mm, camera_amplitude=camera_amplitude)


def parallax_from_depth(depth_mm: float, *, camera_amplitude: float) -> float:
    """Travel fraction for a layer at ``depth_mm``, given the camera's amplitude.

    Inverse-depth, normalised so the character plane travels at the camera's full
    declared amplitude. Near layers move more, far layers less, and the city barely at
    all — which is the whole of what parallax is.

    The reference plane is the character at 2 248 mm (CAM_7's subject distance). A layer
    at half that distance moves twice as much; the far city at 400 m moves 0.6 % as much,
    which is correctly almost nothing.
    """
    reference = 2248.0
    if depth_mm <= 0:
        raise ValueError("depth must be positive")
    return camera_amplitude * (reference / depth_mm)


#: Character masks. Specified before painting because a flattened figure cannot animate,
#: and finding that out after nine plates is expensive and avoidable.
CHARACTER_MASKS: Final[tuple[str, ...]] = (
    "eyes",            # sclera/iris/pupil separable for gaze
    "eyelids",         # upper and lower, independently closable
    "brows",
    "mouth_jaw",
    "hand_left",
    "hand_right",
    "forearm_left",
    "forearm_right",
    "upper_arm_left",
    "upper_arm_right",
    "torso",
    "neck",
    "head",
    "hair_front",
    "hair_rear",
    "headphones_band",
    "headphones_cup_left",
    "headphones_cup_right",
    "pendant",
)

#: Occlusion masks the environment must provide, so the character can pass behind things.
ENVIRONMENT_MASKS: Final[tuple[str, ...]] = (
    "mug_occlusion",        # the mug in front of the hand
    "keyboard_occlusion",   # fingers behind key tops
    "monitor_foreground",   # screens and bezels in front of the character
    "desk_foreground",      # the near desk edge
    "chair_occlusion",      # chair arms and back over the torso
    "plant_foreground",
)

#: Lighting layers, delivered **unbaked** so they can modulate with market state.
#: Baking them makes `OFFICE_BIBLE.md` §5's response unimplementable.
LIGHTING_LAYERS: Final[tuple[str, ...]] = (
    "warm_key",
    "cool_fill",
    "monitor_spill",
    "gold_accent",
    "occlusion_ao",
)

#: The minimum separable character regions. The brief's instruction is to use the
#: *minimum* separation that achieves believable motion, not to split everything — each
#: extra layer costs atlas space and a seam that can show.
CHARACTER_LAYERS: Final[tuple[LayerSpec, ...]] = (
    LayerSpec("hair_rear", 0, "hair behind the head silhouette", 2300.0, deformable=True),
    LayerSpec("torso", 1, "chest, shoulders, overshirt", 2250.0, deformable=True),
    LayerSpec("upper_arm_left", 2, "left upper arm", 2240.0, deformable=True),
    LayerSpec("forearm_left", 3, "left forearm", 2200.0, deformable=True),
    LayerSpec("hand_left", 4, "left hand and fingers", 2170.0, deformable=True,
              masks=("hand_left",)),
    LayerSpec("upper_arm_right", 5, "right upper arm", 2240.0, deformable=True),
    LayerSpec("forearm_right", 6, "right forearm", 2200.0, deformable=True),
    LayerSpec("hand_right", 7, "right hand and fingers", 2170.0, deformable=True,
              masks=("hand_right",)),
    LayerSpec("neck", 8, "neck and collar", 2240.0, deformable=True),
    LayerSpec("head", 9, "skull, face base, ears", 2230.0, deformable=True,
              masks=("head",)),
    LayerSpec("brows", 10, "both brows, independently movable", 2228.0, deformable=True,
              masks=("brows",)),
    LayerSpec("eyes", 11, "sclera, iris, pupil, specular", 2228.0, deformable=True,
              masks=("eyes",)),
    LayerSpec("eyelids", 12, "upper and lower lids", 2227.0, deformable=True,
              masks=("eyelids",)),
    LayerSpec("mouth_jaw", 13, "lips, inner mouth, jaw line", 2228.0, deformable=True,
              masks=("mouth_jaw",)),
    LayerSpec("hair_front", 14, "fringe and the stray strands", 2220.0, deformable=True),
    LayerSpec("headphones", 15, "band and both cups", 2215.0, deformable=True,
              masks=("headphones_band", "headphones_cup_left", "headphones_cup_right")),
    LayerSpec("pendant", 16, "chain and bar, visible on a forward lean", 2210.0,
              deformable=True, masks=("pendant",)),
)


# ============================================================ assets


@dataclass(frozen=True, slots=True)
class AssetSpec:
    """One painted asset the pipeline must deliver."""

    asset_id: str
    asset_type: str          # character_reference | environment_plate | character_model
    description: str
    #: Canonical delivery size in pixels, already oversampled.
    width: int
    height: int
    #: Blockout anchor this asset is positioned by, if any.
    anchor: str | None
    #: Pivot in normalised plate coordinates, origin top-left.
    pivot: tuple[float, float]
    #: Cameras this asset applies to. Empty means all, or not camera-specific.
    cameras: tuple[str, ...]
    #: Character pose depicted, for reference plates.
    pose: str | None
    layers: tuple[LayerSpec, ...]
    masks: tuple[str, ...]
    lighting_layers: tuple[str, ...]
    #: The specification section this asset is judged against.
    source_reference: str
    version: str = "0.0.0"   # 0.0.0 = not yet delivered
    #: Acceptance gate the delivered asset must pass.
    acceptance: str = ""

    @property
    def delivered(self) -> bool:
        return self.version != "0.0.0"

    def to_json(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "asset_type": self.asset_type,
            "description": self.description,
            "canonical_dimensions": {"width": self.width, "height": self.height},
            "anchor": self.anchor,
            "pivot": {"x": self.pivot[0], "y": self.pivot[1]},
            "cameras": list(self.cameras),
            "character_pose": self.pose,
            "layer_order": [
                {
                    "layer_id": layer.layer_id,
                    "order": layer.order,
                    "description": layer.description,
                    "depth_mm": layer.depth_mm,
                    "deformable": layer.deformable,
                    "runtime_composite": layer.runtime_composite,
                    "masks": list(layer.masks),
                }
                for layer in self.layers
            ],
            "required_masks": list(self.masks),
            "lighting_state": {
                "baked": False,
                "layers": list(self.lighting_layers),
                "rule": "delivered unbaked; the runtime composites and modulates them",
            },
            "source_reference": self.source_reference,
            "version": self.version,
            "delivered": self.delivered,
            "acceptance": self.acceptance,
        }


def _plate_size(scale: float = PLATE_OVERSAMPLE) -> tuple[int, int]:
    return int(CANONICAL_OUTPUT[0] * scale), int(CANONICAL_OUTPUT[1] * scale)


#: The nine canonical character references. Painting order is an order: view 1 is
#: accepted first and becomes the identity reference for the other eight.
CHARACTER_REFERENCES: Final[tuple[AssetSpec, ...]] = tuple(
    AssetSpec(
        asset_id=asset_id,
        asset_type="character_reference",
        description=description,
        width=2048,
        height=2048 if "full_body" not in asset_id else 3072,
        anchor=None,
        pivot=(0.5, 0.5),
        cameras=cameras,
        pose=pose,
        layers=(),
        masks=CHARACTER_MASKS if index == 0 else (),
        lighting_layers=LIGHTING_LAYERS,
        source_reference=f"CHARACTER_BIBLE.md §9 view {index + 1}",
        acceptance=(
            "CHARACTER_BIBLE.md §5 in full — any single failure rejects the plate. "
            + (
                "This is the IDENTITY MASTER: nothing else may be painted until it passes."
                if index == 0
                else "Judged against view 1, not against the spec in isolation: the "
                "question is 'is this the same man', not 'is this a good face'."
            )
        ),
    )
    for index, (asset_id, description, pose, cameras) in enumerate(
        (
            ("char_ref_01_front_portrait",
             "Front portrait, head and shoulders, eye level, neutral focus",
             "seated_neutral", ("CAM_1", "CAM_4")),
            ("char_ref_02_three_quarter_left",
             "Three-quarter turned 35 degrees to his left", "seated_neutral", ("CAM_7",)),
            ("char_ref_03_three_quarter_right",
             "Three-quarter turned 35 degrees to his right", "seated_neutral", ("CAM_1",)),
            ("char_ref_04_profile_left",
             "True left profile at 90 degrees; ear and full headphone band visible",
             "seated_neutral", ("CAM_2",)),
            ("char_ref_05_profile_right",
             "True right profile at 90 degrees; braided cable behind the shoulder",
             "seated_neutral", ()),
            ("char_ref_06_full_body_front",
             "Standing full body, relaxed A-pose, full outfit including footwear",
             "standing_a_pose", ("CAM_5",)),
            ("char_ref_07_full_body_back",
             "Standing full body from behind; overshirt back, nape, rear of the band",
             "standing_a_pose", ("CAM_3",)),
            ("char_ref_08_seated_trading",
             "Seated at the desk, three-quarter front, forearms on the surface",
             "seated_working", ("CAM_1", "CAM_7")),
            ("char_ref_09_seated_side",
             "Seated side / three-quarter; the rig target for desk geometry",
             "seated_working", ("CAM_2", "CAM_6")),
        )
    )
)

#: The seven environment plates, one per camera. All depict one geometrically consistent
#: room, composed against the V1 blockout.
_ENVIRONMENT_PLATES: Final = (
    ("env_plate_01_hero_front", "CAM_1",
     "Hero front: monitor ledge at 30 % of frame, character centred, city behind",
     "The reference plate for colour and value. Every other plate is graded against it."),
    ("env_plate_02_side_desk", "CAM_2",
     "Side profile from the east: desk depth, window right, watch on the left wrist",
     "The plate TF-HP-01 is judged on — the only view with the full band profile."),
    ("env_plate_03_over_shoulder", "CAM_3",
     "Over his right shoulder: chart array lit, TF_MARK_SOUTH above, no city",
     "The only composition with readable chart content."),
    ("env_plate_04_wide_office", "CAM_5",
     "Wide office from the south-east, high: all six zones in one frame",
     "THE SPATIAL COHERENCE CHECK. If another plate disagrees with this one, that "
     "plate is wrong."),
    ("env_plate_05_desk_detail", "CAM_6",
     "Hands and desk: pen, notebook, mouse, keyboard, mug, stream pad",
     "The anchor-accuracy plate. Must contain all five interactive props."),
    ("env_plate_06_coffee_zone", "CAM_5",
     "Zone C as a destination: machine, mug shelf, practical, plant",
     "Must agree with the wide plate on every shared object."),
    ("env_plate_07_window_city", "CAM_2",
     "Window and city: three depth planes, mullion division, traffic road",
     "Night only. Window luminance below screen and amber-pool luminance."),
)


def _hero_layer_stack(blockout: Blockout) -> tuple[LayerSpec, ...]:
    """The CAM_1 stack, with depths read off the blockout.

    Derived rather than authored, so a layer's parallax cannot disagree with where the
    thing it depicts actually is. The camera sits at y 900; depth is the distance from
    it to each object's plane.
    """
    camera_y = blockout.camera("CAM_1").position[1]
    room_depth = blockout.room[1]

    def depth(y: float) -> float:
        return max(1.0, y - camera_y)

    return (
        LayerSpec("L00_far_city", 0, "far city haze and sky", 400_000.0),
        LayerSpec("L01_mid_city", 1, "tower silhouettes, lit crowns, aircraft light", 150_000.0),
        LayerSpec("L02_near_city", 2, "near building masses, window grids, traffic", 25_000.0),
        LayerSpec("L03_window_glass", 3, "glazing, reflections, mullions",
                  depth(room_depth), runtime_composite=False),
        LayerSpec("L04_rear_wall", 4, "north wall returns beside the glazing",
                  depth(room_depth)),
        LayerSpec("L05_shelves", 5, "west shelving and books, seen at frame edge",
                  depth(2600.0)),
        LayerSpec("L06_plants_rear", 6, "east plant", depth(1500.0)),
        LayerSpec("L07_chair_rear", 7, "chair back behind the figure",
                  depth(blockout.cameras["CAM_1"].target[1] + 250.0),
                  masks=("chair_occlusion",)),
        LayerSpec("L08_character", 8, "the TF_TRADER_01 layer group",
                  depth(3000.0), deformable=True, masks=CHARACTER_MASKS),
        LayerSpec("L09_monitor_screens", 9, "live screen surfaces", depth(2350.0)),
        LayerSpec("L10_monitor_bodies", 10, "bezels, arms, crossbar", depth(2350.0),
                  masks=("monitor_foreground",)),
        LayerSpec("L11_desk_props", 11, "notebook, pen, phone, laptop, mic",
                  depth(2700.0)),
        LayerSpec("L12_keyboard_mouse", 12, "keyboard, mouse, mat", depth(2740.0),
                  masks=("keyboard_occlusion",)),
        LayerSpec("L13_mug", 13, "the mug, in front of the hand", depth(2620.0),
                  masks=("mug_occlusion",)),
        LayerSpec("L14_desk_front", 14, "near desk edge and apron", depth(2200.0),
                  masks=("desk_foreground",)),
        LayerSpec("L15_coffee_steam", 15, "steam, driven by mug temperature",
                  depth(2600.0), runtime_composite=True),
        LayerSpec("L16_atmosphere", 16, "dust motes in the amber pools, bloom",
                  depth(2400.0), runtime_composite=True),
    )


def environment_plates(blockout: Blockout | None = None) -> tuple[AssetSpec, ...]:
    """The seven environment plates, with their layer stacks."""
    geometry = blockout or default_blockout()
    width, height = _plate_size()
    out: list[AssetSpec] = []
    for asset_id, camera_id, description, acceptance in _ENVIRONMENT_PLATES:
        # Only the hero stack is enumerated in full. The other six derive from the same
        # room and the same depth rule, and enumerating them before the hero plate is
        # accepted would be guessing at compositions nobody has seen.
        layers = _hero_layer_stack(geometry) if camera_id == "CAM_1" else ()
        out.append(
            AssetSpec(
                asset_id=asset_id,
                asset_type="environment_plate",
                description=description,
                width=width,
                height=height,
                anchor=None,
                pivot=(0.5, 0.5),
                cameras=(camera_id,),
                pose=None,
                layers=layers,
                masks=ENVIRONMENT_MASKS,
                lighting_layers=LIGHTING_LAYERS,
                source_reference=f"OFFICE_BIBLE.md §9, CAMERA_PLAN.md §3 {camera_id}",
                acceptance=acceptance + "  Plus OFFICE_BIBLE.md §10 in full.",
            )
        )
    return tuple(out)


def character_model() -> AssetSpec:
    """The one master layered character model (ADR-13)."""
    return AssetSpec(
        asset_id="TF_TRADER_01_master",
        asset_type="character_model",
        description=(
            "The single master layered model. Per-camera work is a projected view of "
            "the SAME layer tree — never a new face."
        ),
        width=4096,
        height=4096,
        anchor=None,
        pivot=(0.5, 0.92),   # at the seat, so a lean rotates about the right place
        cameras=tuple(sorted(default_blockout().cameras)),
        pose="seated_working",
        layers=CHARACTER_LAYERS,
        masks=CHARACTER_MASKS,
        lighting_layers=LIGHTING_LAYERS,
        source_reference="CHARACTER_BIBLE.md §4 layer taxonomy, §8 rig",
        acceptance=(
            "Layer names match CHARACTER_BIBLE.md §4 exactly — the exporter fails the "
            "build on a mismatch. Seated geometry must agree with the blockout: elbow "
            "z700, eye z1295, forearms in contact with a z735 surface."
        ),
    )


# ============================================================ the manifest


@dataclass
class AssetManifest:
    """Everything the art pipeline must deliver, and what is still outstanding."""

    contract_version: str = CONTRACT_VERSION
    assets: tuple[AssetSpec, ...] = field(default_factory=tuple)

    @classmethod
    def build(cls, blockout: Blockout | None = None) -> AssetManifest:
        geometry = blockout or default_blockout()
        return cls(
            assets=(character_model(), *CHARACTER_REFERENCES, *environment_plates(geometry))
        )

    @property
    def outstanding(self) -> tuple[str, ...]:
        return tuple(asset.asset_id for asset in self.assets if not asset.delivered)

    def by_id(self, asset_id: str) -> AssetSpec:
        for asset in self.assets:
            if asset.asset_id == asset_id:
                return asset
        raise KeyError(f"unknown asset {asset_id!r}")

    def to_json(self, blockout: Blockout | None = None) -> dict[str, Any]:
        geometry = blockout or default_blockout()
        return {
            "$contract_version": self.contract_version,
            "generated_from": "tradefix_radio/visual/assets.py",
            "authority": (
                "This file is GENERATED. Edit assets.py and re-run "
                "`tradefix visual manifest --write`. Hand edits will be overwritten."
            ),
            "blockout": geometry.blockout_id,
            "output": {
                "canonical": {"width": CANONICAL_OUTPUT[0], "height": CANONICAL_OUTPUT[1]},
                "alternate": {"width": ALTERNATE_OUTPUT[0], "height": ALTERNATE_OUTPUT[1]},
                "plate_oversample": PLATE_OVERSAMPLE,
                "target_fps": 30,
                "fps_note": (
                    "30 fps default, configurable. The frame-rate decision is deferred to "
                    "a measurement, not assumed: a stable 30 may beat an unstable 60."
                ),
            },
            "parallax": {
                "rule": (
                    "Every layer's parallax is DERIVED from its blockout depth via "
                    "parallax_from_depth(). Eyeballed parallax is the most common way a "
                    "2.5D scene reads as a stack of cards rather than as a room."
                ),
                "reference_plane_mm": 2248.0,
                "camera_amplitudes": _camera_amplitudes(),
            },
            "masks": {
                "character": list(CHARACTER_MASKS),
                "environment": list(ENVIRONMENT_MASKS),
                "rule": (
                    "Specified BEFORE painting. A flattened image cannot animate, and "
                    "discovering that after nine plates is expensive and avoidable."
                ),
            },
            "lighting": {
                "layers": list(LIGHTING_LAYERS),
                "baked": False,
                "rule": (
                    "Delivered unbaked. The runtime composites and modulates them; "
                    "baking makes OFFICE_BIBLE.md §5's response unimplementable."
                ),
            },
            "painting_order": {
                "rule": (
                    "char_ref_01_front_portrait is painted and accepted FIRST and becomes "
                    "the identity reference for the other eight. If a later view cannot be "
                    "made to match it, view 1 is NOT re-opened — the later view is "
                    "repainted. That is the whole mechanism by which one man appears in "
                    "nine drawings."
                ),
                "sequence": [
                    "char_ref_01_front_portrait",
                    "char_ref_02_three_quarter_left",
                    "char_ref_03_three_quarter_right",
                    "char_ref_04_profile_left",
                    "char_ref_05_profile_right",
                    "char_ref_06_full_body_front",
                    "char_ref_08_seated_trading",
                    "char_ref_07_full_body_back",
                    "char_ref_09_seated_side",
                    "TF_TRADER_01_master",
                    "env_plate_01_hero_front",
                    "env_plate_04_wide_office",
                ],
            },
            "character_layers": [
                {
                    "layer_id": layer.layer_id,
                    "order": layer.order,
                    "description": layer.description,
                    "deformable": layer.deformable,
                    "masks": list(layer.masks),
                }
                for layer in CHARACTER_LAYERS
            ],
            "assets": [asset.to_json() for asset in self.assets],
            "outstanding": list(self.outstanding),
            "summary": {
                "total": len(self.assets),
                "delivered": sum(1 for asset in self.assets if asset.delivered),
                "outstanding": len(self.outstanding),
            },
        }

    def validate(self, blockout: Blockout | None = None) -> list[str]:
        """Check the manifest against frozen geometry. Problems, not exceptions."""
        geometry = blockout or default_blockout()
        problems: list[str] = []
        seen: set[str] = set()
        for asset in self.assets:
            if asset.asset_id in seen:
                problems.append(f"duplicate asset id {asset.asset_id!r}")
            seen.add(asset.asset_id)
            for camera_id in asset.cameras:
                if camera_id not in geometry.cameras:
                    problems.append(f"{asset.asset_id}: unknown camera {camera_id!r}")
            if asset.anchor and asset.anchor not in geometry.anchors:
                problems.append(f"{asset.asset_id}: unknown anchor {asset.anchor!r}")
            orders = [layer.order for layer in asset.layers]
            if orders != sorted(orders):
                problems.append(f"{asset.asset_id}: layer order is not monotonic")
            if len(set(orders)) != len(orders):
                problems.append(f"{asset.asset_id}: duplicate layer order")
            for mask in asset.masks:
                if mask not in CHARACTER_MASKS + ENVIRONMENT_MASKS:
                    problems.append(f"{asset.asset_id}: unknown mask {mask!r}")
            for layer in asset.layers:
                if layer.depth_mm <= 0:
                    problems.append(f"{asset.asset_id}/{layer.layer_id}: non-positive depth")
        return problems


def _camera_amplitudes() -> dict[str, float]:
    # Imported here rather than at module scope: `camera` imports `geometry`, and a
    # top-level import would make assets -> camera -> geometry -> assets if the asset
    # contract is ever read from the camera metadata.
    from tradefix_radio.visual.camera import CAMERA_METADATA  # noqa: PLC0415

    return {
        camera_id: meta.parallax_amplitude for camera_id, meta in CAMERA_METADATA.items()
    }


def manifest_path() -> Path:
    return Path(__file__).resolve().parents[2] / "visual" / "assets" / "manifest.json"


def write_manifest(path: Path | None = None) -> Path:
    """Write `visual/assets/manifest.json` from this module."""
    target = path or manifest_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = AssetManifest.build().to_json()
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


__all__ = [
    "ALTERNATE_OUTPUT",
    "CANONICAL_OUTPUT",
    "CHARACTER_LAYERS",
    "CHARACTER_MASKS",
    "CHARACTER_REFERENCES",
    "CONTRACT_VERSION",
    "ENVIRONMENT_MASKS",
    "LIGHTING_LAYERS",
    "PLATE_OVERSAMPLE",
    "AssetManifest",
    "AssetSpec",
    "LayerSpec",
    "character_model",
    "environment_plates",
    "manifest_path",
    "parallax_from_depth",
    "write_manifest",
]
