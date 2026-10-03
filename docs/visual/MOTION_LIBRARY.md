# Motion library and behaviour director

Every movement TF_TRADER_01 can make, the parameters that govern it, the machine that chooses
between them, and the rules that stop the result from looking like a loop.

---

## 1. Principles

**Five independent clocks, never one.** Breathing, blinking, gaze, posture and task are
separate systems running at unrelated rates. A single scheduler driving all of them is what
makes animation read as mechanical, and no amount of randomisation inside one clock fixes it.

**Nothing is periodic.** Every interval is a draw from a distribution, every amplitude is
scaled by noise, and every return-to-neutral lands somewhere slightly different. The brief's
instruction is literal: *do not blink at exact fixed periods.*

**Subtraction, not addition.** When a motion could be bigger or smaller, it is smaller. The
character is a disciplined professional at hour six, and the failure mode of this kind of system
is always over-animation — a man who fidgets constantly reads as anxious, not as alive.

**He is experienced.** The tie-breaker for every ambiguous call. See `CHARACTER_BIBLE.md` §1.

**Intensity, not frequency, carries market state.** The naive mapping is "more volatility, more
movements per minute", and taken far it produces a twitching man. Market state is expressed
mostly through *which* motions are selected and how sharply they execute, and only secondarily
through how often. §11 quantifies the split.

---

## 2. The motion record

Every entry in the library carries the same fields. This is the schema the behaviour director
reads and the renderer executes.

| Field | Type | Meaning |
|---|---|---|
`id` | str | Stable identifier, e.g. `coffee_sip`
`layer` | enum | Which blend layer it occupies (§8)
`duration_min` / `duration_max` | ms | Sampled uniformly unless noted
`blend_in` / `blend_out` | ms | Cross-fade windows. **A cut may not occur inside either** (`CAMERA_PLAN.md` §5)
`cooldown_min` / `cooldown_max` | ms | Sampled per execution, so the gap is never fixed
`base_weight` | float | Selection weight before all modifiers
`interruptible` | enum | `always` / `after_blend_in` / `never`
`priority` | 0–100 | Higher interrupts lower where interruption is permitted
`band_bias` | map | Multiplier per intensity band B0–B5 (§11)
`music_bias` | float | How much music energy scales the weight, 0 = not at all
`locks` | list | Resources held for the duration (§6)
`gaze_override` | id or null | A gaze target held for the duration (§7)
`expression_hint` | id or null | A facial state biased for the duration
`anti_repeat_key` | str | Group key for the anti-repeat rules (§12)

Durations are in milliseconds, cooldowns shown below in human units for readability.

---

## 3. Base idle — four loops that never align

The idle state must run indefinitely without reading as static or as a cycle. It is not a clip.
It is four independent generators, and its quality is the single largest determinant of whether
the stream feels alive, because it is what is on screen most of the time.

| Generator | Period | Amplitude | Drives |
|---|---|---|---|
| **Breath** | 3.4–4.8 s, drifting ±8 % per cycle | Chest rise 7 mm, shoulder lift 4 mm, slight spine extension | `chest`, `clavicle_L/R`, `spine_02` |
| **Blink** | **see below** | Lid 0→1→0 over 110–150 ms | `lid_upper_L/R` |
| **Ocular micro-motion** | Saccade every 0.4–1.6 s | 0.3–1.2° within the current gaze target | `eye_L/R` |
| **Postural noise** | Continuous | Perlin, ±2 mm translation, ±0.4° rotation | `pelvis`, `spine_01`, `neck` |

### Blink timing

Human blink intervals are not uniform — they cluster. Drawing uniformly produces a visibly
metronomic eye, which is why this gets its own model:

- Base interval: **log-normal**, median 4.1 s, σ 0.55, clamped to [1.2 s, 11 s].
- **Double blink** on 8 % of draws: two blinks 180–260 ms apart.
- **Slow blink** on 4 % of draws: 280–380 ms, correlated with the fatigue curve (§13).
- Rate ×1.35 during `tired_focus`; ×0.75 during `concentrated` — people suppress blinks while
  reading a chart, and that suppression is a strong concentration cue.
- **A blink is forced within 400 ms** of a gaze saccade larger than 12°, because real eyes blink
  across large gaze shifts and omitting it is one of the clearest tells of synthetic animation.

### Why these four and no clip

Breath at ~4 s, blink at ~4.1 s median, saccades at ~1 s, postural noise continuous and
aperiodic. Their least common multiple does not exist in any practical sense, so the combined
state never recurs. A single 4-second idle clip, however well animated, recurs 900 times an
hour.

### Idle over 30 minutes

Phase V4's acceptance bar is that 30 minutes of idle must not visibly loop. On top of the four
generators:

- **Posture sets.** Five authored neutral poses — differing in lean (4–9°), shoulder height,
  head yaw (±6°) and forearm placement. The current set changes every 8–22 minutes via
  `posture_shift`, which makes the *baseline* drift, not just the motion on top of it.
- **Weight shift.** Very slow lateral pelvis drift, 25–70 s period, ±6 mm.
- **Hand tension.** Finger curl 0.1–0.3 on a 12–40 s cycle, independent per hand.

---

## 4. The library

Cooldowns are per-motion and sampled, not fixed. `band_bias` columns are B0–B5 as defined in
§11; `—` means the motion is unavailable in that band.

### 4.1 Micro movements — `layer: micro`

Small, frequent, interruptible. The texture layer.

| id | Dur. (ms) | Blend in/out | Cooldown | Wt | Interr. | B0 | B1 | B2 | B3 | B4 | B5 |
|---|---|---|---|---|---|---|---|---|---|---|---|
`blink` | 110–150 | 0/0 | *see §3* | — | always | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0
`blink_double` | 380–510 | 0/0 | *see §3* | — | always | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0
`blink_slow` | 280–380 | 0/0 | *see §3* | — | always | 1.4 | 1.2 | 1.0 | 0.8 | 0.5 | 0.4
`eye_glance_left` | 600–1 400 | 90/140 | 4–14 s | 1.0 | always | 0.6 | 0.8 | 1.0 | 1.2 | 1.5 | 1.7
`eye_glance_right` | 600–1 400 | 90/140 | 4–14 s | 1.0 | always | 0.6 | 0.8 | 1.0 | 1.2 | 1.5 | 1.7
`eye_glance_down` | 500–1 100 | 90/140 | 8–26 s | 0.7 | always | 0.8 | 1.0 | 1.0 | 1.0 | 0.9 | 0.8
`eye_return_center` | 300–600 | 80/120 | 2–6 s | 1.6 | always | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0
`brow_raise_micro` | 400–900 | 120/200 | 15–60 s | 0.6 | always | 0.4 | 0.6 | 0.9 | 1.1 | 1.4 | 1.5
`frown_micro` | 500–1 200 | 150/240 | 20–75 s | 0.5 | always | 0.3 | 0.5 | 0.9 | 1.2 | 1.5 | 1.6
`jaw_small` | 300–700 | 100/160 | 25–90 s | 0.4 | always | 0.7 | 0.9 | 1.0 | 1.0 | 0.9 | 0.8
`head_tilt_small` | 900–2 200 | 250/380 | 20–70 s | 0.9 | always | 0.9 | 1.1 | 1.0 | 1.0 | 0.9 | 0.8
`neck_stretch_tiny` | 1 100–2 000 | 300/420 | 70–240 s | 0.5 | always | 1.1 | 1.1 | 1.0 | 0.9 | 0.7 | 0.6
`shoulder_shift` | 800–1 700 | 260/360 | 35–130 s | 0.8 | always | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0
`finger_tap` | 500–1 600 | 90/150 | 12–50 s | 0.7 | always | 0.5 | 0.9 | 1.0 | 1.1 | 1.2 | 1.1
`hand_reposition` | 600–1 300 | 160/240 | 25–85 s | 0.7 | always | 0.9 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0

### 4.2 Trading movements — `layer: task`

The bulk of visible work. Most hold a lock and most override gaze.

| id | Dur. (ms) | Blend in/out | Cooldown | Wt | Interr. | Locks | B0 | B1 | B2 | B3 | B4 | B5 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
`mouse_move` | 700–2 200 | 160/220 | 6–30 s | 1.4 | always | `hand_R` | 0.3 | 0.7 | 1.0 | 1.3 | 1.7 | 1.9
`mouse_click` | 180–320 | 60/90 | 4–22 s | 1.1 | after | `hand_R` | 0.2 | 0.6 | 1.0 | 1.3 | 1.6 | 1.8
`mouse_double_click` | 320–480 | 60/90 | 25–120 s | 0.5 | after | `hand_R` | 0.1 | 0.4 | 0.9 | 1.1 | 1.3 | 1.4
`mouse_scroll` | 600–1 800 | 110/170 | 10–45 s | 0.9 | always | `hand_R` | 0.2 | 0.7 | 1.0 | 1.2 | 1.4 | 1.4
`mouse_grab` | 400–800 | 90/140 | 40–180 s | 0.5 | after | `hand_R` | — | 0.2 | 0.7 | 1.1 | 1.6 | 1.9
`chart_drag_pan` | 1 400–3 200 | 200/280 | 60–260 s | 0.6 | after | `hand_R` | — | 0.5 | 1.0 | 1.3 | 1.5 | 1.4
`typing_short` | 1 000–4 000 | 140/200 | **10–60 s** | 1.5 | after | `hand_L`,`hand_R` | 0.2 | 0.7 | 1.0 | 1.4 | 1.7 | 1.8
`typing_long` | 4 500–11 000 | 180/280 | 90–420 s | 0.7 | after | `hand_L`,`hand_R` | 0.1 | 0.5 | 1.0 | 1.3 | 1.5 | 1.3
`hotkey_press` | 250–500 | 70/110 | 30–150 s | 0.6 | after | `hand_L` | 0.1 | 0.4 | 0.9 | 1.3 | 1.7 | 1.9
`monitor_focus_switch` | 800–1 600 | 220/300 | 12–50 s | 1.3 | always | `gaze` | 0.4 | 0.7 | 1.0 | 1.3 | 1.7 | 2.0
`chart_inspect` | 2 500–7 000 | 300/400 | 25–110 s | 1.4 | always | `gaze` | 0.3 | 0.9 | 1.1 | 1.5 | 1.7 | 1.6
`read_chart_sustained` | 7 000–18 000 | 400/550 | 60–280 s | 0.9 | always | `gaze` | 0.5 | 1.3 | 1.1 | 1.0 | 0.7 | 0.5
`lean_forward` | 1 200–2 600 | 380/520 | 30–140 s | 1.0 | after | `spine` | 0.2 | 0.6 | 1.0 | 1.4 | 1.9 | 2.1
`lean_back` | 1 400–3 000 | 420/600 | 45–200 s | 0.9 | after | `spine` | 1.0 | 1.2 | 1.0 | 0.9 | 0.8 | 0.9
`hand_to_chin` | 3 000–9 000 | 420/560 | 120–480 s | 0.7 | after | `hand_L` | 0.6 | 1.2 | 1.1 | 1.0 | 0.7 | 0.5
`hand_on_mouth` | 2 500–7 000 | 400/540 | 150–540 s | 0.5 | after | `hand_L` | 0.5 | 1.0 | 1.1 | 1.1 | 0.8 | 0.6
`write_note` | 3 500–9 000 | 350/480 | 180–720 s | 0.8 | never¹ | `hand_R`,`gaze` | 0.4 | 1.2 | 1.1 | 1.1 | 1.0 | 0.8
`check_notebook` | 2 000–5 000 | 300/400 | 120–480 s | 0.7 | after | `gaze` | 0.6 | 1.2 | 1.0 | 1.0 | 0.8 | 0.6
`look_at_watch` | 1 100–2 000 | 250/340 | 300–1 200 s | 0.4 | after | `gaze` | 1.2 | 1.1 | 1.0 | 0.9 | 0.7 | 0.6
`glance_secondary` | 700–1 500 | 180/250 | 15–70 s | 1.1 | always | `gaze` | 0.4 | 0.8 | 1.0 | 1.3 | 1.6 | 1.8

¹ `write_note` is a locked sequence (§6) and cannot be interrupted once the pen is in hand.

**On `typing_short`.** The brief gives it as a worked example — *duration 1–4 s, cooldown
10–60 s, high probability during trend/breakout*. Those exact figures are reproduced above, and
the B3/B4 bias of 1.4/1.7 is the "high probability during trend/breakout" clause.

### 4.3 Headphone movements — `layer: task`

| id | Dur. (ms) | Blend in/out | Cooldown | Wt | Interr. | Locks | Note |
|---|---|---|---|---|---|---|---|
`hp_adjust_left` | 1 300–2 400 | 280/380 | 240–900 s | 0.6 | after | `hand_L`, `hp_cup_L` | The cable side |
`hp_adjust_right` | 1 300–2 400 | 280/380 | 240–900 s | 0.6 | after | `hand_R`, `hp_cup_R` | Releases the mouse |
`hp_press_closer` | 900–1 600 | 220/300 | 300–1 200 s | 0.4 | after | `hand_L`, `hp_cup_L` | Brief; a listening gesture |
`hp_lift_one_ear` | 1 800–3 400 | 320/440 | 900–3 600 s | 0.25 | never | `hand_L`, `hp_cup_L` | Rare. Locked sequence |
`hp_settle` | 1 100–1 900 | 260/360 | 180–700 s | 0.5 | after | `hand_L`, `hp_band` | Both hands to the band, then down |

Band bias for all five is flat at 1.0 except `hp_lift_one_ear`, which is suppressed to 0.3 in
B4–B5 — he does not take an ear off during a breakout.

A **shared group cooldown of 150 s** applies across all five, enforced by
`anti_repeat_key: headphones`. Without it the scheduler can satisfy five separate cooldowns in
sequence and produce a man fiddling with his headphones for a minute.

### 4.4 Caffeine movements — `layer: task`, locked sequence

The coffee interaction is one sequence, not six motions. See §6.

| id | Dur. (ms) | Blend in/out | Cooldown | Wt | Interr. | Locks |
|---|---|---|---|---|---|---|
`coffee_sequence` | **4 000–8 000** | 400/520 | **8–25 min** | 0.9 | **never** | `hand_L`, `mug`, `gaze` |
`coffee_check_level` | 700–1 400 | 180/250 | 240–900 s | 0.5 | always | `gaze` |
`breath_after_sip` | 900–1 700 | 200/280 | — (chained) | — | always | — |
`coffee_refill_trip` | 18 000–34 000 | 600/800 | **35–90 min** | 0.3 | never | `hand_L`, `mug`, `spine`, `gaze` |

The brief's worked example — *coffee_sip: duration 4–8 s, cooldown 8–25 min, cannot interrupt
emergency reaction, can interrupt idle* — is reproduced exactly, with "cannot interrupt
emergency reaction" expressed as priority 40 against `MARKET_REACTION`'s 85 (§9).

`coffee_refill_trip` is the brief's *reach toward the coffee machine area occasionally*. Mug
state must agree on both sides of any camera cut that covers it (`OFFICE_BIBLE.md` §7).

Band bias: B0 1.3, B1 1.2, B2 1.0, B3 0.9, B4 0.6, B5 0.4. He drinks in the quiet.

### 4.5 Fatigue movements — `layer: task` / `posture`

Used sparingly. The brief's constraint is the specification: *intense, not sick or exhausted.*

| id | Dur. (ms) | Blend in/out | Cooldown | Wt | Interr. | Locks |
|---|---|---|---|---|---|---|
`rub_eye` | 1 400–2 600 | 300/400 | **900–2 700 s** | 0.3 | after | `hand_L`, `gaze` |
`neck_stretch` | 2 000–3 600 | 420/560 | 600–1 800 s | 0.4 | after | `neck`, `spine` |
`deep_exhale` | 1 600–2 800 | 350/480 | 420–1 500 s | 0.5 | always | `chest` |
`head_down_brief` | 1 500–2 800 | 380/500 | 1 200–3 600 s | 0.2 | after | `neck`, `gaze` |
`shoulder_roll` | 1 600–2 900 | 360/480 | 500–1 600 s | 0.4 | after | `clavicle_L/R` |
`look_away_from_monitors` | 2 500–6 000 | 400/550 | 700–2 400 s | 0.4 | always | `gaze` |
`refocus_sharp` | 500–900 | 120/180 | — (chained) | — | never | `gaze` |

Three rules that keep this category from tipping the character over:

1. **Combined fatigue share is capped at 4 % of runtime**, hard, regardless of fatigue phase.
2. **`refocus_sharp` is chained after every one of them.** This is what makes the whole category
   read as discipline rather than decline — the message is *he was tired for two seconds and
   then went back to work*, and without the refocus it is just *he is tired*.
3. All six are **gated by the fatigue curve** (§13) and unavailable below phase 0.35.

### 4.6 Market reaction movements — `layer: reaction`, priority 85

Triggered by state change, not by the scheduler's clock. §11.3 defines the trigger.

| id | Dur. (ms) | Blend in/out | Wt | Interr. | Locks |
|---|---|---|---|---|---|
`react_brow_raise` | 500–1 000 | 100/170 | 1.3 | never | — |
`react_lean_in_quick` | 900–1 700 | 180/300 | 1.5 | never | `spine` |
`react_scan_screens` | 1 400–2 800 | 160/240 | 1.4 | never | `gaze` |
`react_nod_small` | 700–1 300 | 150/220 | 1.0 | never | `neck` |
`react_smirk` | 900–1 800 | 220/320 | **0.35** | never | — |
`react_frustration_exhale` | 1 200–2 200 | 250/350 | 0.6 | never | `chest` |
`react_typing_burst` | 1 200–3 000 | 120/180 | 1.2 | never | `hand_L`,`hand_R` |
`react_mouse_grab` | 500–900 | 90/140 | 1.1 | never | `hand_R` |
`react_write_note` | 2 500–6 000 | 300/420 | 0.7 | never | `hand_R`,`gaze` |
`react_lean_back_after` | 1 600–3 000 | 400/560 | 0.9 | never | `spine` |

A reaction is a **composite of two or three of these**, not one. The composition rules:

- Always opens with one of `react_brow_raise`, `react_lean_in_quick` or `react_scan_screens`.
- Optionally one action: `react_typing_burst`, `react_mouse_grab` or `react_write_note`.
- Optionally one resolution: `react_nod_small`, `react_smirk`, `react_frustration_exhale` or
  `react_lean_back_after`.
- Total duration **2.0–5.5 s**. Longer stops reading as a reaction and starts reading as a
  state change.

**`react_smirk` is directionally gated and cannot be overridden.** It is forbidden when the
move is adverse to the regime's direction, per `CHARACTER_BIBLE.md` §3. The character smirking
at a loss would be the single most character-breaking frame the system could produce, so this
is a hard filter applied after selection, not a weight.

**Forbidden, explicitly:** any celebration, fist pump, arms raised, standing up, head in hands,
desk slam, laughter, shock, any gesture toward the camera. He is experienced. His largest
possible response to a violent move is `subtle_concern` plus a quick lean and a scan.

### 4.7 Music movements — `layer: micro`, additive

| id | Dur. (ms) | Blend in/out | Cooldown | Wt | Note |
|---|---|---|---|---|---|
`music_head_nod` | 1 up to 8 beats | 1 beat/1 beat | 25–160 s | 0.8 | ±1.1° at the neck. **±1.1°, not ±5°** |
`music_finger_tap` | 2–8 beats | 1 beat/1 beat | 20–130 s | 0.7 | Single finger, 1.5 mm travel |
`music_shoulder_rhythm` | 4–12 beats | 2 beats/2 beats | 60–300 s | 0.4 | 2 mm vertical, barely visible |

Capped and constrained in §10. Combined music-motion share of runtime: **under 12 %**.

---

## 5. Character states

High-level states that set the selection context. Transitions are natural because each state
has authored entry and exit motions, and because `RESET_POSTURE` exists as a neutral junction
rather than every state transitioning to every other.

| State | Share (target) | Dominant motions | Typical dwell |
|---|---|---|---|
`IDLE_FOCUS` | **~45 %** | Idle generators, micro layer, occasional `glance_secondary` | 20–90 s |
`ANALYZING` | ~20 % | `lean_forward`, `chart_inspect`, `mouse_move`, `monitor_focus_switch`, `hand_to_chin` | 15–70 s |
`EXECUTING` | ~8 % | `typing_short`, `mouse_click`, `hotkey_press`, `mouse_grab` | 4–20 s |
`WAITING` | ~10 % | `read_chart_sustained`, `lean_back`, `coffee_check_level`, slow blinks | 25–120 s |
`NOTE_TAKING` | ~5 % | `check_notebook`, `write_note`, `eye_glance_down` | 10–40 s |
`CAFFEINE_BREAK` | ~4 % | `coffee_sequence`, `breath_after_sip`, occasionally `coffee_refill_trip` | 6–35 s |
`MARKET_REACTION` | ~3 % | §4.6 composites | 2–6 s |
`RESET_POSTURE` | ~5 % | `posture_shift`, `shoulder_roll`, `neck_stretch`, `deep_exhale` | 3–10 s |

### Transition graph

```
IDLE_FOCUS  ⇄  ANALYZING  →  EXECUTING  →  ANALYZING  →  IDLE_FOCUS
     ⇅              ↓                            ↓
  WAITING  ⇄  NOTE_TAKING                  RESET_POSTURE
     ↓                                           ↓
CAFFEINE_BREAK  ──────────────────────────→  IDLE_FOCUS

MARKET_REACTION  ←── (interrupts any state except a locked sequence)
       └──→ returns to ANALYZING (0.6) | IDLE_FOCUS (0.3) | EXECUTING (0.1)
```

The brief's worked example is this path:
`ANALYZING` → `lean_forward` → `chart_inspect` → `mouse_move` → `typing_short` →
`IDLE_FOCUS`.

Three rules:

- **`EXECUTING` is only reachable from `ANALYZING`.** Nobody types an order out of idle. This
  one constraint does more for believability than any amount of motion polish — it means
  visible action always has visible deliberation in front of it.
- **`MARKET_REACTION` returns to `ANALYZING` 60 % of the time.** A reaction that resolves
  straight back to idle implies he saw something and dismissed it. Mostly he looks into it.
- **`RESET_POSTURE` is the junction.** Any state may route through it, and it is where the
  posture set (§3) is permitted to change. Changing posture sets anywhere else produces a
  visible discontinuity in the baseline.

---

## 6. Object interaction and locks

The brief's constraint list — *hands clipping through the keyboard, cup floating, movement
restarting mid-cycle* — is addressed by two mechanisms: **anchors** make contact accurate, and
**locks** make sequences atomic.

### Anchors

Declared in `TRADE_FIX_OFFICE_01.blockout.json` under `anchors`, in room coordinates, so hand
targets resolve against the real object rather than against a per-camera tuned pose.

| Anchor | Position (X, Y, Z) | Reach | Hand | Approach |
|---|---|---|---|---|
`ANCHOR_MOUSE` | 2 720, 2 560, 760 | 726 | R | From above, 25 mm settle |
`ANCHOR_KEYBOARD_HOME_L` | 3 080, 2 740, 757 | 432 | L | From above, 18 mm |
`ANCHOR_KEYBOARD_HOME_R` | 3 320, 2 740, 757 | 432 | R | From above, 18 mm |
`ANCHOR_MUG_BODY` | 3 760, 2 620, 790 | 736 | L | Lateral, from the north-east |
`ANCHOR_MUG_RING` | 3 760, 2 620, 735 | 760 | — | The landing ring. ±6 mm tolerance |
`ANCHOR_MUG_LIP` | 3 760, 2 620, 845 | 716 | — | Rim height for the sip pose |
`ANCHOR_NOTEBOOK` | 2 480, 2 810, 742 | 818 | R | From above with the pen |
`ANCHOR_PEN` | 2 480, 2 890, 745 | 802 | R | Pinch, from above |
`ANCHOR_HP_CUP_L` | 3 340, 2 985, 1 330 | 287 | L | From below and outward |
`ANCHOR_HP_CUP_R` | 3 060, 2 985, 1 330 | 287 | R | From below and outward |
`ANCHOR_HP_BAND` | 3 200, 2 990, 1 455 | 375 | L+R | From above |
`ANCHOR_STREAM_PAD` | 3 900, 2 860, 758 | 783 | L | From above |

**Every anchor is inside the 830 mm seated reach** from the shoulder at (3 200, 3 000, 1 080) —
upper arm 365 + forearm 270 + hand 195. That constraint, not composition, is what decides where
the notebook, pen, mouse and stream pad sit: the desk is 3 600 mm wide and its outer thirds
cannot be touched from the chair. `scripts/visual/validate_blockout.py` fails on any anchor
beyond it, and its first run rejected three of these twelve.

Reach is solved by a **two-bone analytic solve** in the camera's 2D projection, from
`shoulder` through `elbow` to `wrist`, with the wrist orientation taken from the anchor's
declared approach vector. Not general IK — adequate because every target is a known seated
reach to a fixed object, and because anchors give contact position directly.

**Contact tolerance is 4 mm.** Beyond that the hand visibly misses or sinks, and both are listed
in the brief's constraint set.

### Locks

A lock is a resource held for a motion's duration. A motion cannot start if any of its locks is
held.

| Lock | Held by |
|---|---|
`hand_L`, `hand_R` | Any motion using that hand |
`gaze` | Any motion overriding gaze |
`spine`, `neck`, `chest` | Posture and breathing overrides |
`mug`, `hp_cup_L`, `hp_cup_R`, `hp_band` | Prop interactions |

### Locked sequences

Three motions are **atomic**: once begun they run to completion and nothing — not the
scheduler, not a market reaction, not an operator — interrupts them.

```
coffee_sequence:
  reach → grip → lift → raise_to_lip → sip(1.2–2.4s) → lower → place → release
  Locks hand_L, mug, gaze for the whole run. Chains breath_after_sip on exit.

write_note:
  glance_down → reach_pen → pinch → write(2–6s) → lift → place_pen → release → glance_up
  Locks hand_R, gaze.

hp_lift_one_ear:
  reach → grip_cup → lift_clear → hold(0.8–1.6s) → return → settle → release
  Locks hand_L, hp_cup_L.
```

**This is why the system cannot produce a floating mug.** A market reaction arriving mid-sip
does not interrupt — it is **deferred** and executes on the first frame after `release`, with
its blend-in starting from the pose the sequence ended in. The brief's own example says coffee
*cannot interrupt* an emergency reaction; the converse is equally required, because a reaction
that cancels a grip leaves a mug in mid-air with no hand on it.

Deferral is capped at **1.5 s**. Past that the reaction is dropped entirely rather than fired
late, because a reaction 3 s after its cause reads as a reaction to nothing.

---

## 7. Gaze

Gaze is the single strongest aliveness cue and the easiest to get wrong. It runs as its own
layer, independent of everything above, and it resolves against **world-space targets declared
in the blockout** — never against per-camera angles. Hand-tuned angles are precisely how eyes
end up looking through monitors.

| Target | Resolves to | Yaw | Pitch | Base share |
|---|---|---|---|---|
`GAZE_MON_1` | `MON_1` centre — 3 200, 2 350, 1 130 | 0° | −14.2° | **42.0 %** |
`GAZE_MON_2` | `MON_2` centre — 2 500, 2 350, 1 130 | −47.1° | −9.8° | 16.0 % |
`GAZE_MON_3` | `MON_3` centre — 3 900, 2 350, 1 130 | +47.1° | −9.8° | 14.0 % |
`GAZE_MON_4` | `MON_4` centre — 4 560, 2 350, 1 130 | +64.5° | −6.2° | 6.0 % |
`GAZE_MON_5` | `MON_5` centre — 1 840, 2 350, 1 130 | −64.5° | −6.2° | 3.0 % |
`GAZE_PANEL_W1` | West wall — 60, 1 900, 1 700 | −70.7° | +6.9° | 4.5 % |
`GAZE_PANEL_W2` | West wall — 60, 2 600, 1 700 | −82.7° | +7.3° | 3.5 % |
`GAZE_NOTEBOOK` | 2 480, 2 810, 742 | −75.2° | −36.6° | 5.0 % |
`GAZE_MOUSE` | 2 720, 2 560, 760 | −47.5° | −39.4° | 2.5 % |
`GAZE_MUG` | 3 760, 2 620, 845 | +55.8° | −33.6° | 1.5 % |
`GAZE_MIDDLE_DISTANCE` | Unfocused, past the screens | 0° | −0.5° | 1.2 % |
`GAZE_CAMERA` | The active camera's position | — | — | **≤ 0.6 %** |
`GAZE_OVER_SHOULDER_WINDOW` | 2 600, 4 800, 1 600 — **torso turn** | −161.6° | +9.1° | **0.2 %** |

**89.0 % of gaze is on a screen** — the seven surfaces above. That is what a working trader's
eyes do, and it is the number that makes the rest read correctly. Shares sum to exactly 1.000;
the blockout validator asserts both.

### Two targets that are not here, and why

**There is no `GAZE_KEYBOARD`.** The keyboard centre sits at pitch −64° from his eye — he would
be looking almost straight down. He is a touch typist, and the brief's own gaze target list
does not include the keyboard. Removed rather than accommodated by loosening the pitch limit.

**The window is entirely behind him**, at yaw −162°, because that is exactly what makes it his
backdrop in every front camera. Looking out of it is a real thing a trader does, but it needs a
chair swivel and a torso turn, so it is not part of the routine distribution: it belongs to
`look_away_from_monitors` alone, at a 0.2 % share, flagged `requires_torso_turn`. A 3 % share of
180° turns — which the first draft had — would have been one of the most noticeable artefacts in
the system.

### Camera glance

`GAZE_CAMERA` is capped at **0.6 % of runtime, at most once per 12 minutes, 0.5–1.2 s**, and is
only permitted from `CAM_4` or `CAM_7` during `IDLE_FOCUS` or `WAITING`.

The brief's instruction is *do not stare directly at viewer constantly* and *occasional camera
glance only if appropriate*. A rare, brief, unhurried glance is enormously effective — it is
the moment the viewer feels acknowledged. The same glance at four times the rate makes him a
presenter, and the station is not a presentation.

### Mechanics

- **Saccade then head.** Eyes arrive first (40–90 ms), head follows for 25–60 % of the
  remaining angle over 180–400 ms. Simultaneous eye-and-head motion is the clearest signal of a
  cheap gaze rig.
- **Head contribution scales with angle.** Under 10°, eyes only. Over 25°, the head carries most
  of it. Over 45°, shoulders participate. Over 70°, the torso participates — which, per the table
  above, is the normal case for the notebook and both wall panels.
- **A blink is forced** across saccades over 12° (§3).
- **Default pitch is −14.2°**: eye at Z 1 295 to `MON_1` centre at Z 1 130 is −165 mm over
  650 mm horizontal. Inside the 15–20° ergonomic band for screen centre, and the reason he reads
  as looking *at* his charts rather than over them.
- **Vergence** shifts between near (notebook, ~820 mm) and far (screens, ~700 mm; middle
  distance, 1 800 mm+) targets.
- **No instant snapping.** Minimum 180 ms to any new target, including during reactions —
  `react_scan_screens` is fast, not instantaneous.

---

## 8. Blend architecture

ADR-10 records that this is hand-built rather than inherited from an engine's blend tree. This
is what gets built.

### Layers, composited bottom to top

| # | Layer | Mode | Content |
|---|---|---|---|
0 | `base_posture` | Replace | The current posture set from §3 |
1 | `breath` | Additive | Chest, shoulder, spine |
2 | `postural_noise` | Additive | Perlin on pelvis, spine, neck |
3 | `task` | Override, masked | Arms, hands, spine where claimed |
4 | `micro` | Additive | Head, shoulders, fingers, brows |
5 | `gaze` | Override, masked | Eyes, head yaw contribution |
6 | `face` | Override, masked | Lids, brows, mouth, cheeks |
7 | `reaction` | Override, masked, priority 85 | Pre-empts 3 and 4 |

Masks are per-joint, so `task` claiming the arms does not stop `breath` moving the chest or
`micro` moving the brows. **Additive layers are never masked off by an override layer** — this
is what keeps him breathing during a coffee sip, and a rig that loses breathing inside task
motions looks frozen from the chest up.

### Blending

- Cross-fade on a per-joint **cubic ease-in-out** over the motion's declared blend window.
- **Phase-preserving on interrupt.** A motion interrupted at 60 % blends out *from its current
  pose*, not from its start. The brief lists *movement restarting mid-cycle* as a violation, and
  this is the mechanism that prevents it.
- **Blend windows are never zero** except for blinks, which are instantaneous by nature.
- **No blend may be entered while another blend on the same joint is in flight.** The second
  motion waits. A double blend on one joint produces the neck snap the brief forbids.

### Deformation

Per-layer mesh deformation driven by the §8 skeleton in `CHARACTER_BIBLE.md`: each sprite layer
carries a triangulated mesh with vertex weights against nearby joints. Standard 2D skinning,
with two additions the character needs:

- **Depth-ordering swaps** on head yaw past ±18°, so the far ear, the far brow and the far
  headphone cup re-sort correctly.
- **Free-motion on `34_hair_strays`** — a two-segment verlet chain per stray, driven by head
  acceleration, damped hard. This is the brief's *realistic hair movement where supported*, and
  three strands is where the cost/benefit peaks.

---

## 9. The behaviour director

Lives in Python (ADR-11). Runs at **20 Hz** — fast enough that a reaction fires within 50 ms,
slow enough to be free.

### Tick

```
1.  Advance clocks. Update fatigue phase (§13) and the music beat phase.
2.  Retire finished motions. Release their locks.
3.  Resolve deferred reactions whose locks have cleared (§6).
4.  Check reaction triggers (§11.3). If fired → MARKET_REACTION, skip to 8.
5.  Evaluate the state machine (§5). Possibly transition.
6.  For each free layer, if its layer clock has elapsed:
        build the candidate set
        score every candidate
        apply the hard filters
        select, or select nothing
7.  Sample the next layer-clock interval. Never a constant.
8.  Emit commands for anything starting this tick.
9.  Record to history (§12). Update rolling shares.
```

### Scoring

```
score(m) = m.base_weight
         × band_bias[m][band]                     # §11
         × music_factor(m, music_energy)          # §10
         × state_affinity[state][m]                # §5
         × cooldown_gate(m)                        # 0 or 1
         × anti_repeat_penalty(m, history)         # §12
         × fatigue_factor(m, phase)                # §13
         × share_penalty(m, rolling_share)
         × lock_available(m)                       # 0 or 1
```

Selection is **weighted sampling without replacement** over the non-zero scores, plus an
explicit **do-nothing candidate** whose weight rises with recent motion density. The
do-nothing option is not an implementation detail — it is what produces stillness, and a
scheduler that always selects something is a scheduler that produces a fidgeting man.

The brief's prohibition on `random.choice` is satisfied by every multiplier above. The pattern
follows `director/selection.py`, `director/diversity.py` and `director/history.py`, which
already solve weighted choice under anti-repetition constraints for music.

### Hard filters, applied after scoring

Expressed as filters rather than weights because a weight can always be overcome by enough
other multipliers, and these must never be:

1. `react_smirk` during an adverse move (§4.6).
2. Fatigue motions below fatigue phase 0.35, or above the 4 % cap (§4.5).
3. Any motion whose locks are held.
4. Any motion inside its sampled cooldown.
5. Any motion matching the last entry's `anti_repeat_key` (§12).
6. `GAZE_CAMERA` outside its permitted cameras and states (§7).
7. Any `task`-layer motion during a locked sequence (§6).

---

## 10. Music reactivity

**He is trading, not dancing.** The whole of this section is a set of ceilings.

Per ADR-11, Python sets rhythmic *policy* and the renderer keeps rhythmic *phase*: the director
sends BPM, downbeat phase and a nod-probability weight; the renderer runs the local beat clock
and decides which beats land. Round-tripping per beat is not possible and not needed.

| Target | Effect of `music_energy` (0→1) | Ceiling |
|---|---|---|
`music_head_nod` weight | 0.2 → 1.4 | Amplitude **±1.1°** at any energy |
`music_finger_tap` weight | 0.3 → 1.2 | 1.5 mm travel |
`music_shoulder_rhythm` weight | 0.0 → 0.7 | 2 mm vertical |
Typing cadence | +0 % → **+6 %** | Keystroke rate only; never the motion's duration |
Breath rate | +0 % → +3 % | — |
Camera cut licence | §6 of `CAMERA_PLAN.md` | 0.3 probability |

Hard ceilings, which no state and no operator setting may exceed:

- Combined music-motion share of runtime: **under 12 %**.
- `music_head_nod` runs at most **8 consecutive beats**, then a mandatory 25 s gap. *"Do not
  head-nod continuously"* is in the brief, and this is the number that enforces it.
- Beat synchronisation applies to **at most one** motion at a time. Nodding and tapping together
  on the same beat reads as dancing immediately.
- Music **never** affects: posture, gaze, expression, reaction selection, trading motion
  selection, or lighting beyond the ±3 % in `OFFICE_BIBLE.md` §5.
- Above 160 BPM, nod lands on **every second beat**. At 174 BPM a per-beat nod is physically
  wrong for a seated man and reads as a glitch.
- Below 85 BPM, nod lands on beats 1 and 3 only.

The target: a viewer who mutes the stream should notice that something subtle has gone, without
having been able to name it while it was playing.

---

## 11. Market reactivity

### 11.1 The abstraction

The brief is explicit — *do not directly embed market logic into animations* — so no motion in
§4 names a regime. The fourteen `MarketRegime` values collapse into **six intensity bands**,
and only the bands appear in the library.

| Band | Name | Regimes |
|---|---|---|
**B0** | `DORMANT` | `UNKNOWN`; any regime while the feed is `DISCONNECTED` or the session is `CLOSED` |
**B1** | `QUIET` | `QUIET`, `LOW_VOLATILITY_RANGE` |
**B2** | `STEADY` | `NORMAL_RANGE`, `COMPRESSION`, `POST_EVENT_NORMALIZATION` |
**B3** | `FOCUSED` | `BULLISH_TREND`, `BEARISH_TREND`, `BREAKOUT_BUILDUP` |
**B4** | `ALERT` | `BULLISH_BREAKOUT`, `BEARISH_BREAKOUT`, `HIGH_VOLATILITY_RANGE`, `REVERSAL` |
**B5** | `PEAK` | `EXTREME_VOLATILITY` |

The mapping lives in exactly one place — the state bridge (`STATE_BRIDGE.md` §5) — so a new
regime is a one-line change there and touches no animation data. This is the same seam ADR-02
drew around the generator: market-aware upstream, behaviour-aware downstream.

### 11.2 Band effects

| Band | Motion rate | Mean blend | Reaction threshold | Idle share | Fatigue |
|---|---|---|---|---|---|
B0 | **×0.55** | ×1.30 | disabled | 70 % | ×1.2 |
B1 | ×0.75 | ×1.15 | 0.80 | 58 % | ×1.1 |
B2 | ×1.00 | ×1.00 | 0.65 | 45 % | ×1.0 |
B3 | ×1.20 | ×0.90 | 0.50 | 34 % | ×0.8 |
B4 | ×1.45 | ×0.80 | 0.38 | 24 % | ×0.5 |
B5 | **×1.60** | **×0.72** | 0.30 | 20 % | ×0.3 |

**The rate span is only 0.55 → 1.60.** Deliberately narrow. The visible difference between
quiet and extreme comes mostly from *which* motions are selected — B5's bias table favours
`mouse_move` 1.9, `monitor_focus_switch` 2.0 and `lean_forward` 2.1 while suppressing
`read_chart_sustained` to 0.5 and `coffee_sequence` to 0.4 — and from blend times 28 % shorter,
which makes every motion land more sharply. A man moving 1.6× as often with crisper motions
reads as markedly more alert. A man moving 4× as often reads as panicking, and the brief's
instruction for `EXTREME_VOLATILITY` is *increase movement frequency slightly, but still
preserve realism.*

`market_energy` (0–100) modulates within the band by ±15 % of the rate multiplier, so the
response is continuous rather than stepped at band boundaries.

**B0 is quieter, not frozen.** When the feed is down he keeps breathing, blinking, reading and
drinking coffee — he just has nothing to react to, which is honest. Reactions are disabled
outright: a reaction with no market event behind it is the visual equivalent of a fabricated
price, and the station's contracts make that unrepresentable rather than merely discouraged.

### 11.3 Reaction triggers

A reaction fires when a **salience score** crosses the band's threshold:

```
salience = 0.40 × normalised |Δ energy over 30 s|
         + 0.30 × regime_change_magnitude      # band distance, 0 if none
         + 0.20 × normalised |energy_velocity|
         + 0.10 × confidence_delta
```

Gates:

- **Refractory period: 25 s.** No second reaction inside it, at any salience.
- **Rolling cap: 10 reactions per hour.** Above that they stop being events.
- Suppressed entirely in B0.
- Suppressed when `confidence < 0.35` — he does not react to a classification the engine itself
  does not believe.
- Deferred, not cancelled, during a locked sequence; dropped if the defer exceeds 1.5 s (§6).

Direction (`BULLISH` / `BEARISH`) selects the resolution motion, never the opener. He notices
movement before he notices which way, which is both true and the only way `react_smirk`'s
directional gate can be applied correctly.

---

## 12. Anti-repetition

The brief's requirement is specific, and so is the implementation.

### Tracked

| Record | Depth |
|---|---|
Motion history | **Last 20**, with timestamps and layer |
Per-key last-use time | Every `anti_repeat_key` |
Last coffee, headphone, note, posture-shift, reaction | Individually, by wall clock |
Rolling 60-minute share | Per motion and per category |
Sequence memory | Last **6** ordered triples |

### Rules

1. **No motion repeats back-to-back on its layer.** Hard filter, no exceptions.
2. **No `anti_repeat_key` repeats within 3 entries** on its layer.
3. **Penalty by recency:** ×0.15 if in the last 3 entries, ×0.45 in the last 8, ×0.8 in the last
   20, ×1.0 beyond.
4. **No sequence triple repeats within the last 6.** This is what stops
   *lean → inspect → type* becoming a visible signature even when no individual rule is broken,
   and it is the rule the brief is reaching for with *avoid obvious sequences repeating*.
5. **Group cooldowns** across related motions: headphones 150 s, fatigue 300 s, coffee 480 s.
6. **Share ceilings** over any rolling hour: no single motion above 14 %, no category above its
   §5 target plus 8 points.
7. **Jitter on everything.** Durations, cooldowns, amplitudes and blend times all sampled, never
   constant. Amplitudes carry ±12 % noise so the same motion is never the same size twice.

### Why rule 4 matters most

Rules 1–3 are satisfied by most naive schedulers and are not what makes a stream feel looped.
What viewers actually notice is the *n*-gram: the same three motions in the same order, twenty
minutes apart. Tracking ordered triples is cheap and it is the difference between a system that
passes a two-minute review and one that passes a two-hour soak.

---

## 13. Fatigue and long-term variation

### The fatigue curve

A slow 0→1 phase advancing over a configurable session, default **7 hours** to full, then
resetting on a `coffee_refill_trip` by −0.25 and on a station-ID break by −0.1.

| Phase | Effect |
|---|---|
0.00–0.35 | Fatigue motions unavailable |
0.35–0.60 | Available at low weight. `blink_slow` rate ×1.15 |
0.60–0.85 | `tired_focus` expression permitted. Blink rate ×1.25. Posture lean reduces 1° |
0.85–1.00 | Fatigue weight peaks. Blink ×1.35. `refocus_sharp` weight ×1.4 |

**It never reads as decline.** The 4 % cap holds at every phase, `refocus_sharp` always chains,
and the lean reduction is 1°. The curve's real job is to make hour six *different* from hour
one, not worse.

### Multi-day variation

Over days, slow drift so a returning viewer does not see the same man doing the same things:

| Parameter | Varies | Period |
|---|---|---|
Posture set distribution | Which of the five dominate | 6–18 h |
Monitor focus bias | Which secondary screen he favours | 4–12 h |
Coffee frequency | ±35 % of base cooldown | Per session |
Work rhythm | `ANALYZING` / `WAITING` ratio, ±20 % | 8–24 h |
Micro-motion density | ±15 % | 3–9 h |
Camera emphasis | Shot weight drift, ±20 % | 6–18 h |

**None of this touches identity.** `CHARACTER_BIBLE.md` §10 draws that line, and nothing here
crosses it.

---

## 14. Motion reference sheet

Visual targets for human review (`VISUAL_IMPLEMENTATION_PLAN.md` V11). Each needs a reference
clip in `visual/references/motion/`.

| Motion | What to check | Fails if |
|---|---|---|
`blink` | 110–150 ms, lid eases, lower lid lifts 15 % | Linear, or upper lid only |
`breath` | Chest 7 mm, shoulders 4 mm, ~4 s, continues under all task motions | Visible in the stomach; stops during a sip |
`music_head_nod` | ±1.1° at the neck, on the beat, stops after 8 beats | Reads as dancing; shoulders join |
`mouse_move` | Wrist pivots, forearm slides, fingers stay in contact | Whole arm translates; hand hovers |
`typing_short` | Fingers strike independently, wrists float 15 mm, no key passed through | Hands move as blocks; fingers sink into keys |
`coffee_sequence` | Grip closes before lift; mug returns within 6 mm of the ring; steam matches temperature | Mug floats; lands off-ring; steam from a cold mug |
`hp_adjust_left` | Fingers contact the cup, cup deforms the pad, band flexes | Hand passes through; band rigid |
`chart_inspect` | Eyes lead, head follows 25–60 %, blink across large shifts | Eyes and head move together; no blink |
`lean_forward` | Spine articulates across three joints; shoulders lead | Rotates as one rigid block |
`write_note` | Pen contacts paper; wrist pivots; gaze stays down | Pen floats; gaze drifts to screen mid-write |
`posture_shift` | Baseline moves and stays moved | Snaps back; or reads as a separate motion |
`react_lean_in_quick` | Inside 1.7 s, blend ≤ 300 ms, no camera cut for 2.5 s | Too slow to read; cut lands on it |

---

## 15. Forbidden

Collected from the brief and from the sections above, in one place, because a single list is
auditable and ten scattered prohibitions are not.

**Animation:** hands clipping the keyboard, desk or mug · floating mug · eyes looking through
monitors · neck snapping · instant posture change · motion restarting mid-cycle · double blend
on one joint · interrupting a locked sequence · gaze snapping under 180 ms · simultaneous
eye-and-head saccade onset.

**Behaviour:** the same motion twice in a row · the same triple within six · coffee more often
than every 8 minutes · continuous head-nodding · blinking on a fixed period · a visible idle
clip · fatigue above 4 % of runtime · more than 10 reactions an hour · reactions with the feed
down · `EXECUTING` entered from `IDLE_FOCUS`.

**Character:** celebration, fist pump, raised arms, standing, head in hands, desk slam,
laughter, shock, rage, any gesture to camera · `react_smirk` on an adverse move · dancing ·
staring at the viewer · any expression outside the six in `CHARACTER_BIBLE.md` §3 or beyond its
stated amplitude.
