# Visual layer — Implementation Plan

**Created:** 2026-10-03 · **Basis:** `INITIAL_VISUAL_AUDIT.md`, `ADR_VISUAL_RUNTIME.md`
**Runtime:** hybrid 2.5D — WebGL2 in an OBS browser source over a numeric 3D blockout (ADR-10)

Decomposed into phases that are each **independently verifiable**. Every milestone states its
**exit test**: the concrete, runnable check that proves it works. No exit test is "the code
exists" or "it looks right".

---

## 0. Guiding constraints

Apply to every phase, not restated per milestone.

| Constraint | Source | Enforcement |
|---|---|---|
| **Music generation outranks the picture** | ADR-10 §4 | Renderer sheds quality at the `GPUManager` VRAM floor; never the other way round |
| Renderer failure cannot touch the radio | brief, ADR-12 | Separate processes; the visual layer is a read-only socket client |
| Visual layer never writes to the station | `STATE_BRIDGE.md` §12 | No DB session, no event-bus publisher in `tradefix_radio/visual/` |
| No regime value reaches a motion selector | brief, `MOTION_LIBRARY.md` §11.1 | Band mapping exists in exactly one module; test asserts no other import of `MarketRegime` under `visual/` |
| No fabricated market data on any surface | brief, `STATE_BRIDGE.md` §8 | Charts stop advancing when `feed_trustworthy` is false; test asserts it |
| Versioned typed contracts, never bare dicts | house §90 | Pydantic `*V1`; mypy |
| No magic numbers | house §71 | Motion library and blockout are data files, validated on load |
| Identity is frozen once accepted | ADR-13 | `CHARACTER_BIBLE.md` §5 checklist is a review gate |
| One room | ADR-13 | Every plate derives from the blockout; CAM 5 is the coherence check |
| Tests accompany features | house §58 | Phase reports list coverage |
| Never fake success | house §102 | Phase reports paste real output and real measurements |

**Definition of "tested"** here: unit tests for selection, cooldown and lock logic; Hypothesis
property tests for the anti-repetition invariants over simulated days; integration tests for
bridge degradation; accelerated soak for long-term behaviour; and **recorded video review** for
everything that is a judgement about whether it looks alive. The last one is not optional and
not replaceable — §V11.4 says so explicitly.

---

## PHASE V1 — References and specification
*Goal: identity and layout locked. No runtime.*

| # | Milestone | Exit test |
|---|---|---|
| 1.1 | Environment and software audit | `INITIAL_VISUAL_AUDIT.md` committed with measured figures, not estimates ✅ |
| 1.2 | Runtime decision with rejected alternatives and reversal conditions | `ADR_VISUAL_RUNTIME.md` committed ✅ |
| 1.3 | Character specification, identity lock, acceptance checklist, prompt blocks | `CHARACTER_BIBLE.md` committed ✅ |
| 1.4 | Environment specification, palette, zones, lighting, ambient motion | `OFFICE_BIBLE.md` committed ✅ |
| 1.5 | Motion library, behaviour director, gaze, blending, anti-repetition | `MOTION_LIBRARY.md` committed ✅ |
| 1.6 | Seven camera compositions with bezel-clearance arithmetic, director rules, overlay zones | `CAMERA_PLAN.md` committed ✅ |
| 1.7 | State contract, field provenance, degradation ladder, honesty rules | `STATE_BRIDGE.md` committed ✅ |
| 1.8 | Numeric blockout: room, objects, anchors, gaze targets, cameras | `TRADE_FIX_OFFICE_01.blockout.json` loads, and a validator confirms every camera's sight line clears the bezel plane and every anchor is reachable from the seated shoulder ✅ |
| 1.9 | Construction drawings | Floor plan, elevations and proportion sheet committed as SVG ✅ |
| 1.10 | Asset directory skeleton | `visual/` tree exists with a README per directory stating what belongs there ✅ |
| **1.11** | **Character reference view 1 painted and accepted** | **Passes every item of `CHARACTER_BIBLE.md` §5. BLOCKED — see below** |
| 1.12 | Character views 2–9 painted and accepted | Each passes §5 *against view 1*, in the §9 order |
| 1.13 | Environment plates 1–7 painted and accepted | Each passes `OFFICE_BIBLE.md` §10; all cross-check against the CAM 5 wide plate |

**Phase exit:** identity and layout locked. Nothing proceeds to rigging before 1.11–1.13.

> **1.11 is blocked and it is the gate for the whole phase.** There is no image generator on
> this machine and I have no image-generation tool, so the painted plates cannot be produced
> here. Everything that makes them reproducible and checkable is done — the geometry, the
> drawings, the frozen identity spec, the prompt blocks, the acceptance checklist, the painting
> order. `INITIAL_VISUAL_AUDIT.md` §6 sets out the three ways to close it and recommends a local
> diffusion install with an identity adapter, run offline between station sessions. **This is a
> decision for you, and it is the one thing holding V2.**

---

## PHASE V2 — Static scene
*Goal: one camera, rendering the real room, inside OBS, with measured cost.*

| # | Milestone | Exit test |
|---|---|---|
| 2.1 | Install Blender (offline tool only, per ADR-13) and build the low-poly blockout from the JSON | Blender scene matches the JSON within 5 mm on every object; a script round-trips JSON → scene |
| 2.2 | Render the seven camera underlays from the blockout | Seven perspective renders committed; CAM 1's bezel line lands at 30 % ± 2 % of frame height as `CAMERA_PLAN.md` §3 predicts |
| 2.3 | Layer-stack format and loader: depth assignment, atlas packing, compressed textures | Loader reads a stack and reports its resident VRAM; a malformed stack fails loudly with the layer name |
| 2.4 | WebGL2 renderer skeleton: orthographic composite, depth-offset parallax, breathing drift | CAM 7 renders at 1920×1080; drift is a Perlin walk, verified non-periodic over 10 minutes of recorded transform values |
| 2.5 | Lighting composite: four layers, unbaked, modulable | Each layer toggles independently; the ±8 % energy modulation is visible in a diffed frame pair and invisible in side-by-side playback |
| 2.6 | OBS browser source integration | OBS composites the renderer at 1080p; alpha confirmed by placing it over a colour source |
| 2.7 | **Quality profiles and the VRAM budget** | LOW / BALANCED / HIGH each measured for VRAM, frame time and CPU **with ACE-Step resident**; all three leave the `GPUManager` floor intact; figures committed |
| 2.8 | All seven cameras composited | Each renders; switching between them is a cut with no asset reload stall above 80 ms |

**Phase exit:** a still, lit, correct room in OBS at a measured cost that fits the ADR-10 §4
budget. → `docs/status/VISUAL_PHASE_2_REPORT.md`.

---

## PHASE V3 — Character rig
*Goal: the master model deforms correctly. Still no behaviour.*

| # | Milestone | Exit test |
|---|---|---|
| 3.1 | Layer taxonomy exporter | A master file exporting to atlases with layer names matching `CHARACTER_BIBLE.md` §4 exactly; a mismatch fails the build |
| 3.2 | Skeleton and skinning: every joint in §8 of the bible | Each joint moves its intended layers and nothing else; a per-joint influence map is committed as an image |
| 3.3 | Face rig: lids, brows, mouth, jaw, cheeks | All six expressions reachable at their specified amplitudes, and **no expression exceeds them** — asserted numerically, not reviewed |
| 3.4 | Gaze rig: world-space targets | Eyes aimed at each `GAZE_*` anchor land within 1.5° of the true direction **from all seven cameras** |
| 3.5 | Depth-ordering swaps on head yaw | Far ear, far brow and far cup re-sort correctly through ±40° yaw with no popping |
| 3.6 | Hair strays: verlet chains | Three strands respond to head acceleration and settle without oscillation |
| 3.7 | Headphone rig | Band flexes, cups pivot, pads compress on contact, cable follows |
| 3.8 | Seated pose verified against the room | Elbow at Z 700 ± 5, eye at Z 1 295 ± 5, forearms in contact with the desk surface at Z 735 |
| 3.9 | Two-bone analytic reach solver | Both hands reach all twelve anchors within the **4 mm** contact tolerance, from all seven cameras |

**Phase exit:** the character can be posed accurately and touch every object. →
`VISUAL_PHASE_3_REPORT.md`.

---

## PHASE V4 — Idle life
*Goal: he is alive and doing nothing, for half an hour, without looping.*

| # | Milestone | Exit test |
|---|---|---|
| 4.1 | Four idle generators (`MOTION_LIBRARY.md` §3) | Each runs independently; recorded transform traces show no shared period |
| 4.2 | Blink model: log-normal, double, slow, forced-on-saccade | 10 000 simulated intervals match the specified distribution; **no two consecutive intervals equal**; a forced blink follows every >12° saccade |
| 4.3 | Five posture sets and `posture_shift` | Baseline changes every 8–22 min; the change is a blend, not a snap |
| 4.4 | Weight shift and hand tension | Present, aperiodic, within amplitude |
| 4.5 | **30-minute idle soak** | **Recorded at 30 fps and reviewed. No repeated motion sequence identified by a reviewer who has not read this document.** Frame-difference autocorrelation shows no peak above 0.75 at any lag from 2 s to 30 min |

**Phase exit:** the brief's bar — *run for 30 minutes, must not visibly loop.* The
autocorrelation check is the automated half; the review is the half that decides. →
`VISUAL_PHASE_4_REPORT.md`.

---

## PHASE V5 — Work movements
*Goal: every motion in the library exists and is believable.*

| # | Milestone | Exit test |
|---|---|---|
| 5.1 | Blend architecture: 8 layers, masks, phase-preserving interrupt | Additive layers survive an override on the same joint — breathing continues through a coffee sip, asserted on transform traces. An interrupt at 60 % blends out **from the current pose** |
| 5.2 | Lock system | A motion cannot start while a lock is held; a Hypothesis test over random motion sequences finds **no** double-claim |
| 5.3 | Micro movements (§4.1) | All fifteen play at their specified durations and amplitudes |
| 5.4 | Trading movements (§4.2) | All twenty play; hand anchors hold within 4 mm throughout each one |
| 5.5 | Headphone movements (§4.3) | All five; contact deforms the pad; the 150 s group cooldown holds |
| 5.6 | **Coffee sequence as an atomic sequence** (§6) | Runs to completion; **a reaction arriving mid-sip is deferred, not cancelled**, and fires within 1.5 s of release or is dropped. Mug returns within 6 mm of its landing ring on 1 000 consecutive runs |
| 5.7 | Mug temperature state machine (`OFFICE_BIBLE.md` §7) | Steam tracks temperature; level only falls; no steam from a `COLD` mug |
| 5.8 | Fatigue movements (§4.5) | All six; `refocus_sharp` chains to every one; share stays under 4 % over an 8-hour accelerated run |
| 5.9 | Market reaction composites (§4.6) | Composites obey the opener/action/resolution grammar; total duration 2.0–5.5 s; **`react_smirk` never fires on an adverse move** — asserted exhaustively over the direction × regime product |
| 5.10 | Gaze system (§7) | 89 % ± 3 % of gaze on screens over an hour; `GAZE_CAMERA` ≤ 0.6 %; eyes lead the head; no transit under 180 ms |
| 5.11 | **Interaction constraint audit** | A recorded pass of all 60 motions from CAM 6 and CAM 1, reviewed against the brief's constraint list. **Zero** instances of clipping, floating, look-through, snapping or mid-cycle restart |

**Phase exit:** every motion exists, contacts correctly, and violates none of the brief's
constraints. → `VISUAL_PHASE_5_REPORT.md`.

---

## PHASE V6 — Behaviour director
*Goal: the thing that decides, in Python, under test.*

| # | Milestone | Exit test |
|---|---|---|
| 6.1 | `tradefix_radio/visual/` package, own process, injectable `Clock` | Starts and stops cleanly; runs in accelerated time; no import of station persistence |
| 6.2 | Motion library as a validated data file | Loads; every record complete; an invalid record fails on load naming the field |
| 6.3 | Weighted scorer (§9) | Every multiplier applied in order; **`random.choice` appears nowhere** — asserted by a source check |
| 6.4 | Cooldowns, sampled per execution | No motion fires inside its sampled cooldown over 10 000 ticks; sampled gaps are never equal twice running |
| 6.5 | Character state machine (§5) | **`EXECUTING` is unreachable except from `ANALYZING`** — exhaustive over the transition product. State shares land within 5 points of §5 targets over an accelerated 24 h |
| 6.6 | **Anti-repetition (§12)** | Hypothesis property tests over an accelerated **7 days**: no back-to-back repeat; no key within 3; **no ordered triple repeated within 6**; no motion above 14 % of any rolling hour |
| 6.7 | Do-nothing candidate | Stillness occurs; motion density does not increase monotonically with runtime |
| 6.8 | Fatigue curve (§13) | Phase advances over 7 h, resets on refill; fatigue motions gated below 0.35; 4 % cap holds at every phase |
| 6.9 | Long-term variation (§13) | Six parameters drift on their stated periods over an accelerated week; **no identity parameter changes** |
| 6.10 | Command emission at 20 Hz | Commands are monotonic, idempotent on resend, and dropped rather than queued when the renderer stalls |

**Phase exit:** the director passes a 7-day accelerated property suite. → `VISUAL_PHASE_6_REPORT.md`.

---

## PHASE V7 — Market and music bridge
*Goal: the station drives the performance, honestly.*

| # | Milestone | Exit test |
|---|---|---|
| 7.1 | WebSocket client of the station's `/ws`, read-only | Receives `LiveStateV1`; **a test asserts the visual package opens no DB session and publishes no event** |
| 7.2 | `VisualStateV1` and the field mapping (§3) | Every field populated from its stated source; a golden `LiveStateV1` maps to a golden `VisualStateV1` |
| 7.3 | Band mapping (§5), rate-limited | All fourteen regimes map; one step per 20 s; downward requires 45 s; **a source check asserts `MarketRegime` is imported in exactly one module under `visual/`** |
| 7.4 | Band overrides | Stale or disconnected feed forces B0; `CLOSED` session caps at B1 |
| 7.5 | Salience and reaction triggers (§11.3) | Fires above threshold; 25 s refractory holds; ≤ 10/hour; suppressed in B0 and below 0.35 confidence |
| 7.6 | Band effects (§11.2) | Rate multiplier 0.55→1.60 and blend scaling measured over accelerated runs per band; `market_energy` modulates ±15 % within band |
| 7.7 | Rhythm contract (§7) | Policy from Python, phase in the renderer; re-anchored every 2 s; drift under 20 ms over an hour; **subdivision 2 above 160 BPM** |
| 7.8 | Music ceilings (`MOTION_LIBRARY.md` §10) | Nod ≤ ±1.1° at any energy; ≤ 8 consecutive beats then 25 s gap; one beat-synced motion at a time; combined music share < 12 % — **all asserted, none merely configured** |
| 7.9 | Screen content from state | All seven surfaces live; `MON_1`/`MON_2` follow the active symbol |
| 7.10 | **Symbol switch** | XAUUSD → BTCUSD retitles and redraws the charts, updates labels, licenses a cut at p = 0.5, and **does not reset animation** — asserted on motion-history continuity across the switch |
| 7.11 | **Honesty rules (§8)** | Charts stop advancing when `feed_trustworthy` is false; `STALE` / `NO FEED` marker present; `SIMULATED` badge on every simulated frame; no price text when price is `None`; **no account or position data anywhere** |
| 7.12 | Degradation ladder (§10) | Each of the four windows produces its stated behaviour; reconnect ramps at one band step per 20 s and does not snap |

**Phase exit:** the character reacts to the real station, and cannot react to data that is not
there. → `VISUAL_PHASE_7_REPORT.md`.

---

## PHASE V8 — Camera director

| # | Milestone | Exit test |
|---|---|---|
| 8.1 | Seven cameras selectable with their lenses and targets | Each matches `CAMERA_PLAN.md` §3 within 1 mm and 0.2° |
| 8.2 | Hold distributions per regime | Measured medians over accelerated runs land within 10 % of §5; **the 35 s floor is never crossed** |
| 8.3 | Per-shot caps | Rolling-hour shares respected; `CAM_4` ≤ 8 %, `CAM_6` ≤ 6 % |
| 8.4 | Weighted selection with narrative bonus | `CAM_6` preferred during a locked interaction; `CAM_4` during `MARKET_REACTION` |
| 8.5 | **The five vetoes (§5)** | Exhaustive test: no cut mid-blend, mid-sequence, before minimum hold, within 15 s of the last, or within **2.5 s of a reaction starting** |
| 8.6 | Licensed cuts (§6) | Symbol change cuts at p = 0.5 ± 0.05 over 500 trials; music drop at 0.3; **no guaranteed cut on any event** |
| 8.7 | Transitions (§8) | Cut / crossfade / push-continue only; shares ≈ 80/15/5; `CAM_1`→`CAM_4` push ≤ 2 per hour; **no wipes exist in the code** |
| 8.8 | Overlay-safe zones (§7) | Per-camera protected regions enforced; `CAM_4` suppresses all overlay; zones cross-fade over 400 ms on a cut |
| 8.9 | Manual control | Auto off pins the shot **and drift continues**; `select_camera` queues behind vetoes rather than bypassing them |

**Phase exit:** an hour of recorded footage with no cut a reviewer calls jarring, and no veto
violation in the logs. → `VISUAL_PHASE_8_REPORT.md`.

---

## PHASE V9 — Control Center page

Adds `/visual` to the existing ten-item nav in `frontend/src/components/Shell.tsx`, following
the established panel and token conventions.

| # | Milestone | Exit test |
|---|---|---|
| 9.1 | Visual page shell and nav entry | Renders; reachable; matches the existing page conventions |
| 9.2 | Health and performance panel | Renderer up/down, `fps_mean`, **`fps_p05`**, frame-time p95, dropped frames, GL memory, quality profile, resident stacks |
| 9.3 | Behaviour panel | Character state, current motion with blend weight, last action, **next-action timer**, animation queue, cooldown table |
| 9.4 | State panel | Active symbol, regime, **intensity band**, market energy, BPM, music energy, genre, station mode, degraded flag and reason |
| 9.5 | Camera panel | Current camera, time in shot, rolling-hour shares, auto on/off, manual select |
| 9.6 | Controls | Visual energy, music reactivity, market reactivity, force idle, trigger test motion, quality profile |
| 9.7 | **Control safety** | A test asserts no control can exceed a hard ceiling, cap or veto; no station-mutating endpoint is reachable from this page |
| 9.8 | Simulator controls (§11) | All nine scenarios drive the director without the station; **every simulated frame shows the `SIMULATED` badge** |
| 9.9 | Debug overlay | Shows current motion, blend weight, state, band, energy, BPM, camera, cooldowns. **A test asserts it is off when the production flag is set** |
| 9.10 | Graceful absence | With the visual process down, the page says so and the rest of the console is unaffected |

**Phase exit:** an operator can see and steer the visual layer and cannot break it. →
`VISUAL_PHASE_9_REPORT.md`.

---

## PHASE V10 — Output

| # | Milestone | Exit test |
|---|---|---|
| 10.1 | Production OBS scene and browser source | Composites at 1080p; audio **muted** — the station owns audio through its own `AudioSink` |
| 10.2 | Overlay compositing | Now-playing, market and station overlays in their zones, per-camera suppression honoured |
| 10.3 | **Latency measurement** | Measured state-to-pixel latency committed. ADR-12 predicts sub-frame to one frame; the measurement stands whatever it says |
| 10.4 | **Encoder contention** | Frame time and encoder utilisation measured with Parsec active and inactive, recording on and off. Audit risk R4 closed with numbers |
| 10.5 | Full-stack GPU cost | VRAM, GPU util, CPU and frame time with ACE-Step **generating**, at all three profiles. `GPUManager` floor intact in every combination |
| 10.6 | Frame-rate decision | **30 or 60 fps chosen from these measurements, not assumed.** The brief is explicit that a stable 30 may be the better answer |
| 10.7 | Quality shedding | Driving GL memory toward the budget triggers a profile drop and stack eviction within 30 s; it recovers when headroom returns |
| 10.8 | Renderer restart | Killing the CEF process leaves the radio untouched, and the reload resumes with **no visible behaviour reset** |

**Phase exit:** a clean stream output with every cost measured. → `VISUAL_PHASE_10_REPORT.md`.

---

## PHASE V11 — Endurance and human review

| # | Milestone | Exit test |
|---|---|---|
| 11.1 | 2-hour real-time soak | No memory growth above 5 %, no VRAM growth, no dropped socket, no frozen state, no asset-load stall. **The brief's stated bar** |
| 11.2 | 8-hour overnight soak | As above, plus: fatigue curve completed and reset; every camera used within its share; no repeated motion triple in the logs |
| 11.3 | 24-hour accelerated behaviour soak | A full day of director decisions in accelerated time: share targets held, anti-repeat invariants held, long-term variation observed, no state deadlock |
| 11.4 | **Human quality review** | The §V11.5 question set answered against recorded footage, in writing, by a reviewer **who has not read these documents** |
| 11.5 | Leak audit | Heap and GL object counts flat across 8 hours; texture atlas count returns to baseline after stack eviction |
| 11.6 | Recovery matrix | Station restart, renderer kill, director kill, OBS restart, feed loss, symbol switch during a locked sequence — each recovers to normal with no viewer-visible break |

### The human review question set

Automated tests are not enough, and these are the questions that decide whether this worked:

- Does he look alive?
- Does he repeat obvious movements?
- Does typing look natural?
- Do hands intersect the desk?
- Does the coffee cup align with his hand?
- Do his eyes target the monitors?
- Do head turns make sense?
- **Does he feel 35–40 years old?**
- Does he look like a real, disciplined trader?
- Does the office feel like a real place?
- **Is the scene still interesting after 30 minutes?**
- Does market intensity change the visual energy?
- Does music affect him subtly, without making him dance?

A "no" on any of these is a defect, not a note. The last two in bold are the ones most likely to
fail quietly, because both are only visible over time and neither shows up in a frame grab.

**Phase exit:** all of V11, plus the success criteria below. → `VISUAL_PHASE_11_REPORT.md`.

---

## Success criteria

The brief's sixteen, each mapped to where it is proven. The system is complete when every row
has a passing exit test.

| # | Criterion | Proven by |
|---|---|---|
| 1 | Same character, consistently | V1.11–1.12 · `CHARACTER_BIBLE.md` §5 |
| 2 | Same office, consistently | V1.13 · `OFFICE_BIBLE.md` §10 · CAM 5 coherence check |
| 3 | Idles indefinitely | V4.5 · V11.1–11.2 |
| 4 | No obvious short loop | V4.5 autocorrelation · V6.6 · V11.4 |
| 5 | Believable mouse and keyboard | V5.4 · V5.11 · V11.4 |
| 6 | Coffee interaction works | V5.6 · V5.7 |
| 7 | Monitor gaze works | V3.4 · V5.10 |
| 8 | XAUUSD/BTCUSD changes screens | V7.9 · V7.10 |
| 9 | Market energy changes behaviour | V7.6 · V11.4 |
| 10 | Music BPM subtly affects rhythm | V7.7 · V7.8 · V11.4 |
| 11 | Cameras switch professionally | V8.2–8.8 |
| 12 | Renderer independent of the radio | V7.1 · V10.8 · V11.6 |
| 13 | OBS receives clean output | V10.1–10.2 |
| 14 | GPU usage measured and acceptable | V2.7 · V10.4–10.5 |
| 15 | 2-hour soak passes | V11.1 |
| 16 | Human review: still interesting | V11.4 |

---

## Asset layout

```
visual/
  character/      master model, atlases, rig, exports
  environment/    blockout JSON, Blender scene, layer stacks per camera
  animations/     motion library data, authored curves
  cameras/        camera definitions, overlay-zone maps
  materials/      shared shaders and composite definitions
  textures/       source and compressed atlases
  references/
    character/    the nine canonical views, proportion sheets
    office/       the seven canonical plates, plan and elevations
    motion/       reference clips for the §14 sheet
  exports/        build output consumed by the renderer
```

Nothing generated lands in Downloads or on the Desktop. Each directory carries a README stating
what belongs in it and what does not.

---

## Sequencing and the critical path

```
V1 ──(1.11 BLOCKED: no paint capability)──► V2 ──► V3 ──► V4 ──► V5 ──► V6 ──► V7 ──► V8 ──► V9 ──► V10 ──► V11
          │                                                        ▲
          └── V6 (behaviour director) can start NOW ────────────────┘
              It needs the motion library data, not the art.
```

**V6 does not depend on the art, and that is worth exploiting.** The behaviour director is pure
Python consuming the motion library as data, it is the single largest piece of logic in the
project, and its entire test surface — weighted selection, cooldowns, locks, state transitions,
anti-repetition over simulated weeks, the fatigue curve, long-term drift — runs headless against
a stub renderer. The same is true of the state bridge in V7.1–7.6.

So the critical path through the art and the critical path through the logic are independent,
and the honest recommendation is: **decide the paint route for 1.11, and meanwhile build V6 and
the bridge half of V7.** When the plates arrive, the thing that drives them is already tested.

**What is genuinely blocked on the art:** V2 (needs plates), V3 (needs the master model), V4
and V5 (need the rig), V8 (needs the plates), V10 and V11 (need all of it).
