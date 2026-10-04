# B3 — The production lyric path

**Status:** complete. Critical acceptance met and confirmed by listening.
**Scope:** the music station only. The anime/office/visual workstream was not touched.
**Not started:** Phase 8 / OBS.

**The station now broadcasts tracks with audible vocals.** Two were produced end to end
with no manual intervention and confirmed audible on the listening device:

| track | genre | style | lyric | state |
|---|---|---|---|---|
| TF-20261004-00039 "Running Gravity" | deep house | sung | 140 words, hook-heavy | played |
| TF-20261004-00041 "Fast Doubt at no Bid" | amapiano | chopped hook | 187 words, melodic rap | ready |

---

## 1. What was actually wrong

B3 was specified as "make vocal/rap tracks a real production path". The defect turned out to
be narrower and worse than "lyrics are not implemented": **every component existed, was
correct, and was connected to nothing.**

The chain is: director decides vocals → lyric is composed and validated → lyric is attached
to the generation request → prompt builder puts it in the payload → ACE-Step sings it.

- `MusicDirector` set `vocal.enabled = True` and chose a style. Correct.
- `LyricsDirector` and the composer could produce lyrics. Correct, and never called.
- `GenerationRequest.lyrics` existed. Always `None`.
- `GenerationManager.claim_and_generate` accepted a `lyrics_for` callback. **No caller
  anywhere in the codebase passed one.**
- `AceStepPromptBuilder` saw a vocal blueprint with no lyric and — entirely correctly, under
  §7.10 and §14 — refused to let the model invent words about trading, substituting
  `[Instrumental]`.

So the station behaved exactly as designed at every step and produced instrumentals for
every rap blueprint. The `lyrics` table was empty across the whole database. Nothing logged
an error, because nothing had gone wrong by any component's own standard.

This is the same failure mode as three other findings in this codebase, listed in §7 below.

---

## 2. What was built

### `LyricOrchestrator` (`tradefix_radio/lyrics/orchestrator.py`)

Explicit and standalone, not buried in `MusicDirector` or `AceStepProvider`, as required.

- **Modes**: `NONE`, `HOOK_ONLY`, `MINIMAL_VOCAL`, `FULL_RAP`, `MELODIC_RAP`, `FULL_SONG`,
  `SPOKEN_WORD`. `is_instrumental` and `is_sparse` are properties of the mode rather than
  checks scattered at call sites.
- **Mode selection** honours an explicit blueprint format over the vocal style — the more
  specific request wins.
- **Duration-aware word budget**: 0.35–2.2 words per second. Sparse modes are exempt from
  the lower bound; see §6 for why that was necessary.
- **Market scoping preserved**: a BTCUSD track cannot carry gold-session framing and a gold
  track cannot carry crypto-weekend framing. Neutral content is available to both.
- **Retries** re-seed per attempt by CRC32 of the track id and attempt number. The composer
  is a seeded grammar, so without re-seeding attempt 2 reproduced attempt 1 exactly and the
  retry was decorative. CRC32 rather than `hash()` because Python salts string hashing per
  process, which would have broken the reproducibility the code claims.
- **Failure is a result, never an exception** — `LyricGenerationResult` carries a named
  `LyricFailure`, because the caller has to choose between an instrumental and abandoning
  the track, and that is policy rather than an error.

### Provider safety boundary

> *"ACE-Step is a performer. ACE-Step must NOT be allowed to independently invent live
> trading claims, signals or arbitrary financial lyrics in production mode."*

Held structurally, and it was already held before B3 — that is precisely why vocal tracks
came out silent rather than wrong. The builder's rule is: validated lyrics, or
`[Instrumental]`. There is no third branch. `thinking` is off in the submission payload so
the model cannot rewrite what it is given.

### What the provider was told is now recorded

New table `provider_submissions`, one row per **attempt**:

| column | why |
|---|---|
| `caption` | the final provider prompt |
| `requested_lyrics` | what the station composed — a copy; the validated lyric still lives in `lyrics` and is not touched |
| `provider_lyrics` | what was actually submitted, marker and all |
| `lyrics_modified`, `lyric_notes` | §7.10: the provider must not silently replace lyrics |
| `instrumental` | whether the submission asked for one |
| `profile`, `inference_steps`, `guidance_scale`, `seed` | reproducibility (§7.9, §7.26) |

`GenerationSpec.as_metadata()` already described itself as *"the record persisted with the
track"* and was already being written into `provider_metadata` by the provider. **Nothing
read it.** It is now persisted, and it is what diagnosed the finding in §3.

A vocal track realised as an instrumental sets `lyrics_modified=True` **and**
`instrumental=True` — a queryable signature (`ProviderSubmissionsRepository.downgraded()`)
rather than a log line. Before this change that downgrade was recorded only as a *warning*
and `lyrics_modified` stayed `False`, so the station's own record claimed the lyric had been
passed through unchanged while the track went out wordless.

---

## 3. The vocals were still inaudible after the wiring — and why

With `lyrics_for` wired, the station composed, validated, stored and submitted complete
lyrics. Listening tests still reported **"no vocals at all"** (TF-00159) and **"still no
vocals"** (TF-00170).

The submission record settled it. For TF-20261003-00183:

```
instrumental=False  lyrics_modified=False
profile=balanced  steps=8  guidance=3.0
requested_lyrics: '[hook]\nconfidence in the process, not the last print\n…'
provider_lyrics : '[hook]\nconfidence in the process, not the last print\n…'
```

The lyric reached the model intact. The problem was **how the model was asked**.

Two hypotheses were tested rather than argued:

1. **Caption position** — the vocal descriptor ("rap vocal") is appended last, at character
   197 of 206, where a hand-written probe that produced clear rap had led with it. *Refuted*
   by an A/B at identical seed: both captions sang.
2. **Denoising steps** — the station ran `balanced`: **8 inference steps, guidance 3.0**. The
   probe that produced clear rap used **27 steps, guidance 7.5**. *Confirmed*: at 4–8 steps
   the arrangement renders convincingly and the diction never resolves.

The configured profiles carried the comment *"Starting points, to be revisited against the
§7.21 benchmark"* and had never been revisited.

### Cost, measured at production length (215 s, same prompt, same seed)

| profile | steps / guidance | wall time | §93 capacity |
|---|---|---|---|
| `balanced` | 8 / 3.0 | 67 s | **3.22×** |
| `vocal` | 28 / 7.5 | 145 s | **1.48×** |

Clarity costs about 2.2× the GPU time and still generates faster than real time.

### The fix: a vocal-specific profile

Raising the global default would have charged every ambient instrumental for diction nobody
is singing and halved capacity for nothing. Instead, `generation.ace_step.vocal_profile`
(default `"vocal"`, 28 steps / guidance 7.5) is selected **when the request carries lyrics**
— keyed on `request.lyrics`, not on `blueprint.vocal.enabled`, so a vocal blueprint that was
downgraded to an instrumental does not pay for words that are not in the payload.

At a ~50/50 vocal/instrumental mix this blends to roughly **2.0× capacity**, above the >1.5×
preference and well above the >1.0× requirement.

The retry ladder becomes `vocal → quality → balanced → fast`: a retry after a timeout or an
OOM gives up diction before it gives up the track.

---

## 4. Tests added

| file | what it pins |
|---|---|
| `tests/unit/test_lyric_orchestrator.py` (39) | modes, duration budget, market scoping, Trade Fix mention rate, determinism, claim validation with deliberately bad fixtures, no real-artist naming, and the six lyric-originality cases |
| `tests/integration/test_vocal_path.py` (5) | the end-to-end path against the real station, real director, real orchestrator and real prompt builder — only the GPU is faked |
| `tests/integration/test_ace_step_provider.py` (+2) | lyrics select the vocal profile; an instrumental does not pay for diction |
| `tests/unit/test_ace_step_lifecycle.py` (+1) | the vocal profile has enough steps for diction |
| `tests/integration/test_phase4_gates.py` (+1) | recovery hands the scheduler titles oldest-first |

The integration fake builds its spec with the **real** `AceStepPromptBuilder` and returns the
real `detail` block. A fake that invented its own submission record would have been asserting
on the test's idea of the payload rather than the one production builds — which is exactly
where this bug lived.

### Lyric originality

Driven through the real path: the station reads `recent_hashes()` and `recent_shingles()`
and passes both into `generate`, which hands them to the validator's `ValidationContext`.

- exact repeat of a previous lyric → rejected
- same hook, different verses → **allowed** (a recurring hook is an identity, not a repeat)
- same topic twice → **allowed** (§12 limits repetition *rate*, it does not make a topic
  single-use)
- common trading vocabulary → **allowed**, and tested against a history made of nothing else
- a duplication rejection names `duplicate_lyrics`, not a bare score
- empty history never blocks the first lyric

Comparison is on shingles rather than word overlap. "Risk", "stop", "entry" and "momentum"
are the station's dialect; a measure that punished their recurrence would reject every lyric
the topic graph can produce, and the symptom would have looked like a model failure rather
than a measurement one.

Title history (same title, near title) was already covered by `tests/unit/test_titles.py`.
The defect there was ordering, not absence — see §5.

---

## 5. Other defects found while doing this

### Title history was being read backwards (production only)

`TrackRepository.recent_titles()` returns **newest first**. The scheduler grew its list by
appending each title it planned and then read `titles[-40:]` as "the most recent forty".
Against a newest-first list that slice is the **forty oldest titles on the station** — so
§99's similarity rejection compared each new title against names from hundreds of tracks ago
and never against the ones just aired.

The repository's own test asserts newest-first, and the director's test slices `[:40]`
correctly. Only the production path had it backwards. Fixed by reversing at the load site,
with a regression test.

### An orchestrator exception killed the whole scheduling stage

`_compose_lyrics` called the orchestrator unguarded. A raise propagated out of
`_persist_tracks` and took the entire schedule stage with it: the test that found this
measured 49 scheduling cycles, **zero tracks planned**, and a station living on procedural
audio. Now converted to `LyricFailure.ORCHESTRATOR_ERROR` and routed through the normal
failure policy.

### Buffer-aware vocal suppression was a setting nothing read

`lyrics.suppress_vocals_at_buffer` existed and had no consumer. Now wired in
`_compose_lyrics` with its own counter (`lyrics_suppressed`), kept separate from
`lyrics_failed` — this is the station choosing, not the station struggling, and conflating
them would make a healthy pressure response look like a defect.

### The `fail` lyric policy did not actually stop the track

`_compose_lyrics` dropped the queue slot, but the generation job is planned by the *caller*,
**after** `_persist_tracks` returns — so the slot vanished and the GPU rendered the track
anyway, producing an instrumental under the one policy whose entire purpose is to say "do
not make this". `_persist_tracks` now returns the abandoned track ids and the caller skips
planning jobs for them. My own defect, introduced earlier in B3.

### `minimal vocal` was three identical sections

The one format whose sections were all the same kind: `[phrase, phrase, phrase]`, and
`phrase` maps to the hook role, which the composer deliberately writes **once** and reuses
wherever a format repeats it — that is what makes it a hook rather than three different
choruses. A format made entirely of hooks therefore rendered as the same block three times.
Measured: **23 of 41** `minimal vocal` lyrics were all-identical, 54 words of which 18 were
distinct. The first vocal-profile track the station produced (`TF-20261004-00038`) was one,
and it is why that track is `quarantined`.

Fixed in two places: a `passage` between the hooks so there is something for it to come back
from (and `max_words` 90 → 110 to fit the new shape), plus the `no_section_variety` validator
rule so a format that drifts this way again is caught rather than aired.

The rule is written to tell a chorus from a loop. Two matching sections pass — a statement
and its restatement is a song. Three or more sections *all* identical, compared with their
tags stripped so a rename cannot disguise them, is a generator repeating itself.

### The composer sang stage directions

`persona.signature` entries are directions to a writer — *"ends verses on the decision, not
the outcome"*, *"holds the last note of the hook"* — and were being inserted verbatim as sung
lines about 25% of the time. The first real vocal track rapped one mid-verse. Removed.

---

## 6. Things I got wrong during B3

Recorded because the reasoning matters more than the outcome.

- **Retries were decorative.** The composer is seeded and deterministic, so attempt 2
  reproduced attempt 1 byte for byte. Fixed with per-attempt re-seeding.
- **Mode was reported from the blueprint, not the composed format.** Fixed by re-reading it
  from `plan.lyric_format.key`.
- **Sparse modes were rejected outright.** "Mostly instrumental with vocal hook" is 16–60
  words, and a 180-second track demanded ≥62, so every hook-only blueprint fell back to
  instrumental. Added `LyricMode.is_sparse`.
- **My own test harness built contradictory blueprints** — the shared fixture hardcodes
  `lyrics.format = "full rap"`, and `mode_for` correctly honours format over style.
- **I claimed the caption was the cause before testing it.** The A/B refuted it. The step
  count was the cause, and asserting the first plausible mechanism would have shipped a
  caption rewrite that fixed nothing.

---

## 7. The pattern worth naming

Four separate findings in this workstream share one shape: **a component is built correctly,
exports a clean interface, and nothing calls it.**

| component | state |
|---|---|
| `GenerationManager.claim_and_generate(lyrics_for=…)` | no caller — **the B3 defect itself** |
| `GenerationResult.detail["prompt"]` | written every generation, never read — fixed here |
| `profile_for_buffer()` + `ace_step.buffer_aware_profile` | no production caller; the setting does nothing |
| `TrackFileRepository` | no production caller — `has_audio` always false, §36 retention never deletes (**B5**) |
| `PlayEvent` / `play_events` | contract, model, table and five indexes; **no writer**. 85 tracks played, 1577 state transitions recorded, zero play events |

Each is invisible to tests that exercise the component, and invisible to tests that exercise
the caller, because the defect is in the *absence* of an edge between them. The tests added
in B3 assert on what the provider **received**, not on what the station **stored**, which is
the only framing that catches this class.

---

## 8. Acceptance

> *"At least one real vocal track through blueprint → validated lyrics → ACE-Step → Phase 6
> → APPROVED → READY → playout, no manual DB edits, no bypass."*

**Met.** Run via the normal entry point (`tradefix station start --test-mode --provider
ace_step`), scenario `breakout_up`, seed 909.

`TF-20261004-00039` — "Running Gravity", deep house, 119 BPM, sung, 150 s:

1. Director produced a vocal blueprint (`vocal.enabled`, style `sung`).
2. `LyricOrchestrator` composed a 140-word `hook-heavy` lyric and §17 accepted it.
3. `LyricsRepository` stored it; the station cached it for the generation callback.
4. The provider received it — recorded in `provider_submissions`: `profile=vocal`,
   `inference_steps=28`, `instrumental=False`, `lyrics_modified=False`,
   `requested_lyrics == provider_lyrics`.
5. Phase 6 approved it; the queue marked it READY; playout aired it (`state=played`).
6. **Confirmed audible by listening**, together with `TF-20261004-00041`.

No manual database edits. No bypass. The only non-production element was the audio sink
(`null_sink`), so generation was not gated on real-time playback; the files were played
afterwards from `generated/mastered/`.

Per the instruction, these are **not** labelled `PRODUCTION_RADIO` merely because they
physically played during a test. Both carry `provenance = engineering_test`, so they do not
enter the graded novelty history that real programming is compared against.

---

## 9. Measurements

### Effective approved capacity (§93)

Measured by `scripts/generation/effective_capacity.py`, which divides **audio seconds that
reached a playable state** by **GPU seconds spent, rejections included**. Raw throughput
would flatter the station: a generator four times faster than real time with three quarters
of its output rejected does not stay on air, it goes silent slightly later.

| corpus | tracks | approval | GPU wasted | capacity |
|---|---:|---:|---:|---:|
| This run (post-B2 + B3) | 40 | **90%** | 9% | **2.18×** |
| Whole database (mostly pre-B2) | 226 | 38% | 58% | 1.03× |
| `profile:vocal` only | 3 | 67% | 28% | 1.20× |

**Both targets met on current behaviour**: above the >1.0× requirement and above the >1.5×
preference, at 2.18×.

The comparison is the interesting part. The historical corpus sat at **1.03× with 38%
approval and 58% of GPU time wasted** — the bottleneck was never generation speed, it was
rejection. B2's `ReviewResolver` is what moved approval from 38% to 90%, and capacity
followed. T1's threshold work is the remaining lever on the same axis.

Honest caveats: this run is 40 tracks and the `profile:vocal` row is **3 tracks** — directional,
not settled. The 2.2× GPU cost of the vocal profile is measured precisely (§3) and will pull
the blended figure down as the vocal share rises; at a 50/50 mix the arithmetic gives ≈2.0×.
Rows marked `unrecorded`/`unknown` predate `provider_submissions` and are not back-filled —
inventing prompts for historical tracks would corrupt the one table meant to record what
truly went to the model.

### Similarity distribution, re-run with vocal tracks present

226 entries with full evidence (up from the 154 B2 was designed against), 25,425 pairs. The
B2 conclusions hold and are sharper on the larger corpus:

| component | all pairs | genre delta | BPM delta | duplication authority |
|---|---|---|---|---|
| fingerprint | 0.511 ±0.021 | **+0.005** | **+0.002** | genre-blind — the only real one |
| mfcc | 0.973 ±0.023 | +0.005 | +0.002 | saturated; returns the same answer for everything |
| chroma | 0.284 ±0.270 | +0.070 | +0.027 | weak |
| tempo | 0.386 ±0.354 | **+0.413** | **+0.554** | measures tempo, not identity |

Tempo at 0.774 within a genre against 0.361 across genres, and 0.861 within 4 BPM against
0.307 beyond it, is a measurement of *production family*. Treating it as duplication
evidence is what made the station reject its own catalogue for sounding like itself. This is
T1's input.

### Lyric yield

120 randomised vocal blueprints across six vocal styles and five durations, through the real
orchestrator:

- **111/120 usable (92%)** after the section-variety rule was added, before the composer fix
- **0 rejections in 300** composer outputs after the `minimal vocal` format fix (§5)
- mode distribution: `hook_only` 47, `melodic_rap` 21, `full_rap` 20, `full_song` 17,
  `minimal_vocal` 15 — varied rather than collapsing onto one shape
- remaining failures are `too_long` (6/120), spread across `full_rap` (2), `melodic_rap` (3)
  and `full_song` (1) in the sweep, and seen once as `spoken_word` during the station run:
  the format's word ceiling against what the composer emits at the top of its range. A
  tuning issue, not a defect, and left for T1 rather than adjusted by feel here.

### Test totals

205 lyric tests (composer, validators, orchestrator, library) and 7 vocal-path integration
tests pass. The full suite passes with the anime/office workstream excluded — it is mid-edit
in another terminal and `tests/unit/test_visual_renderer.py` currently fails to import
(`CAMERA_NAMES`), which is not mine to fix.

---

## 10. Left undone, deliberately

- **`recent_lyrics` / `LyricFingerprint` comparison** is accepted by `generate` and not
  populated by the station. Not a hole: originality runs through `previous_hashes` and
  `previous_shingles`, which *are* wired, and the fingerprint path would be a second
  redundant Jaccard. Recorded rather than wired so it is a decision, not an oversight.
- **`profile_for_buffer` / `buffer_aware_profile`** still has no production caller — the
  setting does nothing. Wiring it is out of B3's scope, but `vocal` was added to its ladder
  so that enabling it later cannot silently demote vocal tracks to 16 steps.
- **`spoken word` word ceiling** — see above; T1.
- **B5** (`TrackFileRepository` unwired, so `has_audio` is always false and §36 retention
  never deletes) is visible in this run: the two acceptance tracks have no `track_files`
  rows despite their masters existing on disk. Next task, not this one.

### Two B3 items are unmeasurable until `play_events` has a writer

The **ready-buffer test** ("3+ approved tracks, ideally 10–15 minutes, before Tier 3") and
the **transition listening review** both need per-airing data: which tier produced each
block, what transition was used, whether the track completed, how long it actually played.

`play_events` is exactly that table, and nothing writes to it. `tracks.play_count` and
`last_played_at` are denormalised separately and *are* populated — which is why rotation
works and why the gap is invisible until you ask a question only the event rows can answer.
I tried to answer the ready-buffer question from the acceptance run and found an empty table.

I did **not** wire it, deliberately, on two grounds:

1. Everything needed is available at `_record_finished` *except* elapsed seconds, which
   would have to be threaded out of the playout engine. That is the one loop in the station
   that must never stop, and changing it after B3 was already verified green is the wrong
   order of operations.
2. The alternative — recording `played_seconds = duration if completed else 0.0` — is a
   fabricated metric, which §86 forbids. An empty table is honest; a table of invented
   numbers is worse than no table.

So these two items are **not done and not fakeable**. They need `play_events` wired first,
and whether that goes before or after B5 is the operator's call, not mine to assume.

### The UI requirement, honestly

B3 asked the Control Center to show lyric mode, topics, lyrics, market scope, persona,
Trade Fix mentions, validation status, lyric originality and the provider-facing structure.

All of it is now **served and typed**: `GET /api/library/tracks/{id}` returns a `lyrics`
panel and a `submission` panel (including `vocals_downgraded`, the one field an operator
would look for first), and `TrackLyrics` / `ProviderSubmission` are declared in
`frontend/src/lib/api.ts`.

What does not exist is anywhere to render it. The Control Center has `Dashboard`,
`Originality` and `Overlay` — **there is no track-detail view at all**. `fetchTrackDetail`
and the `TrackDetail` type were written and never called by any component, which is the same
pattern §7 describes. Building that page is real UI work on files another terminal is
currently editing, and it is not lyric work, so I stopped at the contract rather than
half-build a page. Flagging it rather than quietly reporting the UI item as done.
