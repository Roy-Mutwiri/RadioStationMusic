"""Imported art: the slot contract, the validator, and proof mode's fallback.

The validator exists so that a wrong plate is caught by a command rather than by a human
squinting at a browser. Its most important check is the pivot one: metadata carrying
``{"x": …, "y": …}`` instead of ``[x, y]`` is refused, because that exact inconsistency
in the scene payload stopped the renderer on the first draw call of every frame.

No artwork exists yet, so the honest state these tests pin is "0 of 13 required present,
everything procedural" — and that the renderer says so rather than looking broken.
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tradefix_radio.visual import art
from tradefix_radio.visual.demo import DemoStateSource
from tradefix_radio.visual.service import RUNTIME_DIR, VisualRuntime, create_app

REPO = Path(__file__).resolve().parents[2]


# ============================================================ a real PNG, built here


def _png(width: int, height: int, *, alpha: bool, pad: int = 4096) -> bytes:
    """A minimal valid PNG. Built rather than checked in, so the fixtures cannot rot.

    `pad` inflates the file past the validator's placeholder threshold; a test that
    tripped the "too small to be artwork" check would be testing the wrong thing.
    """
    colour_type = 6 if alpha else 2
    channels = 4 if alpha else 3
    raw = b"".join(
        b"\x00" + bytes([(x * 7) % 256] * channels * width)
        for x in range(height)
    )

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, colour_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 1))
        + chunk(b"tEXt", b"pad\x00" + b"x" * pad)
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def art_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point the module's directories at a temporary tree.

    Done by monkeypatching rather than by writing into `visual/assets/source/`: a test
    that left a file in the real import folder would make the next demo run show art that
    nobody approved.
    """
    character = tmp_path / "character"
    environment = tmp_path / "environment"
    character.mkdir()
    environment.mkdir()
    monkeypatch.setattr(art, "SOURCE_ROOT", tmp_path)
    monkeypatch.setattr(art, "CHARACTER_DIR", character)
    monkeypatch.setattr(art, "ENVIRONMENT_DIR", environment)
    return {"character": character, "environment": environment}


def _slot(filename: str) -> art.ArtSlot:
    for slot in art.ART_SLOTS:
        if slot.filename == filename:
            return slot
    raise AssertionError(f"{filename} is not a declared slot")


# ============================================================ the slot contract


def test_the_slots_cover_the_filenames_the_guide_promises() -> None:
    """The guide is what a human reads; the slots are what the loader enforces.

    If they disagree, somebody copies in a correctly-named file and the loader ignores it.
    """
    guide = (REPO / "visual" / "assets" / "IMPORT_ART_HERE.md").read_text(
        encoding="utf-8"
    )
    for slot in art.ART_SLOTS:
        assert f"`{slot.filename}`" in guide, (
            f"{slot.filename} is a declared slot but the import guide does not list it"
        )
        assert f"{slot.width}×{slot.height}" in guide or (
            f"{slot.width}x{slot.height}" in guide
        ), f"{slot.filename}'s dimensions are not stated in the guide"


def test_the_nine_motion_layers_are_all_required() -> None:
    """The brief's minimum for seeing the engine work: head, torso, arms, hands, eyes,
    lids. Those cannot be optional, or a partial delivery leaves the character static."""
    needed = {"head", "torso", "arm_l", "arm_r", "hand_l", "hand_r", "eyes", "eyelids"}
    by_layer = {slot.layer: slot for slot in art.ART_SLOTS}
    for layer in needed:
        assert layer in by_layer, layer
        assert by_layer[layer].required, f"{layer} must be required"


def test_every_character_layer_needs_an_alpha_channel() -> None:
    """A layer that composites over another and ships opaque hides everything behind it."""
    for slot in art.ART_SLOTS:
        if slot.group == "character":
            assert slot.needs_alpha, slot.filename


def test_the_office_background_does_not_need_alpha() -> None:
    """It is the backmost layer. Requiring transparency there would be cargo-culting."""
    assert _slot("office_hero.png").needs_alpha is False
    assert _slot("desk_foreground.png").needs_alpha is True


def test_every_slot_declares_which_cameras_it_can_serve() -> None:
    """A flat plate painted for the hero shot cannot be stretched across the hands shot."""
    for slot in art.ART_SLOTS:
        assert slot.cameras, f"{slot.filename} declares no cameras"
        for camera in slot.cameras:
            assert camera.startswith("CAM_")


# ============================================================ PNG inspection


def test_the_png_header_reader_works_without_an_imaging_library(
    tmp_path: Path,
) -> None:
    """Parsed from the IHDR directly, so the validator runs on a bare machine."""
    rgba = tmp_path / "rgba.png"
    rgba.write_bytes(_png(64, 32, alpha=True))
    assert art.read_png_header(rgba) == (64, 32, 6)

    rgb = tmp_path / "rgb.png"
    rgb.write_bytes(_png(8, 9, alpha=False))
    assert art.read_png_header(rgb) == (8, 9, 2)

    not_png = tmp_path / "nope.png"
    not_png.write_bytes(b"JFIF nonsense")
    assert art.read_png_header(not_png) is None

    assert art.read_png_header(tmp_path / "absent.png") is None


def test_a_valid_plate_passes(art_dirs: dict[str, Path]) -> None:
    slot = _slot("character_head.png")
    (art_dirs["character"] / slot.filename).write_bytes(
        _png(slot.width, slot.height, alpha=True)
    )
    status = art.inspect_slot(slot)
    assert status.present
    assert status.valid, status.problems
    assert (status.width, status.height) == (slot.width, slot.height)


def test_the_wrong_dimensions_are_refused(art_dirs: dict[str, Path]) -> None:
    slot = _slot("character_head.png")
    (art_dirs["character"] / slot.filename).write_bytes(_png(512, 512, alpha=True))
    status = art.inspect_slot(slot)
    assert status.present
    assert not status.valid
    assert any("dimensions are 512x512" in p for p in status.problems)
    assert any("expected 1024x1024" in p for p in status.problems)


def test_a_missing_alpha_channel_is_refused(art_dirs: dict[str, Path]) -> None:
    slot = _slot("character_torso.png")
    (art_dirs["character"] / slot.filename).write_bytes(
        _png(slot.width, slot.height, alpha=False)
    )
    status = art.inspect_slot(slot)
    assert not status.valid
    assert any("no alpha channel" in p for p in status.problems)


def test_a_placeholder_sized_file_is_refused(art_dirs: dict[str, Path]) -> None:
    """A 200-byte PNG is somebody testing the pipeline, not a delivered plate."""
    slot = _slot("character_eyes.png")
    (art_dirs["character"] / slot.filename).write_bytes(
        _png(slot.width, slot.height, alpha=True, pad=0)[:900]
    )
    status = art.inspect_slot(slot)
    assert not status.valid


def test_a_file_that_is_not_a_png_is_refused(art_dirs: dict[str, Path]) -> None:
    slot = _slot("character_head.png")
    (art_dirs["character"] / slot.filename).write_bytes(b"this is not a png" * 200)
    status = art.inspect_slot(slot)
    assert status.present
    assert not status.valid
    assert "not a readable PNG" in status.problems[0]


# ============================================================ pivot metadata


def test_an_object_shaped_pivot_is_refused(art_dirs: dict[str, Path]) -> None:
    """The defect that stopped the renderer, caught at the import boundary this time.

    `{"x": 0.5, "y": 0.9}` reaching an array destructuring throws
    `object is not iterable`. Vectors are arrays here, everywhere, including in art
    metadata.
    """
    slot = _slot("character_head.png")
    (art_dirs["character"] / slot.filename).write_bytes(
        _png(slot.width, slot.height, alpha=True)
    )
    (art_dirs["character"] / "character_head_pivot.json").write_text(
        json.dumps({"pivot": {"x": 0.5, "y": 0.92}}), encoding="utf-8"
    )
    status = art.inspect_slot(slot)
    assert not status.valid
    problem = " ".join(status.problems)
    assert "pivot is an object" in problem
    assert "must be a [x, y] array" in problem
    assert "object is not iterable" in problem


def test_an_array_pivot_passes(art_dirs: dict[str, Path]) -> None:
    slot = _slot("character_head.png")
    (art_dirs["character"] / slot.filename).write_bytes(
        _png(slot.width, slot.height, alpha=True)
    )
    (art_dirs["character"] / "character_head_pivot.json").write_text(
        json.dumps({"pivot": [0.5, 0.92]}), encoding="utf-8"
    )
    assert art.inspect_slot(slot).valid


@pytest.mark.parametrize(
    "pivot", [[0.5], [0.5, 0.5, 0.5], [1.4, 0.5], [-0.1, 0.5], ["a", "b"]]
)
def test_a_malformed_pivot_array_is_refused(
    art_dirs: dict[str, Path], pivot: object
) -> None:
    slot = _slot("character_head.png")
    (art_dirs["character"] / slot.filename).write_bytes(
        _png(slot.width, slot.height, alpha=True)
    )
    (art_dirs["character"] / "character_head_pivot.json").write_text(
        json.dumps({"pivot": pivot}), encoding="utf-8"
    )
    assert not art.inspect_slot(slot).valid


def test_an_empty_camera_assignment_is_refused(art_dirs: dict[str, Path]) -> None:
    """A plate assigned to no camera is imported, valid and invisible.

    The worst of the three outcomes: the validator says fine, the HUD counts it, and
    nothing is ever drawn. An empty list is not shorthand for "all cameras".
    """
    slot = _slot("character_head.png")
    (art_dirs["character"] / slot.filename).write_bytes(
        _png(slot.width, slot.height, alpha=True)
    )
    (art_dirs["character"] / "character_head_pivot.json").write_text(
        json.dumps({"pivot": [0.5, 0.9], "cameras": []}), encoding="utf-8"
    )
    status = art.inspect_slot(slot)
    assert not status.valid
    problem = " ".join(status.problems)
    assert "cameras is empty" in problem
    assert "imported, valid and invisible" in problem


def test_an_unknown_camera_is_refused(art_dirs: dict[str, Path]) -> None:
    """A typo in a camera id means a plate that silently never appears."""
    slot = _slot("character_head.png")
    (art_dirs["character"] / slot.filename).write_bytes(
        _png(slot.width, slot.height, alpha=True)
    )
    (art_dirs["character"] / "character_head_pivot.json").write_text(
        json.dumps({"pivot": [0.5, 0.9], "cameras": ["CAM_1", "CAM_9"]}),
        encoding="utf-8",
    )
    status = art.inspect_slot(slot)
    assert not status.valid
    assert any("CAM_9" in p for p in status.problems)


def test_a_sidecar_may_narrow_the_camera_assignment(art_dirs: dict[str, Path]) -> None:
    """The artist knows which angle they painted. A valid override is honoured."""
    slot = _slot("character_head.png")
    (art_dirs["character"] / slot.filename).write_bytes(
        _png(slot.width, slot.height, alpha=True)
    )
    (art_dirs["character"] / "character_head_pivot.json").write_text(
        json.dumps({"pivot": [0.5, 0.9], "cameras": ["CAM_4"]}), encoding="utf-8"
    )
    status = art.inspect_slot(slot)
    assert status.valid, status.problems
    assert status.cameras == ("CAM_4",)
    assert art.sidecar_cameras(slot) == ("CAM_4",)


def test_a_misspelled_filename_is_reported_with_a_suggestion(
    art_dirs: dict[str, Path],
) -> None:
    """The likeliest import mistake, and the hardest to spot from the browser.

    The loader does not guess, so a misspelled plate simply never appears while the HUD
    keeps saying 0/9 and the file sits right there in the folder.
    """
    (art_dirs["character"] / "charcter_hed.png").write_bytes(_png(1024, 1024, alpha=True))
    (art_dirs["environment"] / "office.jpg").write_bytes(b"x" * 5_000)

    strays = art.unexpected_files()
    assert len(strays) == 2
    joined = " ".join(strays)
    assert "charcter_hed.png is not a declared asset" in joined
    assert "did you mean character_head.png?" in joined
    assert "office.jpg" in joined

    ok, lines = art.validate()
    assert ok is False
    assert any("REJECTED" in line for line in lines)


def test_a_readme_is_not_treated_as_a_stray(art_dirs: dict[str, Path]) -> None:
    """The import folders ship with a README. Flagging it every run is noise."""
    (art_dirs["character"] / "README.md").write_text("notes", encoding="utf-8")
    assert art.unexpected_files() == []


def test_only_a_valid_plate_is_offered_to_the_browser(art_dirs: dict[str, Path]) -> None:
    """The renderer loads by URL, and a URL exists only for a plate that passed.

    So a file with the wrong dimensions can sit in a served directory without the browser
    ever being told to fetch it.
    """
    slot = _slot("character_head.png")
    (art_dirs["character"] / slot.filename).write_bytes(_png(512, 512, alpha=True))
    bad = art.inspect_slot(slot).to_json()
    assert bad["valid"] is False
    assert bad["url"] is None

    (art_dirs["character"] / slot.filename).write_bytes(
        _png(slot.width, slot.height, alpha=True)
    )
    good = art.inspect_slot(slot).to_json()
    assert good["valid"] is True
    assert good["url"] == "/art/character/character_head.png"


# ============================================================ status reporting


def test_the_status_reports_nothing_imported_today() -> None:
    """The real state of the repository, asserted rather than assumed.

    When art arrives this test changes — which is the point: nobody should be able to
    claim art is wired up while this still reads zero.
    """
    report = art.status()
    assert report["any_art_present"] is False
    assert report["character"]["present"] == 0
    assert report["environment"]["present"] == 0
    assert report["motion_layers"]["valid"] == 0
    assert report["cameras_with_art"] == []
    assert report["problems"] == []


def test_the_status_counts_a_partial_delivery(art_dirs: dict[str, Path]) -> None:
    """Proof mode composes per layer, so a partial delivery must be reported as partial."""
    for filename in ("character_head.png", "character_torso.png"):
        slot = _slot(filename)
        (art_dirs["character"] / filename).write_bytes(
            _png(slot.width, slot.height, alpha=True)
        )
    report = art.status()
    assert report["any_art_present"] is True
    assert report["character"]["present"] == 2
    assert report["character"]["valid"] == 2
    assert report["character"]["required_valid"] == 2
    assert report["character"]["required"] == 9
    assert report["motion_layers"]["valid"] == 2
    assert set(report["cameras_with_art"]) >= {"CAM_1", "CAM_7"}


def test_absent_art_is_not_a_validation_failure() -> None:
    """No art exists. Failing every run on that trains everyone to ignore the output."""
    ok, lines = art.validate()
    assert ok is True
    assert any("MISSING" in line for line in lines)


def test_a_broken_file_is_a_validation_failure(art_dirs: dict[str, Path]) -> None:
    slot = _slot("character_head.png")
    (art_dirs["character"] / slot.filename).write_bytes(_png(256, 256, alpha=False))
    ok, lines = art.validate()
    assert ok is False
    assert any("INVALID" in line for line in lines)


# ============================================================ the service and HUD


def _client() -> TestClient:
    return TestClient(create_app(VisualRuntime(seed=7, demo=DemoStateSource(seed=7))))


def test_the_service_exposes_the_asset_status() -> None:
    with _client() as client:
        report = client.get("/api/visual/assets").json()
    for key in ("character", "environment", "motion_layers", "any_art_present",
                "source_root", "import_guide", "slots", "cameras_with_art"):
        assert key in report, key
    assert report["any_art_present"] is False


def test_the_hud_has_an_art_panel() -> None:
    """So a viewer can see *why* the trader is procedural rather than guessing."""
    html = (RUNTIME_DIR / "index.html").read_text(encoding="utf-8")
    for element in ("a-mode", "a-char", "a-env", "a-motion", "a-source"):
        assert f'id="{element}"' in html, element
    source = (RUNTIME_DIR / "app.js").read_text(encoding="utf-8")
    assert "/api/visual/assets" in source


# ============================================================ proof mode


def test_the_page_loads_proof_before_app() -> None:
    """`TF_PROOF` must exist when the draw loop takes its first frame."""
    html = (RUNTIME_DIR / "index.html").read_text(encoding="utf-8")
    assert html.index('src="proof.js"') < html.index('src="app.js"')


def test_proof_mode_is_the_default_and_blockout_is_still_reachable() -> None:
    source = (RUNTIME_DIR / "app.js").read_text(encoding="utf-8")
    assert "const ART_MODES = ['proof', 'blockout', 'final']" in source
    assert "ART_MODES.includes(params.get('art')) ? params.get('art') : 'proof'" in source


def test_the_blockout_view_is_not_deleted() -> None:
    """Kept on purpose: it is what the geometry freeze suite verifies against, and the
    right view for checking a camera transform rather than a composition."""
    source = (RUNTIME_DIR / "app.js").read_text(encoding="utf-8")
    assert "_drawRoomShell" in source
    assert "_drawBoxes" in source
    assert "ART_MODE === 'blockout'" in source


def test_both_art_modes_share_one_animation_path() -> None:
    """Proof art draws different shapes at the *same* joint transforms.

    A second resolver would be a second thing to keep in step, and the first divergence
    would be invisible until someone noticed the two views disagreeing about where a hand
    was.
    """
    source = (RUNTIME_DIR / "app.js").read_text(encoding="utf-8")
    assert "_resolveCharacter(now)" in source
    proof = (RUNTIME_DIR / "proof.js").read_text(encoding="utf-8")
    assert "runtime.worlds" in proof
    # Proof art must not resolve its own transforms.
    assert "_worldFor" not in proof
    assert "_advancePose" not in proof


def test_proof_art_is_labelled_as_not_approved() -> None:
    """It must be impossible to mistake procedural shapes for the locked concept art."""
    proof = (RUNTIME_DIR / "proof.js").read_text(encoding="utf-8")
    assert "TEMP_PROOF" in proof
    assert "What this is NOT: the approved concept art" in proof
    source = (RUNTIME_DIR / "app.js").read_text(encoding="utf-8")
    assert "NOT APPROVED ARTWORK" in source
    html = (RUNTIME_DIR / "index.html").read_text(encoding="utf-8")
    assert "TEMP_PROOF" in html


def test_proof_art_uses_only_frozen_positions() -> None:
    """Nothing may be eyeballed into the frame.

    Every shape resolves from `scene.anchors`, `scene.joints`, `scene.boxes` or
    `scene.room` — which is why the composition holds from all seven cameras rather than
    only the one it was tuned on.
    """
    proof = (RUNTIME_DIR / "proof.js").read_text(encoding="utf-8")
    for source_of_truth in ("scene.anchors", "scene.boxes", "scene.quads", "this.room",
                            "runtime.worlds"):
        assert source_of_truth in proof, source_of_truth


def test_proof_art_declares_the_seven_layers() -> None:
    proof = (RUNTIME_DIR / "proof.js").read_text(encoding="utf-8")
    for layer in ("city", "rearOffice", "monitors", "character", "desk", "props",
                  "atmosphere"):
        assert f"{layer}(" in proof, layer


def test_the_charts_freeze_when_the_feed_does() -> None:
    """The honesty rule applies to proof art too: a chart inventing candles is a
    fabricated price whether or not it is painted."""
    proof = (RUNTIME_DIR / "proof.js").read_text(encoding="utf-8")
    assert "charts_advance" in proof
    assert "advancing" in proof


def _node_available() -> bool:
    import subprocess
    try:
        return subprocess.run(
            ["node", "--version"], capture_output=True, check=False, timeout=30
        ).returncode == 0
    except (OSError, Exception):
        return False


requires_node = pytest.mark.skipif(not _node_available(), reason="node is unavailable")


def _run_proof_probe(script: str) -> dict:
    import subprocess

    from tradefix_radio.visual.scene import build_scene

    result = subprocess.run(
        ["node", "-e", script],
        input=json.dumps(build_scene().to_json()),
        capture_output=True, text=True, check=False, timeout=300, cwd=str(REPO),
    )
    assert result.stdout.strip(), (
        f"no output\nexit {result.returncode}\nstderr:\n{result.stderr[-3000:]}"
    )
    return json.loads(result.stdout)


@requires_node
def test_the_trader_is_framed_on_every_camera() -> None:
    """The milestone: the trader must be visible from all seven cameras, not one.

    Measured as projected screen coverage, because a draw-call count says nothing about
    whether anything is visible. Before the character shapes were billboarded they lay in
    the world XZ plane and projected edge-on from `CAM_2` — the side profile covered 15 %
    of frame and read as a sliver. The floors below are deliberately loose: `CAM_5` is a
    wide office shot where a small figure is correct, and `CAM_4` is a face close-up where
    a large one is.
    """
    script = """
const { buildRuntime } = require('./tests/renderer/harness.js');
let data = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (c) => { data += c; });
process.stdin.on('end', () => {
  const scene = JSON.parse(data);
  const out = {};
  for (const cam of ['CAM_1','CAM_2','CAM_3','CAM_4','CAM_5','CAM_6','CAM_7']) {
    const built = buildRuntime(scene, { search: '?demo=1&art=proof', camera: cam });
    const { runtime } = built;
    runtime.draw(400);
    const m = runtime.viewProj;
    const proj = (p) => {
      const w = m[3]*p[0]+m[7]*p[1]+m[11]*p[2]+m[15];
      if (w <= 0) return null;
      return [(m[0]*p[0]+m[4]*p[1]+m[8]*p[2]+m[12])/w,
              (m[1]*p[0]+m[5]*p[1]+m[9]*p[2]+m[13])/w];
    };
    let character = 0, everything = 0;
    const orig = runtime._quad.bind(runtime);
    runtime._quad = function (c, size, bx, by, colour, shape, edge, opts) {
      const corners = [[-0.5,-0.5],[0.5,-0.5],[0.5,0.5],[-0.5,0.5]].map(([u,v]) => proj([
        c[0] + bx[0]*u*size[0] + by[0]*v*size[1],
        c[1] + bx[1]*u*size[0] + by[1]*v*size[1],
        c[2] + bx[2]*u*size[0] + by[2]*v*size[1]]));
      if (!corners.some((p) => p === null)) {
        const xs = corners.map((p) => Math.max(-1, Math.min(1, p[0])));
        const ys = corners.map((p) => Math.max(-1, Math.min(1, p[1])));
        const a = (Math.max(...xs)-Math.min(...xs)) * (Math.max(...ys)-Math.min(...ys)) / 4;
        everything += a;
        if ((runtime.primitive || '').startsWith('proof:character')) character += a;
      }
      return orig(c, size, bx, by, colour, shape, edge, opts);
    };
    runtime.draw(420);
    out[cam] = { character, everything, calls: runtime.drawCalls };
  }
  process.stdout.write(JSON.stringify(out));
});
"""
    report = _run_proof_probe(script)

    #: Minimum summed character coverage per camera. Summed rather than unique, so these
    #: are relative figures — the point is that none is near zero.
    floors = {
        "CAM_1": 0.40,   # hero front: he is the subject
        "CAM_2": 0.25,   # side profile: a medium shot
        "CAM_3": 0.40,   # over shoulder
        "CAM_4": 1.00,   # face close-up: the head alone exceeds the frame
        "CAM_5": 0.04,   # wide office: a small figure is correct here
        "CAM_6": 0.20,   # hands and desk
        "CAM_7": 0.25,   # three-quarter hero
    }
    for camera, floor in floors.items():
        coverage = report[camera]["character"]
        assert coverage >= floor, (
            f"{camera}: the trader covers {coverage * 100:.0f}% of frame, "
            f"below the {floor * 100:.0f}% floor — he is not readable from this camera"
        )
        assert report[camera]["calls"] > 100, f"{camera} drew almost nothing"


@requires_node
def test_the_character_is_billboarded_toward_the_camera() -> None:
    """Shapes laid in a fixed world plane vanish at a right angle to it.

    Legitimate here because these shapes are generated rather than a painted front view
    stretched across an angle it was never drawn for. A painted plate gets the honest
    treatment: a camera with no plate says ART VIEW NOT YET AVAILABLE.
    """
    source = (RUNTIME_DIR / "app.js").read_text(encoding="utf-8")
    assert "_billboard(roll)" in source
    proof = (RUNTIME_DIR / "proof.js").read_text(encoding="utf-8")
    assert "r._billboard(" in proof
    # No character primitive may use the fixed world plane.
    start = proof.index("    character(now, lidClose, gazeEase) {")
    end = proof.index("    /* A limb as one quad spanning two world points")
    assert "X, Z," not in proof[start:end], (
        "a character primitive is laid in the world XZ plane; it will project edge-on "
        "from the side cameras"
    )


@requires_node
def test_off_frame_geometry_is_culled() -> None:
    """The skyline spans the room; CAM_1 sees 39.6 degrees of it.

    Measured before culling: 601 of 1191 draw calls were city windows and most were off
    frame — half the frame's cost thrown away.
    """
    script = """
const { buildRuntime } = require('./tests/renderer/harness.js');
let data = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (c) => { data += c; });
process.stdin.on('end', () => {
  const scene = JSON.parse(data);
  const out = {};
  for (const cam of ['CAM_1', 'CAM_4', 'CAM_5']) {
    const { runtime } = buildRuntime(scene, { search: '?demo=1&art=proof', camera: cam });
    runtime.draw(400);
    out[cam] = runtime.drawCalls;
  }
  process.stdout.write(JSON.stringify(out));
});
"""
    calls = _run_proof_probe(script)
    # The narrow lenses must cost less than the wide one, which is the cull working.
    assert calls["CAM_4"] < calls["CAM_5"], (
        f"the 85mm face close-up ({calls['CAM_4']}) costs as much as the 24mm wide shot "
        f"({calls['CAM_5']}); off-frame geometry is not being culled"
    )
    assert calls["CAM_1"] < 1_100, f"CAM_1 draws {calls['CAM_1']} primitives"


@requires_node
def test_proof_mode_moves_every_visible_body_part() -> None:
    """The movements the brief lists must visibly displace geometry, not just log a name.

    Thresholds in millimetres of world travel. `MOUSE` is low because his right hand
    rests on the mouse already — the blockout puts the anchor 30 mm from the hand's rest
    position, so a large number there would mean the anchor was wrong.
    """
    script = """
const { buildRuntime } = require('./tests/renderer/harness.js');
let data = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (c) => { data += c; });
process.stdin.on('end', () => {
  const scene = JSON.parse(data);
  const cases = {
    COFFEE: ['sip', 'ANCHOR_MUG_LIP'], TYPING: ['typing_long', 'ANCHOR_KEYBOARD_HOME_L'],
    NOTE: ['note_write_short', 'ANCHOR_NOTEBOOK'], MOUSE: ['mouse_move', 'ANCHOR_MOUSE'],
    HEAD_TURN: ['small_head_turn', null], HEAD_TILT: ['small_head_tilt', null],
    LEAN_IN: ['lean_forward', null], LEAN_BACK: ['lean_back', null],
    HAND_TO_CHIN: ['hand_to_chin', null], HEADPHONES: ['adjust_left', 'ANCHOR_HP_CUP_L'],
    POSTURE_RESET: ['spine_straighten', null], SHOULDER: ['shoulder_shift', null],
  };
  const out = {};
  for (const [label, [id, anchor]] of Object.entries(cases)) {
    const { runtime } = buildRuntime(scene, { search: '?demo=1&art=proof', camera: 'CAM_1' });
    runtime.draw(0);
    const base = new Map();
    for (const [j, w] of runtime.worlds) base.set(j, { p: w.position.slice(), r: w.rotation });
    runtime.handle([{ v: 1, kind: 'START', seq: 1, action_id: id, category: 'work',
      duration_ms: 1600, blend_in_ms: 200, blend_out_ms: 240, amplitude: 1.0,
      anchor, character_state: 'idle_focus', interruptibility: 'always' }]);
    let translate = 0, rotate = 0;
    for (let t = 50; t <= 2200; t += 25) {
      runtime.draw(t);
      for (const [j, w] of runtime.worlds) {
        const b = base.get(j);
        if (!b) continue;
        translate = Math.max(translate, Math.hypot(
          w.position[0]-b.p[0], w.position[1]-b.p[1], w.position[2]-b.p[2]));
        rotate = Math.max(rotate, Math.abs(w.rotation - b.r) * 180 / Math.PI);
      }
    }
    out[label] = { translate, rotate };
  }
  process.stdout.write(JSON.stringify(out));
});
"""
    report = _run_proof_probe(script)
    floors = {
        "COFFEE": 300, "TYPING": 300, "NOTE": 300, "HEADPHONES": 300,
        "HAND_TO_CHIN": 300, "LEAN_IN": 60, "LEAN_BACK": 60,
        "POSTURE_RESET": 20, "SHOULDER": 8, "MOUSE": 20,
    }
    for label, floor in floors.items():
        travel = report[label]["translate"]
        assert travel >= floor, (
            f"{label} moved {travel:.0f} mm, below the {floor} mm floor — it would not "
            "be visible"
        )
    # The head movements read mostly as rotation, so they are checked on that.
    for label in ("HEAD_TURN", "HEAD_TILT"):
        assert report[label]["rotate"] >= 2.0, (
            f"{label} rotated {report[label]['rotate']:.1f} degrees"
        )


@requires_node
def test_the_music_nod_needs_music() -> None:
    """Subtle by design: the rhythm policy caps nods at 1.1 degrees, and silence means
    no nod at all. Asserted so a future change cannot make him bob to nothing."""
    script = """
const { buildRuntime } = require('./tests/renderer/harness.js');
let data = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (c) => { data += c; });
process.stdin.on('end', () => {
  const scene = JSON.parse(data);
  const run = (bpm) => {
    const { runtime } = buildRuntime(scene, { search: '?demo=1&art=proof', camera: 'CAM_4' });
    if (bpm) {
      runtime.handle([{ v: 1, kind: 'SET', seq: 1, bpm, downbeat_phase: 0,
        beat_subdivision: 1, nod_probability: 0.9, max_nod_degrees: 1.1,
        max_consecutive_beats: 8, mandatory_gap_seconds: 25 }]);
    }
    runtime.draw(0);
    const base = runtime.worlds.get('head').position.slice();
    runtime.handle([{ v: 1, kind: 'START', seq: 1, action_id: 'micro_head_nod',
      category: 'music', duration_ms: 3000, blend_in_ms: 200, blend_out_ms: 240,
      amplitude: 1.0, anchor: null, character_state: 'idle_focus',
      interruptibility: 'always' }]);
    let peak = 0;
    for (let t = 50; t <= 3600; t += 25) {
      runtime.draw(t);
      const p = runtime.worlds.get('head').position;
      peak = Math.max(peak, Math.hypot(p[0]-base[0], p[1]-base[1], p[2]-base[2]));
    }
    return peak;
  };
  process.stdout.write(JSON.stringify({ with_music: run(128), silence: run(null) }));
});
"""
    report = _run_proof_probe(script)
    assert report["with_music"] > 6, (
        f"the nod moved {report['with_music']:.1f} mm with music playing"
    )
    assert report["silence"] < report["with_music"] / 2, (
        "the head nods in silence; the nod is not driven by the music"
    )


@requires_node
def test_a_carried_prop_follows_the_hand_and_returns() -> None:
    """FREE uses the world anchor, ACQUIRED follows the hand, RELEASING returns."""
    script = """
const { buildRuntime } = require('./tests/renderer/harness.js');
let data = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (c) => { data += c; });
process.stdin.on('end', () => {
  const scene = JSON.parse(data);
  const { runtime } = buildRuntime(scene, { search: '?demo=1&art=proof', camera: 'CAM_6' });
  runtime.draw(0);
  const free = runtime.propOffsets.get('MUG') || null;
  let peak = 0;
  for (const [step, anchor] of [['reach_cup','ANCHOR_MUG_BODY'],
                                ['pick_cup','ANCHOR_MUG_LIP'], ['sip', null],
                                ['place_cup','ANCHOR_MUG_RING']]) {
    runtime.handle([{ v: 1, kind: 'START', seq: 1, action_id: step, category: 'caffeine',
      duration_ms: 900, blend_in_ms: 180, blend_out_ms: 200, amplitude: 1.0,
      anchor, character_state: 'caffeine_break', chain_id: 'coffee',
      interruptibility: 'always' }]);
  }
  for (let t = 50; t <= 5000; t += 50) {
    runtime.draw(t);
    const held = runtime.propOffsets.get('MUG');
    if (held) peak = Math.max(peak, Math.hypot(held[0], held[1], held[2]));
  }
  // Long after the chain, the mug must be back on its anchor.
  for (let t = 5000; t <= 9000; t += 100) runtime.draw(t);
  const settled = runtime.propOffsets.get('MUG') || null;
  process.stdout.write(JSON.stringify({ free, peak, settled }));
});
"""
    report = _run_proof_probe(script)
    assert report["free"] is None, "the mug is attached to a hand before anything grabs it"
    assert report["peak"] > 200, (
        f"the mug travelled only {report['peak']:.0f} mm; it is not following the hand"
    )
    assert report["settled"] is None, (
        "the mug never returned to its world anchor after the chain finished"
    )


def _last_json(stdout: str) -> dict:
    """The JSON payload, ignoring anything the library logged to the console.

    `ArtLibrary` logs each plate it loads, which is useful in a browser and noise here.
    """
    for line in reversed(stdout.strip().splitlines()):
        stripped = line.strip()
        if stripped.startswith("{"):
            return json.loads(stripped)
    raise AssertionError(f"no JSON object in output:\n{stdout[-2000:]}")


# ============================================================ the load path


def test_the_missing_art_notice_exists_and_carries_the_counts() -> None:
    """A dark frame must never again be mistakeable for a working visual."""
    html = (RUNTIME_DIR / "index.html").read_text(encoding="utf-8")
    assert "REAL ART NOT IMPORTED" in html
    for element in ("noart", "n-char", "n-env", "n-motion", "n-source", "n-problem"):
        assert f'id="{element}"' in html, element
    assert "0/9" in html and "0/4" in html

    source = (RUNTIME_DIR / "app.js").read_text(encoding="utf-8")
    assert "updateArtNotices" in source
    # Shown in proof and final mode; suppressed over the deliberate grey-box view.
    assert "ART_MODE !== 'blockout'" in source


def test_the_camera_unavailable_notice_exists() -> None:
    """A plate painted for the hero shot must not be stretched across another angle."""
    html = (RUNTIME_DIR / "index.html").read_text(encoding="utf-8")
    assert "ART VIEW NOT YET AVAILABLE" in html
    assert 'id="nocam"' in html
    art_js = (RUNTIME_DIR / "art.js").read_text(encoding="utf-8")
    assert "cameraUnavailable" in art_js


def test_the_notice_clears_itself_when_art_arrives() -> None:
    """Driven from the library, not from a flag somebody has to remember to unset."""
    source = (RUNTIME_DIR / "app.js").read_text(encoding="utf-8")
    assert "runtime.art.counts()" in source
    assert "noart.classList.remove('shown')" in source
    art_js = (RUNTIME_DIR / "art.js").read_text(encoding="utf-8")
    assert "anyImported" in art_js


def test_the_art_library_loads_only_validated_plates() -> None:
    art_js = (RUNTIME_DIR / "art.js").read_text(encoding="utf-8")
    assert "if (!slot.valid || !slot.url)" in art_js
    # And a plate that stops being valid must stop being drawn.
    assert "_release(slot.layer)" in art_js


def test_art_substitution_is_per_layer_and_per_camera() -> None:
    """A partial delivery must compose: painted office, procedural trader."""
    proof = (RUNTIME_DIR / "proof.js").read_text(encoding="utf-8")
    assert "function drawPlate(" in proof
    assert "art.plateFor(layer, runtime.cameraId)" in proof
    # Each of the seven layers falls back independently.
    for layer in ("'city'", "'rear_office'", "'monitors'", "'character'", "'desk'"):
        assert f"drawPlate(runtime, {layer}" in proof


def test_screen_content_is_never_taken_from_a_plate() -> None:
    """A painted chart is a fabricated price. Bezels may be art; candles never are."""
    proof = (RUNTIME_DIR / "proof.js").read_text(encoding="utf-8")
    index = proof.index("drawPlate(runtime, 'monitors'")
    after = proof[index:index + 400]
    assert "art.monitors(now);" in after, (
        "the monitor plate suppresses the live chart draw; screen content must always "
        "be generated"
    )


def _strip_js_comments(source: str) -> str:
    """Code only. The isolation checks below must not match a word in a docstring."""
    import re

    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", without_blocks)


def test_importing_art_requires_no_change_to_the_behaviour_system() -> None:
    """The brief's constraint: no changes to the director, camera director or bridge.

    Asserted on executable code with comments stripped — an earlier version of this test
    failed on its own explanatory prose, which proves nothing about what the code does.
    """
    import ast

    art_py = REPO / "tradefix_radio" / "visual" / "art.py"
    tree = ast.parse(art_py.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    for module in imported:
        assert not module.startswith("tradefix_radio"), (
            f"art.py imports {module}; the art path must not reach into the system it "
            "supplies textures to"
        )

    code = _strip_js_comments((RUNTIME_DIR / "art.js").read_text(encoding="utf-8"))
    for forbidden in ("runtime.handle", "BehaviorDirector", "propOffsets",
                      "runtime.gaze", "_advancePose", "_worldFor"):
        assert forbidden not in code, f"art.js touches {forbidden}"


@requires_node
def test_a_valid_plate_is_loaded_and_used(tmp_path: Path) -> None:
    """End to end: a plate on disk becomes a texture the renderer draws.

    The path that must work the moment approved art arrives. Driven through the real
    `ArtLibrary` with a stub `Image`, because jsdom is not available and the decode is
    the browser's job rather than this test's.
    """
    script = """
const { loadRuntime } = require('./tests/renderer/harness.js');
const loaded = loadRuntime({ search: '?demo=1&art=proof' });
const { ArtLibrary } = loaded.proof_art;

const library = new ArtLibrary(loaded.gl);
const status = {
  any_art_valid: true,
  character: { required_valid: 1, required: 9 },
  environment: { required_valid: 0, required: 4 },
  motion_layers: { valid: 1, required: 9 },
  source_root: '/tmp', import_guide: '/tmp/guide', problems: [], unexpected_files: [],
  slots: [
    { layer: 'head', filename: 'character_head.png', valid: true,
      url: '/art/character/character_head.png', cameras: ['CAM_1', 'CAM_4'],
      pivot: [0.5, 0.9] },
    { layer: 'desk', filename: 'desk_foreground.png', valid: false, url: null,
      cameras: ['CAM_1'] },
  ],
};
library.sync(status);

const out = {
  beforeDecode: library.any,
  counts: library.counts(),
};
// Fire the stubbed image load the harness recorded.
loaded.sandbox.__images[0].onload();
out.afterDecode = library.any;
out.plateOnCam1 = Boolean(library.plateFor('head', 'CAM_1'));
out.plateOnCam6 = Boolean(library.plateFor('head', 'CAM_6'));
out.invalidLayerSkipped = library.plateFor('desk', 'CAM_1') === null;
out.cameraUnavailableCam6 = library.cameraUnavailable('CAM_6');
out.cameraUnavailableCam1 = library.cameraUnavailable('CAM_1');
process.stdout.write(JSON.stringify(out));
"""
    import subprocess

    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True, text=True, check=False, timeout=300, cwd=str(REPO),
    )
    assert result.stdout.strip(), (
        f"no output\nexit {result.returncode}\nstderr:\n{result.stderr[-3000:]}"
    )
    report = _last_json(result.stdout)

    assert report["beforeDecode"] is False, "a plate was used before it decoded"
    assert report["afterDecode"] is True, "the decoded plate was not registered"
    assert report["plateOnCam1"] is True
    assert report["plateOnCam6"] is False, (
        "the head plate was offered to CAM_6, which it was not painted for"
    )
    assert report["invalidLayerSkipped"] is True, "an invalid plate was loaded"
    assert report["cameraUnavailableCam6"] is True, (
        "CAM_6 has no plate but does not report ART VIEW NOT YET AVAILABLE"
    )
    assert report["cameraUnavailableCam1"] is False
    assert report["counts"]["anyImported"] is True
    assert report["counts"]["motion"] == {"valid": 1, "required": 9}


@requires_node
def test_the_notice_is_live_with_nothing_imported() -> None:
    """Today's real state, asserted through the library rather than by reading HTML."""
    script = """
const { loadRuntime } = require('./tests/renderer/harness.js');
const loaded = loadRuntime({ search: '?demo=1&art=proof' });
const library = new loaded.proof_art.ArtLibrary(loaded.gl);
library.sync({
  any_art_valid: false,
  character: { required_valid: 0, required: 9 },
  environment: { required_valid: 0, required: 4 },
  motion_layers: { valid: 0, required: 9 },
  source_root: 'D:/.Music/visual/assets/source', import_guide: 'guide',
  problems: [], unexpected_files: [], slots: [],
});
const counts = library.counts();
process.stdout.write(JSON.stringify({
  any: library.any, counts,
  // With nothing imported every camera is equally valid, so no camera notice.
  cameraUnavailable: library.cameraUnavailable('CAM_6'),
}));
"""
    import subprocess

    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True, text=True, check=False, timeout=300, cwd=str(REPO),
    )
    assert result.stdout.strip(), f"stderr:\n{result.stderr[-3000:]}"
    report = _last_json(result.stdout)

    assert report["any"] is False
    assert report["counts"]["anyImported"] is False
    assert report["counts"]["character"] == {"valid": 0, "required": 9}
    assert report["counts"]["environment"] == {"valid": 0, "required": 4}
    assert report["counts"]["motion"] == {"valid": 0, "required": 9}
    assert report["cameraUnavailable"] is False, (
        "a camera notice fires with no art imported; the missing-art notice covers that"
    )


def test_the_shader_supports_the_proof_shapes() -> None:
    source = (RUNTIME_DIR / "app.js").read_text(encoding="utf-8")
    for shape in ("ROUNDED", "GRADIENT", "GLOW", "TAPER", "TEXTURE"):
        assert f"{shape}:" in source, shape
    # And the texture path exists for when plates arrive.
    assert "u_useTex" in source
    assert "sampler2D u_tex" in source
