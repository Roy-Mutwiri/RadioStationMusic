# B5 — the legacy storage corpus, before anything is deleted

**Nothing in this report has been deleted, moved, or registered.** It is the read-only
inventory produced by `tradefix storage audit` against the station's real storage roots,
taken before the first sweep so that the sweep has something to be judged against.

Measured 2026-10-04 on `D:\.Music\generated` and `D:\.Music\emergency`.

---

## The headline

| | files | bytes |
|---|---:|---:|
| **Total on disk** | **328** | **8.68 GB** |
| Owned (has a `track_files` row) | 0 | 0.00 GB |
| Unowned | 328 | 8.68 GB |

Every audio file the station has ever produced is unowned. That is not a surprise — it is
the defect B5 exists to fix, and it is stated here as a number so the fix has a baseline.

---

## Classification

Role is established from evidence or not at all. A file is called a master because it sits
in the masters location **and** names a track that exists **and** has a mastering record;
two of those three is `UNKNOWN_LEGACY`.

| classification | files | bytes | basis |
|---|---:|---:|---|
| `master` | 78 | 3.82 GB | masters location + known track + mastering record |
| `raw_generation` | 227 | 3.55 GB | provider output location + known track |
| `unknown_legacy` | 23 | 1.32 GB | could not be proven |

**227 distinct tracks** are linked to at least one file on disk.

### Why 23 files could not be classified

| files | bytes | reason |
|---:|---:|---|
| 13 | 0.86 GB | the filename does not name a track that exists |
| 10 | 0.46 GB | in the masters location, track exists, but no mastering record |

Both groups are consistent with one known event: the SQLite cascade incident recorded in
`docs/MIGRATIONS.md`, where a batch rebuild of `tracks` under `ON DELETE CASCADE` deleted
every child row. `mastering_results` currently holds 80 rows against 88 files in the
masters directory, and the 13 unnamed files are from tracks that no longer have rows.

They are **not** being treated as deletable. Their bytes are intact and the inventory
reports them every time it runs.

---

## Findings

| finding | count |
|---|---:|
| `file_without_row` | 328 |
| `row_without_file` | 0 |
| `hash_mismatch` | 0 (not checked — pass `--verify-hashes`) |
| `stray_staging` | 0 |
| non-audio files in audio roots | 0 |

`row_without_file` is the dangerous direction — the database promising audio that is not
there — and there are none, trivially, because there are no rows at all.

### Possible duplicate content

18 groups of files share an exact byte size, covering 46 files. Size equality is a *hint*,
not a duplicate: two 180-second renders at the same rate are the same length. Confirming
would need `--verify-hashes`, which re-reads 8.68 GB, so it has not been run as part of
this report and no file has been treated as a duplicate.

---

## What would be deleted under current policy

**Nothing.**

```
$ tradefix storage sweep
RETENTION DRY RUN
  candidates        0
  would delete      0
  reclaimable       0.00 GB
  free now          163.4 GB
  nothing is eligible.
  nothing was deleted. Pass --yes to execute this plan.
```

Zero candidates, for a reason worth stating plainly: the retention planner reads
`track_files`, and these 328 files have no rows. **A file the database does not know about
cannot be swept.** That is the intended safety property — the engine deletes what it owns
and is blind to everything else — and it is why the legacy corpus is safe while this is
sorted out, and equally why it will never shrink on its own.

Even once rows exist, two further protections apply to this corpus:

* every one of these tracks predates provenance or carries `unknown`, and
  `provenance_retention_days` deliberately has no `unknown` key — the settings validator
  *rejects* a config that adds one;
* `UNKNOWN_LEGACY` files have no role the planner recognises, so they are skipped rather
  than guessed at.

---

## Reclaimable, if the corpus were adopted

Hypothetical, not a plan. If every provable file were registered with its current
provenance, the policy would make these classes eligible:

| class | files | bytes | why |
|---|---:|---:|---|
| `raw_generation` with a verified master | ≤227 | ≤3.55 GB | raw output is superseded by its master (2-day role allowance) |
| `master`, `production_radio` | 78 | 3.82 GB | kept — archive of the last 200 masters |
| `unknown_legacy` | 23 | 1.32 GB | **never** auto-deleted |

So the realistic ceiling is **~3.55 GB of raw output**, and only for tracks whose master is
present and verified. The 3.82 GB of masters is the station's catalogue, and the 1.32 GB of
unknowns is not the engine's to judge.

---

## Recommendation

1. Leave the corpus alone for now. New output is registered from this commit onward, so the
   unowned set is now closed and will not grow.
2. Adopt provable files in a **separate, explicit** step — a reconciliation command, run
   deliberately, not a side effect of a sweep. It should register `raw_generation` and
   `master` with the owning track's provenance and leave `unknown_legacy` untouched.
3. Only then run `sweep --dry-run`, review the per-file reasons, and decide.

The order matters: adoption makes files *eligible*, and doing it in the same motion as a
sweep would mean the first run of the retention engine is also the first time anyone sees
what it considers deletable.
