# REAL_STATION_TEST_01 — run plan

A human validation run of the actual product, not another development phase. Sit at the PC,
open the Control Center, start the station, and listen to real ACE-Step music through a
real Windows audio device.

The path under test, end to end, with nothing mocked:

```
MarketSimulator → MarketRouter → MusicDirector → LyricsDirector → ACE-Step
  → Phase 6 QC → originality → mastering → queue → PlayoutEngine → Windows audio device
```

**Observe first.** No architectural changes unless the run exposes a genuine defect, and no
tuning changes during the first run at all. Record evidence; fix clear BUGS afterwards.

Minimum 30 minutes of continuous operation; 60 preferred.

---

## Before starting

| | |
|---|---|
| Report | `docs/status/REAL_STATION_TEST_01.md` |
| Device | Headphones (C15B) — WASAPI, device index 24 (confirmed by beep sweep) |
| Entry point | `tradefix station start --test-mode --open` |
| Buffer | TEST MODE lowers the targets to 1.5 / 12 / 25 min. It is an **override**, not a config change — `_apply_overrides` uses `model_copy` and the files on disk are untouched. The banner says so on every launch. |

Every finding is classified. The classification is the point: it decides what happens next.

- **BUG** — the system does something it should not. Fix after the run.
- **TUNING ISSUE** — correct behaviour, wrong number. Record; do not change during the run.
- **MODEL LIMITATION** — ACE-Step cannot do this. Record; not a defect.
- **UX ISSUE** — the system is right and the screen is misleading. Tracked separately from
  runtime defects.
- **EXPECTED BEHAVIOUR** — recorded so the run is reproducible.

---

## Steps

### 1. Pre-flight
`tradefix doctor`. Do **not** start the station while any REQUIRED component fails. Record
the full output, including what is OPTIONAL and missing.

### 2. Audio device discovery
`tradefix audio devices`. Record the table and what the configured name actually resolves
to. Never proceed on an unknown device — Windows enumerates every device once per host API
and a bare name is ambiguous on essentially every machine.

### 3. Audio smoke test
A short tone at a conservative level, confirmed audible by ear before anything else runs.
(The first attempt at this failed silently at −26 dBFS; the sweep that resolved it ran at
−14 dBFS with distinct beep counts per device.)

### 4. Start the real services
`tradefix station start`. Normal entry point, real provider, real pipeline.

### 5. Control Center, live
Open it. Every number on screen must come from the running station. No fake values, no
placeholder metrics.

### 6. Market simulation
Confirm the simulator is driving the feed and the simulation chip is visible.

### 7. First real track
Print and record the event sequence: blueprint → job → generation → QC → originality →
mastering → queue → playout.

### 8. Human listening test
GOOD / BAD / SKIP per track, persisted. This is the only measurement of whether any of it
works.

### 9. Market reaction
Drive the simulator through regimes. Does the programming follow?

### 10. Rap test
A lyrical track. Are the words audible, and do they say anything defensible?

### 11. Instrumental test
Confirm an instrumental is actually instrumental.

### 12. Transition test
Listen across a boundary. Does it sound like a station?

### 13. Buffer and capacity
`effective_capacity = approved_audio_duration / total generation wall time`. Record the
measurement, not an estimate.

### 14. Fast-profile comparison
Run both. **Do not adopt `fast` automatically** — record the evidence and decide after.

### 15. Longer tracks
60 / 180 / 240 s. Capacity at each.

### 16. Failure while listening
Kill ACE-Step mid-run. The broadcast must not stop.

### 17. Skip control
Use it. It must end the current track at a block boundary and nothing else.

### 18. Dashboard review
Walk every page. Record UX defects **separately** from runtime defects.

---

### 19. Market routing *(added before the run)*

The subsystem did not exist when the 20 steps were written. It changes what the station
does on a weekend, so it is exercised deliberately rather than waited for.

**The waits in this step are real and are not to be shortened.** Closing takes 2 minutes
to confirm and reopening takes 5, at the production defaults. TEST MODE lowers the *buffer*
targets so a run fits in an hour; it deliberately leaves the routing windows alone, because
the asymmetry between them is the behaviour under test and a run with both set to ten
seconds would observe nothing. Budget roughly 10 minutes for 19b–19d.

Automated coverage is already in place — 34 deterministic tests across
`tests/unit/test_market_router.py` and `tests/integration/test_market_routing.py`, covering
all fourteen required scenarios. What this step adds is the part no test can assert: whether
a switch *sounds* like a decision or like a fault.

**19a — Start on gold.** Header shows `XAUUSD`. Market page shows both symbols, gold
`OPEN` and active, Bitcoin `OPEN` and in reserve with a bar count that is rising. Confirm
Bitcoin is being polled — if its bar count is frozen the reopen path cannot work and nothing
later in this step is meaningful.

**19b — Close gold.** Market page → `Simulate XAUUSD closed`.

Expect, in order:
1. gold's chip turns `CLOSED`, with a reason naming the override;
2. gold is **not** marked `Feed degraded` — a closure is not a fault;
3. a pending-switch line appears with the confirmation window counting down;
4. **the track on air keeps playing**, uninterrupted;
5. at the end of the window the header changes to `BTCUSD` and the `Fallback market` chip
   appears;
6. flexible queue rows are replanned; HARD and SOFT rows are not;
7. the queue shows both symbols at once — this is correct, and the per-row symbol is there
   so it does not look like a bug.

Listen across the switch. Record whether the change of subject is audible as programming.

**19c — The lyrics.** Wait for a lyrical track planned under BTCUSD and read its topic and
text. It must not describe itself as a live gold track and must not reference a London or
New York session. This is the §14 requirement, and it is the one thing in this step that is
a correctness failure rather than a quality one.

**19d — Reopen gold.** Release the override.

Expect: gold returns to `OPEN`; the station does **not** switch back immediately; the
pending line shows the longer reopen window; the switch happens only after it elapses; no
interruption to playback; gold's regime is immediately meaningful rather than `UNKNOWN`,
because its engine kept its own warmed history throughout.

**19e — The distinction, by hand.** The one failure mode this subsystem exists to prevent.
With gold active and **not** forced closed, stop its feed so data simply stops arriving.

Expect: gold goes `STALE` then `UNAVAILABLE`, is marked `Feed degraded`, is **never** marked
`CLOSED`, and **the station stays on gold**. If it moves to Bitcoin here, that is a BUG of
the highest severity in this run and the test stops.

---

### 20. Write the report

`docs/status/REAL_STATION_TEST_01.md`, every finding classified.

---

## Success criteria

- The station runs ≥ 30 minutes without unintended silence.
- Real ACE-Step audio reaches the chosen Windows device and is audible.
- Every track that aired passed Phase 6 QC, originality and mastering — nothing bypassed.
- The programming visibly tracks the market.
- A market closure moves the station without interrupting playback.
- A feed outage does not.
- Every figure on the Control Center came from the running station.
