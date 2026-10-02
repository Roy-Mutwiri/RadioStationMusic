# PHASE 4 REPORT — The Radio Runtime

**Date:** 2026-10-02 · **Status:** complete · **Basis:** `docs/IMPLEMENTATION_PLAN.md` Phase 4,
and the milestone 4.2–4.10 specification with acceptance gates A–J

---

## 1. Summary

The station broadcasts. Continuously, unattended, through a dead generator, an empty queue, a
missing file, a failing sink and a crash — and it proves it by doing it, accelerated, with the
result wired to an exit code.

```
  TRADE FIX RADIO SOAK REPORT

  Simulated runtime:  2.02h  (1440 steps in 137.5s wall clock)

  Audio
    Unintended silence: 0 ms
    Audio coverage:     100.00% (7200s of 7200s)
    Transitions:        160
    Transition failures: 0
  Radio
    Queue underruns:    0
    Tier 3 activations: 1 (1.0 min)
  Generation
    p50: 20.0s   p95: 20.0s   capacity ratio: 2.25x   Retries: 0   Timeouts: 0
  Creative
    Genres used: 28   Longest identical genre run: 1   Distinct BPMs: 68
    Blueprint duplicates: 0   Track id duplicates: 0
  Runtime
    Unhandled exceptions: 0   Subscriber errors: 0   Leaked tasks: 0   Pending leases: 0
  Memory
    Start: 70 MB   Peak: 116 MB   End: 114 MB

  PASS: every acceptance invariant held
```

| Metric | Phase 3 | Phase 4 |
| --- | --- | --- |
| Python files in package | 76 | **102** |
| Package lines | 21 416 | **31 665** |
| Test files / lines | — | **51 / 22 084** |
| **Tests passing** | 1 365 | **1 796** |
| `ruff` / `mypy` | clean | clean |
| Simulated broadcast proven continuous | — | **2 h and 24 h, zero dead air** |
| Acceleration achieved | — | **52×–60× real time** |

`1 796 passed, 8 warnings in 909.85s` for the whole suite, gates included. The eight warnings
are `ResourceWarning`s from aiosqlite connections closed by garbage collection during the
deliberately abrupt shutdowns Gates F and G perform; they are noise from the simulation of a
crash, not from the station.

The nine new modules are listed in §4. **Twenty genuine defects** were found by the tests
and gates and fixed; they are in §6, with the measurement that exposed each. Three of them —
the playout/scheduling task split, the audio-coverage metric, and the clock hold — were the
difference between a station that *reported* perfect continuity and one that had it.

---

## 2. What was asked, and what holds

| Required (§4.2–4.10) | State |
| --- | --- |
| Nine job states, lease + expiry, no infinite retries, no duplicate completions | ✅ `core/job_states.py`, `persistence/repositories/jobs.py` |
| Injectable clock everywhere | ✅ nothing in the package calls `datetime.now()` or `asyncio.sleep` directly |
| Typed domain events, subscriber isolation, clean shutdown | ✅ `runtime/coordinator.py` |
| Layered locking, transactional queue operations | ✅ `radio/queue.py` |
| Buffers 20/45/90 configurable, `time_to_buffer_failure`, capacity ratio + p50/p95 | ✅ `radio/buffer.py`, `generation/capacity.py` |
| Creative Risk Controller coupling creativity to operational health | ✅ `radio/scheduler.py` |
| Generation **outside** the playout thread | ✅ four independent tasks — §3 of `docs/ARCHITECTURE.md` |
| Canonical playout format, conversion at input boundaries | ✅ `audio/format.py` |
| Configurable station-ID records, five categories, anti-repetition | ✅ `radio/station_ids.py` |
| Three emergency tiers, Tier 3 non-looping, automatic escalation and return | ✅ `radio/emergency.py` |
| Crash recovery that classifies rather than blanket-fails | ✅ `radio/station.py::recover` |
| `tradefix soak --simulated-hours N` driving the real runtime | ✅ `cli/soak.py` |

Prohibitions in §4.2 that are structurally impossible rather than merely avoided:

* **Two workers cannot generate one job** — the claim is a conditional `UPDATE … WHERE state
  IN (…)`; the loser sees zero affected rows. There is no application-level lock to get wrong.
* **An expired lease cannot block a job forever** — nothing detects the crash; the lease runs
  out and the row becomes claimable again.
* **A generator failure cannot propagate into playout** — `claim_and_generate` returns a
  `GenerationOutcome` and never raises, and the generation worker is a different task from the
  playout pump.

---

## 3. Evidence

### 3.1 Acceptance gates A–J

`tests/integration/test_phase4_gates.py`, 16 tests, driving the **real** station — real
scheduler, real queue, real mixer, real generation manager with its leases and retries, mock
provider, `NullSink`, virtual clock. A gate that passed against a simplified harness would be
a statement about the harness.

| Gate | Subject | Result |
| --- | --- | --- |
| A | Continuous playout, zero underruns, zero dead air | ✅ 15 simulated minutes, 45 tracks, 0 ms silence |
| A | Cold start escalates to Tier 3 and returns | ✅ escalated, recovered, ≥3 generated tracks aired |
| B | Generator dies mid-broadcast; playback continues | ✅ |
| C | Cold start with no reserve goes straight to Tier 3 | ✅ |
| C | Tier 2 is used before Tier 3 | ✅ ordering and reserve accounting |
| D | Generator returns; the station resumes real programming | ✅ |
| E | Market shift replans the future only | ✅ protected programming never discarded unplayed |
| E | A starving station does not replan | ✅ survival outranks fit |
| F | Kill and restart: queue restored, no duplicates, no stuck leases, no false PLAYED | ✅ two stations over one database file |
| G | Scheduler + generator + playout concurrently: nothing duplicated or lost | ✅ |
| G | One job per track | ✅ idempotency |
| H | Virtual time produces exact playback durations | ✅ frame-counted, not clock-derived |
| H | The clock advances exactly as asked | ✅ |
| I | Audio invariants hold across a broadcast | ✅ no clipping, canonical format at the sink |
| I | Every block reaching the sink is canonical | ✅ |
| J | `tradefix soak` drives the real runtime and gates its own exit code | ✅ |

All sixteen pass in one run, and the run is the evidence rather than the summary: each gate
drives the station for between ten and forty-five simulated minutes of real audio, so the file
takes about twelve minutes of wall clock. Three of the four gates that failed on the first full
run failed because the *test* was wrong, not the station — see §6.

### 3.2 The 2-hour soak (milestone 4.10)

Quoted in §1. 160 tracks, 28 genres, no identical genre twice in a row, 68 distinct BPMs,
zero duplicate blueprints, zero duplicate track ids, memory flat at 114 MB after two hours.

### 3.3 The 24-hour accelerated soak

See §9. Run as the same command with `--simulated-hours 24`; it exercises the identical
runtime, which is the whole point of §4.10's instruction that the soak must not be a special
fake implementation.

### 3.4 Unit coverage of the new modules

| File | Tests |
| --- | --- |
| `tests/unit/test_queue.py` | 46 |
| `tests/unit/test_scheduler.py` | 36 |
| `tests/integration/test_generation_manager.py` | 36 |
| `tests/unit/test_buffer.py` | 27 |
| `tests/unit/test_emergency.py` | 26 |
| `tests/unit/test_playout.py` | 24 |
| `tests/unit/test_station_ids.py` | 20 |
| `tests/integration/test_phase4_gates.py` | 16 |
| `tests/unit/test_coordinator.py` | 15 |

All twelve named §4.2 lease scenarios are covered in `test_generation_manager.py`, against a
real SQLite database rather than a fake repository — the lease model is a statement about what
the database guarantees, and a fake would be asserting my own mock.

---

## 4. Module tree added in Phase 4

```
tradefix_radio/
  core/
    job_states.py          224   nine states, transition table, failure taxonomy
    clock.py                 +   hold(), run_to(), real_yield_seconds
  runtime/
    coordinator.py         306   typed bus + supervised tasks + isolation
  generation/
    manager.py             800   leases, retries, timeouts, cancellation, idempotency
    capacity.py            216   §93 ratio, p50/p95, cold-start handling
  radio/
    station.py           1 116   composition root; four tasks; recovery
    playout.py             647   the audio path, and nothing else
    queue.py               588   layered locks, transactional mutation
    scheduler.py           522   buffer targets, replanning, creative risk
    emergency.py           448   three tiers; non-looping procedural source
    station_ids.py         409   configurable records, anti-repetition
    buffer.py              342   level, trajectory, time-to-failure
  audio/
    format.py              115   canonical format; conform() at the boundary
  persistence/repositories/
    jobs.py                669   the conditional-UPDATE claim
    queue.py               189   storage-shaped slots, no radio types
  cli/
    soak.py                699   the accelerated proof
```

---

## 5. Architecture decisions recorded in Phase 4

**ADR-11 — The playout engine gets its own task, exclusively.** Scheduling, generation and
persistence each get their own. Measured alternative: 290 s of audio across 3 600 s of
broadcast, with every underrun counter reading zero.

**ADR-12 — Continuity is measured as audio coverage, not as underruns.** `seconds_on_air /
broadcast_seconds`. Underrun counting answers "was the engine ever asked for audio it did not
have", which is not the same question and reads perfect while audio is missing.

**ADR-13 — Virtual time is made faithful by holds, not by sleeping.** A task declares a leaf of
real work; the clock waits on an event for its release. Sleeping instead cost 70.7 of 75.6
seconds of a six-minute simulated broadcast in event-loop timer waits.

**ADR-14 — The playout format is the sink's format.** `PLAYOUT_SAMPLE_RATE` is the default a
production sink declares, not a constant the engine enforces against every sink.

**ADR-15 — Emergency de-escalation is immediate.** A dwell does not delay a status; it keeps
procedural noise on air while a finished track waits.

**ADR-16 — The soak owns a throwaway database.** An endurance run is a measurement, and a
measurement that mutates the thing it measures is not one.

---

## 6. Defects found by the tests and fixed

Each was found by a measurement, not by reading the code. The figure is what the measurement
said.

| # | Defect | How it showed up |
| --- | --- | --- |
| 1 | Playout shared a task with scheduling and maintenance | **290 s of audio across 3 600 s** of broadcast; underruns reported 0 |
| 2 | Silence measured by underruns alone | zero silence reported while 12 % of the broadcast was missing |
| 3 | Virtual clock stepped over work the playout was about to do | coverage 97.22 %, plus a spurious Tier 3 activation at startup |
| 4 | Track-finish persistence sat on the audio path | coverage 78 % → 88.6 % once moved to its own task |
| 5 | `VirtualClock.sleep` leaked cancelled waiters | `pending_waiters` permanently wrong; `advance_sync` refused to run for the rest of the process |
| 6 | Blind real-time yields, not holds, made the clock faithful | 70.7 s of a 75.6 s profile inside `GetQueuedCompletionStatus`, 15 435 waits, under 2 s of audio work |
| 7 | A hold wrapped an orchestration call, not a leaf | clock deadlocked against its own waiters 11 times in a 360 s run; every generation deadline expired |
| 8 | The station never persisted blueprints | **every** queue slot dropped on restore — queue recovery did not work at all |
| 9 | `mark_ready` wrapped in `contextlib.suppress(Exception)` | generated tracks silently never reached the queue; the log said nothing (§86) |
| 10 | Playout hooks could kill the audio task | a station-side listener that raised propagated out of `pump()` |
| 11 | Station IDs counted as tracks | overstated programming by a sixth; made a gate's arithmetic go negative |
| 12 | `ScheduleDecision.replan_from_position` was always `None` | the decision misreported itself; replans that replaced nothing were counted |
| 13 | A station-ID library no larger than its repeat horizon suppressed everything forever | a one-record library lost its identity permanently, silently |
| 14 | **The soak wrote into the station's own database** | runs inherited each other's history; "1 blueprint duplicate" was a collision against a track from an earlier run |
| 15 | `RuntimeCoordinator.drain` polled `queue.empty()` | returned while a handler was still awaiting; a test asserting on ten events saw nine |
| 16 | Signature-collision retries re-rolled identical weights | 2 repeated blueprints across 54 tracks in a narrow market |
| 17 | Emergency recovery dwelled 30 s | 52 procedural blocks against 5 scheduled tracks, with ready tracks queued throughout |
| 18 | `release_backoffs` requeued while iterating ORM rows | **829 `MissingGreenlet` errors in one run**; the station stopped generating entirely while the scheduler kept planning work nobody claimed |
| 19 | The gate harness closed its event loop without driving the clock through shutdown | a live generation task outlived the loop: `GeneratorExit`, "Event loop is closed" |
| 20 | Gate F's simulated crash never stopped the crashed station's tasks | an abandoned generation task was still suspended when the loop closed; the error was attributed to an unrelated test |

Defect 18 is the clearest argument for the gates existing at all. `release_backoffs` requeues
every job whose retry backoff has elapsed; it did so while iterating the ORM rows it had just
selected, and each requeue flushes, which expires those instances — so reading `row.job_id` on
the next iteration triggered a lazy reload, and a lazy reload from a plain attribute access has
no greenlet to run in.

It is invisible until a provider has actually failed, because nothing enters backoff until
then. **Both soaks passed with zero occurrences**, 1 920 tracks and twenty-four simulated hours
between them, because their generator never fails. Gate D kills the generator on purpose, and
the error fired 829 times in a single run — inside the claim cycle, so no job could be claimed
and the station stopped generating while the scheduler went on planning work nobody picked up.
A green soak is not evidence about the failure paths.

Three were found in the *tests* rather than the code and are worth separating, because a wrong
test is a wrong claim: the gate harness originally modelled a generator slower than playback
(capacity 0.6×, so no assertion about scheduled programming could hold); several gates sampled
an instantaneous queue depth or tier on a station that airs each track as it lands, which is a
coin toss; and the playout unit tests deadlocked because a virtual clock with no driver cannot
service the engine's own sleeps.

---

## 7. Known limitations

Recorded at the point of compromise in code, repeated here.

* **`resample()` is linear interpolation**, not a windowed sinc. Adequate for the mock
  provider's 16 kHz output; it should become `librosa.resample` in Phase 6, where audio
  quality is the subject.
* **Station-ID selection accepts `regime` and `session` and does not use them.** Stated in the
  docstring rather than quietly dropped. Wiring them in before any record declares a regime
  affinity would be inventing a mechanism with no content.
* **Tier 3 is not good music.** It is valid, varied, non-looping audio. The module says so.
* **The soak renders at 16 kHz mono**, not the canonical 48 kHz stereo, because every simulated
  second is real DSP. Gate I pins the production constants separately.
* **Acceleration is CPU-bound at ~52×.** A 7-day run is therefore ~3.2 hours of wall clock.
  Phase 10 is where that becomes the subject; nothing in the design prevents it.
* **Tier 2 ships empty.** `reserve_from_directory` indexes whatever is on disk and the station
  runs correctly with nothing there — it has Tier 3 — but a real deployment should populate it.
* **No end-to-end test of the three-process topology.** Phase 4 runs the station in one
  process. The lease model is what makes the worker horizontally safe, and that is tested; the
  deployment shape is Phase 9's.

---

## 8. Performance

| Figure | Value |
| --- | --- |
| Acceleration, 2 h soak | **52×** (137.5 s wall clock) |
| Generation latency p50 / p95 (mock provider) | 20.0 s / 20.0 s simulated |
| §93 capacity ratio, steady state | 2.25× |
| Memory, 2 h soak | 70 MB → 116 MB peak → 114 MB |
| Transitions in 2 h | 160, zero failures |
| Peak sample at the sink | 0.708 — no clipping |

Memory is the figure that matters for §64: flat across two simulated hours and 160 tracks,
with the queue, history and event bus all bounded by construction.

---

## 9. The 24-hour accelerated soak

```
  TRADE FIX RADIO SOAK REPORT

  Simulated runtime:  24.02h  (17280 steps in 1450.5s wall clock)
  Feed warm-up:       1 min simulated, before the broadcast began
  Track length:       45s (fixed for this run)

  Tracks
    Generated:        1933      Played: 1920      Failed: 0      Skipped: 0

  Audio
    Unintended silence: 0 ms
    Audio coverage:     100.00% (86400s of 86400s)
    Transitions:        1920
    Transition failures: 0
    Peak sample:        0.7079

  Radio
    Queue underruns:    0
    Tier 2 activations: 0 (0.0 min)
    Tier 3 activations: 1 (1.0 min)

  Generation
    p50: 20.0s   p95: 20.0s   capacity ratio: 2.25x   Retries: 0   Timeouts: 0

  Creative
    Genres used: 28   Longest identical genre run: 1   Distinct BPMs: 84
    Blueprint duplicates: 0   Track id duplicates: 0

  Runtime
    Unhandled exceptions: 0   Subscriber errors: 0   Leaked tasks: 0   Pending leases: 0

  Memory
    Start: 70 MB   Peak: 121 MB   End: 120 MB

  PASS: every acceptance invariant held
```

**Twenty-four simulated hours in 24 minutes 10 seconds — 59.6× real time.** Read against the
2-hour run, the figures that matter are the ones that did *not* move:

| | 2 h | 24 h |
| --- | --- | --- |
| Audio coverage | 100.00 % | **100.00 %** |
| Unintended silence | 0 ms | **0 ms** |
| Queue underruns | 0 | **0** |
| Transition failures | 0 of 160 | **0 of 1 920** |
| Blueprint duplicates | 0 | **0 of 1 920 tracks** |
| Capacity ratio | 2.25× | **2.25×** |
| Memory, end | 114 MB | **120 MB** |
| Leaked tasks / pending leases | 0 / 0 | **0 / 0** |

Twelve times the broadcast produced 6 MB more resident memory and no new failure of any kind.
Tier 3 activated once, for one minute, at the cold start — exactly where §33 expects it, and
the station returned to generated programming and stayed there for the remaining 23.98 hours.

§81's criterion 17 ("24 h accelerated, no underrun") and criterion 19 ("no unhandled
exceptions in simulation") are met here, ahead of Phase 10, which is where they were planned.
Phase 10 still owns the 72-hour and 7-day runs and the real-time overnight soak (§65).

---

## 10. Next step

**Phase 5 — API + Control Center.** The runtime now has state worth displaying and every
figure in this report comes from a real counter, which is what §51's "no static fake metrics in
production UI" requires of the backend before a UI exists.

ACE-Step integration remains Phase 7, unchanged.
