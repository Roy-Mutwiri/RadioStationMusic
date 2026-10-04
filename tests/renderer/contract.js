/*
 * Renderer draw-contract sweep.
 *
 * Drives the real `Runtime.draw()` through every combination that can reach the geometry
 * boundary and reports any that violates the contract:
 *
 *   68 catalogued actions  x  7 frozen cameras  x  debug on/off
 *   + the four motion kinds (reach / pose / oscillation / prop-carry)
 *   + every prop state (free / acquired / releasing)
 *   + both symbols, every intensity band, the BPM range
 *   + every camera transition
 *
 * Run by `tests/unit/test_visual_renderer_contract.py`. Reads a JSON job on stdin
 * ({scene, actions, chains}) so the catalogue stays the single source of truth on the
 * Python side, and writes a JSON report on stdout.
 *
 * `STRICT_GEOMETRY` is on under Node, so every vector is validated at every draw call —
 * which is the point: a browser only tells you that *something* was not iterable.
 */
'use strict';

const { buildRuntime } = require('./harness.js');

const CAMERAS = ['CAM_1', 'CAM_2', 'CAM_3', 'CAM_4', 'CAM_5', 'CAM_6', 'CAM_7'];
const ART_MODES = ['proof', 'blockout'];
const BANDS = ['b0_dormant', 'b1_quiet', 'b2_steady', 'b3_focused', 'b4_alert', 'b5_peak'];
const TRANSITIONS = ['cut', 'crossfade', 'push_continue'];

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (chunk) => { data += chunk; });
    process.stdin.on('end', () => resolve(JSON.parse(data)));
    process.stdin.on('error', reject);
  });
}

/** A START command shaped exactly as `renderer.py::action_command` produces it. */
function actionCommand(action, overrides = {}) {
  return {
    v: 1,
    kind: 'START',
    seq: 1,
    action_id: action.action_id,
    category: action.category,
    duration_ms: action.duration_ms,
    blend_in_ms: action.blend_in_ms,
    blend_out_ms: action.blend_out_ms,
    amplitude: action.amplitude,
    anchor: action.anchor,
    gaze_target: action.gaze_target,
    character_state: action.character_state,
    chain_id: action.chain_id,
    chain_step: action.chain_step,
    interruptibility: 'always',
    ...overrides,
  };
}

function gazeCommand(target, overrides = {}) {
  return {
    v: 1, kind: 'GAZE', seq: 1, target,
    from: 'monitor_main', transit_ms: 320, dwell_ms: 1400,
    head_contribution: 0.5, angle_degrees: 20, forces_blink: false,
    ...overrides,
  };
}

function screenCommand(symbol, band, overrides = {}) {
  return {
    v: 1, kind: 'SCREEN', seq: 1,
    active_symbol: symbol, band, energy: 50,
    honesty: { charts_advance: true, stale_marker: null, price_displayable: false },
    station_mode: 'normal',
    now_playing: { bpm: 120 },
    ...overrides,
  };
}

function rhythmCommand(bpm, overrides = {}) {
  return {
    v: 1, kind: 'SET', seq: 1, bpm,
    downbeat_phase: 0, beat_subdivision: 1, nod_probability: 0.5,
    max_nod_degrees: 1.1, max_consecutive_beats: 8, mandatory_gap_seconds: 25,
    ...overrides,
  };
}

/* Run one scenario and capture any contract failure with full attribution. */
function trial(scene, { camera, debug, apply, frames = 10, step = 33.4, art = 'proof' }) {
  /* Art mode is part of the matrix, not a default. Proof mode draws ~4.5x the primitives
   * of the blockout view from the same joint transforms, so a geometry fault can exist in
   * one and not the other. */
  const search = `?demo=1&art=${art}${debug ? '&debug=1' : ''}`;
  const built = buildRuntime(scene, { search, camera });
  const { runtime, gl, advance } = built;

  try {
    if (apply) apply(runtime, built);
    let drawn = 0;
    for (let frame = 0; frame < frames; frame++) {
      const now = advance(step);
      runtime.draw(now);
      drawn += runtime.drawCalls;
      if (debug) {
        /* The debug overlay projects the same geometry through the same matrix. It is a
         * development tool and must not be able to break the production draw path, so it
         * is exercised here rather than trusted. */
        built.internals.updateOverlay(runtime);
      }
    }
    /* Finiteness and arity are checked inside the mock at every uniform call, so there
     * is nothing left to validate here. What is worth asserting is that drawing
     * happened at all: a trial that silently drew nothing would pass every check. */
    if (drawn === 0) {
      return { ok: false, message: 'the trial produced no draw calls' };
    }
    return { ok: true, drawCalls: drawn, recorded: gl.drawArraysCount };
  } catch (error) {
    return {
      ok: false,
      name: error.name,
      message: error.message,
      primitive: error.primitive || runtime.primitive || null,
      field: error.field || null,
      expected: error.expected || null,
      received: error.received || null,
      stack: String(error.stack || '').split('\n').slice(0, 5).join(' | '),
    };
  }
}

async function main() {
  const job = await readStdin();
  const { scene, actions } = job;
  const failures = [];
  let trials = 0;
  const record = (label, result) => {
    trials++;
    if (!result.ok) failures.push({ label, ...result });
  };

  // ---- 1. the bare scene on every camera, every art mode, debug on and off
  for (const art of ART_MODES) {
    for (const camera of CAMERAS) {
      for (const debug of [false, true]) {
        record(`scene/${art}/${camera}/debug=${debug}`,
               trial(scene, { camera, debug, art }));
      }
    }
  }

  // ---- 2. every catalogued action on every camera, both art modes, debug on and off
  for (const art of ART_MODES) {
    for (const action of actions) {
      for (const camera of CAMERAS) {
        for (const debug of [false, true]) {
          record(
            `action/${art}/${action.action_id}/${camera}/debug=${debug}`,
            trial(scene, {
              camera, debug, art,
              apply: (runtime) => {
                runtime.handle([actionCommand(action)]);
                if (action.gaze_target) {
                  runtime.handle([gazeCommand(action.gaze_target)]);
                }
              },
            }),
          );
        }
      }
    }
  }

  // ---- 3. every action held across its whole envelope on the three key cameras
  //
  // Blend-in, hold and blend-out are separate code paths in `_advancePose`, and the
  // blend-out is the one a 10-frame trial can miss. CAM_1/CAM_4/CAM_6 because they are
  // the hero, the close-up and the hands shot — the projections most sensitive to a
  // joint landing in the wrong place.
  for (const action of actions) {
    for (const camera of ['CAM_1', 'CAM_4', 'CAM_6']) {
      record(
        `envelope/${action.action_id}/${camera}`,
        trial(scene, {
          camera, debug: true, frames: 90, step: 60,
          apply: (runtime) => { runtime.handle([actionCommand(action)]); },
        }),
      );
    }
  }

  // ---- 4. prop-carry states
  //
  // FREE: the mug sits on its world anchor. ACQUIRED: it follows the hand. RELEASING:
  // it interpolates back. Each is a different transform source for the same box, which
  // is exactly the kind of split that produced this bug.
  const coffee = ['reach_cup', 'pick_cup', 'hold_cup', 'sip', 'coffee_drink',
                  'breath_after_sip', 'place_cup', 'coffee_reset'];
  for (const camera of CAMERAS) {
    record(`prop/chain/${camera}`, trial(scene, {
      camera, debug: true, frames: 120, step: 50,
      apply: (runtime) => {
        for (const id of coffee) {
          const action = actions.find((a) => a.action_id === id);
          if (action) runtime.handle([actionCommand(action)]);
        }
      },
    }));
  }
  // The pen chain: a second prop, acquired by the other hand.
  for (const camera of CAMERAS) {
    record(`prop/pen/${camera}`, trial(scene, {
      camera, debug: true, frames: 120, step: 50,
      apply: (runtime) => {
        for (const id of ['note_gaze_down', 'reach_pen', 'acquire_pen',
                          'note_write_short', 'return_pen', 'note_gaze_up']) {
          const action = actions.find((a) => a.action_id === id);
          if (action) runtime.handle([actionCommand(action)]);
        }
      },
    }));
  }

  // ---- 5. the four motion kinds, isolated, with a parent transform already active
  //
  // The reach pin composes with the parent chain and the oscillation applies after it.
  // Running a reach while the torso is mid-lean is the case that would overshoot.
  const kinds = {
    reach: 'mouse_move',
    pose: 'lean_forward',
    oscillation: 'typing_long',
    prop: 'sip',
  };
  for (const [kind, id] of Object.entries(kinds)) {
    const action = actions.find((a) => a.action_id === id);
    if (!action) continue;
    for (const camera of CAMERAS) {
      record(`kind/${kind}/${camera}`, trial(scene, {
        camera, debug: true, frames: 60, step: 50,
        apply: (runtime) => {
          // Parent transform first, then the motion under test on top of it.
          const lean = actions.find((a) => a.action_id === 'lean_forward');
          if (lean) runtime.handle([actionCommand(lean)]);
          runtime.handle([actionCommand(action)]);
        },
      }));
    }
  }

  // ---- 6. gaze at every target, including the debug sight lines
  for (const target of Object.keys(scene.gaze_targets)) {
    record(`gaze/${target}`, trial(scene, {
      camera: 'CAM_1', debug: true, frames: 20,
      apply: (runtime) => {
        runtime.handle([gazeCommand(target, { head_contribution: 1.0 })]);
      },
    }));
  }

  // ---- 7. market and music state, both symbols, every band, the BPM range
  for (const symbol of ['XAUUSD', 'BTCUSD']) {
    for (const band of BANDS) {
      record(`market/${symbol}/${band}`, trial(scene, {
        camera: 'CAM_3', debug: true, frames: 20,
        apply: (runtime) => { runtime.handle([screenCommand(symbol, band)]); },
      }));
    }
  }
  for (const bpm of [null, 72, 100, 128, 176]) {
    record(`music/bpm=${bpm}`, trial(scene, {
      camera: 'CAM_1', debug: true, frames: 40,
      apply: (runtime) => {
        runtime.handle([rhythmCommand(bpm)]);
        const nod = actions.find((a) => a.action_id === 'micro_head_nod');
        if (nod) runtime.handle([actionCommand(nod)]);
      },
    }));
  }
  // A frozen feed must not advance the charts, and must not break them either.
  record('market/feed_down', trial(scene, {
    camera: 'CAM_1', debug: true, frames: 20,
    apply: (runtime) => {
      runtime.handle([screenCommand('NO_ACTIVE_MARKET', 'b0_dormant', {
        energy: null,
        honesty: { charts_advance: false, stale_marker: 'NO FEED',
                   price_displayable: false },
      })]);
    },
  }));

  // ---- 8. camera transitions: a cut must change coordinates, not data structure
  for (const from of CAMERAS) {
    for (const transition of TRANSITIONS) {
      record(`transition/${from}/${transition}`, trial(scene, {
        camera: from, debug: true, frames: 30,
        apply: (runtime) => {
          for (const to of CAMERAS) {
            runtime.handle([{ v: 1, kind: 'CAMERA', seq: 1, camera_id: to,
                              transition, parallax: 0.008 }]);
          }
        },
      }));
    }
  }

  // ---- 9. a RESYNC, which sets state without any preceding command
  record('resync', trial(scene, {
    camera: 'CAM_5', debug: true, frames: 20,
    apply: (runtime) => {
      runtime.handle([{
        v: 1, kind: 'RESYNC', seq: 1, character_state: 'analyzing',
        gaze_target: 'monitor_main', camera_id: 'CAM_3', running: [],
        fatigue_phase: 0.4,
        target: { width: 1920, height: 1080, fps: 30 },
      }]);
    },
  }));

  process.stdout.write(JSON.stringify({
    trials,
    failures,
    cameras: CAMERAS.length,
    actions: actions.length,
    art_modes: ART_MODES,
  }, null, 2));
  process.exitCode = failures.length ? 1 : 0;
}

main().catch((error) => {
  process.stdout.write(JSON.stringify({
    trials: 0,
    failures: [{ label: 'harness', name: error.name, message: error.message,
                 stack: String(error.stack).split('\n').slice(0, 6).join(' | ') }],
  }, null, 2));
  process.exitCode = 1;
});
