/*
 * TRADE FIX VISUAL — placeholder runtime.
 *
 * Verification, not beauty. What this page exists to prove:
 *
 *   - the seven frozen camera transforms produce the compositions CAMERA_PLAN.md claims
 *   - layer ordering and depth sorting work
 *   - parallax derived from blockout depth reads as a camera rather than a zoom
 *   - the state bridge reaches the browser
 *   - action and gaze commands arrive and are interpolated, not snapped
 *   - transform parenting composes down a joint chain
 *   - the whole thing runs in an OBS browser source at a measurable cost
 *
 * Everything drawn is derived from the frozen blockout in millimetres and projected by a
 * real perspective matrix. Nothing here is art, and the PLACEHOLDER stamp is unmissable
 * on purpose: this page must never be mistaken for the product.
 *
 * WebGL2 with one program and an SDF fragment shader. Rect and ellipse are the same quad
 * with a shape flag, so the placeholder character needs no textures at all — which keeps
 * the VRAM cost of the verification harness near zero, and the whole point is that the
 * renderer must not starve ACE-Step.
 */
'use strict';

// ============================================================ constants

// VERSION for cache verification - update when debugging render issues
const APP_VERSION = '2026-10-04-room-diagnostic';
console.log(`[VISUAL] app.js version: ${APP_VERSION}`);

const TARGET_W = 1920;
const TARGET_H = 1080;

/* Frame cap. 30 by default because the brief defers the 30-vs-60 decision to a
 * measurement, and a stable 30 may beat an unstable 60. `?fps=60` overrides for the
 * benchmark. */
/* `location` is absent under Node, where this file is loaded by the renderer-contract
 * tests. Guarded rather than assumed so the draw path can be exercised without a
 * browser — which is the only way to cover 68 actions across 7 cameras. */
const params = new URLSearchParams(
  typeof location === 'undefined' ? '' : location.search);
const FPS_CAP = Math.max(1, Math.min(120, Number(params.get('fps')) || 30));
/* Toggleable at runtime from the control panel, so these are the initial values rather
 * than constants. A broadcast source sets them once in its URL and never touches them. */
const view = {
  hud: params.get('hud') !== '0',
  debug: params.get('debug') === '1',
  /* The control panel is development only and must never appear on a broadcast source,
   * so it is opt-in via `?demo=1` rather than opt-out. */
  demo: params.get('demo') === '1',
};

/* Per-draw-call geometry validation. On in development, off for a broadcast source.
 *
 * Defaults to on whenever the control panel is up, and to on under Node so the contract
 * tests always run strict. `?strict=0` forces it off for a clean GPU measurement, since
 * it costs a type check per uniform and there are 266 draw calls a frame. */
const STRICT_GEOMETRY = params.get('strict') === '0'
  ? false
  : (params.get('strict') === '1' || view.demo || typeof document === 'undefined');

/* Art mode.
 *
 *   proof     — TEMP_PROOF procedural office and trader. The default, because the
 *               blockout view is near-black ink on near-black ink and reads as an empty
 *               rectangle to anyone who is not reading the HUD.
 *   blockout   — the grey-box geometry view. Kept, not replaced: it is what the geometry
 *                freeze suite verifies against, and it is the right view for checking a
 *                camera transform rather than a composition.
 *   final      — painted plates. Selectable, but falls back per layer to proof for any
 *                plate that has not been imported.
 */
const ART_MODES = ['proof', 'blockout', 'final'];
const ART_MODE = ART_MODES.includes(params.get('art')) ? params.get('art') : 'proof';

/* High-contrast debug mode: `?contrast=debug`
 *
 * Renders scene elements with intentionally obvious, distinguishable colors to verify
 * geometry, projection, and visibility. If the scene is STILL blank in this mode, the
 * issue is projection/viewport/depth, not the palette. */
const CONTRAST_MODE = params.get('contrast') === 'debug' ? 'debug' : 'normal';

/* RENDER PIPELINE DIAGNOSTIC MODE: `?render_test=<mode>`
 *
 * Progressive pipeline tests to isolate where visibility breaks:
 *   dom       - show DOM overlay only (no WebGL)
 *   canvas    - show canvas CSS background (lime green, no GL clear)
 *   clear     - GL clear to magenta only (no geometry)
 *   triangle  - magenta clear + yellow triangle (no scene)
 *   room      - room shell only with bright colors
 *   desk      - room + desk
 *   character - room + desk + character
 *   full      - complete scene
 */
const RENDER_TEST = params.get('render_test') || null;
const RENDER_TEST_MODES = ['dom', 'canvas', 'clear', 'triangle', 'room', 'desk', 'character', 'full'];

/* Easing curves. Every visible motion uses one — linear motion is the robotic tell the
 * brief rules out, and instant head turns and teleporting hands are the same defect at
 * different amplitudes. */
const Ease = {
  inOut:   (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2),
  /* Fast attention: most of the travel early, then settles. Gaze and reactions. */
  attention: (t) => 1 - Math.pow(1 - t, 4),
  /* Slow settle: eases out of a long hold. Posture and lean. */
  settle:  (t) => 1 - Math.pow(1 - t, 2.2),
  /* Micro drift: a gentle sine, for sub-perceptual idle motion. */
  drift:   (t) => 0.5 - 0.5 * Math.cos(Math.PI * 2 * t),
  linear:  (t) => t,
};

// ============================================================ small maths

const mat4 = {
  identity: () => new Float32Array([1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]),
  multiply(a, b) {
    const out = new Float32Array(16);
    for (let row = 0; row < 4; row++) {
      for (let col = 0; col < 4; col++) {
        let sum = 0;
        for (let k = 0; k < 4; k++) sum += a[k * 4 + row] * b[col * 4 + k];
        out[col * 4 + row] = sum;
      }
    }
    return out;
  },
  perspective(vfovDeg, aspect, near, far) {
    const f = 1 / Math.tan((vfovDeg * Math.PI / 180) / 2);
    const nf = 1 / (near - far);
    return new Float32Array([
      f / aspect, 0, 0, 0,
      0, f, 0, 0,
      0, 0, (far + near) * nf, -1,
      0, 0, 2 * far * near * nf, 0,
    ]);
  },
  /* Right-handed look-at. The blockout is right-handed with +Z up, so the world up
   * vector is (0,0,1) — using (0,1,0) here would roll every camera onto its side, which
   * is the classic way a correct camera position still produces a wrong picture. */
  lookAt(eye, target) {
    const sub = (a, b) => [a[0]-b[0], a[1]-b[1], a[2]-b[2]];
    const norm = (v) => { const l = Math.hypot(...v) || 1; return [v[0]/l, v[1]/l, v[2]/l]; };
    const cross = (a, b) => [
      a[1]*b[2] - a[2]*b[1], a[2]*b[0] - a[0]*b[2], a[0]*b[1] - a[1]*b[0],
    ];
    const dot = (a, b) => a[0]*b[0] + a[1]*b[1] + a[2]*b[2];

    const z = norm(sub(eye, target));
    let up = [0, 0, 1];
    if (Math.abs(dot(z, up)) > 0.999) up = [0, 1, 0]; // degenerate: looking straight up
    const x = norm(cross(up, z));
    const y = cross(z, x);
    return new Float32Array([
      x[0], y[0], z[0], 0,
      x[1], y[1], z[1], 0,
      x[2], y[2], z[2], 0,
      -dot(x, eye), -dot(y, eye), -dot(z, eye), 1,
    ]);
  },
};

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const lerp = (a, b, t) => a + (b - a) * t;

// ============================================================ the geometry contract
//
// ONE schema for every vector that crosses the Python -> WebSocket/HTTP -> browser
// boundary, and for every vector built on this side:
//
//     Vec2 = [number, number]           Vec3 = [number, number, number]
//
// A plain JSON array. Never `{x, y, z}`. This is not a style preference — it is the bug
// that produced `DRAW FAILED object is not iterable`. The scene payload serialised
// twenty-odd vectors as arrays and exactly one (`room`) as an object, and the one object
// reached an array destructuring. `scene.py::_vec` is now the single producer, so there is
// no second path that could disagree again.
//
// WebGL makes the failure mode worse than a plain TypeError: `uniform3fv` converts its
// argument to a WebIDL `sequence<GLfloat>`, so a plain object throws
// "object is not iterable (cannot read property Symbol(Symbol.iterator))" from inside the
// GL call, naming neither the uniform nor the field. Hence `expectVec` below, which
// checks on *our* side of the call where the names are still known.

class ContractError extends Error {
  constructor({ primitive, field, expected, received, value }) {
    super(`${primitive}.${field}: expected ${expected}, received ${received}`);
    this.name = 'ContractError';
    this.primitive = primitive;
    this.field = field;
    this.expected = expected;
    this.received = received;
    this.value = value;
  }
}

/* What a value actually is, said usefully. `typeof` reports "object" for an array, for
 * `null` and for `{x, y}` alike, which is precisely the distinction that matters here. */
function describeType(value) {
  if (value === null) return 'null';
  if (value === undefined) return 'undefined';
  if (Array.isArray(value)) return `array(${value.length})`;
  if (value instanceof Float32Array) return `Float32Array(${value.length})`;
  if (typeof value === 'object') {
    const keys = Object.keys(value).slice(0, 4).join(',');
    return `object{${keys}}`;
  }
  return typeof value;
}

/* Validate a vector at the geometry boundary.
 *
 * Enabled in development (`?strict=1`, on by default when the control panel is up) and
 * off in a broadcast source, because this runs per draw call — 266 of them a frame — and
 * a 24/7 stream should not pay for an assertion that has already been proven by the
 * contract tests. The tests run with it ON, which is where it earns its keep. */
function expectVec(value, length, primitive, field) {
  if (!STRICT_GEOMETRY) return value;
  if (!Array.isArray(value) && !(value instanceof Float32Array)) {
    throw new ContractError({
      primitive, field,
      expected: `Vec${length} as [${Array(length).fill('number').join(', ')}]`,
      received: describeType(value),
      value,
    });
  }
  if (value.length !== length) {
    throw new ContractError({
      primitive, field, expected: `Vec${length}`,
      received: `array(${value.length})`, value,
    });
  }
  for (let i = 0; i < length; i++) {
    if (!Number.isFinite(value[i])) {
      throw new ContractError({
        primitive, field: `${field}[${i}]`, expected: 'finite number',
        received: String(value[i]), value,
      });
    }
  }
  return value;
}

/* The scene payload's vector fields, by the shape each must have.
 *
 * Checked ONCE when the scene arrives rather than per draw call. A malformed vector in
 * the payload surfaces far from the boundary otherwise: a joint position arriving as
 * `{x, y, z}` fails inside `_worldFor` as "position.slice is not a function", which names
 * neither the joint nor the field nor where it came from. */
const SCENE_VECTORS = [
  { path: 'room', length: 3 },
  { collection: 'boxes', field: 'min', length: 3 },
  { collection: 'boxes', field: 'max', length: 3 },
  { collection: 'quads', field: 'centre', length: 3 },
  { collection: 'joints', field: 'position', length: 3 },
  { collection: 'joints', field: 'size', length: 2 },
  { collection: 'cameras', field: 'position', length: 3 },
  { collection: 'cameras', field: 'target', length: 3 },
];

/**
 * Validate the scene payload against the geometry contract.
 *
 * Throws a `ContractError` naming the exact path on the first violation. Does NOT
 * normalise: a malformed payload is a boundary bug on the Python side, and quietly
 * coercing `{x, y, z}` into `[x, y, z]` here would leave two conventions alive and move
 * the next failure somewhere harder to find.
 *
 * Runs regardless of `STRICT_GEOMETRY`: it is one pass over a few hundred numbers at
 * startup, and the cost of getting this wrong is the whole picture.
 */
function validateScene(scene) {
  if (!scene || typeof scene !== 'object') {
    throw new ContractError({
      primitive: 'scene', field: '(root)', expected: 'an object',
      received: describeType(scene), value: scene,
    });
  }
  for (const rule of SCENE_VECTORS) {
    if (rule.path) {
      expectVecAlways(scene[rule.path], rule.length, 'scene', rule.path);
      continue;
    }
    const items = scene[rule.collection];
    if (!Array.isArray(items)) {
      throw new ContractError({
        primitive: 'scene', field: rule.collection,
        expected: 'an array', received: describeType(items), value: items,
      });
    }
    for (const item of items) {
      expectVecAlways(
        item[rule.field], rule.length,
        `${rule.collection}[${item.id}]`, rule.field);
    }
  }
  for (const [name, point] of Object.entries(scene.anchors || {})) {
    expectVecAlways(point, 3, 'anchors', name);
  }
  for (const [name, point] of Object.entries(scene.gaze_targets || {})) {
    expectVecAlways(point, 3, 'gaze_targets', name);
  }
  return scene;
}

/* `expectVec` honours `STRICT_GEOMETRY`; the boundary check never skips. */
function expectVecAlways(value, length, primitive, field) {
  if (!Array.isArray(value) && !(value instanceof Float32Array)) {
    throw new ContractError({
      primitive, field,
      expected: `Vec${length} as [${Array(length).fill('number').join(', ')}]`,
      received: describeType(value), value,
    });
  }
  if (value.length !== length) {
    throw new ContractError({
      primitive, field, expected: `Vec${length}`,
      received: `array(${value.length})`, value,
    });
  }
  for (let i = 0; i < length; i++) {
    if (!Number.isFinite(value[i])) {
      throw new ContractError({
        primitive, field: `${field}[${i}]`, expected: 'finite number',
        received: String(value[i]), value,
      });
    }
  }
  return value;
}

// ============================================================ the motion map
//
// How a BehaviorDirector action becomes visible movement.
//
// This is the part that makes the demo a demo rather than a text log. Every schedulable
// action gets a placeholder representation here: not an animation, but a declaration of
// which joints move and how far, played through the command's OWN blend_in/duration/
// blend_out so the timing a viewer sees is the timing the director chose.
//
// Four motion kinds, which between them cover the catalogue:
//
//   reach  ease a joint to a frozen V1 anchor, hold, ease back to rest
//   pose   offset and/or rotate joints, hold, return
//   osc    oscillate on an axis for the action's duration (typing, scrolling, rhythm)
//   prop   attach a desk prop to a joint so it travels with the hand (the mug)
//
// Offsets are millimetres in blockout space, rotations radians. Nothing here invents a
// position: anything the character touches resolves through `scene.anchors`, which is
// frozen V1 geometry. A hand that reached the "about right" place would make the demo
// prove nothing about whether the anchors are reachable.

const R = (joint, anchor, extra) => ({ kind: 'reach', joint, anchor, ...extra });
const P = (joints, extra) => ({ kind: 'pose', joints, ...extra });
const O = (joint, extra) => ({ kind: 'osc', joint, ...extra });

/* Shorthand for the common upper-body poses, so the table below reads as intent. */
const LEAN = (amount) => P({
  torso: { offset: [0, -58 * amount, 6 * amount], rot: 0.05 * amount },
  neck:  { offset: [0, -26 * amount, 0] },
  head:  { offset: [0, -34 * amount, -10 * amount] },
}, { ease: 'settle' });

const HEAD_TURN = (amount) => P({
  head: { offset: [34 * amount, 0, 0], rot: 0.16 * amount },
  neck: { offset: [10 * amount, 0, 0] },
}, { ease: 'attention' });

const HEAD_TILT = (amount) => P({
  head: { offset: [12 * amount, 0, -6 * Math.abs(amount)], rot: 0.1 * amount },
}, { ease: 'inOut' });

const SHOULDERS = (amount) => P({
  shoulder_l: { offset: [0, 0, 18 * amount], rot: 0.07 * amount },
  shoulder_r: { offset: [0, 0, 18 * amount], rot: -0.07 * amount },
  torso: { offset: [0, 0, 7 * amount] },
}, { ease: 'settle' });

const MOTION = {
  // ---- work: the keyboard and the mouse -------------------------------------
  typing_short:  [R('hand_l', 'ANCHOR_KEYBOARD_HOME_L'), R('hand_r', 'ANCHOR_KEYBOARD_HOME_R'),
                  O('hand_l', { axis: 2, amp: 13, hz: 3.6 }),
                  O('hand_r', { axis: 2, amp: 13, hz: 3.6, phase: 0.5 }),
                  P({ head: { offset: [0, 0, -7] } }, { ease: 'inOut' })],
  typing_medium: 'typing_short',
  typing_long:   'typing_short',
  hotkey:        [R('hand_l', 'ANCHOR_KEYBOARD_HOME_L'),
                  O('hand_l', { axis: 2, amp: 16, hz: 5.0 })],
  mouse_move:    [R('hand_r', 'ANCHOR_MOUSE'),
                  O('hand_r', { axis: 0, amp: 26, hz: 0.9 })],
  mouse_click:   [R('hand_r', 'ANCHOR_MOUSE'), O('hand_r', { axis: 2, amp: 9, hz: 2.2 })],
  mouse_double_click: [R('hand_r', 'ANCHOR_MOUSE'),
                       O('hand_r', { axis: 2, amp: 10, hz: 5.5 })],
  mouse_scroll:  [R('hand_r', 'ANCHOR_MOUSE'), O('hand_r', { axis: 1, amp: 14, hz: 2.8 })],
  rapid_mouse:   [R('hand_r', 'ANCHOR_MOUSE'),
                  O('hand_r', { axis: 0, amp: 42, hz: 2.6 }),
                  LEAN(0.5)],
  chart_pan:     [R('hand_r', 'ANCHOR_MOUSE'), O('hand_r', { axis: 0, amp: 58, hz: 0.5 })],

  // ---- work: the notebook ---------------------------------------------------
  reach_pen:     [R('hand_l', 'ANCHOR_PEN', { ease: 'attention' })],
  acquire_pen:   [R('hand_l', 'ANCHOR_PEN'), P({ head: { offset: [-16, 0, -22] } })],
  note_write_short: [R('hand_l', 'ANCHOR_NOTEBOOK'),
                     O('hand_l', { axis: 0, amp: 22, hz: 2.4 }),
                     O('hand_l', { axis: 1, amp: 7, hz: 0.8 }),
                     P({ head: { offset: [-22, 0, -40], rot: -0.12 },
                         neck: { offset: [-8, 0, -10] } }, { ease: 'settle' })],
  note_write_long: 'note_write_short',
  quick_note:    'note_write_short',
  return_pen:    [R('hand_l', 'ANCHOR_PEN', { ease: 'settle' })],
  note_gaze_down: [P({ head: { offset: [-18, 0, -34], rot: -0.1 } }, { ease: 'attention' })],
  note_gaze_up:   [P({ head: { offset: [0, 0, 10] } }, { ease: 'attention' })],

  // ---- work: posture and attention -----------------------------------------
  lean_forward:  [LEAN(1.0)],
  lean_forward_reaction: [LEAN(1.25)],
  lean_back:     [LEAN(-0.85)],
  posture_lean_back: [LEAN(-1.0), SHOULDERS(-0.4)],
  chart_inspect: [LEAN(0.7), P({ head: { offset: [0, -18, -6] } })],
  quick_chart_glance: [HEAD_TURN(0.5)],
  monitor_left_glance:  [HEAD_TURN(0.95)],
  monitor_right_glance: [HEAD_TURN(-0.95)],
  hand_to_chin:  [R('hand_r', 'CHIN'), LEAN(0.3)],
  hand_to_mouth: [R('hand_r', 'MOUTH')],
  watch_check:   [R('hand_l', 'WATCH'),
                  P({ head: { offset: [-10, 0, -26], rot: -0.08 } }, { ease: 'attention' })],

  // ---- caffeine: the mug travels ------------------------------------------
  reach_cup:     [R('hand_r', 'ANCHOR_MUG_BODY', { ease: 'attention' }),
                  P({ torso: { rot: 0.04 }, shoulder_r: { offset: [40, 0, 0] } })],
  pick_cup:      [R('hand_r', 'ANCHOR_MUG_LIP'), { kind: 'prop', prop: 'MUG', joint: 'hand_r' }],
  hold_cup:      [R('hand_r', 'MUG_CARRY'), { kind: 'prop', prop: 'MUG', joint: 'hand_r' }],
  sip:           [R('hand_r', 'MOUTH'), { kind: 'prop', prop: 'MUG', joint: 'hand_r' },
                  P({ head: { offset: [0, 18, 4], rot: 0.06 } }, { ease: 'settle' })],
  coffee_drink:  'sip',
  breath_after_sip: [SHOULDERS(0.5), { kind: 'prop', prop: 'MUG', joint: 'hand_r' },
                     R('hand_r', 'MUG_CARRY')],
  place_cup:     [R('hand_r', 'ANCHOR_MUG_RING', { ease: 'settle' }),
                  { kind: 'prop', prop: 'MUG', joint: 'hand_r' }],
  coffee_reset:  [R('hand_r', 'ANCHOR_MOUSE', { ease: 'settle' })],

  // ---- headphones ----------------------------------------------------------
  adjust_left:   [R('hand_l', 'ANCHOR_HP_CUP_L', { ease: 'attention' }),
                  P({ head: { rot: 0.05 } })],
  adjust_right:  [R('hand_r', 'ANCHOR_HP_CUP_R', { ease: 'attention' }),
                  P({ head: { rot: -0.05 } })],
  press_earcup:  [R('hand_r', 'ANCHOR_HP_CUP_R'), O('hand_r', { axis: 0, amp: 8, hz: 1.6 })],
  settle_headphones: [R('hand_l', 'ANCHOR_HP_BAND'), R('hand_r', 'ANCHOR_HP_BAND'),
                      P({ head: { offset: [0, 0, 6] } })],

  // ---- posture family: the long-horizon body maintenance -------------------
  chair_reposition: [P({ pelvis: { offset: [26, 14, 0] }, torso: { offset: [18, 8, 0], rot: 0.05 } },
                       { ease: 'settle' }), SHOULDERS(0.5)],
  spine_straighten: [P({ torso: { offset: [0, 0, 26] }, neck: { offset: [0, 0, 12] },
                         head: { offset: [0, 0, 14] } }, { ease: 'settle' }), SHOULDERS(0.8)],
  shoulder_roll:    [SHOULDERS(1.0), P({ head: { rot: 0.05 } })],
  neck_reset:       [P({ neck: { offset: [0, 0, 10], rot: 0.07 },
                         head: { offset: [16, 0, 8], rot: 0.12 } }, { ease: 'settle' })],
  neck_stretch:     [P({ neck: { rot: -0.1 }, head: { offset: [-26, 0, -6], rot: -0.18 } },
                       { ease: 'settle' })],
  elbow_reposition: [P({ forearm_l: { offset: [0, 22, 10] },
                         forearm_r: { offset: [0, 22, 10] } }, { ease: 'settle' })],
  hand_rest_reset:  [R('hand_l', 'ANCHOR_KEYBOARD_HOME_L', { ease: 'settle' }),
                     R('hand_r', 'ANCHOR_MOUSE', { ease: 'settle' })],

  // ---- fatigue -------------------------------------------------------------
  brief_head_down: [P({ head: { offset: [0, 0, -40], rot: -0.14 },
                        neck: { offset: [0, 0, -12] } }, { ease: 'settle' })],
  deep_exhale:     [SHOULDERS(-0.8), P({ torso: { offset: [0, 0, -14] } }, { ease: 'settle' })],
  refocus:         [P({ head: { offset: [0, -14, 8] } }, { ease: 'attention' }), SHOULDERS(0.4)],

  // ---- micro ---------------------------------------------------------------
  small_head_turn: [HEAD_TURN(0.38)],
  small_head_tilt: [HEAD_TILT(0.6)],
  shoulder_shift:  [SHOULDERS(0.35)],
  hand_reposition: [P({ hand_r: { offset: [16, 10, 6] } }, { ease: 'inOut' })],
  finger_tap:      [O('hand_r', { axis: 2, amp: 7, hz: 4.5 })],
  micro_brow_raise:[P({ lid_l: { offset: [0, 0, 5] }, lid_r: { offset: [0, 0, 5] } })],
  micro_frown:     [P({ lid_l: { offset: [0, 0, -3] }, lid_r: { offset: [0, 0, -3] },
                        head: { offset: [0, -4, 0] } })],
  eye_left:        [P({ head: { offset: [7, 0, 0] } }, { ease: 'attention' })],
  eye_right:       [P({ head: { offset: [-7, 0, 0] } }, { ease: 'attention' })],
  eye_down:        [P({ head: { offset: [0, 0, -6] } }, { ease: 'attention' })],
  eye_main_monitor:[P({ head: { offset: [0, -4, 0] } }, { ease: 'attention' })],

  // ---- music: weaker than the market, by design ----------------------------
  micro_head_nod:      [O('head', { axis: 2, amp: 9, hz: null, beats: true })],
  small_shoulder_rhythm: [O('shoulder_l', { axis: 2, amp: 7, hz: null, beats: true }),
                          O('shoulder_r', { axis: 2, amp: 7, hz: null, beats: true,
                                            phase: 0.5 })],
  finger_rhythm:       [O('hand_r', { axis: 2, amp: 6, hz: null, beats: true })],

  // ---- reaction ------------------------------------------------------------
  small_nod:        [P({ head: { offset: [0, 0, -16], rot: -0.06 } }, { ease: 'attention' })],
  controlled_exhale:[SHOULDERS(-0.6)],
  subtle_smirk:     [P({ head: { offset: [4, 0, 0], rot: 0.03 } })],
};

/* Positions the motion map refers to that are not frozen anchors, because they are on the
 * body rather than on the desk. Derived from the joint rest positions at load, so they
 * move with the blockout rather than being typed in twice. */
const BODY_POINTS = {
  CHIN:      (joints) => offsetOf(joints, 'head', [0, -70, -96]),
  MOUTH:     (joints) => offsetOf(joints, 'head', [0, -84, -58]),
  WATCH:     (joints) => offsetOf(joints, 'head', [-150, -210, -230]),
  MUG_CARRY: (joints) => offsetOf(joints, 'head', [240, -150, -300]),
};

/* The character's segment list, declared once so the draw pass and the contract tests
 * agree on what an arm is made of. `scene.py::LIMB_SEGMENTS` counts these for the derived
 * frame budget; `test_the_limb_segment_count_matches_the_renderer` pins the two together. */
const LIMB_SEGMENTS = [
  ['shoulder_l', 'forearm_l', 64], ['forearm_l', 'hand_l', 58],
  ['shoulder_r', 'forearm_r', 64], ['forearm_r', 'hand_r', 58],
  ['pelvis', 'torso', 150], ['torso', 'neck', 96], ['neck', 'head', 86],
];

function offsetOf(joints, jointId, delta) {
  const joint = joints.find((j) => j.id === jointId);
  if (!joint) return null;
  return [joint.position[0] + delta[0],
          joint.position[1] + delta[1],
          joint.position[2] + delta[2]];
}

/* Resolve `MOTION[id]`, following string aliases. */
function motionFor(actionId) {
  let spec = MOTION[actionId];
  let hops = 0;
  while (typeof spec === 'string' && hops++ < 4) spec = MOTION[spec];
  if (Array.isArray(spec)) return spec;
  /* Unlisted actions still move: a small generic settle, so the viewer sees that
   * *something* happened rather than nothing, and the HUD names it. Better than silence,
   * and the gap is visible in the HUD's "generic" marker. */
  return null;
}

/* Deterministic value noise, for the camera's breathing drift.
 * A Perlin-ish walk rather than a sine: a sine is recognisable as a loop within about
 * ninety seconds, and the whole purpose of the drift is to be unrecognisable. */
function noise1(x) {
  const i = Math.floor(x), f = x - i;
  const h = (n) => {
    const s = Math.sin(n * 127.1) * 43758.5453;
    return s - Math.floor(s);
  };
  return lerp(h(i), h(i + 1), f * f * (3 - 2 * f)) * 2 - 1;
}

// ============================================================ GL

const VERTEX_SRC = `#version 300 es
in vec2 a_quad;
uniform mat4 u_viewProj;
uniform vec3 u_centre;      // world position, millimetres
uniform vec2 u_size;        // world size, millimetres
uniform vec3 u_basisX;      // quad's world X axis
uniform vec3 u_basisY;      // quad's world Y axis
uniform vec2 u_parallax;    // screen-space offset from depth
out vec2 v_uv;
void main() {
  v_uv = a_quad;
  vec3 world = u_centre
             + u_basisX * (a_quad.x - 0.5) * u_size.x
             + u_basisY * (a_quad.y - 0.5) * u_size.y;
  vec4 clip = u_viewProj * vec4(world, 1.0);
  clip.xy += u_parallax * clip.w;
  gl_Position = clip;
}`;

/* One fragment shader for every shape. `u_shape` selects a signed-distance field.
 *
 * Eight shapes, still one program and still no atlas — proof mode needs rounded corners,
 * vertical gradients, soft glows and tapers to read as a room with a person in it rather
 * than as a pile of grey rectangles, and all four are a few lines of SDF each. The
 * texture path is dormant until painted plates are imported; `u_useTex` keeps it a branch
 * rather than a second program.
 *
 * VRAM stays near zero in procedural mode, which matters because ACE-Step has priority
 * on this GPU. */
const SHAPE = {
  RECT: 0, ELLIPSE: 1, OUTLINE: 2, ROUNDED: 3,
  GRADIENT: 4, GLOW: 5, TAPER: 6, TEXTURE: 7,
};

const FRAGMENT_SRC = `#version 300 es
precision mediump float;
in vec2 v_uv;
uniform vec4 u_colour;
uniform vec4 u_colour2;     // gradient far end / glow edge
uniform int u_shape;
uniform float u_edge;       // outline thickness | corner radius | taper fraction
uniform sampler2D u_tex;
uniform int u_useTex;
out vec4 o_colour;

float roundedBox(vec2 p, vec2 half_size, float radius) {
  vec2 q = abs(p) - half_size + radius;
  return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - radius;
}

void main() {
  vec4 colour = u_colour;
  float alpha = colour.a;

  if (u_shape == 1) {                       // ellipse
    float d = length((v_uv - 0.5) * 2.0);
    alpha *= 1.0 - smoothstep(0.94, 1.0, d);
  } else if (u_shape == 2) {                // rect outline
    vec2 d = min(v_uv, 1.0 - v_uv);
    alpha *= 1.0 - smoothstep(u_edge * 0.6, u_edge, min(d.x, d.y));
  } else if (u_shape == 3) {                // rounded rect
    float d = roundedBox((v_uv - 0.5) * 2.0, vec2(1.0), clamp(u_edge, 0.0, 1.0) * 2.0);
    alpha *= 1.0 - smoothstep(-0.02, 0.02, d);
  } else if (u_shape == 4) {                // vertical gradient
    colour = mix(u_colour, u_colour2, smoothstep(0.0, 1.0, v_uv.y));
    alpha = colour.a;
  } else if (u_shape == 5) {                // radial glow
    float d = length((v_uv - 0.5) * 2.0);
    float falloff = 1.0 - smoothstep(0.0, 1.0, d);
    colour = mix(u_colour2, u_colour, falloff);
    /* Squared falloff: a linear glow reads as a flat disc, which is worse than no glow. */
    alpha = colour.a * falloff * falloff;
  } else if (u_shape == 6) {                // taper — wide at the bottom, u_edge at the top
    float halfWidth = mix(1.0, clamp(u_edge, 0.02, 1.0), v_uv.y) ;
    float x = abs((v_uv.x - 0.5) * 2.0);
    alpha *= 1.0 - smoothstep(halfWidth - 0.04, halfWidth, x);
  }

  if (u_useTex == 1) {
    vec4 sampled = texture(u_tex, vec2(v_uv.x, 1.0 - v_uv.y));
    colour = vec4(sampled.rgb * colour.rgb, 1.0);
    alpha *= sampled.a;
  }

  if (alpha < 0.004) discard;
  o_colour = vec4(colour.rgb, alpha);
}`;

function compile(gl, type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    throw new Error('shader: ' + gl.getShaderInfoLog(shader));
  }
  return shader;
}

// ============================================================ the renderer

class Runtime {
  constructor(canvas) {
    this.canvas = canvas;
    this.gl = canvas.getContext('webgl2', {
      alpha: true, antialias: true, premultipliedAlpha: false,
      powerPreference: 'low-power', // ACE-Step has priority on this GPU
      desynchronized: true,
      preserveDrawingBuffer: true,  // Required for screenshots and diagnostics
    });
    if (!this.gl) throw new Error('WebGL2 is unavailable in this browser');

    this.scene = null;
    this.cameras = new Map();
    this.camera = null;
    this.cameraId = null;

    /* Interpolated character pose: joint id -> {offset:[x,y,z], rot, close}. Every value
     * is eased toward a target; nothing is ever assigned directly, which is what keeps
     * hands from teleporting. */
    this.pose = new Map();
    this.actions = [];          // in-flight actions with their timing
    this.gaze = { target: null, from: null, started: 0, transit: 300, head: 0 };
    this.blink = { closing: 0, duration: 0 };
    this.charts = new Map();    // surface id -> generated series
    this.screen = null;         // last SCREEN command
    this.rhythm = { bpm: null, phase: 0, subdivision: 1, nod: 0, maxDeg: 1.1 };
    this.fatigue = 0;
    this.characterState = '—';
    this.chain = null;
    this.queued = 0;

    this.transition = { from: null, started: 0, kind: 'cut', ms: 0 };
    this.parallax = 0.007;

    this.frames = 0;
    this.dropped = 0;
    this.frameTimes = [];
    this.drawCalls = 0;
    this.lastFrame = 0;
    this.started = performance.now();
    this.socketState = 'connecting';

    this._initGl();
    this._resize();
    window.addEventListener('resize', () => this._resize());
  }

  _initGl() {
    const gl = this.gl;
    const program = gl.createProgram();
    gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, VERTEX_SRC));
    gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, FRAGMENT_SRC));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error('link: ' + gl.getProgramInfoLog(program));
    }
    this.program = program;
    gl.useProgram(program);

    this.u = {};
    for (const name of ['u_viewProj','u_centre','u_size','u_basisX','u_basisY',
                        'u_parallax','u_colour','u_colour2','u_shape','u_edge',
                        'u_tex','u_useTex']) {
      this.u[name] = gl.getUniformLocation(program, name);
    }

    const quad = new Float32Array([0,0, 1,0, 0,1, 0,1, 1,0, 1,1]);
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, quad, gl.STATIC_DRAW);
    const location = gl.getAttribLocation(program, 'a_quad');
    gl.enableVertexAttribArray(location);
    gl.vertexAttribPointer(location, 2, gl.FLOAT, false, 0, 0);

    gl.enable(gl.BLEND);
    gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    /* Depth testing is OFF deliberately. This is a 2.5D compositor: order comes from the
     * declared layer order, exactly as it will when painted plates replace these shapes.
     * A depth buffer would make the placeholder behave differently from the product. */
    gl.disable(gl.DEPTH_TEST);

    this.memExt = gl.getExtension('GMAN_webgl_memory') || null;

    /* Imported art. Empty until plates are validated and loaded; every layer that has no
     * plate draws procedurally, so a partial delivery composes rather than being
     * all-or-nothing. */
    this.art = (typeof TF_ART !== 'undefined')
      ? new TF_ART.ArtLibrary(gl) : null;
  }

  _resize() {
    /* Letterbox the 16:9 target into whatever the browser source gives us, so framing is
     * identical at any browser-source size. */
    const w = window.innerWidth, h = window.innerHeight;
    const scale = Math.min(w / TARGET_W, h / TARGET_H) || 1;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.canvas.style.width = `${Math.round(TARGET_W * scale)}px`;
    this.canvas.style.height = `${Math.round(TARGET_H * scale)}px`;
    this.canvas.width = Math.round(TARGET_W * dpr * scale);
    this.canvas.height = Math.round(TARGET_H * dpr * scale);
    this.gl.viewport(0, 0, this.canvas.width, this.canvas.height);
  }

  // ---------------------------------------------------------- scene

  async loadScene() {
    const response = await fetch('/api/visual/scene');
    if (!response.ok) throw new Error(`scene: HTTP ${response.status}`);
    /* Validated before anything is drawn with it. The payload crosses a process
     * boundary, and a geometry field of the wrong shape must be reported as a contract
     * violation at the boundary rather than as an unattributed TypeError sixty draw
     * calls later. */
    this.scene = validateScene(await response.json());
    for (const camera of this.scene.cameras) this.cameras.set(camera.id, camera);
    this.setCamera(this.scene.cameras.find((c) => c.id === 'CAM_7')?.id
                   || this.scene.cameras[0].id, 'cut', 0);

    for (const joint of this.scene.joints) {
      /* `offset` and `rot` are what the draw pass reads; `channels` are the in-flight
       * motions that sum into them. A joint can carry several at once — a hand reaching
       * for the keyboard while oscillating over it is two channels, not one compromise. */
      this.pose.set(joint.id, { offset: [0,0,0], rot: 0, channels: [] });
    }
    this.bodyPoints = {};
    for (const [name, resolve] of Object.entries(BODY_POINTS)) {
      const point = resolve(this.scene.joints);
      if (point) this.bodyPoints[name] = point;
    }
    this.propOffsets = new Map();   // box id -> [dx,dy,dz], for a carried mug
    this.propHolds = [];            // in-flight prop attachments
    for (const quad of this.scene.quads) {
      if (quad.live) this.charts.set(quad.id, this._series(quad.id));
    }
  }

  /* Locally generated chart-like data. Deliberately NOT live market data: the brief says
   * the placeholder must not depend on TradingView, and the honesty rules say a chart
   * must never advance without data behind it — so this is visibly synthetic, labelled,
   * and frozen whenever the state says charts must not advance. */
  _series(seed) {
    let h = 0;
    for (const ch of seed) h = (h * 31 + ch.charCodeAt(0)) & 0x7fffffff;
    const points = [];
    let value = 0.5;
    for (let i = 0; i < 64; i++) {
      h = (h * 1103515245 + 12345) & 0x7fffffff;
      value = clamp(value + ((h / 0x7fffffff) - 0.5) * 0.09, 0.05, 0.95);
      points.push(value);
    }
    return points;
  }

  setCamera(id, kind, ms) {
    if (!this.cameras.has(id)) return;
    this.transition = { from: this.cameraId, started: performance.now(), kind, ms: ms || 0 };
    this.cameraId = id;
    this.camera = this.cameras.get(id);
    this.parallax = this.camera.parallax_amplitude;
    this.cameraSince = performance.now();
  }

  // ---------------------------------------------------------- commands

  handle(commands) {
    for (const command of commands) {
      switch (command.kind) {
        case 'RESYNC':
          this.characterState = command.character_state;
          this.fatigue = command.fatigue_phase ?? 0;
          if (command.camera_id) this.setCamera(command.camera_id, 'cut', 0);
          if (command.gaze_target) this.gaze.target = command.gaze_target;
          break;
        case 'START':  this._startAction(command); break;
        case 'GAZE':   this._startGaze(command); break;
        case 'CAMERA': this.setCamera(command.camera_id, command.transition, 0); break;
        case 'SCREEN': this.screen = command; break;
        case 'SET':
          this.rhythm.bpm = command.bpm;
          this.rhythm.phase = command.downbeat_phase ?? 0;
          this.rhythm.subdivision = command.beat_subdivision ?? 1;
          this.rhythm.nod = command.nod_probability ?? 0;
          this.rhythm.maxDeg = command.max_nod_degrees ?? 1.1;
          break;
        default: break;
      }
    }
  }

  _startAction(command) {
    const now = performance.now();
    this.characterState = command.character_state || this.characterState;
    this.chain = command.chain_id || null;

    if (command.action_id === 'blink' || command.action_id === 'double_blink'
        || command.action_id === 'slow_blink') {
      this.blink = { closing: now, duration: command.duration_ms };
      return;
    }

    this.actions.push({
      id: command.action_id,
      started: now,
      blendIn: command.blend_in_ms,
      blendOut: command.blend_out_ms,
      duration: command.duration_ms,
      ends: now + command.blend_in_ms + command.duration_ms + command.blend_out_ms,
      amplitude: command.amplitude ?? 1,
      anchor: command.anchor || null,
      chain: command.chain_id || null,
    });
    if (this.actions.length > 24) this.actions.splice(0, this.actions.length - 24);

    this._applyMotion(command, now);
  }

  /* Turn one action command into pose channels.
   *
   * Timing comes from the command, not from this file: blend_in_ms is the ramp, duration
   * is the hold, blend_out_ms is the return to rest. That is the brief's blend-in / hold
   * / blend-out, and it means a long typing_long and a short typing_short read as the
   * same gesture at different lengths rather than as two different animations. */
  _applyMotion(command, now) {
    const spec = motionFor(command.action_id);
    const amp = command.amplitude ?? 1;
    const timing = {
      t0: now,
      inMs: Math.max(60, command.blend_in_ms || 120),
      holdMs: Math.max(0, command.duration_ms || 0),
      outMs: Math.max(60, command.blend_out_ms || 160),
    };

    if (!spec) {
      /* Not in the motion map. Give it a small honest settle on the torso so the viewer
       * sees the beat land, and mark it so the HUD can say the representation is generic
       * rather than pretending this action is fully drawn. */
      this._pushChannel('torso', {
        ...timing, kind: 'pose', ease: 'inOut',
        offset: [0, 0, 5 * amp], rot: 0.012 * amp,
      });
      this.genericMotion = command.action_id;
      return;
    }

    for (const entry of spec) {
      if (entry.kind === 'reach') {
        const point = this._resolvePoint(entry.anchor);
        if (!point) continue;
        /* Stored as a WORLD destination, not as an offset from rest.
         *
         * An offset would be added on top of whatever the parent chain contributes, so a
         * hand reaching the mug while the torso leaned would overshoot the mug by the
         * lean. Resolving in world space means the hand lands exactly on the frozen
         * anchor regardless of what the rest of the body is doing — which is the whole
         * reason the demo can verify that anchors are reachable at all.
         *
         * Amplitude deliberately does not scale the destination. It shapes the approach;
         * contact is contact. */
        this._pushChannel(entry.joint, {
          ...timing,
          kind: 'reach',
          ease: entry.ease || 'attention',
          target: point.slice(),
        });
      } else if (entry.kind === 'pose') {
        for (const [jointId, pose] of Object.entries(entry.joints)) {
          this._pushChannel(jointId, {
            ...timing,
            kind: 'pose',
            ease: entry.ease || 'inOut',
            offset: (pose.offset || [0, 0, 0]).map((v) => v * amp),
            rot: (pose.rot || 0) * amp,
          });
        }
      } else if (entry.kind === 'osc') {
        /* `beats: true` locks the oscillation to the music's own tempo, which is how the
         * nod stays musical instead of merely rhythmic. Capped at the rhythm policy's
         * max_nod_degrees, restated on every SET so a dropped message cannot raise it. */
        const hz = entry.beats
          ? (this.rhythm.bpm ? (this.rhythm.bpm / 60) / (this.rhythm.subdivision || 1) : 0)
          : entry.hz;
        if (!hz) continue;
        const ceiling = entry.beats
          ? Math.min(entry.amp, this.rhythm.maxDeg * 9) : entry.amp;
        this._pushChannel(entry.joint, {
          ...timing, kind: 'osc', ease: 'inOut',
          axis: entry.axis, amp: ceiling * amp, hz, phase: entry.phase || 0,
        });
      } else if (entry.kind === 'prop') {
        this.propHolds.push({
          prop: entry.prop, joint: entry.joint,
          until: now + timing.inMs + timing.holdMs + timing.outMs,
        });
      }
    }
  }

  /* A frozen V1 anchor, or a body point derived from the joint rest pose. Nothing else —
   * an unresolved name is dropped rather than guessed at. */
  _resolvePoint(name) {
    return this.scene?.anchors?.[name] || this.bodyPoints?.[name] || null;
  }

  _pushChannel(jointId, channel) {
    const joint = this.pose.get(jointId);
    if (!joint) return;
    /* One channel per joint per kind per axis-set: a second reach replaces the first
     * rather than summing with it, because two simultaneous destinations average into a
     * place neither action asked for. Oscillations on different axes coexist. */
    const key = channel.kind === 'osc' ? `osc${channel.axis}` : 'pose';
    joint.channels = joint.channels.filter((c) => c._key !== key);
    channel._key = key;
    joint.channels.push(channel);
    if (joint.channels.length > 6) joint.channels.shift();
  }

  /* Advance every channel and collapse them into the joint's offset and rotation. */
  _advancePose(now) {
    for (const [, joint] of this.pose) {
      let ox = 0, oy = 0, oz = 0, rot = 0;
      let sx = 0, sy = 0, sz = 0;   // oscillation, applied after the reach
      joint.reach = null;
      const alive = [];
      for (const channel of joint.channels) {
        const elapsed = now - channel.t0;
        const total = channel.inMs + channel.holdMs + channel.outMs;
        if (elapsed >= total) continue;
        alive.push(channel);

        /* Envelope: 0 -> 1 over blend-in, 1 across the hold, 1 -> 0 over blend-out. */
        let envelope;
        if (elapsed < channel.inMs) {
          envelope = (Ease[channel.ease] || Ease.inOut)(elapsed / channel.inMs);
        } else if (elapsed < channel.inMs + channel.holdMs) {
          envelope = 1;
        } else {
          const out = (elapsed - channel.inMs - channel.holdMs) / channel.outMs;
          envelope = 1 - (Ease.settle)(clamp(out, 0, 1));
        }

        if (channel.kind === 'reach') {
          /* Resolved against the world transform in `_worldFor`, after the parent chain,
           * so the destination is absolute. */
          joint.reach = { target: channel.target, envelope };
        } else if (channel.kind === 'osc') {
          const wave = Math.sin(
            ((elapsed / 1000) * channel.hz + channel.phase) * Math.PI * 2);
          const value = wave * channel.amp * envelope;
          /* Kept apart from the pose offset because it has to be applied AFTER the reach
           * pin. Summed into the pose, a hand pinned to the keyboard anchor would stop
           * oscillating the instant it arrived — which is to say typing would look like
           * a hand resting on a keyboard. */
          if (channel.axis === 0) sx += value;
          else if (channel.axis === 1) sy += value;
          else sz += value;
        } else {
          ox += channel.offset[0] * envelope;
          oy += channel.offset[1] * envelope;
          oz += channel.offset[2] * envelope;
          rot += channel.rot * envelope;
        }
      }
      joint.channels = alive;
      joint.offset[0] = ox;
      joint.offset[1] = oy;
      joint.offset[2] = oz;
      joint.osc = [sx, sy, sz];
      joint.rot = rot;
    }

    /* Props carried by a hand. The mug travels to the mouth and back because the HAND
     * does — it is parented to the motion rather than animated alongside it, which is
     * why it cannot drift out of the hand. */
    this.propOffsets.clear();
    this.propHolds = this.propHolds.filter((hold) => hold.until > now);
    for (const hold of this.propHolds) {
      const world = this._worldFor(hold.joint);
      const rest = this.scene.joints.find((j) => j.id === hold.joint);
      if (!world || !rest) continue;
      this.propOffsets.set(hold.prop, [
        world.position[0] - rest.position[0],
        world.position[1] - rest.position[1],
        world.position[2] - rest.position[2],
      ]);
    }
  }

  _startGaze(command) {
    this.gaze = {
      target: command.target,
      from: command.from || this.gaze.target,
      started: performance.now(),
      /* Never below the geometry's floor. Gaze that snaps is the clearest tell of a
       * synthetic rig, and the director already guarantees this — the renderer enforces
       * it again so a malformed command cannot produce it. */
      transit: Math.max(180, command.transit_ms),
      head: command.head_contribution ?? 0,
    };
    if (command.forces_blink) this.blink = { closing: performance.now(), duration: 130 };
  }

  // ---------------------------------------------------------- frame

  draw(now) {
    const gl = this.gl;
    this.drawCalls = 0;

    /* Clear to a distinguishable dark blue-gray, NOT pure black. This ensures any black
     * geometry remains visible against the background. In debug contrast mode, use an
     * even more visible teal so near-black scene elements cannot disappear. */
    if (CONTRAST_MODE === 'debug') {
      gl.clearColor(0.05, 0.12, 0.15, 1);  // RGB(13, 31, 38) - visible teal
    } else {
      gl.clearColor(0.047, 0.071, 0.110, 1);  // RGB(12, 18, 28) - dark blue-gray
    }
    gl.clear(gl.COLOR_BUFFER_BIT);
    if (!this.scene || !this.camera) return;

    const aspect = TARGET_W / TARGET_H;
    const seconds = (now - this.started) / 1000;

    /* Camera breathing drift: a two-axis noise walk, 0.3-0.8 % of frame, 45-90 s period.
     * Plus a slow push-in on the cameras that permit one. */
    const driftX = noise1(seconds / 61) * 0.004;
    const driftY = noise1(seconds / 47 + 11) * 0.004;
    this.drift = Math.hypot(driftX, driftY);

    const held = (now - (this.cameraSince || now)) / 1000;
    const push = this.camera.allows_push_in ? clamp(held / 240, 0, 1) * 0.04 : 0;

    const eye = this.camera.position.slice();
    const target = this.camera.target.slice();
    for (let i = 0; i < 3; i++) eye[i] = lerp(eye[i], target[i], push);

    const view = mat4.lookAt(eye, target);
    const projection = mat4.perspective(this.camera.vfov, aspect, 50, 60000);
    const viewProj = mat4.multiply(projection, view);

    gl.uniformMatrix4fv(this.u.u_viewProj, false, viewProj);
    this._drift = [driftX, driftY];
    /* Kept so the overlay can project world points to screen with the same matrix the
     * geometry used — a debug line drawn through a different matrix is worse than none. */
    this.viewProj = viewProj;

    // Retire finished actions before the pose is read.
    this.actions = this.actions.filter((a) => a.ends > now);
    this.queued = this.actions.length;

    this._advancePose(now);

    /* The character's world transforms are resolved first either way, because proof mode
     * draws different shapes at the *same* joint positions. One animation path, two
     * appearances — a second path would be a second thing to keep in sync. */
    const pose = this._resolveCharacter(now);

    /* RENDER TEST MODE: Draw only specific layers for progressive debugging */
    if (RENDER_TEST === 'room') {
      // Just the room shell with BRIGHT DEBUG COLORS
      this._drawRoomShellDebug(now);
    } else if (RENDER_TEST === 'world_marker') {
      // Draw BRIGHT markers at known world positions to verify MVP works
      this._drawWorldMarkers(now);
    } else if (RENDER_TEST === 'world_marker_identity') {
      // CRITICAL TEST: Use identity matrix to verify shader path works
      this._drawWorldMarkersIdentity(now);
    } else if (RENDER_TEST === 'desk') {
      this._drawRoomShellDebug(now);
      this._drawDeskDebug(now);
    } else if (RENDER_TEST === 'character') {
      this._drawRoomShellDebug(now);
      this._drawDeskDebug(now);
      this._drawCharacterDebug(now, pose);
    } else if (ART_MODE === 'blockout' || typeof TF_PROOF === 'undefined') {
      this._drawRoomShell(now);
      this._drawBoxes(now);
      this._drawQuads(now);
      this._drawCharacter(now, pose);
    } else {
      TF_PROOF.draw(this, now, pose.lidClose, pose.gazeEase, CONTRAST_MODE);
    }
    if (view.debug) this._drawDebug(now);

    /* Diagnostic logging for first frame and when requested. */
    if (this.frames === 0 || RENDER_TEST) {
      this._logDiagnostics(now);
    }
  }

  /* DEBUG: Draw room shell with BRIGHT IMPOSSIBLE-TO-MISS colors */
  _drawRoomShellDebug() {
    this.primitive = 'room_shell_debug';
    const [rw, rd, rh] = this.scene.room;

    // BRIGHT RED floor
    this._quad([rw / 2, rd / 2, 0], [rw, rd], [1, 0, 0], [0, 1, 0],
               [0.8, 0.2, 0.2, 1], 0);
    // BRIGHT BLUE back wall
    this._quad([rw / 2, rd, rh / 2], [rw, rh], [1, 0, 0], [0, 0, 1],
               [0.2, 0.4, 0.8, 1], 0);
    // BRIGHT GREEN side walls
    this._quad([0, rd / 2, rh / 2], [rd, rh], [0, 1, 0], [0, 0, 1],
               [0.2, 0.7, 0.3, 1], 0);
    this._quad([rw, rd / 2, rh / 2], [rd, rh], [0, 1, 0], [0, 0, 1],
               [0.2, 0.6, 0.3, 1], 0);
  }

  /* DEBUG: Draw desk with BRIGHT color */
  _drawDeskDebug() {
    this.primitive = 'desk_debug';
    const desk = this.scene.boxes.find((b) => b.id === 'DESK');
    if (!desk) return;

    const [x0, y0, z0] = desk.min;
    const [x1, y1, z1] = desk.max;
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;

    // BRIGHT BROWN desk surface
    this._quad([cx, cy, z1], [x1 - x0, y1 - y0], [1, 0, 0], [0, 1, 0],
               [0.6, 0.4, 0.2, 1], 0);

    // Monitors - BRIGHT BLUE
    for (const quad of this.scene.quads) {
      if (quad.role === 'window') continue;
      const yaw = quad.yaw * Math.PI / 180;
      const bx = [Math.cos(yaw), Math.sin(yaw), 0];
      this._quad(quad.centre, [quad.width, quad.height], bx, [0, 0, 1],
                 [0.2, 0.5, 0.9, 1], 0);
    }
  }

  /* DEBUG: Draw character with BRIGHT IMPOSSIBLE-TO-MISS colors */
  _drawCharacterDebug(now, pose) {
    const { worlds } = pose;
    this.primitive = 'character_debug';

    // Head - BRIGHT PEACH
    const head = worlds.get('head');
    if (head) {
      const { right, up } = this._billboard(0);
      this._quad(head.position, [200, 260], right, up, [0.95, 0.75, 0.6, 1], 1);
    }

    // Torso - BRIGHT GRAY
    const torso = worlds.get('torso');
    if (torso) {
      const { right, up } = this._billboard(0);
      this._quad(torso.position, [400, 560], right, up, [0.5, 0.5, 0.55, 1], 0);
    }

    // Hands - BRIGHT YELLOW
    for (const handId of ['hand_l', 'hand_r']) {
      const hand = worlds.get(handId);
      if (hand) {
        const { right, up } = this._billboard(0);
        this._quad(hand.position, [110, 90], right, up, [0.95, 0.9, 0.4, 1], 1);
      }
    }
  }

  /* WORLD MARKER TEST: Draw bright markers at known-visible world positions.
   * Uses positions verified to be within frustum by matrix audit:
   * - HEAD at NDC.y=+0.104 (near center)
   * - MONITOR at NDC.y=-0.943 (near bottom, but visible)
   * - BACK_WALL at NDC.y=+0.528 (upper half)
   * If these are invisible, the MVP transform or draw call is broken. */
  _drawWorldMarkers() {
    this.primitive = 'world_marker';
    const gl = this.gl;

    // ===== COMPREHENSIVE GPU STATE AUDIT =====
    console.group('[GPU_AUDIT] World Marker Draw');

    // 1. Verify correct program is bound
    const currentProgram = gl.getParameter(gl.CURRENT_PROGRAM);
    console.log('CURRENT_PROGRAM:', currentProgram === this.program ? 'CORRECT' : 'WRONG!');

    // 2. Check all uniform locations
    console.group('Uniform Locations');
    for (const [name, loc] of Object.entries(this.u)) {
      console.log(`  ${name}: ${loc === null ? 'NULL!' : 'OK'}`);
    }
    console.groupEnd();

    // 3. Check GL state
    console.log('FRAMEBUFFER_BINDING:', gl.getParameter(gl.FRAMEBUFFER_BINDING));
    console.log('RASTERIZER_DISCARD:', gl.isEnabled(gl.RASTERIZER_DISCARD) ? 'ENABLED!' : 'disabled');
    console.log('COLOR_WRITEMASK:', gl.getParameter(gl.COLOR_WRITEMASK));
    console.log('VIEWPORT:', Array.from(gl.getParameter(gl.VIEWPORT)));
    console.log('SCISSOR_TEST:', gl.isEnabled(gl.SCISSOR_TEST) ? 'enabled' : 'disabled');
    console.log('DEPTH_TEST:', gl.isEnabled(gl.DEPTH_TEST) ? 'enabled' : 'disabled');
    console.log('BLEND:', gl.isEnabled(gl.BLEND) ? 'enabled' : 'disabled');

    // 4. Check vertex attribute state
    const posLoc = gl.getAttribLocation(this.program, 'a_quad');
    console.log('a_quad location:', posLoc);
    console.log('a_quad enabled:', gl.getVertexAttrib(posLoc, gl.VERTEX_ATTRIB_ARRAY_ENABLED));
    console.log('a_quad buffer:', gl.getVertexAttrib(posLoc, gl.VERTEX_ATTRIB_ARRAY_BUFFER_BINDING));

    // 5. Log viewProj matrix
    const m = this.viewProj;
    console.log('viewProj matrix (row-by-row for readability):');
    console.log('  Row 0:', [m[0], m[4], m[8], m[12]].map(v => v.toFixed(6)));
    console.log('  Row 1:', [m[1], m[5], m[9], m[13]].map(v => v.toFixed(6)));
    console.log('  Row 2:', [m[2], m[6], m[10], m[14]].map(v => v.toFixed(6)));
    console.log('  Row 3:', [m[3], m[7], m[11], m[15]].map(v => v.toFixed(6)));

    // 6. Verify u_viewProj uniform value after upload
    gl.uniformMatrix4fv(this.u.u_viewProj, false, this.viewProj);
    const err1 = gl.getError();
    console.log('u_viewProj upload error:', err1 === gl.NO_ERROR ? 'NONE' : `ERROR ${err1}`);

    console.groupEnd();

    // ===== DRAW MARKERS =====
    const { right, up } = this._billboard(0);

    // Helper to draw marker with full logging
    const drawMarker = (name, centre, size, basisX, basisY, colour) => {
      console.group(`[MARKER] ${name}`);

      // Log uniforms being uploaded
      console.log('u_centre:', centre);
      console.log('u_size:', size);
      console.log('u_basisX:', basisX);
      console.log('u_basisY:', basisY);
      console.log('u_colour:', colour);

      // Compute expected clip position for center point
      const x = m[0]*centre[0] + m[4]*centre[1] + m[8]*centre[2] + m[12];
      const y = m[1]*centre[0] + m[5]*centre[1] + m[9]*centre[2] + m[13];
      const z = m[2]*centre[0] + m[6]*centre[1] + m[10]*centre[2] + m[14];
      const w = m[3]*centre[0] + m[7]*centre[1] + m[11]*centre[2] + m[15];
      console.log(`CPU clip: [${x.toFixed(2)}, ${y.toFixed(2)}, ${z.toFixed(2)}, ${w.toFixed(2)}]`);
      console.log(`CPU NDC: [${(x/w).toFixed(3)}, ${(y/w).toFixed(3)}, ${(z/w).toFixed(3)}]`);
      console.log(`W positive: ${w > 0}`);

      // Upload uniforms and check for errors
      gl.uniform3fv(this.u.u_centre, centre);
      gl.uniform2fv(this.u.u_size, size);
      gl.uniform3fv(this.u.u_basisX, basisX);
      gl.uniform3fv(this.u.u_basisY, basisY);
      gl.uniform2fv(this.u.u_parallax, [0, 0]);  // No parallax for test
      gl.uniform4fv(this.u.u_colour, colour);
      gl.uniform4fv(this.u.u_colour2, colour);
      gl.uniform1i(this.u.u_shape, 0);  // RECT
      gl.uniform1f(this.u.u_edge, 0);
      gl.uniform1i(this.u.u_useTex, 0);

      const errBefore = gl.getError();
      console.log('Pre-draw error:', errBefore === gl.NO_ERROR ? 'NONE' : `ERROR ${errBefore}`);

      // Draw
      gl.drawArrays(gl.TRIANGLES, 0, 6);
      this.drawCalls++;

      const errAfter = gl.getError();
      console.log('Post-draw error:', errAfter === gl.NO_ERROR ? 'NONE' : `ERROR ${errAfter}`);

      console.groupEnd();
    };

    // Draw markers
    drawMarker('HEAD_CYAN', [3200, 3000, 1375], [400, 400], right, up, [0, 1, 1, 1]);
    drawMarker('MONITOR_RED', [3200, 2350, 1130], [600, 336], [1, 0, 0], [0, 0, 1], [1, 0, 0, 1]);
    drawMarker('BACKWALL_MAGENTA', [3200, 4800, 1500], [2000, 1500], [1, 0, 0], [0, 0, 1], [1, 0, 1, 1]);

    // ===== PIXEL READBACK AFTER DRAW =====
    gl.finish();  // Ensure draw completes
    const canvas = this.canvas;
    const cx = Math.floor(canvas.width / 2);
    const cy = Math.floor(canvas.height / 2);

    const readPixel = (x, y, label) => {
      const pixel = new Uint8Array(4);
      gl.readPixels(x, canvas.height - y, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
      return { label, x, y, r: pixel[0], g: pixel[1], b: pixel[2], a: pixel[3] };
    };

    console.group('[PIXEL_READBACK] After marker draw');
    const samples = [
      readPixel(cx, cy, 'center'),
      readPixel(cx, cy - 100, 'above_center'),
      readPixel(cx, cy + 100, 'below_center'),
    ];
    for (const s of samples) {
      const isBackground = s.r < 20 && s.g < 30 && s.b < 40;
      console.log(`${s.label}: RGB(${s.r},${s.g},${s.b}) A=${s.a} ${isBackground ? 'BACKGROUND' : 'COLORED!'}`);
    }

    // Count non-background pixels in center region
    const sampleSize = 100;
    let nonBgCount = 0;
    for (let sy = cy - sampleSize/2; sy < cy + sampleSize/2; sy += 10) {
      for (let sx = cx - sampleSize/2; sx < cx + sampleSize/2; sx += 10) {
        const p = readPixel(sx, sy, '');
        if (p.r > 20 || p.g > 30 || p.b > 40) nonBgCount++;
      }
    }
    console.log(`NON-BACKGROUND PIXELS in center 100x100: ${nonBgCount}/100`);
    console.groupEnd();

    console.log('[WORLD_MARKER] Total draw calls:', this.drawCalls);
  }

  /* IDENTITY MATRIX TEST: Use identity viewProj to verify shader works.
   * Draws clip-space quads using the world shader path but identity matrix.
   * If this is invisible, the shader/attribute path itself is broken.
   * If visible, the matrix calculation or upload is wrong. */
  _drawWorldMarkersIdentity() {
    this.primitive = 'world_marker_identity';
    const gl = this.gl;

    console.group('[IDENTITY_TEST] Drawing with identity viewProj');

    // Create identity matrix
    const identity = new Float32Array([
      1, 0, 0, 0,
      0, 1, 0, 0,
      0, 0, 1, 0,
      0, 0, 0, 1
    ]);

    // Upload identity as viewProj
    gl.uniformMatrix4fv(this.u.u_viewProj, false, identity);
    const err = gl.getError();
    console.log('Identity matrix upload error:', err === gl.NO_ERROR ? 'NONE' : `ERROR ${err}`);

    // Draw markers in CLIP SPACE (since identity means world = clip)
    // Centre at [0, 0, 0], small size, axis-aligned
    const drawClipMarker = (name, centre, size, colour) => {
      gl.uniform3fv(this.u.u_centre, centre);
      gl.uniform2fv(this.u.u_size, size);
      gl.uniform3fv(this.u.u_basisX, [1, 0, 0]);
      gl.uniform3fv(this.u.u_basisY, [0, 1, 0]);
      gl.uniform2fv(this.u.u_parallax, [0, 0]);
      gl.uniform4fv(this.u.u_colour, colour);
      gl.uniform4fv(this.u.u_colour2, colour);
      gl.uniform1i(this.u.u_shape, 0);
      gl.uniform1f(this.u.u_edge, 0);
      gl.uniform1i(this.u.u_useTex, 0);

      console.log(`${name}: centre=${centre}, size=${size}`);
      gl.drawArrays(gl.TRIANGLES, 0, 6);
      this.drawCalls++;
    };

    // These are in clip space (-1 to +1)
    drawClipMarker('LEFT_CYAN', [-0.5, 0, 0], [0.3, 0.3], [0, 1, 1, 1]);
    drawClipMarker('CENTER_YELLOW', [0, 0, 0], [0.3, 0.3], [1, 1, 0, 1]);
    drawClipMarker('RIGHT_RED', [0.5, 0, 0], [0.3, 0.3], [1, 0, 0, 1]);

    // Pixel readback
    gl.finish();
    const canvas = this.canvas;
    const cx = Math.floor(canvas.width / 2);
    const cy = Math.floor(canvas.height / 2);

    const pixel = new Uint8Array(4);
    gl.readPixels(cx, canvas.height - cy, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
    console.log(`Center pixel: RGB(${pixel[0]},${pixel[1]},${pixel[2]}) - expect YELLOW (255,255,0)`);

    const isYellow = pixel[0] > 200 && pixel[1] > 200 && pixel[2] < 50;
    console.log(`IDENTITY TEST: ${isYellow ? 'PASS - shader works!' : 'FAIL - shader broken!'}`);

    console.groupEnd();
  }

  /* Floor, back wall and side wall, so the office reads as a room with depth rather than
   * as furniture floating in black. Three quads; the cost is nil and the gain in
   * readability is the difference between "I can see the space" and "I cannot". */
  _drawRoomShell() {
    this.primitive = 'room_shell';
    const [rw, rd, rh] = this.scene.room;
    this._quad([rw / 2, rd / 2, 0], [rw, rd], [1, 0, 0], [0, 1, 0],
               this._colour('ink_900', 1), 0);
    this._quad([rw / 2, rd, rh / 2], [rw, rh], [1, 0, 0], [0, 0, 1],
               this._colour('ink_800', 0.75), 0);
    this._quad([0, rd / 2, rh / 2], [rd, rh], [0, 1, 0], [0, 0, 1],
               this._colour('ink_800', 0.55), 0);
    this._quad([rw, rd / 2, rh / 2], [rd, rh], [0, 1, 0], [0, 0, 1],
               this._colour('ink_800', 0.55), 0);
  }

  _colour(key, alpha) {
    const hex = this.scene.palette[key] || '#9aa1ad';
    return [
      parseInt(hex.slice(1, 3), 16) / 255,
      parseInt(hex.slice(3, 5), 16) / 255,
      parseInt(hex.slice(5, 7), 16) / 255,
      alpha === undefined ? 1 : alpha,
    ];
  }

  /* The camera's right and up vectors in world space, optionally rolled in-plane.
   *
   * Proof-mode character shapes are billboarded with these rather than laid in the world
   * XZ plane. Measured before: from `CAM_2`, the side profile, a head laid in XZ projects
   * nearly edge-on and covered 1 % of the frame — the shot showed a sliver of a man.
   * Billboarding is legitimate here because these shapes are generated rather than a
   * painted front view stretched across an angle it was not drawn for. A painted plate
   * gets the honest treatment instead: if it has no view for a camera, that camera says
   * so.
   *
   * `roll` applies the joint's own rotation inside the billboard plane, so a head tilt
   * reads from every angle rather than only from the front.
   */
  _billboard(roll) {
    const eye = this.camera.position, target = this.camera.target;
    const forward = [eye[0] - target[0], eye[1] - target[1], eye[2] - target[2]];
    const length = Math.hypot(forward[0], forward[1], forward[2]) || 1;
    for (let i = 0; i < 3; i++) forward[i] /= length;

    let up = [0, 0, 1];
    if (Math.abs(forward[2]) > 0.999) up = [0, 1, 0];
    const right = [
      up[1] * forward[2] - up[2] * forward[1],
      up[2] * forward[0] - up[0] * forward[2],
      up[0] * forward[1] - up[1] * forward[0],
    ];
    const rl = Math.hypot(right[0], right[1], right[2]) || 1;
    for (let i = 0; i < 3; i++) right[i] /= rl;
    const trueUp = [
      forward[1] * right[2] - forward[2] * right[1],
      forward[2] * right[0] - forward[0] * right[2],
      forward[0] * right[1] - forward[1] * right[0],
    ];

    if (!roll) return { right, up: trueUp };
    const c = Math.cos(roll), s = Math.sin(roll);
    return {
      right: [
        right[0] * c + trueUp[0] * s,
        right[1] * c + trueUp[1] * s,
        right[2] * c + trueUp[2] * s,
      ],
      up: [
        trueUp[0] * c - right[0] * s,
        trueUp[1] * c - right[1] * s,
        trueUp[2] * c - right[2] * s,
      ],
    };
  }

  /* Whether a world point is anywhere near the frame.
   *
   * Used to cull the city, which spans the whole room width while CAM_1 sees 39.6° of it.
   * Measured before adding this: 601 of 1191 draw calls were skyline windows and most
   * were off-frame — half the frame's cost thrown away. The margin is generous because
   * this culls a *centre* and the quad around it has extent; a tight bound would blink
   * towers out at the frame edge, which is far worse than the cost it saves.
   *
   * Deliberately not applied to the character or the desk: they are always on frame, and
   * a per-call projection to discover that is the cull costing more than it returns. */
  _nearFrame(point, margin) {
    const m = this.viewProj;
    if (!m) return true;
    const w = m[3] * point[0] + m[7] * point[1] + m[11] * point[2] + m[15];
    if (w <= 0) return false;                       // behind the camera
    const x = (m[0] * point[0] + m[4] * point[1] + m[8] * point[2] + m[12]) / w;
    const y = (m[1] * point[0] + m[5] * point[1] + m[9] * point[2] + m[13]) / w;
    const bound = 1 + (margin === undefined ? 0.35 : margin);
    return Math.abs(x) <= bound && Math.abs(y) <= bound;
  }

  /* Parallax from blockout depth, never eyeballed. ASSET_MANIFEST §2's rule applied to
   * the placeholder, so the runtime's parallax path is exercised with real numbers. */
  _parallaxFor(centre) {
    const eye = this.camera.position;
    const distance = Math.max(1, Math.hypot(
      centre[0] - eye[0], centre[1] - eye[1], centre[2] - eye[2]));
    const amount = this.parallax * (2248 / distance);
    return [this._drift[0] * amount * 120, this._drift[1] * amount * 120];
  }

  /* Every primitive in the scene goes through here, so this is the one place worth
   * validating. `this.primitive` is set by each draw pass before it calls in, so a
   * contract failure names the primitive and field rather than surfacing as an
   * unattributed Symbol.iterator error from inside a GL call. */
  _quad(centre, size, basisX, basisY, colour, shape, edge, options) {
    const gl = this.gl;
    if (STRICT_GEOMETRY) {
      const where = this.primitive || 'quad';
      expectVec(centre, 3, where, 'centre');
      expectVec(size, 2, where, 'size');
      expectVec(basisX, 3, where, 'basisX');
      expectVec(basisY, 3, where, 'basisY');
      expectVec(colour, 4, where, 'colour');
      if (options && options.colour2) {
        expectVec(options.colour2, 4, where, 'colour2');
      }
    }
    gl.uniform3fv(this.u.u_centre, centre);
    gl.uniform2fv(this.u.u_size, size);
    gl.uniform3fv(this.u.u_basisX, basisX);
    gl.uniform3fv(this.u.u_basisY, basisY);
    const parallax = this._parallaxFor(centre);
    if (STRICT_GEOMETRY) {
      expectVec(parallax, 2, this.primitive || 'quad', 'parallax');
    }
    gl.uniform2fv(this.u.u_parallax, parallax);
    gl.uniform4fv(this.u.u_colour, colour);
    /* Always uploaded, even when the shape ignores it: a stale `u_colour2` from the
     * previous draw call is how a gradient inherits an unrelated colour. */
    gl.uniform4fv(this.u.u_colour2, (options && options.colour2) || colour);
    gl.uniform1i(this.u.u_shape, shape);
    gl.uniform1f(this.u.u_edge, edge === undefined ? 0.03 : edge);
    const texture = options && options.texture;
    gl.uniform1i(this.u.u_useTex, texture ? 1 : 0);
    if (texture) {
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.uniform1i(this.u.u_tex, 0);
    }
    gl.drawArrays(gl.TRIANGLES, 0, 6);
    this.drawCalls++;
  }

  _drawBoxes() {
    this.primitive = 'box_face';
    for (const box of this.scene.boxes) {
      /* A carried prop travels with the hand that holds it. */
      const carried = this.propOffsets.get(box.id);
      const d = carried || [0, 0, 0];
      const [x0, y0, z0] = [box.min[0] + d[0], box.min[1] + d[1], box.min[2] + d[2]];
      const [x1, y1, z1] = [box.max[0] + d[0], box.max[1] + d[1], box.max[2] + d[2]];
      const colour = this._colour(
        carried ? 'gold_400' : box.colour,
        box.filled ? box.opacity : 0.85);
      const shape = box.filled ? 0 : 2;
      // Six faces as oriented quads. Enough to read the volume from any camera.
      const faces = [
        { c: [(x0+x1)/2, y0, (z0+z1)/2], s: [x1-x0, z1-z0], bx: [1,0,0], by: [0,0,1] },
        { c: [(x0+x1)/2, y1, (z0+z1)/2], s: [x1-x0, z1-z0], bx: [1,0,0], by: [0,0,1] },
        { c: [x0, (y0+y1)/2, (z0+z1)/2], s: [y1-y0, z1-z0], bx: [0,1,0], by: [0,0,1] },
        { c: [x1, (y0+y1)/2, (z0+z1)/2], s: [y1-y0, z1-z0], bx: [0,1,0], by: [0,0,1] },
        { c: [(x0+x1)/2, (y0+y1)/2, z1], s: [x1-x0, y1-y0], bx: [1,0,0], by: [0,1,0] },
      ];
      for (const face of faces) this._quad(face.c, face.s, face.bx, face.by, colour, shape, 0.02);
    }
  }

  _drawQuads(now) {
    this.primitive = 'panel';
    const advancing = this.screen?.honesty?.charts_advance !== false;
    for (const quad of this.scene.quads) {
      const yaw = quad.yaw * Math.PI / 180;
      const basisX = [Math.cos(yaw), Math.sin(yaw), 0];
      const basisY = [0, 0, 1];
      this._quad(quad.centre, [quad.width, quad.height], basisX, basisY,
                 this._colour(quad.colour, 0.96), 0);

      if (!quad.live) continue;
      const series = this.charts.get(quad.id);
      if (!series) continue;

      /* Charts advance only when the state says they may. When the feed is not
       * trustworthy they FREEZE and a marker is shown — a chart drawing invented candles
       * on a public stream is a fabricated price with extra steps. */
      const phase = advancing ? Math.floor(now / 2000) : 0;
      const bars = 26;
      const barWidth = quad.width / (bars * 1.7);
      for (let i = 0; i < bars; i++) {
        const value = series[(i + phase) % series.length];
        const up = value >= series[(i + phase + series.length - 1) % series.length];
        const height = Math.max(quad.height * 0.03, value * quad.height * 0.62);
        const offset = (i / (bars - 1) - 0.5) * quad.width * 0.86;
        const centre = [
          quad.centre[0] + basisX[0] * offset,
          quad.centre[1] + basisX[1] * offset,
          quad.centre[2] - quad.height * 0.14 + height / 2,
        ];
        // Green and red appear ONLY in chart marks. OFFICE_BIBLE §2.
        const colour = advancing
          ? this._colour(up ? 'green' : 'red', 0.9)
          : this._colour('ink_500', 0.5);
        this._quad(centre, [barWidth, height], basisX, basisY, colour, 0);
      }
    }
  }

  _worldFor(jointId) {
    /* Compose the world transform down the parent chain. This is the thing the brief
     * asks to be preserved: torso -> shoulder -> forearm -> hand, not independent
     * screen-space sprites. */
    const rest = this.scene.joints.find((j) => j.id === jointId);
    if (!rest) return null;
    let position = rest.position.slice();
    let rotation = 0;
    let node = rest;
    const guard = new Set();
    while (node && !guard.has(node.id)) {
      guard.add(node.id);
      const pose = this.pose.get(node.id);
      if (pose) {
        /* A parent's oscillation propagates to its children — a head nod carries the eyes
         * with it. The joint's OWN oscillation is excluded here and added after the reach
         * pin below, so it is not cancelled by arriving at an anchor. */
        const own = node.id === jointId;
        const osc = pose.osc || [0, 0, 0];
        position = [
          position[0] + pose.offset[0] + (own ? 0 : osc[0]),
          position[1] + pose.offset[1] + (own ? 0 : osc[1]),
          position[2] + pose.offset[2] + (own ? 0 : osc[2]),
        ];
        rotation += pose.rot;
      }
      node = node.parent ? this.scene.joints.find((j) => j.id === node.parent) : null;
    }

    /* A reach is absolute: blend from wherever the chain put this joint toward the frozen
     * anchor, so at full envelope the hand is ON the anchor whatever the body is doing.
     * Applied last, after the whole parent chain, for exactly that reason. */
    const own = this.pose.get(jointId);
    if (own?.reach) {
      const { target, envelope } = own.reach;
      position = [
        lerp(position[0], target[0], envelope),
        lerp(position[1], target[1], envelope),
        lerp(position[2], target[2], envelope),
      ];
    }
    const osc = own?.osc;
    if (osc) {
      position = [position[0] + osc[0], position[1] + osc[1], position[2] + osc[2]];
    }
    return { position, rotation };
  }

  /* Resolve the character's world transforms, breathing, blink and gaze.
   *
   * Separated from drawing so proof art and the blockout view share one animation path:
   * proof mode draws richer shapes at exactly these positions. A second resolver would be
   * a second thing to keep in step, and the first divergence would be invisible until
   * somebody noticed the blockout and the art disagreeing about where a hand was. */
  _resolveCharacter(now) {
    /* Breathing: chest and shoulders, ~4 s, always, under every action. Added after the
     * channels have been collapsed so it is never cancelled by a pose returning to rest —
     * a character who stops breathing during a blend-out is a corpse with good timing. */
    const breath = Math.sin(now / 4100 * Math.PI * 2);
    const chest = this.pose.get('torso');
    if (chest) chest.offset[2] += breath * 1.6;
    const shoulderBreath = Math.sin(now / 4100 * Math.PI * 2 - 0.4) * 1.1;
    for (const id of ['shoulder_l', 'shoulder_r']) {
      const joint = this.pose.get(id);
      if (joint) joint.offset[2] += shoulderBreath;
    }

    const blinkT = this.blink.duration
      ? clamp((now - this.blink.closing) / this.blink.duration, 0, 1) : 1;
    const lidClose = blinkT < 1 ? Math.sin(blinkT * Math.PI) : 0;

    const gazeT = clamp((now - this.gaze.started) / this.gaze.transit, 0, 1);
    const gazeEase = Ease.attention(gazeT);

    /* Resolve every joint's world transform including the gaze offsets. The limb segments
     * need both endpoints resolved before either is drawn, and an arm that does not follow
     * its own hand is the most obvious way a placeholder rig reads as broken. */
    const worlds = new Map();
    for (const joint of this.scene.joints) {
      const world = this._worldFor(joint.id);
      if (!world) continue;

      if (joint.id.startsWith('eye_')) {
        /* Gaze offset, eased. The eyes lead; the head follows for its declared share —
         * simultaneous onset is the clearest signal of a cheap rig. */
        const resolved = this.gaze.target
          ? this.scene.gaze_targets[this.gaze.target] : null;
        if (resolved) {
          const dx = clamp((resolved[0] - world.position[0]) / 2600, -1, 1);
          const dz = clamp((resolved[2] - world.position[2]) / 1800, -1, 1);
          world.position[0] += dx * 14 * gazeEase * (1 - this.gaze.head);
          world.position[2] += dz * 9 * gazeEase * (1 - this.gaze.head);
        }
      }
      if (joint.id === 'head' && this.gaze.target) {
        const resolved = this.scene.gaze_targets[this.gaze.target];
        if (resolved) {
          const dx = clamp((resolved[0] - world.position[0]) / 2600, -1, 1);
          const dz = clamp((resolved[2] - world.position[2]) / 1800, -1, 1);
          world.position[0] += dx * 46 * gazeEase * this.gaze.head;
          world.position[2] += dz * 16 * gazeEase * this.gaze.head;
        }
      }
      worlds.set(joint.id, world);
    }
    /* Lids ride their eye, which has just been moved by the gaze offset. Resolved after
     * the loop because a lid's parent is an eye and the eye's own offset is applied in
     * this pass rather than in the transform chain. */
    for (const side of ['l', 'r']) {
      const lid = worlds.get(`lid_${side}`), eye = worlds.get(`eye_${side}`);
      if (lid && eye) lid.position = eye.position.slice();
    }
    this.worlds = worlds;
    return { worlds, lidClose, gazeEase };
  }

  _drawCharacter(now, pose) {
    const { worlds, lidClose } = pose;

    // -- limb segments, drawn under the joints
    const limb = this._colour('ink_500', 1);
    for (const [a, b, width] of LIMB_SEGMENTS) {
      this.primitive = `limb:${a}->${b}`;
      const from = worlds.get(a), to = worlds.get(b);
      /* A segment needs exactly two resolved endpoints. A missing one means the joint
       * chain did not resolve — a real defect — so it is reported rather than skipped:
       * a silently dropped arm is a bug that ships. */
      if (!from || !to) {
        throw new ContractError({
          primitive: `limb:${a}->${b}`,
          field: !from ? a : b,
          expected: 'a resolved joint world transform',
          received: 'undefined (joint missing from the transform chain)',
          value: null,
        });
      }
      this._limb(from.position, to.position, width, limb);
    }

    // -- the joints themselves
    this.primitive = 'joint';
    for (const joint of this.scene.joints) {
      const world = worlds.get(joint.id);
      if (!world) continue;
      const yaw = world.rotation;
      const basisX = [Math.cos(yaw), Math.sin(yaw), 0];
      const basisY = [0, 0, 1];
      let size = joint.size.slice();
      let colour = this._colour(joint.colour, 1);

      if (joint.id.startsWith('lid_')) {
        size = [size[0], Math.max(1, size[1] * lidClose)];
        if (lidClose < 0.02) continue;
      }
      if (joint.id.startsWith('eye_')) colour = this._colour('ink_100', 1);
      /* Hands brighter than the limbs: they are what the viewer needs to track to judge
       * whether an interaction landed on its anchor. */
      if (joint.id.startsWith('hand_')) colour = this._colour('ink_300', 1);

      this._quad(world.position, size, basisX, basisY, colour,
                 joint.shape === 'ellipse' ? 1 : 0);
    }
  }

  /* A limb as one quad spanning two world points. Oriented along the segment, widened
   * across a perpendicular — enough to read as an arm, and it tracks the hand exactly
   * because both endpoints come from the same transform chain the hand does. */
  _limb(a, b, width, colour) {
    if (STRICT_GEOMETRY) {
      expectVec(a, 3, this.primitive || 'limb', 'from');
      expectVec(b, 3, this.primitive || 'limb', 'to');
    }
    const d = [b[0] - a[0], b[1] - a[1], b[2] - a[2]];
    const length = Math.hypot(d[0], d[1], d[2]);
    if (length < 1) return;
    const x = [d[0] / length, d[1] / length, d[2] / length];
    const up = Math.abs(x[2]) > 0.98 ? [1, 0, 0] : [0, 0, 1];
    const y = [
      x[1] * up[2] - x[2] * up[1],
      x[2] * up[0] - x[0] * up[2],
      x[0] * up[1] - x[1] * up[0],
    ];
    const yl = Math.hypot(y[0], y[1], y[2]) || 1;
    this._quad(
      [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2],
      [length, width], x, [y[0] / yl, y[1] / yl, y[2] / yl], colour, 0);
  }

  _drawDebug() {
    this.primitive = 'debug_marker';
    for (const [id, point] of Object.entries(this.scene.anchors)) {
      const active = this.actions.some((a) => a.anchor === id);
      this._quad(point, [46, 46], [1,0,0], [0,0,1],
                 this._colour(active ? 'gold_400' : 'ink_500', active ? 1 : 0.5), 1);
    }
    for (const [id, point] of Object.entries(this.scene.gaze_targets)) {
      const live = id === this.gaze.target;
      this._quad(point, live ? [92, 92] : [40, 40], [1,0,0], [0,0,1],
                 this._colour(live ? 'gold_500' : 'ink_600', live ? 0.95 : 0.35), 2, 0.14);
    }
  }

  /* Diagnostic logging for visibility verification.
   *
   * Reports canvas dimensions, viewport, camera projection, and samples pixel luminance
   * to verify the scene is actually being drawn with visible colors. */
  _logDiagnostics(now) {
    const gl = this.gl;
    const canvas = this.canvas;

    console.group(`[visual] FRAME ${this.frames} DIAGNOSTICS`);

    // Canvas and viewport
    console.log('Canvas:', {
      width: canvas.width,
      height: canvas.height,
      cssWidth: canvas.style.width,
      cssHeight: canvas.style.height,
      devicePixelRatio: window.devicePixelRatio,
    });

    const viewport = gl.getParameter(gl.VIEWPORT);
    console.log('GL Viewport:', {
      x: viewport[0],
      y: viewport[1],
      width: viewport[2],
      height: viewport[3],
    });

    // Camera info
    if (this.camera) {
      console.log('Camera:', {
        id: this.cameraId,
        position: this.camera.position,
        target: this.camera.target,
        vfov: this.camera.vfov,
        parallax: this.parallax,
      });
    }

    // Sample pixels from key regions
    const sample = (x, y, label) => {
      const pixels = new Uint8Array(4);
      gl.readPixels(x, canvas.height - y, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
      const luminance = (0.299 * pixels[0] + 0.587 * pixels[1] + 0.114 * pixels[2]) / 255;
      return { label, x, y, r: pixels[0], g: pixels[1], b: pixels[2], a: pixels[3], luminance };
    };

    const centerX = Math.floor(canvas.width / 2);
    const centerY = Math.floor(canvas.height / 2);

    const samples = [
      sample(centerX, centerY, 'center'),
      sample(centerX, centerY - 200, 'upper-center (head area)'),
      sample(centerX, centerY + 200, 'lower-center (desk area)'),
      sample(centerX - 400, centerY, 'left (character area)'),
      sample(centerX + 400, centerY, 'right (monitor area)'),
      sample(100, 100, 'top-left corner'),
      sample(canvas.width - 100, 100, 'top-right corner'),
    ];

    console.log('Pixel samples:');
    for (const s of samples) {
      console.log(`  ${s.label}: RGB(${s.r}, ${s.g}, ${s.b}) A=${s.a} lum=${s.luminance.toFixed(3)}`);
    }

    // Calculate average luminance
    const avgLum = samples.reduce((sum, s) => sum + s.luminance, 0) / samples.length;
    const maxLum = Math.max(...samples.map((s) => s.luminance));
    console.log(`Average luminance: ${avgLum.toFixed(3)}, Max: ${maxLum.toFixed(3)}`);

    if (maxLum < 0.05) {
      console.warn('⚠️ VERY LOW LUMINANCE - scene may appear black!');
    }

    // Scene info
    if (this.scene) {
      console.log('Scene:', {
        room: this.scene.room,
        boxes: this.scene.boxes.length,
        quads: this.scene.quads.length,
        joints: this.scene.joints.length,
        anchors: Object.keys(this.scene.anchors).length,
      });
    }

    console.log('Draw calls this frame:', this.drawCalls);

    // ===== MATRIX AUDIT FOR RENDER_TEST =====
    if (RENDER_TEST && this.viewProj && this.camera) {
      console.group('=== MATRIX AUDIT ===');

      // Print raw matrices
      console.log('viewProj (first 8 values):', Array.from(this.viewProj.slice(0, 8)).map(v => v.toFixed(4)));

      // Test points in world space (millimetres)
      const testPoints = {
        'room_origin': [0, 0, 0],
        'floor_center': [this.scene.room[0] / 2, this.scene.room[1] / 2, 0],
        'desk_center': [3200, 2600, 715],
        'head_center': [3200, 3000, 1375],
        'monitor_main': [3200, 2350, 1130],
      };

      console.log('Camera:', {
        position: this.camera.position,
        target: this.camera.target,
        vfov: this.camera.vfov,
      });

      // Compute forward direction
      const eye = this.camera.position;
      const tgt = this.camera.target;
      const fwd = [tgt[0] - eye[0], tgt[1] - eye[1], tgt[2] - eye[2]];
      const fwdLen = Math.hypot(fwd[0], fwd[1], fwd[2]);
      console.log('Forward vector:', fwd.map(v => (v / fwdLen).toFixed(3)));

      for (const [name, world] of Object.entries(testPoints)) {
        // Project through viewProj
        const m = this.viewProj;
        const x = m[0] * world[0] + m[4] * world[1] + m[8] * world[2] + m[12];
        const y = m[1] * world[0] + m[5] * world[1] + m[9] * world[2] + m[13];
        const z = m[2] * world[0] + m[6] * world[1] + m[10] * world[2] + m[14];
        const w = m[3] * world[0] + m[7] * world[1] + m[11] * world[2] + m[15];

        const ndc = w !== 0 ? [x / w, y / w, z / w] : [Infinity, Infinity, Infinity];
        const screenX = (ndc[0] + 1) / 2 * canvas.width;
        const screenY = (1 - ndc[1]) / 2 * canvas.height;

        const inViewX = ndc[0] >= -1 && ndc[0] <= 1;
        const inViewY = ndc[1] >= -1 && ndc[1] <= 1;
        const inViewZ = ndc[2] >= -1 && ndc[2] <= 1;

        console.log(`${name}:`, {
          world: world.map(v => v.toFixed(0)),
          clip: [x.toFixed(1), y.toFixed(1), z.toFixed(1), w.toFixed(1)],
          NDC: ndc.map(v => v.toFixed(3)),
          screen: [screenX.toFixed(0), screenY.toFixed(0)],
          W_positive: w > 0,
          in_view: `X:${inViewX} Y:${inViewY} Z:${inViewZ}`,
        });

        // CRITICAL: Check if W <= 0 (point behind camera)
        if (w <= 0) {
          console.error(`⚠️ ${name} has W=${w.toFixed(2)} - BEHIND CAMERA or invalid!`);
        }
      }

      console.groupEnd();
    }
    console.log('Contrast mode:', CONTRAST_MODE);
    console.log('Art mode:', ART_MODE);

    console.groupEnd();
  }

  // ---------------------------------------------------------- telemetry

  telemetry() {
    const sorted = this.frameTimes.slice().sort((a, b) => a - b);
    const pick = (q) => sorted.length ? sorted[Math.floor(sorted.length * q)] : 0;
    const mean = sorted.length
      ? sorted.reduce((a, b) => a + b, 0) / sorted.length : 0;
    let memory = null;
    if (this.memExt) {
      const info = this.memExt.getMemoryInfo();
      memory = (info?.memory?.total || 0) / (1024 * 1024);
    }
    return {
      at: new Date().toISOString(),
      frames_rendered: this.frames,
      fps_mean: mean ? 1000 / mean : 0,
      /* p05 of fps is p95 of frame time. A 24/7 stream's problem is never the average,
       * it is the periodic hitch a mean makes invisible. */
      fps_p05: pick(0.95) ? 1000 / pick(0.95) : 0,
      frame_time_p95_ms: pick(0.95),
      dropped_frames: this.dropped,
      gl_memory_mb: memory,
      resident_camera_stacks: this.cameraId ? [this.cameraId] : [],
      active_actions: this.actions.map((a) => a.id).slice(0, 8),
      queued_actions: this.actions.length,
      current_gaze: this.gaze.target,
      current_camera: this.cameraId,
      beat_phase: this.rhythm.phase,
      quality_profile: 'balanced',
      last_command_sequence: this.lastSequence || 0,
    };
  }
}

// ============================================================ HUD and controls

const el = (id) => document.getElementById(id);

/* The server's own view of itself, polled at 4 Hz.
 *
 * Deliberately not reconstructed in the browser from the command stream: interaction
 * locks, the camera's stated reason, the rhythm phase and the anti-repetition memory all
 * live in Python, and a HUD that inferred them would be describing a different system
 * from the one running. The renderer contributes only what it alone knows — frame cost. */
let serverState = null;
let assetState = null;

/* Imported-art status, polled slowly. It changes only when somebody copies a file in, so
 * once every ten seconds is generous; the point is that dropping a plate into the source
 * folder shows up in the HUD without a restart. */
async function pollAssets(runtime) {
  try {
    const response = await fetch('/api/visual/assets', { cache: 'no-store' });
    if (response.ok) {
      assetState = await response.json();
      /* Hand it straight to the library, which loads any newly-valid plate. This is
       * what makes "drop a file in and it appears" true without a restart — and nothing
       * upstream of here changes: the director, the camera director and the bridge are
       * not involved in art at all. */
      if (runtime && runtime.art) runtime.art.sync(assetState);
    }
  } catch (error) {
    assetState = null;
  }
}

/* The two full-screen notices. Driven from the library rather than from the poll, so
 * what they say matches what the renderer is actually able to draw. */
function updateArtNotices(runtime) {
  const noart = el('noart');
  const nocam = el('nocam');
  if (!runtime || !runtime.art) return;

  const counts = runtime.art.counts();
  const missing = !counts.anyImported;

  /* In proof mode, procedural anime art IS the intended demo - don't block it with a notice.
   * Only show the notice in 'final' mode when plates are expected but missing.
   * Blockout is the grey-box geometry check - no notice there either. */
  if (missing && ART_MODE === 'final') {
    noart.classList.add('shown');
    const set = (id, valid, required) => {
      const node = el(id);
      node.textContent = `${valid}/${required}`;
      node.className = 'value' + (valid === 0 ? '' : (valid < required ? ' part' : ' done'));
    };
    set('n-char', counts.character.valid, counts.character.required);
    set('n-env', counts.environment.valid, counts.environment.required);
    set('n-motion', counts.motion.valid, counts.motion.required);
    if (counts.sourceRoot) el('n-source').textContent = counts.sourceRoot;

    /* A file that is present but rejected is the most useful thing to say: somebody has
     * already tried, and the reason is one line away. */
    const problems = [];
    for (const entry of counts.problems) {
      problems.push(`${entry.filename}: ${entry.problems[0]}`);
    }
    for (const stray of counts.unexpected) problems.push(stray);
    el('n-problem').textContent = problems.length
      ? `REJECTED — ${problems.slice(0, 3).join(' · ')}` : '';
  } else {
    noart.classList.remove('shown');
  }

  const unavailable = runtime.art.cameraUnavailable(runtime.cameraId);
  if (unavailable) {
    nocam.classList.add('shown');
    el('nocam-detail').textContent =
      `No imported plate is painted for ${runtime.cameraId}. `
      + 'Stretching a plate drawn for another angle across it would be a fake view.';
  } else {
    nocam.classList.remove('shown');
  }
}

async function pollState() {
  try {
    const response = await fetch('/api/visual/state', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    serverState = await response.json();
  } catch (error) {
    serverState = null;
  }
}

const show = (id, value, fallback) => {
  const node = el(id);
  if (node) {
    node.textContent = (value === null || value === undefined || value === '')
      ? (fallback === undefined ? '—' : fallback) : value;
  }
};
const num = (value, digits) =>
  (value === null || value === undefined) ? null : Number(value).toFixed(digits ?? 1);

function updateHud(runtime) {
  const hud = el('hud');
  if (!view.hud) { hud.classList.add('off'); return; }
  hud.classList.remove('off');

  const t = runtime.telemetry();
  const s = serverState;

  // ---- renderer: the only panel the browser is the authority on
  show('p-fps', t.fps_mean.toFixed(1));
  show('p-p05', t.fps_p05.toFixed(1));
  show('p-p95', t.frame_time_p95_ms.toFixed(2) + ' ms');
  show('p-drop', t.dropped_frames);
  show('p-draws', runtime.drawCalls);
  show('p-layers', runtime.scene
    ? runtime.scene.boxes.length + runtime.scene.quads.length + runtime.scene.joints.length
    : null);
  show('p-mem', t.gl_memory_mb === null
    ? 'n/a (no extension)' : t.gl_memory_mb.toFixed(1) + ' MB');
  show('p-cap', FPS_CAP);

  const sock = el('p-sock');
  sock.textContent = runtime.socketState;
  sock.className = runtime.socketState === 'open' ? 'ok'
    : (runtime.socketState === 'closed' ? 'bad' : 'warn');

  const feed = el('p-state');
  feed.textContent = s
    ? `${s.mode} · ${s.renderers} client${s.renderers === 1 ? '' : 's'}`
    : 'unreachable';
  feed.className = s ? 'ok' : 'bad';

  // ---- character: the server is the authority
  const character = s && s.character;
  show('c-state', (character && character.state) || runtime.characterState);
  const action = runtime.actions.length
    ? runtime.actions[runtime.actions.length - 1] : null;
  show('c-action', (character && character.action) || (action ? action.id : null), 'still');
  show('c-chain', character && character.chain);
  show('c-gaze', (character && character.gaze) || runtime.gaze.target);
  show('c-blend', action ? `${action.blendIn}/${action.blendOut} ms` : null);
  show('c-queue', runtime.queued);
  const next = character ? num(character.next_action_in, 1) : null;
  show('c-next', next === null ? null : `${next} s`);

  const rhythm = s && s.rhythm;
  show('c-fatigue', (rhythm && num(rhythm.fatigue_level, 3)) || runtime.fatigue.toFixed(3));
  show('c-focus', rhythm && num(rhythm.focus_level, 3));
  show('c-phase', rhythm && rhythm.phase);

  const locks = el('c-locks');
  const held = ((character && character.locks) || []).filter((lock) => lock !== 'none');
  locks.textContent = held.length ? held.join(' ') : 'none';
  locks.className = held.length ? 'warn' : 'dim';

  // ---- camera
  const serverCamera = s && s.camera;
  if (runtime.camera) {
    const name = (serverCamera && serverCamera.name) || runtime.camera.name || '';
    show('k-id', `${runtime.cameraId}${name ? ' ' + name : ''}`);
    show('k-lens', `${runtime.camera.focal_mm_35eq} mm`);
    const heldFor = serverCamera ? num(serverCamera.held_for, 0) : null;
    show('k-held', heldFor === null
      ? ((performance.now() - (runtime.cameraSince || 0)) / 1000).toFixed(0) + ' s'
      : `${heldFor} s`);
    const target = serverCamera ? num(serverCamera.hold_target, 0) : null;
    show('k-target', target === null ? null : `${target} s`);
    show('k-reason', serverCamera && serverCamera.last_reason);
    show('k-trans', runtime.transition.kind);
    show('k-par', runtime.parallax.toFixed(4));

    if (serverCamera) {
      const eligible = el('k-elig');
      eligible.textContent = serverCamera.cut_eligible
        ? 'eligible' : `held ${serverCamera.seconds_to_eligible} s`;
      eligible.className = serverCamera.cut_eligible ? 'ok' : 'dim';

      const auto = el('k-auto');
      auto.textContent = serverCamera.auto ? 'AUTO' : 'MANUAL LOCK';
      auto.className = serverCamera.auto ? 'ok' : 'warn';
    }
  }

  // ---- market and music
  const market = s && s.market;
  const screen = runtime.screen;
  show('m-sym', (market && market.symbol) || (screen && screen.active_symbol));
  show('m-regime', market && market.regime);
  show('m-band', (s && s.drive && s.drive.intensity_band) || (screen && screen.band));
  show('m-energy', market && num(market.energy, 1));

  const velocity = el('m-vel');
  if (market && market.energy_velocity !== null && market.energy_velocity !== undefined) {
    const v = Number(market.energy_velocity);
    velocity.textContent = `${v > 0 ? '+' : ''}${v.toFixed(2)} /s`;
    velocity.className = Math.abs(v) > 4 ? 'warn' : '';
  } else {
    velocity.textContent = '—';
  }
  show('m-dir', market && market.direction);
  show('m-sal', market && num(market.salience, 3));

  const charts = el('m-charts');
  const advancing = !(screen && screen.honesty && screen.honesty.charts_advance === false);
  charts.textContent = screen
    ? (advancing ? 'advancing' : (screen.honesty.stale_marker || 'frozen')) : '—';
  charts.className = advancing ? 'ok' : 'bad';

  const music = s && s.music;
  show('u-bpm', (music && music.bpm) || 'silent');
  show('u-energy', music && num(music.energy, 2));
  show('u-genre', music && music.genre);
  show('u-nod', num(runtime.rhythm.nod, 2));
  show('u-max', `${runtime.rhythm.maxDeg.toFixed(2)}°`);

  // ---- anti-repetition
  const repeat = s && s.anti_repeat;
  show('r-actions', repeat && repeat.recent_action_count);
  show('r-last', repeat && repeat.recent_actions.slice(-1)[0]);
  show('r-cams', repeat ? `${repeat.recent_camera_count} distinct` : null);
  show('r-seq', repeat && repeat.recent_cameras.slice(-4).join(' ').replace(/CAM_/g, ''));

  // ---- imported art, and what proof mode is standing in for
  const artMode = el('a-mode');
  artMode.textContent = ART_MODE.toUpperCase();
  artMode.className = ART_MODE === 'proof' ? 'warn' : (ART_MODE === 'final' ? 'ok' : 'dim');
  if (assetState) {
    const character = assetState.character;
    const environment = assetState.environment;
    const motion = assetState.motion_layers;
    const tally = (node, valid, required) => {
      const target = el(node);
      target.textContent = `${valid} / ${required}`;
      target.className = valid === 0 ? 'bad' : (valid < required ? 'warn' : 'ok');
    };
    tally('a-char', character.required_valid, character.required);
    tally('a-env', environment.required_valid, environment.required);
    tally('a-motion', motion.valid, motion.required);
    const source = el('a-source');
    source.textContent = assetState.any_art_present
      ? assetState.source_root
      : 'none imported — procedural';
    source.className = assetState.any_art_present ? 'ok' : 'dim';
  }

  // ---- the demo timeline
  const demoPanel = el('demo');
  const demo = s && s.demo;
  if (!demo) {
    demoPanel.style.display = 'none';
  } else {
    demoPanel.style.display = '';
    show('d-phase', demo.headline);
    show('d-remain', `${demo.seconds_remaining.toFixed(0)} s`);
    show('d-cycle', `${demo.cycle + 1} · ${(demo.cycle_seconds / 60).toFixed(0)} min loop`);
    show('d-elapsed', `${(demo.elapsed / 60).toFixed(1)} min`);
    show('d-swaps', demo.symbol_changes);
    const over = el('d-over');
    const active = Object.entries(demo.overrides)
      .filter((pair) => pair[1]).map((pair) => `${pair[0]}=${pair[1]}`);
    over.textContent = active.length ? active.join(' ') : 'none';
    over.className = active.length ? 'warn' : 'dim';
  }
}

// ============================================================ debug overlay

/* Project a world point to screen pixels with the SAME matrix the geometry used. A debug
 * line drawn through a different projection is worse than no line at all, because it
 * looks authoritative while disagreeing with the picture. */
function project(runtime, point) {
  const m = runtime.viewProj;
  if (!m) return null;
  const x = m[0] * point[0] + m[4] * point[1] + m[8] * point[2] + m[12];
  const y = m[1] * point[0] + m[5] * point[1] + m[9] * point[2] + m[13];
  const w = m[3] * point[0] + m[7] * point[1] + m[11] * point[2] + m[15];
  if (w <= 0) return null;
  const rect = runtime.canvas.getBoundingClientRect();
  return [
    rect.left + (x / w + 1) / 2 * rect.width,
    rect.top + (1 - (y / w + 1) / 2) * rect.height,
  ];
}

const SVG_NS = 'http://www.w3.org/2000/svg';

function updateOverlay(runtime) {
  const svg = el('overlay');
  if (!view.debug || !runtime.scene) { svg.classList.add('off'); return; }
  svg.classList.remove('off');
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  const line = (a, b, colour, dash) => {
    const from = project(runtime, a), to = project(runtime, b);
    if (!from || !to) return;
    const node = document.createElementNS(SVG_NS, 'line');
    node.setAttribute('x1', from[0]); node.setAttribute('y1', from[1]);
    node.setAttribute('x2', to[0]);   node.setAttribute('y2', to[1]);
    node.setAttribute('stroke', colour);
    node.setAttribute('stroke-width', '1.1');
    if (dash) node.setAttribute('stroke-dasharray', dash);
    svg.appendChild(node);
  };
  const label = (point, text, live) => {
    const at = project(runtime, point);
    if (!at) return;
    const node = document.createElementNS(SVG_NS, 'text');
    node.setAttribute('x', at[0] + 6); node.setAttribute('y', at[1] - 4);
    if (live) node.setAttribute('class', 'live');
    node.textContent = text;
    svg.appendChild(node);
  };

  // -- eyes to the current gaze target. Verifies the gaze geometry resolves.
  const target = runtime.gaze.target
    ? runtime.scene.gaze_targets[runtime.gaze.target] : null;
  if (target) {
    for (const eye of ['eye_l', 'eye_r']) {
      const world = runtime.worlds && runtime.worlds.get(eye);
      if (world) line(world.position, target, 'rgba(201,162,39,.55)', '4 3');
    }
    label(target, runtime.gaze.target, true);
  }

  // -- each interacting hand to the anchor it was sent to
  for (const action of runtime.actions) {
    if (!action.anchor) continue;
    const anchor = runtime.scene.anchors[action.anchor];
    if (!anchor) continue;
    for (const hand of ['hand_l', 'hand_r']) {
      const world = runtime.worlds && runtime.worlds.get(hand);
      if (!world) continue;
      const distance = Math.hypot(
        world.position[0] - anchor[0], world.position[1] - anchor[1],
        world.position[2] - anchor[2]);
      /* Only the hand actually near it: drawing both would imply the director sent both,
       * which it did not. */
      if (distance < 420) line(world.position, anchor, 'rgba(74,127,194,.75)');
    }
    label(anchor, action.anchor.replace('ANCHOR_', ''), true);
  }

  // -- the furniture, named. This is what makes the blockout readable.
  for (const box of runtime.scene.boxes) {
    if (!box.label) continue;
    label([(box.min[0] + box.max[0]) / 2,
           (box.min[1] + box.max[1]) / 2,
           box.max[2]], box.label.toUpperCase(), false);
  }
  for (const joint of ['head', 'torso', 'hand_l', 'hand_r']) {
    const world = runtime.worlds && runtime.worlds.get(joint);
    if (world) label(world.position, joint.toUpperCase(), false);
  }
}

// ============================================================ control panel

/* Every button posts to the endpoint a production operator would use. Nothing here
 * reaches into the renderer to fake a movement: a press either produces a real director
 * action or reports why the director refused. That distinction is the whole value of the
 * panel — a button that always "worked" would tell you nothing about the locks. */
function wireControls(runtime) {
  const panel = el('controls');
  if (!view.demo) return;             // development only; never on a broadcast source
  panel.classList.remove('off');

  const result = el('result');
  const say = (text, kind) => {
    result.textContent = text;
    result.className = kind || '';
  };

  const post = async (path, describe) => {
    try {
      const response = await fetch(path, { method: 'POST' });
      const body = await response.json().catch(() => ({}));
      if (response.ok) {
        say(describe, 'ok');
        return body;
      }
      /* The case worth seeing. An action refused by an interaction lock is the safety
       * system working, so it is reported rather than swallowed. */
      const reason = body.reason || body.detail || `HTTP ${response.status}`;
      say(`BLOCKED: ${reason}`, 'blocked');
      return null;
    } catch (error) {
      say(`BLOCKED: ${error.message}`, 'blocked');
      return null;
    }
  };

  const mark = (selector, node) => {
    for (const button of panel.querySelectorAll(selector)) {
      button.classList.remove('active');
    }
    if (node) node.classList.add('active');
  };

  panel.addEventListener('click', async (event) => {
    const button = event.target.closest('button');
    if (!button) return;
    const d = button.dataset;

    if (d.trigger) {
      await post(`/api/visual/trigger/${d.trigger}`, `${d.trigger} — started`);
    } else if (d.market) {
      mark('[data-market]', button);
      await post(`/api/visual/demo/market/${d.market}`,
                 `market -> ${d.market.toUpperCase()}`);
    } else if (d.symbol) {
      mark('[data-symbol]', button);
      const body = await post(`/api/visual/demo/symbol/${d.symbol}`,
                              `symbol -> ${d.symbol}`);
      if (body) {
        say(`symbol -> ${d.symbol}\ncharacter NOT reset — still ${body.character_state}`,
            'ok');
      }
    } else if (d.music) {
      mark('[data-music]', button);
      await post(`/api/visual/demo/music/${d.music}`,
                 `music -> ${d.music.toUpperCase()} BPM`);
    } else if (d.camera === 'auto') {
      mark('[data-camera]', button);
      await post('/api/visual/camera/auto/true', 'camera -> AUTO');
    } else if (d.camera) {
      mark('[data-camera]', button);
      /* Lock first, then request: in the other order the director could take its own
       * next cut in the window between the two calls, and the viewer would see a camera
       * other than the one they pressed. */
      await post('/api/visual/camera/auto/false', 'manual lock');
      const queued = await post(`/api/visual/camera/${d.camera}`,
                                `${d.camera} requested`);
      if (queued) {
        /* Say when it will land. The 30 s minimum hold floor gates operator requests
         * too — deliberately, since bypassing it would mean the panel stopped
         * demonstrating production — and without the countdown a press that is simply
         * waiting is indistinguishable from one that was ignored. */
        const wait = serverState && serverState.camera
          ? serverState.camera.seconds_to_eligible : null;
        say(wait && wait > 0
          ? `${d.camera} requested — lands in ~${wait}s\n`
            + 'the 30s minimum hold floor applies to operator cuts too'
          : `${d.camera} requested — cutting now`, 'ok');
      }
    } else if (d.resume) {
      mark('[data-market]', null);
      mark('[data-symbol]', null);
      mark('[data-music]', null);
      await post('/api/visual/demo/resume', 'timeline resumed');
    } else if (d.toggle === 'hud') {
      view.hud = !view.hud;
      button.classList.toggle('active', !view.hud);
      say(`HUD ${view.hud ? 'on' : 'off'}`);
    } else if (d.toggle === 'debug') {
      view.debug = !view.debug;
      button.classList.toggle('active', view.debug);
      say(`debug lines ${view.debug ? 'on' : 'off'}`);
    }
  });
}

function fault(message) {
  const box = el('fault');
  box.textContent = message;
  box.classList.add('shown');
}

/* Everything known at the moment a draw failed.
 *
 * `object is not iterable` on its own cost an afternoon: it names neither the draw call
 * nor the field nor the type. This records the lot — exception, stack, frame, camera,
 * character state, action, chain step, gaze, carried prop, locks, market, BPM and the
 * layer ids in flight — and for a `ContractError` it states the primitive, field,
 * expected type and received type outright.
 *
 * Deliberately no textures, buffers or typed-array contents: a report nobody can read is
 * the same as no report. */
function describeDrawFailure(error, runtime) {
  const server = typeof serverState !== 'undefined' ? serverState : null;
  const contract = error instanceof ContractError || error?.name === 'ContractError';
  const action = runtime?.actions?.length
    ? runtime.actions[runtime.actions.length - 1] : null;

  return {
    error: {
      name: error?.name || 'Error',
      message: error?.message || String(error),
      stack: (error?.stack || '').split('\n').slice(0, 14).join('\n'),
    },
    contract: contract ? {
      primitive: error.primitive,
      field: error.field,
      expected: error.expected,
      received: error.received,
    } : null,
    renderer: {
      primitive: runtime?.primitive || null,
      frame: runtime?.frames ?? null,
      draw_calls_this_frame: runtime?.drawCalls ?? null,
      camera: runtime?.cameraId || null,
      camera_transition: runtime?.transition?.kind || null,
      strict_geometry: STRICT_GEOMETRY,
      debug_lines: view.debug,
    },
    character: {
      state: server?.character?.state || runtime?.characterState || null,
      action: action ? action.id : null,
      action_anchor: action ? action.anchor : null,
      action_chain: runtime?.chain || null,
      chain_step: server?.character?.chain ? server.character.chain : null,
      gaze: runtime?.gaze?.target || null,
      in_flight: (runtime?.actions || []).map((a) => a.id),
      carried_props: runtime?.propOffsets ? [...runtime.propOffsets.keys()] : [],
      locks: server?.character?.locks || [],
    },
    market: {
      symbol: server?.market?.symbol || runtime?.screen?.active_symbol || null,
      regime: server?.market?.regime || null,
      band: server?.drive?.intensity_band || null,
      bpm: server?.music?.bpm ?? runtime?.rhythm?.bpm ?? null,
    },
    /* Layer ids only, never their contents. Enough to say "which layer", small enough
     * to read in a console. */
    layers: runtime?.scene ? {
      boxes: runtime.scene.boxes.map((b) => b.id),
      quads: runtime.scene.quads.map((q) => q.id),
      joints: runtime.scene.joints.map((j) => j.id),
    } : null,
  };
}

/* The development error screen. States the four things that matter. */
function showDrawFailure(report) {
  const box = el('fault');
  const lines = ['RENDER ERROR', ''];
  if (report.contract) {
    lines.push(
      `primitive: ${report.contract.primitive}`,
      `field:     ${report.contract.field}`,
      `expected:  ${report.contract.expected}`,
      `received:  ${report.contract.received}`,
      '');
  } else {
    lines.push(`${report.error.name}: ${report.error.message}`, '');
    if (report.renderer.primitive) {
      lines.push(`primitive: ${report.renderer.primitive}`, '');
    }
  }
  lines.push(
    `action:    ${report.character.action || 'none'}`
      + (report.character.action_anchor ? ` -> ${report.character.action_anchor}` : ''),
    `chain:     ${report.character.action_chain || 'none'}`,
    `camera:    ${report.renderer.camera || 'none'}`,
    `state:     ${report.character.state || 'unknown'}`,
    `gaze:      ${report.character.gaze || 'none'}`,
    `props:     ${report.character.carried_props.join(', ') || 'none'}`,
    `frame:     ${report.renderer.frame}`,
    `debug:     ${report.renderer.debug_lines ? 'on' : 'off'}`,
    '',
    'Full report in the browser console: __tfDrawFailure');
  box.textContent = lines.join('\n');
  box.classList.add('shown');
}

// ============================================================ render pipeline diagnostics

/* STEP 2-3: Canvas and element stacking diagnostics */
function runCanvasDiagnostics() {
  const canvas = el('gl');
  const debugPanel = el('render-debug');
  if (!debugPanel) return;
  debugPanel.style.display = 'block';

  const rect = canvas.getBoundingClientRect();
  const computed = getComputedStyle(canvas);

  const lines = [
    '=== CANVAS DIAGNOSTICS ===',
    '',
    'canvas.width: ' + canvas.width,
    'canvas.height: ' + canvas.height,
    'canvas.clientWidth: ' + canvas.clientWidth,
    'canvas.clientHeight: ' + canvas.clientHeight,
    '',
    'getBoundingClientRect():',
    '  left: ' + rect.left.toFixed(1),
    '  top: ' + rect.top.toFixed(1),
    '  width: ' + rect.width.toFixed(1),
    '  height: ' + rect.height.toFixed(1),
    '',
    'devicePixelRatio: ' + window.devicePixelRatio,
    '',
    'Computed styles:',
    '  display: ' + computed.display,
    '  visibility: ' + computed.visibility,
    '  opacity: ' + computed.opacity,
    '  position: ' + computed.position,
    '  zIndex: ' + computed.zIndex,
    '',
  ];

  // STEP 3: Element stacking at center
  const centerX = rect.left + rect.width / 2;
  const centerY = rect.top + rect.height / 2;
  const elements = document.elementsFromPoint(centerX, centerY);

  lines.push('=== ELEMENT STACK AT CENTER ===');
  lines.push(`Point: (${centerX.toFixed(0)}, ${centerY.toFixed(0)})`);
  lines.push('');

  for (let i = 0; i < Math.min(elements.length, 10); i++) {
    const elem = elements[i];
    const style = getComputedStyle(elem);
    lines.push(`[${i}] <${elem.tagName.toLowerCase()}>`);
    lines.push(`    id: ${elem.id || '(none)'}`);
    lines.push(`    class: ${elem.className || '(none)'}`);
    lines.push(`    z-index: ${style.zIndex}`);
    lines.push(`    opacity: ${style.opacity}`);
    lines.push(`    background: ${style.backgroundColor}`);
  }

  debugPanel.textContent = lines.join('\n');
  console.log('[RENDER DIAG]', lines.join('\n'));

  return { rect, centerX, centerY, elements };
}

/* STEP 4-7: WebGL clear and triangle test */
function runWebGLDiagnostics(gl, canvas) {
  const debugPanel = el('render-debug');
  const lines = [];

  lines.push('');
  lines.push('=== WEBGL DIAGNOSTICS ===');

  // Check GL context
  lines.push('GL context: ' + (gl ? 'OK' : 'MISSING'));
  if (!gl) {
    if (debugPanel) debugPanel.textContent += '\n' + lines.join('\n');
    return null;
  }

  // Viewport
  const viewport = gl.getParameter(gl.VIEWPORT);
  lines.push('GL viewport: [' + viewport.join(', ') + ']');

  // Scissor
  const scissorEnabled = gl.isEnabled(gl.SCISSOR_TEST);
  const scissorBox = gl.getParameter(gl.SCISSOR_BOX);
  lines.push('Scissor enabled: ' + scissorEnabled);
  lines.push('Scissor box: [' + scissorBox.join(', ') + ']');

  // Depth test
  lines.push('Depth test: ' + gl.isEnabled(gl.DEPTH_TEST));

  // Blend
  lines.push('Blend: ' + gl.isEnabled(gl.BLEND));

  // Cull face
  lines.push('Cull face: ' + gl.isEnabled(gl.CULL_FACE));

  // GL error
  const error = gl.getError();
  lines.push('GL error: ' + (error === gl.NO_ERROR ? 'NONE' : error));

  // STEP 4: Magenta clear test
  lines.push('');
  lines.push('=== CLEAR TEST ===');
  gl.clearColor(1.0, 0.0, 1.0, 1.0);  // MAGENTA
  gl.clear(gl.COLOR_BUFFER_BIT);

  // STEP 7: Read pixels
  const pixels = new Uint8Array(4);
  const cx = Math.floor(canvas.width / 2);
  const cy = Math.floor(canvas.height / 2);
  gl.readPixels(cx, cy, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
  lines.push('Center pixel after MAGENTA clear: RGBA(' + pixels.join(', ') + ')');

  const isMagenta = pixels[0] > 200 && pixels[1] < 50 && pixels[2] > 200;
  lines.push('Clear test: ' + (isMagenta ? 'PASS - WebGL working!' : 'FAIL - pixels not magenta'));

  // Read corners
  const corners = [
    { name: 'top-left', x: 10, y: canvas.height - 10 },
    { name: 'top-right', x: canvas.width - 10, y: canvas.height - 10 },
    { name: 'bottom-left', x: 10, y: 10 },
    { name: 'bottom-right', x: canvas.width - 10, y: 10 },
  ];
  lines.push('');
  for (const corner of corners) {
    gl.readPixels(corner.x, corner.y, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    lines.push(corner.name + ': RGBA(' + pixels.join(', ') + ')');
  }

  if (debugPanel) debugPanel.textContent += '\n' + lines.join('\n');
  console.log('[RENDER DIAG]', lines.join('\n'));

  return { isMagenta };
}

/* STEP 6: Draw a simple yellow triangle */
function drawTestTriangle(gl, canvas) {
  const debugPanel = el('render-debug');
  const lines = ['', '=== TRIANGLE TEST ==='];

  // Clear to magenta first
  gl.clearColor(1.0, 0.0, 1.0, 1.0);
  gl.clear(gl.COLOR_BUFFER_BIT);

  // Minimal triangle shader
  const vsSource = `#version 300 es
    in vec2 a_pos;
    void main() {
      gl_Position = vec4(a_pos, 0.0, 1.0);
    }`;
  const fsSource = `#version 300 es
    precision mediump float;
    out vec4 fragColor;
    void main() {
      fragColor = vec4(1.0, 1.0, 0.0, 1.0);  // YELLOW
    }`;

  // Compile shaders
  const vs = gl.createShader(gl.VERTEX_SHADER);
  gl.shaderSource(vs, vsSource);
  gl.compileShader(vs);
  if (!gl.getShaderParameter(vs, gl.COMPILE_STATUS)) {
    lines.push('VS compile error: ' + gl.getShaderInfoLog(vs));
    if (debugPanel) debugPanel.textContent += '\n' + lines.join('\n');
    return;
  }
  lines.push('Vertex shader: compiled OK');

  const fs = gl.createShader(gl.FRAGMENT_SHADER);
  gl.shaderSource(fs, fsSource);
  gl.compileShader(fs);
  if (!gl.getShaderParameter(fs, gl.COMPILE_STATUS)) {
    lines.push('FS compile error: ' + gl.getShaderInfoLog(fs));
    if (debugPanel) debugPanel.textContent += '\n' + lines.join('\n');
    return;
  }
  lines.push('Fragment shader: compiled OK');

  const program = gl.createProgram();
  gl.attachShader(program, vs);
  gl.attachShader(program, fs);
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    lines.push('Link error: ' + gl.getProgramInfoLog(program));
    if (debugPanel) debugPanel.textContent += '\n' + lines.join('\n');
    return;
  }
  lines.push('Program: linked OK');

  gl.useProgram(program);

  // Giant triangle vertices in clip space (-1 to 1)
  const vertices = new Float32Array([
    0.0, 0.7,    // top
    -0.7, -0.5,  // bottom-left
    0.7, -0.5,   // bottom-right
  ]);

  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.STATIC_DRAW);

  const posLoc = gl.getAttribLocation(program, 'a_pos');
  gl.enableVertexAttribArray(posLoc);
  gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);

  // Disable depth/scissor for test
  gl.disable(gl.DEPTH_TEST);
  gl.disable(gl.SCISSOR_TEST);
  gl.disable(gl.CULL_FACE);

  // Draw
  gl.drawArrays(gl.TRIANGLES, 0, 3);
  lines.push('Triangle drawn');

  // Read center pixel - should be yellow
  const pixels = new Uint8Array(4);
  gl.readPixels(Math.floor(canvas.width / 2), Math.floor(canvas.height / 2),
                1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
  lines.push('Center pixel: RGBA(' + pixels.join(', ') + ')');

  const isYellow = pixels[0] > 200 && pixels[1] > 200 && pixels[2] < 50;
  lines.push('Triangle test: ' + (isYellow ? 'PASS - Triangle visible!' : 'FAIL - no yellow'));

  if (!isYellow) {
    // Check if it's still magenta (triangle not drawn)
    const isMagenta = pixels[0] > 200 && pixels[1] < 50 && pixels[2] > 200;
    if (isMagenta) {
      lines.push('Diagnosis: Magenta visible but triangle not. Check shader/draw call.');
    } else {
      lines.push('Diagnosis: Neither magenta nor yellow. Canvas may be occluded or wrong context.');
    }
  }

  if (debugPanel) debugPanel.textContent += '\n' + lines.join('\n');
  console.log('[RENDER DIAG]', lines.join('\n'));

  return { isYellow };
}

// ============================================================ frozen world marker diagnostic
/**
 * FROZEN DIAGNOSTIC MODE for world_marker
 * - Does NOT use requestAnimationFrame loop
 * - Draws once and stops
 * - Creates readback mirror canvas
 * - Measures actual bounding boxes
 * - Forces CAM_1
 * - Instruments gl.clear to detect later clears
 */
async function runFrozenWorldMarkerDiagnostic(mode) {
  console.log(`[FROZEN_DIAG] Starting frozen diagnostic mode: ${mode}`);

  // Fetch version info
  let buildInfo = { build_id: 'unknown', server_pid: 'unknown' };
  try {
    const resp = await fetch('/api/visual/version');
    if (resp.ok) buildInfo = await resp.json();
  } catch (e) {
    console.warn('[FROZEN_DIAG] Could not fetch version:', e);
  }
  console.log('[FROZEN_DIAG] Build:', buildInfo.build_id, 'PID:', buildInfo.server_pid);

  // Create frozen badge
  const badge = document.createElement('div');
  badge.id = 'frozen-badge';
  badge.style.cssText = `
    position: fixed; top: 50px; left: 50%; transform: translateX(-50%);
    background: #c00; color: #fff; padding: 10px 20px; font-size: 14px;
    font-family: monospace; z-index: 100001; border-radius: 4px;
  `;
  badge.innerHTML = `DIAGNOSTIC FRAME FROZEN<br>BUILD: ${buildInfo.build_id} PID: ${buildInfo.server_pid}`;
  document.body.appendChild(badge);

  // Hide all HUD elements
  const hud = el('hud');
  if (hud) hud.style.display = 'none';
  const controls = el('controls');
  if (controls) controls.style.display = 'none';
  const banner = el('banner');
  if (banner) banner.style.display = 'none';
  const stamp = el('stamp');
  if (stamp) stamp.style.display = 'none';

  // Initialize runtime
  let runtime;
  try {
    runtime = new Runtime(el('gl'));
    await runtime.loadScene();
  } catch (error) {
    fault('FROZEN DIAGNOSTIC FAILED\n\n' + (error && error.message ? error.message : error));
    return;
  }

  const gl = runtime.gl;
  const canvas = runtime.canvas;

  // Force CAM_1
  runtime.setCamera('CAM_1', 'cut', 0);
  console.log('[FROZEN_DIAG] Forced camera: CAM_1');

  // Instrument gl.clear to count calls after marker draw
  let clearCallsAfterMarker = 0;
  let markerDrawComplete = false;
  const originalClear = gl.clear.bind(gl);
  gl.clear = function(mask) {
    if (markerDrawComplete) {
      clearCallsAfterMarker++;
      console.warn('[FROZEN_DIAG] gl.clear called AFTER marker draw!', clearCallsAfterMarker);
    }
    return originalClear(mask);
  };

  // Clear to dark background
  gl.clearColor(0.02, 0.02, 0.05, 1);
  gl.clear(gl.COLOR_BUFFER_BIT);
  gl.viewport(0, 0, canvas.width, canvas.height);

  // Build view-projection matrix
  const aspect = TARGET_W / TARGET_H;
  const cam = runtime.camera;
  const view = mat4.lookAt(cam.position, cam.target);
  const proj = mat4.perspective(cam.vfov, aspect, 50, 60000);
  const viewProj = mode === 'world_marker_identity'
    ? mat4.identity()
    : mat4.multiply(proj, view);

  gl.uniformMatrix4fv(runtime.u.u_viewProj, false, viewProj);

  console.log('[FROZEN_DIAG] Camera position:', cam.position);
  console.log('[FROZEN_DIAG] Camera target:', cam.target);
  console.log('[FROZEN_DIAG] Camera vfov:', cam.vfov);

  // Helper to project world point to screen
  const projectToScreen = (world) => {
    const m = viewProj;
    const x = m[0]*world[0] + m[4]*world[1] + m[8]*world[2] + m[12];
    const y = m[1]*world[0] + m[5]*world[1] + m[9]*world[2] + m[13];
    const z = m[2]*world[0] + m[6]*world[1] + m[10]*world[2] + m[14];
    const w = m[3]*world[0] + m[7]*world[1] + m[11]*world[2] + m[15];
    if (w <= 0) return null;
    const ndcX = x / w, ndcY = y / w;
    const screenX = (ndcX + 1) / 2 * canvas.width;
    const screenY = (1 - ndcY) / 2 * canvas.height;
    return { screenX, screenY, ndcX, ndcY, w };
  };

  // Define marker positions - offset X to avoid occlusion
  // All markers should be visible without overlapping each other
  const markers = [
    { name: 'HEAD', world: [3200, 3000, 1375], color: [1, 1, 0, 1], worldSize: [400, 400] },  // YELLOW - center top
    { name: 'DESK', world: [2400, 2800, 900], color: [0, 1, 1, 1], worldSize: [400, 400] },   // CYAN - left side
    { name: 'MONITOR', world: [4000, 2800, 900], color: [1, 0, 0, 1], worldSize: [400, 400] }, // RED - right side
  ];

  // For identity mode, use clip-space positions
  if (mode === 'world_marker_identity') {
    markers[0].world = [0, 0.3, 0];
    markers[0].worldSize = [0.4, 0.4];
    markers[1].world = [-0.5, -0.3, 0];
    markers[1].worldSize = [0.4, 0.2];
    markers[2].world = [0.5, -0.3, 0];
    markers[2].worldSize = [0.4, 0.3];
  }

  // Draw HUGE markers
  console.group('[FROZEN_DIAG] Drawing markers');
  for (const marker of markers) {
    const projected = projectToScreen(marker.world);
    console.log(`${marker.name}: world=${marker.world}, screen=${projected ? `(${projected.screenX.toFixed(0)}, ${projected.screenY.toFixed(0)})` : 'BEHIND'}`);

    gl.uniform3fv(runtime.u.u_centre, marker.world);
    gl.uniform2fv(runtime.u.u_size, marker.worldSize);
    gl.uniform3fv(runtime.u.u_basisX, [1, 0, 0]);
    gl.uniform3fv(runtime.u.u_basisY, [0, 0, 1]);
    gl.uniform2fv(runtime.u.u_parallax, [0, 0]);
    gl.uniform4fv(runtime.u.u_colour, marker.color);
    gl.uniform4fv(runtime.u.u_colour2, marker.color);
    gl.uniform1i(runtime.u.u_shape, 0);
    gl.uniform1f(runtime.u.u_edge, 0);
    gl.uniform1i(runtime.u.u_useTex, 0);

    gl.drawArrays(gl.TRIANGLES, 0, 6);
  }
  console.groupEnd();

  // Mark drawing complete - any clears after this are bugs
  gl.finish();
  markerDrawComplete = true;

  // Full framebuffer readback
  const pixels = new Uint8Array(canvas.width * canvas.height * 4);
  gl.readPixels(0, 0, canvas.width, canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);

  // Analyze bounding boxes for each color
  const colorRanges = {
    HEAD: { color: [255, 255, 0], minX: Infinity, maxX: -1, minY: Infinity, maxY: -1, count: 0 },  // Yellow
    DESK: { color: [0, 255, 255], minX: Infinity, maxX: -1, minY: Infinity, maxY: -1, count: 0 },  // Cyan
    MONITOR: { color: [255, 0, 0], minX: Infinity, maxX: -1, minY: Infinity, maxY: -1, count: 0 },  // Red
  };

  const isColorMatch = (r, g, b, target, threshold = 50) =>
    Math.abs(r - target[0]) < threshold &&
    Math.abs(g - target[1]) < threshold &&
    Math.abs(b - target[2]) < threshold;

  for (let y = 0; y < canvas.height; y++) {
    for (let x = 0; x < canvas.width; x++) {
      const i = (y * canvas.width + x) * 4;
      const r = pixels[i], g = pixels[i + 1], b = pixels[i + 2];

      for (const [name, range] of Object.entries(colorRanges)) {
        if (isColorMatch(r, g, b, range.color)) {
          range.count++;
          range.minX = Math.min(range.minX, x);
          range.maxX = Math.max(range.maxX, x);
          range.minY = Math.min(range.minY, y);
          range.maxY = Math.max(range.maxY, y);
        }
      }
    }
  }

  // Report bounding boxes
  console.group('[FROZEN_DIAG] Marker Bounding Boxes');
  const results = {};
  for (const [name, range] of Object.entries(colorRanges)) {
    const width = range.maxX - range.minX + 1;
    const height = range.maxY - range.minY + 1;
    const pass = range.count > 0 && width >= 100 && height >= 100;
    results[name] = { ...range, width, height, pass };
    console.log(`${name}: pixels=${range.count}, bbox=[${range.minX},${range.minY}]-[${range.maxX},${range.maxY}], size=${width}x${height}, ${pass ? 'PASS' : 'FAIL'}`);
  }
  console.groupEnd();

  // Create readback mirror canvas
  const mirrorContainer = document.createElement('div');
  mirrorContainer.style.cssText = `
    position: fixed; top: 100px; right: 10px; background: #111; padding: 10px;
    border: 2px solid #0f0; z-index: 100000; font-family: monospace; color: #0f0;
  `;
  mirrorContainer.innerHTML = '<div style="margin-bottom:5px;">GPU READBACK MIRROR</div>';

  const mirrorCanvas = document.createElement('canvas');
  mirrorCanvas.width = canvas.width / 4;
  mirrorCanvas.height = canvas.height / 4;
  mirrorCanvas.style.cssText = 'border: 1px solid #0f0;';

  const ctx = mirrorCanvas.getContext('2d');
  const imageData = ctx.createImageData(canvas.width, canvas.height);

  // Flip vertically and copy
  for (let y = 0; y < canvas.height; y++) {
    for (let x = 0; x < canvas.width; x++) {
      const srcIdx = (y * canvas.width + x) * 4;
      const dstIdx = ((canvas.height - 1 - y) * canvas.width + x) * 4;
      imageData.data[dstIdx] = pixels[srcIdx];
      imageData.data[dstIdx + 1] = pixels[srcIdx + 1];
      imageData.data[dstIdx + 2] = pixels[srcIdx + 2];
      imageData.data[dstIdx + 3] = 255;
    }
  }

  // Draw scaled down
  const tempCanvas = document.createElement('canvas');
  tempCanvas.width = canvas.width;
  tempCanvas.height = canvas.height;
  tempCanvas.getContext('2d').putImageData(imageData, 0, 0);
  ctx.drawImage(tempCanvas, 0, 0, mirrorCanvas.width, mirrorCanvas.height);

  mirrorContainer.appendChild(mirrorCanvas);
  document.body.appendChild(mirrorContainer);

  // Check if mirror has colored pixels
  const mirrorData = ctx.getImageData(0, 0, mirrorCanvas.width, mirrorCanvas.height).data;
  let mirrorColoredPixels = 0;
  for (let i = 0; i < mirrorData.length; i += 4) {
    if (mirrorData[i] > 50 || mirrorData[i + 1] > 50 || mirrorData[i + 2] > 50) {
      mirrorColoredPixels++;
    }
  }
  const mirrorVisible = mirrorColoredPixels > 100;

  // Final report
  console.group('[FROZEN_DIAG] === FINAL REPORT ===');
  console.log('AUTHORITATIVE VISUAL SERVER:');
  console.log('  port: 8766');
  console.log('  PID:', buildInfo.server_pid);
  console.log('  BUILD_ID:', buildInfo.build_id);
  console.log('');
  console.log('normal RAF active during diagnostic: NO');
  console.log('clear_calls_after_marker:', clearCallsAfterMarker);
  console.log('');
  console.log('HEAD marker bbox:', results.HEAD.pass ? 'PASS' : 'FAIL',
    `(${results.HEAD.width}x${results.HEAD.height}, ${results.HEAD.count} pixels)`);
  console.log('DESK marker bbox:', results.DESK.pass ? 'PASS' : 'FAIL',
    `(${results.DESK.width}x${results.DESK.height}, ${results.DESK.count} pixels)`);
  console.log('MONITOR marker bbox:', results.MONITOR.pass ? 'PASS' : 'FAIL',
    `(${results.MONITOR.width}x${results.MONITOR.height}, ${results.MONITOR.count} pixels)`);
  console.log('');
  console.log('WebGL visible: check canvas');
  console.log('Readback mirror visible:', mirrorVisible ? 'YES' : 'NO');
  console.log('Mirror colored pixels:', mirrorColoredPixels);
  console.log('');

  const allPass = results.HEAD.pass && results.DESK.pass && results.MONITOR.pass;
  if (!allPass) {
    console.error('ROOT CAUSE: Markers not rendering at sufficient size');
    console.error('FIX: Check viewProj matrix and marker world positions');
  } else {
    console.log('ROOT CAUSE: N/A - all markers pass');
    console.log('FIX: N/A');
  }
  console.groupEnd();

  // Update badge with results
  badge.innerHTML = `
    DIAGNOSTIC FRAME FROZEN<br>
    BUILD: ${buildInfo.build_id}<br>
    HEAD: ${results.HEAD.pass ? '✓' : '✗'} ${results.HEAD.width}x${results.HEAD.height}<br>
    DESK: ${results.DESK.pass ? '✓' : '✗'} ${results.DESK.width}x${results.DESK.height}<br>
    MONITOR: ${results.MONITOR.pass ? '✓' : '✗'} ${results.MONITOR.width}x${results.MONITOR.height}<br>
    clears_after: ${clearCallsAfterMarker}
  `;
  badge.style.background = allPass ? '#090' : '#c00';

  // Store results globally for automated testing
  window.FROZEN_DIAG_RESULTS = {
    buildInfo,
    clearCallsAfterMarker,
    markers: results,
    mirrorVisible,
    mirrorColoredPixels,
    allPass,
  };

  console.log('[FROZEN_DIAG] Complete. Frame is frozen. No further draws will occur.');
}

// ============================================================ frozen room diagnostic

/**
 * FROZEN ROOM DIAGNOSTIC
 * Renders the room shell with bright debug colors through the production camera/matrix path.
 * Forces CAM_1, freezes the frame, measures pixel coverage.
 */
async function runFrozenRoomDiagnostic() {
  console.log('[ROOM_DIAG] Starting frozen room diagnostic');

  // Fetch version info
  let buildInfo = { build_id: 'unknown', server_pid: 'unknown' };
  try {
    const resp = await fetch('/api/visual/version');
    if (resp.ok) buildInfo = await resp.json();
  } catch (e) {
    console.warn('[ROOM_DIAG] Could not fetch version:', e);
  }
  console.log('[ROOM_DIAG] Build:', buildInfo.build_id, 'PID:', buildInfo.server_pid);

  // Create frozen badge
  const badge = document.createElement('div');
  badge.id = 'room-badge';
  badge.style.cssText = `
    position: fixed; top: 20px; left: 20px;
    background: #222; color: #0f0; padding: 10px 15px; font-size: 12px;
    font-family: monospace; z-index: 100001; border-radius: 4px; border: 2px solid #0f0;
  `;
  badge.innerHTML = `ROOM DIAGNOSTIC<br>BUILD: ${buildInfo.build_id}<br>Loading...`;
  document.body.appendChild(badge);

  // Hide all HUD elements
  const hud = el('hud');
  if (hud) hud.style.display = 'none';
  const controls = el('controls');
  if (controls) controls.style.display = 'none';
  const banner = el('banner');
  if (banner) banner.style.display = 'none';
  const stamp = el('stamp');
  if (stamp) stamp.style.display = 'none';

  // Initialize runtime
  let runtime;
  try {
    runtime = new Runtime(el('gl'));
    await runtime.loadScene();
  } catch (error) {
    fault('ROOM DIAGNOSTIC FAILED\n\n' + (error && error.message ? error.message : error));
    return;
  }

  const gl = runtime.gl;
  const canvas = runtime.canvas;

  // Use CAM_5 WIDE OFFICE for room geometry verification
  // (CAM_1 HERO FRONT has 22.9° FOV - too narrow to see floor/side walls)
  runtime.setCamera('CAM_5', 'cut', 0);
  console.log('[ROOM_DIAG] Using camera: CAM_5 (Wide office, 45.7° FOV)');
  console.log('[ROOM_DIAG] Camera position:', runtime.camera.position);
  console.log('[ROOM_DIAG] Camera target:', runtime.camera.target);
  console.log('[ROOM_DIAG] Camera vfov:', runtime.camera.vfov);

  // Get room dimensions
  const [rw, rd, rh] = runtime.scene.room;
  console.log('[ROOM_DIAG] Room dimensions:', { width: rw, depth: rd, height: rh });

  // Clear to dark background
  gl.clearColor(0.02, 0.02, 0.05, 1);
  gl.clear(gl.COLOR_BUFFER_BIT);
  gl.viewport(0, 0, canvas.width, canvas.height);

  // Build view-projection matrix
  const aspect = TARGET_W / TARGET_H;
  const cam = runtime.camera;
  const view = mat4.lookAt(cam.position, cam.target);
  const proj = mat4.perspective(cam.vfov, aspect, 50, 60000);
  const viewProj = mat4.multiply(proj, view);

  gl.uniformMatrix4fv(runtime.u.u_viewProj, false, viewProj);

  // Define room surfaces with DEBUG COLORS (no lighting, no transparency)
  // floor = RED, back wall = BLUE, left wall = GREEN, right wall = DARKER GREEN, ceiling = PURPLE
  const surfaces = [
    {
      name: 'FLOOR',
      centre: [rw / 2, rd / 2, 0],
      size: [rw, rd],
      basisX: [1, 0, 0],
      basisY: [0, 1, 0],
      color: [1.0, 0.2, 0.2, 1.0]  // RED
    },
    {
      name: 'BACK_WALL',
      centre: [rw / 2, rd, rh / 2],
      size: [rw, rh],
      basisX: [1, 0, 0],
      basisY: [0, 0, 1],
      color: [0.2, 0.4, 1.0, 1.0]  // BLUE
    },
    {
      name: 'LEFT_WALL',
      centre: [0, rd / 2, rh / 2],
      size: [rd, rh],
      basisX: [0, 1, 0],
      basisY: [0, 0, 1],
      color: [0.2, 0.8, 0.3, 1.0]  // GREEN
    },
    {
      name: 'RIGHT_WALL',
      centre: [rw, rd / 2, rh / 2],
      size: [rd, rh],
      basisX: [0, 1, 0],
      basisY: [0, 0, 1],
      color: [0.15, 0.5, 0.2, 1.0]  // DARKER GREEN
    },
    {
      name: 'CEILING',
      centre: [rw / 2, rd / 2, rh],
      size: [rw, rd],
      basisX: [1, 0, 0],
      basisY: [0, 1, 0],
      color: [0.6, 0.3, 0.8, 1.0]  // PURPLE
    },
  ];

  // Draw room surfaces
  console.group('[ROOM_DIAG] Drawing room surfaces');
  for (const surface of surfaces) {
    console.log(`${surface.name}: centre=${surface.centre}, size=${surface.size}`);

    gl.uniform3fv(runtime.u.u_centre, surface.centre);
    gl.uniform2fv(runtime.u.u_size, surface.size);
    gl.uniform3fv(runtime.u.u_basisX, surface.basisX);
    gl.uniform3fv(runtime.u.u_basisY, surface.basisY);
    gl.uniform2fv(runtime.u.u_parallax, [0, 0]);
    gl.uniform4fv(runtime.u.u_colour, surface.color);
    gl.uniform4fv(runtime.u.u_colour2, surface.color);
    gl.uniform1i(runtime.u.u_shape, 0);
    gl.uniform1f(runtime.u.u_edge, 0);
    gl.uniform1i(runtime.u.u_useTex, 0);

    gl.drawArrays(gl.TRIANGLES, 0, 6);
  }
  console.groupEnd();

  // Ensure drawing is complete
  gl.finish();

  // Full framebuffer readback
  const pixels = new Uint8Array(canvas.width * canvas.height * 4);
  gl.readPixels(0, 0, canvas.width, canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);

  // Define expected colors for each surface
  const colorRanges = {
    FLOOR: { color: [255, 51, 51], minX: Infinity, maxX: -1, minY: Infinity, maxY: -1, count: 0 },  // Red
    BACK_WALL: { color: [51, 102, 255], minX: Infinity, maxX: -1, minY: Infinity, maxY: -1, count: 0 },  // Blue
    LEFT_WALL: { color: [51, 204, 77], minX: Infinity, maxX: -1, minY: Infinity, maxY: -1, count: 0 },  // Green
    RIGHT_WALL: { color: [38, 128, 51], minX: Infinity, maxX: -1, minY: Infinity, maxY: -1, count: 0 },  // Dark Green
    CEILING: { color: [153, 77, 204], minX: Infinity, maxX: -1, minY: Infinity, maxY: -1, count: 0 },  // Purple
  };

  const isColorMatch = (r, g, b, target, threshold = 60) =>
    Math.abs(r - target[0]) < threshold &&
    Math.abs(g - target[1]) < threshold &&
    Math.abs(b - target[2]) < threshold;

  // Count all non-background pixels
  let totalColoredPixels = 0;
  let minX = Infinity, maxX = -1, minY = Infinity, maxY = -1;

  for (let y = 0; y < canvas.height; y++) {
    for (let x = 0; x < canvas.width; x++) {
      const i = (y * canvas.width + x) * 4;
      const r = pixels[i], g = pixels[i + 1], b = pixels[i + 2];

      // Check if not background (dark blue/black)
      if (r > 20 || g > 20 || b > 30) {
        totalColoredPixels++;
        minX = Math.min(minX, x);
        maxX = Math.max(maxX, x);
        minY = Math.min(minY, y);
        maxY = Math.max(maxY, y);

        // Identify which surface
        for (const [name, range] of Object.entries(colorRanges)) {
          if (isColorMatch(r, g, b, range.color)) {
            range.count++;
            range.minX = Math.min(range.minX, x);
            range.maxX = Math.max(range.maxX, x);
            range.minY = Math.min(range.minY, y);
            range.maxY = Math.max(range.maxY, y);
          }
        }
      }
    }
  }

  // Calculate coverage
  const totalPixels = canvas.width * canvas.height;
  const coveragePercent = (totalColoredPixels / totalPixels * 100).toFixed(1);
  const roomBbox = {
    x: minX,
    y: canvas.height - maxY - 1,  // Flip Y for screen coords
    width: maxX - minX + 1,
    height: maxY - minY + 1,
  };

  // Count visible walls
  let visibleWalls = 0;
  const surfaceResults = {};
  for (const [name, range] of Object.entries(colorRanges)) {
    const width = range.count > 0 ? range.maxX - range.minX + 1 : 0;
    const height = range.count > 0 ? range.maxY - range.minY + 1 : 0;
    const pass = range.count > 1000;  // Require substantial pixel count
    surfaceResults[name] = { ...range, width, height, pass };
    if (pass) visibleWalls++;
    console.log(`[ROOM_DIAG] ${name}: pixels=${range.count}, bbox=${width}x${height}, ${pass ? 'VISIBLE' : 'NOT VISIBLE'}`);
  }

  // Determine pass/fail
  // Must see floor, back wall, and at least one side wall
  const floorVisible = surfaceResults.FLOOR.pass;
  const backWallVisible = surfaceResults.BACK_WALL.pass;
  const leftWallVisible = surfaceResults.LEFT_WALL.pass;
  const rightWallVisible = surfaceResults.RIGHT_WALL.pass;
  const sideWallsVisible = leftWallVisible || rightWallVisible;
  const substantialCoverage = parseFloat(coveragePercent) > 15;  // At least 15% coverage

  const allPass = floorVisible && backWallVisible && sideWallsVisible && substantialCoverage;

  // Create readback mirror canvas
  const mirrorContainer = document.createElement('div');
  mirrorContainer.style.cssText = `
    position: fixed; top: 20px; right: 20px; background: #111; padding: 10px;
    border: 2px solid #0f0; z-index: 100000; font-family: monospace; color: #0f0;
  `;
  mirrorContainer.innerHTML = '<div style="margin-bottom:5px;">GPU READBACK MIRROR</div>';

  const mirrorCanvas = document.createElement('canvas');
  mirrorCanvas.width = canvas.width / 4;
  mirrorCanvas.height = canvas.height / 4;
  mirrorCanvas.style.cssText = 'border: 1px solid #0f0;';

  const ctx = mirrorCanvas.getContext('2d');
  const imageData = ctx.createImageData(canvas.width, canvas.height);

  // Flip vertically and copy
  for (let y = 0; y < canvas.height; y++) {
    for (let x = 0; x < canvas.width; x++) {
      const srcIdx = (y * canvas.width + x) * 4;
      const dstIdx = ((canvas.height - 1 - y) * canvas.width + x) * 4;
      imageData.data[dstIdx] = pixels[srcIdx];
      imageData.data[dstIdx + 1] = pixels[srcIdx + 1];
      imageData.data[dstIdx + 2] = pixels[srcIdx + 2];
      imageData.data[dstIdx + 3] = 255;
    }
  }

  // Draw scaled down
  const tempCanvas = document.createElement('canvas');
  tempCanvas.width = canvas.width;
  tempCanvas.height = canvas.height;
  tempCanvas.getContext('2d').putImageData(imageData, 0, 0);
  ctx.drawImage(tempCanvas, 0, 0, mirrorCanvas.width, mirrorCanvas.height);

  mirrorContainer.appendChild(mirrorCanvas);
  document.body.appendChild(mirrorContainer);

  // Update badge
  badge.innerHTML = `
    ROOM DIAGNOSTIC<br>
    BUILD: ${buildInfo.build_id}<br>
    FLOOR: ${floorVisible ? '✓' : '✗'} ${surfaceResults.FLOOR.count} px<br>
    BACK_WALL: ${backWallVisible ? '✓' : '✗'} ${surfaceResults.BACK_WALL.count} px<br>
    LEFT_WALL: ${leftWallVisible ? '✓' : '✗'} ${surfaceResults.LEFT_WALL.count} px<br>
    RIGHT_WALL: ${rightWallVisible ? '✓' : '✗'} ${surfaceResults.RIGHT_WALL.count} px<br>
    CEILING: ${surfaceResults.CEILING.pass ? '✓' : '✗'} ${surfaceResults.CEILING.count} px<br>
    Coverage: ${coveragePercent}%<br>
    Bbox: ${roomBbox.width}x${roomBbox.height}
  `;
  badge.style.background = allPass ? '#090' : '#c00';
  badge.style.borderColor = allPass ? '#0f0' : '#f00';

  // Store results globally
  window.FROZEN_ROOM_RESULTS = {
    buildInfo,
    surfaces: surfaceResults,
    totalColoredPixels,
    coveragePercent: parseFloat(coveragePercent),
    roomBbox,
    visibleWalls,
    floorVisible,
    backWallVisible,
    sideWallsVisible,
    substantialCoverage,
    allPass,
  };

  console.log('[ROOM_DIAG] === FINAL REPORT ===');
  console.log('FLOOR:', floorVisible ? 'VISIBLE' : 'NOT VISIBLE', surfaceResults.FLOOR.count, 'pixels');
  console.log('BACK_WALL:', backWallVisible ? 'VISIBLE' : 'NOT VISIBLE', surfaceResults.BACK_WALL.count, 'pixels');
  console.log('LEFT_WALL:', leftWallVisible ? 'VISIBLE' : 'NOT VISIBLE', surfaceResults.LEFT_WALL.count, 'pixels');
  console.log('RIGHT_WALL:', rightWallVisible ? 'VISIBLE' : 'NOT VISIBLE', surfaceResults.RIGHT_WALL.count, 'pixels');
  console.log('CEILING:', surfaceResults.CEILING.pass ? 'VISIBLE' : 'NOT VISIBLE', surfaceResults.CEILING.count, 'pixels');
  console.log('Coverage:', coveragePercent + '%');
  console.log('Room bbox:', roomBbox);
  console.log('OVERALL:', allPass ? 'PASS' : 'FAIL');
  console.log('[ROOM_DIAG] Complete. Frame is frozen.');
}

// ============================================================ frozen desk diagnostic

/**
 * FROZEN DESK DIAGNOSTIC
 * Renders room + desk + monitors with bright debug colors.
 * Uses CAM_1 (Hero front) to show the desk area.
 */
async function runFrozenDeskDiagnostic() {
  console.log('[DESK_DIAG] Starting frozen desk diagnostic');

  // Fetch version info
  let buildInfo = { build_id: 'unknown', server_pid: 'unknown' };
  try {
    const resp = await fetch('/api/visual/version');
    if (resp.ok) buildInfo = await resp.json();
  } catch (e) {
    console.warn('[DESK_DIAG] Could not fetch version:', e);
  }
  console.log('[DESK_DIAG] Build:', buildInfo.build_id, 'PID:', buildInfo.server_pid);

  // Create diagnostic badge
  const badge = document.createElement('div');
  badge.id = 'desk-badge';
  badge.style.cssText = `
    position: fixed; top: 20px; left: 20px;
    background: #222; color: #0f0; padding: 10px 15px; font-size: 11px;
    font-family: monospace; z-index: 100001; border-radius: 4px; border: 2px solid #0f0;
  `;
  badge.innerHTML = `DESK DIAGNOSTIC<br>Loading...`;
  document.body.appendChild(badge);

  // Hide all HUD elements
  for (const id of ['hud', 'controls', 'banner', 'stamp']) {
    const elem = el(id);
    if (elem) elem.style.display = 'none';
  }

  // Initialize runtime
  let runtime;
  try {
    runtime = new Runtime(el('gl'));
    await runtime.loadScene();
  } catch (error) {
    fault('DESK DIAGNOSTIC FAILED\n\n' + (error && error.message ? error.message : error));
    return;
  }

  const gl = runtime.gl;
  const canvas = runtime.canvas;

  // Use CAM_6 HANDS/DESK for desk view (looks down at desk items)
  runtime.setCamera('CAM_6', 'cut', 0);
  console.log('[DESK_DIAG] Using camera: CAM_6 (Hands/desk)');

  // Clear to dark background
  gl.clearColor(0.02, 0.02, 0.05, 1);
  gl.clear(gl.COLOR_BUFFER_BIT);
  gl.viewport(0, 0, canvas.width, canvas.height);

  // Build view-projection matrix
  const aspect = TARGET_W / TARGET_H;
  const cam = runtime.camera;
  const view = mat4.lookAt(cam.position, cam.target);
  const proj = mat4.perspective(cam.vfov, aspect, 50, 60000);
  const viewProj = mat4.multiply(proj, view);
  gl.uniformMatrix4fv(runtime.u.u_viewProj, false, viewProj);

  // Get room dimensions for room shell
  const [rw, rd, rh] = runtime.scene.room;

  // Define debug colors
  const COLORS = {
    // Room
    FLOOR: [0.15, 0.1, 0.1, 1.0],       // Dark red-ish (subtle)
    BACK_WALL: [0.1, 0.15, 0.2, 1.0],   // Dark blue-ish (subtle)
    // Desk items
    DESK: [0.6, 0.4, 0.2, 1.0],         // BROWN
    DESK_MAT: [0.3, 0.3, 0.3, 1.0],     // DARK GREY
    KEYBOARD: [0.75, 0.75, 0.75, 1.0],  // LIGHT GREY
    MOUSE: [0.9, 0.9, 0.9, 1.0],        // WHITE/GREY
    MUG: [1.0, 0.5, 0.0, 1.0],          // ORANGE
    NOTEBOOK: [0.95, 0.9, 0.8, 1.0],    // CREAM
    // Monitors
    MON_MAIN: [0.0, 0.5, 1.0, 1.0],     // ELECTRIC BLUE
    MON_SECONDARY: [0.0, 0.9, 0.9, 1.0], // CYAN
  };

  // Helper to draw a box surface (top face)
  const drawBoxTop = (box, color) => {
    const [x0, y0, z0] = box.min;
    const [x1, y1, z1] = box.max;
    const cx = (x0 + x1) / 2;
    const cy = (y0 + y1) / 2;
    const w = x1 - x0;
    const d = y1 - y0;

    gl.uniform3fv(runtime.u.u_centre, [cx, cy, z1]);
    gl.uniform2fv(runtime.u.u_size, [w, d]);
    gl.uniform3fv(runtime.u.u_basisX, [1, 0, 0]);
    gl.uniform3fv(runtime.u.u_basisY, [0, 1, 0]);
    gl.uniform2fv(runtime.u.u_parallax, [0, 0]);
    gl.uniform4fv(runtime.u.u_colour, color);
    gl.uniform4fv(runtime.u.u_colour2, color);
    gl.uniform1i(runtime.u.u_shape, 0);
    gl.uniform1f(runtime.u.u_edge, 0);
    gl.uniform1i(runtime.u.u_useTex, 0);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
  };

  // Helper to draw a quad (monitor/panel)
  const drawQuad = (centre, width, height, yaw, color) => {
    const yawRad = yaw * Math.PI / 180;
    const bx = [Math.cos(yawRad), Math.sin(yawRad), 0];

    gl.uniform3fv(runtime.u.u_centre, centre);
    gl.uniform2fv(runtime.u.u_size, [width, height]);
    gl.uniform3fv(runtime.u.u_basisX, bx);
    gl.uniform3fv(runtime.u.u_basisY, [0, 0, 1]);
    gl.uniform2fv(runtime.u.u_parallax, [0, 0]);
    gl.uniform4fv(runtime.u.u_colour, color);
    gl.uniform4fv(runtime.u.u_colour2, color);
    gl.uniform1i(runtime.u.u_shape, 0);
    gl.uniform1f(runtime.u.u_edge, 0);
    gl.uniform1i(runtime.u.u_useTex, 0);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
  };

  // Helper to draw a wall quad
  const drawWall = (centre, size, basisX, basisY, color) => {
    gl.uniform3fv(runtime.u.u_centre, centre);
    gl.uniform2fv(runtime.u.u_size, size);
    gl.uniform3fv(runtime.u.u_basisX, basisX);
    gl.uniform3fv(runtime.u.u_basisY, basisY);
    gl.uniform2fv(runtime.u.u_parallax, [0, 0]);
    gl.uniform4fv(runtime.u.u_colour, color);
    gl.uniform4fv(runtime.u.u_colour2, color);
    gl.uniform1i(runtime.u.u_shape, 0);
    gl.uniform1f(runtime.u.u_edge, 0);
    gl.uniform1i(runtime.u.u_useTex, 0);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
  };

  console.group('[DESK_DIAG] Drawing scene');

  // 1. Draw room shell (subtle background)
  drawWall([rw / 2, rd / 2, 0], [rw, rd], [1, 0, 0], [0, 1, 0], COLORS.FLOOR);
  drawWall([rw / 2, rd, rh / 2], [rw, rh], [1, 0, 0], [0, 0, 1], COLORS.BACK_WALL);
  console.log('Room shell drawn');

  // 2. Draw desk (BROWN)
  const desk = runtime.scene.boxes.find(b => b.id === 'DESK');
  if (desk) {
    drawBoxTop(desk, COLORS.DESK);
    console.log('DESK: drawn (BROWN)');
  }

  // 3. Draw desk items
  const items = [
    { id: 'KEYBOARD', color: COLORS.KEYBOARD, name: 'Keyboard (LIGHT GREY)' },
    { id: 'MOUSE', color: COLORS.MOUSE, name: 'Mouse (WHITE/GREY)' },
    { id: 'MUG', color: COLORS.MUG, name: 'Mug (ORANGE)' },
    { id: 'NOTEBOOK', color: COLORS.NOTEBOOK, name: 'Notebook (CREAM)' },
  ];

  for (const item of items) {
    const box = runtime.scene.boxes.find(b => b.id === item.id);
    if (box) {
      drawBoxTop(box, item.color);
      console.log(`${item.id}: drawn (${item.name})`);
    }
  }

  // 4. Draw monitors
  for (const quad of runtime.scene.quads) {
    if (quad.role === 'window') continue;  // Skip window
    const isMain = quad.id === 'MON_1';
    const color = isMain ? COLORS.MON_MAIN : COLORS.MON_SECONDARY;
    drawQuad(quad.centre, quad.width, quad.height, quad.yaw || 0, color);
    console.log(`${quad.id}: drawn (${isMain ? 'ELECTRIC BLUE' : 'CYAN'})`);
  }

  console.groupEnd();

  // Ensure drawing is complete
  gl.finish();

  // Full framebuffer readback
  const pixels = new Uint8Array(canvas.width * canvas.height * 4);
  gl.readPixels(0, 0, canvas.width, canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);

  // Analyze pixel content
  const colorCounts = {
    DESK: 0,
    KEYBOARD: 0,
    MOUSE: 0,
    MUG: 0,
    NOTEBOOK: 0,
    MON_MAIN: 0,
    MON_SECONDARY: 0,
    BACKGROUND: 0,
  };

  const isMatch = (r, g, b, color, threshold = 40) =>
    Math.abs(r - color[0] * 255) < threshold &&
    Math.abs(g - color[1] * 255) < threshold &&
    Math.abs(b - color[2] * 255) < threshold;

  let totalColored = 0;
  for (let i = 0; i < pixels.length; i += 4) {
    const r = pixels[i], g = pixels[i + 1], b = pixels[i + 2];

    if (isMatch(r, g, b, COLORS.DESK)) colorCounts.DESK++;
    else if (isMatch(r, g, b, COLORS.KEYBOARD)) colorCounts.KEYBOARD++;
    else if (isMatch(r, g, b, COLORS.MOUSE)) colorCounts.MOUSE++;
    else if (isMatch(r, g, b, COLORS.MUG)) colorCounts.MUG++;
    else if (isMatch(r, g, b, COLORS.NOTEBOOK)) colorCounts.NOTEBOOK++;
    else if (isMatch(r, g, b, COLORS.MON_MAIN)) colorCounts.MON_MAIN++;
    else if (isMatch(r, g, b, COLORS.MON_SECONDARY)) colorCounts.MON_SECONDARY++;
    else if (r > 30 || g > 30 || b > 40) totalColored++;
  }

  // Determine visibility
  const results = {
    DESK: { pixels: colorCounts.DESK, visible: colorCounts.DESK > 1000 },
    KEYBOARD: { pixels: colorCounts.KEYBOARD, visible: colorCounts.KEYBOARD > 100 },
    MOUSE: { pixels: colorCounts.MOUSE, visible: colorCounts.MOUSE > 50 },
    MUG: { pixels: colorCounts.MUG, visible: colorCounts.MUG > 100 },
    NOTEBOOK: { pixels: colorCounts.NOTEBOOK, visible: colorCounts.NOTEBOOK > 100 },
    MON_MAIN: { pixels: colorCounts.MON_MAIN, visible: colorCounts.MON_MAIN > 1000 },
    MON_SECONDARY: { pixels: colorCounts.MON_SECONDARY, visible: colorCounts.MON_SECONDARY > 1000 },
  };

  // Count visible items
  const visibleCount = Object.values(results).filter(r => r.visible).length;
  const allPass = results.DESK.visible && results.MON_MAIN.visible && visibleCount >= 5;

  // Create readback mirror
  const mirrorContainer = document.createElement('div');
  mirrorContainer.style.cssText = `
    position: fixed; top: 20px; right: 20px; background: #111; padding: 10px;
    border: 2px solid #0f0; z-index: 100000; font-family: monospace; color: #0f0;
  `;
  mirrorContainer.innerHTML = '<div style="margin-bottom:5px;">GPU READBACK MIRROR</div>';

  const mirrorCanvas = document.createElement('canvas');
  mirrorCanvas.width = canvas.width / 4;
  mirrorCanvas.height = canvas.height / 4;
  mirrorCanvas.style.cssText = 'border: 1px solid #0f0;';

  const ctx = mirrorCanvas.getContext('2d');
  const imageData = ctx.createImageData(canvas.width, canvas.height);

  for (let y = 0; y < canvas.height; y++) {
    for (let x = 0; x < canvas.width; x++) {
      const srcIdx = (y * canvas.width + x) * 4;
      const dstIdx = ((canvas.height - 1 - y) * canvas.width + x) * 4;
      imageData.data[dstIdx] = pixels[srcIdx];
      imageData.data[dstIdx + 1] = pixels[srcIdx + 1];
      imageData.data[dstIdx + 2] = pixels[srcIdx + 2];
      imageData.data[dstIdx + 3] = 255;
    }
  }

  const tempCanvas = document.createElement('canvas');
  tempCanvas.width = canvas.width;
  tempCanvas.height = canvas.height;
  tempCanvas.getContext('2d').putImageData(imageData, 0, 0);
  ctx.drawImage(tempCanvas, 0, 0, mirrorCanvas.width, mirrorCanvas.height);
  mirrorContainer.appendChild(mirrorCanvas);
  document.body.appendChild(mirrorContainer);

  // Update badge
  badge.innerHTML = `
    DESK DIAGNOSTIC<br>
    BUILD: ${buildInfo.build_id}<br>
    DESK: ${results.DESK.visible ? '✓' : '✗'} ${results.DESK.pixels} px<br>
    MON_MAIN: ${results.MON_MAIN.visible ? '✓' : '✗'} ${results.MON_MAIN.pixels} px<br>
    MON_SEC: ${results.MON_SECONDARY.visible ? '✓' : '✗'} ${results.MON_SECONDARY.pixels} px<br>
    KEYBOARD: ${results.KEYBOARD.visible ? '✓' : '✗'} ${results.KEYBOARD.pixels} px<br>
    MOUSE: ${results.MOUSE.visible ? '✓' : '✗'} ${results.MOUSE.pixels} px<br>
    MUG: ${results.MUG.visible ? '✓' : '✗'} ${results.MUG.pixels} px<br>
    NOTEBOOK: ${results.NOTEBOOK.visible ? '✓' : '✗'} ${results.NOTEBOOK.pixels} px<br>
    Visible: ${visibleCount}/7
  `;
  badge.style.background = allPass ? '#090' : '#c00';
  badge.style.borderColor = allPass ? '#0f0' : '#f00';

  // Store results
  window.FROZEN_DESK_RESULTS = {
    buildInfo,
    items: results,
    visibleCount,
    allPass,
  };

  console.log('[DESK_DIAG] === FINAL REPORT ===');
  for (const [name, data] of Object.entries(results)) {
    console.log(`${name}: ${data.visible ? 'VISIBLE' : 'NOT VISIBLE'} (${data.pixels} px)`);
  }
  console.log(`Visible items: ${visibleCount}/7`);
  console.log('OVERALL:', allPass ? 'PASS' : 'FAIL');
  console.log('[DESK_DIAG] Complete. Frame is frozen.');
}

// ============================================================ frozen character diagnostic

/**
 * FROZEN CHARACTER DIAGNOSTIC
 * Renders room + desk + character with bright debug colors.
 * Tests character visibility across CAM_1, CAM_4, and CAM_6.
 */
async function runFrozenCharacterDiagnostic() {
  console.log('[CHAR_DIAG] Starting frozen character diagnostic');

  // Fetch version info
  let buildInfo = { build_id: 'unknown', server_pid: 'unknown' };
  try {
    const resp = await fetch('/api/visual/version');
    if (resp.ok) buildInfo = await resp.json();
  } catch (e) {
    console.warn('[CHAR_DIAG] Could not fetch version:', e);
  }

  // Create diagnostic badge
  const badge = document.createElement('div');
  badge.id = 'char-badge';
  badge.style.cssText = `
    position: fixed; top: 20px; left: 20px;
    background: #222; color: #0f0; padding: 10px 15px; font-size: 11px;
    font-family: monospace; z-index: 100001; border-radius: 4px; border: 2px solid #0f0;
  `;
  badge.innerHTML = `CHARACTER DIAGNOSTIC<br>Loading...`;
  document.body.appendChild(badge);

  // Hide HUD
  for (const id of ['hud', 'controls', 'banner', 'stamp']) {
    const elem = el(id);
    if (elem) elem.style.display = 'none';
  }

  // Initialize runtime
  let runtime;
  try {
    runtime = new Runtime(el('gl'));
    await runtime.loadScene();
  } catch (error) {
    fault('CHARACTER DIAGNOSTIC FAILED\n\n' + (error && error.message ? error.message : error));
    return;
  }

  const gl = runtime.gl;
  const canvas = runtime.canvas;

  // Get URL parameter for camera (default CAM_1)
  const params = new URLSearchParams(location.search);
  const cameraId = params.get('camera') || 'CAM_1';
  runtime.setCamera(cameraId, 'cut', 0);
  console.log('[CHAR_DIAG] Using camera:', cameraId);

  // Clear background
  gl.clearColor(0.02, 0.02, 0.05, 1);
  gl.clear(gl.COLOR_BUFFER_BIT);
  gl.viewport(0, 0, canvas.width, canvas.height);

  // Build view-projection matrix
  const aspect = TARGET_W / TARGET_H;
  const cam = runtime.camera;
  const view = mat4.lookAt(cam.position, cam.target);
  const proj = mat4.perspective(cam.vfov, aspect, 50, 60000);
  const viewProj = mat4.multiply(proj, view);
  gl.uniformMatrix4fv(runtime.u.u_viewProj, false, viewProj);

  // Debug colors for character parts
  const CHAR_COLORS = {
    SKIN: [0.95, 0.75, 0.6, 1.0],      // LIGHT PEACH
    HAIR: [0.45, 0.3, 0.2, 1.0],       // BROWN
    HOODIE: [0.45, 0.45, 0.5, 1.0],    // MID GREY
    SHIRT: [0.3, 0.3, 0.35, 1.0],      // DARK GREY
    HANDS: [0.95, 0.75, 0.6, 1.0],     // LIGHT PEACH
    HEADPHONES: [0.1, 0.1, 0.1, 1.0],  // BLACK
    HEADPHONES_GOLD: [0.8, 0.65, 0.2, 1.0], // GOLD
    EYES: [0.15, 0.4, 0.7, 1.0],       // BLUE
    WATCH: [0.1, 0.1, 0.1, 1.0],       // BLACK
  };

  // Get rest positions from scene joints
  const getJointPos = (id) => {
    const joint = runtime.scene.joints.find(j => j.id === id);
    return joint ? joint.position : null;
  };

  // Helper to draw billboard quad
  const drawQuad = (centre, size, color, shape = 0) => {
    // Billboard facing camera
    const toCamera = [
      cam.position[0] - centre[0],
      cam.position[1] - centre[1],
      cam.position[2] - centre[2]
    ];
    const len = Math.sqrt(toCamera[0]**2 + toCamera[1]**2 + toCamera[2]**2);
    const forward = [toCamera[0]/len, toCamera[1]/len, toCamera[2]/len];

    // Right vector (cross of up and forward)
    const up = [0, 0, 1];
    const right = [
      forward[1] * up[2] - forward[2] * up[1],
      forward[2] * up[0] - forward[0] * up[2],
      forward[0] * up[1] - forward[1] * up[0]
    ];
    const rightLen = Math.sqrt(right[0]**2 + right[1]**2 + right[2]**2);
    if (rightLen > 0.001) {
      right[0] /= rightLen;
      right[1] /= rightLen;
      right[2] /= rightLen;
    } else {
      right[0] = 1; right[1] = 0; right[2] = 0;
    }

    gl.uniform3fv(runtime.u.u_centre, centre);
    gl.uniform2fv(runtime.u.u_size, size);
    gl.uniform3fv(runtime.u.u_basisX, right);
    gl.uniform3fv(runtime.u.u_basisY, up);
    gl.uniform2fv(runtime.u.u_parallax, [0, 0]);
    gl.uniform4fv(runtime.u.u_colour, color);
    gl.uniform4fv(runtime.u.u_colour2, color);
    gl.uniform1i(runtime.u.u_shape, shape);
    gl.uniform1f(runtime.u.u_edge, 0);
    gl.uniform1i(runtime.u.u_useTex, 0);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
  };

  // Draw subtle room/desk background first
  const [rw, rd, rh] = runtime.scene.room;
  const drawWall = (centre, size, basisX, basisY, color) => {
    gl.uniform3fv(runtime.u.u_centre, centre);
    gl.uniform2fv(runtime.u.u_size, size);
    gl.uniform3fv(runtime.u.u_basisX, basisX);
    gl.uniform3fv(runtime.u.u_basisY, basisY);
    gl.uniform2fv(runtime.u.u_parallax, [0, 0]);
    gl.uniform4fv(runtime.u.u_colour, color);
    gl.uniform4fv(runtime.u.u_colour2, color);
    gl.uniform1i(runtime.u.u_shape, 0);
    gl.uniform1f(runtime.u.u_edge, 0);
    gl.uniform1i(runtime.u.u_useTex, 0);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
  };

  console.group('[CHAR_DIAG] Drawing scene');

  // Subtle room background
  drawWall([rw/2, rd/2, 0], [rw, rd], [1,0,0], [0,1,0], [0.1, 0.08, 0.08, 1]); // Floor
  drawWall([rw/2, rd, rh/2], [rw, rh], [1,0,0], [0,0,1], [0.08, 0.1, 0.12, 1]); // Back wall

  // Draw character parts using joint positions
  const head = getJointPos('head');
  const torso = getJointPos('torso');
  const handL = getJointPos('hand_l');
  const handR = getJointPos('hand_r');

  if (head) {
    // Hair (behind head)
    drawQuad([head[0], head[1] + 20, head[2] + 60], [180, 100], CHAR_COLORS.HAIR, 1);
    // Head/Face (PEACH skin)
    drawQuad(head, [160, 200], CHAR_COLORS.SKIN, 1);
    // Headphones band (on top)
    drawQuad([head[0], head[1] - 10, head[2] + 130], [200, 30], CHAR_COLORS.HEADPHONES, 0);
    // Headphone cups
    drawQuad([head[0] - 90, head[1], head[2] + 30], [50, 70], CHAR_COLORS.HEADPHONES, 1);
    drawQuad([head[0] + 90, head[1], head[2] + 30], [50, 70], CHAR_COLORS.HEADPHONES, 1);
    // Headphone gold accents
    drawQuad([head[0] - 90, head[1], head[2] + 30], [30, 30], CHAR_COLORS.HEADPHONES_GOLD, 1);
    drawQuad([head[0] + 90, head[1], head[2] + 30], [30, 30], CHAR_COLORS.HEADPHONES_GOLD, 1);
    // Eyes
    drawQuad([head[0] - 35, head[1] - 60, head[2] + 10], [25, 20], CHAR_COLORS.EYES, 1);
    drawQuad([head[0] + 35, head[1] - 60, head[2] + 10], [25, 20], CHAR_COLORS.EYES, 1);
    console.log('HEAD: drawn at', head);
  }

  if (torso) {
    // Hoodie/Torso (MID GREY)
    drawQuad(torso, [360, 450], CHAR_COLORS.HOODIE, 0);
    // Shirt collar visible
    drawQuad([torso[0], torso[1] - 30, torso[2] + 180], [120, 60], CHAR_COLORS.SHIRT, 0);
    console.log('TORSO: drawn at', torso);
  }

  if (handL) {
    drawQuad(handL, [100, 80], CHAR_COLORS.HANDS, 1);
    // Watch on left wrist
    drawQuad([handL[0] + 10, handL[1] + 30, handL[2] + 30], [40, 25], CHAR_COLORS.WATCH, 0);
    console.log('HAND_L: drawn at', handL);
  }

  if (handR) {
    drawQuad(handR, [100, 80], CHAR_COLORS.HANDS, 1);
    console.log('HAND_R: drawn at', handR);
  }

  console.groupEnd();

  // Ensure complete
  gl.finish();

  // Framebuffer readback
  const pixels = new Uint8Array(canvas.width * canvas.height * 4);
  gl.readPixels(0, 0, canvas.width, canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);

  // Color matching
  const colorCounts = {
    SKIN: 0,
    HAIR: 0,
    HOODIE: 0,
    HEADPHONES: 0,
    EYES: 0,
  };

  const isMatch = (r, g, b, color, threshold = 40) =>
    Math.abs(r - color[0] * 255) < threshold &&
    Math.abs(g - color[1] * 255) < threshold &&
    Math.abs(b - color[2] * 255) < threshold;

  for (let i = 0; i < pixels.length; i += 4) {
    const r = pixels[i], g = pixels[i + 1], b = pixels[i + 2];
    if (isMatch(r, g, b, CHAR_COLORS.SKIN)) colorCounts.SKIN++;
    else if (isMatch(r, g, b, CHAR_COLORS.HAIR)) colorCounts.HAIR++;
    else if (isMatch(r, g, b, CHAR_COLORS.HOODIE)) colorCounts.HOODIE++;
    else if (isMatch(r, g, b, CHAR_COLORS.HEADPHONES)) colorCounts.HEADPHONES++;
    else if (isMatch(r, g, b, CHAR_COLORS.EYES)) colorCounts.EYES++;
  }

  const results = {
    SKIN: { pixels: colorCounts.SKIN, visible: colorCounts.SKIN > 1000 },
    HAIR: { pixels: colorCounts.HAIR, visible: colorCounts.HAIR > 500 },
    HOODIE: { pixels: colorCounts.HOODIE, visible: colorCounts.HOODIE > 2000 },
    HEADPHONES: { pixels: colorCounts.HEADPHONES, visible: colorCounts.HEADPHONES > 200 },
    EYES: { pixels: colorCounts.EYES, visible: colorCounts.EYES > 50 },
  };

  const visibleCount = Object.values(results).filter(r => r.visible).length;
  const hasHead = results.SKIN.visible;
  const hasTorso = results.HOODIE.visible;
  const allPass = hasHead && hasTorso && visibleCount >= 3;

  // Create mirror
  const mirrorContainer = document.createElement('div');
  mirrorContainer.style.cssText = `
    position: fixed; top: 20px; right: 20px; background: #111; padding: 10px;
    border: 2px solid #0f0; z-index: 100000; font-family: monospace; color: #0f0;
  `;
  mirrorContainer.innerHTML = '<div style="margin-bottom:5px;">GPU READBACK</div>';

  const mirrorCanvas = document.createElement('canvas');
  mirrorCanvas.width = canvas.width / 4;
  mirrorCanvas.height = canvas.height / 4;
  mirrorCanvas.style.cssText = 'border: 1px solid #0f0;';

  const ctx = mirrorCanvas.getContext('2d');
  const imageData = ctx.createImageData(canvas.width, canvas.height);

  for (let y = 0; y < canvas.height; y++) {
    for (let x = 0; x < canvas.width; x++) {
      const srcIdx = (y * canvas.width + x) * 4;
      const dstIdx = ((canvas.height - 1 - y) * canvas.width + x) * 4;
      imageData.data[dstIdx] = pixels[srcIdx];
      imageData.data[dstIdx + 1] = pixels[srcIdx + 1];
      imageData.data[dstIdx + 2] = pixels[srcIdx + 2];
      imageData.data[dstIdx + 3] = 255;
    }
  }

  const tempCanvas = document.createElement('canvas');
  tempCanvas.width = canvas.width;
  tempCanvas.height = canvas.height;
  tempCanvas.getContext('2d').putImageData(imageData, 0, 0);
  ctx.drawImage(tempCanvas, 0, 0, mirrorCanvas.width, mirrorCanvas.height);
  mirrorContainer.appendChild(mirrorCanvas);
  document.body.appendChild(mirrorContainer);

  // Update badge
  badge.innerHTML = `
    CHARACTER DIAGNOSTIC<br>
    Camera: ${cameraId}<br>
    SKIN: ${results.SKIN.visible ? '✓' : '✗'} ${results.SKIN.pixels} px<br>
    HAIR: ${results.HAIR.visible ? '✓' : '✗'} ${results.HAIR.pixels} px<br>
    HOODIE: ${results.HOODIE.visible ? '✓' : '✗'} ${results.HOODIE.pixels} px<br>
    HEADPHONES: ${results.HEADPHONES.visible ? '✓' : '✗'} ${results.HEADPHONES.pixels} px<br>
    EYES: ${results.EYES.visible ? '✓' : '✗'} ${results.EYES.pixels} px<br>
    Visible: ${visibleCount}/5
  `;
  badge.style.background = allPass ? '#090' : '#c00';
  badge.style.borderColor = allPass ? '#0f0' : '#f00';

  // Store results
  window.FROZEN_CHAR_RESULTS = {
    buildInfo,
    camera: cameraId,
    parts: results,
    visibleCount,
    hasHead,
    hasTorso,
    allPass,
  };

  console.log('[CHAR_DIAG] === FINAL REPORT ===');
  console.log('Camera:', cameraId);
  for (const [name, data] of Object.entries(results)) {
    console.log(`${name}: ${data.visible ? 'VISIBLE' : 'NOT VISIBLE'} (${data.pixels} px)`);
  }
  console.log('OVERALL:', allPass ? 'PASS' : 'FAIL');
  console.log('[CHAR_DIAG] Complete. Frame is frozen.');
}

// ============================================================ boot

/* Guarded so Node can load this file for the renderer-contract tests without starting a
 * renderer. The browser path is unchanged: `document` is always present there. */
const IN_BROWSER = typeof document !== 'undefined';

(async function main() {
  if (!IN_BROWSER) return;

  // ============ RENDER TEST MODE HANDLING ============
  if (RENDER_TEST) {
    console.log('[RENDER TEST] Mode:', RENDER_TEST);

    // STEP 1: DOM test - show magenta overlay
    if (RENDER_TEST === 'dom') {
      const domTest = el('dom-test');
      if (domTest) {
        domTest.style.display = 'block';
      }
      runCanvasDiagnostics();
      console.log('[RENDER TEST] DOM test active. You should see a MAGENTA box saying "DOM VISIBLE"');
      return;  // Don't start WebGL
    }

    // STEP 2: Canvas CSS test - lime green background, no GL
    if (RENDER_TEST === 'canvas') {
      const canvas = el('gl');
      canvas.style.background = 'lime';
      runCanvasDiagnostics();
      console.log('[RENDER TEST] Canvas test active. Canvas should be LIME GREEN.');
      return;  // Don't start WebGL
    }

    // STEP 4: Clear test - just magenta clear
    if (RENDER_TEST === 'clear') {
      const canvas = el('gl');
      const gl = canvas.getContext('webgl2', { alpha: true, antialias: true, preserveDrawingBuffer: true });
      if (!gl) {
        fault('WebGL2 unavailable');
        return;
      }
      gl.viewport(0, 0, canvas.width, canvas.height);
      runCanvasDiagnostics();
      runWebGLDiagnostics(gl, canvas);
      console.log('[RENDER TEST] Clear test active. Canvas should be MAGENTA.');
      // Keep refreshing the clear
      function clearLoop() {
        gl.clearColor(1.0, 0.0, 1.0, 1.0);
        gl.clear(gl.COLOR_BUFFER_BIT);
        requestAnimationFrame(clearLoop);
      }
      requestAnimationFrame(clearLoop);
      return;
    }

    // STEP 6: Triangle test
    if (RENDER_TEST === 'triangle') {
      const canvas = el('gl');
      const gl = canvas.getContext('webgl2', { alpha: true, antialias: true, preserveDrawingBuffer: true });
      if (!gl) {
        fault('WebGL2 unavailable');
        return;
      }
      gl.viewport(0, 0, canvas.width, canvas.height);
      runCanvasDiagnostics();
      runWebGLDiagnostics(gl, canvas);
      drawTestTriangle(gl, canvas);
      console.log('[RENDER TEST] Triangle test. Should see YELLOW triangle on MAGENTA background.');
      // Keep refreshing
      function triangleLoop() {
        drawTestTriangle(gl, canvas);
        requestAnimationFrame(triangleLoop);
      }
      requestAnimationFrame(triangleLoop);
      return;
    }

    // CAMERA-BYPASS: Room in clip space (no transforms)
    if (RENDER_TEST === 'room_clip') {
      const canvas = el('gl');
      const gl = canvas.getContext('webgl2', { alpha: true, antialias: true, preserveDrawingBuffer: true });
      if (!gl) { fault('WebGL2 unavailable'); return; }
      gl.viewport(0, 0, canvas.width, canvas.height);
      runCanvasDiagnostics();

      // Draw room as clip-space polygons - NO MATRICES
      function drawRoomClip() {
        gl.clearColor(0.1, 0.1, 0.1, 1.0);
        gl.clear(gl.COLOR_BUFFER_BIT);

        const vsSource = `#version 300 es
          in vec2 a_pos;
          void main() { gl_Position = vec4(a_pos, 0.0, 1.0); }`;
        const fsSource = `#version 300 es
          precision mediump float;
          uniform vec4 u_color;
          out vec4 fragColor;
          void main() { fragColor = u_color; }`;

        const vs = gl.createShader(gl.VERTEX_SHADER);
        gl.shaderSource(vs, vsSource);
        gl.compileShader(vs);
        const fs = gl.createShader(gl.FRAGMENT_SHADER);
        gl.shaderSource(fs, fsSource);
        gl.compileShader(fs);
        const prog = gl.createProgram();
        gl.attachShader(prog, vs);
        gl.attachShader(prog, fs);
        gl.linkProgram(prog);
        gl.useProgram(prog);

        const colorLoc = gl.getUniformLocation(prog, 'u_color');
        const posLoc = gl.getAttribLocation(prog, 'a_pos');

        // Draw floor (RED) - bottom half of screen
        let verts = new Float32Array([-0.9, -0.9, 0.9, -0.9, 0.9, 0.0, -0.9, 0.0]);
        let buf = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, buf);
        gl.bufferData(gl.ARRAY_BUFFER, verts, gl.STATIC_DRAW);
        gl.enableVertexAttribArray(posLoc);
        gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);
        gl.uniform4f(colorLoc, 0.8, 0.2, 0.2, 1.0);
        gl.drawArrays(gl.TRIANGLE_FAN, 0, 4);

        // Draw back wall (BLUE) - top half
        verts = new Float32Array([-0.9, 0.0, 0.9, 0.0, 0.9, 0.9, -0.9, 0.9]);
        gl.bufferData(gl.ARRAY_BUFFER, verts, gl.STATIC_DRAW);
        gl.uniform4f(colorLoc, 0.2, 0.4, 0.8, 1.0);
        gl.drawArrays(gl.TRIANGLE_FAN, 0, 4);

        // Draw side indicator (GREEN) - left strip
        verts = new Float32Array([-0.95, -0.9, -0.85, -0.9, -0.85, 0.9, -0.95, 0.9]);
        gl.bufferData(gl.ARRAY_BUFFER, verts, gl.STATIC_DRAW);
        gl.uniform4f(colorLoc, 0.2, 0.7, 0.3, 1.0);
        gl.drawArrays(gl.TRIANGLE_FAN, 0, 4);
      }

      drawRoomClip();
      console.log('[RENDER TEST] room_clip: RED floor, BLUE wall, GREEN side - NO camera transforms');

      // Read center pixel
      const pixels = new Uint8Array(4);
      gl.readPixels(canvas.width/2, canvas.height/2, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
      console.log('[RENDER TEST] Center pixel:', pixels[0], pixels[1], pixels[2], pixels[3]);

      function roomClipLoop() {
        drawRoomClip();
        requestAnimationFrame(roomClipLoop);
      }
      requestAnimationFrame(roomClipLoop);
      return;
    }

    // WORLD MARKER: FROZEN DIAGNOSTIC MODE
    // Does NOT use RAF loop - draws once and stops
    if (RENDER_TEST === 'world_marker' || RENDER_TEST === 'world_marker_identity') {
      await runFrozenWorldMarkerDiagnostic(RENDER_TEST);
      return;  // Do not start normal runtime
    }

    // ROOM: FROZEN DIAGNOSTIC MODE
    // Renders room geometry through production camera/matrix path
    if (RENDER_TEST === 'room') {
      await runFrozenRoomDiagnostic();
      return;  // Do not start normal runtime
    }

    // DESK: FROZEN DIAGNOSTIC MODE
    // Renders room + desk + monitors with debug colors
    if (RENDER_TEST === 'desk') {
      await runFrozenDeskDiagnostic();
      return;  // Do not start normal runtime
    }

    // CHARACTER: FROZEN DIAGNOSTIC MODE
    // Renders room + desk + character with debug colors
    // Use ?camera=CAM_1 or CAM_4 or CAM_6 to test different views
    if (RENDER_TEST === 'character') {
      await runFrozenCharacterDiagnostic();
      return;  // Do not start normal runtime
    }
  }

  // ============ NORMAL STARTUP ============
  let runtime;
  try {
    runtime = new Runtime(el('gl'));
    await runtime.loadScene();
  } catch (error) {
    fault('PLACEHOLDER RUNTIME FAILED\n\n' + (error && error.message ? error.message : error));
    return;
  }

  // Run diagnostics on first frame for render_test modes that use the full runtime
  // BUT don't show diagnostic overlay for world_marker tests - it covers the canvas!
  if (RENDER_TEST && !RENDER_TEST.startsWith('world_marker')) {
    runCanvasDiagnostics();
    runWebGLDiagnostics(runtime.gl, runtime.canvas);
  }

  // -- the command socket
  const connect = () => {
    const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
    const socket = new WebSocket(url);
    runtime.socketState = 'connecting';
    socket.onopen = () => { runtime.socketState = 'open'; };
    socket.onmessage = (event) => {
      try {
        const commands = JSON.parse(event.data);
        runtime.lastSequence = commands.reduce(
          (max, c) => Math.max(max, c.seq || 0), runtime.lastSequence || 0);
        runtime.handle(commands);
      } catch (error) {
        console.warn('[visual] bad command frame', error);
      }
    };
    socket.onclose = () => {
      runtime.socketState = 'closed';
      /* Reconnect indefinitely. A 24/7 browser source must not need a human to reload
       * it, and the director's state survives on the Python side so nothing resets. */
      setTimeout(connect, 2000);
    };
    socket.onerror = () => { runtime.socketState = 'error'; };
    runtime.socket = socket;
  };
  connect();

  // -- telemetry, 1 Hz
  setInterval(() => {
    if (runtime.socket?.readyState === WebSocket.OPEN) {
      runtime.socket.send(JSON.stringify(runtime.telemetry()));
    }
    runtime.frameTimes = runtime.frameTimes.slice(-120);
  }, 1000);

  /* The server's state at 4 Hz, the HUD at 4 Hz off the cached copy, and the debug
   * overlay at 20 Hz because it tracks moving geometry and a 250 ms overlay on a hand
   * mid-reach reads as lag in the renderer rather than in the overlay. */
  /* The badge and banner state the live art mode, so the page cannot claim to be showing
   * approved artwork while drawing procedural shapes — or vice versa. */
  const badge = {
    proof: ['PROCEDURAL',
            'PROCEDURAL ANIME · RUNTIME GENERATED · TRADE FIX RADIO'],
    blockout: ['BLOCKOUT',
               'BLOCKOUT GEOMETRY · GREY BOXES · NOT ARTWORK'],
    final: ['IMPORTED ART',
            'IMPORTED PLATES · procedural fallback for any missing layer'],
  }[ART_MODE];
  el('stamp').textContent = badge[0];
  el('banner').textContent = badge[1];

  pollState();
  pollAssets(runtime);
  setInterval(pollState, 250);
  /* Every five seconds, so copying a plate in shows up without a restart. The cost is
   * one small JSON fetch; the texture upload happens once per file, not per poll. */
  setInterval(() => pollAssets(runtime), 5_000);
  setInterval(() => updateArtNotices(runtime), 500);
  setInterval(() => updateHud(runtime), 250);
  setInterval(() => updateOverlay(runtime), 50);
  wireControls(runtime);

  // -- the frame loop, capped
  const minFrameMs = 1000 / FPS_CAP;
  let previous = performance.now();
  function frame(now) {
    const since = now - previous;
    if (since >= minFrameMs - 0.5) {
      previous = now;
      runtime.frameTimes.push(since);
      if (runtime.frameTimes.length > 600) runtime.frameTimes.shift();
      /* A frame that took more than 1.8x the cap is a hitch, and counting them is the
       * only way the mean does not hide them. */
      if (since > minFrameMs * 1.8 && runtime.frames > FPS_CAP) runtime.dropped++;
      try {
        runtime.draw(now);
        runtime.frames++;
      } catch (error) {
        /* Stop on the first failure and report it in full. Not a swallowing catch: the
         * loop does NOT continue, because a renderer that keeps drawing through a broken
         * contract hides the defect behind a picture that is already wrong. */
        const report = describeDrawFailure(error, runtime);
        window.__tfDrawFailure = report;
        console.error('[visual] DRAW FAILED', report);
        console.error(error);
        /* Posted to the server so the failure survives a closed tab and shows up in the
         * same log as the director that produced the state. */
        fetch('/api/visual/draw_failure', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(report),
        }).catch(() => {});
        showDrawFailure(report);
        return;
      }
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  // Expose for the benchmark harness and for manual inspection in devtools.
  window.__tfVisual = runtime;
})();

/* Node entry point for the renderer-contract tests.
 *
 * The draw path has to be exercisable without a browser. Sixty-eight actions across
 * seven cameras with the debug overlay on and off is 950-odd combinations, and no amount
 * of reloading a page covers that — nor would it tell you *which* combination broke. */
if (typeof module !== 'undefined' && typeof module.exports === 'object') {
  module.exports = {
    Runtime, MOTION, motionFor, mat4, Ease, BODY_POINTS, offsetOf,
    project, updateOverlay, updateHud, view, FPS_CAP, CONTRAST_MODE,
    expectVec, expectVecAlways, validateScene, ContractError, describeDrawFailure,
    describeType, LIMB_SEGMENTS, STRICT_GEOMETRY,
  };
}
