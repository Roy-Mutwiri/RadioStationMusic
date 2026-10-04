"""Imported art: what proof mode needs, what is present, and whether it is valid.

Three jobs, and the third is the one that matters:

**Declare** the files proof mode can consume, with their exact dimensions, whether an
alpha channel is required, and which layer each replaces.

**Report** which are present, so the HUD can say `CHARACTER 0 / 9` rather than leaving a
viewer to guess why the trader looks procedural.

**Refuse** art that will not work. A plate at the wrong size, missing its alpha, or with
pivot metadata serialised as ``{"x": …, "y": …}`` instead of ``[x, y]`` fails
`tradefix visual assets validate` with the reason — rather than loading, looking subtly
wrong, and costing an afternoon. That last case is not hypothetical: one object-shaped
geometry vector took the renderer down completely, on the first draw call of every frame.

No artwork exists yet. This module's honest output today is "0 of 13 present", and the
renderer draws procedural proof shapes instead. See `visual/assets/IMPORT_ART_HERE.md`.
"""

from __future__ import annotations

import difflib
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

#: Where imported source art lives. Two directories, because character and environment
#: are delivered and versioned separately.
SOURCE_ROOT: Final = Path(__file__).resolve().parents[2] / "visual" / "assets" / "source"
CHARACTER_DIR: Final = SOURCE_ROOT / "character"
ENVIRONMENT_DIR: Final = SOURCE_ROOT / "environment"
IMPORT_GUIDE: Final = SOURCE_ROOT.parent / "IMPORT_ART_HERE.md"


@dataclass(frozen=True, slots=True)
class ArtSlot:
    """One file proof mode can consume."""

    filename: str
    group: str
    width: int
    height: int
    needs_alpha: bool
    #: The proof layer this replaces. The renderer falls back per layer, so a partial
    #: delivery composes rather than being all-or-nothing.
    layer: str
    #: False for the extras that improve the result without being required.
    required: bool = True
    #: Cameras this plate can serve. A flat plate painted for the hero shot cannot be
    #: stretched across the hands shot, and the renderer says so rather than faking it.
    cameras: tuple[str, ...] = ()

    @property
    def directory(self) -> Path:
        return CHARACTER_DIR if self.group == "character" else ENVIRONMENT_DIR

    @property
    def path(self) -> Path:
        return self.directory / self.filename


#: Nine character layers, four environment plates, three props.
#:
#: The character split is the brief's own hierarchy — head, torso, two arms, two hands,
#: eyes, eyelids — which is the minimum that lets the existing BehaviorDirector output be
#: seen. Hair and headphones are separate so the head can turn under them.
ART_SLOTS: Final[tuple[ArtSlot, ...]] = (
    # -- character
    ArtSlot("character_seated_hero.png", "character", 2048, 2048, True,
            "character", cameras=("CAM_1",)),
    ArtSlot("character_head.png", "character", 1024, 1024, True, "head",
            cameras=("CAM_1", "CAM_4", "CAM_7")),
    ArtSlot("character_torso.png", "character", 1536, 1536, True, "torso",
            cameras=("CAM_1", "CAM_7")),
    ArtSlot("character_arm_left.png", "character", 1024, 1024, True, "arm_l",
            cameras=("CAM_1", "CAM_7")),
    ArtSlot("character_arm_right.png", "character", 1024, 1024, True, "arm_r",
            cameras=("CAM_1", "CAM_7")),
    ArtSlot("character_hand_left.png", "character", 512, 512, True, "hand_l",
            cameras=("CAM_1", "CAM_6", "CAM_7")),
    ArtSlot("character_hand_right.png", "character", 512, 512, True, "hand_r",
            cameras=("CAM_1", "CAM_6", "CAM_7")),
    ArtSlot("character_eyes.png", "character", 512, 256, True, "eyes",
            cameras=("CAM_1", "CAM_4", "CAM_7")),
    ArtSlot("character_eyelids.png", "character", 512, 256, True, "eyelids",
            cameras=("CAM_1", "CAM_4", "CAM_7")),
    ArtSlot("character_hair.png", "character", 1024, 1024, True, "hair",
            required=False, cameras=("CAM_1", "CAM_4", "CAM_7")),
    ArtSlot("character_headphones.png", "character", 1024, 512, True, "headphones",
            required=False, cameras=("CAM_1", "CAM_4", "CAM_7")),
    # -- environment
    ArtSlot("office_hero.png", "environment", 3840, 2160, False, "rear_office",
            cameras=("CAM_1",)),
    ArtSlot("desk_foreground.png", "environment", 3840, 2160, True, "desk",
            cameras=("CAM_1",)),
    ArtSlot("monitor_layer.png", "environment", 2048, 1024, True, "monitors",
            cameras=("CAM_1", "CAM_3")),
    ArtSlot("city_skyline.png", "environment", 4096, 1536, True, "city",
            cameras=("CAM_1", "CAM_5", "CAM_7")),
    ArtSlot("coffee_mug.png", "environment", 512, 512, True, "prop_mug",
            required=False, cameras=("CAM_1", "CAM_6")),
    ArtSlot("notebook.png", "environment", 512, 512, True, "prop_notebook",
            required=False, cameras=("CAM_1", "CAM_6")),
    ArtSlot("pen.png", "environment", 256, 256, True, "prop_pen",
            required=False, cameras=("CAM_1", "CAM_6")),
)


# ============================================================ PNG inspection


def read_png_header(path: Path) -> tuple[int, int, int] | None:
    """`(width, height, colour_type)` from a PNG's IHDR, or `None` if it is not a PNG.

    Parsed directly rather than through Pillow: the validator must work on a machine with
    no imaging library installed, and an IHDR is 13 bytes at a fixed offset. Colour type
    4 is greyscale+alpha and 6 is RGBA — those are the two that carry an alpha channel.
    """
    try:
        with path.open("rb") as handle:
            signature = handle.read(8)
            if signature != b"\x89PNG\r\n\x1a\n":
                return None
            length_bytes = handle.read(4)
            chunk_type = handle.read(4)
            if chunk_type != b"IHDR" or len(length_bytes) != 4:
                return None
            width, height = struct.unpack(">II", handle.read(8))
            handle.read(1)  # bit depth
            colour_type = handle.read(1)[0]
            return width, height, colour_type
    except (OSError, struct.error, IndexError):
        return None


def _has_alpha(colour_type: int) -> bool:
    return colour_type in (4, 6)


# ============================================================ status


@dataclass(frozen=True, slots=True)
class SlotStatus:
    slot: ArtSlot
    present: bool
    valid: bool
    problems: tuple[str, ...]
    width: int | None = None
    height: int | None = None
    #: Resolved assignment — the sidecar's list if it overrides, else the slot's.
    cameras: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "filename": self.slot.filename,
            "group": self.slot.group,
            "layer": self.slot.layer,
            "required": self.slot.required,
            "expected": [self.slot.width, self.slot.height],
            "found": [self.width, self.height] if self.present else None,
            "present": self.present,
            "valid": self.valid,
            "problems": list(self.problems),
            "cameras": list(self.cameras or self.slot.cameras),
            "needs_alpha": self.slot.needs_alpha,
            #: The URL the renderer loads it from. Only ever set for a valid plate, so
            #: the browser cannot be told to fetch something that failed validation.
            "url": (
                f"/art/{self.slot.group}/{self.slot.filename}" if self.valid else None
            ),
        }


def inspect_slot(slot: ArtSlot) -> SlotStatus:
    """Check one slot. Reports problems; never raises, never guesses."""
    path = slot.path
    if not path.is_file():
        return SlotStatus(slot=slot, present=False, valid=False, problems=())

    problems: list[str] = []
    header = read_png_header(path)
    if header is None:
        return SlotStatus(
            slot=slot, present=True, valid=False,
            problems=("not a readable PNG",),
        )

    width, height, colour_type = header
    if (width, height) != (slot.width, slot.height):
        problems.append(
            f"dimensions are {width}x{height}, expected {slot.width}x{slot.height}"
        )
    if slot.needs_alpha and not _has_alpha(colour_type):
        problems.append(
            f"no alpha channel (PNG colour type {colour_type}); this layer composites "
            "over others and must be transparent where it is empty"
        )
    if path.stat().st_size < 1024:
        problems.append(
            f"only {path.stat().st_size} bytes — this looks like a placeholder rather "
            "than artwork"
        )

    # Optional sidecar metadata: pivot schema and camera assignment.
    pivot_path = path.with_name(path.stem + "_pivot.json")
    if pivot_path.is_file():
        problems.extend(_sidecar_problems(pivot_path, slot))

    cameras = sidecar_cameras(slot)
    if not cameras:
        problems.append(
            "no camera assignment: this plate would be imported and never drawn"
        )

    return SlotStatus(
        slot=slot, present=True, valid=not problems,
        problems=tuple(problems), width=width, height=height, cameras=cameras,
    )


#: Every camera a sidecar may name. Anything else is a typo, and a typo in a camera
#: assignment means a plate silently never appears.
KNOWN_CAMERAS: Final = frozenset(
    {"CAM_1", "CAM_2", "CAM_3", "CAM_4", "CAM_5", "CAM_6", "CAM_7"}
)


def _sidecar_problems(path: Path, slot: ArtSlot) -> list[str]:
    """Validate a `*_pivot.json` sidecar: pivot schema and camera assignment.

    **Pivot** must carry `[x, y]`, not `{x, y}`. The same inconsistency in the scene
    payload took the renderer down on frame zero: `{"x": …, "y": …}` reached an array
    destructuring as `object is not iterable`. Vectors are arrays here, everywhere, and
    this is where an imported file is told so.

    **Cameras**, when the sidecar overrides the slot's defaults, must be a non-empty list
    of known camera ids. An empty list is not "all cameras" — it is a plate that can
    never be drawn, and silently accepting it produces a layer that is imported, valid
    and invisible, which is the worst of the three.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"{path.name} is not readable JSON: {error}"]
    if not isinstance(data, dict):
        return [f"{path.name} must contain a JSON object"]

    problems: list[str] = []

    # -- pivot
    pivot = data.get("pivot")
    if pivot is None:
        problems.append(f"{path.name} has no `pivot` key")
    elif isinstance(pivot, dict):
        problems.append(
            f"{path.name}: pivot is an object {pivot!r}; it must be a [x, y] array. "
            "An object-shaped vector reaches the renderer as "
            "`object is not iterable` and stops the draw loop."
        )
    elif not isinstance(pivot, list) or len(pivot) != 2:
        problems.append(f"{path.name}: pivot must be a 2-element array, got {pivot!r}")
    else:
        for index, value in enumerate(pivot):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                problems.append(f"{path.name}: pivot[{index}] is not a number")
            elif not 0.0 <= float(value) <= 1.0:
                problems.append(
                    f"{path.name}: pivot[{index}] is {value}, expected 0..1 "
                    "(a fraction of the plate, not a pixel)"
                )

    # -- camera assignment
    if "cameras" in data:
        cameras = data["cameras"]
        if not isinstance(cameras, list):
            problems.append(
                f"{path.name}: cameras must be an array of camera ids, got {cameras!r}"
            )
        elif not cameras:
            problems.append(
                f"{path.name}: cameras is empty. A plate assigned to no camera is "
                "imported, valid and invisible — name the cameras it was painted for, "
                f"or delete the key to use the defaults {list(slot.cameras)}"
            )
        else:
            unknown = sorted(str(c) for c in cameras if c not in KNOWN_CAMERAS)
            if unknown:
                problems.append(
                    f"{path.name}: unknown camera(s) {unknown}; the seven frozen V1 "
                    f"cameras are {sorted(KNOWN_CAMERAS)}"
                )
    return problems


def sidecar_cameras(slot: ArtSlot) -> tuple[str, ...]:
    """The cameras this plate serves: the sidecar's list, or the slot's declaration.

    Resolved in one place so the renderer and the validator cannot disagree about which
    camera a plate is for.
    """
    path = slot.path.with_name(slot.path.stem + "_pivot.json")
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return slot.cameras
        cameras = data.get("cameras")
        if isinstance(cameras, list) and cameras:
            valid = tuple(c for c in cameras if c in KNOWN_CAMERAS)
            if valid:
                return valid
    return slot.cameras


def unexpected_files() -> list[str]:
    """Files in the import directories that no slot declares.

    Reported rather than ignored. A misspelled filename is the likeliest import mistake
    and the hardest to spot from the browser: the loader does not guess, so the plate
    simply never appears and the HUD keeps saying 0/9 while the file sits right there.
    """
    declared = {slot.filename for slot in ART_SLOTS}
    sidecars = {slot.path.stem + "_pivot.json" for slot in ART_SLOTS}
    allowed = declared | sidecars | {"README.md"}

    out: list[str] = []
    for directory in (CHARACTER_DIR, ENVIRONMENT_DIR):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.is_file() and path.name not in allowed:
                suggestion = _closest(path.name, declared)
                hint = f" — did you mean {suggestion}?" if suggestion else ""
                out.append(f"{directory.name}/{path.name} is not a declared asset{hint}")
    return out


def _closest(name: str, candidates: set[str]) -> str | None:
    """The nearest declared filename, for a typo hint. `difflib`, nothing clever."""
    matches = difflib.get_close_matches(name, sorted(candidates), n=1, cutoff=0.6)
    return matches[0] if matches else None


def inspect_all() -> list[SlotStatus]:
    return [inspect_slot(slot) for slot in ART_SLOTS]


def status() -> dict[str, Any]:
    """What the HUD's ART panel and `/api/visual/assets` report."""
    statuses = inspect_all()

    def tally(group: str) -> dict[str, int]:
        subset = [s for s in statuses if s.slot.group == group]
        required = [s for s in subset if s.slot.required]
        return {
            "present": sum(1 for s in subset if s.present),
            "valid": sum(1 for s in subset if s.valid),
            "required": len(required),
            "required_valid": sum(1 for s in required if s.valid),
            "total": len(subset),
        }

    character = tally("character")
    environment = tally("environment")
    #: The nine layers the brief names as enough to see the engine working.
    motion_layers = [
        "head", "torso", "arm_l", "arm_r", "hand_l", "hand_r",
        "eyes", "eyelids", "headphones",
    ]
    motion_valid = sum(
        1 for s in statuses if s.slot.layer in motion_layers and s.valid
    )

    # Which cameras an imported plate could actually serve.
    served: set[str] = set()
    for entry in statuses:
        if entry.valid:
            served.update(entry.cameras or entry.slot.cameras)

    strays = unexpected_files()
    return {
        "source_root": str(SOURCE_ROOT),
        "import_guide": str(IMPORT_GUIDE),
        "any_art_present": any(s.present for s in statuses),
        "any_art_valid": any(s.valid for s in statuses),
        "character": character,
        "environment": environment,
        "motion_layers": {"valid": motion_valid, "required": len(motion_layers)},
        "cameras_with_art": sorted(served),
        "slots": [s.to_json() for s in statuses],
        "problems": [
            {"filename": s.slot.filename, "problems": list(s.problems)}
            for s in statuses if s.present and not s.valid
        ],
        "unexpected_files": strays,
    }


def validate() -> tuple[bool, list[str]]:
    """`(ok, lines)` for the CLI. `ok` is False if any present file is unusable.

    Absent files are not failures — no art exists yet, and reporting that as an error
    every run would train everyone to ignore the output. A *broken* file is a failure.
    """
    statuses = inspect_all()
    lines: list[str] = []
    broken = [s for s in statuses if s.present and not s.valid]
    strays = unexpected_files()

    for group in ("character", "environment"):
        lines.append(f"  {group.upper()}")
        for entry in [s for s in statuses if s.slot.group == group]:
            alpha = "alpha" if entry.slot.needs_alpha else "  -  "
            cameras = ",".join(
                c.replace("CAM_", "") for c in (entry.cameras or entry.slot.cameras)
            )
            if not entry.present:
                mark = "MISSING " if entry.slot.required else "optional"
                lines.append(
                    f"    {mark} {entry.slot.filename:<32} "
                    f"{entry.slot.width:>5}x{entry.slot.height:<5} {alpha}  cam {cameras}"
                )
                continue
            if entry.valid:
                lines.append(
                    f"    ok       {entry.slot.filename:<32} "
                    f"{entry.width:>5}x{entry.height:<5} {alpha}  cam {cameras}"
                )
            else:
                lines.append(f"    INVALID  {entry.slot.filename}")
                for problem in entry.problems:
                    lines.append(f"               {problem}")
        lines.append("")

    if strays:
        lines.append("  UNEXPECTED FILES")
        for stray in strays:
            lines.append(f"    REJECTED {stray}")
        lines.append("")

    return not (broken or strays), lines


__all__ = [
    "ART_SLOTS",
    "CHARACTER_DIR",
    "ENVIRONMENT_DIR",
    "IMPORT_GUIDE",
    "KNOWN_CAMERAS",
    "SOURCE_ROOT",
    "ArtSlot",
    "SlotStatus",
    "inspect_all",
    "inspect_slot",
    "read_png_header",
    "sidecar_cameras",
    "status",
    "unexpected_files",
    "validate",
]
