# ARCHITECTURE

**As built through Phase 4.** Phases 5–10 add the control centre, the real generator, OBS and
the endurance harness; none of them change the shape described here, which is the point of
describing it.

The README has the one-screen version. This document covers the parts that are not obvious
from the module names: where the boundaries are, which invariants hold where, and why the
runtime is split the way it is.

---

## 1. The one rule that shapes everything

> §1 — "THE MARKET COMPOSES THE RADIO."

Market state flows one way. A `MarketStateV1` becomes a `MusicBlueprintV1`, and **nothing
downstream of the blueprint is market-aware**. The generator does not know what gold did; it
knows a genre, a BPM, a key, a structure and a seed.

That boundary is what makes a second generation backend an adapter rather than a rewrite, and
it is enforced by the type system: `MusicGenerationProvider.generate` takes a
`GenerationRequest` whose only creative input is a blueprint.

The reverse boundary is equally deliberate: **nothing upstream of the blueprint is
model-specific.** The director has no idea that ACE-Step exists.

---

## 2. Layers

Dependencies point downward only. The import graph is acyclic and `mypy` is configured to
keep it that way.

```
  cli/                 commands: doctor, init, migrate, config, market-sim,
                       report-director, soak
  ─────────────────────────────────────────────────────────────────────────
  radio/               station.py — the composition root
                       scheduler · queue · buffer · playout · emergency · station_ids
  ─────────────────────────────────────────────────────────────────────────
  runtime/             coordinator: typed event bus + supervised tasks
  generation/          manager (leases, retries, timeouts) · capacity · provider · mock
  director/            music_director · lyrics · diversity · energy_curve · library
  market/              service · feeds · features · regimes · simulation
  ─────────────────────────────────────────────────────────────────────────
  persistence/         database · models · repositories · migrations
  audio/               pcm · mixer · synthesis · io · sinks · format
  contracts/           versioned pydantic models shared across every layer
  core/                clock · errors · events · state machines · job states
  config/              layered settings (defaults → mode → env → overrides)
  monitoring/          structured logging · health
```

Two boundary rules earned their place by being broken first:

**Persistence knows nothing about the radio layer.** `QueueRepository` stores a
`PersistedQueueSlot` of plain strings, and `radio/station.py` converts. The first version had
the repository import `QueueEntry`, which produced an import cycle
`persistence → radio → generation → persistence`. The cycle was the symptom; the cause was a
storage layer reaching up into a domain layer.

**The playout format is the sink's format.** `audio/format.py` declares the canonical
48 kHz stereo float32 default and a single `conform()` that converts at the *input* boundary.
The engine then renders in whatever the sink declares. Hard-coding the constants inside the
engine meant it rejected every other sink, which made an accelerated endurance run pay for
full-rate stereo DSP on audio nobody listens to.

---

## 3. The runtime: four tasks, one rule each

`RadioStation` is the composition root. It owns no loop of its own; it spawns four supervised
tasks on the coordinator and each has exactly one job:

| Task | Does | Never does |
| --- | --- | --- |
| `playout` | writes one block of audio to the sink, forever | database work, generation, scheduling |
| `generation-worker` | claims one job, generates it, accepts the result | touch the audio path |
| `scheduler` | plans, replans, maintains, on a timer | block on generation |
| `persistence` | drains a bounded queue of finished tracks into the database | sit on the audio path |

**This split is not tidiness; it is the continuity mechanism.** An earlier version interleaved
the pump with scheduling and maintenance in one `step()`. While that task was doing database
work the sink was not being fed, so a virtual clock advanced over time no audio covered:
measured, **290 seconds of audio written across 3 600 seconds of broadcast** — and because the
silence counter only looked for *underruns*, it reported zero. Separated, the engine is always
either writing a block or sleeping on the sink.

The same lesson applies one level down. Recording a finished track is three database writes;
done inline on the audio path it cost 12 % of a broadcast's coverage. It now goes onto a
bounded `asyncio.Queue` (`FINISHED_QUEUE_SIZE = 256`) that the persistence worker drains. If
that queue ever fills, the *record* is dropped with a loud error and the broadcast continues —
the broadcast wins, deliberately and visibly.

### Events

`runtime/coordinator.py` wraps the `core/events.py` bus with supervision. The contract is
narrow on purpose (§4.3: "do not create an overengineered distributed message bus yet"):
strongly typed domain events, in-process, ordered per publisher, with two guarantees that
matter more than features.

* **`publish` never raises for a subscriber's sake.** A handler that throws is logged with a
  traceback and counted against its name; publication continues. Cancellation is *not*
  counted as an error, because treating shutdown as a fault makes every clean stop look like
  one in the logs §57 alerts on.
* **A crashing task does not take the station down.** `spawn` supervises; a task that dies is
  counted and logged, and the bus keeps working.

Shutdown order is tasks first, bus second. Reversed, a component publishing during its own
teardown would meet a closed bus and log an error on every clean shutdown.

---

## 4. Generation: leases, not locks

§70's mechanism, implemented literally. A job row carries an owner and an expiry. **Nothing
detects a crash** — the lease simply runs out, and the job becomes claimable again.

Single-claimant is enforced by the database, not by the application:

```sql
UPDATE generation_jobs SET state = 'leased', owner = ?, lease_expires_at = ?
 WHERE job_id = ? AND state IN ('queued', 'retry_pending')
```

The loser of a race sees zero affected rows and moves on. There is no lock, no coordination
service, and no window in which two workers believe they own the same job.

Nine states (`PLANNED → QUEUED → LEASED → GENERATING → GENERATED`, plus `FAILED`,
`CANCELLED`, `RETRY_PENDING`, `ABANDONED`) with an explicit transition table. Illegal
transitions raise rather than silently doing nothing.

`GenerationOutcome` is **returned, never raised**. A generator failure is an expected event in
a station designed to run for weeks, and making it an exception would put the burden of
remembering to catch it on every call site — including the one on the audio path.

### Capacity (§93)

`generation_capacity_ratio = audio_duration_generated / generation_wall_time`, aggregated
**per job** rather than as totals over totals. Totals would credit concurrency: four workers
each at 0.5× would report 2.0× and look healthy while the buffer drained.

---

## 5. The queue and its locks (§28)

Lock level is a **function of position and readiness**, recomputed after every mutation —
never stored and restored. A restored lock would be a lock computed against a queue that no
longer exists.

```
position 0..locked_slots-1        LOCKED          cannot move, cannot be replaced
next semi_locked_slots            SEMI_LOCKED     may be reordered, not replaced
the rest                          REPLACEABLE     a replan may take these
any position                      OPERATOR_PINNED a human said so; outranks all of the above
```

Mutations run inside `transaction()`, which rolls back both the entry list and the playing
slot on exception and then recomputes locks. The invariant that matters: **the currently
playing track is not in the queue at all.** It leaves the list and lives in `playing`.
Keeping it in place with an index makes every length, duration and position calculation
conditional on whether index 0 is playing — and one of those conditions is always wrong.

---

## 6. No dead air: three tiers (§33)

```
Tier 1  SCHEDULED          the queue. Generated programming.
Tier 2  EMERGENCY_RESERVE  approved, pre-validated files. Independent of the generator —
                           a reserve that needed the generator would be useless in the
                           one situation it exists for.
Tier 3  PROCEDURAL         synthesised on demand. Needs no files, no model, no disk.
```

Escalation and recovery are both **immediate**. The recovery direction took a measurement to
get right: it originally dwelled 30 seconds to stop a flapping queue producing constant tier
changes, which is sound reasoning and the wrong behaviour — withholding Tier 1 does not delay
a *status*, it keeps procedural noise on air while a finished track waits. On a thin queue the
dwell could never elapse anyway, because the one ready track was consumed before 30 seconds
were up and consumption reset the timer. Generated music now wins the moment it exists, and a
station that genuinely alternates is *reported* as alternating rather than smoothed.

Tier 3 must not sound like a loop (§33, explicitly). Four cycles at different periods — seed
per block, tonal centre every fourth, density on a sine of period 7, alternating section
lists — so the combination does not recur on any short cycle. It is not good music. It is
audio a listener reads as "the station is in a quiet mode" rather than "the station is stuck".

---

## 7. Time: the injectable clock

Every subsystem that cares about time takes a `Clock`. This exists for one concrete reason:
§64 requires 24 h / 72 h / 7 d endurance runs that finish in minutes.

`VirtualClock` advances only when work is waiting. Two mechanisms make it *faithful* rather
than merely fast, and both were added in response to measurements:

**`hold()`** — a task declares that it is mid-cycle and time must not pass. A task between
sleeps is *runnable* and holds no waiter, so a driver advancing virtual time can step straight
over an interval that task was about to claim; for the playout engine, skipped time is audio
never written. Measured: audio coverage went from 97.22 % to 100.00 % once the engine held the
clock, with every underrun counter reading zero in both runs.

A hold is **suspended for the duration of any sleep the holder makes**, which is what makes it
safe — the naive version deadlocks the instant a holder sleeps, and the playout engine's sink
paces the broadcast by sleeping from inside the cycle.

A hold may only wrap a **leaf** of real work: something synchronous, or a thread. Wrapping an
orchestration call deadlocked the clock against its own waiters, because the awaited child
tasks needed virtual time to advance. The holders are therefore the provider (around its
render), the playout engine (around one block), and `Database.session` (around one unit of
work — aiosqlite runs every statement on a thread, so a query is exactly a leaf of real work).

**Where time comes from.** `now()` for timestamps, `monotonic()` for durations. Never
`now()` differences for elapsed time: that moves under NTP and DST.

### What accelerated time cannot do

It cannot accelerate **work**. A simulated hour still requires an hour of audio to be
synthesised, resampled and mixed. The achieved figure is **~52× real time** for a 2-hour soak,
and that is a CPU bound, not a scheduling one.

The honest failure mode here is worth recording. Making the simulation faithful by *sleeping*
more worked and was a bad trade: a profile of a six-minute simulated broadcast spent **70.7 of
its 75.6 seconds** inside `GetQueuedCompletionStatus` across 15 435 waits, against under two
seconds of actual audio work — Windows' event-loop timer granularity is ~15 ms, so every "1 ms"
yield waited fifteen. Holds replaced the polling with an event, and the same run took 19.7 s.

---

## 8. Measuring continuity honestly

Three metrics, because the first two can both read perfect while audio is missing.

| Metric | Measures | Blind to |
| --- | --- | --- |
| `underruns` | the engine was asked for audio and had none | time it was never asked |
| `unintended_silence_seconds` | blocks the sink rejected or that did not exist | the same |
| **`seconds_on_air / broadcast_seconds`** | what actually reached the sink | nothing that matters |

Coverage is the one that found real defects. It is why the soak gates on it, and why the
threshold is 98 % rather than 100 % — a run stops on a clock boundary that can fall mid-block.

---

## 9. Crash recovery (§75)

Recovery **classifies**; it does not blanket-fail. On start the station:

1. classifies interrupted jobs — expired leases are reclaimed, work in flight becomes
   `ABANDONED`, backoffs are released;
2. loads persisted queue slots and **drops any track that already aired** (§4.9: a played
   track must not return as new programming);
3. verifies each slot's audio actually exists on disk, marking the rest `UNAVAILABLE` rather
   than removing them — a silently shorter queue makes a storage fault look like a short
   queue;
4. discards the interrupted track, which was mid-air and whose position is unknowable;
5. restores director memory (§96), station-ID history and the emergency reserve position.

The emergency **tier is deliberately not restored.** It is a conclusion about *right now*, and
a restart has not yet looked at the queue. Restoring `PROCEDURAL` would keep a recovered
station on cover it no longer needs.

---

## 10. Processes (ADR-08)

Three processes in production: API, worker, playout. One in development mode (§72). They share
the database and nothing else; the event bus is in-process per process, and job leases are
what make the worker horizontally safe.

Nothing here requires Redis, a broker or a coordination service. §88's extension points exist
so that remains a choice rather than a rewrite.

---

## 11. Deliberate limitations

Recorded in code at the point of compromise, repeated here so the list is in one place.

* **`resample()` is linear interpolation.** Adequate for the mock provider's 16 kHz output;
  it should become `librosa.resample` in Phase 6, where audio quality becomes the subject.
* **Station-ID selection ignores `regime` and `session`.** Both are accepted and documented as
  unused: wiring them in before any record declares a regime affinity would be inventing a
  mechanism with no content.
* **Tier 3 is not good music.** It is valid, varied, non-looping audio.
* **The soak renders at 16 kHz mono.** The canonical production format is 48 kHz stereo; the
  endurance run deliberately uses a cheap one, and Gate I pins the production constants
  separately.
* **No copyright guarantee.** The originality engine prevents the station repeating *itself*.
  Nothing in it is evidence of legal originality, and nothing claims to be.

---

## 12. Where to look

| Question | File |
| --- | --- |
| How does a market state become a track? | `director/music_director.py` |
| Why did the station play *that*? | the blueprint's `rationale`, built as it decides |
| How does it survive a dead generator? | `radio/emergency.py`, `radio/playout.py` |
| How does it survive a crash? | `radio/station.py::recover` |
| Why can't two workers run one job? | `persistence/repositories/jobs.py::claim` |
| How is a week simulated in minutes? | `core/clock.py`, `cli/soak.py` |
| What are the acceptance gates? | `tests/integration/test_phase4_gates.py` |
