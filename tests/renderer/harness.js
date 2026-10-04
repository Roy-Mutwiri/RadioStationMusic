/*
 * Headless renderer harness.
 *
 * Loads the real `visual/runtime/app.js` under Node with a strict mock WebGL2 context and
 * drives the real `Runtime.draw()` against the real serialised scene. No browser, no GPU.
 *
 * Why this exists: `DRAW FAILED object is not iterable` was reported from a live page and
 * could not be attributed from the message alone. Sixty-eight catalogued actions across
 * seven cameras with the debug overlay on and off is ~950 combinations; reloading a tab
 * covers none of them reproducibly and tells you nothing about which one broke.
 *
 * The mock GL is deliberately STRICTER than a real driver. `uniform3fv` in a browser
 * converts its argument to a WebIDL `sequence<GLfloat>`, which throws
 * "object is not iterable (cannot read property Symbol(Symbol.iterator))" — an error that
 * names neither the uniform nor the field. The mock reproduces that exact failure mode and
 * then adds the attribution the browser withholds.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const RUNTIME_DIR = path.resolve(__dirname, '..', '..', 'visual', 'runtime');

// ------------------------------------------------------------------ mock WebGL2

class MockGl {
  constructor() {
    this.calls = [];
    this.uniforms = new Map();
    this.drawArraysCount = 0;
    // Enum values only need to be distinct.
    let next = 0x1000;
    for (const name of [
      'VERTEX_SHADER', 'FRAGMENT_SHADER', 'COMPILE_STATUS', 'LINK_STATUS',
      'ARRAY_BUFFER', 'STATIC_DRAW', 'FLOAT', 'TRIANGLES', 'BLEND', 'DEPTH_TEST',
      'SRC_ALPHA', 'ONE_MINUS_SRC_ALPHA', 'ONE', 'COLOR_BUFFER_BIT',
    ]) this[name] = next++;
  }

  createShader() { return { shader: true }; }
  shaderSource() {}
  compileShader() {}
  getShaderParameter() { return true; }
  getShaderInfoLog() { return ''; }
  createProgram() { return { program: true }; }
  attachShader() {}
  linkProgram() {}
  getProgramParameter() { return true; }
  getProgramInfoLog() { return ''; }
  useProgram() {}
  getUniformLocation(_program, name) { return { name }; }
  createBuffer() { return { buffer: true }; }
  bindBuffer() {}
  bufferData() {}
  getAttribLocation() { return 0; }
  enableVertexAttribArray() {}
  vertexAttribPointer() {}
  enable() {}
  disable() {}
  blendFuncSeparate() {}
  getExtension() { return null; }
  createTexture() { return { texture: true }; }
  bindTexture() {}
  activeTexture() {}
  texImage2D() {}
  texParameteri() {}
  deleteTexture() {}
  viewport() {}
  clearColor() {}
  clear() {}

  /* The WebIDL `sequence<GLfloat>` conversion, reproduced.
   *
   * This is the line that produced the reported failure. A real browser throws from here
   * with no attribution; the harness throws the same error type and message so a
   * regression test can assert on them, with the uniform name appended so a human can
   * act on it. */
  _sequence(value, expected, where) {
    if (value === null || value === undefined || typeof value !== 'object') {
      throw new TypeError(
        `${where}: expected a sequence, received ${value === null ? 'null' : typeof value}`);
    }
    if (typeof value[Symbol.iterator] !== 'function') {
      const error = new TypeError(
        'object is not iterable (cannot read property Symbol(Symbol.iterator))');
      error.uniform = where;
      error.receivedKeys = Object.keys(value);
      throw error;
    }
    const array = [...value];
    if (array.length !== expected) {
      throw new TypeError(
        `${where}: expected ${expected} floats, received ${array.length}`);
    }
    for (let i = 0; i < array.length; i++) {
      if (!Number.isFinite(array[i])) {
        throw new TypeError(`${where}[${i}]: ${array[i]} is not finite`);
      }
    }
    return array;
  }

  uniform1i(location, value) {
    if (!Number.isInteger(value)) {
      throw new TypeError(`${location.name}: expected an int, received ${value}`);
    }
    this.uniforms.set(location.name, value);
  }

  uniform1f(location, value) {
    if (!Number.isFinite(value)) {
      throw new TypeError(`${location.name}: expected a finite float, received ${value}`);
    }
    this.uniforms.set(location.name, value);
  }

  uniform2fv(location, value) {
    this.uniforms.set(location.name, this._sequence(value, 2, location.name));
  }

  uniform3fv(location, value) {
    this.uniforms.set(location.name, this._sequence(value, 3, location.name));
  }

  uniform4fv(location, value) {
    this.uniforms.set(location.name, this._sequence(value, 4, location.name));
  }

  uniformMatrix4fv(location, _transpose, value) {
    this.uniforms.set(location.name, this._sequence(value, 16, location.name));
  }

  drawArrays(mode, first, count) {
    if (count !== 6) throw new TypeError(`drawArrays: expected 6 vertices, got ${count}`);
    this.drawArraysCount++;

    /* Validate at the call rather than accumulating for a later pass. The sweep runs
     * hundreds of thousands of draw calls, and retaining each one's uniforms exhausted
     * the heap — a test harness that OOMs reports nothing at all. Every uniform has
     * already been range-checked by `_sequence`; what remains is that each one was
     * actually set before the draw. */
    for (const name of ['u_centre', 'u_size', 'u_basisX', 'u_basisY', 'u_parallax',
                        'u_colour', 'u_shape']) {
      if (!this.uniforms.has(name)) {
        throw new TypeError(`drawArrays: ${name} was never set`);
      }
    }

    /* A bounded ring of the most recent calls, so a test can assert on real geometry
     * without the harness growing without limit. */
    if (this.calls.length >= 64) this.calls.shift();
    this.calls.push({
      centre: this.uniforms.get('u_centre'),
      size: this.uniforms.get('u_size'),
      basisX: this.uniforms.get('u_basisX'),
      basisY: this.uniforms.get('u_basisY'),
      parallax: this.uniforms.get('u_parallax'),
      colour: this.uniforms.get('u_colour'),
      shape: this.uniforms.get('u_shape'),
    });
  }
}

// ------------------------------------------------------------------ mock DOM

function mockElement(id) {
  const node = {
    id,
    textContent: '',
    style: {},
    dataset: {},
    children: [],
    classList: {
      _set: new Set(),
      add(name) { this._set.add(name); },
      remove(name) { this._set.delete(name); },
      toggle(name, on) { if (on) this._set.add(name); else this._set.delete(name); },
      contains(name) { return this._set.has(name); },
    },
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 1920, height: 1080 }),
    appendChild(child) { this.children.push(child); return child; },
    removeChild(child) {
      this.children = this.children.filter((c) => c !== child);
      return child;
    },
    setAttribute(name, value) { this[name] = value; },
    querySelectorAll: () => [],
    addEventListener() {},
    get firstChild() { return this.children[0] || null; },
  };
  return node;
}

/* A canvas whose context is the strict mock. */
function mockCanvas(gl) {
  const node = mockElement('gl');
  node.width = 1920;
  node.height = 1080;
  node.getContext = () => gl;
  return node;
}

// ------------------------------------------------------------------ loading

/**
 * Load `app.js` in a sandbox and return its exported internals.
 *
 * @param {object} options
 * @param {string} options.search  query string, e.g. '?debug=1&demo=1'
 * @returns {{internals: object, gl: MockGl, elements: Map<string, object>, sandbox: object}}
 */
/* Compiled module source, cached: reading and parsing `app.js` per trial is wasted work
 * across a sweep of over a thousand. */
let CACHED_SOURCE = null;
let CACHED_PROOF = null;
let CACHED_ART = null;

function loadRuntime({ search = '?demo=1' } = {}) {
  const gl = new MockGl();
  const elements = new Map();
  const canvas = mockCanvas(gl);
  elements.set('gl', canvas);

  const getElement = (id) => {
    if (!elements.has(id)) elements.set(id, mockElement(id));
    return elements.get(id);
  };

  let clock = 0;
  const sandbox = {
    console,
    Symbol,
    URLSearchParams,
    Math,
    JSON,
    Number,
    Array,
    Object,
    String,
    Boolean,
    Float32Array,
    Map,
    Set,
    Error,
    TypeError,
    Promise,
    isNaN,
    parseInt,
    parseFloat,
    fetch: () => Promise.reject(new Error('fetch is not available in the harness')),
    setInterval: () => 0,
    setTimeout: () => 0,
    requestAnimationFrame: () => 0,
    performance: { now: () => clock },
    location: { search, protocol: 'http:', host: '127.0.0.1:8090' },
    WebSocket: function WebSocket() { return { readyState: 0 }; },
    document: {
      getElementById: getElement,
      createElementNS: (_ns, tag) => mockElement(tag),
    },
    module: { exports: {} },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.WebSocket.OPEN = 1;
  sandbox.addEventListener = () => {};
  /* A stub `Image`, recorded so a test can fire the load itself. Decoding a PNG is the
   * browser's job; what this harness verifies is what the library does with the result. */
  sandbox.__images = [];
  sandbox.Image = function Image() {
    const image = { naturalWidth: 1024, naturalHeight: 1024, onload: null, onerror: null };
    Object.defineProperty(image, 'src', {
      set(value) { image._src = value; },
      get() { return image._src; },
    });
    sandbox.__images.push(image);
    return image;
  };
  sandbox.innerWidth = 1920;
  sandbox.innerHeight = 1080;
  sandbox.devicePixelRatio = 1;

  if (CACHED_SOURCE === null) {
    CACHED_SOURCE = fs.readFileSync(path.join(RUNTIME_DIR, 'app.js'), 'utf8');
  }
  if (CACHED_PROOF === null) {
    CACHED_PROOF = fs.readFileSync(path.join(RUNTIME_DIR, 'proof.js'), 'utf8');
  }
  vm.createContext(sandbox);
  /* Loaded in the page's order: proof.js first, so `TF_PROOF` exists when app.js takes
   * its first frame. Running them in the other order here would test a load order the
   * browser never uses. */
  vm.runInContext(CACHED_PROOF, sandbox, { filename: 'proof.js' });
  const proofExports = sandbox.module.exports;
  sandbox.module = { exports: {} };
  if (CACHED_ART === null) {
    CACHED_ART = fs.readFileSync(path.join(RUNTIME_DIR, 'art.js'), 'utf8');
  }
  vm.runInContext(CACHED_ART, sandbox, { filename: 'art.js' });
  const artExports = sandbox.module.exports;
  sandbox.module = { exports: {} };
  vm.runInContext(CACHED_SOURCE, sandbox, { filename: 'app.js' });
  sandbox.proofExports = proofExports;
  sandbox.artExports = artExports;

  return {
    internals: sandbox.module.exports,
    proof: sandbox.proofExports,
    proof_art: sandbox.artExports,
    gl,
    elements,
    sandbox,
    canvas,
    advance: (ms) => { clock += ms; return clock; },
    now: () => clock,
  };
}

/** Build a Runtime with a scene already loaded from a plain JSON object. */
function buildRuntime(scene, options = {}) {
  const loaded = loadRuntime(options);
  const { Runtime } = loaded.internals;
  if (!Runtime) throw new Error('app.js did not export Runtime');

  const runtime = new Runtime(loaded.canvas);

  /* `loadScene` fetches; the harness injects instead, so the test controls the payload
   * byte for byte. The rest of the method's setup is replicated here deliberately — if it
   * gains a step, the harness must gain it too and a test will say so.
   *
   * The boundary validation is NOT skipped: it is the thing a malformed payload must hit
   * first, so the harness has to run it exactly where the browser does. */
  runtime.scene = loaded.internals.validateScene(scene);
  for (const camera of scene.cameras) runtime.cameras.set(camera.id, camera);
  runtime.setCamera(options.camera || 'CAM_1', 'cut', 0);
  for (const joint of scene.joints) {
    runtime.pose.set(joint.id, { offset: [0, 0, 0], rot: 0, channels: [] });
  }
  runtime.bodyPoints = {};
  for (const [name, resolve] of Object.entries(loaded.internals.BODY_POINTS)) {
    const point = resolve(scene.joints);
    if (point) runtime.bodyPoints[name] = point;
  }
  runtime.propOffsets = new Map();
  runtime.propHolds = [];
  for (const quad of scene.quads) {
    if (quad.live) runtime.charts.set(quad.id, runtime._series(quad.id));
  }

  return { ...loaded, runtime };
}

module.exports = { MockGl, loadRuntime, buildRuntime, mockElement, RUNTIME_DIR };
