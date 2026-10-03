# Migrations

Alembic, SQLite in development and PostgreSQL-compatible models. Nothing here is unusual
except one rule that exists because it was learned the expensive way.

## The rule: a migration must preserve station history

A migration test that asserts "upgrade succeeded" is **not sufficient**. The defect that
prompted this policy raised no error, left the schema perfectly valid, and passed every
test the project had:

```
audio_fingerprints   144 -> 0
track_qc_results       3 -> 0
track_qc_checks       65 -> 0
audio_features, mastering_results, track_blueprints, lyric_fingerprints -> 0
```

Only the rows were gone, on a database holding the only copy of a diagnostic corpus.

### Why it happened

SQLite cannot `ALTER COLUMN`, so Alembic's batch mode rebuilds the table:

```
CREATE TABLE tracks_new (...)
INSERT INTO tracks_new SELECT ... FROM tracks
DROP TABLE tracks            <-- here
ALTER TABLE tracks_new RENAME TO tracks
```

Every child of `tracks` declares `ON DELETE CASCADE`, and the application turns foreign
keys on. That `DROP` therefore cascades. Adding one column to a parent table deletes
every row attached to it, and SQLite is behaving exactly as instructed.

Two migrations did this before it was noticed: `symbol_at_generation` and
`track provenance`.

### What prevents it now

`apply_sqlite_pragmas_sync` takes a `foreign_keys` argument, and `env.py` asks for it
**off** during migrations while the application keeps it **on**. The two want opposite
things and the difference is destructive, so it is a parameter rather than a constant.

It is applied on *connect*, not by executing the pragma on a live connection. Two reasons,
both discovered by getting it wrong first:

* SQLite silently ignores `PRAGMA foreign_keys` inside a transaction.
* Issuing it through SQLAlchemy opens an implicit transaction, which left the migration's
  own work uncommitted and rolled back six tables.

Afterwards `PRAGMA foreign_key_check` runs and **reports**. A migration that leaves a
dangling child row has a bug of its own, and deleting the offending rows to make the check
pass would destroy the evidence of the defect the check exists to surface.

## Required tests for every migration

`tests/integration/test_migrations.py` holds these, and they apply to all future
migrations rather than to the ones that happened to need them:

| test | what it guarantees |
|---|---|
| `test_migration_schema_matches_the_models` | the schema Alembic builds equals the models |
| `test_migrations_run_with_foreign_keys_disabled` | migration engine off, application engine on |
| `test_a_batch_rebuild_of_tracks_keeps_its_child_rows` | the mechanism, demonstrated |
| `test_migrating_to_head_preserves_every_child_row` | full relational fixture survives a real upgrade, no dangling keys |
| `test_downgrading_one_revision_preserves_child_rows` | the same in reverse |
| `test_every_migration_has_a_downgrade` | reversibility |
| `test_exactly_one_migration_head_exists` | no accidental branch |

The preservation tests populate `CHILD_TABLES` — every child of `tracks`, plus
`track_qc_checks`, which hangs off `track_qc_results` and so exercises a two-level chain.
That list is written out rather than discovered: adding a child table and forgetting to
list it should be a visible omission, not a silent gap in coverage.

## Writing a migration

1. `alembic revision --autogenerate -m "short description"`.
2. **Replace the generated docstring.** Say what the migration does to *data*, not just to
   the schema — particularly any backfill, and why the backfilled value is defensible
   rather than convenient.
3. Delete the `### commands auto generated ###` markers.
4. A new `NOT NULL` column needs a `server_default`, and the model needs the matching
   `server_default` or `alembic check` will keep reporting drift.
5. Run `alembic upgrade head`, then `alembic check`, then the migration tests.

## Backfills

Backfill to what is **provable**, never to what is convenient.

`track provenance` is the worked example. Nothing recorded where the existing 154 tracks
came from, so every one became `unknown` rather than being assigned a plausible class. A
provenance column containing a guess is worse than one that admits it does not know, and
`unknown` is excluded from graded novelty for exactly that reason — what is known about
those rows is that nothing proves a listener heard them.

Do not fabricate historical rows that were lost. If the source data still exists and
recomputation is worthwhile, recompute explicitly and record that it was recomputed.
Otherwise leave the absence visible.
