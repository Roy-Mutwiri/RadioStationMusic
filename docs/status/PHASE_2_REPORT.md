# PHASE 2 REPORT — Market Engine

**Date:** 2026-10-02 · **Status:** complete · **Basis:** `docs/IMPLEMENTATION_PLAN.md` Phase 2

---

## 1. Summary

XAUUSD is now understood, entirely offline. A simulated market drives the real feature
engine, the real energy score and the real regime classifier, and the whole chain
satisfies the two properties the brief cares most about: **scale invariance** (§6) and
**no flicker** (§5).

| Metric | Phase 1 | Phase 2 |
| --- | --- | --- |
| Python files in package | 44 | 58 |
| Package lines | 8 488 | ~13 400 |
| **Tests passing** | 615 | **919** |
| Coverage (statement + branch) | 88 % | **88 %** |
| `ruff` / `mypy` | clean | clean |
| Third-party numeric deps in the market layer | — | **none** |

All seven Phase 2 milestones met their exit tests. **Eight genuine defects** were found
by those tests and fixed — including three that made parts of the station unreachable
and one that could have left the engine permanently unable to classify anything (§5).

---

## 2. Implemented

### 2.1 `market/rolling.py` — normalisation primitives (§6)

`RollingWindow` with exact statistics, percentile ranks, quantiles, least-squares
slope; `ExponentialSmoother`; `clamp` / `safe_divide`.

Deliberately **dependency-free** — no NumPy. The feature engine runs once per bar
(once a minute in production), so NumPy would buy nothing measurable, while a
dependency-free market layer imports instantly and cannot be broken by a wheel problem.
Percentile lookups use a sorted mirror with `bisect`: O(log n) and *exact*, rather than
a streaming approximation.

Two decisions carry weight:

* **Midpoint percentile convention.** A quiet session produces long runs of identical
  ATR. A strictly-less-than rank would report 0 for a value that is in fact typical,
  making a quiet market look like a record low and dragging energy to zero.
* **Blended ranks while sparse** (`percentile_rank_stable`) — see defect 6.

### 2.2 `market/simulation.py` — all 15 §7 scenarios

Each scenario is a **phase sequence**, not a constant. A real breakout coils, builds,
expands, then normalises; `FAKE_BREAKOUT` and `VIOLENT_BREAKOUT` differ only in what
follows the expansion, which is precisely the distinction the regime engine has to
make. A simulator built from single volatility numbers would let a naive classifier
pass its own tests.

Everything is expressed as a **fraction of price**, never an amount, so a scenario
behaves identically at 180 or 40 000 — §6 enforced at the source. Paths are
reproducible from a seed and never touch the global RNG.

### 2.3 `market/indicators.py` — incremental technical indicators

ATR, RSI, ADX (+DI/−DI), Wilder smoothing, compression/expansion ratios, breakout
strength, returns. Written as **incremental state machines**, O(1) per bar, because
§64's 7-day run processes ~10 000 bars and batch recomputation would make the market
engine the slowest thing in the simulation.

Scale invariance is documented per indicator. ATR is *not* scale-free and is therefore
only ever consumed as a percentile or a ratio — never directly.

Two design points worth stating:

* **ADX and direction are kept separate.** ADX measures strength without direction;
  `+DI > −DI` supplies direction. That lets the engine express "strong trend, direction
  unclear" — a real state a single signed number cannot represent, and one that must not
  be programmed as a breakout.
* **Volume confirmation is a multiplier, not an addend**, in `BreakoutCalculator`. A
  large move on no participation is suspect regardless of size; adding the terms would
  let it still score highly, which is exactly the fake breakout §7 exists to produce.

### 2.4 `market/features.py` — the §4 feature vector

`BarAggregator` (wall-clock-aligned buckets, cumulative-volume handling, out-of-order
tolerance, gaps counted but **never filled**) plus `FeatureEngine` producing all 20 §4
features.

Scale invariance required discipline at every step: returns are fractional, the MA
slope is reported as a *fraction of price per bar*, and `distance_from_high` /
`distance_from_low` are in **ATR units** rather than dollars.

`trend_strength` combines ADX with slope significance **relative to current
volatility**. ADX alone rises during a directionless chop, which would have the station
play trending programming through a volatile mess; requiring the slope to exceed the
noise is the veto.

### 2.5 `market/energy.py` — the §6 energy triple

Every input is normalised to 0–100 **before** weighting. Mixing a percentile with a raw
return would let whichever happened to be larger dominate regardless of its configured
weight. Inputs saturate rather than overflow, so a flash crash cannot pin energy at 100
for an hour afterwards.

`energy_velocity` is a least-squares slope of recent smoothed energy, so §6's "music can
prepare for rising market intensity" has an actual signal behind it. Per-component
contributions are exposed for the §47 Market Lab — tuning weights blind is how a station
ends up playing drum and bass through a dead Asian session.

### 2.6 `market/regimes.py` — classification with hysteresis (§5)

Split into a **stateless scorer** (pure, so §47 can show every score) and a **stateful
stabiliser** holding all five anti-flicker mechanisms:

| Mechanism | Failure mode it fixes |
| --- | --- |
| `min_confidence` | adopting a near-tie winner |
| `confirmation_bars` | a single-bar false positive |
| `hysteresis_margin` | dithering at a threshold boundary |
| `min_duration_seconds` | rapid churn |
| `cooldown_seconds` | A→B→A oscillation specifically |

They are five separate knobs rather than one "stability" setting because they are
independent: confirmation alone still permits A→B→A if the challenger genuinely wins
three bars each way; cooldown alone still lets a single spike flip the regime.

Score shapes use **soft ramps and bands** rather than hard thresholds — a hard threshold
is what makes a classifier dither in the first place, and hysteresis is the second line
of defence, not the first.

### 2.7 `market/sessions.py` — sessions with real DST (§2.6, §97)

Local exchange time via `zoneinfo`, not fixed UTC offsets. London and New York change
clocks on *different dates*; a fixed table is right for about ten months a year and
quietly wrong for the rest, during which the station would announce the London open an
hour early and apply the wrong §97 personality.

The weekend gap is a first-class `CLOSED` state. Public holidays are deliberately **not**
modelled: a wrong holiday table would be worse than none, and a genuinely dead feed is
caught by staleness (§63-E) instead — more reliable and self-correcting.

### 2.8 `market/feeds/` — four implementations behind one interface (ADR-03)

Pull-based (`poll()`), so the virtual clock drives it and staleness is a *measurement*
rather than the absence of a callback.

| Feed | Status |
| --- | --- |
| `SimulatedFeed` | fully verified |
| `ReplayFeed` (§7 scenario 15) | fully verified |
| `MetaTrader5Feed` | verified against an injected fake; **not** against a live terminal |
| `RestPollingFeed` | verified against a stubbed transport; **not** against a live provider |

### 2.9 `market/service.py` — `MarketDataService`

Assembles `MarketStateV1` and owns the §63-E honesty rules. Data age is measured
**monotonically** — NTP corrections and DST shifts move `now()` and would make a healthy
feed momentarily look hours stale.

### 2.10 `tradefix market-sim` — Phase 2's verification path

The §47 Market Lab arrives in Phase 5, but the market engine has to be inspectable
*before* an API and a frontend exist. Runs the **production** engines (not a simplified
copy) under a virtual clock, so a simulated day takes a couple of seconds and output is
byte-identical for a given seed.

---

## 3. Evidence

### 3.1 Lint, types, tests

```
$ ruff check tradefix_radio tests --output-format concise
All checks passed!

$ mypy
Success: no issues found in 58 source files

$ pytest tests/unit tests/integration
919 passed, 1 warning in 59.76s

TOTAL  5823 stmts  553 miss  1226 branch  164 partial  88%
```

| Module | Tests | Covers |
| --- | --- | --- |
| `unit/test_rolling.py` | 48 | §6 normalisation primitives |
| `unit/test_simulation.py` | 44 | §7 — all 15 scenarios' statistical signatures |
| `unit/test_feeds.py` | 44 | ADR-03 — four feeds |
| `unit/test_features.py` | 41 | §4, §6 — incl. affine invariance |
| `unit/test_regimes.py` | 38 | §5 — incl. the anti-flicker suite |
| `unit/test_energy.py` | 37 | §6 |
| `unit/test_sessions.py` | 32 | §2.6, §97 — incl. DST |
| `integration/test_market_service.py` | 20 | §4, §63-E |

### 3.2 The market composes the radio — measured

`tradefix market-sim --scenario flat --switch-to violent_breakout --switch-at 150
--bars 300 --every 60`, verbatim:

```
  bar     time      price regime                      conf   enr     dE   vol   trd   cmp   exp   brk dir
----------------------------------------------------------------------------------------------------------
  103*14:43:00    4000.04 low_volatility_range        0.55  10.7  -0.10  15.0   5.3   6.1   0.0   0.0 neutral
  120 15:00:00    4000.16 normal_range                0.38  17.6  +0.19  61.2   7.3   0.0   0.4   0.0 neutral
  154*15:34:00    4000.57 high_volatility_range       0.77  21.6  +2.17  96.1   5.6   0.0   6.7   0.0 neutral
  172*15:52:00    4064.21 extreme_volatility          0.60  65.2  +3.52  99.7  31.8   0.0 100.0  74.7 bullish
  180 16:00:00    4130.08 extreme_volatility          0.82  77.4  +1.15  99.7  56.5   0.0 100.0  68.8 bullish
  235*16:55:00    4329.78 bullish_trend               0.59  64.3  +0.03  83.1  56.7  16.8   0.0   0.0 bullish
  248*17:08:00    4333.61 reversal                    0.69  58.6  -0.43  73.8  40.2   8.2   0.0   0.0 neutral
  278*17:38:00    4415.29 bullish_trend               0.68  57.5  -0.52  75.2  34.2   0.0   0.8   0.0 bullish

  bars                299
  regime transitions  11
  bars per transition 27.2
  energy min/mean/max 8.3 / 39.2 / 80.5
```

Reading across: a dead market sits at **energy 10.7** in `low_volatility_range`. Four
bars after the breakout begins, volatility hits the 96th percentile and
`energy_velocity` turns **+2.17** — the engine is signalling *rising* intensity before
the move completes, which is exactly what §6 says velocity is for. Energy peaks at
**80.5**, the regime passes through `extreme_volatility`, settles into `bullish_trend`,
and a genuine `reversal` is detected at bar 248. An **8× energy swing** with only
**11 transitions across 299 bars**.

### 3.3 Scale invariance (§6), proven

`test_energy_is_invariant_under_price_rescaling` runs the same seeded scenario at
**0.05×, 10× and 250×** price and asserts every energy value matches to 1e-6.
`test_percentile_features_are_invariant_under_price_rescaling` does the same for all 18
scale-free features at four scales.

`test_atr_itself_does_scale_with_price` is the control: ATR *must* change by exactly
10×. Without it, the invariance tests could pass because nothing was being computed.

### 3.4 Anti-flicker (§5), proven adversarially

`test_adversarial_oscillation_does_not_flicker` alternates features **every single
bar** between a dead-quiet market and a violent breakout for 200 bars — the most hostile
input possible. An unstabilised classifier transitions on nearly every bar.

**Result: ≤ 4 transitions in 200 alternating bars.**

Supported by: a single spike bar changes nothing; a sustained change *is* eventually
adopted (stability must not become deafness); each of the five mechanisms tested in
isolation; and `test_regime_transitions_stay_bounded_over_a_long_run`, which drives
8 simulated hours of event-driven volatility through the whole service.

---

## 4. Milestone exit tests

| # | Milestone | Exit test | Result |
| --- | --- | --- | --- |
| 2.1 | `MarketFeed` + simulator, 15 scenarios | each scenario shows its claimed **statistical signature**, measured on the path produced | ✅ 44 tests |
| 2.2 | 20-feature engine, NaN-safe warm-up | fixtures; `insufficient_history` instead of garbage; no NaN ever escapes | ✅ 41 tests |
| 2.3 | Rolling normalisation, no absolute-price assumptions | **affine invariance at 4 scales** | ✅ 48 + property tests |
| 2.4 | Energy: raw / smoothed / velocity | always ∈ [0,100]; monotone per input; velocity sign correct | ✅ 37 tests |
| 2.5 | 14 regimes + hysteresis/confirmation/duration/cooldown | **no flicker** under adversarial input; each regime reachable; `UNKNOWN` on insufficient data | ✅ 38 tests |
| 2.6 | Sessions incl. DST and weekend gaps | boundary timestamps across **both** DST schedules | ✅ 32 tests |
| 2.7 | `MarketState` + staleness + last-good retention | stale ⇒ marked, **no fabricated price** | ✅ 20 tests |

Milestone 2.5's reachability test deserves a note: it drives **every** §7 scenario at
three seeds through the real pipeline and asserts that all 13 non-`UNKNOWN` regimes are
reached. Three were not, and all three were real defects (§5 below) rather than test
artefacts.

---

## 5. Defects found by the tests and fixed

| # | Defect | Why it mattered | Fix |
| --- | --- | --- | --- |
| 1 | **The engine could stay in `UNKNOWN` forever.** `min_confidence` is a *margin* gate, and it was applied to the first classification. Neighbouring regimes legitimately score closely — `QUIET` vs `LOW_VOLATILITY_RANGE` in a dead market — so the margin never cleared the bar. | The worst class of bug in the phase: the station would broadcast neutral programming indefinitely and **never react to the market at all**, while every component reported healthy. | The first classification is gated on the winner's **absolute** score (`FIRST_CLASSIFICATION_MIN_SCORE`). The margin gate still guards *challenges*, which is the only place flicker is a risk. Reported confidence stays the honest margin. |
| 2 | **`QUIET` was unreachable.** `LOW_VOLATILITY_RANGE`'s band started at the very floor and scored a perfect 100 there; `QUIET`, capped below 100 by its weighted blend, could never win. | The station's quietest programming would never have aired — a whole musical register silently dead. | The quiet-family bands are now nested with **distinct centres** (`QUIET` below 12th percentile, `LOW_VOLATILITY_RANGE` 14–34, `NORMAL_RANGE` 36–62). |
| 3 | **`REVERSAL` was unreachable.** It required the slope-confirmed direction to have flipped — but the 20-bar MA slope lags, so by the time it flips, the earlier history has flipped too and there is no opposition left to detect. | A reversal *is* the moment the fast and slow signals disagree. Requiring the slow one to confirm meant detecting a reversal only after it had completed, which is not detecting it. | The reversal detector now uses a **fast** directional signal (`fast_direction_of`, 5-bar return) while the trend regimes keep the conservative slope-confirmed one. The asymmetry is the point. |
| 4 | **`BREAKOUT_BUILDUP` was unreachable** — no scenario produced "tight range with rising volume". | Same consequence as 2: dead configuration and unaired programming. | A **buildup phase** was added to the breakout scenarios. Volume leads price; participation arrives while the range is still tight. That is both realistic and the exact signature distinguishing buildup from compression and from breakout. |
| 5 | **The energy-event throttle was dead logic.** A 10-second gate, but `_publish` is only reached when a bar closes and bars are 60 s apart — so the gate was always already satisfied and **every bar published an event**. | Not harmful, but the code claimed to throttle and did not. Throttling on a time axis finer than the publisher's own cadence cannot throttle anything. | Replaced with a magnitude gate plus a **bar-counted** heartbeat (`ENERGY_EVENT_HEARTBEAT_BARS`). |
| 6 | **Cold start read as near-record volatility.** The first few ATR observations are by definition the highest ever seen, so a quiet coil at startup reported the **97th percentile** and the engine classified `HIGH_VOLATILITY_RANGE`. | The station would open **every run** playing breakout music for the first half hour before settling — audibly wrong, and the kind of bug that looks like a feature. | `percentile_rank_stable` blends from 50 (no opinion) to the measured rank in proportion to window fill. Blending depends only on sample *count*, so §6 invariance is preserved. Verified: the same run now reads `normal_range` at vol 63 instead of `high_volatility_range` at 97. |
| 7 | **`ASIAN_LONDON_OVERLAP` was unreachable.** Tokyo's 15:00 close precedes the London open in both GMT and BST, so the two windows never intersected. | §97's Asian/London personality was dead configuration. | The Asian session for gold is not Tokyo *equities* hours — Singapore and Hong Kong carry liquidity later. Close moved to 17:00 JST, which both matches the conventional Asian metals session and creates the real one-hour overlap. |
| 8 | **`RegimeSettings` rejected `cooldown_seconds > 0` whenever `min_duration_seconds == 0`.** The 10× ratio check degenerates at zero. | `min_duration_seconds = 0` is a legitimate setting, and the invariant made it unusable with any cooldown. | The ratio check now applies only when a minimum duration is actually set. |

Also fixed: a `VirtualClock.advance_sync` was added so the `market-sim` driver stops
poking a private attribute — and it **raises** if any task is sleeping on the clock,
because silently skipping a waiter would make a simulation quietly wrong.

---

## 6. Architecture decisions made during Phase 2

1. **The market layer has no third-party numeric dependency.** One bar per minute makes
   NumPy pointless here, and dependency-freedom means instant imports and no wheel risk.
   NumPy arrives in Phase 6 where audio genuinely needs it.
2. **Indicators are incremental, not batch.** O(1) per bar, because §64 processes ~10 000
   bars and recomputation would dominate.
3. **Scorer and stabiliser are separate objects.** The scorer is pure so §47 can display
   every score and reproduce a decision; all mutable state lives in the stabiliser, which
   is therefore the single target for §75 recovery and §96 persistence.
4. **Direction has two speeds, deliberately.** The conservative slope-confirmed signal
   for anything the station *asserts* (§14 forbids implying direction on a provisional
   signal); a fast signal used only by the reversal detector, whose job is to notice
   disagreement between them.
5. **Gaps are counted, never filled.** §86 forbids inventing market data, and that
   includes synthesising bars across a weekend or an outage.
6. **Staleness is measured monotonically**, so NTP and DST cannot fake an outage.
7. **Feeds are pull-based.** A push feed owns its own timing and would make §64's
   accelerated runs impossible.

---

## 7. Known limitations

| Limitation | Assessment |
| --- | --- |
| **`MetaTrader5Feed` is unverified against a live terminal.** | Tested against an injected `Mt5Client` fake covering symbol discovery, Market Watch selection, tick deduplication, zero-quote rejection and shutdown-on-failed-open. The *wire behaviour* of a real broker's terminal is **not** verified, because no account is logged in on this host (audit R-04). Nothing else depends on it: development and simulation run on `SimulatedFeed`, and `tradefix doctor` reports its state accurately. To be validated before production use. |
| **`RestPollingFeed` is unverified against a live provider.** | Field mapping, failure escalation, timestamp heuristics and header-based secret handling are tested against a stubbed transport. The specific JSON shape of any given provider is unverified (no API key available). The field map is configuration precisely so this is a config change, not a code change. |
| Public holidays are not modelled. | Deliberate, documented in `sessions.py`: a wrong holiday table would silence the station on a day the market was open. Staleness detection (§63-E) covers the real case better. |
| Regime scoring weights are code constants, not YAML. | The *thresholds* (`RegimeSettings`) are configurable; the per-regime score blends are not yet. Acceptable for now — they are a classifier, not programming policy, and §10/§14's config-driven requirement is about genres and topics. Worth revisiting if operators want to tune classification. |
| `~90 %` of bars classify; the rest are warm-up `UNKNOWN`. | Correct and honest. With a 720-bar production percentile window and a 30-bar warm-up, the engine withholds judgement for roughly the first half hour after a cold start, then blends in. §63-E neutral programming covers that window. |
| Coverage is 88 %, unchanged. | The market layer is well covered; the uncovered remainder is still concentrated in `resources.py`, `storage/paths.py` and `checks.py`, which need real GPU/audio/MT5/OBS states. |

---

## 8. Performance

| Measurement | Value |
| --- | --- |
| Full 919-test suite | **60 s** |
| `market-sim`, 300 bars through the real engines | **< 1 s** wall clock |
| Simulated hours per real second (virtual clock) | **> 5 000×** |
| Feature vector construction | once per bar; O(1) in all indicators |

The 5 000× ratio is the figure that matters: §64's 7-day endurance run needs roughly
10 000 bars, which at this rate is a few seconds of real time.

---

## 9. Next step

**Phase 3 — Music director, lyric director, diversity engine.** The market now produces
a trustworthy `MarketState`; Phase 3 turns it into a `MusicBlueprint`.

The exit test that matters is §3.12: **10 000 simulated decisions** across all regimes,
with a committed statistical report proving genre/BPM/key/topic distributions do not
collapse (§81-18). Also due: the §11 anti-boredom rules at all §12 horizons, the §14
topic graph with `certainty` qualifiers, the §17 lyric validators against a labelled
corpus of bad lyrics, and §96 radio memory surviving a restart without causing immediate
repeats.
