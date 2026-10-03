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

const TARGET_W = 1920;
const TARGET_H = 1080;

/* Frame cap. 30 by default because the brief defers the 30-vs-60 decision to a
 * measurement, and a stable 30 may beat an unstable 60. `?fps=60` overrides for the
 * benchmark. */
const params = new URLSearchParams(location.search);
const FPS_CAP = Math.max(1, Math.min(120, Number(params.get('fps')) || 30));
const SHOW_HUD = params.get('hud') !== '0';
const SHOW_DEBUG = params.get('debug') === '1';

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

/* One fragment shader for every shape. `u_shape` selects a signed-distance field:
 * 0 = filled rect, 1 = ellipse, 2 = rect outline. Three shapes, no textures, no atlas —
 * so the harness costs almost nothing in VRAM, which matters because ACE-Step has
 * priority on this GPU. */
const FRAGMENT_SRC = `#version 300 es
precision mediump float;
in vec2 v_uv;
uniform vec4 u_colour;
uniform int u_shape;
uniform float u_edge;       // outline thickness, as a fraction of the quad
out vec4 o_colour;
void main() {
  float alpha = u_colour.a;
  if (u_shape == 1) {
    vec2 p = (v_uv - 0.5) * 2.0;
    float d = length(p);
    alpha *= 1.0 - smoothstep(0.96, 1.0, d);
  } else if (u_shape == 2) {
    vec2 d = min(v_uv, 1.0 - v_uv);
    float m = min(d.x, d.y);
    alpha *= 1.0 - smoothstep(u_edge * 0.6, u_edge, m);
  }
  if (alpha < 0.004) discard;
  o_colour = vec4(u_colour.rgb, alpha);
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
                        'u_parallax','u_colour','u_shape','u_edge']) {
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
    this.scene = await response.json();
    for (const camera of this.scene.cameras) this.cameras.set(camera.id, camera);
    this.setCamera(this.scene.cameras.find((c) => c.id === 'CAM_7')?.id
                   || this.scene.cameras[0].id, 'cut', 0);

    for (const joint of this.scene.joints) {
      this.pose.set(joint.id, { offset: [0,0,0], target: [0,0,0], rot: 0, rotTarget: 0,
                                close: 0, closeTarget: 0, since: 0, ease: 'inOut',
                                durationMs: 400 });
    }
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

    /* Drive the placeholder rig. A hand with an anchor eases to that anchor's blockout
     * position — which is how the runtime verifies that anchors resolve and that reach
     * looks plausible, without any rig existing yet. */
    if (command.anchor && this.scene?.anchors?.[command.anchor]) {
      const anchor = this.scene.anchors[command.anchor];
      const hand = command.anchor.includes('_L') || command.anchor.includes('HOME_L')
        ? 'hand_l' : 'hand_r';
      this._easeJointTo(hand, anchor, 'attention', clamp(command.duration_ms, 180, 900));
    }
    if (command.action_id.startsWith('lean') || command.action_id.includes('spine')
        || command.action_id.includes('chair')) {
      this._easeJointRot('torso', (command.action_id.includes('back') ? -1 : 1)
        * 0.07 * command.amplitude, 'settle', command.blend_in_ms + 240);
    }
    if (command.action_id.includes('shoulder')) {
      this._easeJointRot('shoulder_l', 0.09 * command.amplitude, 'settle', 420);
      this._easeJointRot('shoulder_r', -0.09 * command.amplitude, 'settle', 420);
    }
    if (command.action_id === 'micro_head_nod') {
      this._easeJointRot('head', (this.rhythm.maxDeg * Math.PI / 180), 'inOut', 260);
    }
  }

  _easeJointTo(jointId, worldPosition, ease, durationMs) {
    const joint = this.pose.get(jointId);
    const rest = this.scene.joints.find((j) => j.id === jointId);
    if (!joint || !rest) return;
    joint.offset = joint.offset.slice();
    joint.target = [
      worldPosition[0] - rest.position[0],
      worldPosition[1] - rest.position[1],
      worldPosition[2] - rest.position[2],
    ];
    joint.since = performance.now();
    joint.ease = ease;
    joint.durationMs = Math.max(120, durationMs);
  }

  _easeJointRot(jointId, radians, ease, durationMs) {
    const joint = this.pose.get(jointId);
    if (!joint) return;
    joint.rotTarget = radians;
    joint.since = performance.now();
    joint.ease = ease;
    joint.durationMs = Math.max(120, durationMs);
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

    gl.clearColor(0.027, 0.031, 0.039, 1);
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

    // Retire finished actions before the pose is read.
    this.actions = this.actions.filter((a) => a.ends > now);
    this.queued = this.actions.length;

    this._drawBoxes(now);
    this._drawQuads(now);
    this._drawCharacter(now);
    if (SHOW_DEBUG) this._drawDebug(now);
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

  /* Parallax from blockout depth, never eyeballed. ASSET_MANIFEST §2's rule applied to
   * the placeholder, so the runtime's parallax path is exercised with real numbers. */
  _parallaxFor(centre) {
    const eye = this.camera.position;
    const distance = Math.max(1, Math.hypot(
      centre[0] - eye[0], centre[1] - eye[1], centre[2] - eye[2]));
    const amount = this.parallax * (2248 / distance);
    return [this._drift[0] * amount * 120, this._drift[1] * amount * 120];
  }

  _quad(centre, size, basisX, basisY, colour, shape, edge) {
    const gl = this.gl;
    gl.uniform3fv(this.u.u_centre, centre);
    gl.uniform2fv(this.u.u_size, size);
    gl.uniform3fv(this.u.u_basisX, basisX);
    gl.uniform3fv(this.u.u_basisY, basisY);
    gl.uniform2fv(this.u.u_parallax, this._parallaxFor(centre));
    gl.uniform4fv(this.u.u_colour, colour);
    gl.uniform1i(this.u.u_shape, shape);
    gl.uniform1f(this.u.u_edge, edge === undefined ? 0.03 : edge);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
    this.drawCalls++;
  }

  _drawBoxes() {
    for (const box of this.scene.boxes) {
      const [x0, y0, z0] = box.min, [x1, y1, z1] = box.max;
      const colour = this._colour(box.colour, box.filled ? box.opacity : 0.85);
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
        position = [
          position[0] + pose.offset[0],
          position[1] + pose.offset[1],
          position[2] + pose.offset[2],
        ];
        rotation += pose.rot;
      }
      node = node.parent ? this.scene.joints.find((j) => j.id === node.parent) : null;
    }
    return { position, rotation };
  }

  _drawCharacter(now) {
    // Advance every eased joint before drawing.
    for (const [, pose] of this.pose) {
      const t = clamp((now - pose.since) / pose.durationMs, 0, 1);
      const eased = (Ease[pose.ease] || Ease.inOut)(t);
      for (let i = 0; i < 3; i++) {
        pose.offset[i] = lerp(pose.offset[i], pose.target[i], eased * 0.25);
      }
      pose.rot = lerp(pose.rot, pose.rotTarget, eased * 0.2);
      if (t >= 1) { pose.rotTarget *= 0.9; for (let i = 0; i < 3; i++) pose.target[i] *= 0.985; }
    }

    // Breathing: chest and shoulders, ~4 s, always, under every action.
    const breath = Math.sin(now / 4100 * Math.PI * 2);
    const chest = this.pose.get('torso');
    if (chest) chest.offset[2] += breath * 0.9;

    const blinkT = this.blink.duration
      ? clamp((now - this.blink.closing) / this.blink.duration, 0, 1) : 1;
    const lidClose = blinkT < 1 ? Math.sin(blinkT * Math.PI) : 0;

    const gazeT = clamp((now - this.gaze.started) / this.gaze.transit, 0, 1);
    const gazeEase = Ease.attention(gazeT);

    for (const joint of this.scene.joints) {
      const world = this._worldFor(joint.id);
      if (!world) continue;
      const yaw = world.rotation;
      const basisX = [Math.cos(yaw), Math.sin(yaw), 0];
      const basisY = [-Math.sin(yaw) * 0.0, 0, 1];
      let size = joint.size.slice();
      let colour = this._colour(joint.colour, 1);

      if (joint.id.startsWith('lid_')) {
        size = [size[0], Math.max(1, size[1] * lidClose)];
        if (lidClose < 0.02) continue;
      }
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
        colour = this._colour('ink_100', 1);
      }
      if (joint.id === 'head' && this.gaze.target) {
        const resolved = this.scene.gaze_targets[this.gaze.target];
        if (resolved) {
          const dx = clamp((resolved[0] - world.position[0]) / 2600, -1, 1);
          world.position[0] += dx * 46 * gazeEase * this.gaze.head;
        }
      }
      this._quad(world.position, size, basisX, basisY, colour,
                 joint.shape === 'ellipse' ? 1 : 0);
    }
  }

  _drawDebug() {
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

// ============================================================ HUD

const el = (id) => document.getElementById(id);

function updateHud(runtime) {
  if (!SHOW_HUD) { el('hud').classList.add('off'); return; }
  const t = runtime.telemetry();
  el('p-fps').textContent = t.fps_mean.toFixed(1);
  el('p-p05').textContent = t.fps_p05.toFixed(1);
  el('p-p95').textContent = t.frame_time_p95_ms.toFixed(2) + ' ms';
  el('p-drop').textContent = t.dropped_frames;
  el('p-draws').textContent = runtime.drawCalls;
  el('p-layers').textContent = runtime.scene
    ? runtime.scene.boxes.length + runtime.scene.quads.length + runtime.scene.joints.length
    : '—';
  el('p-mem').textContent = t.gl_memory_mb === null
    ? 'n/a (no extension)' : t.gl_memory_mb.toFixed(1) + ' MB';
  el('p-cap').textContent = FPS_CAP;
  const sock = el('p-sock');
  sock.textContent = runtime.socketState;
  sock.className = runtime.socketState === 'open' ? 'ok'
    : (runtime.socketState === 'closed' ? 'bad' : 'warn');

  el('c-state').textContent = runtime.characterState;
  el('c-action').textContent = runtime.actions.length
    ? runtime.actions[runtime.actions.length - 1].id : 'still';
  el('c-chain').textContent = runtime.chain || '—';
  el('c-gaze').textContent = runtime.gaze.target || '—';
  const last = runtime.actions[runtime.actions.length - 1];
  el('c-blend').textContent = last
    ? `${last.blendIn}/${last.blendOut} ms` : '—';
  el('c-queue').textContent = runtime.queued;
  el('c-fatigue').textContent = runtime.fatigue.toFixed(3);

  if (runtime.camera) {
    el('k-id').textContent = `${runtime.cameraId} ${runtime.camera.name}`;
    el('k-lens').textContent = `${runtime.camera.focal_mm_35eq} mm`;
    el('k-held').textContent =
      ((performance.now() - (runtime.cameraSince || 0)) / 1000).toFixed(0) + ' s';
    el('k-trans').textContent = runtime.transition.kind;
    el('k-par').textContent = runtime.parallax.toFixed(4);
    el('k-drift').textContent = (runtime.drift || 0).toFixed(4);
  }

  const s = runtime.screen;
  el('m-sym').textContent = s?.active_symbol || '—';
  el('m-band').textContent = s?.band || '—';
  el('m-energy').textContent = s?.energy === null || s?.energy === undefined
    ? 'absent' : s.energy.toFixed(1);
  el('m-bpm').textContent = s?.now_playing?.bpm ?? 'silent';
  const charts = el('m-charts');
  const advancing = s?.honesty?.charts_advance !== false;
  charts.textContent = s ? (advancing ? 'live' : (s.honesty.stale_marker || 'frozen')) : '—';
  charts.className = advancing ? 'ok' : 'bad';
  el('m-station').textContent = s?.station_mode || '—';
}

function fault(message) {
  const box = el('fault');
  box.textContent = message;
  box.classList.add('shown');
}

// ============================================================ boot

(async function main() {
  let runtime;
  try {
    runtime = new Runtime(el('gl'));
    await runtime.loadScene();
  } catch (error) {
    fault('PLACEHOLDER RUNTIME FAILED\n\n' + (error && error.message ? error.message : error));
    return;
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

  setInterval(() => updateHud(runtime), 250);

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
        fault('DRAW FAILED\n\n' + (error && error.message ? error.message : error));
        return;
      }
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  // Expose for the benchmark harness and for manual inspection in devtools.
  window.__tfVisual = runtime;
})();
