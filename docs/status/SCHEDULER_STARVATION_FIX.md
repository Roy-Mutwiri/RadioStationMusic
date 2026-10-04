# Scheduler starvation: a dead queue slot that counted as supply

**Severity:** production-critical. The station can stay on Tier 3 procedural fallback
indefinitely while reporting substantial pending audio.

**Found:** live, during a listening session on 2026-10-04. Not by a test.

---

## 1. What happened

The station was on Tier 3 procedural for an entire session. The Control Center reported:

```
READY   0.0 min
PENDING 13.7 min
LEVEL   empty
```

Those two lines are contradictory and nobody noticed, because "pending 13.7" reads like
work in flight. It was not. The queue held five slots in `planned` whose generation jobs
had **all failed terminally** seven hours earlier:

```
TF-20261003-00180   queue=planned  track=planned  job=failed
TF-20261003-00181   queue=planned  track=planned  job=failed
TF-20261004-00237   queue=planned  track=planned  job=failed
TF-20261004-00239   queue=planned  track=planned  job=failed
TF-20261004-00240   queue=planned  track=planned  job=failed
```

Those slots could never become playable. Nothing re-planned a job for them and nothing
retired them. But `pending_minutes` counted their durations, so the scheduler believed
13.7 minutes of audio was on its way and planned no replacements. The station was starved
and its own instruments said it was busy.

## 2. Root cause

Three separate correct-looking decisions combined into a trap.

1. **`pending_minutes` was derived arithmetically**, as
   `queue.total_seconds() - queue.ready_seconds()`. That is "everything not yet playable",
   which silently includes "everything that can never be playable".
2. **Nothing related a queue slot to its generation job.** `ReadinessState`
   (`PENDING`/`READY`/`UNAVAILABLE`) describes a slot's *usability* and deliberately knows
   nothing about jobs; `TrackState` describes a track's *progress*. Neither answers "is
   there still a credible path to audio here", and that question had no owner.
3. **Recovery's orphan sweep did not see them.** It drops slots whose track rows are
   missing or terminal (added in B4). These tracks were `planned` — a perfectly normal,
   non-terminal state — so the sweep passed over them every restart, faithfully restoring
   five permanently dead slots.

The underlying error is the one this codebase keeps producing in different costumes: a
quantity was *computed* rather than *asserted*. `total - ready` is an arithmetic identity,
not a statement about supply, and it stayed true while becoming meaningless.

## 3. State diagram

```
                 queue slot (PLANNED)
                          |
             latest generation job state?
                          |
   +----------------+-----+------+-----------------+
   |                |            |                 |
PLANNED/QUEUED   GENERATED    FAILED /          (no job)
LEASED/GENERATING    |        CANCELLED /            |
RETRY_PENDING        |        ABANDONED              |
   |                 |            |                 |
 VIABLE           VIABLE    TERMINALLY_FAILED    MISSING_JOB
   |              (audio          |                 |
   |              exists,         |                 |
   |              in post)        |                 |
counts as        counts as    counts as 0       counts as 0
pending          pending      + retire slot     + repair
                              + replan          + replan
```

`GENERATED` is deliberately on the viable side. The job is terminal but *successful* — the
audio exists and post-production is the remaining path — so excluding it would have swung
the bug the other way and discarded tracks that were nearly ready.

## 4. Old behaviour vs new

| | before | after |
|---|---|---|
| `pending_minutes` | every non-ready slot | only slots with a viable path |
| dead slot | counted as supply, forever | counted as 0, retired, replaced |
| buffer readout | `READY 0 / PENDING 13.7 / empty` | `READY 0 / PENDING 0 / DEAD 5 / empty` |
| scheduler | planned nothing (believed busy) | plans replacements immediately |
| Tier 3 reason | "buffer empty" | "no viable ready or pending tracks" |
| recovery | restored dead slots verbatim | reconciles each slot against its job |

*(Sections 5 onward — reproduction, runtime recovery, Tier 3 behaviour and tests — are
completed as the fix lands.)*
