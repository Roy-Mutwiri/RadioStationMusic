# Behaviour Director

What TF_TRADER_01 does next, and how that is decided. Pure logic: no artwork, no
rendering, no station dependency.

**Code:** `tradefix_radio/visual/` — `catalog.py` (data), `scheduler.py` (mechanics),
`modulation.py` (market/music intensity), `rhythm.py` (the long-run work cycle),
`gaze.py` (eyes), `director.py` (decisions), `simulate.py` (harness).
**Tests:** `tests/unit/test_visual_behavior.py`, `tests/unit/test_visual_rhythm.py`,
`tests/endurance/test_visual_soak.py`.
**Run it:** `tradefix visual simulate --scenario breakout --duration 2h`.

---

## 1. Why this is Python

ADR-11 decided it; the soak proves it. A 24-hour behaviour run completes in minutes
against `VirtualClock.advance_sync`, is reproducible from a seed, and produces numbers
a human can argue with. None of that is practical in a browser, and all three were
needed: **nine real defects in this system were found by the soak, not by reading the
code.** They are listed in `V6_REPORT.md` §4.

The division of labour is exact:

| | Decides | Performs |
|---|---|---|
Python | which action, when, how long, with what amplitude, where the eyes go | — |
Renderer | which beats a nod lands on | everything else |

The one thing the renderer decides is beat phase, because per-beat round trips are
impossible at 174 BPM. Everything else is a choice, and choices live where they can be
tested.

## 2. The tick

20 Hz. Fast enough that a reaction fires within 50 ms, slow enough to be free. Nine
steps, and the order matters:

```
1  advance clocks        fatigue phase, rolling activity, history trim
2  retire                finished actions; release their locks
3  advance chains        step a running chain forward
4  resolve deferrals     reactions whose locks have cleared
5  check reactions       salience against the band's threshold
6  evaluate state        possibly transition
7  select                score candidates, if the action clock has elapsed
8  drive gaze and blink  independently, on their own clocks
9  record                history, trigram log, activity
```

**Selection is last among the decisions, deliberately.** Everything before step 7 can
only *remove* freedom: a running chain forbids new work, a reaction pre-empts, a state
change narrows the admissible set. Scoring against an already-narrowed set is both
cheaper and correct. The reverse order — select, then discover the hand is busy — is how
a scheduler silently drops choices, which shows up as a character who does nothing for
ten seconds at a time.

## 3. The action model

Two contracts, not one, and the split is load-bearing.

**`ActionSpecV1`** is the catalogue entry: ranges, weights, biases, locks. Static,
loaded once, never mutated.

**`CharacterActionV1`** is one instance: ranges already sampled, amplitude already
jittered. This is what the director emits and the renderer performs.

The brief asks for a single `CharacterAction` carrying `duration`, `cooldown_range`,
`weight` and `market_bias` together. Those belong to two different lifetimes: a cooldown
range is a property of *what a coffee sip is*, a duration of 5 240 ms is a property of
*this* sip. Collapsing them means either re-sending static tuning data on every command
or mutating a frozen contract. `action_id` joins them.

### Fields

| Field | On | Meaning |
|---|---|---|
`action_id`, `category` | both | identity and family |
`duration_ms` | spec: range · action: sample | — |
`blend_in_ms` / `blend_out_ms` | both | cross-fade windows; scaled by the band |
`interruptibility` | both | `always` / `after_blend_in` / `never` |
`interaction_locks` | spec | claimed for the duration; `BOTH_HANDS` expands |
`cooldown_range_seconds` | spec | **sampled per execution, never fixed** |
`weight` | spec | base selection weight |
`market_bias` | spec | `BandBiasV1` — six multipliers, B0–B5. **No regime names** |
`music_bias` | spec | 0.0 on 68 of 71 actions |
`allowed_character_states` | spec | empty means every state |
`required_anchor` | spec | resolves in the frozen blockout |
`gaze_target` | spec | semantic; resolves to real geometry |
`camera_affinity` | spec | advisory, for the later camera director |
`energy_cost` | spec | feeds the activity damper |
`tags` | spec | `reflex`, `beat_locked`, `favourable_only`, reaction roles |
`anti_repeat_key` | spec | group key for shared cooldowns and penalties |
`amplitude` | action | 0.88–1.12 jitter, pre-applied |
`chain_id` / `chain_step` | action | set when part of a transactional interaction |
`factors` | action | why it was chosen — for the debug overlay |

## 4. The catalogue

71 actions, 56 schedulable. Every action the brief names exists under that name;
`test_catalogue_covers_every_brief_action` asserts it.

| Family | Count | Hour-share ceiling |
|---|---|---|
`MICRO` | 14 | 62 % |
`WORK` | 24 | 48 % |
`POSTURE` | 7 | 6 % |
`HEADPHONES` | 4 | 4 % |
`CAFFEINE` | 8 | 8 % |
`FATIGUE` | 4 | 4 % |
`REACTION` | 7 | 6 % |
`MUSIC` | 3 | 12 % |

The counts exceed the brief's lists because chain internals are specs too — the renderer
needs something concrete to perform and the timeline needs something to print. They carry
`chain_only=True` and the scheduler cannot reach them.

**No action names a market regime.** `test_no_action_names_a_market_regime` checks every
id, tag and key. That is the seam: a fifteenth regime is one line in the bridge.

## 5. Character states

Eight behavioural states. **Not animation labels** — a state does not play, it changes
what is likely to play, what is admissible at all, and how long the director dwells.

| State | Share (24 h, measured) | Dominant actions |
|---|---|---|
`IDLE_FOCUS` | 34 % | idle generators, micro layer, occasional glance |
`ANALYZING` | 43 % | `lean_forward`, `chart_inspect`, `mouse_move`, monitor switches |
`EXECUTING` | 3 % | `typing_*`, `mouse_click`, `hotkey` |
`WAITING` | 11 % | long chart holds, `lean_back`, slow blinks |
`NOTE_TAKING` | 6 % | the `NOTE_WRITE` chain |
`CAFFEINE_BREAK` | 2 % | the `COFFEE_DRINK` chain, `coffee_reset` |
`MARKET_REACTION` | 1 % | reaction composites |
`POSTURE_RESET` | 0.2 % | the `POSTURE` family — though it is no longer confined to this state; see §15 |

`ANALYZING` runs higher than the V1 target of 20 % and `WAITING` lower. That is the
measured outcome of the dwell distributions and it reads correctly — a trader at a desk
is mostly looking at charts — so the targets were wrong rather than the behaviour.

### The transition rule

**`EXECUTING` is reachable only from `ANALYZING` or `MARKET_REACTION`.** Nobody types an
order out of idle, and that single constraint does more for believability than any amount
of motion polish: visible action always has visible motivation in front of it.

The reaction edge belongs for exactly that reason — he saw something, looked at it, acted.
V1's own motion library stated the rule as "only from ANALYZING" *and* drew the reaction
edge; both cannot hold, and the purpose of the rule settles it.
`EXECUTING_PREDECESSORS` is the machine-readable form, asserted by the simulator.

A reaction resolves to `ANALYZING` 6:3:1 against idle and executing. Resolving straight
to idle would imply he saw something and dismissed it; mostly he looks into it.

## 6. Scoring

```
score(action) = weight
              × band_bias[band]                  # None = unavailable, not unlikely
              × music_factor                     # 1.0 for 68 of 71 actions
              × anti_repeat_penalty               # graded, never a veto
              × fatigue_gate                      # FATIGUE family only
              × posture_gate[tier]                # POSTURE family only (§15)
              × energy × focus                    # WORK family only (§15b)
              × symbol_switch_bias                # 30 s after a routing change
```

Then one hard constraint — no immediate repeat — and weighted sampling over the
survivors, reusing `director/selection.py`: the same `WeightedSelector` / `Candidate` /
`Constraint` machinery the music director uses for genre choice. `random.choice` appears
nowhere.

### The do-nothing candidate

Not an implementation detail. A scheduler that always selects something produces a
fidgeting man, and this character is disciplined and still. So stillness is an explicit
candidate whose weight **rises with recent activity**:

```
idle_weight = 0.35 × (1 + 2.6 × activity) × (0.5 + 1.5 × band.idle_share)
```

That negative feedback is what makes work arrive in bouts rather than at a constant drip.

## 7. Interaction locks

Ten resources. The brief's rules become arithmetic rather than things to remember:
"while the mug is held, typing must not start" is the observation that `typing_short`
claims `LEFT_HAND` and `coffee_drink` already holds it.

`BOTH_HANDS` **expands** to `LEFT_HAND` + `RIGHT_HAND` on claim. The naive version treats
it as its own resource and lets a one-handed action start during a two-handed one.

Two rules the lock set alone does not express, both enforced in the director:

- **One object interaction at a time.** `COFFEE_DRINK` needs {coffee, left_hand} and a
  pen chain holds {pen, right_hand}, so both can physically proceed — and the soak duly
  started a coffee chain mid-note. Two hands could do it; a man holding a pen in one hand
  and a mug in the other is not a work moment anyone would recognise.
- **An object lock implies `NEVER` interruptible.** Rejected at the contract boundary, in
  `ActionSpecV1`'s validator: an interruptible grip is the floating-mug bug waiting.

## 8. Action chains

Three transactional interactions. The brief: *"Do not fake them as independent random
actions."* Six independent actions that happen to occur in the right order will
eventually occur in the wrong one, and the wrong one is a mug lifted to a mouth already
holding a pen.

```
COFFEE_DRINK     reach_cup → pick_cup → sip → hold_cup → place_cup → breath_after_sip
                 commits at step 1 (the grip)        holds {coffee, left_hand}

NOTE_WRITE       note_gaze_down → reach_pen → acquire_pen → note_write_short
                 → return_pen → note_gaze_up
                 commits at step 2 (the pinch)       holds {head, pen, right_hand}

NOTE_WRITE_LONG  as NOTE_WRITE, longer writing beat
```

**`commit_step` is the heart of it.** Before it the chain may still be abandoned —
reaching toward the mug and changing your mind is a real thing. From it onward nothing
stops the chain: not the scheduler, not a reaction, not an operator.

**A chain reserves the union of every step's locks at step 0**, held to completion. Per
step would leave gaps between steps for another action to grab the hand; claiming only
step 0's locks let `NOTE_WRITE` start with the right hand on the mouse, and it raised
`LockConflict` mid-chain inside two simulated hours. `ActionChain.required_locks()`.

A reaction arriving mid-chain is **deferred, not cancelled** — capped at 1.5 s, then
dropped, because a reaction three seconds after its cause reads as a reaction to nothing.
The brief says coffee cannot interrupt a reaction; the converse is equally required.

## 9. Cooldowns

**Sampled, never constant.** Enforced structurally: `CooldownTable` has no method that
accepts a fixed interval. Every arm draws from the spec's range, so no two gaps between
the same action are ever equal and an action cannot acquire a visible period even if
someone later tunes its range badly.

Group cooldowns are the second half, shared across an `anti_repeat_key`:

| Group | Range | Source |
|---|---|---|
`headphones` | 240–1 200 s | the brief's 4–20 minutes |
`coffee` | 480–1 800 s | the brief's 8–30 minutes |
`posture` | 300–1 500 s | the brief's 5–25 minutes |
`note` | 120–900 s | the brief's 2–15 minutes |
`fatigue` | 300–1 500 s | — |
`lean` | 60–180 s | — |
`smirk` | 900–3 600 s | restraint |

Two things the soak taught, both now asserted:

- **A group cooldown below its members' own floors never binds.** The headphone group was
  a fixed 150 s against member floors of 240 s, so the rule was inert and a quiet
  half-hour produced 18 adjustments per hour. `test_group_cooldowns_bind_over_member_cooldowns`.
- **A frequent member starves a rare one.** `shoulder_shift` (35–130 s) shared the key
  `posture` with `posture_reset` (5–25 min); the major reset fired **zero** times across
  two simulated hours. `test_frequent_and_rare_actions_do_not_share_a_key`.

## 10. Blinking

Its own generator, outside the action scheduler, because the brief asks for three things
a scheduler cannot express: no exact timing, no blink while the lids are already closing,
and no double blink at a predictable cadence.

- **Log-normal intervals**, median 4.1 s, σ 0.55, bounded [2.0, 8.0]. Human blink
  intervals cluster rather than spreading uniformly, and a uniform draw produces a
  visibly metronomic eye.
- **Resampled, not clamped.** Clamping put 88 of 400 draws on exactly 2.0 s — a spike at
  a single value is a quasi-fixed period, which is the one thing blink timing must not
  have.
- **Concentration suppresses.** ×1.25 during `ANALYZING`, ×1.35 during a reaction. People
  hold their eyes open while reading closely, and that suppression reads as focus with no
  other cue.
- **Variants cannot cluster.** Independent 8 % and 4 % draws let two double blinks land
  two seconds apart; minimum gaps of 25 s and 45 s now apply.
- **Suppressed mid-close**, and **forced across a saccade over 12°**. Real eyes blink
  across large gaze shifts; omitting it is one of the clearest tells of synthetic
  animation.

Measured over 8 simulated hours: **19.5 blinks per minute, mean interval 4.7 s**. The
human range is 15–20.

## 11. Gaze

Its own clock too. Routing every eye movement through cooldowns would make gaze discrete,
and gaze is the one thing that must be continuous.

Resolved through the frozen blockout, so "look at the left monitor" means the actual left
monitor in all seven cameras. Hand-tuned per-camera angles are precisely how eyes end up
looking *through* monitors.

**`MONITOR_LEFT` is his left** — he faces −Y, so his left is east, `MON_3`. Getting this
backwards is invisible as a bug: it looks like bad animation.

| Target | Yaw | Pitch | Participation | Share |
|---|---|---|---|---|
`MONITOR_MAIN` | 0° | −14.2° | eyes | 42.0 % |
`MONITOR_RIGHT` (MON_2) | −47.1° | −9.8° | shoulders | 16.0 % |
`MONITOR_LEFT` (MON_3) | +47.1° | −9.8° | shoulders | 14.0 % |
`MONITOR_LOWER` (MON_4) | +64.5° | −6.2° | shoulders | 6.0 % |
`MONITOR_FAR_RIGHT` (MON_5) | −64.5° | −6.2° | shoulders | 3.0 % |
`PANEL_UPPER_RIGHT` (W1) | −70.7° | +6.9° | torso | 4.5 % |
`PANEL_UPPER_LEFT` (W2) | −82.7° | +7.3° | torso | 3.5 % |
`NOTEBOOK` | −75.2° | −36.6° | torso | 4.5 % |
`MOUSE` | −47.5° | −39.4° | shoulders | 2.5 % |
`COFFEE` | +55.8° | −33.6° | shoulders | 1.2 % |
`MIDDLE_DISTANCE` | 0° | −0.5° | eyes | 1.0 % |
`KEYBOARD` | 0° | −59.3° | head_pitch_strong | 0.8 % |
`CAMERA` | dynamic | — | eyes | **≤ 0.6 %** |
`WINDOW` | −161.6° | +9.1° | chair_swivel | 0.4 % |

**89.0 % on screens**, which is what a working trader's eyes do and the number that makes
the remaining 11 % read correctly. Measured 82–92 % across bands: quiet biases toward the
notebook and mug, alert locks onto screens.

### Mechanics

- **Eyes lead, head follows.** Eyes arrive in 40–90 ms; the head takes 25–60 % of the
  remaining angle. Simultaneous onset is the clearest signal of a cheap rig.
- **Participation scales with angle**, and each target declares the tier its geometry
  requires — see `GEOMETRY_FREEZE.md` CR-001 for why a flat limit was the wrong model.
- **Nothing snaps.** 180 ms floor to any target, including inside a reaction.
- **Camera glance rationed** to once per 12 minutes, 0.5–1.2 s, only from `CAM_4` or
  `CAM_7`, only in `IDLE_FOCUS` or `WAITING`. Hard-gated, not weighted: an operator
  slider must not be able to make him stare at the viewer. Measured 0.15 % of shifts.

## 12. Anti-repetition

The most important system, and the one the brief is most specific about.

### Windows

| Record | Depth |
|---|---|
Recent actions | last 30, with the last 3 and last 10 read separately |
Per-key last use | every `anti_repeat_key` |
Rolling share | 60 minutes, by count |
Trigram memory | last 48 ordered triples |

### Penalty, not veto

The brief: *"Create a repetition penalty instead of only hard exclusions. This allows
natural actions to recur eventually."* A veto-only system starves — after twenty minutes
everything plausible has been used.

```
penalty = recency(own)        0.15 within 3 · 0.45 within 10 · 0.82 within 30
        × key_recency         0.22 if the family appeared within 3
        × trigram             0.12 if this completes a remembered triple
        × category_taper      → 0 as the family approaches its ceiling
        × action_taper        → 0 as the action approaches its own cap
```

One hard exclusion only: the same action twice in a row, expressed as a `Constraint` in
the director where it belongs.

**Share ceilings require a 20-entry sample.** Without that a one-entry window reports
every category at 100 % and the ceiling zeroes the penalty outright — turning the graded
rule back into the veto it was meant to replace.

### Why trigrams matter most

Rules against back-to-back repeats are satisfied by almost any scheduler and are *not*
what makes a stream feel looped. What viewers notice is the n-gram: the same three actions
in the same order, twenty minutes apart. Tracking ordered triples is cheap and is the
difference between passing a two-minute review and passing a two-hour soak.

**Measured over 24 simulated hours:** the most common triple takes **0.42 %** of all
triples, across **12 643 distinct** triples. Longest identical run 1. Back-to-back
repeats 0.

## 13. Market reactivity

The brief: *"Do NOT directly select animations based on market labels."* So the market's
entire influence is one scalar plus one band.

### `behavior_energy`

```
energy = 0.46 × market_energy/100       (0.5 when absent — not 0.0)
       + 0.18 × tanh(velocity/8)
       + 0.26 × band.rank/5
       + 0.10 × music_energy
       − 0.22 × recent_activity
       clamped to [0.08, 0.92]
```

The 0.92 ceiling is reserved headroom, deliberately unreachable: he is experienced, and a
man at 100 % is a man with nothing held back.

**Absent market energy contributes the neutral 0.5, not 0.0.** A missing feed must not
read as a calm market — the brief forbids fake zeros, and an energy of zero is exactly
that mistake with a behavioural consequence.

### Bands

| Band | Rate | Blend | Reaction threshold | Idle share | Gaze dwell |
|---|---|---|---|---|---|
B0 `DORMANT` | ×0.55 | ×1.30 | **1.01 — unreachable** | 70 % | ×1.45 |
B1 `QUIET` | ×0.75 | ×1.15 | 0.80 | 58 % | ×1.30 |
B2 `STEADY` | ×1.00 | ×1.00 | 0.65 | 45 % | ×1.00 |
B3 `FOCUSED` | ×1.20 | ×0.90 | 0.50 | 34 % | ×0.85 |
B4 `ALERT` | ×1.45 | ×0.80 | 0.38 | 24 % | ×0.70 |
B5 `PEAK` | ×1.60 | ×0.72 | 0.30 | 20 % | ×0.62 |

**The rate span is only 2.9×, deliberately.** The naive mapping is "more volatility, more
movements per minute", and taken far it produces a twitching man. Most of the visible
difference comes from *which* actions win — B5 biases mouse work, monitor comparison and
leaning forward while suppressing sustained reading and coffee — and from blends 28 %
shorter, which makes every motion land more sharply.

Energy modulates ±18 % inside the band, and **that span is chosen so adjacent bands
overlap**: a classification flip alone cannot change the action rate unless the energy
moved too. At ±15 % the B0→B1 boundary had a 0.8 % discontinuity.

B0's reaction threshold is above 1.0, which is how reactions are disabled without a
special case: salience is a unit value and can never reach it.

**Measured deliberate actions per hour:** 498 (B0) → 773 → 1 048 → 1 286 → 1 578 → 1 768
(B5). Monotonic, 3.5× span, and `test_market_energy_measurably_changes_action_density`
asserts the monotonicity.

### Reactions

Salience is computed in the **bridge**, not here — it is a market judgement, and putting
it downstream would be the market logic leaking into animation. The director consumes one
number and a threshold.

Composed from an opener, an optional action and an optional resolution; total 2–5.5 s.

Gates: 25 s refractory · 10 per rolling hour · disabled in B0 · suppressed below 0.35
confidence · deferred behind a committed chain, dropped after 1.5 s.

**`subtle_smirk` is removed on an adverse move as a filter, not a weight.** A weight can
always be overcome by enough other multipliers, and the character smirking at a loss
would be the single most character-breaking frame the system could produce.

Forbidden outright: celebration, fist pump, raised arms, standing, head in hands, desk
slam, laughter, shock, any gesture to camera.

## 14. Music reactivity

Much weaker than the market, **by construction rather than by tuning**: music can reach
three actions and one rhythm policy, and nothing else.
`test_music_reaches_only_the_music_actions` checks all 71.

| Target | Effect | Ceiling |
|---|---|---|
`micro_head_nod` weight | 0.2 → 1.4 with energy | **±1.1° at any energy** |
`finger_rhythm` weight | 0.3 → 1.2 | 1.5 mm travel |
`small_shoulder_rhythm` | 0.0 → 0.7 | 2 mm vertical |
Beat subdivision | — | 2 above 160 BPM **and** below 85 |
Combined music share | — | **under 12 %** |

The ceilings are clamps applied after every multiplier, so no reactivity setting can
exceed them — `test_music_nod_ceiling_cannot_be_raised` tries 0.0, 1.0, 1.5 and 99.0.

Above 160 BPM a per-beat nod is physically wrong for a seated man and reads as a glitch;
below 85 the nod belongs on beats 1 and 3. Both end on every second beat, for opposite
reasons.

Target: a viewer who mutes the stream notices something subtle has gone, without having
been able to name it while it played.

## 15. Body maintenance

Seven actions in their own `POSTURE` category. **A family, not one animation.**

| Tier | Members | Gate | Measured |
|---|---|---|---|
**Major** | `chair_reposition`, `spine_straighten`, `posture_lean_back`, `shoulder_roll`, `neck_reset` | 420–1 020 s, sampled | **3.1 / h** |
**Minor** | `elbow_reposition`, `hand_rest_reset` | 210–540 s, sampled | 5.9 / h |

### Why the V6 version fired zero times

The original was a single `posture_reset` action, and it never happened. Raising its
weight would not have helped, because it was almost never *eligible* — three
independent causes, all structural:

1. It lived only in `CharacterState.POSTURE_RESET`, a state entered rarely and exited in
   3–10 s, often before the next action consideration fired.
2. It sat in the `FATIGUE` category, behind a fatigue ramp a quiet market never reached.
3. It shared the anti-repeat key `posture` with `shoulder_shift`, which fires every
   35–130 s and so armed the 5–25 minute group cooldown they shared.

### The gate

Eligibility is the brief's three conditions and nothing else:

```
time since the last major posture change exceeds a SAMPLED threshold
AND no object-interaction chain is active          (chain slot and lock table both)
AND no market reaction is active                   (state, deferred queue, in-flight)
```

Sampled, not fixed: a constant interval is a visible period, and over eight hours a
viewer would learn when he is about to shift in his chair.

The gate returns a **multiplier per tier**, not a boolean, fading in over the last third
of the interval. A hard switch clusters resets immediately after each gate opens.

Admissible from `IDLE_FOCUS`, `WAITING`, `ANALYZING` and `POSTURE_RESET`. A man shifts in
his chair while reading a chart, not only during a dedicated interlude.

A major reset is also one of the recovery influences in §15b.

## 15b. The long-run work rhythm

Four phases, cyclic. `tradefix_radio/visual/rhythm.py`.

```
FOCUS_BUILD  ──▶  DEEP_WORK  ──▶  FATIGUE_RISE  ──▶  RECOVERY  ──┐
     ▲                                                            │
     └────────────────────────────────────────────────────────────┘
```

### Why the V6 version failed in both directions

It modelled fatigue as a quantity that only goes up, with step decrements for coffee.
That single choice produced two failures that pulled opposite ways, so no constant fixed
both:

| Coffee decrement | Outcome |
|---|---|
0.25 | 2.17 coffees/h × 0.25 = 13.0 of recovery against 3.43 of accumulation per day. Phase never exceeded 0.16; the fatigue family fired 24 times in 24 hours |
0.035 | Pinned at the ceiling by about hour sixteen. A character permanently on the late shift |

### Four properties the redesign has

**Phases set rates, never values.** Nothing assigns `fatigue_level`; each phase
contributes a per-second derivative integrated against elapsed time. Transitions are
smooth by construction — there is no jump to smooth. Measured: no 10-second step exceeds
0.01.

**Thresholds are sampled per cycle.** `DEEP_WORK` exits on fatigue ∈ [0.45, 0.65],
`FATIGUE_RISE` on [0.74, 0.90], `RECOVERY` on [0.12, 0.30]. Combined with a
workload-dependent rate, measured cycle lengths across a day were **4.07, 2.91, 3.30,
3.72, 3.61, 3.74 hours** — a 1.16-hour spread. A fixed-period oscillation is as
mechanical as a pinned ceiling, and min/max/mean cannot tell them apart, which is why
the test checks the spread.

**Recovery influences change the rate, not the level.** Coffee does not subtract from
fatigue; it raises `recovery_rate` for 900 s, which steepens `RECOVERY` and damps the
accumulation phases. The brief's "gradual decay, no instant reset" falls out of that
rather than being enforced on top. A sustained strong signal can also trigger `RECOVERY`
early from `FATIGUE_RISE`, so the influences matter rather than only mattering once
recovery has begun.

**The ceiling is unreachable.** `FATIGUE_RISE` exits below the 0.95 clamp, which exists
as a guard against a pathological workload rather than as a destination.

### Variables, all bounded

| Variable | Range | Driven by |
|---|---|---|
`fatigue_level` | [0.02, 0.95] | phase rate × workload × damping |
`focus_level` | [0.10, 1.00] | rises in `FOCUS_BUILD`/`RECOVERY`, decays in `FATIGUE_RISE` |
`recovery_rate` | [0.0, 1.0] | baseline 0.30 + quiet band + low workload + influences |
`workload` | [0.0, 1.0] | the director's rolling activity, smoothed |
`time_in_phase` | ≥ 0 | with a per-phase floor, so a workload spike cannot thrash it |

### Recovery influences

| Influence | Strength | Decays over |
|---|---|---|
`coffee` | +0.30 | 900 s |
`session_transition` | +0.22 | 1 800 s |
`posture_reset` | +0.18 | 600 s |
`long_gaze_hold` | +0.10 | 240 s |
quiet or dormant band | +0.22 | while it holds |
low workload | up to +0.24 | while it holds |

Linear decay rather than exponential: indistinguishable at this timescale, and far easier
to reason about when reading a soak report.

### Measured over 24 hours

```
fatigue   min 0.080   max 0.849   mean 0.472
cycles    6 completed, 13 turning points
lengths   4.07, 2.91, 3.30, 3.72, 3.61, 3.74 hours
near floor 2.6 %      near ceiling 1.5 %
pinned    ceiling=False   floor=False
phases    deep_work 35 % · recovery 43 % · fatigue_rise 16 % · focus_build 5 %
```

And the behavioural consequence: the fatigue family now fires **4.3 / hour** against V6's
1.0 per day, while still holding under its 4 % share ceiling.

### Why focus is tracked separately

`work_intensity_scale()` multiplies WORK-family weight by 0.82 + 0.30 × focus. That is
why his output varies over hours for reasons the market did not cause: `DEEP_WORK` at high
focus produces more work actions than `RECOVERY` does at the same market band.

## 16. Personality, as numbers

The brief asks for experienced, disciplined, calm, intense, self-controlled. That is
expressed as ceilings rather than as intent:

| Constraint | Value |
|---|---|
Largest emotional response | `subtle_concern` — inner brows up, 5 % wider eyes |
`subtle_smirk` share cap | 0.4 % of a rolling hour |
`subtle_smirk` on an adverse move | **forbidden, filtered** |
Reactions per hour | ≤ 10 |
Reaction duration | 2.0–5.5 s |
Head nod amplitude | ±1.1° |
Fatigue family share | < 4 % |
Camera gaze | ≤ 0.6 % |
Action rate span, dormant→peak | 2.9× |

## 17. Running it

```
tradefix visual simulate --scenario breakout --duration 2h --spike-every 240
tradefix visual simulate --scenario range --duration 24h --tick 0.25 --actions 20
tradefix visual timeline --scenario trend --duration 5m --lines 60
tradefix visual doctor
```

Eleven scenarios: `quiet`, `range`, `trend`, `breakout`, `extreme_volatility`,
`low_bpm`, `high_bpm`, `btcusd`, `no_active_market`, `feed_down`.

`--tick 0.25` samples at 4 Hz instead of 20. The director decides on 1.4–4.2 s intervals
so 4 Hz samples them amply at a fifth of the cost; it changes reaction *latency*, which is
why the latency assertions run at full rate.

The timeline is the brief's debug representation, and it is useful precisely because it is
readable before any artwork exists — a reviewer can judge whether the *behaviour* feels
human from the text alone:

```
22:14:02  micro        blink
22:14:06  work         monitor_right_glance  gaze->monitor_right
22:14:09  work         mouse_move  gaze->monitor_main
22:14:12  work         typing_short  gaze->monitor_main
22:14:20  micro        shoulder_shift
22:14:33  work         note_gaze_down  [NOTE_WRITE 0]  gaze->notebook
22:14:35  work         reach_pen  [NOTE_WRITE 1]  gaze->notebook
```

## 18. What the director must never do

- Select the same action twice in a row.
- Fire an action whose locks are held.
- Fire an action inside its sampled cooldown.
- Interrupt a committed chain.
- Start an object chain while another object is held.
- Enter `EXECUTING` from anything but `ANALYZING` or `MARKET_REACTION`.
- Start an object interaction while another object is in hand — checked on **every** start
  path, not just the scheduler's. A reaction composing `quick_note` took the pen while the
  mug was held, and the scheduler-only check did not see it.
- Run body maintenance inside an object chain or a market reaction.
- Let fatigue pin at the ceiling or the floor, or cycle on a fixed period.
- React with a stale feed, in B0, or below 0.35 confidence.
- Smirk on an adverse move.
- Exceed any share ceiling or any music clamp.
- Reset anything on a symbol change.
- Reference an anchor or gaze target that is not in the frozen blockout.
