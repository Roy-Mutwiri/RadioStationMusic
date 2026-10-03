# Visual Phase V1 — References and specification

**Date:** 2026-10-03 · **Plan:** `docs/visual/VISUAL_IMPLEMENTATION_PLAN.md`
**Goal:** identity and layout locked. No runtime.

**Status: 10 of 13 milestones complete. 3 blocked on a paint capability this machine does not
have. No runtime code written, as the phase specifies.**

---

## 1. What was delivered

| # | Milestone | Artefact | State |
|---|---|---|---|
| 1.1 | Environment and software audit | `docs/visual/INITIAL_VISUAL_AUDIT.md` | **Done** |
| 1.2 | Runtime decision | `docs/visual/ADR_VISUAL_RUNTIME.md` — ADR-10…13 | **Done** |
| 1.3 | Character specification | `docs/visual/CHARACTER_BIBLE.md` | **Done** |
| 1.4 | Environment specification | `docs/visual/OFFICE_BIBLE.md` | **Done** |
| 1.5 | Motion library and behaviour director | `docs/visual/MOTION_LIBRARY.md` | **Done** |
| 1.6 | Camera plan | `docs/visual/CAMERA_PLAN.md` | **Done** |
| 1.7 | State contract | `docs/visual/STATE_BRIDGE.md` | **Done** |
| 1.8 | Numeric blockout | `visual/environment/TRADE_FIX_OFFICE_01.blockout.json` + `scripts/visual/validate_blockout.py` | **Done, verified** |
| 1.9 | Construction drawings | `visual/references/{office,character}/*.svg` + `scripts/visual/render_reference_svg.py` | **Done** |
| 1.10 | Asset directory skeleton | `visual/` with a README per directory | **Done** |
| **1.11** | **Character reference view 1** | — | **BLOCKED** |
| 1.12 | Character views 2–9 | — | Blocked behind 1.11 |
| 1.13 | Environment plates 1–7 | — | Blocked behind 1.11 |

Eight specification documents, one authoritative geometry file, two generated drawings, two
scripts, nine directory READMEs. **Zero runtime code**, per the phase's own instruction that V1
delivers no runtime.

---

## 2. The audit, and what it decided

Three measured facts determined the runtime more than any preference about engines could.

| Finding | Measurement |
|---|---|
**Free VRAM** | **2 487 MiB** of 12 288, with the desktop holding ≈4 772 MiB and ACE-Step ≈4 857 MiB |
**3D/2D animation software installed** | **None.** No Blender, Unreal, Unity, Godot, Live2D or Spine |
**Transport to OBS** | OBS 32.1.2, **all plugins stock**. No Spout, no NDI. `obs-browser` and `obs-websocket` present |
Free RAM | 6.1 GB of 31.85 GB |
GPU at rest | 40 % utilisation, 54 °C, 48.5 W of 170 W |
Encoder | 0 % — but one NVENC engine shared with Parsec |
Existing visual work in the repo | **None.** `artwork/` empty; an unfilled artwork slot exists in the architecture |
Image generator anywhere on either disk | **None** |

A lit UE5 scene wants 4–6 GB for the runtime alone. Against 2 487 MiB, beside a music model
that must not be starved, the question answered itself.

**Decision (ADR-10): hybrid 2.5D** — an offline numeric blockout locks room geometry and camera
placement, high-resolution painted layer stacks derive from it, and a WebGL2 runtime inside an
OBS browser source animates them. Quality comes from art, which the GPU does not pay for,
rather than from the renderer, which it cannot afford. Budget: **≈400 MiB**, leaving ≈2 080 MiB
of headroom.

Three supporting decisions: behaviour decided in Python and performed in the browser (ADR-11),
OBS browser source as the only transport (ADR-12), one master model over one blockout with
Blender as an offline-only tool (ADR-13).

Godot 4's 2D stack was the closest rejected alternative and is argued on its merits in
ADR §3 — it brings a real blend tree, which is the chosen option's one genuine deficit. It lost
on integration surface and on having more places to fail unattended.

---

## 3. Verification

### The blockout validator found real errors

Milestone 1.8's exit test is a scripted geometry check, and it was run rather than asserted. Its
**first run failed with 15 errors**, of which these were genuine design faults, not test bugs:

| Fault | Consequence had it shipped |
|---|---|
Monitor centres at z1250 put the top bezel at z1418 — above his chin at z1215 | **Every front camera would have had his face hidden behind a monitor.** Corrected to z1130 / z1298 |
`ANCHOR_NOTEBOOK` 1 189 mm, `ANCHOR_PEN` 1 108 mm, `ANCHOR_STREAM_PAD` 1 185 mm from the shoulder | Three objects he physically cannot reach from the chair. Seated reach is **830 mm**; all four objects relocated |
`GAZE_WINDOW` at yaw 180° | The window is *behind* him — that is what makes it his backdrop. A 3 % share would have been 180° torso turns. Replaced with a 0.2 % flagged target |
`PANEL_W2` at y3300, yaw −95° | Behind his shoulder line. Moved to y2600 |
`GAZE_KEYBOARD` at pitch −64° | Looking almost straight down at keys he touch-types. Removed |
Default gaze pitch recorded as −6.8° | Arithmetic error; the geometry gives **−14.2°** |
Front bezel clearances overstated by 20–26 mm | Recorded figures now match computed to within 1 mm |
`CAM_6` framing 2 545–3 755 mm | Cut off both the notebook and the mug once the anchors moved. Pulled back to 1 399 mm, now spans 2 431–3 869 and holds all five interactive props |

All corrections were propagated to `OFFICE_BIBLE.md`, `MOTION_LIBRARY.md` and `CAMERA_PLAN.md`.

### Current output — `python scripts/visual/validate_blockout.py`

```
[1] monitor top bezel = 1298.0  bottom = 962.0  eye = 1295.0  (delta eye/top = -3)

[2] bezel clearance for south-side cameras (must be > 0):
    CAM_1: ray z at bezel =  1395.6  clearance =   +97.6 mm  recorded 98  [OK ]
    CAM_2: not south of the desk, skipped
    CAM_3: not south of the desk, skipped
    CAM_4: ray z at bezel =  1401.6  clearance =  +103.6 mm  recorded 104  [OK ]
    CAM_5: ray z at bezel =  1564.9  clearance =  +266.9 mm  recorded 267  [OK ]
    CAM_6: desk-surface shot, exempt by rule
    CAM_7: ray z at bezel =  1436.1  clearance =  +138.1 mm  recorded 138  [OK ]

[3] anchor reach from seated shoulder (limit 830 mm):
    ANCHOR_MOUSE                   725.5 mm  [OK ]
    ANCHOR_KEYBOARD_HOME_L         431.7 mm  [OK ]
    ANCHOR_KEYBOARD_HOME_R         431.7 mm  [OK ]
    ANCHOR_MUG_BODY                736.3 mm  [OK ]
    ANCHOR_MUG_RING                759.6 mm  [OK ]
    ANCHOR_MUG_LIP                 716.4 mm  [OK ]
    ANCHOR_NOTEBOOK                817.8 mm  [OK ]
    ANCHOR_PEN                     801.7 mm  [OK ]
    ANCHOR_HP_CUP_L                286.9 mm  [OK ]
    ANCHOR_HP_CUP_R                286.9 mm  [OK ]
    ANCHOR_HP_BAND                 375.1 mm  [OK ]
    ANCHOR_STREAM_PAD              783.1 mm  [OK ]

[4] gaze target angles from the eye (yaw +-100, pitch -45..30):
    GAZE_MON_1                       yaw    +0.0  pitch  -14.2  eyes      [OK ]
    GAZE_MON_2                       yaw   -47.1  pitch   -9.8  shoulders [OK ]
    GAZE_MON_3                       yaw   +47.1  pitch   -9.8  shoulders [OK ]
    GAZE_MON_4                       yaw   +64.5  pitch   -6.2  shoulders [OK ]
    GAZE_MON_5                       yaw   -64.5  pitch   -6.2  shoulders [OK ]
    GAZE_PANEL_W1                    yaw   -70.7  pitch   +6.9  torso     [OK ]
    GAZE_PANEL_W2                    yaw   -82.7  pitch   +7.3  torso     [OK ]
    GAZE_NOTEBOOK                    yaw   -75.2  pitch  -36.6  torso     [OK ]
    GAZE_MOUSE                       yaw   -47.5  pitch  -39.4  shoulders [OK ]
    GAZE_MUG                         yaw   +55.8  pitch  -33.6  shoulders [OK ]
    GAZE_MIDDLE_DISTANCE             yaw    +0.0  pitch   -0.5  eyes      [OK ]
    GAZE_CAMERA                      dynamic, skipped
    GAZE_OVER_SHOULDER_WINDOW        yaw  -161.6  pitch   +9.1  [exempt: torso turn]

[5] default gaze pitch to MON_1 = -14.24 deg, claimed -14.20

[6] monitor x extents (desk panels must not overlap):
    MON_5     1593.8 ..  2086.2
    MON_2     2201.0 ..  2799.0
    MON_1     2901.0 ..  3499.0
    MON_3     3601.0 ..  4199.0
    MON_4     4313.8 ..  4806.2
    array span = 1594 .. 4806  claimed [1594, 4806]

[7] reserved hand envelope intrusion:
    MOUSE          inside envelope  [allowed]
    KEYBOARD       inside envelope  [allowed]
    (objects not listed are outside the envelope)

[8] camera positions inside room bounds:
    CAM_1: (3200, 900, 1620)  [OK ]
    CAM_2: (5950, 2750, 1340)  [OK ]
    CAM_3: (2780, 4080, 1600)  [OK ]
    CAM_4: (3340, 1750, 1500)  [OK ]
    CAM_5: (5400, 700, 2250)  [OK ]
    CAM_6: (3981, 1691, 1259)  [OK ]
    CAM_7: (4550, 1180, 1690)  [OK ]

[9] gaze shares sum = 1.000; screen share = 0.890 (target 0.89)

==============================================================
ALL CHECKS PASSED
```

### Drawings

```
$ python scripts/visual/render_reference_svg.py
wrote visual\references\office\TRADE_FIX_OFFICE_01_plan.svg  (25,111 bytes)
wrote visual\references\character\TF_TRADER_01_proportions.svg  (21,174 bytes)
```

Both generated **from the blockout**, so they cannot drift out of agreement with the geometry.
Verified well-formed XML with no off-canvas coordinates; all seven cameras with their FOV
wedges, all five zones, and every key callout present.

**Not visually confirmed.** The Chrome extension would not screenshot a localhost page in this
session (site permissions), so the drawings were checked structurally and numerically rather
than by eye. Serve `visual/references/` over HTTP and open `index.html` to review them.

### Lint

```
$ python -m ruff check scripts/visual/
All checks passed!
$ python -m ruff check tradefix_radio/
All checks passed!
```

One real defect was caught by lint rather than by the eye: a dead `INK_200 if False else
INK_300` ternary referencing an undefined name. It never evaluated at runtime, which is exactly
why it survived a successful run.

`N806` was added to the existing `scripts/**` per-file-ignores, with a reasoned comment, because
the drawing generators work in geometry where `W`/`H`/`CX`/`CY`/`HU` are the conventional names.

---

## 4. What is blocked, and the decision it needs

**There is no image generator on this machine** — no ComfyUI, no Stable Diffusion, no local
checkpoint, and nothing in the repository that produces a raster image. And I have no
image-generation tool in this session. So the sixteen painted plates — nine character views,
seven environment plates — cannot be produced here.

Everything that makes them reproducible and checkable **is** done:

| Delivered | Where |
|---|---|
Numeric room geometry, verified | `TRADE_FIX_OFFICE_01.blockout.json` |
Floor plan and construction sheet | `visual/references/**/*.svg` |
Frozen identity: 14 measurements with tolerances | `CHARACTER_BIBLE.md` §3 |
Acceptance checklist — any single failure rejects a plate | `CHARACTER_BIBLE.md` §5 |
Reusable prompt block, negative constraints, per-view additions | `CHARACTER_BIBLE.md` §9 |
Painting order, with the rule that view 1 is never re-opened | `CHARACTER_BIBLE.md` §9 |
Environment acceptance checklist | `OFFICE_BIBLE.md` §10 |

### The three routes

| Route | Consistency | Cost | Note |
|---|---|---|---|
Photoshop 2026 generative, driven by hand | Weakest across nine views | Already installed | Best colour control; finished in the tool that composites |
A hosted image model + the accepted view 1 as reference | Best | Per-image spend, external dependency | — |
**Local diffusion + an identity adapter** trained on view 1 | Best, and reproducible for years | Free after setup | Another multi-GB VRAM tenant — **can only run with the station stopped**, which is fine because reference painting is offline work |

**Recommended: route 3**, run offline between station sessions. A 24/7 character whose face must
stay recognisable for years is exactly the case where a trained identity adapter pays for
itself. Nothing in the architecture depends on which route is chosen — all three are handed the
same blockout underlay and the same prompt block, so switching later does not restart anything.

**This is the one decision holding V2.**

---

## 5. What can proceed now

The critical path through the art and the critical path through the logic are **independent**.

**V6 — the behaviour director — does not need the art.** It is pure Python consuming the motion
library as data, it is the single largest piece of logic in this layer, and its entire test
surface runs headless against a stub renderer: weighted selection, sampled cooldowns, the lock
system, the character state machine, anti-repetition over simulated weeks, the fatigue curve,
long-term drift. The same is true of V7.1–7.6, the state bridge.

So the recommendation is: **choose the paint route, and meanwhile build V6 and the bridge half
of V7.** When the plates arrive, the thing that drives them is already tested.

Genuinely blocked on art: V2, V3, V4, V5, V8, V10, V11.

---

## 6. Carried forward

| # | Item | Severity | Lands in |
|---|---|---|---|
| R1 | 2 487 MiB free VRAM is the renderer's entire budget | **Critical** | V2.7, V10.5, V11 |
| R2 | No paint capability for the sixteen plates | **Blocking V1 sign-off** | §4 above |
| R3 | 6.1 GB free RAM; CEF wants 400–700 MB | Moderate | V11.1 |
| R4 | One NVENC engine shared with Parsec | Moderate | V10.4 |
| R5 | 40 % GPU utilisation at rest with ACE-Step merely resident | Moderate | V11 |
| R6 | Hand-built blending — no blend tree comes in the box | **Moderate, and the ADR's weakest point** | V5.1, V6. Contained fallback: the Spine web runtime inside the same architecture |
| R7 | Seven camera plates are seven painted assets; an eighth camera is an art task | Accepted | — |
| O1 | The 5750G iGPU exists but is firmware-disabled. Enabling it could move the desktop *and* the renderer off the 3060 | Opportunity | A maintenance window, not a phase |

---

## 7. Phase exit

**Not signed off.** 1.11–1.13 are outstanding and the plan makes them the gate: nothing
proceeds to rigging before identity and layout are locked, and identity is not locked until a
plate exists that passes `CHARACTER_BIBLE.md` §5.

Specification and layout **are** locked and verified. The blocker is a paint capability, not a
design question.
