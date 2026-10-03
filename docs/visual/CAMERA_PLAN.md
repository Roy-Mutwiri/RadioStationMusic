# Camera plan

Seven compositions of one room, the director that chooses between them, and the rules that keep
cuts from becoming the most noticeable thing on the stream.

All positions are in the `TRADE_FIX_OFFICE_01` coordinate system (`OFFICE_BIBLE.md` §3):
millimetres, origin at the floor of the south-west corner, +X east, +Y north, +Z up. The
authoritative values are in `visual/environment/TRADE_FIX_OFFICE_01.blockout.json` under
`cameras`; the tables here are that data with its reasoning attached.

---

## 1. The constraint every front camera obeys

The character's eye is at **Z 1 295**. The top bezel of the three main monitors is at
**Z 1 298** (`OFFICE_BIBLE.md` §B). They are level, within 3 mm.

So a front camera at eye level sees a monitor where his face should be. Every south-side camera
is therefore **elevated and angled slightly down**, and each one's sight line to his eye is
checked against the bezel plane at Y 2 350. The clearance column in §3 is that check, and it is
not decoration — it is the number that decides whether a composition exists.

This is also why the brief's "medium-wide hero front" works out as a gentle high angle rather
than a level one. It is not a stylistic choice. It is the only way to see a seated man over his
own monitors.

---

## 2. Lens convention

Fields of view are given as **35 mm-equivalent focal length** plus the horizontal FOV it
implies, because focal length is how compositions are actually discussed and FOV is what the
renderer needs.

| Focal | hFOV | vFOV (16:9) | Character |
|---|---|---|---|
| 24 mm | 73.7° | 46.8° | Wide. Environment reads, character is small |
| 35 mm | 54.4° | 33.1° | Medium-wide. Subject plus context |
| 40 mm | 48.5° | 29.3° | The house lens. Natural, slightly selective |
| 50 mm | 39.6° | 23.5° | Medium. Close to human perspective |
| 85 mm | 23.9° | 13.8° | Close. Compressed, intimate |

Output is **1920 × 1080, 16:9**. Everything below is framed for it.

Nothing wider than 24 mm and nothing longer than 85 mm. Wider distorts the room and makes the
desk read as a wedge; longer flattens the character off the backdrop and loses the window.

---

## 3. The seven compositions

Each row gives the camera position, the look-at target, the lens, and the clearance of the
sight line above the Z 1 298 bezel plane at Y 2 350. Negative clearance would mean the shot does
not exist.

| ID | Name | Position (X, Y, Z) | Target (X, Y, Z) | Lens | Dist. | Bezel clearance |
|---|---|---|---|---|---|---|
| `CAM_1` | **Hero front** | 3 200, 900, 1 620 | 3 200, 3 000, 1 330 | 50 mm | 2 120 | **+98 mm** |
| `CAM_2` | **Side profile** | 5 950, 2 750, 1 340 | 3 300, 3 000, 1 290 | 50 mm | 2 662 | n/a — east of the array |
| `CAM_3` | **Over-shoulder** | 2 780, 4 080, 1 600 | 3 350, 2 400, 1 150 | 40 mm | 1 830 | n/a — north of the array |
| `CAM_4` | **Face close-up** | 3 340, 1 750, 1 500 | 3 215, 2 995, 1 330 | 85 mm | 1 267 | **+104 mm** |
| `CAM_5` | **Wide office** | 5 400, 700, 2 250 | 2 900, 3 100, 1 150 | 24 mm | 3 636 | +267 mm |
| `CAM_6` | **Hands / desk** | 3 981, 1 691, 1 259 | 3 150, 2 700, 760 | 35 mm | 1 399 | exempt — desk surface |
| `CAM_7` | **Three-quarter cinematic** | 4 550, 1 180, 1 690 | 3 250, 2 980, 1 340 | 40 mm | 2 248 | **+138 mm** |

Every figure in this table is checked by `scripts/visual/validate_blockout.py`, which recomputes
each clearance from the blockout and fails if it disagrees with the recorded value by more than
1 mm. The first run of that check found all three front clearances overstated by 20–26 mm, three
anchors beyond seated reach, and two gaze targets physically behind the character. The values
here are the corrected, verified ones.

### CAM 1 — Hero front

Medium-wide: character, desk and the monitor array as a ledge. The reference composition for
colour and value; every other plate is graded against it.

Frame at the subject is 1 528 × 859 mm, spanning Z 900 → 1 760. Where the important lines land,
as a fraction of frame height from the bottom:

| Element | Z | Frame position |
|---|---|---|
| Monitor top bezel (at Y 2 350, nearer camera) | 1 298 | **30 %** |
| Shoulders | 1 080 | 21 % |
| Chin | 1 215 | 37 % |
| **Eye line** | 1 295 | **46 %** |
| Crown | 1 455 | 65 % |

The bezel crosses at 30 %, just above his shoulder line, with cyan glow rising from it into his
face. The window and city sit behind his head. Eye line at 46 % with 35 % headroom is a
deliberately slightly loose medium — it leaves room for the overlay zone in §7 and for a slow
push-in to tighten into.

Downward angle 7.9°. Gentle; it reads as a camera on a tripod slightly above desk height, not
as surveillance.

### CAM 2 — Side profile

True-ish profile from the east, looking west and slightly north. Desk depth runs away from
camera; the window wall fills the right of frame with the city behind it.

Frame 1 917 × 1 078 mm. The east side rather than the west for three specific reasons: the
**watch is on his left wrist** and only reads from here; the window falls on the right of frame
where it balances the lamp pool at the far west end; and the mic boom enters from the east,
giving the near foreground a dark element to sit against.

The one composition where his ear and the full headphone band profile are visible, which makes
it the plate that `TF-HP-01` is judged on.

### CAM 3 — Over-shoulder

From behind and above his **right** shoulder. His head and shoulder occupy the lower-left
foreground, dark and partially cropped; the lit monitor array fills the centre and right;
`TF_MARK_SOUTH` catches the amber falloff on the south wall above the screens.

The one camera that genuinely shows chart content at a readable size, which makes it the shot
the director reaches for when the market is the story. Looking south means the window is behind
camera, so this is the only composition with no city in it — a deliberate contrast beat in a
rotation where every other front shot has skyline.

### CAM 4 — Face close-up

Headphones, eyes, expression, monitor reflections. Slightly off-axis from his left-front rather
than dead-on, because a symmetrical close-up reads as a portrait and an off-axis one reads as
observation.

Frame 536 × 302 mm, spanning Z 1 179 → 1 481: chin at 1 215 and crown at 1 455 both inside, with
26 mm of headroom. Tight.

At 85 mm the depth of field is shallowest here, so the background falls away and the monitor
specular in the iris becomes the brightest thing in frame. **Hold this one short.** It is the
most intimate composition and it is also where any deficiency in the face rig is magnified, so
the director caps it hard in §5.

### CAM 5 — Wide office

From the south-east, high, at 24 mm. Frame 5 450 × 3 066 mm at the subject — the full room
width and the full ceiling height.

All six zones in one frame: desk and character centre, window and city behind, bookshelf and
gold statement west, coffee zone and plant east, door south-east. The character is small, which
is the point: this is the shot that says he is one person in a real room at night.

**This is the spatial coherence check.** If any other plate disagrees with this one about where
something is, that plate is wrong.

### CAM 6 — Hands / desk

Low and close from the south-east, looking down across the desk surface. Frame 1 438 × 809 mm
covering X ≈ 2 431 → 3 869, which holds **all five interactive props**: pen and notebook at
2 480, mouse at 2 720, keyboard at 3 200, mug at 3 760.

Pulled back to 1 399 mm rather than framed tighter, because the composition's job is to prove
hand-to-object contact and a frame missing two of the anchors proves much less. (The first draft
sat at 1 179 mm and cut off both the notebook and the mug — caught when the anchors moved to
satisfy the 830 mm seated-reach limit.)

Carries the highest risk in the whole plan: it is where clipping through the keyboard, a
floating mug and a mug that has drifted off its landing ring all become obvious. The director
rule in §5 is correspondingly strict — **CAM 6 only during an interaction that is already
mid-sequence and locked**, never as a cut into idle hands.

### CAM 7 — Three-quarter cinematic

The primary stream camera and the default. From the south-east, slightly high and angled down
4.5°.

Frame 2 025 × 1 139 mm. Three-quarter view of his face toward screen-left, body angled, window
and city behind, `MON_4` near camera on the right providing a soft out-of-focus foreground glow.
The watch reads on his left wrist. This is the composition that should be on screen when
someone arrives on the stream for the first time, and it holds longest.

### Why all seven work from one set

Every camera resolves against the same blockout, so a mug at (3 760, 2 620) is at the same
place in all seven. The brief's warning — *do not create unrelated backgrounds per angle* — is
answered structurally: the plates are painted from blockout renders, so a plate cannot disagree
with the room without disagreeing with a file.

The honest cost, recorded in ADR-10: in a 2.5D pipeline each of these is its own painted layer
stack, and an eighth camera is an art task, not a code task.

---

## 4. Movement within a shot

A static camera on a 24/7 stream reads as a still photograph within about ninety seconds. Every
camera therefore carries continuous sub-perceptual motion.

| Move | Amplitude | Period | Applies to |
|---|---|---|---|
| **Breathing drift** | 0.3–0.8 % of frame | 45–90 s, randomised | All seven, always |
| **Slow push-in** | Up to 4 % over the hold | One-way per hold | `CAM_1`, `CAM_4`, `CAM_7` |
| **Slow pull-out** | Up to 3 % over the hold | One-way per hold | `CAM_5`, `CAM_2` |
| **Parallax** | Derived from layer depth | Follows the drift | All seven |

Rules:

- Breathing drift is a **two-axis Perlin walk**, not a sine. A sine is recognisable as a loop;
  noise is not, and the whole point is to be unrecognisable.
- A push-in never reverses mid-hold. It resolves, or the shot cuts away during it.
- Push and pull alternate across consecutive holds, so the stream does not creep steadily
  tighter over an hour.
- Parallax is what makes the drift read as a camera rather than as a zoom, and it is the only
  reason the plates must be delivered as depth-assigned layer stacks (`OFFICE_BIBLE.md` §9).
- `CAM_6` gets drift only. A push-in on a hands shot amplifies exactly the contact errors it is
  most exposed to.

---

## 5. The camera director

**Cuts are infrequent and purposeful.** The brief's instruction is explicit — do not switch
every few seconds, typical hold thirty seconds to several minutes, avoid motion sickness. The
director is built to make a dull cut impossible rather than merely unlikely.

### Inputs

| Input | From | Use |
|---|---|---|
| `market_regime`, `market_energy` | `VisualStateV1` | Sets the shot pool and the hold distribution |
| `music_energy`, `transition_state` | `VisualStateV1` | Licenses the optional cinematic cut |
| `character_state`, active motion | `BehaviorDirector` | **Veto.** Blocks cuts mid-blend |
| Time since last cut | Director | Minimum hold enforcement |
| Last six shots | Director | Anti-repeat |
| `active_symbol` change | `VisualStateV1` | Licenses, never forces, a cut |

### Hold times by regime

| Regime | Min hold | Median | Max hold | Shot pool |
|---|---|---|---|---|
| `QUIET`, `LOW_VOLATILITY_RANGE` | 75 s | **150 s** | 300 s | Weighted to `CAM_5`, `CAM_1`, `CAM_2`, `CAM_7` |
| `NORMAL_RANGE`, `COMPRESSION` | 60 s | **110 s** | 240 s | All seven, balanced |
| `BULLISH_TREND`, `BEARISH_TREND` | 50 s | **95 s** | 200 s | `CAM_7`, `CAM_1`, `CAM_3`, `CAM_4` |
| `BREAKOUT_BUILDUP` | 45 s | **80 s** | 170 s | Adds `CAM_3`, `CAM_6` |
| `BULLISH_BREAKOUT`, `BEARISH_BREAKOUT` | **40 s** | **65 s** | 140 s | Weighted to `CAM_3`, `CAM_4`, `CAM_7` |
| `EXTREME_VOLATILITY` | **35 s** | **55 s** | 120 s | As breakout, `CAM_3` strongest |
| `REVERSAL`, `POST_EVENT_NORMALIZATION` | 55 s | 100 s | 220 s | Balanced, `CAM_4` raised |
| `UNKNOWN` / feed down | 90 s | **200 s** | 420 s | `CAM_1`, `CAM_7`, `CAM_5` only |

Quiet to extreme is **150 s down to 55 s** — a factor of 2.7. Visible to a viewer who stays an
hour, invisible as a mechanism. The floor of 35 s is absolute: no market condition justifies
faster cutting, because the brief's "avoid motion sickness" outranks any reactivity target.

The `UNKNOWN` row is deliberately the slowest. When the feed is down there is nothing to react
to, and a director that keeps cutting at its normal rate would be performing market activity
that is not happening.

### Per-shot caps

Because a dramatic shot held too long stops being dramatic, and a hands shot held at all is a
risk.

| Shot | Max single hold | Max share of any rolling hour | Min gap before reuse |
|---|---|---|---|
| `CAM_1` | 240 s | 25 % | 2 shots |
| `CAM_2` | 200 s | 15 % | 2 shots |
| `CAM_3` | 150 s | 20 % | 2 shots |
| `CAM_4` | **75 s** | **8 %** | **4 shots** |
| `CAM_5` | 300 s | 18 % | 3 shots |
| `CAM_6` | **50 s** | **6 %** | **5 shots** |
| `CAM_7` | 300 s | **35 %** | 1 shot |

`CAM_7` is the home shot and is allowed the largest share; the stream should feel like it has a
primary camera it returns to. `CAM_4` and `CAM_6` are the intimate and the risky one, and both
are rationed.

### Selection

Weighted selection with anti-repeat, not `random.choice` — the same discipline ADR-11 applies
to behaviour, and the same in-house pattern as `director/selection.py`.

```
score(shot) = base_weight[regime][shot]
            × recency_penalty(shots_since_last_use)
            × hour_share_penalty(share_in_rolling_hour)
            × narrative_bonus(current_character_state)
            × availability(plate_resident, veto)
```

- `recency_penalty` is 0 inside the minimum gap, then ramps back to 1 over three more shots.
- `hour_share_penalty` falls sharply as a shot approaches its cap and reaches 0 at it.
- `narrative_bonus` is where the camera follows the performance rather than the clock:
  `CAM_6` ×3.0 during a locked `coffee_sequence` or `typing_long`; `CAM_4` ×2.5 during
  `MARKET_REACTION`; `CAM_3` ×2.0 during `ANALYZING`; `CAM_5` ×1.8 during `CAFFEINE_BREAK`.
- `availability` is 0 when the shot's plate stack is not resident and VRAM headroom forbids
  loading it — the quality profile can legitimately reduce the usable shot pool, and the
  director must degrade rather than stall.

### Vetoes — cuts that are simply not allowed

The brief lists *camera cuts during awkward animation blends* as a constraint violation. These
are the hard blocks, checked after selection and before the cut executes:

1. **Mid-blend.** No cut while any motion is inside its blend-in or blend-out window.
2. **Mid-sequence.** No cut during a locked object-interaction sequence
   (`MOTION_LIBRARY.md` §6) — except *into* `CAM_6`, which is the one shot the sequence
   improves.
3. **Minimum hold.** Never before the regime's minimum, with one exception in §6.
4. **Double cut.** Never two cuts within 15 s, regardless of what any other rule wants.
5. **Reaction window.** No cut for 2.5 s after a `MARKET_REACTION` begins. The reaction must be
   seen from the camera that was already running, or the viewer cannot tell that he reacted to
   something — they only see a new shot.

Rule 5 is the one that is easy to get wrong and most damaging: a cut on the reaction frame
converts the single most meaningful moment in the system into an edit.

---

## 6. Licensed cuts

Three events may cut early, and all three are licences rather than commands — the director may
decline, and the vetoes in §5 still apply.

| Event | Earliest | Preferred shot | Probability |
|---|---|---|---|
| **Active symbol change** (XAUUSD ⇄ BTCUSD) | 20 s into hold | `CAM_3`, then `CAM_1` | 0.5 |
| **Music drop / high-energy transition** | 25 s into hold | Any not currently held | 0.3 |
| **Regime escalation of two or more steps** | 20 s into hold | `CAM_3`, `CAM_4` | 0.4 |

**The symbol change is a licence, not a trigger, and this matters.** The brief is explicit that
when the router moves XAUUSD → BTCUSD the screens update and behaviour continues seamlessly —
*do not reset animation*. A guaranteed cut on every switch would make the switch the most
dramatic recurring event on the stream and would teach viewers to read a cut as a routing
event. At probability 0.5 it is sometimes seen and sometimes not, which is how it would read if
a real camera operator were sitting there.

The music licence is the only place music affects the camera, and it is capped at 0.3 for the
reason in `MOTION_LIBRARY.md` §10: he is trading, not performing. A camera that cuts on every
drop turns the stream into a music video.

---

## 7. Overlay-safe zones

The stream composites now-playing metadata, the active market and a station mark over the
render. The brief's constraint: do not cover the character's face or important monitors.

Zones in 1920 × 1080 output coordinates, origin top-left:

| Zone | Rect (x, y, w, h) | Contents |
|---|---|---|
| `OVERLAY_NOW_PLAYING` | 48, 912, 620, 120 | Title, genre, BPM |
| `OVERLAY_MARKET` | 1 320, 48, 552, 92 | Active symbol, regime, energy |
| `OVERLAY_STATION` | 48, 48, 240, 64 | Trade Fix mark, `LIVE` |
| `OVERLAY_TICKER` | 0, 1 032, 1 920, 48 | Optional. Off by default |

**Protected regions — never overlaid, per camera:**

| Camera | Protected |
|---|---|
| `CAM_1` | Face box (x 740–1 180, y 300–620); `MON_1` face (x 620–1 300, y 760–1 010) |
| `CAM_2` | Face box (x 980–1 320, y 340–640) |
| `CAM_3` | **All screen content** (x 480–1 740, y 240–840) |
| `CAM_4` | **The whole frame.** No overlay at all |
| `CAM_5` | Nothing protected; full overlay permitted |
| `CAM_6` | Hand envelope (x 420–1 540, y 380–940) |
| `CAM_7` | Face box (x 560–980, y 280–600); `MON_4` (x 1 400–1 880, y 620–980) |

Two consequences the renderer must implement rather than the operator remember:

- **The overlay set is per camera**, so a zone that is safe on `CAM_5` is suppressed on `CAM_4`.
- Overlays **cross-fade on a cut** over 400 ms rather than popping, so a zone that is valid in
  the outgoing shot and invalid in the incoming one leaves rather than vanishes.

---

## 8. Transitions

| Type | Duration | Used for | Share |
|---|---|---|---|
| **Hard cut** | 0 | The default | ~80 % |
| **Subtle crossfade** | 350–500 ms | Into and out of `CAM_5`; after a long hold | ~15 % |
| **Slow push-in continuation** | 1 200 ms | `CAM_1` → `CAM_4` only, as one continuous move | ~5 % |

Hard cut is the default because it is what a real multi-camera production uses and because it
is the only transition that is invisible when it is right.

The `CAM_1` → `CAM_4` push continuation is the one piece of overt camera language in the
system: the hero front tightens into the close-up as a single move. Reserved for
`MARKET_REACTION` and `ANALYZING`, at most **twice per hour**, because its entire value is that
it is rare enough to register.

**Forbidden:** wipes of any kind, zoom-blur, whip pans, digital glitch, light leaks, film burn,
dissolves longer than 500 ms, any transition with a shape. The brief says no cheesy wipes;
this is the full list that phrase covers.

---

## 9. Manual control

Exposed on the V9 Visual page, and the override semantics matter more than the controls:

| Control | Behaviour |
|---|---|
| `camera_auto` | Off pins the current shot indefinitely. Breathing drift continues — a pinned camera must not become a still |
| `select_camera(id)` | Cuts to it **subject to the §5 vetoes**. An operator cannot cut through a blend either; the request queues until the veto clears, by design |
| `camera_energy` (0.5–1.5) | Scales all hold times inversely. Never below the 35 s floor |
| `hold_shot` | Suspends the maximum-hold cap for the current shot only |

An operator override that bypassed the vetoes would let a single click produce exactly the
artefact the vetoes exist to prevent, on a live stream, with no undo. Queuing instead costs at
most a couple of seconds and cannot produce a broken frame.

---

## 10. What the director must never do

- Cut faster than one shot per 35 s under any condition.
- Cut twice within 15 s.
- Cut during a blend or a locked interaction sequence.
- Cut within 2.5 s of a market reaction starting.
- Use `CAM_4` or `CAM_6` more than their rolling-hour share.
- Return to the shot it just left.
- Cut on every symbol change.
- Beat-match cuts to music.
- Let a shot go fully static.
- Keep cutting at the normal rate when the feed is down.
