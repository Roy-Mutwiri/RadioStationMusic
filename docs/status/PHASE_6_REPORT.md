# Phase 6 — Audio QC, Originality, Mastering

**Baseline:** `2a98b96` (Phase 5)
**Scope:** §6.1–§6.27, gates A–N
**Status:** complete, with the residual limitation in §"What is not proven" stated plainly.

---

## 1. The claim this phase does and does not make

§6 opens by ruling out the strong claim, and nothing built here makes it:

> **Do not claim that the system can prove a song has never existed before. That is not
> technically defensible.**

What exists is a station-internal novelty and quality system. It compares a candidate against
**this station's own library** and refuses exact duplicates, near-duplicates, suspiciously
similar lyrics, excessively repeated creative blueprints and broken audio; it normalises to a
consistent broadcast format; and it records the evidence behind every approval and rejection.

That boundary is enforced in code rather than left to discipline. `GET /api/originality/summary`
returns a `scope_note` field, the Originality page renders it verbatim above every number, and
an API test asserts its content — so the claim and the data cannot drift apart, and a careless
copy edit cannot turn it into a promise the system cannot keep.

**The central rule — a track that fails this pipeline never becomes READY — is enforced at the
station boundary and tested there**, not merely at component level.

---

## 2. What was built

| § | Component | File |
|---|---|---|
| 6.1 | 21-check audio QC, PASS/WARN/FAIL per check | `tradefix_radio/audio/qc.py` |
| 6.2 | Feature extraction, compact summaries only | `tradefix_radio/audio/analysis.py` |
| 6.3 | Canonical PCM hash | `tradefix_radio/audio/fingerprint.py` |
| 6.4 | Chromaprint adapter + built-in fallback | `tradefix_radio/audio/fingerprint.py` |
| 6.5 | Staged similarity search | `tradefix_radio/originality/similarity.py` |
| 6.6 | Novelty score | `tradefix_radio/originality/similarity.py` |
| 6.7 | Blueprint similarity with recency rules | `tradefix_radio/originality/blueprint.py` |
| 6.8 | Lyric originality, no cloud dependency | `tradefix_radio/originality/lyrics.py` |
| 6.9–6.11 | Mastering and the canonical master format | `tradefix_radio/audio/mastering.py` |
| 6.12 | Migrations (not `create_all`) | `migrations/versions/20261003_*.py` ×3 |
| 6.13 | Explicit pipeline state machine | `tradefix_radio/postprocess/pipeline.py` |
| 6.14 | Station boundary: generator success ≠ playable track | `tradefix_radio/radio/station.py` |
| 6.15 | Originality API | `tradefix_radio/api/routes/engineering.py` |
| 6.16 | Originality page | `frontend/src/pages/Originality.tsx` |
| 6.17 | Post-production panel on Generation | `frontend/src/pages/pages.tsx` |
| 6.18–6.22 | Fixtures, unit, property, integration, performance | `tests/` |
| 6.23–6.24 | Soak with defect injection | `tradefix_radio/generation/defects.py`, `cli/soak.py` |
| 6.25 | Doctor checks | `tradefix_radio/monitoring/checks.py` |
| 6.26 | CLI tools | `tradefix_radio/cli/audio_tools.py` |
| 6.27 | Documentation | `docs/{AUDIO_QC,ORIGINALITY,MASTERING,PIPELINE}.md` |

Persistence: three new migrations, `alembic check` clean, downgrade/upgrade round-trip
verified. No `create_all` anywhere in the production path.

---

## 3. Defects found and fixed

Seven of these were found by building the tests and running the soak, not by reading the code.
Each is recorded with the evidence that exposed it, because the reasoning is the part worth
keeping.

### 3.1 The top-end check rejected every track at 16 kHz

**Found by:** the first acceptance soak. 355 of 355 tracks rejected; the station carried
entirely by its emergency tiers while reporting 100 % audio coverage.

A 16 kHz file's Nyquist limit is **exactly** 8 kHz, so it contains no energy above 8 kHz by
construction — sampling theory, not a fault. `high_frequency_content` measured anyway and
concluded *"the top end has collapsed, which means a decode or render fault"*, a cause that had
not occurred.

The check now declines to measure when the band is at or above the source's Nyquist limit,
returning `value: null` with a reason naming the limit. No number, because nothing was
measured. Two regression tests: a 16 kHz file must pass, and a 44.1 kHz file with a genuinely
filtered top end must still fail — the exemption is about sample rate, not a blanket amnesty.

### 3.2 Perfect anti-phase reported as perfectly mono-compatible

`_stereo_metrics` guarded against `log10(0)` by falling through to its `0.0` initialiser —
recording the **worst** possible case (loud in stereo, silent in mono) as "no fold-down loss".
The track still failed, but via the correlation check, by accident, with a wrong number on the
record. Total cancellation now reports a documented −120 dB floor.

### 3.3 "No meter installed" and "too quiet to measure" were the same answer

Both produced `integrated_lufs = None`, and QC reported *"no loudness meter is installed"* for
both — telling an operator to install software they already had while a near-silent track
passed with a warning. `AudioFeatures.loudness_meter_available` now separates them: a missing
meter warns, a meter that ran and found nothing fails.

### 3.4 Mastering rejected its own correct output

**Found by:** the first end-to-end pipeline probe. Every clean track rejected at final QC,
2.2 LU below target.

The source measured −22.1 LUFS with a 20.8 dB crest factor at −3.0 dBTP. Reaching a
−12.4 LUFS target linearly needs +9.7 dB, which would put the true peak at +6.7 dBTP. FFmpeg's
limiter did 5 dB of crest reduction and stopped at the −1 dBTP ceiling. **The master was
correct**; the symmetric tolerance was wrong.

Final QC's loudness test is now asymmetric: louder than target is always a defect, quieter is
a defect *only when there was headroom left unused*. The tolerance is unchanged at ±1.5 LU —
the undershoot branch is excused only on measured evidence that the ceiling was reached, so a
quiet master with headroom still fails. `peak_constrained` is recorded and surfaced, because a
station where many tracks are peak-constrained is telling the operator the target is too
ambitious for what the generator produces.

Also fixed: `gain_applied_db` recorded the gain *requested*, not the gain *applied*. With the
limiter engaged those differ, and the stored evidence disagreed with the file it described.

### 3.5 The embedding could not discriminate anything

**Measured:** four tracks of different genres, tempos and keys scored **0.93–0.95** against
each other, with an MFCC cosine of **0.998** between every pair.

Two structural causes:

- **Chroma is non-negative.** The cosine of two vectors in the positive orthant has a floor far
  above zero — unrelated tracks measured 0.86–0.91. On that scale a 0.84 rejection threshold
  rejects essentially everything.
- **MFCC[0] is log-energy, not timbre.** An order of magnitude larger than the coefficients
  describing spectral shape, so it dominated the dot product entirely.

Embedding version 2 centres chroma (making the cosine a correlation over *which pitch classes
stand out*) and drops MFCC[0]. The same four tracks now score **0.32–0.55**. `EMBEDDING_VERSION`
is stored with every embedding so a version mismatch is detectable rather than silent.

The identical correction was then needed for the `chroma` and `mfcc` components of the full
comparison — fixing the embedding alone left one calibrated component outvoted by two saturated
ones.

### 3.6 One measurement counted twice, with half the weight

`ChromaFingerprintProvider` published `features.embedding()` as its vector — the *identical
array* the engine already compares as its `embedding` component. The engine therefore scored
one piece of evidence under two names with 0.48 of the combined weight between them, which is
precisely the "pretend these numbers are independent" error §6.5 warns against. The builtin
provider now publishes no vector; its contribution is the quantised signature equality check,
which the continuous comparison genuinely does not provide.

### 3.7 The library was quieter than it looked

`load_library` wrote the perceptual fingerprint and the blueprint fields to the database and
then returned `None` for both. The weight renormalisation redistributed their share across the
components that *were* present — including the timbre comparison that saturates on
single-generator material — and clean tracks began failing as near-duplicates of each other.
Nothing errored.

Both are now rehydrated. Blueprint comparison needs *composition* energy on 0–1, distinct from
the *market* energy on 0–100 that `tracks` already denormalised — the same conflation Phase 5
had to untangle at the API boundary — so `tracks.composition_energy` was added as its own
column with its own migration.

### 3.8 A rejected track kept its queue slot

**Found by:** the §6.14 station-boundary test.

§28's positional lock made `remove()` raise `ProtectedItemError` for a track at position 0, so
a rejected track stayed at the head of the queue — unplayable, and exactly the dead air this
phase exists to prevent. The lock protects a slot from *reprogramming*; it was never meant to
compel the station to keep a track that cannot play. The terminal-generation-failure path had
already reached that conclusion and used `force=True`; the new path now matches it.

### 3.9 The defect injector double-counted itself

**Found by:** the injector's own arithmetic test (200 calls, `stats.total == 400`).

Both `generate()` and `record()` incremented `total`. Harmless-looking, but it would have made
the soak report an injection rate half the real one — flattering, and wrong.

### 3.10 A lint error that was a real bug

`ruff` flagged an unused `clamp` parameter: I had added it to `_cosine` and never used it in
the body, so the correlation helpers were receiving values already clamped to zero and then
mapping them to 0.5 — recording *unrelated* as half-way to a duplicate.

---

## 4. Measurements

All figures measured on this machine, this build.

### Throughput — 180 s of 44.1 kHz stereo

| stage | time | share |
|---|---|---|
| decode | 0.03 s | 0.1 % |
| feature extraction | 6.11 s | 22.1 % |
| QC (21 checks) | 0.001 s | 0.0 % |
| canonical hash + fingerprint | 0.44 s | 1.6 % |
| similarity | <0.001 s | 0.0 % |
| mastering (2-pass loudnorm) | 15.91 s | 57.5 % |
| final QC | 5.17 s | 18.7 % |
| **total** | **27.66 s** | **6.5× real time** |

At one three-minute track every three minutes: **15.4 % of one core.**

librosa JIT warm-up, paid once at construction: **6.0 s** (29 s MFCC + 15 s beat tracking on
the very first cold call before `warm_up` existed).

### Similarity search scaling

| library size | time |
|---|---|
| 100 | 3.7 ms |
| 1 000 | 5.0 ms |
| 10 000 | 20.8 ms |
| 50 000 | 95.5 ms |

The staged search holds: stage 2 is one vectorised matrix multiply, and only the 24-track
shortlist reaches the expensive comparison.

### Mastering accuracy

Target hit to within 0.1 LU on material with headroom; peak-constrained and recorded as such
on material without.

---

## 5. Acceptance evidence

Full suite: **1920 tests, exit 0.** `ruff` clean across 125 source files; `mypy --strict`
clean; frontend `tsc --noEmit` clean, 35 Vitest tests passing, production build succeeds.

| Requirement | Evidence |
|---|---|
| §6.1 QC returns name/status/value/threshold/reason per check, never one Boolean | `test_every_check_reports_its_own_status_value_and_threshold` asserts the shape of **all 21** checks, not a sample |
| §6.1 a quiet ambient track must not fail for low RMS | `test_quiet_ambient_is_not_failed_for_being_quiet`, paired with `test_the_quiet_allowance_is_bounded` so the first cannot be satisfied by deleting the check |
| §6.2 no gigantic per-frame matrices | `test_features_are_summaries_not_frame_matrices` asserts < 256 stored floats per track |
| §6.2 dependency cost benchmarked | measured: 6.1 s per 180 s track, 6.0 s one-off JIT; reported by `tradefix doctor` |
| §6.3 exact duplicates rejected, container/metadata-independent | `test_the_hash_ignores_container_and_metadata`, `test_the_hash_ignores_channel_layout_and_rate`; the limit is asserted too (`test_a_near_duplicate_does_not_hash_the_same`) |
| §6.4 Chromaprint if practical, missing `fpcalc` handled gracefully | `fpcalc` is **not** installed here; the built-in provider is active, `doctor` reports WARN with the remediation, and cross-provider comparison is refused |
| §6.5 staged search; components preserved; not one scalar | `test_component_scores_survive_the_verdict`, `test_the_shortlist_bounds_the_expensive_stage`; measured 95.5 ms at 50 000 tracks |
| §6.6 documented 0–1 novelty formula | `test_novelty_is_one_minus_max_similarity` is the documentation, executable |
| §6.7 stronger rules for recent repetition | `test_recent_repetition_is_held_to_a_stricter_threshold`, plus a property test that the threshold never loosens for newer tracks |
| §6.8 lyric originality without a cloud dependency | entirely lexical; `test_a_reused_hook_is_caught_even_with_new_verses` covers the case a blended score would hide |
| §6.9 no naive peak normalisation; energy bands documented | `test_quiet_material_stays_quieter_than_loud_material` is the test a peak-normalising implementation fails |
| §6.10 final QC on the master | asymmetric loudness test; `test_mastering_hits_its_target_or_explains_why_not` |
| §6.11 canonical master format | `test_a_master_is_written_in_the_canonical_format` — 48 kHz stereo 24-bit, matching playout |
| §6.12 proper migrations, not `create_all` | three Alembic revisions; `alembic check` clean; downgrade/upgrade round-trip verified |
| §6.13 illegal transitions fail explicitly | `test_illegal_stage_transitions_fail_explicitly` |
| §6.14 generator success ≠ playable track | `test_a_rejected_track_never_becomes_ready` **and** its paired positive, both through the real station |
| §6.15 Originality API | 4 API tests including the scope-note assertion |
| §6.16 Originality page | built against real data; scope note rendered verbatim above every figure |
| §6.17 Generation Lab integration | post-production panel; refuses with its reason when the pipeline is absent |
| §6.18 deterministic fixtures | `tests/audio_fixtures.py`; defects constructed, not hoped for |
| §6.19 unit tests | 18 QC + 35 originality + 11 mastering |
| §6.20 property tests | 10 Hypothesis properties on bounds, symmetry, monotonicity and robustness |
| §6.21 integration | 12 end-to-end tests |
| §6.22 performance | measured and tabulated above |
| §6.23 soak with 20 % invalid + 10 % duplicate | `tradefix soak --post-production --invalid-rate 0.2 --duplicate-rate 0.1` |
| §6.24 continuous playback under injection | **100 % audio coverage, 0 ms unintended silence, 0 underruns, 0 broken tracks aired** |
| §6.25 doctor | `audio_analysis` and `fingerprinting` checks added |
| §6.26 CLI | `analyze`, `qc`, `fingerprint`, `compare`, `master`, `lyrics` |
| §6.27 docs | four documents, each recording the reasoning and the measurements |

**No threshold was lowered to make a test pass.** Where a test failed, the cause was diagnosed
and the defect fixed — §3 records all ten. The one threshold that moved was the mastering
*undershoot* branch, and it moved to require *more* evidence (a measured ceiling hit), not
less.

### The acceptance soak

```
2.02 simulated hours, 1440 steps, seed 606, 20 % invalid + 10 % duplicate injection

  Audio coverage        100.00 %   (7200 s of 7200 s)
  Unintended silence    0 ms
  Queue underruns       0
  Transition failures   0
  Unhandled exceptions  0
  Leaked tasks          0

  Defects injected      103   (21 silence, 17 dropout, 17 clipping, 5 truncation, 43 duplicate)
  Tracks rejected       355
    qc_defect            60   <- exactly the 60 corrupted renders
    exact_duplicate      43   <- exactly the 43 injected copies
    audio_near_duplicate 218
    review_unresolved     21
    final_qc_defect       12
    blueprint_repetition   1

  Broken tracks aired   0
```

The two counts that match exactly — 60 corruptions caught as `qc_defect`, 43 copies caught as
`exact_duplicate` — are the evidence that the pipeline catches precisely what was injected,
attributed to the right cause. **No deliberately broken track reached air**, checked against
the played history rather than a counter.

---

## 6. What is not proven, and the two invariants the soak fails

The soak reports **two** breached invariants. Both have the same root cause. They are reported
rather than tuned away.

```
FAIL: post-production rejected 355 of 355 tracks (100%); 103 were deliberately
      broken, so the programming is being starved rather than filtered
FAIL: only 0 genre(s) across 240 tracks — §81-18
```

### The cause, measured

The mock provider cannot supply enough acoustic variety for a library of this size. Measured
directly over 32 blueprints constructed to be maximally different — 8 genres × 4 keys, with
BPM and energy spread across their ranges:

| quantity | value |
|---|---|
| all 496 pairwise embedding cosines | min 0.280, **median 0.486**, max 0.999 |
| each track's *closest* neighbour | min 0.865, **median 0.902**, max 0.916 |
| chroma at the closest neighbour | mean 0.977 |
| MFCC at the closest neighbour | mean 0.998 |
| would be rejected at the 0.84 threshold | **32 / 32** |

The pairwise *distribution* is healthy — most pairs of mock tracks are clearly distinguishable
and the median of 0.486 sits well clear of the 0.84 threshold. But the synthesiser has
collisions: for **every** track there exists some other track whose audio is near-identical,
however different their blueprints. Rejecting those is correct behaviour.

So the 218 `audio_near_duplicate` rejections are the originality engine working correctly on
input it was never going to accept. The limitation is the **Phase 4 synthesis stub**, which
exists to produce plausible-length audio for runtime testing, not musical variety. Phase 7
replaces it.

### Why this is reported rather than configured away

Lowering `reject_similarity` until the mock provider passes is exactly what the brief forbids,
and it would silently weaken duplicate detection for the real generator. The measured pairwise
median of 0.486 supports 0.84 as a sane threshold; nothing in the data suggests it is too tight
for genuinely varied material.

Instead the soak was taught to **name the condition**. A 100 % rejection rate previously looked
healthy on every continuity metric — 100 % coverage, zero silence, zero underruns — because the
emergency tiers carried the broadcast. That is the most dangerous shape a failure can take, and
it now fails the run with the cause stated.

### What this means for Phase 7

Two things must be checked when ACE-Step lands, and neither can be checked before then:

1. **Whether real output clusters.** If generated music also has a near-identical neighbour for
   every track, the rejection rate stays high and the thresholds need revisiting *against real
   data* — the only basis on which they should be revisited.
2. **Whether the station needs a starvation response.** A sustained rejection rate currently
   leaves the station on procedural filler indefinitely. The soak detects it; the station does
   not react to it. That is a resilience question and belongs in Phase 9, but it is recorded
   here because this is where the evidence for it appeared.

### Other limits, stated plainly

- **Originality is station-internal.** It compares against this library and nothing else. It is
  not a copyright clearance and makes no claim about any other music.
- **The built-in fingerprint is weaker than Chromaprint.** `fpcalc` is not installed on this
  machine; `doctor` says so, and the active provider is recorded with every fingerprint.
- **The canonical hash does not survive sample-format conversion.** Measured, documented, and
  covered deliberately by the similarity engine's exact-audio threshold rather than papered
  over.
- **Lyric comparison cannot detect copied text.** It catches the station repeating itself. §17
  and §86 both say the stronger claim is not available, and nothing here makes it.
