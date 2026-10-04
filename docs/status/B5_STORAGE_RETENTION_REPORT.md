# B5 — track file lifecycle, storage and retention

**Status:** implemented. Legacy corpus deliberately untouched — see
`docs/status/B5_LEGACY_STORAGE_AUDIT.md`.
**Scope:** music station only. The anime/office/visual workstream was not touched.
**Not started:** Phase 8 / OBS.

---

## 1. The problem, measured

Three components existed and none were connected to a lifecycle:

* `TrackFileRepository` — complete, including a `retention_candidates` query that already
  did a correlated EXISTS against live queue states. **No production caller.**
* `storage/retention.py` — a pure, tested planner with protection rules. **No caller.**
* `generation/storage.py` — `GenerationStorage`, with its *own* `TrackStage` enum and a
  filename-scanning `sweep()`. **No caller**, and a second answer to "what is this file".

Production used none of them: raw audio went to `generated/<track_id>.wav` from
`station._output_path_for`, masters to `generated/mastered/` from the pipeline. The result,
at the start of B5: **328 files, 8.68 GB, zero `track_files` rows.**

Two consequences, one visible and one not:

* the Library's detail panel rendered **"Audio on disk: No" for all 247 tracks**, because
  it counted rows that were never written;
* retention could not run at all, because it could not tell a master from a discarded
  render — and `generated/` grew without bound.

---

## 2. Chosen authority

`StoragePaths` + `TrackFileRepository` + `storage/retention.py`. `track_files` is the
authoritative metadata record for station-owned audio.

**`GenerationStorage` is demoted.** It is no longer an ownership or retention authority:
nothing calls its `sweep()`, and `TrackStage` no longer competes with `FileRole`. The
module remains available for path/placement helpers. There is now exactly one enum that
answers "what is this file", and exactly one engine that decides whether it may be deleted.

### `FileRole`

Extended rather than duplicated. Values are the strings already in `track_files.role`;
`RAW_GENERATION` keeps the value `"raw"` deliberately — the name was wrong, the stored data
was not.

| member | value | meaning |
|---|---|---|
| `RAW_GENERATION` | `raw` | provider output |
| `MASTER` | `master` | the file that airs |
| `REJECTED_RAW` | `rejected_raw` | output of a refused candidate, kept for triage |
| `QUARANTINE` | `quarantine` | failed verification; never queued |
| `EMERGENCY` | `emergency` | Tier 2 reserve (§33) |
| `STATION_ID` | `station_id` | identifier clips (§31) |
| `ARTWORK` | `artwork` | cover art (§54); not audio, tracked for the same reasons |

Two properties carry the rules that used to be scattered: `is_audio`, and `is_airable` —
an explicit allowlist of `{MASTER, EMERGENCY, STATION_ID}`, so a role added later has to
argue its way onto the air rather than arrive there by default. Quarantine is the case
that matters: it is audio, on disk, with a track id, and must never be broadcast.

---

## 3. The write path

`FileRegistrar.register` is the one place a file becomes owned. Fixed order:

1. move (or copy) into a `.partial` staging name **beside** the destination;
2. verify the bytes decode, and measure them — the file, not the request;
3. hash (SHA-256, chunked);
4. atomic `Path.replace` into place;
5. insert the row.

Steps 1–4 run in a worker thread via `asyncio.to_thread`. That is not tidiness: the event
loop they would otherwise block is the one pacing the audio sink, and §86 does not accept
"the broadcast stuttered while we checksummed a 45 MB file".

### Why that order

File and database operations fail independently, so one must go second. The failure this
order makes **impossible** is a row promising audio that is not there — the dangerous one,
because a row is what the queue and the retention planner trust. The failures it permits
are both recoverable and visible:

| failure | result | recovery |
|---|---|---|
| crash before step 4 | a `.partial` file | identifiable, swept by `sweep_staging`, never mistaken for audio |
| DB insert fails after step 4 | real file, no row | `storage audit` reports `FILE_WITHOUT_ROW` |
| source missing / undecodable | `StorageError`, nothing placed, no row | caller quarantines and marks the slot unavailable |

### Wiring

* **Raw** — `RadioStation._accept_generated` registers provider output as
  `RAW_GENERATION` *before* anything downstream reads it, and replaces the result's path
  with the managed one. This also subsumes the old `read_info` readability check: the
  registrar verifies and measures as part of taking ownership.
* **Master** — `_run_post_production` registers the mastered file as `MASTER` *before*
  returning, so `mark_ready` cannot put a track on air whose bytes the database does not
  know about. If registration fails the file is quarantined and the slot is marked
  unavailable — an unusable master is a post-production failure, not a storage footnote.
* **Mastering declined** — post-production may approve without mastering. The raw file
  airs; it is already registered, so the queue still references owned bytes.

---

## 4. "Audio on disk", corrected

`GET /api/library/tracks/{id}` now returns an `audio` object keyed by role, with the
filesystem actually consulted:

| status | meaning |
|---|---|
| `present` | live row, bytes are there |
| `deleted` | retention reclaimed them on purpose — expected, not a fault |
| `missing` | a row says the file exists and it does not — a real defect |
| `unknown` | no row at all; true of every pre-B5 track |

`has_audio` survives for callers that want one boolean, now meaning `any(status ==
present)`.

The Library panel renders **Master** and **Raw** rows instead of one "Audio on disk: Yes/No".
The old answer was not merely wrong, it was conflating three situations an operator needs
to tell apart: reclaimed on purpose, genuinely lost, and never recorded.

---

## 5. Retention

The existing DB-authoritative planner, extended with provenance.

### Policy

```
provenance_retention_days = {production_radio: 60, engineering_test: 2,
                             manual_lab: 14, simulation: 0.5}
role_retention_days       = {raw: 2, rejected_raw: 7, quarantine: 30}
```

The **tighter** of the two binds. They answer different questions — "how precious is the
track" and "how redundant is this particular file" — and a raw render of a production
track is still a raw render.

Two deliberate abstentions:

* **`unknown` provenance is never auto-deleted**, and the settings validator *rejects* a
  config that adds an `unknown` key. A file whose provenance cannot be established is
  ambiguous, not expendable, and the station has 8.68 GB of exactly that.
* **production masters** take no provenance allowance: they are governed by
  `keep_recent_masters` + `audio_retention_days`, which is already an authority. Adding a
  second number would be the two-authorities mistake B5 exists to remove.

### Protections

Unchanged and still enforced by `_is_protected`: currently playing, in the live queue
(`READY`/`QUEUED`/`PLAYING` via correlated EXISTS, so there is no stale-view window),
`retain_forever`, and anything under the protected emergency tree.

---

## 6. CLI

```
tradefix storage audit [--verify-hashes] [--list-unknown]
tradefix storage stats
tradefix storage sweep [--dry-run] [--yes] [--limit N]
```

`sweep` is a dry run unless `--yes` is passed, and exits non-zero in that case so a script
cannot mistake a refusal for a completed sweep. Every eligible file prints with the reason
it is eligible.

Real output is in `docs/status/B5_LEGACY_STORAGE_AUDIT.md`.

---

## 7. Legacy corpus

328 files, 8.68 GB, all unowned. Classified from evidence: 78 provable masters (3.82 GB),
227 provable raw generations (3.55 GB), **23 unclassifiable (1.32 GB)** — 13 whose filename
names no existing track, 10 in the masters location with no mastering record. Both groups
are consistent with the SQLite cascade incident in `docs/MIGRATIONS.md`.

Current sweep result against this corpus: **0 candidates, 0 bytes.** A file the database
does not know about cannot be swept. That is the safety property working, and it is also
why the corpus will never shrink on its own — adoption has to be a separate, deliberate
step, not a side effect of the first sweep.

---

## 8. Tests

| file | count | what it pins |
|---|---:|---|
| `tests/unit/test_storage_registrar.py` | 7 | measured metadata, copy-vs-move, missing source, undecodable audio, zero-length render, **row failure leaves a file not a false claim**, staging sweep only when cold |
| `tests/unit/test_retention.py` | 50 (+5) | existing protections, plus unknown-never-reclaimed, simulation reclaimed fast, fresh engineering kept, tighter-of-two, quarantine outlives rejected |
| `tests/integration/test_vocal_path.py` | 11 (+2) | raw and master registered before anything consumes them; every queued track resolves to a file a row owns |

The integration assertions are deliberately on `track_files` and on what the *queue*
resolves, not on the filesystem: the defect was bytes existing with nothing recording them,
so a test that checks the file exists would have passed throughout.

---

## 8a. Migration safety

**B5 required no migration.** `track_files` and every column it uses already existed from
the initial schema; the new roles are new *values* in an existing `String(24)` column, and
the retention policy lives in configuration rather than the database.

That is the best possible outcome under the preservation policy established after the
cascade incident: the policy's hazard is a batch rebuild of a parent table dropping its
children, and a change that rebuilds nothing cannot trigger it. `alembic upgrade head` from
a clean `git archive` of HEAD still exits 0 across all nine migrations.

---

## 9. Acceptance gates

| gate | status |
|---|---|
| A. raw produces authoritative metadata | **met** — integration-tested |
| B. master produces authoritative metadata | **met** |
| C. queue/playout resolves a real master | **met** — every ready entry maps to a row |
| D. Track Detail no longer falsely reports | **met** — four states, filesystem consulted |
| E. dry-run reports eligible files accurately | **met** — 0 against the legacy corpus, correctly |
| F. playing/queued audio cannot be swept | **met** — pre-existing guards, still tested |
| G. metadata survives deletion | **met** — `deleted_at`, row retained (ADR-07) |
| H. production provenance protected | **met** — archive rules, no short allowance |
| I. engineering/test content reclaimable | **met** — 2-day and 0.5-day allowances |
| J. audit finds DB/file inconsistencies | **met** — both directions, run against real data |
| K. affected suites green | see §11 |
| L. Phase 4 gates green | see §11 |
| M. ruff/mypy clean | **met** |

---

## 10. Known limitations

Stated rather than quietly left:

* **No reconciliation command yet.** The 23 unknown and 305 provable legacy files are
  reported, not adopted. Adoption is deliberately a separate step (see the legacy audit's
  recommendation) and is not in this commit.
* **Disk-pressure sweep is partially wired.** `plan_retention` already takes `free_bytes`
  and runs a pressure pass; `critical_free_gb` is defined and validated, but no scheduled
  sweep task calls it on a timer. The CLI is the only trigger today.
* **`tradefix doctor` has no storage section.** The data is available via
  `StorageInventory`; the doctor integration is not done.
* **Duplicate content is not confirmed.** 18 same-size groups covering 46 files are
  reported as a hint; proving it needs `--verify-hashes` over 8.68 GB, which has not been
  run.
* **`GenerationStorage.sweep()` still exists.** It has no caller and is no longer an
  authority, but it has not been deleted — removing a public function is a separate change
  from demoting it, and its tests still pass.
* **Metrics** (audio bytes by class, reclaimable, last sweep) are not emitted; the
  `MetricRepository` they would use is itself unwired, which the production wiring audit
  records as a separate finding.
