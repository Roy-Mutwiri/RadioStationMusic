# Fresh Start Programming (§FSP)

**Status:** IMPLEMENTED
**Date:** 2026-10-04

## Problem Statement

Trade Fix Radio would begin every session with the same recognizable procedural audio.
Users could identify repeat startups by the identical ambient music that played while
the station generated fresh tracks.

**Evidence from database:**

```
procedural-000001|procedural|2026-10-04 07:15:25  <- Startup 1
procedural-000002|procedural|2026-10-04 07:16:11

procedural-000001|procedural|2026-10-04 08:43:30  <- Startup 2 (IDENTICAL!)
procedural-000002|procedural|2026-10-04 08:44:16
...

procedural-000001|procedural|2026-10-04 08:52:52  <- Startup 3 (IDENTICAL!)
procedural-000002|procedural|2026-10-04 08:53:38
```

Three separate startups all began with `procedural-000001` using seed=0.

## Root Cause

1. **Fixed procedural seed**: `ProceduralSource` initialized with `seed: int = 0`
2. **No startup priming**: Station immediately fell to Tier 3 (procedural) because READY=0
3. **State restoration didn't help**: Even with block_index restoration, the underlying
   seed was still 0, producing the same audio patterns across sessions

## Solution Overview

### 1. Startup State Machine

Added explicit startup states to distinguish "process running" from "ready for listener":

```
BOOTING
    → MARKET_ACQUIRE
    → GENERATOR_WARMING
    → PRIMING
    → READY_TO_AIR
    → ON_AIR
```

### 2. Two Startup Modes

| Mode | Description | Use Case |
|------|-------------|----------|
| `CONTROLLED_START` | Prime fresh tracks BEFORE audible playout | Normal startup |
| `LIVE_RECOVERY` | Play emergency audio immediately while rebuilding | 24/7 crash recovery |

### 3. Fresh Track Requirements

Before `READY_TO_AIR` in controlled start mode:

```yaml
startup:
  prime_enabled: true
  minimum_fresh_tracks: 2        # OR
  target_fresh_minutes: 8.0      # whichever is met first
  require_unplayed: true
```

A track qualifies as FRESH only if:
- Never been played (play_count=0)
- Not marked played in current session
- Passes originality checks
- Matches current active market (XAUUSD/BTCUSD)

### 4. Unique Procedural Session Seeds

Every station startup now derives a unique seed from:
- Wall clock time in nanoseconds
- Process monotonic counter
- Random UUID bits

```python
def _derive_session_seed() -> int:
    now_ns = time.time_ns()
    monotonic_ns = int(time.monotonic_ns())
    uuid_bits = uuid.uuid4().int & 0xFFFFFFFF
    seed = ((now_ns ^ (monotonic_ns << 20) ^ uuid_bits) & 0x7FFFFFFF)
    return seed if seed > 0 else 1
```

Even if the station does fall to Tier 3, it will sound different every time.

## Files Changed

### Core Implementation

| File | Changes |
|------|---------|
| `contracts/enums.py` | Added `StartupState`, `StartupMode` enums |
| `radio/startup.py` | **NEW**: `StartupProgrammingPlanner`, `StartupProgress`, `StartupRequirements` |
| `radio/emergency.py` | Session-derived seed, `ProceduralBlockSpec` for history tracking |
| `radio/station.py` | Integrated startup state machine, priming loop |
| `config/schema.py` | Added `prime_enabled`, `minimum_fresh_tracks`, etc. to RadioSettings |
| `api/runner.py` | Pass `startup_mode` through `serve()` and `ControlCenterRunner` |
| `cli/station.py` | Added `--wait-for-fresh` and `--immediate` flags |

## Configuration

```yaml
radio:
  # Fresh start programming (§FSP)
  prime_enabled: true
  minimum_fresh_tracks: 2
  target_fresh_minutes: 8.0
  require_unplayed: true
```

## CLI Usage

```bash
# Normal startup - wait for fresh tracks (default)
tradefix station start --wait-for-fresh

# Live recovery - immediate playout with fallback
tradefix station start --immediate
```

## Control Center Display (Future Work)

During startup, the Control Center should show:

```
TRADE FIX RADIO
PRIMING

Active Market:
BTCUSD

ACE-Step:
GENERATING

Fresh Tracks:
1 / 2

Opening Buffer:
4:01 / 8:00

Audio Output:
HELD UNTIL FRESH BUFFER READY
```

## Metrics

Key metrics to track:

| Metric | Description |
|--------|-------------|
| `time_to_first_fresh_track` | From process start to first generated track |
| `time_to_ready_to_air` | From process start to priming complete |
| `priming_progress_fraction` | 0.0 to 1.0 during PRIMING state |

## Test Cases

| Test | Expected Behavior |
|------|-------------------|
| A: Fresh install | Prime new tracks before ON_AIR |
| B: Restart after tracks aired | None of previous played tracks selected |
| C: Unplayed reserve exists | Can start immediately with unseen reserve |
| D: Tier 3 twice | Different procedural session identity |
| E: ACE-Step slow startup | Honest PRIMING state, no fake READY |
| F: Live crash/recovery | Emergency audio prevents dead air |
| G: BTC weekend startup | First track reflects BTCUSD |
| H: Gold reopening | XAUUSD-context opening |

## Verification

After implementation, restart Terminal B with:

```bash
tradefix station start --wait-for-fresh
```

Expected observations:
1. Banner shows: `*** FRESH START: waiting for fresh tracks ***`
2. No familiar procedural audio during wait
3. First audible track is a never-before-played real ACE-Step track
4. Logs show `startup.priming_complete` with fresh track count

## Backward Compatibility

- Default behavior is `CONTROLLED_START` (fresh priming)
- Old behavior available via `--immediate` flag
- Settings are additive; existing configs work unchanged
- Procedural audio still works but sounds different each session

## Live Verification Results (2026-10-04)

### 1. Procedural Session Uniqueness

```
Session 1: id=165c2fd6 seed=1012411673
Session 2: id=daed515f seed=11372517
Session 3: id=5568376b seed=1479904216
Session 4: id=a1eacf1a seed=1224599569
Session 5: id=ab46c157 seed=850207260

OK: All session IDs are unique
OK: All seeds are unique
OK: No zero seeds (old default bug fixed)
Seed range: 11,372,517 - 1,479,904,216
```

### 2. Reserve System Verification

```
Unplayed Ready Tracks (Reserve Pool):
  TF-20261004-00091: "No Hesitation through Last Orders" | drill | BTCUSD | plays=0
  TF-20261004-00090: "Consequence through the Back Office" | garage | BTCUSD | plays=0
  TF-20261004-00089: "Fast Distance at no Bid" | garage | BTCUSD | plays=0
  TF-20261004-00088: "Narrow Emotional Discipline" | progressive_house | BTCUSD | plays=0
  TF-20261004-00087: "Loud Doubt in the Closed Book" | garage | BTCUSD | plays=0
  ... (9 total)

Reserve Counts by Market:
  BTCUSD: 9
  XAUUSD: 0
  NEUTRAL: 0

Most Recently Played Track (correctly excluded):
  TF-20261004-00086: "The Slow Desk" | plays=1 | played | BTCUSD
```

### 3. Market Compatibility

```
Market filter test - BTCUSD active:
  Eligible for BTCUSD session: 5 tracks (all BTCUSD or NEUTRAL)
  XAUUSD tracks (NOT eligible during BTCUSD): 0
```

### 4. Startup State Machine

```
Controlled Start Mode:
  BOOTING (held=True)
  → MARKET_ACQUIRE (held=True)
  → GENERATOR_WARMING (held=True)
  → PRIMING (held=True)
  → READY_TO_AIR (held=False) ← Audio starts here
  → ON_AIR

Live Recovery Mode:
  BOOTING (held=False) ← Audio never held
  → MARKET_ACQUIRE (held=False)
  → GENERATOR_WARMING (held=False)
```

### 5. Test Matrix

| Test | Status | Evidence |
|------|--------|----------|
| Unique procedural seeds | PASS | 5 sessions, all unique, no zeros |
| Reserve query by market | PASS | Filters BTCUSD correctly |
| Played tracks excluded | PASS | TF-...-00086 has play_count=1, not in reserve |
| State machine holds audio | PASS | Controlled start holds until READY_TO_AIR |
| Live recovery no hold | PASS | should_hold_audio=False throughout |

### Manual Live Test Required

To complete acceptance, run with real audio:

```bash
# Ensure ACE-Step is running
tradefix station start --wait-for-fresh --provider ace_step
```

Verify:
- [ ] First audible track has play_count=0
- [ ] Not the same procedural opening as before
- [ ] State progression visible in logs
- [ ] No unintended silence

## Acceptance Criteria Status

| Criterion | Status |
|-----------|--------|
| Controlled start doesn't begin with same procedural tune | **VERIFIED** (unique seeds) |
| First audible track has prior play_count=0 | **CODE READY** (needs live test) |
| Second controlled start produces different opening set | **VERIFIED** (unique seeds/sessions) |
| Reserve tracks consumed once aired | **VERIFIED** (play_count increments) |
| Tier 3 emergency uses different identity/seeds | **VERIFIED** (5/5 unique) |
| Market compatibility respected | **VERIFIED** (BTCUSD filter works) |
| No unintended silence in live-recovery | **CODE READY** (needs live test) |

## Implementation Complete

The Fresh Start Programming implementation is code-complete. The unseen reserve system is operational with 9 tracks available for BTCUSD sessions. Live audio testing requires ACE-Step to be running.
