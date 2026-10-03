# V8 Report — behaviour fixes, Camera Director, placeholder renderer

Covers the work after V1/V6/V7 were accepted. Sections match the requested boundary
report: **A** behaviour fixes, **B** the Camera Director, **C** the placeholder renderer,
**D** the GPU benchmark, **E** the OBS browser-source test, **F** remaining art.

Two of these are **not done**, for a reason this document states plainly rather than
burying: §D and §E need a browser with a GPU process driven by hand, and the browser
automation available in this session cannot load a local address. They are specified,
scripted and runnable in one command — but they have not been run, and no number in them
is reported as measured.

---

## A. Behaviour fixes

### A.1 `posture_reset` starvation

**The defect.** Over an eight-hour soak, `posture_reset` fired **zero** times. Raising
its weight would have hidden three separate structural causes, none of which is a
weighting problem:

1. It was eligible only from a state whose dwell was shorter than its own cooldown, so
   the state never lasted long enough for it to come up.
2. It sat in the `FATIGUE` category behind a fatigue ramp, so it could not fire until
   fatigue was already high — by which point other fatigue actions outcompeted it.
3. It shared an anti-repetition key with `shoulder_shift`, a 35–130 s micro movement.
   Every shoulder shift re-penalised the posture reset.

**The fix: a posture *family*, not an animation.** Long-horizon body maintenance is now
its own category with its own interval, split into two tiers:

| Tier | Members | Interval |
|---|---|---|
| Major (`posture`) | `chair_reposition`, `spine_straighten`, `posture_lean_back`, `shoulder_roll`, `neck_reset` | 420–1 020 s |
| Minor (`posture_minor`) | `elbow_reposition`, `hand_rest_reset` | 210–540 s |

`shoulder_roll` moved out of `FATIGUE`; `shoulder_shift` was re-keyed to itself so it no
longer suppresses the family. Group cooldowns are sampled ranges — `posture` 420–1 800 s,
`posture_minor` 210–720 s — never constants.

Eligibility is a **gate**, matching the brief's three conditions: eligible from
`IDLE_FOCUS`, `WAITING` or `ANALYZING` when time since the last major reset exceeds a
sampled threshold, **and** no object chain is in flight, **and** no high-priority reaction
is pending. `_posture_gate` returns a per-tier multiplier rather than a boolean, so the
urge builds rather than switching on.

**Measured** (`breakout`, seed 7, salience spikes every 420 s):

| Duration | Major / h | Minor / h | During a chain | During a reaction |
|---|---|---|---|---|
| 2 h | 4.00 | 6.50 | 0 | 0 |
| 8 h | 3.38 | 6.38 | 0 | 0 |
| 24 h | 3.46 | 6.17 | 0 | 0 |
| 24 h `quiet` | 3.04 | 5.96 | 0 | 0 |

Target was "several per hour, not dozens". **3.4–4.0 major and ~6 minor per hour**, and
zero in either forbidden context across 24 hours.

### A.2 Fatigue phase saturation

**The defect.** Fatigue was a one-direction accumulator. It pinned at its ceiling after
roughly 16 hours — a character permanently on the late shift. Increasing the coffee
decrement produced the mirror failure: it never rose above 0.16.

**The fix: a cyclic rhythm where phases set *rates*, not values.** `visual/rhythm.py`
replaces the accumulator with `FOCUS_BUILD → DEEP_WORK → FATIGUE_RISE → RECOVERY →
FOCUS_BUILD`. Nothing in it ever assigns a fatigue level; every phase multiplies the
integration rate, which is what makes "no sudden state jumps" structural rather than a
thing to remember:

- `FATIGUE_PHASE_SCALE` — `FOCUS_BUILD` 0.40, `DEEP_WORK` 1.00, `FATIGUE_RISE` 1.55,
  `RECOVERY` 0.0.
- Recovery influences (coffee, posture reset, long gaze hold, session transition) raise
  the **recovery rate**. They do not subtract from the level, which is why no influence
  can produce a step.
- Phase transitions use **sampled** thresholds (`FOCUS_TARGET_RANGE`,
  `DEEP_WORK_EXIT_RANGE`, `RECOVERY_TRIGGER_RANGE`, `RECOVERY_EXIT_RANGE`) plus a minimum
  dwell per phase, so the cycle cannot lock to a fixed period.
- Bounded at both ends: `FATIGUE_FLOOR` 0.02, `FATIGUE_CEILING` 0.95.

**Measured** (`breakout`, seed 7; plus a 24 h `quiet` run at seed 11 as a control):

| | 2 h | 8 h | 24 h | 24 h `quiet` |
|---|---|---|---|---|
| min | 0.080 | 0.080 | 0.080 | 0.080 |
| max | 0.842 | 0.842 | 0.852 | 0.725 |
| mean | 0.433 | 0.449 | 0.472 | 0.395 |
| completed cycles | 0 | 2 | **7** | **7** |
| cycle lengths (h) | — | 4.00, 3.47 | 2.22 – 4.00 | 2.48 – 3.74 |
| time near floor | 15.8 % | 6.2 % | **2.1 %** | 4.0 % |
| time near ceiling | 8.3 % | 3.5 % | **2.4 %** | **0.0 %** |
| pinned at ceiling | no | no | **no** | no |
| pinned at floor | no | no | **no** | no |

Against the four stated prohibitions over 24 hours: it does not rise monotonically
(15 turning points), does not pin at the ceiling (2.4 % near it, never pinned), does not
pin at zero (2.1 % near the floor, never pinned), and does not oscillate on a fixed
schedule — the seven cycle lengths span 2.22 to 4.00 hours.

**The `quiet` control is the strongest evidence the rhythm is market-driven rather than a
free-running oscillator.** On a quiet day fatigue never approaches the ceiling at all
(max 0.725, 0.0 % of time near it), and the phase shares *invert*:

| Phase | 24 h `breakout` | 24 h `quiet` |
|---|---|---|
| `deep_work` | 23.5 % | **60.3 %** |
| `recovery` | 53.3 % | 24.7 % |
| `fatigue_rise` | 17.4 % | 9.2 % |
| `focus_build` | 5.8 % | 5.8 % |

A loud market tires him and he spends the day recovering from pushes; a quiet one lets him
sit in deep work. That is the behaviour the design intends, and it is not something a
fixed-period cycle could produce.

**One thing I would flag rather than quietly tune.** `focus_build` sits at 5.8 % in both
scenarios — about 11 minutes of a ~3.2 h cycle, which is exactly its minimum dwell. The
phase therefore always exits as soon as it is allowed to, and is close to vestigial. It
still applies its 0.40 rate scale for those minutes, and every stated criterion passes, so
I have left the four-phase shape the brief asked for intact — but widening
`FOCUS_TARGET_RANGE` is the next thing I would look at, and it should be a deliberate
decision rather than something I change while reporting.

### A.3 Tests

`tests/unit/test_visual_rhythm.py` — 39 tests. The eight requested proofs, plus the
boundary cases: that no influence can step the level, that the ceiling and floor hold
under adversarial influence streams, that cycle lengths vary across seeds, that phase
transitions respect minimum dwell, and that a 24-hour accelerated run is neither pinned
nor periodic.

Posture coverage lives in `test_visual_behavior.py`: the gate's three conditions
individually, the two tiers' independent intervals, that the family shares no
anti-repetition key with `shoulder_shift`, and the per-hour rate at each soak length.

---

## B. V8 Camera Director

`tradefix_radio/visual/camera_director.py`. Chooses between the **seven frozen V1
transforms** and invents no geometry.

### B.1 The design

Three ideas, in this order:

**A cut needs a motivation, a satisfied minimum hold, and no veto — all three.**
Motivations are scored; the hold is a gate; vetoes are checked last and absolutely. There
is no path by which a timer reaching a round number causes a cut: `hold_expired` is itself
a motivation that must then survive the vetoes.

**Safety is a veto, never a weight.** A weight can always be overcome by enough other
multipliers. `MID_BLEND`, `OBJECT_ACQUISITION`, `COMMITTED_CHAIN_INVISIBLE` and
`REACTION_LOCKOUT` are filters applied after scoring.

**`CAM_6` is gated, not weighted.** The hands shot is simply unavailable unless a desk
interaction is in flight, because a weight however small eventually fires on an empty
desk.

Holds are sampled **log-normally** about a per-band median (σ 0.45), which produces mostly
long holds with an occasional short one. A uniform draw over the same range gives too many
mid-length shots and reads as a rotation. Anti-repetition acts on **camera families**
(hero / profile / work / intimate / atmosphere), because `CAM_1 → CAM_7 → CAM_1` is three
hero shots running even though no camera repeated.

Transitions are only the three permitted: hard cut, short crossfade, slow digital-parallax
push. The `CAM_1 → CAM_4` push continuation is rationed to a few per hour.

### B.2 A failed acceptance criterion, found and fixed

The first soak reported **`CAM_7` 22.1 %, `CAM_3` 19.8 %, `CAM_1` 15.9 %** of airtime.
`CAM_1` was third. That fails the criterion that `CAM_1` remain primary.

Two causes, and neither was a weighting accident:

1. `CAMERA_PLAN.md` designated `CAM_7` the home shot and gave it the 35 % share cap
   against `CAM_1`'s 25 %. The code faithfully implemented a plan that contradicted the
   acceptance criterion.
2. Primacy was expressed as a **flat 1.35× multiplier** on the home shot. `CAM_1` then
   lost twice over: it had the smaller cap, and the family penalty suppressed it whenever
   `CAM_7` — its own family-mate, winning on the flat bonus — had just been used.

**The fix.** `CAM_1` is now the home shot and default, with the 34 % cap; `CAM_7` takes
24 %. More importantly, primacy is now **return to master**, not a constant: `CAM_1`'s pull
is 1.0 while it is live or just left, and grows with time away, reaching 2.6× after 420 s.
A flat bonus makes the home shot win nearly every contest it enters and the stream stops
exploring; a growing one makes it the shot the stream keeps coming *back* to, which is
what primary means. It also composes correctly with the family penalty — `CAM_1 → CAM_7 →
CAM_1` is still discouraged, while a return after minutes elsewhere is not.

**A third cause, in the frozen artefact itself.** Chasing this down found the blockout's
`CAM_7` entry carrying `"hour_share_cap": 0.35` and a composition string beginning
"PRIMARY STREAM CAMERA and the default" — scheduling claims living in the geometry file,
which §5 of `GEOMETRY_FREEZE.md` explicitly declares tunable. `validate_metadata` then
cross-checked the code against them, so changing a value declared *not frozen* required
editing the *frozen* file. That is how a freeze quietly stops meaning anything.

**CR-003** removes the field, corrects both cameras' prose, and changes
`validate_metadata` to **reject** any blockout declaring a share cap rather than
cross-check it — the rule is now enforced instead of documented. No geometry changed: not
one position, target, lens, FOV, distance, frame size or clearance. The scheduling change
that follows is deliberately *not* part of the CR, because §5 puts it outside the freeze.
`CAMERA_PLAN.md` §3 and §5 are corrected, with the measurement that forced the change
recorded in place.

**And a dead validator.** `scripts/visual/validate_blockout.py` — which V1 established as
a permanent regression suite — had been **crashing on import since CR-001**. It still read
`gaze_rules.yaw_limit_degrees`, a key CR-001 deleted when participation tiers replaced the
flat angular limits, so it raised `KeyError` at module scope and never ran a single check.
Nothing noticed because the freeze *tests* carried the enforcement independently.

Check 4 is rewritten against the tier model it was supposed to be testing: each gaze
target's actual angle is validated against the envelope of the tier it **declares**, which
is CR-001's whole point — a large angle is fine when the right body parts are declared to
reach it; a target claiming "eyes only" for a 65° yaw is not. A new check 4b asserts no
camera carries scheduling metadata, so CR-003's rule cannot regress. All nine checks now
run and pass, including the tier validation for the first time.

**The test that let it through has been replaced.** The old
`test_cam_1_and_cam_7_remain_primary` asserted only `>= 0.15` for the primary camera and
`>= 0.28` for the hero family. Both passed at the failing numbers. A floor is not primacy.
The replacement asserts `CAM_1` holds the single largest share, leads the runner-up by at
least 15 %, and stays under its own cap — plus a second test that it leads in `quiet`,
`breakout` *and* `extreme_volatility`, so primacy cannot be an artefact of one band mix.

### B.3 Camera soak

`breakout`, seed 7, salience spikes every 420 s:

| | 2 h | 8 h | 24 h |
|---|---|---|---|
| cuts | 94 | 351 | 1 092 |
| cuts / h | 47.0 | 43.9 | 45.5 |
| hold min / median / max (s) | 42 / 69 / 170 | 35 / 70 / 170 | 31 / 70 / 170 |
| mean hold (s) | 76 | 82 | 79 |
| `CAM_1` share | **24.7 %** | **23.0 %** | **22.4 %** |
| runner-up | `CAM_3` 19.7 % | `CAM_3` 18.5 % | `CAM_3` 18.5 % |
| `CAM_5` share | 15.0 % | 15.2 % | 15.0 % |
| `CAM_6` share | 4.1 % | 4.8 % | 5.0 % |
| cameras used | 7 / 7 | 7 / 7 | 7 / 7 |
| top 3-cut sequence | 4.30 % | 2.29 % | **1.92 %** |
| A–B–A alternations | 0 | 0 | **0** |
| **unsafe cuts** | **0** | **0** | **0** |
| `CAM_6` without interaction | 0 | 0 | **0** |

Against the stated acceptance:

- **Zero unsafe cuts** — across 1 092 cuts at 24 h.
- **No short deterministic rotation** — the most frequent three-cut sequence takes 1.92 %
  of all sequences, and there are zero A–B–A alternations.
- **All cameras used eventually** — seven of seven at every duration.
- **`CAM_1` primary** — largest share at every duration, leading the runner-up by 4–5
  points.
- **`CAM_5` provides breathing room** — a steady 15 %.
- **`CAM_6` only on visible close interaction** — 4–5 %, zero unmotivated.

Hold length is band-driven, so `breakout` is the compressed end by design. The same
director on `quiet` over 24 hours gives **27 cuts/h, median hold 130 s, max 300 s** — the
"30 s to several minutes" the brief asks for — with `CAM_1` still primary at 25.4 %, zero
unsafe cuts across 648 cuts, zero alternations, and the top three-cut sequence at 1.85 %.
The motivation mix shifts sensibly too: `diversity` leads in `quiet` (248 of 648) because
long holds leave cameras starved, where `hold_expired` leads in `breakout`.

**Two metrics that look bad and are not.** `action visibility` is 10–14 %: a 70-second
hold cannot follow actions lasting two to eleven seconds, and the only way to raise it
materially is to cut far more often, which is the cutting the brief rules out.
`state_alignment` — 32–34 % — is the meaningful one, because character states last
10–70 s, which a hold *can* track.

**One real cost of the fix.** `below_minimum_hold_floor` is the second most frequent veto
(654 812 evaluations at 24 h). That is not a problem — it means the floor is doing the
work — but it does mean the 15 s `DOUBLE_CUT_WINDOW` can never be the veto that fires. It
is retained deliberately as the hard limit that must survive any future lowering of the
floor, and `test_the_floor_subsumes_the_double_cut_window` asserts that relationship
rather than pretending the veto is reachable.

### B.4 Camera tests

`tests/unit/test_visual_camera_director.py` — 49 tests, 11 marked `slow`. Safety
assertions are hard; pacing assertions are deliberately wide, because the honest claim is
"this does not read as a rotation", not "this exact number is correct".

---

## C. Placeholder renderer

`visual/runtime/index.html` + `visual/runtime/app.js`, served by
`tradefix_radio/visual/service.py`. WebGL2, one shader program, zero textures.

### C.1 What it is

Every primitive is a single six-vertex screen-space quad shaded by an **SDF fragment
branch** (rect / ellipse / outline), which is why the placeholder needs no art to exist at
all. The scene is built by `visual/scene.py` **from the frozen blockout** — 10 boxes,
8 quads, 15 joints, 7 cameras — so it cannot drift from the geometry the behaviour
director reasons about.

The character is a **parented hierarchy**, not independent sprites:
`pelvis → torso → shoulder → forearm → hand` and `neck → head → eye → lid`, with
headphones parented to the head. `test_no_joint_parent_is_dangling_or_cyclic` and
`test_face_joints_are_parented_to_the_head` pin it.

Motion is eased, never linear: four named curves (`inOut`, `attention`, `settle`,
`drift`), a 180 ms floor on gaze transit, eyes leading the head by the declared share, and
a continuous ~4 s breathing cycle under every action. Parallax is **derived from blockout
depth**, not authored per layer.

It is labelled `PLACEHOLDER` twice over — a centred watermark and a top banner reading
`NOT FINAL ARTWORK` — and a test asserts both, because this page must never be mistaken
for the product.

### C.2 The verification that mattered

The placeholder exists to check that the frozen camera transforms produce the
compositions `CAMERA_PLAN.md` claims. That claim is checkable **without a GPU**: run the
renderer's own projection — right-handed look-at with +Z up, then perspective — in Python
and compare frame positions.

| Element | Plan claims | Projection gives | Δ |
|---|---|---|---|
| monitor top bezel | 30 % | 29.9 % | −0.1 |
| shoulders | 21 % | 21.6 % | +0.6 |
| chin | 37 % | 36.8 % | −0.2 |
| eye line | 46 % | 46.0 % | −0.0 |
| crown | 65 % | 64.5 % | −0.5 |

Worst disagreement **0.6 percentage points**. This is now
`test_cam_1_projection_matches_the_camera_plan`, and it is the single most useful test in
the renderer suite: if `CAM_1`'s bezel line stops landing near 30 % of frame height,
either the plan or the geometry is wrong, and a screenshot would not tell you which.

A companion test asserts the eye line is in frame from every face camera, which catches
the classic error of a correct camera position with the wrong up-vector — `+Y` instead of
`+Z` rolls every camera on its side while leaving the position table looking right.

### C.3 Verified running

`tradefix visual serve --scenario breakout --seed 7`:

- `/api/visual/health` → `{"status":"ok","protocol":1}`
- `/api/visual/scene` → the blockout-derived scene, 10 boxes / 8 quads / 15 joints /
  7 cameras
- `index.html` 5 656 bytes, `app.js` 33 084 bytes, parses under `node --check`
- Over a 16-second WebSocket session the renderer socket received
  `{RESYNC: 1, SET: 1, SCREEN: 8, START: 14, GAZE: 4}` with correct payloads —
  e.g. `START mouse_scroll 899 ms blend 88/136 amp 1.0268 anchor=ANCHOR_MOUSE
  gaze=monitor_main`, `GAZE → panel_upper_left transit 829 ms dwell 1 435 ms head 0.742
  angle 130.98 blink=True`, `SCREEN XAUUSD band=b4_alert energy=79.0 advance=True
  price_ok=False`
- Telemetry round-tripped; `renderer_alive: True`

**Two bugs found and fixed in getting there**, both of the silent kind:

- The WebSocket returned **HTTP 403 with no traceback**. Root cause:
  `from __future__ import annotations` plus a function-local `WebSocket` import meant
  FastAPI's `get_type_hints` could not resolve `socket: WebSocket` against module globals,
  so the route closed every connection instead of accepting it. FastAPI imports now sit at
  module scope with a docstring explaining why they must stay there.
  `test_the_socket_opens_and_resyncs` guards it.
- `@app.on_event` is **ignored entirely** when an explicit `lifespan` is passed, so the
  station link would have been constructed and never started. It is now attached as
  `runtime.link` and started by the lifespan.

Charts **freeze and show a `NO FEED` marker** when the feed is not trustworthy, rather
than advancing invented candles. A chart drawing fabricated bars on a public stream is a
fabricated price with extra steps; `test_the_screen_command_carries_the_honesty_flags`
asserts the flags rather than leaving the renderer to infer them.

### C.4 Renderer tests

`tests/unit/test_visual_renderer.py` — 45 tests: scene derivation, the joint hierarchy,
the projection check above, the wire protocol, the quality controller's shed order,
telemetry bounds, and the service routes. One asserts the service **cannot reach the
radio** — it greps its own source for station imports, because "read-only" is a claim
worth enforcing mechanically.

---

## D. GPU benchmark — **NOT RUN**

**Blocked, and nothing here is reported as measured.** The browser automation in this
session loads an error page for `http://127.0.0.1:<port>/` and `http://localhost:<port>/`
alike ("Frame with ID 0 is showing error page"), on two different ports and both
hostnames. No screenshot has been taken and no GPU figure has been obtained. A renderer
also cannot measure the GPU it runs on, so GPU utilisation and total VRAM need Task
Manager or OBS's stats panel alongside the page regardless.

**It is scripted and runs in one command.** `tradefix visual benchmark` serves the
runtime, waits for a browser to attach, collects the renderer's own frame timings for a
chosen window and prints the table:

```
tradefix visual benchmark --fps 30 --seconds 120 --json bench30.json
tradefix visual benchmark --fps 60 --seconds 120 --json bench60.json
```

Run it twice and compare. ADR-10 prefers 30 fps if the picture holds, because ACE-Step
owns this GPU and the visual layer is the tenant. The command's end-to-end path **is**
verified — a synthetic renderer that connects and reports telemetry produces a correct
summary, so the measurement will not fail on a bug in the harness:

```
  fps cap             30
  samples             9
  frames rendered     270
  fps_mean            min 29.8   mean 29.9   p95 30.0   max 30.0
  ...
```

### What *could* be measured here

Two numbers, kept apart by kind, because conflating them would be the dishonest part:

**MEASURED — the Python side.** Real wall-clock, real code, the service's own 20 Hz tick,
10 simulated minutes of `breakout`:

| | |
|---|---|
| tick mean | **0.099 ms** |
| tick p95 | 0.231 ms |
| tick max | 32.1 ms (one first-tick outlier) |
| CPU of one core | **0.198 %** |
| commands / minute | 75.1 |
| socket bandwidth | 381 B/s (1.3 MiB/h) |

The behaviour director is not a cost worth optimising. Whatever the browser turns out to
cost, this side is free.

**DERIVED — the browser side.** A workload *budget* counted from the scene by mirroring
`app.js`'s draw passes. It says how much work the renderer asks for; it cannot say how
long the GPU takes:

| | |
|---|---|
| draw calls / frame | **255** (50 boxes, 8 panels, 182 chart marks, 15 joints) |
| uniform updates / frame | 2 040 |
| vertices / frame | 1 530 |
| textures / programs | 0 / 1 |
| draw calls / s @ 30 | 7 650 |
| draw calls / s @ 60 | 15 300 |

Cost is dominated by uniform upload and per-call overhead, not fill or texture bandwidth.
**The chart marks are 182 of the 255 calls** — if the measured frame time needs reducing,
that is where to take it from first, and it costs nothing visually in a placeholder.

`scripts/visual/measure_runtime_cost.py` produces both tables, and
`test_the_workload_constants_match_the_shader_loop` fails if `app.js`'s draw loop changes
shape so the derived budget cannot go stale silently.

---

## E. OBS browser-source test — **NOT RUN**

**Blocked for the same reason, and additionally needs OBS driven by hand.** No OBS
verification has been performed: not the transparent/background mode, not refresh
stability, not the WebSocket under OBS's embedded browser, not crash behaviour, not GPU
use.

What to do, when you have the machine:

1. `tradefix visual serve --scenario breakout --seed 7`
2. Add a **stock Browser source** — no Spout, no NDI — at `http://127.0.0.1:8090/`,
   1920 × 1080.
3. Confirm: the canvas renders; the HUD panels update; `/api/visual/state` shows
   `renderers: 1` and `renderer_alive: true`; the picture keeps moving for ten minutes
   without a stall.
4. Read GPU and VRAM from OBS → View → Stats, or Task Manager → Performance → GPU.
5. `?hud=0` removes the overlay; `?fps=60` raises the cap; `?debug=1` adds the debug draw.

This is a rendering test only. The full broadcast scene is not integrated.

---

## F. Remaining art assets

**All 22 painted assets remain outstanding, and no part of §A–C is blocked on them** —
that was the point of building the behaviour architecture and the placeholder first.

There is no image-generation capability on this machine and none available to me, so I
cannot produce them and will not fabricate stand-ins. `ASSET_MANIFEST.md` is the contract
they must satisfy: per-plate layer stacks, mask requirements, pivot points, and the
depth-derived parallax values the runtime already consumes. `CAM_1`'s stack is enumerated
in full; the other six derive from the same room and depth rule and are deliberately not
enumerated, because guessing at compositions nobody has seen would be inventing
requirements.

The placeholder consumes the manifest's *structure* today, so when real plates arrive they
drop into a path that is already exercised rather than one written for them afterwards.

---

## Test status

**VISUAL OWNED TESTS: GREEN.**

| File | Tests |
|---|---|
| `test_visual_geometry_freeze.py` | 17 |
| `test_visual_behavior.py` | behaviour catalogue, locks, chains, posture gate |
| `test_visual_bridge.py` | state bridge, degradation ladder |
| `test_visual_rhythm.py` | 39 |
| `test_visual_camera_director.py` | 49 (11 `slow`) |
| `test_visual_renderer.py` | 45 |

`ruff` and `mypy` clean across `tradefix_radio/visual/`, `tradefix_radio/cli/visual.py`
and the visual tests.

**The repository-wide suite is not green**, and this report does not claim it is. Terminal
B's in-flight work leaves failures in `tests/integration` that are not mine to fix or to
report on.
