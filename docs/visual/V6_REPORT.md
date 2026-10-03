# Visual V6 / V7 — Behaviour Director and State Bridge

**Date:** 2026-10-03
**Scope:** V1 geometry freeze · V6 Behaviour Director · V7 State Bridge · art asset
contract · renderer protocol · simulation harness
**Status:** complete and passing. **No artwork was required and none was fabricated.**

---

## 1. What was built

| Deliverable | Where | Lines |
|---|---|---|
Geometry freeze + change procedure | `docs/visual/GEOMETRY_FREEZE.md` | 190 |
Freeze regression suite | `tests/unit/test_visual_geometry_freeze.py` | 17 tests |
Contracts | `tradefix_radio/visual/contracts.py` | 680 |
Frozen-blockout loader | `tradefix_radio/visual/geometry.py` | 390 |
Action catalogue + chains | `tradefix_radio/visual/catalog.py` | 560 |
Locks, cooldowns, anti-repetition | `tradefix_radio/visual/scheduler.py` | 560 |
Behaviour energy, music, fatigue | `tradefix_radio/visual/modulation.py` | 390 |
Gaze and blink | `tradefix_radio/visual/gaze.py` | 420 |
Behaviour director | `tradefix_radio/visual/director.py` | 910 |
State bridge + socket client | `tradefix_radio/visual/bridge.py` | 530 |
Camera metadata + state stub | `tradefix_radio/visual/camera.py` | 290 |
Renderer wire protocol | `tradefix_radio/visual/renderer.py` | 330 |
Art asset contract | `tradefix_radio/visual/assets.py` | 540 |
Simulation harness | `tradefix_radio/visual/simulate.py` | 790 |
CLI | `tradefix_radio/cli/visual.py` | 320 |
Generated manifest | `visual/assets/manifest.json` | 38 KB |
Docs | `BEHAVIOR_DIRECTOR.md`, `STATE_BRIDGE.md`, `ASSET_MANIFEST.md`, `GEOMETRY_FREEZE.md` | — |

**66 actions** (51 schedulable) · **3 transactional chains** · **10 interaction locks**
· **8 character states** · **14 gaze targets** · **6 intensity bands** · **11 simulation
scenarios**.

`ruff` clean. `mypy` clean across all 14 visual modules.

## 2. Test results

```
tests/unit/test_visual_geometry_freeze.py    17 passed
tests/unit/test_visual_behavior.py           63 passed   (incl. 1 Hypothesis property)
tests/unit/test_visual_bridge.py             34 passed
tests/endurance/test_visual_soak.py          22 passed + 6 slow passed
```

The 24-hour soak runs in **~45 s** at 4 Hz sampling (345 600 ticks) and **~3 min** at
the director's full 20 Hz.

## 3. The 24-hour soak

`tradefix visual simulate --scenario range --duration 24h --tick 0.25 --spike-every 420`

```
actions            53,567 total (24,322 deliberate, 29,245 blinks)
deliberate rate    1013.4 / h
gaze shifts        35,703
state changes      2,145
distinct actions   65 of 66 catalogued

coffee             2.12 / h          brief: 8-30 min  -> 2.0-7.5    OK
headphones         4.75 / h          brief: 4-20 min  -> 3.0-15.0   OK
posture reset      0.62 / h          brief: 5-25 min  -> 2.4-12.0   LOW, see §5
reactions          8.46 / h          cap 10                         OK
fatigue phase      1.000 at end      saturates ~16 h, see §5
blink interval     4.01 s mean (2.00-7.99 s)   ~19 / min            OK

on screens         86.0 %            target 89 %                    OK
at camera          0.14 %            cap 0.6 %                      OK

top triple         note_gaze_down -> reach_pen -> acquire_pen
                   x106 = 0.44 % of all triples
distinct triples   12,732
longest same-run   1
back-to-back       0
visible loop?      no

INVARIANTS         all six: ok
RESULT             STRUCTURALLY SOUND
```

### Across the bands, 2 hours each

| Scenario | delib/h | work/h | music/h | coffee | hp | react | screens | camera | top-3 | sound |
|---|---|---|---|---|---|---|---|---|---|---|
`feed_down` | 492 | 199 | 0.0 | 2.50 | 5.5 | **0.0** | 79.2 % | 0.24 % | 0.82 % | ✓ |
`no_active_market` | 551 | 203 | 52.5 | 2.50 | 4.5 | **0.0** | 79.2 % | 0.20 % | 0.82 % | ✓ |
`quiet` | 746 | 282 | 66.0 | 2.50 | 5.0 | 8.0 | 82.6 % | 0.21 % | 0.60 % | ✓ |
`range` | 1 017 | 407 | 79.0 | 2.50 | 3.5 | 8.5 | 85.8 % | 0.13 % | 0.44 % | ✓ |
`trend` | 1 234 | 506 | 86.0 | 2.50 | 5.0 | 8.5 | 87.7 % | 0.14 % | 0.36 % | ✓ |
`breakout` | 1 496 | 620 | 92.5 | 2.50 | 4.5 | 8.5 | 90.1 % | 0.07 % | 0.30 % | ✓ |
`extreme_volatility` | 1 708 | 718 | 98.5 | 2.00 | 5.0 | 10.0 | 91.9 % | 0.13 % | 0.29 % | ✓ |
`low_bpm` | 1 003 | 396 | 76.5 | 2.50 | 4.5 | 8.5 | 86.3 % | 0.20 % | 0.45 % | ✓ |
`high_bpm` | 1 022 | 407 | 82.0 | 2.50 | 5.0 | 8.5 | 85.3 % | 0.13 % | 0.44 % | ✓ |
`btcusd` | 1 231 | 503 | 85.5 | 2.50 | 5.5 | 8.5 | 88.5 % | 0.11 % | 0.37 % | ✓ |

Three things worth reading off that table:

**Action density is monotonic in band**, 492 → 1 708 per hour, a 3.5× span.
`test_market_energy_measurably_changes_action_density` asserts the monotonicity rather
than the figures, so a tuning pass cannot silently invert it.

**Gaze tightens onto screens as the market loudens**, 79 % → 92 %. That was not designed
directly; it emerges from the band suppressing expensive turns and the quiet bands
favouring the notebook and mug. It is the right behaviour and it is pleasing that it fell
out rather than being asserted.

**`low_bpm` and `high_bpm` differ in music actions (76.5 vs 82.0) and almost nothing
else** — 1 003 vs 1 022 deliberate actions per hour. That is the "much weaker than the
market" requirement, visible as a measurement.

## 4. Nine defects the soak found

These are the argument for building the director in Python. Every one was found by
running it, not by reading it.

| # | Defect | How it would have shown |
|---|---|---|
1 | **Chains reserved only their first step's locks.** `NOTE_WRITE` opens on `HEAD` and acquires the pen two steps later, so admissibility was checked against `HEAD` alone | `LockConflict` raised mid-chain within 2 simulated hours. A hand already on the mouse when the pen was needed |
2 | **A coffee chain could start mid-note.** The lock union does not catch it: coffee needs {coffee, left_hand}, a pen chain holds {pen, right_hand}, so both proceed | A man holding a pen in one hand and a mug in the other, mid-note. The brief forbids it outright |
3 | **`shoulder_shift` and `posture_reset` shared the anti-repeat key `posture`.** The 35–130 s action armed the 5–25 min group cooldown | `posture_reset` fired **zero** times across two simulated hours |
4 | **The headphone group cooldown never bound.** A fixed 150 s against member floors of 240 s | 18 headphone adjustments per hour against the brief's 4–20 minutes |
5 | **Coffee recovery overwhelmed fatigue accumulation.** 0.25 per coffee × 2.17/h = 13.0 against 3.43 of accumulation per day | Fatigue phase never rose above 0.16; the FATIGUE family fired 24 times in 24 hours. The multi-hour rhythm did not exist |
6 | **Share ceilings applied to a one-entry window.** "WORK is 100 % of the last hour" after a single action | The anti-repetition *penalty* became a *veto* — the opposite of what the brief asked for |
7 | **The blink interval clamp created a spike.** 88 of 400 draws landed on exactly 2.0 s | A spike at one value is a quasi-fixed period, which is precisely what blink timing must not have |
8 | **Blink variants could cluster.** Independent 8 % and 4 % draws on every blink | Two double blinks two seconds apart — a twitch, not a variation |
9 | **`MONITOR_LOWER` and `MONITOR_FAR_LEFT` both resolved to `MON_4`.** Two enum members each drawing their own base share from one physical panel | That screen silently got double its intended gaze share |

Two more were found by the freeze suite and are recorded in `GEOMETRY_FREEZE.md`: CAM_4's
distance off by 4.2 mm, and CAM_6 framing excluding two anchored props.

Every one now has a test. Several of those tests are *general* rather than specific — for
instance `test_frequent_and_rare_actions_do_not_share_a_key` catches the whole class of
defect 3, not just that instance.

## 5. Two numbers that are honestly off target

**`posture_reset` at 0.62/h against the brief's 5–25 minutes (2.4–12/h).** It only fires
inside `POSTURE_RESET`, which is a short-dwell state (3–10 s) entered relatively rarely.
Fixing it properly means either raising the state's entry weight or letting the action
fire outside the state, and both are behaviour changes rather than tuning. **Left as is
and reported** rather than quietly adjusted, because it is below target rather than
broken and the correct fix is a decision, not an edit.

**The fatigue phase saturates at 1.000 after about 16 hours.** The brief asks for a
multi-hour cadence change and for him not to progressively collapse. He does not: the 4 %
share ceiling holds at every phase, every fatigue action chains a `refocus`, and the
measured family share at 24 hours is **0.3 %**. But the phase itself pins at the ceiling,
so the ±0.12 oscillation only moves it downward past that point. A genuinely cyclical
long-run rhythm would need the phase to decay as well as accumulate. **Reported, not
hidden.**

## 6. Acceptance criteria

| # | Criterion | Status | Evidence |
|---|---|---|---|
1 | Behaviour simulates indefinitely without artwork | **✓** | `tradefix visual simulate` needs no art, no station, no socket. 24 h soak passes |
2 | Interaction conflicts are impossible | **✓** | Locks + chain unions + one-object rule. Zero violations across every soak. Hypothesis property test over random sequences |
3 | 24 h simulated behaviour has no obvious short loop | **✓** | Top triple 0.44 % of 24 322 triples; 12 732 distinct; longest run 1; zero back-to-back |
4 | Market energy measurably changes action density | **✓** | 492 → 1 708/h, monotonic across six bands, asserted |
5 | Music BPM subtly affects rhythmic actions | **✓** | Music actions 76.5 → 82.0/h; everything else within 2 %; share capped at 12 % |
6 | XAUUSD/BTCUSD switch does not reset behaviour | **✓** | Two tests: per-tick state/history/fatigue continuity, and 20 min across a real switch |
7 | Radio disconnect falls back to neutral idle | **✓** | Four-rung ladder, five tests. `feed_down` scenario: zero reactions, 100+ blinks, 50+ deliberate actions |
8 | All seven cameras remain compatible with V1 geometry | **✓** | `validate_metadata()`; 17-test freeze suite; `tradefix visual doctor` |
9 | Asset manifest precisely defines what plates must provide | **✓** | 22 assets with dimensions, layer order, blockout-derived depths, masks, pivots, acceptance gates |
10 | Final artwork can be inserted without redesigning the runtime | **✓ by construction** | The director addresses anchor ids and `GazeTarget` names, both resolved through the blockout. It has never known about pixels |

## 7. Things built as stubs, deliberately

| Thing | State | Why |
|---|---|---|
**Camera director** | `CameraState` + full metadata, **no selection** | The brief says stub state only. `CameraState` has no `select` method and a test asserts it stays that way |
**Placeholder renderer** | Wire protocol complete; no browser page | The protocol is the contract and is what the behaviour layer needs. The page is a V2 deliverable and needs the OBS measurement loop around it |
**Visual control page** | Not built | V9. `DirectorSnapshot` already exposes everything it will read |
**Per-plate layer stacks** | Hero camera only | The other six derive from the same room and the same depth rule; enumerating them before the hero plate is accepted would be guessing at compositions nobody has seen |

On the renderer: I built the **protocol** (`renderer.py`) and the **quality controller**
rather than the browser page. That is the honest split — the protocol is what makes the
placeholder and the eventual painted renderer interchangeable, and it is testable now.
The page itself is a V2 task whose value is the GPU measurement it enables, and that
measurement needs OBS in the loop.

## 8. The art blocker is unchanged

No artwork was fabricated. There is no image generator on this machine and none was
installed. **All 22 declared assets are outstanding.**

`INITIAL_VISUAL_AUDIT.md` §6 sets out the three routes and recommends a local diffusion
install with an identity adapter trained on the first accepted portrait, run offline
between station sessions.

**This does not block anything built here, and that was the point of the sequencing.**

## 9. Station isolation

Confirmed. Zero changes to music generation, ACE-Step, the originality engine, the queue,
market routing, playout or the audio pipeline.

The only file touched outside `tradefix_radio/visual/` and `tests/`:

- `tradefix_radio/cli/main.py` — two lines, registering the `visual` subcommand
- `tradefix_radio/cli/visual.py` — new
- `pyproject.toml` — `N806` added to the existing `scripts/**` per-file-ignores, with a
  reasoned comment (the drawing generators work in geometry, where `W`/`H`/`CX`/`CY` are
  the conventional names)

Three structural tests keep it that way: no `persistence`/`events`/`radio`/`generation`
import under `visual/`, no send call in the bridge, and `MarketRegime` imported in exactly
one module.

## 10. What to run

```
tradefix visual doctor
tradefix visual simulate --scenario breakout --duration 2h --spike-every 240
tradefix visual simulate --scenario range --duration 24h --tick 0.25 --actions 20
tradefix visual timeline --scenario trend --duration 5m --lines 60
tradefix visual manifest

pytest tests/unit/test_visual_geometry_freeze.py tests/unit/test_visual_behavior.py \
       tests/unit/test_visual_bridge.py
pytest tests/endurance/test_visual_soak.py          # add -m slow for 8 h and 24 h
python scripts/visual/validate_blockout.py
python scripts/visual/render_reference_svg.py
```

## 11. Next

**Blocked on the art decision:** V2 static scene, V3 rig, V4 idle, V5 work movements.

**Not blocked:**

- **V8 camera director.** The metadata, hold distributions, affinities and share caps are
  all declared; what is missing is the selector and the five cut vetoes. It is pure logic
  and testable the same way the behaviour director was.
- **V9 visual control page.** `DirectorSnapshot` and `BridgeStats` already carry
  everything it reads.
- **The placeholder browser renderer**, against the protocol in `renderer.py` — and with
  it the first real GPU measurement, which is what settles the 30-vs-60 fps question the
  brief deliberately left open.
