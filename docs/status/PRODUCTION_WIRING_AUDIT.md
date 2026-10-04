# Production wiring audit

**Question:** which declared capabilities of the music station have no production caller?

**Why:** B3 found that the entire lyric path existed, was correct, was unit-tested, and was
called by nothing. The same shape then appeared in title history, play events and UI data
flow. Before tuning anything in B5 we need to know how much more of it there is.

**Scope:** the music station runtime. The visual/anime workstream is excluded throughout —
another terminal owns it. Phase 8 / OBS not started.

---

## Method

Two passes, because neither alone is trustworthy.

1. **Mechanical.** Every module parsed with `ast`; public classes, functions and methods
   collected; each name counted as an attribute access or bare name across production,
   tests and scripts. 147 modules, 1,589 public symbols, 460 with no production reference.
   This produces *candidates*, never conclusions — it matches by bare name, so a method
   called `create` is worthless as evidence and 100 such names were excluded outright.

2. **Empirical.** Row counts for all 28 tables against a database with 247 tracks and 1,577
   recorded state transitions. A table that is empty after that much traffic is either
   unwired or untriggered, and the difference is decidable by reading the writer.

Every finding below was then confirmed by reading the actual call site. Where the two
passes disagreed, the call site won — see "What the scan got wrong" at the end, which is
not a footnote but the reason the method has two passes.

### Empirical baseline

11 of 28 tables are empty:

```
health_events  market_snapshots  market_states  metric_samples  play_events
setting_overrides  station_ids  system_events  topics  track_files  used_seeds
```

---

## The matrix

`Caller?` means a production call path, not a test and not a re-export in `__all__`.

### Core creative path — all wired

| Feature | Interface | Prod caller | Integration test proves caller | Persistence | UI consumer | Status |
|---|---|---|---|---|---|---|
| MusicDirector blueprints | `create_blueprint` | `Scheduler.plan_tracks` | yes (phase-4 gates) | `tracks`, `track_blueprints` | Library, Dashboard | **WIRED** |
| LyricsDirector / composer | `plan`, `compose` | `LyricOrchestrator.generate` | yes (`test_vocal_path`) | `lyrics` (45) | — (see UI row) | **WIRED** |
| Lyric → provider | `lyrics_for` callback | `RadioStation._lyrics_for` | yes — asserts what the provider *received* | `provider_submissions` (5) | — | **WIRED** (B3) |
| GenerationManager | `claim_and_generate` | station generation loop | yes | `generation_jobs` (250) | Jobs panel | **WIRED** |
| Post-production pipeline | `PostProductionPipeline.run` | `_run_post_production` | yes (`test_phase6_pipeline`) | QC/mastering/similarity | Originality | **WIRED** |
| QC | `AudioQualityControl` | pipeline | yes | `track_qc_results` (163), `track_qc_checks` (3506) | Originality | **WIRED** |
| Fingerprinting | `fingerprint_audio` | pipeline | yes | `audio_fingerprints` (226) | — | **WIRED** |
| Similarity / ReviewResolver | `ReviewResolver.resolve` | pipeline REVIEW branch | yes (B2) | `similarity_results` (215) | Originality page | **WIRED** |
| Mastering | `master_track` | pipeline | yes | `mastering_results` (80) | — | **WIRED** |
| Queue | `RadioQueue` | station + playout | yes | `queue_items` (live snapshot) | Queue panel | **WIRED** |
| Scheduler | `plan_tracks`, `replan` | station schedule stage | yes | via tracks | — | **WIRED** |
| Playout | `PlayoutEngine` | station playout loop | yes (gates A–J) | — | live stream | **WIRED** |
| Track history | `recent_history` | `recover()`, scheduler | yes | `state_transitions` (1577) | History | **WIRED** |
| Provenance | `TrackProvenance` | `_persist_tracks`, `promote_to_production` | yes | `tracks.provenance` (247) | Originality | **WIRED** |
| Market routing | `MarketRouter`, `ActiveMarketService` | station market stage | yes (`test_market_routing`) | `tracks.symbol_at_generation` | Market panel | **WIRED** |
| Buffer metrics | `assess_buffer` | scheduler, API | yes | **none** (see metrics row) | live stream | **PARTIALLY_WIRED** |
| Capacity metrics | `capacity_snapshot` | scheduler, API | yes | **none** | Dashboard | **PARTIALLY_WIRED** |
| Station ID selection | `StationIdLibrary.select` | `_maybe_request_station_id` | yes | `radio_memory` (§96 restore) | — | **WIRED** |

### Gaps

| Feature | Interface | Prod caller | Test proves caller | Persistence | UI consumer | Status |
|---|---|---|---|---|---|---|
| **Play events** | `PlayEventV1`, `PlayEvent`, table, 5 indexes | **none** → *fixed, see gate* | now yes | **0 rows** → writing | none yet | ~~DEAD_INTERFACE~~ → **WIRED** |
| **Track files** | `TrackFileRepository` (all 6 methods) | **none** | no | **0 rows** | `has_audio` renders "No" always | **DEAD_INTERFACE** |
| **Emergency Tier 2 reserve** | `reserve_from_directory` | `api/runner.py:292` ✓ | partial | reads `emergency/` — **0 files, nothing writes it** | reserve minutes = 0.0 | **PARTIALLY_WIRED** |
| **Retention (§36)** | `plan_retention`, `execute_plan`, `RetentionPlan` | **none** | no | n/a | none | **DEAD_INTERFACE** |
| **Generation storage** | `GenerationStorage` (+ `sweep`, `usage_bytes`) | **none** | no | n/a | none | **DEAD_INTERFACE** |
| **Metrics** | `MetricRepository` (`record`, `record_many`, `series`, `aggregate`) | **none** — never constructed | no | **0 rows** | none | **DEAD_INTERFACE** |
| **System events** | `SystemEventRepository` | **none** — never constructed | no | **0 rows** | none | **DEAD_INTERFACE** |
| **Health events** | `HealthEventRepository` | **none** — never constructed | no | **0 rows** | live stream shows *current* health only | **DEAD_INTERFACE** |
| **Market history persistence** | `MarketSnapshotRow`, `MarketStateRow` | **never instantiated** | no | **0 rows** | `/market/history` serves in-memory only | **DEAD_INTERFACE** |
| **Seed registry (§23)** | `used_seeds` table | in-memory set only; **not restored** by `recover()` | no | **0 rows** | none | **PARTIALLY_WIRED** |
| **Title reservation (§99)** | `reserve_title`, `title_exists` | **none** | no | `track_titles` written by `create` instead (247) | — | **PARTIALLY_WIRED** |
| **Buffer-aware profile** | `profile_for_buffer` + `ace_step.buffer_aware_profile` | **none** | no | n/a | setting shown, does nothing | **DEAD_INTERFACE** |
| **Generation adherence** | `measure_adherence`, `AdherenceReport` | **none** | no | n/a | none | **DEAD_INTERFACE** |
| **Track-detail lyrics/submission** | API returns both (B3) | API wired ✓ | no | `lyrics`, `provider_submissions` | **panel exists, does not render them** | **PARTIALLY_WIRED** |
| `topics` table | `Topic` model | never instantiated | no | 0 rows | — | **INTENTIONALLY_UNUSED** (topics load from `topics.yaml`) |
| `station_ids` table | `StationId` model | never instantiated | no | 0 rows | — | **INTENTIONALLY_UNUSED** (library is filesystem; history in `radio_memory`) |
| `setting_overrides` | `SettingOverride` | never instantiated | no | 0 rows | — | **FUTURE_PHASE** |
| OBS status | `/obs/status` | capability stub | n/a | n/a | shows "awaiting Phase 8" | **FUTURE_PHASE** |
| REST alternates (`/status`, `/radio/current`, `/radio/queue`, `/radio/buffer`, `/radio/emergency`, `/markets`, `/capabilities`) | routes exist | no UI caller | — | — | superseded by the live SSE stream | **INTENTIONALLY_UNUSED** |

---

## PLAY_EVENTS — the specific investigation

**Does `PlayoutEngine` emit start/complete/skip/fail?** Yes. It is not a missing signal.

- `playout.py:458` — `await self._on_started(item)` on every item.
- `playout.py:596` — `await self._on_finished(item, completed, reason)` on every item, where
  `completed: bool` and `reason: str` carry the completed / skipped / failed distinction.
- Both hooks are subscribed by the station (`station.py:425-426`).
- `_on_track_finished` publishes a `TrackFinished` bus event and queues the item for the
  persistence worker, which calls `TrackRepository.mark_played`.

**So what is missing?** Only the row. `mark_played` updates `tracks.play_count` and
`last_played_at` — which is why rotation works and why the gap is invisible until you ask a
question only the event rows can answer. Nothing anywhere constructs a `PlayEvent`.

**Verdict: a real bug, not a future phase.** The contract, the model, the table, five
indexes and both hooks are present and active in the current architecture. `PlayEvent`'s own
docstring states its purpose — *"`completed` is persisted separately from the track state so
§75's rule is verifiable after the fact, not just intended"* — and §75 is currently
unverifiable after the fact.

### The honest-data problem, and its answer

A `PlayEvent` row needs `started_at` and `played_seconds`. Earlier I believed neither had an
honest source. That was wrong, and the correction matters:

- `PlayoutEngine._position_frames` counts frames actually written for the current item, and
  is exposed as `position_seconds`. The API already streams it as `PositionUpdate.
  elapsed_seconds`, and `dto.py:228` documents it as coming *"from the playout engine's
  frame counter, not from a timestamp"*.
- `playout.py:576` sets `self._position_frames = 0` — **three lines before** the finish hook
  fires at line 596. The exact honest number is computed, used elsewhere, and discarded
  immediately before its only consumer could read it.

The existing `TrackFinished` bus event already works around this with
`played_seconds=item.duration_seconds if completed else 0.0`. For an interrupted airing that
is wrong, not approximate: a track skipped at 2:58 of 3:00 records as zero. Persisting that
would be a fabricated metric under §86, which is why `play_events` must not be backfilled or
filled from the event as it stands.

**Fix, unambiguous and narrowly scoped:** capture `position_seconds` before the reset, pass
it through the finish hook, write the row from the station's existing persistence worker
(which already has tier, reason, completed, market regime and symbol in hand). Reported
here rather than done inside the audit, per instruction.

---

## READY-BUFFER METRICS — which of A/B/C/D

**D — multiple issues**, and they are not interchangeable. The four quantities asked for:

| Quantity | Available now? | From where | Blocker |
|---|---|---|---|
| **approved audio duration** | **yes** | `tracks.duration_seconds` where state is approved/ready/played | none |
| **ready duration** | **instantaneously only** | `RadioQueue.ready_seconds()` → `BufferAssessment.ready_minutes` | no time series — `metric_samples` is empty and `MetricRepository` is never constructed, so there is no history to plot or test against |
| **played duration** | **no** | would be `play_events.played_seconds` | C — play events unwired |
| **completed duration** | **no** | would be `play_events` filtered on `completed` | C — play events unwired |

So: **not B.** Queue state is sufficient for the live reading and insufficient for anything
historical. The blockers are **C** (play events) for played/completed, plus a second,
independent gap (metrics never persisted) for any *time series* of ready duration. **A** is
false — playout telemetry exists and is accurate; it is simply dropped on the floor.

The distinction matters for the test the user wants. "3+ approved tracks before Tier 3" needs
approved duration (have it) *and* tier-entry timing (needs play events). Substituting
approved for ready, or ready for played, would answer a different question — which is
exactly the kind of substitution this audit exists to prevent.

---

## TRACK DETAIL UI — classification

**Not "UI MISSING", and I reported that wrongly in the B3 report.**

There **is** a track-detail panel: `frontend/src/pages/pages.tsx:747` calls
`api.libraryTrack(selected)` and renders title, id, artist, genre, BPM, key, duration, state,
regime, energy, `has_audio` and the full blueprint JSON.

My B3 claim ("no track-detail view at all", "`fetchTrackDetail` … never called by any
component") came from grepping for `fetchTrackDetail` — a name that does not exist; the
method is `api.libraryTrack`. The B3 report has been corrected.

Correct classification: **PARTIALLY_WIRED.** Backend serves `lyrics` and `submission`; the
existing panel does not render them yet. A field addition to a working component, not a new
page.

One thing that panel shows today is misleading: **"Audio on disk" always renders "No"**,
because `has_audio` counts `track_files` rows and nothing writes them. That is a
user-visible wrong value right now, and it is the same defect as B5.

---

## MIGRATION DEPENDENCY

**What happened:** `debd37e50547` (`track_provenance`, committed in `eb729b7`) declares
`down_revision = 'cda89a476c06'`, and the migration defining `cda89a476c06`
(`operator_feedback`) was never committed. A clean checkout of HEAD had a dangling
`down_revision` and could not run `alembic upgrade head` at all. Fixed in `999e461`.

**Why no test caught it — the part worth keeping.** There are already eleven migration
tests, including `test_exactly_one_migration_head_exists` and
`test_upgrade_head_creates_the_schema_from_nothing`. Every one of them resolves the chain
from `ROOT / "tradefix_radio" / "persistence" / "migrations"` — **the working directory**,
which contains untracked files. The untracked migration satisfied the chain locally while
HEAD was broken, so all eleven passed throughout.

**The missing test** is therefore not "does the chain resolve" but "does the chain resolve
*from what is committed*". Proposed below.

---

## Proposed tests — "what did the downstream component actually receive?"

Framed the way that caught B3: assert on what the *next* component got, never that a helper
returns the right value in isolation.

1. **playback completed → play event persisted.** Drive the station until a track finishes;
   assert a `play_events` row exists with the tier that actually aired it, `completed` true,
   and `played_seconds` within a frame of the audio's real duration. Then skip a track
   mid-way and assert the row records the *partial* elapsed, not zero and not the full
   duration. The second half is the one that matters: it fails against the current
   `duration if completed else 0.0` shortcut.
2. **track approved → queue receives a READY entry.** Assert from the queue's own snapshot,
   not from the pipeline's return value.
3. **title generated → history was consulted.** Spy on the repository and assert the
   director was handed a non-empty recent-title list, newest last. This is the test that
   would have caught the `[-40:]` ordering bug, which no existing test did because the
   repository test and the director test were each correct in isolation.
4. **approved track → `track_files` row exists and `has_audio` is true.** The B5 acceptance
   condition, stated as what the API serves rather than what the repository can do.
5. **migrations resolve from git, not the working tree.** Enumerate migrations via
   `git ls-files`, parse `revision` / `down_revision`, assert no dangling parent and exactly
   one head. Catches the whole class, not the one instance.
6. **generation finished → metric sample recorded** (once metrics are wired), so "capacity
   over time" has a source that is not a log line.

---

## Severity, visibility, and whether it blocks B5

| # | Finding | Severity | User-visible today? | Blocks B5? |
|---|---|---|---|---|
| 1 | `play_events` never written | **High** | No (silently absent) | ~~Yes~~ — **fixed before B5** |
| 2 | `track_files` never written | **High** | **Yes** — "Audio on disk: No" on every track | **This *is* B5** |
| 3 | Retention + `GenerationStorage` dead | **High** | No — `generated/` grows unbounded | **This is B5** |
| 4 | Emergency Tier 2 never populated, and path mismatch | **High** | Partly — reserve shows 0.0 min; station can only fall to Tier 3 | No |
| 5 | Migration tests read the working tree | **High** | No | ~~No~~ — **fixed before B5** |
| 6 | Seed registry lost on restart (§23) | Medium | No | No |
| 7 | `reserve_title` never called (§99) | Medium | Indirectly — a rejected title can return | No |
| 8 | Metrics / system events / health events never persisted | Medium | No — live health only, no history | Partly — no capacity time series |
| 9 | Market history never persisted | Medium | `/market/history` is in-memory only; empty after restart | No |
| 10 | B3 lyrics/submission not rendered | Low | Yes — operator cannot see lyrics | No |
| 11 | `profile_for_buffer` setting does nothing | Low | Setting visible, inert | No |
| 12 | `measure_adherence` dead | Low | No | No |

### Path mismatch detail (#4)

`StoragePaths.emergency_reserve_dir` is `emergency/reserve`; `api/runner.py:292` passes
`settings.paths.emergency_dir` (`emergency/`) to `reserve_from_directory`, which uses a
non-recursive `iterdir()` filtered by `.flac`/`.wav`. So even if something populated the
canonical location, the reserve would still index as empty. Two independent faults, either
of which alone yields a permanently empty Tier 2.

---

## Recommended fix order

**Before B5** — these are missing wiring, not tuning, and B5's own acceptance depends on two
of them:

1. **`play_events`** (#1). Capture `position_seconds` before the reset, thread it through
   the finish hook, write the row. Unblocks the ready-buffer and transition work, and makes
   §75 verifiable. Small, self-contained, one commit.
2. **Git-based migration test** (#5). Ten lines; closes the class.

**B5 proper** — #2 and #3 are the same subsystem and should land together: wire
`TrackFileRepository` at the point masters are written, which makes `has_audio` honest, then
`plan_retention` / `execute_plan` on top of it, because retention cannot sweep files nobody
recorded.

**After B5, before or during T1:**

4. Emergency Tier 2 (#4) — decide whether the reserve is populated from approved tracks or
   curated by hand, then fix the path mismatch either way.
5. Seed registry restore (#6) and `reserve_title` (#7) — both small, both in `recover()` /
   the rejection path.
6. Metrics persistence (#8) — needed for an honest capacity time series, which T1 will want.
7. Render lyrics/submission in the detail panel (#10).

**Not now:** #11 and #12 are inert and harmless; `setting_overrides`, `topics` and
`station_ids` tables are deliberate non-uses, not defects.

---

## Decision gate — and what was done about it

The audit found **current production-critical gaps**, not only minor or future-phase dead
interfaces. Per the stated gate, the two blocking items were fixed first, in one coherent
commit containing no tuning work. Everything else stands as reported above.

### Fixed: `play_events` has a writer

- `PlayoutEngine` now measures each airing before discarding it. `AiredPlay` carries
  `played_seconds` (from the frame counter, captured *before* the `_position_frames = 0`
  reset) and `transition_in` (the §30 decision, which was previously computed, logged and
  thrown away). The finish hook passes it.
- `RadioStation._on_track_started` stamps the start time; the persistence worker writes the
  row through a new `PlayEventRepository`.
- Recorded for **every tier**, including Tier 2 reserve and Tier 3 procedural, which have
  no queue entry and no track row. Excluding them would make "how much of the hour was real
  music" unanswerable, which is the question the emergency tiers exist to raise. Station
  identities are excluded — §31 counts those separately and they are airtime, not
  programming.
- The `TrackFinished` bus event's `played_seconds` was `duration if completed else 0.0`.
  It now carries the measured value. A track cut at 2:58 of 3:00 reported zero.

Verified on a real run via the normal CLI entry point — not only in tests:

```
tier         n   played
procedural   2    90.0s
('procedural-000001', 'procedural', 45.0, completed, 'cold_open', 'BTCUSD')
('procedural-000002', 'procedural', 45.0, completed, 'hard_cut',  'BTCUSD')
```

### Fixed: the migration chain is checked as committed

`test_the_committed_migration_chain_has_no_dangling_parent` and
`..._has_exactly_one_head` resolve the chain from `git ls-files` + `git show HEAD:`, not
from the working directory.

Proven non-vacuous by running the same logic against the commit before the fix:

| ref | migrations | heads | dangling | verdict |
|---|---|---|---|---|
| `eb0c93b` | 7 | `161f873f4932`, `a8fa42dca4ec` | `debd37e50547 -> cda89a476c06` | **would FAIL** |
| `HEAD` | 9 | `185b03dd54d4` | none | passes |

It catches both faults — the dangling parent *and* the second head that the working-tree
version of the same test could not see.

### Ready-buffer metrics: now unblocked

With play events written, three of the four quantities have honest sources:

| Quantity | Source | Status |
|---|---|---|
| approved audio duration | `tracks.duration_seconds` by state | available |
| ready duration | `RadioQueue.ready_seconds()` | live only — still no time series (metrics unwired) |
| played duration | `play_events.played_seconds` | **now available, per tier** |
| completed duration | `play_events` filtered on `completed` | **now available** |

The ready-buffer test B3 left open can now be written. The remaining gap is a *historical*
series for ready duration, which needs the metrics repository wired — medium severity, does
not block B5.

---

## What the scan got wrong

Recorded because the audit's credibility rests on it.

The mechanical pass flagged `EmergencyManager.load_reserve` as having no production caller —
true — and the obvious inference was that the whole Tier 2 reserve was dead. It is not:
`api/runner.py:292` populates the reserve through the **constructor** (`reserve=`), a path
no bare-name scan can see. The real defect turned out to be different and narrower: the read
path is wired, and nothing fills the directory it reads.

Had I reported the inference instead of opening the file, this document would have contained
a confident, wrong finding about the station's emergency behaviour. Every "none" in the
`Prod caller?` column above was checked by reading the call site, not by trusting the count.
