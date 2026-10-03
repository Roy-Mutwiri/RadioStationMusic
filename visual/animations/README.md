# visual/animations/

**The action catalogue is not here.** It is Python, in
`tradefix_radio/visual/catalog.py`, and that is a decision rather than an oversight.

## Why the catalogue is Python and not JSON

V1's asset plan said `motion_library.json`. V6 implemented it as a Python module, for
three reasons that only became clear once the director existed:

1. **Validation at definition.** Every entry is an `ActionSpecV1`, so a malformed record
   fails at import with the field named. A JSON file needs a loader, and a loader that
   runs after startup turns a typo into a runtime surprise. The contract already rejects
   an object lock on an interruptible action — that check has to run somewhere, and the
   constructor is the cheapest place.
2. **Derived values.** `BandBiasV1.ramp(0.2, 1.8)` expresses "suppressed in quiet,
   amplified in alert" in one line; the JSON equivalent is six magic numbers per action
   across 66 actions. And `ActionChain.required_locks()` computes the union of its steps'
   locks *from the catalogue*, which a flat file cannot do.
3. **No second source of truth.** A JSON file plus a Python loader is two places a
   behaviour can be defined. One is better.

The brief's §71 requirement — no magic numbers scattered through logic — is met the same
way either path would meet it: the catalogue is the single place tuning lives, and the
director contains no action names at all.

## What belongs here

| File | Role |
|---|---|
`curves/` | Authored easing and deformation curves, once the rig exists |
`reference/` | Recorded motion reference clips for the `MOTION_LIBRARY.md` §14 sheet |

Both are **art-pipeline** artefacts and both are outstanding, blocked on the rig. The
behaviour layer does not need them: it emits `action_id`, duration, blend windows and
amplitude, and the renderer decides how to interpolate.

## Rules that still hold

- **No motion record names a market regime.** Bias is expressed against intensity bands
  B0–B5, and the band mapping lives in exactly one module in the bridge.
  `test_no_action_names_a_market_regime` checks every id, tag and key.
- **Every interval is a distribution, never a constant.** Durations, cooldowns,
  amplitudes and blend times are all sampled. A fixed cooldown is a visible period, and
  `CooldownTable` has no method that accepts one.

## Inspecting it

```
tradefix visual doctor                    # catalogue vs frozen geometry
tradefix visual timeline --duration 5m    # what it actually produces
```
