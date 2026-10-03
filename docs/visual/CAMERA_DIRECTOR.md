# Camera Director (V8)

Which of the seven frozen compositions is live, and why.

**Code:** `tradefix_radio/visual/camera_director.py` · metadata in `camera.py`
**Tests:** `tests/unit/test_visual_camera_director.py` — 49 tests
**Run it:** `tradefix visual simulate --scenario range --duration 8h --tick 0.25`

V8 chooses between the V1 transforms. **It does not invent camera geometry** — the seven
positions, targets and lenses are frozen (`GEOMETRY_FREEZE.md`), and a test asserts the
director knows about exactly seven.

---

## 1. The shape of a decision

```
a cut requires:   a MOTIVATION   +   a satisfied HOLD   +   NO VETO
                  (scored)           (gate)                (absolute)
```

All three, in that order, and the order is the design:

- **A cut with no motivation does not happen.** A timer alone is the predictable rotation
  the brief rules out — "do not cut because a timer equals exactly 60 seconds."
- **The hold is a gate, not a trigger.** Expiry is one motivation among several; the
  others can cut early, within limits.
- **Vetoes are checked last and absolutely.** A weight can always be overcome by enough
  other multipliers, so "never cut during an object acquisition" is a filter.

Output is a `CameraDecision` carrying the camera, the transition, the motivation, the
scoring factors **and the vetoes** — tuning a camera system blind is how a stream quietly
acquires a rotation.

## 2. The seven cameras and their families

Families exist so anti-repetition can act on *purpose* rather than on identity.
`CAM_1 → CAM_7 → CAM_1` is three hero shots running even though no camera repeated, and
that reads as indecision.

| Camera | Purpose | Family | Hour cap | Push-in |
|---|---|---|---|---|
`CAM_1` | Hero front — default broadcast view | `HERO` | 25 % | yes |
`CAM_7` | Three-quarter cinematic — **primary** | `HERO` | 35 % | yes |
`CAM_2` | Side profile — depth, focused mood | `PROFILE` | 15 % | no |
`CAM_3` | Over-shoulder — chart interaction | `WORK` | 20 % | no |
`CAM_6` | Hands and desk — typing, mouse, notebook, coffee | `WORK` | 6 % | **no** |
`CAM_4` | Face close — expression, headphones | `INTIMATE` | 8 % | yes |
`CAM_5` | Wide office — atmosphere, long holds | `ATMOSPHERE` | 18 % | no |

## 3. Hold times

Sampled **log-normally** about the band's median, then clamped to both the band's range
and the live camera's own declared minimum and maximum.

| Band | min | median | max |
|---|---|---|---|
`B0_DORMANT` | 75 s | 210 s | 420 s |
`B1_QUIET` | 70 s | 180 s | 360 s |
`B2_STEADY` | 55 s | 135 s | 290 s |
`B3_FOCUSED` | 48 s | 110 s | 240 s |
`B4_ALERT` | 40 s | 80 s | 170 s |
`B5_PEAK` | 35 s | 62 s | 130 s |

**Log-normal rather than uniform**, because a uniform draw over the same range produces
far too many mid-length shots — which is exactly what a rotation looks like. Log-normal
gives mostly-long holds with an occasional short one.

**Clamped to the camera, not only the band.** The first version used the band alone, and
the soak caught the consequence: `CAM_5` declares a 70-second minimum but could be handed
a 62-second hold drawn from the `B5_PEAK` range, then cut below its own floor on ordinary
expiry — with no veto to catch it, because expiry is not an early cut.

**`MINIMUM_HOLD_FLOOR` is 30 seconds, absolute.** No band, no motivation and no operator
slider can go below it. The energy multiplier scales holds and cannot breach the floor;
a test tries 0.1, 0.5, 1.0, 1.6 and 99.0.

## 4. Cut motivations

| Motivation | Weight | Earliest | Probability per consideration |
|---|---|---|---|
`HOLD_EXPIRED` | 1.0 | 100 % | 1.0 |
`INTERACTION_VISIBLE` | 1.6 | 60 % | 0.004 |
`MARKET_SHIFT` | 1.3 | 50 % | 0.010 |
`DIVERSITY` | 0.8 | 85 % | 0.006 |
`REACTION_RESOLVED` | 0.9 | 55 % | 0.004 |
`TRACK_TRANSITION` | 0.45 | 65 % | 0.002 |
`OPERATOR` | 10.0 | 0 % | 1.0 |

The probabilities are **per consideration**, and the director considers on every tick, so
0.004 is still a cut within a minute of becoming eligible. They were far higher in the
first draft — 0.25 to 0.55 — which produced **36 cuts an hour with 37 % of them from
`INTERACTION_VISIBLE`**. The brief asks for mostly-long shots and says not to force a cut
merely because an affinity exists, so they are rationed hard.

### Event motivations need a window, not an instant

`MARKET_SHIFT` and `TRACK_TRANSITION` stay live for **90 seconds** after their event.

Without that they were dead. A band change evaluated only on the tick it occurred had one
1 % chance, and the soak measured **zero early cuts across twenty-four runs**. Over a
90-second window at 20 Hz it fires reliably while never being a guaranteed cut — a
licence, not a trigger.

That distinction matters most for music. A guaranteed cut on every song transition would
teach viewers to read a cut as a track change, and a camera that cuts on every drop turns
the stream into a music video. `TRACK_TRANSITION` is the lowest-weighted and
lowest-probability motivation in the table, and a test asserts fewer than ten of thirty
transitions cut immediately.

### Diversity must not fire at startup

With `_last_used` empty every camera reads as starved, and `DIVERSITY` produced **a fifth
of all cuts in the first two hours** — a startup artefact, not a narrowing stream. All
seven entries are now seeded on the first tick.

## 5. Safety — the vetoes

| Veto | Protects against |
|---|---|
`BELOW_FLOOR` | cutting faster than every 30 s |
`DOUBLE_CUT` | two cuts inside 15 s |
`MID_BLEND` | two blends in flight on one joint — the visible pop |
`OBJECT_ACQUISITION` | a hand arriving at nothing; a mug changing hands across an edit |
`COMMITTED_CHAIN_INVISIBLE` | cutting away from a committed chain to a camera that cannot show it |
`REACTION_LOCKOUT` | a cut inside 2.5 s of a reaction starting |
`CAM6_NO_INTERACTION` | the hands shot on an empty desk |
`BELOW_SAMPLED_HOLD` | an early cut before the camera's own declared minimum |

`CameraSoakReport.unsafe_cuts` **must be zero**, and the simulator re-derives each
condition from the state the cut was made in rather than asking the director whether it
obeyed itself.

### `DOUBLE_CUT` is currently unreachable, and kept

Both it and the floor measure from the last cut, so an elapsed time past 30 s already
exceeds the 15 s window. The check can never be the veto that fires. It stays as a guard
against a future tuning pass lowering the floor, and
`test_the_floor_subsumes_the_double_cut_window` asserts the relationship rather than
pretending the veto is reachable.

### The blend veto needed a timing view

`CharacterActionV1` carries a wall-clock `started_at` for the renderer, not a monotonic
one, so it cannot answer "am I inside my blend window". The first implementation inferred
it from `blend_in_ms` alone and therefore vetoed every cut while any blending action ran —
which is nearly always, and would have frozen the camera. `ActionView` now carries the
monotonic start and end.

### `CAM_6` is gated, not weighted

The acceptance criterion is that the hands shot appears *only when close interaction is
visible*. A weight, however small, eventually fires on an empty desk — so the camera is
simply unavailable unless an object chain is running or an action holds a desk anchor.
Same shape as the camera-gaze gate in the behaviour director, for the same reason.

## 6. Affinity

Two layers, from the catalogue and the camera metadata declared in V6.

**Action affinity** — `camera_affinity` on each action spec. A tiebreaker at the moment
of the cut: ×2.2 if the candidate can show the running action, ×0.55 otherwise.

| Action | Cameras |
|---|---|
typing | `CAM_6`, and the hero shots by state |
coffee chain | `CAM_4`, `CAM_6` |
chart inspect | `CAM_3`, `CAM_4` |
market reaction | `CAM_4`, `CAM_7` |
wide idle | `CAM_5` |

**Behaviour affinity** — per character state, raised to the power 2.2 so it competes with
the recency, family and share penalties rather than being diluted by them.

Measured at matched durations and seed, state alignment:

| power | 2 h | 8 h |
|---|---|---|
1.0 | 26.5 % | 28.1 % |
1.6 | 26.5 % | 30.4 % |
**2.2** | **29.4 %** | **30.7 %** |

A real but small effect, and the smallness is structural rather than a tuning shortfall:
a ~115-second hold spans three or four character states at a mean dwell near 35 seconds,
so no cut-time choice can keep the camera aligned for a whole hold. Around 30 % is close
to the ceiling.

**What actually steers framing where it matters is not the multiplier.** It is the `CAM_6`
gate and `CAM_4`'s reaction affinity — those cover the moments a viewer would notice. The
rest of the time several cameras are equally correct, which is why the figure looks low
and the result does not.

The same reasoning applies to `action_visibility`, which measures ~13 %. A long hold
cannot follow a quarter-second mouse click, and a camera that tried would be the
music-video cutting the brief rules out. It is reported because it was asked for, and
judged against `state_alignment`.

## 7. Anti-repetition

| Tracked | Rule |
|---|---|
Last camera | hard constraint: never return to the camera just left |
Last 3 cameras | family penalty — ×0.45, ×0.18, ×0.08 for one, two, three family hits |
Time since used | ×0.12 under 2 min, ×0.5 under 7 min, ×1.6 past 30 min, ×1.4 if never |
Rolling-hour share | tapers to 0 at the camera's cap |

Measured over 24 hours: **zero A-B-A alternations**, the most common three-sequence takes
**2.3 %** of all three-sequences, and all seven cameras are used.

## 8. Transitions

| Transition | Duration | Share (measured) |
|---|---|---|
hard cut | 0 ms | ~80 % |
short crossfade | 420 ms | ~18 % |
slow push continuation | 1 200 ms | ~2 % |

Hard cut dominates because it is what a real multi-camera production uses and the only
transition that is invisible when it is right. The crossfade is reserved for moving into
or out of the wide shot, where a soft transition reads as intentional rather than as an
effect.

The push continuation is `CAM_1 → CAM_4` only — the hero front tightening into the
close-up as one move — capped at **2 per hour** because its entire value is being rare.

**No wipes, no zoom-spin, no streamer effects.** `test_only_three_transitions_exist`
asserts the whole vocabulary.

## 9. Market and music

**Market** sets the hold distribution (§3) and the per-band camera affinity.

| Band | Behaviour |
|---|---|
`QUIET` | longer holds, `CAM_5` ×1.4 and `CAM_2` ×1.3, `CAM_3` ×0.7 |
`BREAKOUT` | shorter holds, `CAM_3` ×1.9, `CAM_4` ×1.5, `CAM_5` ×0.5 |
`EXTREME` | shortest holds, `CAM_3` ×2.1 |

Measured: quiet holds the wide shot more than peak does; peak holds the over-shoulder more
than quiet does; and even at peak the median hold stays above 45 s and cuts stay under
60/h. Restrained — he is a professional trader, not an esports montage.

**Music** is weaker, by construction. It reaches two things only: a song transition opens
a cut *licence* at the lowest probability in the table, and track energy nudges family
weight by at most ±25 % (atmosphere up when energy is low, hero and work up when it is
high). Nothing else.

## 10. Operator control

`request(camera_id)` **queues behind the vetoes** rather than bypassing them. An override
that bypassed them would let one click produce exactly the artefact the vetoes exist to
prevent, live, with no undo. Queuing costs a few seconds and cannot produce a broken
frame.

`CameraState.auto = False` stops the director entirely and pins the current shot. The
renderer's breathing drift continues — a pinned camera must not become a still.

## 11. Soak results

`tradefix visual simulate --scenario range --duration 24h --tick 0.25 --spike-every 420`

| | 2 h | 8 h | 24 h |
|---|---|---|---|
cuts / hour | 33.0 | 30.6 | 29.4 |
mean hold | 108 s | 117 s | 122 s |
median hold | 96 s | 104 s | 112 s |
min hold | 42 s | 42 s | 42 s |
max hold | 251 s | 290 s | 290 s |
state alignment | 34.3 % | 32.6 % | 32.9 % |
cameras used | 7 / 7 | 7 / 7 | 7 / 7 |
A-B-A alternations | 0 | 0 | 0 |
top 3-sequence share | 3.1 % | 2.9 % | 2.3 % |
**unsafe cuts** | **0** | **0** | **0** |
**CAM_6 without interaction** | **0** | **0** | **0** |

### Acceptance criteria

| Criterion | Result |
|---|---|
zero unsafe cuts | **✓** 0 across every soak and every scenario |
no short deterministic rotation | **✓** 0 alternations, top 3-sequence 2.3 % |
all cameras used eventually | **✓** 7 / 7 |
`CAM_1` remains primary | **✓** the hero family holds ~30 %, `CAM_7` ~21 % |
`CAM_5` provides breathing room | **✓** ~16 % |
`CAM_6` only when interaction is visible | **✓** gated; 0 violations; ~5 % share |

## 12. What the camera director must never do

- Cut faster than once per 30 seconds, under any condition.
- Cut twice inside 15 seconds.
- Cut inside a blend window.
- Cut during an object acquisition or release.
- Cut away from a committed chain to a camera that cannot show it.
- Cut within 2.5 seconds of a reaction starting.
- Cut below the live camera's own declared minimum hold.
- Use `CAM_6` without a visible desk interaction.
- Return to the camera it just left.
- Cut on a beat, or guarantee a cut on any event.
- Let an operator request bypass a veto.
- Invent camera geometry.
