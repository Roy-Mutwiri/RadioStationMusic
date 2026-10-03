# B2 — REVIEW resolution

REVIEW had no consumer. `OriginalityVerdict.REVIEW` was converted to `REJECT` by a setting
named `regenerate_on_review` that never regenerated anything, and 43% of everything the
station generated was discarded by a workflow that did not exist. The real station test
measured the result: a 7% approval rate and an hour of procedural fallback.

This is what replaced it, and the evidence each decision rests on.

---

## 1. Chromaprint calibration

Full table in [CHROMAPRINT_CALIBRATION.md](CHROMAPRINT_CALIBRATION.md). One recording,
twelve modifications of itself, eight different recordings.

| population | range |
|---|---|
| same recording, **time-aligned** — copy, WAV rewrite, FLAC, MP3 192k, ±gain, normalise, EQ shelf, 4:1 compression, trailing pad | **0.9068 – 1.0000** |
| same recording, **time-shifted** — 0.5 s lead, 5 s head crop | 0.6222 – 0.6829 |
| different recordings | 0.4733 – **0.6404** |

The aligned population clears the different-recording population by **+0.2664**. That gap
is where `CERTAIN_DUPLICATE = 0.90` sits — below the quietest same-recording variant
(0.9068) and far above the loudest different one (0.6404).

The shifted variants overlap the different-recording range, so agreement there cannot
decide alone. `SUSPECTED_DUPLICATE = 0.65` marks that band — just above the 0.6404
ceiling — and requires corroboration rather than deciding. In practice alignment is
preserved because originality runs on raw audio before mastering trims anything.

No threshold was guessed.

## 2. MFCC

Full table in [COMPONENT_AUTHORITY.md](COMPONENT_AUTHORITY.md). 10 440 pairs.

| component | all pairs | same-genre delta | same-BPM delta |
|---|---|---|---|
| fingerprint | 0.511 ±0.021 | **+0.003** | **+0.002** |
| **mfcc** | **0.971 ±0.026** | **+0.007** | **+0.003** |
| chroma | 0.308 ±0.271 | +0.078 | +0.034 |
| tempo | 0.397 ±0.360 | **+0.450** | **+0.598** |

**MFCC answers ~0.97 for every pair in the corpus.** Its approve-to-reject spread is
0.014. It is not detecting a production family either — the genre delta is +0.007. It is
simply saturated on one generator's output, which is the condition this station
permanently operates in, and a measure that returns the same answer for everything cannot
be evidence about anything.

It was nonetheless the deciding component in 51% of verdicts, precisely *because* a
saturated component is always the closest one.

**Decision: no authority in duplication.** It remains available to the diversity director,
which exists to vary consecutive programming and for which timbral family is a legitimate
signal. One metric is not being asked to solve two problems.

## 3. Tempo

Tempo moves **+0.598** between close and distant BPM bands and **+0.450** between genres.
It is close to a restatement of the brief: the director picks BPM from a narrow band for a
given genre and energy, so agreement is near-guaranteed by design. Two different songs at
148 BPM are two different songs.

**Decision: supporting context and rotation only.** No authority in duplication.

## 4. Architecture — two scores, assessed and adopted

The refactor was assessed against the instruction not to explode scope. Adopted in an
**additive** form: `SimilarityComponents` gained two properties and nothing was removed,
so every existing caller, stored row and API field still works.

```
duplication_risk     = audio_fingerprint          "is this the same recording"
creative_similarity  = max(embedding, chroma, mfcc, tempo, blueprint)
                                                  "does this sound like what we just played"
```

`duplication_risk` is the fingerprint alone. A first version blended in
`min(embedding, chroma)` as corroboration and reported a **mean risk of 0.849** across a
corpus measured to contain no duplicates, because both of those saturate on same-genre
material. It manufactured the alarm it was meant to qualify. Corroboration is now a *rule*
in the resolver, applied only to a fingerprint that is already suspicious — never a score.

With that corrected, over the same corpus:

* duplication risk — mean **0.533**, max **0.613** (entirely inside the measured
  different-recording band)
* creative similarity — mean **0.987**, max **1.000**

That pair of numbers is the whole finding, and a single scalar could not express it.

## 5. ReviewResolver

`tradefix_radio/originality/review.py`. Deterministic, pure, versioned
(`RESOLVER_VERSION = 1.0.0`). Rules in order:

| # | condition | disposition | evidence class |
|---|---|---|---|
| 1 | exact content / canonical hash | FINAL_REJECT | `definitive_duplicate` |
| 2 | fingerprint ≥ 0.90 | FINAL_REJECT | `strong_recording_match` |
| 3 | fingerprint ≥ 0.65 **and** embedding ≥ 0.97 **and** chroma ≥ 0.95 | FINAL_REJECT | `corroborated_recording_match` |
| 4 | no production history | FINAL_APPROVE | `no_production_history` |
| 5 | creative similarity ≥ 0.93 against production aired < 6 h ago | FINAL_REJECT | `rotation_pressure` |
| 6 | otherwise | FINAL_APPROVE | `style_only` |

Rules 1–3 are duplication. Rule 5 is rotation and says so in its reason — it never claims
duplication. Rule 6 names the style signal that flagged the candidate.

`NEEDS_HUMAN_REVIEW` exists in the enum for a supervised workflow and is never returned by
the autonomous path: a station with nobody watching cannot leave a track undecided, and
candidates with no terminal disposition were the original defect.

## 6. Resolution of the 120 REVIEW candidates

Full output in [REVIEW_RECOVERY.md](REVIEW_RECOVERY.md).

| | |
|---|---|
| FINAL_APPROVE | **120** |
| FINAL_REJECT | **0** |

By evidence:

| evidence class | count |
|---|---|
| `style_only` | **120** |

By what had flagged them in the first stage:

| flagged by | approved as style-only |
|---|---|
| mfcc | **73** |
| tempo | **47** |

Not one was approved for a vague reason. Every one was approved because the similarity was
timbre or tempo — the two components measured above to carry no duplication authority —
while the fingerprint placed it inside the different-recording band.

**120 of 120 passing is not evidence the resolver works.** This corpus was measured to
contain no duplicates: the maximum fingerprint agreement across 10 440 pairs was 0.643
against a same-recording floor of 0.907. A resolver that approved everything would score
identically here. Which is why the next section exists.

## 7. The intentional-duplicate test

`tests/unit/test_review_resolver.py`, 24 tests. The duplicate variants are parametrised at
**their measured agreements from the calibration run**, not at invented values, so the
test fails if the threshold ever drifts above the quietest real variant.

Rejected, as they must be:

| variant | agreement |
|---|---|
| exact copy / WAV rewrite / FLAC / ±gain / normalised | 1.0000 |
| MP3 192k | 0.9984 |
| EQ shelf | 0.9196 |
| dynamics compressed | 0.9068 |
| time-shifted copy with structural corroboration | 0.68 + embedding 0.985 + chroma 0.97 |
| exact duplicate aired 900 days ago | — recency must not rescue it |

Accepted, as they must be:

| case | why |
|---|---|
| two lo-fi tracks 1 BPM apart, mfcc 0.99, tempo 1.00 | fingerprint 0.61 — not the same recording |
| mfcc 0.999 alone | saturated component cannot discard a recording |
| tempo 1.000 alone | two songs at one BPM are two songs |
| fingerprint 0.6404 unaided | the highest any different recording reached |
| same similarity, aired 4 days ago | rotation is recency-scoped |
| very similar bench track aired 5 minutes ago | never aired — no rotation pressure |

Plus the property the whole of B2 exists to guarantee: **every REVIEW reaches a terminal
disposition**, driven across 7 fingerprint values × 3 reference counts × 3 air times.

## 8. Provenance behaviour

Policy as accepted, unchanged. Exact and fingerprint checks span every class; graded
creative novelty is PRODUCTION_RADIO only; UNKNOWN is excluded and never silently
promoted.

**When a track becomes PRODUCTION_RADIO** is now defined precisely:
`TrackRepository.promote_to_production`, called from `_record_finished` only when playout
actually began **and** the run mode is production. Not on generation, approval, queueing
or preview. A station generates far more candidates than it airs, and promoting any
earlier refills the novelty library with music nobody heard — the exact shape of the
defect where 154 bench tracks aged real output.

**Recency** applies to creative similarity only, over a 6-hour horizon and the 20 most
recent production tracks. It never applies to exact duplicates: a duplicated recording
stays duplicated forever, and there is a test for that.

## 9. Cold start

The provenance-filtered run approves 145/145. That is **not** a quality result and the UI
no longer lets it read as one. `OriginalitySummaryV1` gained `production_references` and
`cold_start`, and the Originality page renders:

> **Novelty history: COLD START.** No track has aired on the production station yet, so
> there are 0 reference recordings for creative similarity. Approvals below reflect that
> absence, not a judgement about quality. Duplicate detection is unaffected.

## 10. Persistence

`similarity_results` gained `final_disposition`, `evidence_class`, `resolution_reason`,
`resolver_version`, `duplication_risk`, `creative_similarity`, `production_references`.

`verdict` keeps the **first-stage** answer and is never overwritten. A candidate that
entered REVIEW and was approved still shows that it entered REVIEW. `resolver_version` is
stored because a disposition is uninterpretable without it — "approved" means something
different under different rules.

## 11. Migration preservation

Permanent policy, documented in [../MIGRATIONS.md](../MIGRATIONS.md).

A relational fixture populates `tracks` plus every child table — including
`track_qc_checks`, which hangs off `track_qc_results` and so exercises a two-level chain —
records the counts, migrates, and verifies every row survived and no foreign key dangles.
Both directions are covered: `test_migrating_to_head_preserves_every_child_row` and
`test_downgrading_one_revision_preserves_child_rows`.

The lost QC and mastering rows were **not** reconstructed from the report summaries. They
are gone and the absence is left visible. Fingerprints and features *were* recomputed,
explicitly, because the audio survived and they needed recomputing with Chromaprint
anyway — 145 of 154, 0 failures, 9 skipped for missing audio.

## 12. Before and after

| | before | after |
|---|---|---|
| REVIEW consumer | none — silently rejected | `ReviewResolver`, versioned and explainable |
| first-stage reject | 63 (46%) | 15 (10%) |
| REVIEW discarded | 62 | **0** |
| fingerprint contributing | 0 / 134 | 145 / 145 |
| duplication risk reported | not separable | mean 0.533 |
| creative similarity reported | not separable | mean 0.987 |
| thresholds changed | — | **none** |

`reject_similarity` 0.84 and `review_similarity` 0.72 are untouched.

## 13. Not yet measured

**Effective approved capacity is not reported here.** It requires real generation against
the new resolver, and the honest measurement needs B3 first: the `lyrics` component is
still absent for every candidate, which is 10% of the weight and a second identity-bearing
signal. Measuring capacity now would produce a number that changes as soon as the next fix
lands.

The target stands: > 1.0× to call the station sustainable, > 1.5× preferred. It will be
measured in the ready-buffer run after B3, where it can be measured once and meant.

`regenerate_on_review` is now unused by the pipeline and left in configuration pending the
REGENERATE disposition, which rule 5 currently handles as a rejection. Worth revisiting
when the generator can re-roll a blueprint cheaply.
