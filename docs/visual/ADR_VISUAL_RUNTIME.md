# ADR — the visual runtime

Continues the numbering in `docs/INITIAL_AUDIT.md`, which ends at ADR-09. Four decisions are
recorded here: the runtime (ADR-10), where behaviour is decided (ADR-11), the transport to OBS
(ADR-12), and how the character is authored (ADR-13).

Evidence for every figure quoted is in `INITIAL_VISUAL_AUDIT.md`.

---

## 1. The requirement being decided against

From the brief, in the order that turned out to matter:

| # | Requirement | Why it discriminated |
|---|---|---|
| 1 | **GPU-efficient enough to run alongside ACE-Step** | 2 487 MiB free VRAM eliminated two of the seven candidates outright |
| 2 | **24/7 runtime** | Eliminated anything whose stability story is "the editor is open" |
| 3 | **OBS integration** | No Spout and no NDI on this host made transport a real cost, not a footnote |
| 4 | **Easy automation from Python / local API** | The station is Python; the behaviour engine wants to be tested with the existing pytest/hypothesis suite |
| 5 | **Professional rendering** | Where the candidates genuinely differ, and where the honest answer is counter-intuitive |
| 6 | **Many reusable movements, programmable animation** | Favoured engines with real blend trees — the strongest argument *against* the chosen option |
| 7 | **Multiple camera angles** | The one requirement the chosen option satisfies by authoring rather than by projection |
| 8 | **Dynamic screen content** | Trivial for a browser; a build step for an engine |

Requirement 1 is not one consideration among eight. On this machine it is a filter applied
before the others are read.

---

## 2. ADR-10 — Runtime: **hybrid 2.5D — WebGL2 in an OBS browser source, over a numeric 3D blockout**

**Context.** The target is a premium illustrated-realism animated performer in a cinematic
office, rendered continuously, on an RTX 3060 with **2 487 MiB of free VRAM** because the
desktop holds ~4.8 GB and the resident ACE-Step model ~4.9 GB. No 3D or 2D animation software
of any kind is installed. OBS Studio 32.1.2 is installed with the stock `obs-browser` CEF
plugin and `obs-websocket`. The project already runs a Vite/React/TypeScript frontend and
already pushes coherent state frames over a WebSocket from `api/live.py`. Neither Spout nor
NDI is present.

**Decision.** Three parts, and the split between them is the decision:

1. **An offline numeric blockout** — `visual/environment/TRADE_FIX_OFFICE_01.blockout.json` —
   is the single source of spatial truth. Room dimensions, desk height, every monitor's centre
   and normal, every interaction anchor, and all seven camera positions, targets and fields of
   view, in millimetres. It never ships to the renderer and costs nothing at runtime. Its only
   job is to make the seven camera plates **geometrically the same room**.
2. **High-resolution painted layer stacks**, one per camera composition, derived from that
   blockout. This is where visual quality comes from, and the GPU does not pay for it.
3. **A WebGL2 runtime inside an OBS browser source**, animating those layers: skeletal
   deformation of the character's sprite mesh, parallax and slow push-in from layer depth,
   compositing of the four live monitor surfaces, and procedural ambient motion.

**Consequences — the favourable ones:**

- **VRAM cost lands around 250–400 MiB** rather than gigabytes: a handful of compressed
  texture atlases, a few framebuffers, one CEF process. It fits in 2.4 GB with room to spare,
  which is the only reason any of this is viable.
- **Nothing large is installed.** The brief said not to install large software blindly; this
  installs nothing at all. The renderer is a plugin OBS already ships.
- **The transport problem disappears.** No Spout plugin, no NDI plugin, no window capture, no
  visible window, no capture composite step. Alpha is supported natively, so overlay-safe
  compositing in OBS comes free.
- **Renderer death is already handled.** CEF runs out of process from both OBS's core and the
  station. A crashed browser source is a black rectangle that OBS reloads; the radio does not
  notice. This satisfies *"if the animation renderer dies, music/radio continues"* structurally
  rather than by defensive coding — the same argument ADR-02 made about ACE-Step.
- **The existing toolchain is the toolchain.** TypeScript, Vite, the `tailwind.config.js`
  palette, and the `/overlay/live` precedent all carry over. No second language, no second
  build system, no second asset convention.
- **Dynamic monitor content is nearly free.** The four screen surfaces are DOM/canvas layers
  fed by the same state frame. When the router moves XAUUSD → BTCUSD, the chart surfaces
  re-render; no asset swap, no engine rebuild.
- **Quality scales with art budget, not with GPU budget.** A painted 4K plate on this card
  outperforms anything its 2.4 GB could rasterise in real time. The reference product the
  brief names — an iconic study-livestream character — is itself 2D illustration, not 3D.

**Trade-offs accepted, stated without softening:**

- **Cameras are authored, not projected.** Seven free-roaming angles of one 3D set come free
  in an engine. Here each of the seven compositions is its own layer stack and its own painted
  work, and adding an eighth camera later is an art task rather than a code task. The blockout
  is what keeps them consistent, but consistency is *enforced by discipline plus geometry*,
  not guaranteed by a shared scene graph. This is the single largest cost of the decision.
- **No blend tree comes in the box.** Unity's Animator, Unreal's AnimGraph and Godot's
  `AnimationTree` all solve layered blending, additive poses and state machines for free. Here
  that is hand-built — `MOTION_LIBRARY.md` §8 specifies it, and Phase V5/V6 are where the cost
  lands. It is the strongest technical argument against this ADR.
- **No real IK solver.** Hand-to-object contact is solved by authored anchors and a
  two-bone analytic solve in 2D, not by a general IK rig. Adequate because every interaction
  is a seated reach to a fixed object whose anchor is known in the blockout, and because the
  brief's constraint list is about *contact accuracy*, which anchors give directly.
- **No per-frame dynamic relighting of the character by the environment.** Monitor spill and
  amber key are authored into the plates and modulated as composited light layers, not computed.
  Subtle lighting response works; a new light source does not.

**Rejected alternatives.**

| Candidate | Verdict | Deciding reason |
|---|---|---|
| **Unreal Engine 5** | Rejected | 4–6 GB VRAM for a lit 1080p scene against 2.4 GB free. Not installed (~50 GB with the editor). Reaching OBS needs a third-party Spout or NDI plugin, neither present. It would win on every quality and animation axis and lose the only axis that filters first. The brief's own warning about choosing fashionable technology applies most directly here. |
| **Unity (URP)** | Rejected | Lighter than UE5 but still gigabyte-scale for this environment, and it inherits the same missing-transport problem. Not installed. Would also mean C# as a third language. |
| **Godot 4 (2D + `Skeleton2D`)** | **Rejected, but it is close — see §3** | Genuinely cheap, free, small, and the only candidate that brings a real 2D blend tree and animation state machine. Lost on: not installed; another resident VRAM tenant; window capture is the only transport without a plugin; GDScript/C# is a fourth language in a Python+TS project; and no part of the existing asset, test or build pipeline reaches it. |
| **Live2D Cubism** | Rejected | Purpose-built for exactly this kind of character and would produce excellent deformation. Rejected on: not installed; commercial licensing with revenue-threshold terms unsuited to an indefinite commercial stream; a proprietary runtime that is awkward to drive from Python; and it solves the character while solving none of the environment, cameras or monitors. |
| **Spine** | Rejected | Excellent skeletal 2D with a good web runtime — the technically strongest 2D option. Rejected on cost (per-seat licence), not installed, and the same partial-solution problem as Live2D. **Reconsider if hand-built blending in V5/V6 overruns**; the Spine web runtime drops into the chosen WebGL2 architecture without disturbing anything else, which makes this a contained reversal rather than a rewrite. |
| **Blender real-time viewport / Eevee** | Rejected as a *runtime* | Driving a 24/7 broadcast from an application viewport is not a stability story. Eevee at this scene's richness also exceeds the VRAM budget. **Accepted as an offline tool** — see §7. |
| **Pre-rendered video loops** | Rejected | Zero GPU cost and perfect quality, and it fails the central requirement: the brief forbids a system that *"feels like a five-second GIF looping forever"*, and no amount of loop variety makes a recording market-reactive. |

**What would reverse this decision.** Any one of: the station moves to a second machine or a
dedicated GPU (then Unreal becomes the right answer and this ADR should be revisited
immediately); ACE-Step moves to a remote worker, freeing ~4.9 GB (ADR-02 already left that
door open); or V5/V6 demonstrate that hand-built blending cannot reach the required motion
quality, in which case the contained fix is the Spine runtime inside the same architecture,
and the uncontained one is Godot.

---

## 3. On Godot, fairly

This was the closest call and it deserves more than a table row, because a reader who knows
Godot's 2D stack will reasonably ask why it lost.

**Its real advantage is animation infrastructure, not rendering.** `AnimationTree` with blend
spaces, `StateMachine` nodes, additive blending and per-bone masks is precisely the machinery
`MOTION_LIBRARY.md` describes, and it is machinery the chosen option must hand-build. A Godot
2D scene also costs well under 300 MiB of VRAM, so requirement 1 does not disqualify it.

**It lost on integration surface, not on merit.** Choosing it means: a new engine installed and
exported to a binary for production; a fourth language; a hand-built socket bridge to reach the
Python state the browser gets for free; window capture as the only plugin-free transport, with
a real window that must never be minimised, occluded or resized; a separate asset pipeline
disjoint from the Vite build; and no reuse of the `tailwind.config.js` palette that is already
the project's written art direction. Against that, the chosen option's one real deficit is
blend trees — which is bounded, specified, and testable work.

The tiebreaker is honest and specific: **the browser renderer has fewer places to fail
unattended.** For a system whose headline requirement is running for weeks without a human in
the room, a renderer that OBS already owns and already reloads beats a renderer that needs its
window to stay exactly where someone left it.

---

## 4. The VRAM budget this decision commits to

Sized against 2 487 MiB measured free, not against 12 288 MiB nameplate.

| Component | Budget | Note |
|---|---|---|
| Texture atlases — character | 120 MiB | BC7/ASTC compressed; one atlas set shared by all cameras |
| Texture atlases — environment, per active camera | 90 MiB | Only the active camera's stack is resident; neighbours are pre-warmed, the rest evicted |
| Monitor surfaces (4 live + 1 optional) | 40 MiB | 1080p-class render targets at reduced resolution |
| Framebuffers, composite and light layers | 60 MiB | |
| CEF baseline and GL context | 90 MiB | |
| **Total target** | **≈ 400 MiB** | |
| **Headroom retained** | **≈ 2 080 MiB** | Must remain positive at all times |

The quality profiles in `VISUAL_IMPLEMENTATION_PLAN.md` V2 are expressed as choices within
this envelope — plate resolution, how many camera stacks stay resident, monitor surface
resolution and whether light layers are per-pixel or per-layer.

**A hard rule, enforced at runtime:** the renderer must never be the component that makes
generation fail. The station already has a `GPUManager` that pre-flight-checks free VRAM and
*refuses* rather than OOMs (ADR-04, `generation/gpu.py`). The visual process must respect the
same floor and shed quality — dropping to LOW, evicting non-active camera stacks, halving
monitor surfaces — when free VRAM approaches it. Music generation outranks the picture. Stated
here because it is an architectural commitment, not a tuning preference.

---

## 5. ADR-12 — Transport: **OBS browser source**, and no Spout or NDI

**Context.** The renderer must reach OBS cleanly. Four options were named in the brief:
Spout, NDI, window capture, browser/texture bridge. Neither Spout nor NDI is installed, and
adding either means a third-party plugin inside the broadcast-critical process.

**Decision.** The renderer **is** an OBS source. A browser source points at a URL served by
the visual process; OBS composites it directly.

**Consequences.**

| Property | Result |
|---|---|
| Install cost | **Zero.** `obs-browser.dll` is stock in OBS 32.1.2. |
| Copies between renderer and compositor | One, inside OBS. No GPU→CPU→GPU round trip, no network hop, no encode/decode. |
| Expected added latency | **Sub-frame to one frame.** To be measured in V10, not assumed. |
| Alpha | Native, which is what makes the overlay-safe zones in `CAMERA_PLAN.md` §7 implementable. |
| Failure mode | CEF crashes out of process; OBS shows black and reloads. The station is untouched. |
| Audio | Not used. The station owns audio through its own `AudioSink`; the browser source is muted. Said explicitly because a browser source that emits audio would silently double the stream's sound. |

**Rejected.** Spout — needs `obs-spout2-plugin` plus a publishing renderer, to replace a path
that is already zero-copy. NDI — adds a network hop and an encode/decode round trip for a
handoff that never leaves the machine, plus a plugin with a history of breaking across OBS
major versions. Window capture — needs a real visible window that must never be minimised or
occluded, which is a poor property for an unattended 24/7 system.

**Measurement obligation.** V10 records frame time, added latency, VRAM delta and encoder
utilisation with Parsec active and inactive, because R4 in the audit is a real unknown and one
NVENC engine is shared three ways.

---

## 6. ADR-11 — Behaviour is decided in **Python**, performed in the browser

**Context.** The `BehaviorDirector` needs weighted selection, per-action cooldowns, a 20-deep
anti-repeat history, state transitions, and market/music bias. Something has to own it, and it
could live in either process.

**Decision.** **Python decides; the browser performs.** A new in-repo package, `tradefix_radio/visual/`,
runs as its own OS process and owns the `VisualStateBridge`, the `BehaviorDirector`, the
`CameraDirector` and the fatigue rhythm. It emits a stream of timed animation *commands*. The
WebGL2 runtime receives commands, blends and renders them, and reports frame time, resident
VRAM and the executing clip back.

**Consequences.**

- The director is **testable with the suite that already exists** — pytest, `pytest-asyncio`,
  `hypothesis`, `freezegun`. Anti-repeat and cooldown rules are exactly the kind of property a
  Hypothesis test should assert over a simulated week, and that is only cheap in Python.
- It reuses established in-house patterns rather than inventing new ones. `director/diversity.py`,
  `director/history.py`, `director/selection.py` and `director/temperature.py` already solve
  weighted choice under anti-repetition constraints for music. The brief's demand for
  *"weighted behavior selection with cooldowns and anti-repeat rules"* and not `random.choice`
  is the same problem, and the house already has a shape for it.
- It uses the project's injectable `Clock` (`core/clock.py`), which means **a 24-hour behaviour
  soak can be run in accelerated time** — the only practical way to satisfy V11's 24-hour
  check without waiting a day per iteration.
- **The renderer stays stateless and disposable.** Reloading the browser loses nothing: history,
  cooldowns and fatigue phase live in the director. This is what makes recovery from a CEF
  crash a non-event instead of a visible reset, and it is why the split falls here.
- The Control Center reads the director over HTTP like every other panel, so the V9 Visual page
  needs no new plumbing concept.

**Trade-off accepted.** Beat-accurate music synchronisation cannot round-trip to Python per
beat. So the division is explicit: **Python sets rhythmic policy, the browser keeps rhythmic
phase.** The director sends BPM, downbeat phase and a nod-probability weight; the renderer runs
the local beat clock and decides which beats land. `STATE_BRIDGE.md` §6 fixes this contract.

**Trade-off accepted.** A fourth OS process (station, ACE-Step, API, visual). ADR-08 already
established the multi-process shape and the event bus, so this is an extension of an accepted
pattern rather than a new one.

**Rejected alternative.** The director in TypeScript in the browser. Rejected because it makes
accelerated-time soak testing of a 24-hour behaviour curve impractical, because it puts the
anti-repetition logic — the exact thing that decides whether the stream *"feels alive rather
than looped"* — outside the test suite that the rest of this project holds itself to, and
because every CEF reload would reset the history that anti-repetition depends on.

---

## 7. ADR-13 — Authoring: one master 2D model over a 3D blockout; **Blender offline only**

**Context.** The brief is emphatic that the character must be *one* master model and must not
drift between views, and that all cameras must frame the same actual room. In a 2.5D pipeline
both are authoring disciplines, so they need mechanisms rather than good intentions.

**Decision.**

- **One master layered character model.** A single PSD-class source with a fixed layer
  taxonomy, a fixed deformation-zone map and a fixed joint set, exported to one atlas set used
  by every camera. Per-camera work is limited to a projected view of the *same* layer tree —
  never a new face. `CHARACTER_BIBLE.md` §4 fixes the taxonomy; §5 fixes the invariants a
  candidate plate must satisfy to be accepted.
- **One blockout as spatial truth.** `TRADE_FIX_OFFICE_01.blockout.json` is authoritative for
  geometry. Any plate that disagrees with it is wrong, not the blockout.
- **Blender is approved as an offline tool and forbidden as a runtime.** It is the right way to
  build the low-poly blockout, verify that seven cameras see one coherent room, and render
  perspective underlays for the painter. It is free, ~500 MB, and — decisively — **it is never
  running while the station is live**, so it costs zero VRAM against the budget in §4. It is
  not installed yet and should be installed only when V1 is signed off and V2 actually begins.

**Consequences.** Identity consistency becomes checkable rather than hoped for: §5's invariant
list is a review gate. Camera consistency becomes verifiable, because a disputed plate can be
re-rendered from the blockout and compared. And the painting step stays replaceable — whichever
of the three paint routes in the audit's §6 is chosen, it is handed the same blockout
underlay and the same prompt block, so switching routes later does not restart the project.

**Trade-off accepted.** An extra offline step before any pixel is painted. Cheap relative to
repainting nine character views that turned out to be nine different men.

---

## 8. The open opportunity: the firmware-disabled iGPU

The 5750G's Radeon iGPU is on the die and absent from Windows, so it is off in BIOS. If it
were enabled and made the desktop's display adapter, both the desktop compositor (~4.8 GB) and
the animation renderer (~400 MiB) could move off the 3060, leaving almost the whole 12 GB to
ACE-Step — which would also reopen ADR-04's model-size choice.

**Not acted on, and not part of any phase.** It needs a BIOS change and a reboot of a station
intended to run continuously, the eight-CU iGPU would need the renderer sized to it rather than
merely moved to it, and Parsec's display path would have to be re-validated. Recorded because
it is the highest-leverage unexploited fact about this machine, and because the right time to
consider it is a scheduled maintenance window, not mid-build.

---

## 9. Decision summary

| ADR | Decision | Primary reason |
|---|---|---|
| **ADR-10** | Hybrid 2.5D: WebGL2 layer renderer over a numeric 3D blockout | 2 487 MiB free VRAM; nothing to install; quality moves from GPU to art |
| **ADR-11** | Behaviour decided in Python, performed in the browser | Testable with the existing suite; accelerated-time soak; renderer stays disposable |
| **ADR-12** | OBS browser source; no Spout, no NDI | Already installed, zero-copy, out-of-process failure, native alpha |
| **ADR-13** | One master layered model over one blockout; Blender offline only | Makes identity and camera consistency checkable; zero runtime cost |
