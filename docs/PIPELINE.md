# The post-production pipeline

> Generator success is not a playable track.

That sentence from §6.14 is the whole point of this stage. A model producing a file means the
model ran; it says nothing about whether the audio is broadcastable, distinct from what already
aired, or at the right level. Those are separate questions with separate answers, and the
station must not conflate them.

**A track that fails this pipeline never becomes READY for live radio.** That is enforced in
code and in tests, not assumed.

## The chain

```
GENERATOR OUTPUT
      ↓  decode
      ↓  feature extraction          docs/AUDIO_QC.md
      ↓  AUDIO QC (raw)              → QC_REJECTED
      ↓  canonical hash + fingerprint
      ↓  ORIGINALITY / SIMILARITY    docs/ORIGINALITY.md  → ORIGINALITY_REJECTED
      ↓  MASTERING                   docs/MASTERING.md    → MASTERING_FAILED
      ↓  AUDIO QC (mastered) + loudness drift             → FINAL_QC_REJECTED
APPROVED TRACK
```

Order is a correctness property, not an optimisation. QC runs first because mastering a silent
file produces a failure with a confusing reason, and fingerprinting it would put a meaningless
entry in the library that every later candidate is then compared against.

## Stages and the state machine (§6.13)

```
QC_PENDING → QC_PASSED → ORIGINALITY_PENDING → ORIGINALITY_PASSED
           → MASTERING → FINAL_QC → APPROVED
```

with terminal branches `QC_REJECTED`, `ORIGINALITY_REJECTED`, `MASTERING_FAILED`,
`FINAL_QC_REJECTED` and `QUARANTINED`.

Transitions are table-driven and **an illegal transition raises `IllegalPipelineTransition`**.
Tolerating one would let a track reach `APPROVED` without the stages in between having run —
invisible, because the end state would look correct.

These stages are deliberately **not** a replacement for Phase 1's `TrackState`. They are
finer-grained steps *within* `ANALYZING → APPROVED → MASTERING → READY`, so the §27 transition
trail stays intact and the Phase 4 state machine is untouched.

## Rejection reasons

| reason | stage | meaning |
|---|---|---|
| `qc_defect` | raw QC | the audio is broken |
| `exact_duplicate` | originality | byte-identical decoded audio already exists |
| `audio_near_duplicate` | originality | similarity above the rejection threshold |
| `lyric_similarity` | originality | the lyrics repeat an existing track |
| `blueprint_repetition` | originality | the creative decision repeats, within the recency window |
| `review_unresolved` | originality | borderline, and autonomous mode does not leave tracks in limbo |
| `master_failure` | mastering | FFmpeg could not produce a master |
| `final_qc_defect` | final QC | mastering produced something unsound, or missed its target with headroom to spare |

Rejections do not raise. `process()` returns a `PipelineOutcome` carrying the stage, the
reason, a sentence an operator can act on, and every measurement behind it.

## What the station does with the answer

`RadioStation._run_post_production` runs on the **generation worker's task** — not the step
loop and not playout. That is the right place: post-production is part of producing a track,
and ADR-11 gives playout its own task precisely so a long operation here cannot interrupt the
broadcast.

**Approved** → states advance `APPROVED → MASTERING`, the queue slot is pointed at the
*master* (not the raw render — playing the unmastered file would make every loudness guarantee
cosmetic), and only once the queue accepts it does the track reach `READY`.

**Rejected** → the track goes to `REJECTED`, or `QUARANTINED` when the audio itself is broken
so the file is kept for inspection. The queue slot is **removed with `force=True`** so the
scheduler replans into the gap. §28's positional lock protects a slot from *reprogramming*; it
was never meant to compel the station to keep a track that cannot play, and without the force
flag a rejection at position 0 left an unplayable slot at the head of the queue — the dead air
this phase exists to prevent.

Either way the evidence is persisted in one transaction. A partial write would read as though
a stage had run and said nothing, which is worse than no record.

The pipeline is **optional** on the station. Without it attached, the Phase 4 and Phase 5
behaviour is exactly what those phases proved; with it attached, §6.14's rule applies.

## Measured cost

180 s of 44.1 kHz stereo, on this machine, steady state:

| stage | time | share |
|---|---|---|
| decode | 0.03 s | 0.1 % |
| feature extraction | 6.11 s | 22.1 % |
| QC (21 checks) | 0.001 s | 0.0 % |
| hash + fingerprint | 0.44 s | 1.6 % |
| similarity (empty library) | <0.001 s | 0.0 % |
| mastering | 15.91 s | 57.5 % |
| final QC | 5.17 s | 18.7 % |
| **total** | **27.66 s** | 6.5× real time |

At one three-minute track every three minutes that is **15.4 % of one core**.

librosa's numba kernels compile on first use — 29 s for MFCC and 15 s for beat tracking, cold.
The pipeline pays that once at construction via `warm_up()` rather than letting it land on the
station's first real track, which is exactly when the buffer is emptiest.

## Proving it

```console
# Unit and property tests
$ pytest tests/unit/test_audio_qc.py tests/unit/test_originality.py \
         tests/unit/test_mastering.py tests/unit/test_originality_properties.py

# End to end, including the station boundary
$ pytest tests/integration/test_phase6_pipeline.py

# Two simulated hours with 20% corrupted and 10% duplicated output
$ tradefix soak --simulated-hours 2 --post-production \
      --invalid-rate 0.2 --duplicate-rate 0.1
```

The soak asserts the thing that matters: **no deliberately broken track reached air**, computed
against the played history rather than a counter — a counter can be right while the wrong track
is on the radio.
