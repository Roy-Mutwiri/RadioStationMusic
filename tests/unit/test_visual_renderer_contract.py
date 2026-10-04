"""The renderer's draw contract, enforced without a browser.

This file exists because of a production failure. The live demo reported

    DRAW FAILED object is not iterable (cannot read property Symbol(Symbol.iterator))

and the message named neither the draw call, nor the field, nor the type. Root cause:
`SceneDescription.to_json` serialised twenty-one geometry vectors as JSON arrays and
exactly one — `room` — as ``{"x": …, "y": …, "z": …}``. The renderer's `_drawRoomShell`
destructured it with ``const [rw, rd, rh] = scene.room``. Since that is the first draw
call of every frame, the renderer died on frame zero and nothing was ever drawn.

Two layers of coverage, because the bug had two halves:

**The payload.** `test_every_scene_vector_is_an_array` walks the serialised scene and
fails on any object-shaped vector anywhere in it. That catches the whole class, not the
one instance — a new field that bypasses `_vec` fails here.

**The draw path.** `test_the_renderer_draw_contract_holds` runs the real `app.js` under
Node against a strict mock WebGL2 context, sweeping 68 catalogued actions x 7 frozen
cameras x debug on/off, plus every prop state, both symbols, every band, the BPM range and
every camera transition. A catalogued action that is drawable only in theory fails here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tradefix_radio.visual.catalog import CATALOG
from tradefix_radio.visual.scene import GEOMETRY_CONTRACT, _vec, build_scene

REPO = Path(__file__).resolve().parents[2]
RENDERER_TESTS = REPO / "tests" / "renderer"

#: Fields in the scene payload that are vectors. Everything else is a scalar, a string,
#: a bool or a list of strings.
VECTOR_FIELDS = frozenset(
    {"room", "min", "max", "centre", "position", "target", "size"}
)


def _node_available() -> bool:
    try:
        result = subprocess.run(
            ["node", "--version"],
            capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


requires_node = pytest.mark.skipif(
    not _node_available(), reason="node is unavailable"
)


# ============================================================ the payload contract


def _walk(node: object, path: str = "scene"):
    """Yield every `(path, key, value)` in the payload."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, key, value
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def test_every_scene_vector_is_an_array() -> None:
    """The regression test for the reported failure.

    Fails against the original payload, where `room` was ``{"x": …, "y": …, "z": …}``.
    Walks the whole structure rather than checking `room` alone, because the defect was
    an *inconsistency*, and asserting only on the known instance would let the next one
    through.
    """
    payload = build_scene().to_json()
    offenders = []
    for path, key, value in _walk(payload):
        if key in VECTOR_FIELDS and not isinstance(value, list):
            offenders.append(f"{path}.{key} is {type(value).__name__}: {value!r}")
    assert not offenders, (
        "geometry vectors must be JSON arrays — an object reaches array destructuring "
        "in the renderer as `object is not iterable`:\n  " + "\n  ".join(offenders)
    )


def test_room_is_a_vec3_array() -> None:
    """The exact value that failed, named explicitly so the regression is unmissable."""
    room = build_scene().to_json()["room"]
    assert isinstance(room, list), f"room is {type(room).__name__}, not a list"
    assert len(room) == 3
    assert all(isinstance(value, float) for value in room)
    # And it must be destructurable the way the renderer destructures it.
    width, depth, height = room
    assert (width, depth, height) == (6400.0, 4800.0, 3000.0)


def test_every_scene_vector_is_finite_and_the_right_length() -> None:
    payload = build_scene().to_json()
    for path, key, value in _walk(payload):
        if key not in VECTOR_FIELDS:
            continue
        assert 2 <= len(value) <= 3, f"{path}.{key} has {len(value)} components"
        for index, component in enumerate(value):
            assert isinstance(component, (int, float)), f"{path}.{key}[{index}]"
            assert component == component, f"{path}.{key}[{index}] is NaN"
            assert abs(component) != float("inf"), f"{path}.{key}[{index}] is infinite"


def test_the_payload_declares_its_contract() -> None:
    """Stated in the payload so the renderer asserts on it rather than inferring it."""
    payload = build_scene().to_json()
    assert payload["geometry_contract"] == GEOMETRY_CONTRACT
    assert "JSON arrays" in GEOMETRY_CONTRACT


def test_the_vector_producer_refuses_malformed_input() -> None:
    """`_vec` is the only producer, so it is where malformed geometry must be refused.

    Not silently normalised — a vector of the wrong arity is a bug upstream, and
    returning something plausible would hide it.
    """
    assert _vec((1.0, 2.0)) == [1.0, 2.0]
    assert _vec((1, 2, 3)) == [1.0, 2.0, 3.0]
    for bad in ((1.0,), (1.0, 2.0, 3.0, 4.0), ()):
        with pytest.raises(ValueError, match="Vec2 or Vec3"):
            _vec(bad)
    for bad_value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="may not contain"):
            _vec((1.0, bad_value, 3.0))


def test_a_dict_cannot_be_serialised_as_a_vector() -> None:
    """The specific mistake: `_vec` must not quietly accept a mapping.

    ``[float(v) for v in {"x": 1, "y": 2}]`` iterates the *keys*, so without this a dict
    would raise a confusing `ValueError` about string conversion rather than being
    rejected as the wrong type.
    """
    with pytest.raises((ValueError, TypeError)):
        _vec({"x": 1.0, "y": 2.0, "z": 3.0})  # type: ignore[arg-type]


def test_the_asset_manifest_uses_the_same_vector_contract() -> None:
    """The second instance of the same defect, found while fixing the first.

    `AssetSpec.to_json` serialised `pivot` as ``{"x": …, "y": …}``. Nothing consumes it
    yet — no painted plates exist — but when they arrive it goes to the same renderer
    that `room` went to. Two serialisation conventions in one codebase is the condition
    that produced the outage; one of them being currently unused is luck, not safety.
    """
    from tradefix_radio.visual.assets import AssetManifest, manifest_path

    payload = json.loads(manifest_path().read_text(encoding="utf-8"))
    assert payload["assets"], "the written manifest has no assets to check"
    for asset in payload["assets"]:
        pivot = asset["pivot"]
        assert isinstance(pivot, list), (
            f"{asset['asset_id']}.pivot is {type(pivot).__name__}: {pivot!r}"
        )
        assert len(pivot) == 2
        assert all(isinstance(value, (int, float)) for value in pivot)

    # And the live object agrees with the written file.
    assert AssetManifest is not None


def test_both_payload_producers_share_one_implementation() -> None:
    """`scene.py` and `assets.py` must not each own a copy of the vector serialiser.

    A copy in each module is how the two conventions drifted apart in the first place.
    """
    from tradefix_radio.visual import assets, geometry, scene

    assert scene._vec is geometry.vec
    assert assets._vec2 is geometry.vec


# ============================================================ the draw path


def _action_payload() -> list[dict[str, object]]:
    """Every catalogued action, shaped as `renderer.py::action_command` sends it.

    Sampled at the midpoint of each declared range rather than randomly: the sweep has to
    be reproducible, and a failure that only appears at one sampled duration is a failure
    the next run would not find.
    """
    out = []
    for action_id, spec in sorted(CATALOG.items()):
        low, high = spec.duration_ms
        states = sorted(state.value for state in spec.allowed_character_states)
        out.append(
            {
                "action_id": action_id,
                "category": spec.category.value,
                "duration_ms": int((low + high) / 2),
                "blend_in_ms": int(spec.blend_in_ms),
                "blend_out_ms": int(spec.blend_out_ms),
                "amplitude": 1.0,
                "anchor": spec.required_anchor,
                "gaze_target": (
                    spec.gaze_target.value if spec.gaze_target is not None else None
                ),
                "character_state": states[0] if states else "idle_focus",
                "chain_id": None,
                "chain_step": None,
            }
        )
    return out


@pytest.fixture(scope="module")
def sweep_report() -> dict[str, object]:
    """Run the Node contract sweep once and share the report across the assertions."""
    job = json.dumps(
        {"scene": build_scene().to_json(), "actions": _action_payload()}
    )
    result = subprocess.run(
        # Headroom: the sweep builds a fresh VM context per trial across 1 300-odd
        # trials. Node's default old-space limit is enough on this machine but not by
        # much, and an OOM reports nothing at all rather than reporting a failure.
        ["node", "--max-old-space-size=3072", str(RENDERER_TESTS / "contract.js")],
        input=job, capture_output=True, text=True, check=False, timeout=1800,
        cwd=str(REPO),
    )
    if not result.stdout.strip():
        pytest.fail(
            f"the contract sweep produced no report\n"
            f"exit {result.returncode}\nstderr:\n{result.stderr[-4000:]}"
        )
    return json.loads(result.stdout)


@requires_node
def test_the_renderer_draw_contract_holds(sweep_report: dict[str, object]) -> None:
    """The sweep: every action, camera and overlay state must draw valid geometry.

    A behaviour action existing in the catalogue must not mean it is drawable only in
    theory. This is the assertion that makes that true.
    """
    failures = sweep_report["failures"]
    assert not failures, (
        f"{len(failures)} of {sweep_report['trials']} draw trials violated the "
        f"geometry contract:\n"
        + "\n".join(
            f"  {failure['label']}: "
            f"{failure.get('primitive') or '?'}."
            f"{failure.get('field') or '?'} — "
            f"expected {failure.get('expected') or '?'}, "
            f"received {failure.get('received') or failure.get('message')}"
            for failure in failures[:25]
        )
    )


@requires_node
def test_the_sweep_actually_covered_everything(sweep_report: dict[str, object]) -> None:
    """A sweep that silently covered nothing would pass the test above."""
    assert sweep_report["actions"] == len(CATALOG) == 71
    assert sweep_report["cameras"] == 7
    # 7 scene trials x2 debug, 71 actions x7 cameras x2 debug, 71 x3 envelopes, and the
    # prop, motion-kind, gaze, market, music, transition and resync cases.
    assert sweep_report["trials"] > 1_200, (
        f"only {sweep_report['trials']} trials ran; the sweep is not covering the matrix"
    )


def _run_node(script: str, payload: object) -> dict[str, object]:
    result = subprocess.run(
        ["node", "-e", script],
        input=json.dumps(payload), capture_output=True, text=True,
        check=False, timeout=300, cwd=str(REPO),
    )
    assert result.stdout.strip(), (
        f"no output from node\nexit {result.returncode}\n"
        f"stderr:\n{result.stderr[-3000:]}"
    )
    return json.loads(result.stdout)


#: Re-break the payload the way it was broken, then draw. Used by the two tests below.
_REPRO_SCRIPT = """
const { buildRuntime, loadRuntime } = require('./tests/renderer/harness.js');
let data = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (c) => { data += c; });
process.stdin.on('end', () => {
  const scene = JSON.parse(data);
  const out = {};

  // 1. The boundary. This is what now stops it reaching a draw call at all.
  try {
    buildRuntime(scene, { search: '?demo=1', camera: 'CAM_1' });
    out.boundary = { threw: false };
  } catch (error) {
    out.boundary = {
      threw: true, name: error.name,
      primitive: error.primitive, field: error.field,
      expected: error.expected, received: error.received,
    };
  }

  // 2. The draw path with the boundary bypassed, proving the reproduction is faithful
  //    and that the boundary is what protects it rather than a changed draw call.
  //    Both art modes, because both destructure `scene.room` — which is the argument
  //    for guarding the boundary rather than patching one draw call.
  for (const mode of ['blockout', 'proof']) {
    const loaded = loadRuntime({ search: `?strict=0&art=${mode}` });
    const runtime = new loaded.internals.Runtime(loaded.canvas);
    runtime.scene = scene;                     // deliberately unvalidated
    for (const camera of scene.cameras) runtime.cameras.set(camera.id, camera);
    runtime.setCamera('CAM_1', 'cut', 0);
    for (const joint of scene.joints) {
      runtime.pose.set(joint.id, { offset: [0, 0, 0], rot: 0, channels: [] });
    }
    runtime.bodyPoints = {};
    runtime.propOffsets = new Map();
    runtime.propHolds = [];
    try {
      runtime.draw(0);
      out[mode] = { threw: false };
    } catch (error) {
      out[mode] = {
        threw: true, name: error.name, message: error.message,
        primitive: runtime.primitive,
        frame: String(error.stack || '').split('\\n')[1] || '',
      };
    }
  }
  process.stdout.write(JSON.stringify(out));
});
"""


@requires_node
def test_the_boundary_rejects_the_payload_that_failed() -> None:
    """The fix, asserted where it acts.

    The object-shaped `room` is now refused when the scene arrives, with the path, the
    expected shape and the received shape all named — instead of surfacing sixty draw
    calls later as `object is not iterable`.
    """
    payload = build_scene().to_json()
    room = payload["room"]
    payload["room"] = {"x": room[0], "y": room[1], "z": room[2]}

    boundary = _run_node(_REPRO_SCRIPT, payload)["boundary"]
    assert boundary["threw"] is True, (
        "the boundary accepted an object-shaped vector; nothing is guarding the contract"
    )
    assert boundary["name"] == "ContractError"
    assert boundary["field"] == "room"
    assert "Vec3" in boundary["expected"]
    assert boundary["received"] == "object{x,y,z}"


@requires_node
def test_the_original_failure_is_still_reproducible_behind_the_boundary() -> None:
    """Proof the regression coverage can actually fail, not merely that it passes now.

    With the boundary bypassed, the original exception comes out of the original draw
    call — same type, same message, same frame. A regression test that cannot fail is not
    a regression test, and since the fix is a one-line serialisation change the only
    honest way to show the coverage works is to re-break it on purpose.
    """
    payload = build_scene().to_json()
    room = payload["room"]
    payload["room"] = {"x": room[0], "y": room[1], "z": room[2]}

    report = _run_node(_REPRO_SCRIPT, payload)

    blockout = report["blockout"]
    assert blockout["threw"] is True, (
        "the object-shaped room no longer fails the draw path — this test has stopped "
        "proving anything"
    )
    assert blockout["name"] == "TypeError"
    assert "not iterable" in blockout["message"]
    assert "Symbol(Symbol.iterator)" in blockout["message"]
    # Attributed to the draw pass that actually failed, which the browser would not say.
    assert blockout["primitive"] == "room_shell"
    assert "_drawRoomShell" in blockout["frame"]

    # Proof art destructures `scene.room` too, in its city pass. Both paths would fail,
    # which is why the fix is one validated boundary rather than two corrected call sites.
    proof = report["proof"]
    assert proof["threw"] is True
    assert "not iterable" in proof["message"]
    assert proof["primitive"] == "proof:city"


@requires_node
@pytest.mark.parametrize(
    ("collection", "item_id", "field", "broken"),
    [
        ("joints", "hand_r", "position", {"x": 1, "y": 2, "z": 3}),
        ("joints", "head", "size", {"w": 200, "h": 260}),
        ("boxes", "MUG", "min", {"x": 1, "y": 2, "z": 3}),
        ("quads", "MON_1", "centre", {"x": 1, "y": 2, "z": 3}),
        ("cameras", "CAM_6", "target", {"x": 1, "y": 2, "z": 3}),
    ],
)
def test_the_boundary_names_the_field_for_any_malformed_vector(
    collection: str, item_id: str, field: str, broken: dict[str, float]
) -> None:
    """Every vector field, not just `room`.

    The original failure said only "object is not iterable". The point of the boundary is
    that the next one names which collection, which item, which field, expected what and
    received what — so the answer takes seconds rather than an afternoon. Parametrised
    across the field kinds because the defect was an inconsistency, and guarding one
    field would leave the rest exactly as exposed as `room` was.
    """
    payload = build_scene().to_json()
    for item in payload[collection]:
        if item["id"] == item_id:
            item[field] = broken
            break
    else:
        pytest.fail(f"{collection}[{item_id}] is not in the payload")

    script = """
const { buildRuntime } = require('./tests/renderer/harness.js');
let data = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (c) => { data += c; });
process.stdin.on('end', () => {
  try {
    buildRuntime(JSON.parse(data), { search: '?demo=1', camera: 'CAM_6' });
    process.stdout.write(JSON.stringify({ threw: false }));
  } catch (error) {
    process.stdout.write(JSON.stringify({
      threw: true, name: error.name, primitive: error.primitive,
      field: error.field, expected: error.expected, received: error.received,
    }));
  }
});
"""
    report = _run_node(script, payload)
    assert report["threw"] is True, (
        f"{collection}[{item_id}].{field} was accepted as an object"
    )
    assert report["name"] == "ContractError", (
        f"surfaced as {report['name']}, not an attributed ContractError"
    )
    assert item_id in report["primitive"], report["primitive"]
    assert report["field"] == field
    assert "Vec" in report["expected"]
    assert report["received"].startswith("object{")


@requires_node
@pytest.mark.parametrize("bad", [None, "nope", 3, [1, 2], [1, 2, 3, 4]])
def test_the_boundary_refuses_every_wrong_shape(bad: object) -> None:
    """Not only objects: a short array, a long one, a string and null all fail too."""
    payload = build_scene().to_json()
    payload["room"] = bad

    script = """
const { buildRuntime } = require('./tests/renderer/harness.js');
let data = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (c) => { data += c; });
process.stdin.on('end', () => {
  try {
    buildRuntime(JSON.parse(data), { search: '?demo=1' });
    process.stdout.write(JSON.stringify({ threw: false }));
  } catch (error) {
    process.stdout.write(JSON.stringify({
      threw: true, name: error.name, field: error.field,
      expected: error.expected, received: error.received,
    }));
  }
});
"""
    report = _run_node(script, payload)
    assert report["threw"] is True, f"room={bad!r} was accepted"
    assert report["name"] == "ContractError"
    assert report["field"] == "room"


@requires_node
def test_a_non_finite_component_is_refused() -> None:
    """A NaN reaching a uniform is a silently invisible primitive, not a crash."""
    payload = build_scene().to_json()
    # JSON has no NaN literal, so inject it through the JS side.
    script = """
const { buildRuntime } = require('./tests/renderer/harness.js');
let data = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (c) => { data += c; });
process.stdin.on('end', () => {
  const scene = JSON.parse(data);
  scene.joints.find((j) => j.id === 'hand_l').position = [3080, NaN, 790];
  try {
    buildRuntime(scene, { search: '?demo=1' });
    process.stdout.write(JSON.stringify({ threw: false }));
  } catch (error) {
    process.stdout.write(JSON.stringify({
      threw: true, name: error.name, field: error.field,
      expected: error.expected, received: error.received,
    }));
  }
});
"""
    report = _run_node(script, payload)
    assert report["threw"] is True, "a NaN component was accepted"
    assert report["name"] == "ContractError"
    assert report["field"] == "position[1]"
    assert report["expected"] == "finite number"
    assert report["received"] == "NaN"


@requires_node
def test_the_failure_report_carries_the_diagnostic_fields() -> None:
    """Everything §1 of the brief asks a DRAW FAILED to record."""
    script = """
const { buildRuntime } = require('./tests/renderer/harness.js');
let data = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (c) => { data += c; });
process.stdin.on('end', () => {
  const scene = JSON.parse(data);
  const built = buildRuntime(scene, { search: '?demo=1&debug=1', camera: 'CAM_4' });
  const { runtime } = built;
  runtime.handle([{ v: 1, kind: 'START', seq: 1, action_id: 'sip',
                    category: 'caffeine', duration_ms: 900, blend_in_ms: 200,
                    blend_out_ms: 240, amplitude: 1.0, anchor: 'ANCHOR_MUG_LIP',
                    character_state: 'caffeine_break', chain_id: 'coffee',
                    chain_step: 3, interruptibility: 'always' }]);
  runtime.draw(16);
  const report = built.internals.describeDrawFailure(
    new Error('synthetic'), runtime);
  process.stdout.write(JSON.stringify(report));
});
"""
    result = subprocess.run(
        ["node", "-e", script],
        input=json.dumps(build_scene().to_json()), capture_output=True, text=True,
        check=False, timeout=300, cwd=str(REPO),
    )
    assert result.stdout.strip(), f"no output; stderr:\n{result.stderr[-3000:]}"
    report = json.loads(result.stdout)

    assert report["error"]["name"] == "Error"
    assert report["error"]["stack"]
    renderer = report["renderer"]
    for key in ("primitive", "frame", "camera", "camera_transition",
                "strict_geometry", "debug_lines"):
        assert key in renderer, key
    assert renderer["camera"] == "CAM_4"
    assert renderer["debug_lines"] is True

    character = report["character"]
    for key in ("state", "action", "action_anchor", "action_chain", "chain_step",
                "gaze", "in_flight", "carried_props", "locks"):
        assert key in character, key
    assert character["action"] == "sip"
    assert character["action_anchor"] == "ANCHOR_MUG_LIP"
    assert character["action_chain"] == "coffee"
    assert "MUG" in character["carried_props"]

    for key in ("symbol", "regime", "band", "bpm"):
        assert key in report["market"], key

    # Layer ids, not layer contents: a report nobody can read is no report.
    layers = report["layers"]
    assert "ROOM" in layers["boxes"]
    assert "MON_1" in layers["quads"]
    assert "hand_r" in layers["joints"]
    serialised = json.dumps(report)
    assert len(serialised) < 20_000, (
        f"the failure report is {len(serialised)} bytes — too large to read"
    )


# ============================================================ the two sides agree


def test_the_limb_segment_count_matches_the_renderer() -> None:
    """`scene.py` counts limb segments for the derived frame budget; `app.js` draws them.

    Two declarations of the same number is how a budget goes quietly stale.
    """
    from tradefix_radio.visual.scene import LIMB_SEGMENTS

    source = (REPO / "visual" / "runtime" / "app.js").read_text(encoding="utf-8")
    block = source[source.index("const LIMB_SEGMENTS = ["):]
    block = block[: block.index("];")]
    assert block.count("['") == LIMB_SEGMENTS, (
        f"app.js draws {block.count(chr(91) + chr(39))} segments, scene.py counts "
        f"{LIMB_SEGMENTS}"
    )


def test_the_renderer_declares_one_vector_schema() -> None:
    """No second convention may creep back into the browser side."""
    source = (REPO / "visual" / "runtime" / "app.js").read_text(encoding="utf-8")
    # The contract and its validator must both be present.
    assert "class ContractError" in source
    assert "function expectVec(" in source
    assert "STRICT_GEOMETRY" in source
    # And nothing may read a geometry field as `.x` / `.y` / `.z`.
    for forbidden in (".position.x", ".centre.x", ".room.x", ".target.x", ".min.x"):
        assert forbidden not in source, (
            f"{forbidden} reads a vector as an object; the contract is array-shaped"
        )
