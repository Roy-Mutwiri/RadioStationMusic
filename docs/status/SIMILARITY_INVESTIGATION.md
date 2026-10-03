# Similarity investigation — why the station approved 7%

Diagnostic over the 135-candidate corpus the real station test produced. Written before any
code changed, because the conclusion reorders the fix list.

**Answer to the critical question: yes, definitively.** The station is penalising expected
genre similarity as if it were track duplication. The evidence is not a judgement call.

---

## 1. No candidate was ever found to be an actual duplicate

```
audio_fingerprint == 0.0 on the closest match:  134 / 134  (100%)
```

`audio_fingerprint` is the only component that identifies a duplicate *recording*. For every
single candidate it reported "unrelated" — and 125 of 135 were still reviewed or rejected.

## 2. The deciding component is almost always a genre encoder

| deciding component | count | share |
|---|---|---|
| **mfcc** (timbre) | 71 | **53%** |
| **tempo** | 50 | **37%** |
| none | 10 | 7% |
| blueprint | 3 | 2% |
| chroma | 1 | 1% |

90% of dispositions are decided by timbre and tempo — which encode *what genre this is*, not
*which song this is*.

## 3. MFCC cannot tell an approved track from a rejected one

Component means on the closest match, by verdict:

| verdict | mfcc | embedding | chroma | tempo | blueprint | fingerprint | lyrics |
|---|---|---|---|---|---|---|---|
| approve | **0.969** | 0.701 | 0.435 | 0.604 | 0.372 | 0.000 | 0.000 |
| review | **0.975** | 0.867 | 0.761 | 0.694 | 0.410 | 0.000 | 0.000 |
| reject | **0.983** | 0.927 | 0.871 | 0.848 | 0.596 | 0.000 | 0.000 |

**MFCC's spread between approve and reject is 0.014.** It reads ~0.98 for everything. It is
noise as a discriminator, yet it carries 21% of the effective weight and is named the
deciding component in over half of all cases — precisely because a saturated component is
always the closest one.

The components that *do* discriminate are chroma (spread 0.44), tempo (0.24), embedding
(0.23) and blueprint (0.22).

## 4. The mechanism: 32% of the weight is silently redistributed onto genre

Configured weights:

```
audio_fingerprint 0.22   embedding 0.26   chroma 0.14   mfcc 0.14
tempo 0.06               lyrics 0.10      blueprint 0.08
```

Two inputs are absent for every candidate, and the engine renormalises over what remains —
correct behaviour, documented, and the reason an instrumental is not punished for having no
lyrics. But *which* two are absent matters enormously:

* **`audio_fingerprint` (0.22)** — `fpcalc` is not installed, so the provider is
  `chroma-builtin`, which **deliberately publishes no vector**. Its own comment explains
  why: the vector would be `features.embedding()`, already compared as the `embedding`
  component, and publishing both would score one measurement twice under 0.48 of combined
  weight. Sound reasoning. The consequence is that this provider offers only an exact-match
  check, so for any non-identical pair the component is *not comparable* and drops out.
* **`lyrics` (0.10)** — absent because of **B3**: no lyric is ever composed.

Verified arithmetic on the top pair:

```
all 7 components, fingerprint+lyrics scored 0.0  -> 0.6023
fingerprint+lyrics treated as MISSING, renormalised -> 0.8857
stored score                                        -> 0.8857   ✓
```

Effective weights once 0.32 is redistributed:

| component | nominal | **effective** |
|---|---|---|
| embedding | 0.26 | **38%** |
| chroma | 0.14 | **21%** |
| mfcc | 0.14 | **21%** |
| blueprint | 0.08 | **12%** |
| tempo | 0.06 | **9%** |

Every surviving component is a genre/timbre/tempo encoder. The duplication-specific signal
is gone, and 100% of the decision rests on "does this sound like the same kind of music".

## 5. What that does to the distribution

```
0.00-0.60    2  #
0.60-0.72    8  ####
0.72-0.78   17  ########          <- REVIEW
0.78-0.84   45  ######################   <- REVIEW
0.84-0.90   47  #######################  <- REJECT
0.90-1.01   16  ########                 <- REJECT
```

Mean 0.816, against a 0.72 review / 0.84 reject band. 92 of 135 land in a 0.12-wide strip
straddling the reject line.

Same-genre closest match: mean 0.865. Cross-genre: mean 0.804. Same genre is worth +0.06
on its own.

## 6. The top pairs are the same genre at the same tempo

| candidate | closest | score | deciding | A | B | same genre | Δbpm |
|---|---|---|---|---|---|---|---|
| 00098 | 00068 | 0.962 | tempo | rnb 91 | rnb 90 | yes | 1 |
| 00101 | 00066 | 0.951 | tempo | hiphop 89 | hiphop 92 | yes | 3 |
| 00066 | 00065 | 0.950 | mfcc | hiphop 92 | hiphop 92 | yes | 0 |
| 00063 | 00061 | 0.948 | tempo | afrobeat 103 | afrobeat 103 | yes | 0 |
| 00076 | 00061 | 0.923 | tempo | afrobeat 103 | afrobeat 103 | yes | 0 |
| 00123 | 00108 | 0.914 | tempo | liquid_dnb 170 | liquid_dnb 173 | yes | 3 |
| 00031 | 00026 | 0.927 | mfcc | rnb 90 | rnb 86 | yes | 4 |

Every one: `audio_fingerprint=0.00`, `lyrics=0.00`, mfcc 0.96–1.00, tempo 0.70–1.00.

`tempo` scores **1.00 at a 1 BPM gap** — and the director deliberately picks BPM from a
narrow band for a given genre and energy, so tempo proximity is near-guaranteed by design
rather than evidence of copying.

## 7. Provenance

**There is no provenance column.** `tracks` carries `provider` and `model_identifier`, and
all 154 rows are `provider=ace_step`. Engineering-test, simulation and station generations
are indistinguishable and all participate identically in station-wide novelty history.

The corpus analysed here is entirely engineering/test output from 2026-10-02/03. It is
already acting as permanent station novelty history. This is a real contributor: each test
track makes the next one likelier to be rejected, and nothing ever expires.

Classes worth modelling: `ENGINEERING_TEST`, `SIMULATION`, `MANUAL_LAB`, `PRODUCTION_RADIO`.
Exact-duplicate protection should span all of them; graded novelty should probably not.
**Not yet implemented** — recorded here so the policy is a decision rather than an accident.

---

## Conclusions

1. **T1 is mis-stated in the test report.** The thresholds are not simply "too tight" — they
   are being applied to a score computed from the wrong 68% of the evidence. The 0.72/0.84
   band was calibrated for a 7-component score including a real fingerprint; it is being
   used on a 5-component genre score.
2. **Installing `fpcalc` is the single highest-leverage action available** and does not
   weaken any gate. It restores 22% of the weight *and* the only duplication-specific
   signal.
3. **B3 is not only a vocals feature.** Fixing it restores the lyrics component's 10% and
   gives the engine a second identity-bearing signal.
4. **MFCC should not carry 21% effective weight** at a 0.014 approve/reject spread. This is
   a weighting defect, distinct from the threshold question.
5. **B2's review policy has a sound evidential basis**: a candidate whose fingerprint says
   "not comparable", whose lyrics are absent, and whose score is driven by mfcc/tempo is
   exactly a candidate that has never been shown to be a duplicate of anything.

Recommended order, revised from the test report:

```
fpcalc install (free, no gate weakened)
  -> B3 (restores lyrics signal, and ships vocals)
    -> re-measure the distribution
      -> B2 review policy, designed against the corrected distribution
        -> T1 threshold/weight tuning, last and with evidence
```

---

# Part 2 — after Chromaprint, bit-matching and provenance

Three changes, measured. No threshold was altered.

## Correction to conclusion 2 above

"Installing `fpcalc` is the single highest-leverage action available" was right about the
outcome and **wrong about the mechanism**, which matters because acting on it alone would
have changed nothing.

Chromaprint's vector is computed and then dropped: there is no column for it, and
`_rehydrate_fingerprint` documented that it reconstructs none. Installing the binary moved
the provider from `chroma-builtin` to `chromaprint` and left the component exactly as
absent as before — it still offered only an exact-string match, and no two generations
produce an identical Chromaprint string.

What was actually missing was a *comparison*. Chromaprint's stored signature is 512 packed
32-bit subfingerprints, and the right measure over those is bit agreement, not cosine.
Measured over all 10 440 pairs in the corpus:

| measure | self | mean | p95 | max | same − cross genre | approve vs reject |
|---|---|---|---|---|---|---|
| cosine over raw ints | 1.000 | 0.712 | 0.868 | 0.961 | −0.010 | 0.860 / 0.849 |
| **bit agreement** | **1.000** | **0.511** | 0.547 | **0.643** | **+0.003** | 0.569 / 0.569 |

Cosine over packed integers is a third saturated component — the same error MFCC[0] and
the raw chroma cosine already made in this codebase. Bit agreement sits at 0.51 for
unrelated material, which is exactly what half-matching 32-bit words should give, and the
**maximum across every pair is 0.643** — confirming from a second, independent direction
that the corpus contains no duplicates at all.

Critically it is **genre-blind**: +0.003 between same-genre and cross-genre pairs, against
MFCC's saturation at 0.98. It answers "is this the same *song*" where everything else
answers "is this the same *kind of music*".

## Measured effect

**Run A — provenance ignored, so this isolates the fingerprint change:**

| | baseline | now | change |
|---|---|---|---|
| approve | 10 (6.9%) | 10 (6.9%) | — |
| review | 62 | **120 (82.8%)** | +58 |
| **reject** | **63 (46%)** | **15 (10.3%)** | **−48** |
| mean max-similarity | 0.816 | 0.779 | −0.037 |
| closest match carried a fingerprint score | **0 / 134** | **145 / 145** | 100% |
| candidates scoring ≥ 0.90 | 16 | **0** | −16 |

Rejections fell from 46% to 10% **without touching a threshold**. The duplication-specific
signal now says "not the same recording" and pulls the combined score below the reject
line. Nothing scores above 0.90 any more.

The mass moved into REVIEW, which makes **B2 the decisive lever**: 120 of 145 candidates
now sit in a band the station currently discards wholesale.

Drivers are still `mfcc` 51% and `tempo` 38%. The fingerprint fix did not address
saturation — that remains T1's content, and it is now cleanly separable from the
fingerprint question.

**Run B — the provenance policy as production applies it:**

approve 145 (100%), review 0, reject 0, deciding component `none` for all.

This is **not** a 100% approval rate and must not be reported as one. The entire corpus is
`unknown` provenance, so none of it contributes graded novelty, and what Run B actually
shows is the **cold start**: a station with no production history behind it compares
against nothing and approves on the duplication checks alone. Graded novelty returns as
real airings accumulate. The honest claim is narrower and still useful — *bench output no
longer ages production music*, which was the defect.

## Implementation notes

* `AudioFingerprint.similarity_to` owns the measure, so a provider's comparison lives with
  the provider that understands it. `None` means "cannot say" and stays a missing input
  rather than becoming a 0.0 that reads as "definitely unalike".
* `LibraryEntry.provenance` defaults to `production_radio`: a caller that forgets to state
  provenance gets the stricter behaviour, never the looser one.
* The independent repetition rules (identical lyrics, repeated blueprint) are scoped to
  graded history too. Exact *audio* is not — shipping the same recording twice is wrong
  whoever produced the original. Missing this initially left 6 blueprint rejections firing
  against bench tracks under the policy.
* No originality threshold was changed.
