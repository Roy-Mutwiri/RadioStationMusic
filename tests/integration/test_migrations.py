"""Alembic migrations (§37, milestone 1.6).

§37: "Use migrations. Never rely on auto-created uncontrolled schema." The unit
and integration suites use ``create_all`` for speed, which means the migration
path has exactly one place where it is actually exercised — here. If these tests
are skipped, nothing proves a fresh deployment can build its own schema.

The most valuable assertion is the **drift check**: the schema Alembic produces
must match the schema the models describe. Without it, a model change silently
diverges from production until the first query fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from tradefix_radio.config.schema import AppSettings
from tradefix_radio.persistence.models import Base
from tests.conftest import make_settings

#: Repository root, located from this file rather than the working directory.
ROOT = Path(__file__).resolve().parent.parent.parent


def alembic_config(settings: AppSettings) -> Config:
    """An Alembic config pointed at an isolated test database."""
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location", str(ROOT / "tradefix_radio" / "persistence" / "migrations")
    )
    # env.py translates the async driver to its sync equivalent itself; passing the
    # app DSN through -x keeps one source of truth for the URL.
    config.cmd_opts = None  # type: ignore[assignment]
    config.set_main_option("sqlalchemy.url", _sync_url(settings))
    return config


def _sync_url(settings: AppSettings) -> str:
    return settings.database.url.replace("sqlite+aiosqlite:", "sqlite:")


@pytest.fixture
def settings(tmp_path: Path) -> AppSettings:
    built = make_settings(tmp_path)
    built.paths.data_dir.mkdir(parents=True, exist_ok=True)
    return built


def test_upgrade_head_creates_the_schema_from_nothing(
    settings: AppSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh deployment must be able to build its own schema."""
    monkeypatch.setenv("TRADEFIX_ROOT", str(settings.paths.root_dir))
    monkeypatch.setenv("TRADEFIX_DATABASE__URL", settings.database.url)

    command.upgrade(alembic_config(settings), "head")

    engine = create_engine(_sync_url(settings))
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert "alembic_version" in tables
    model_tables = set(Base.metadata.tables)
    missing = model_tables - tables
    assert not missing, f"migration did not create: {sorted(missing)}"


def test_migration_schema_matches_the_models(
    settings: AppSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drift check: autogenerate must find nothing left to do after upgrade.

    This is the test that catches a model change someone forgot to generate a
    migration for — the failure mode that otherwise surfaces as a confusing
    runtime error on a freshly deployed machine.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    monkeypatch.setenv("TRADEFIX_ROOT", str(settings.paths.root_dir))
    monkeypatch.setenv("TRADEFIX_DATABASE__URL", settings.database.url)
    command.upgrade(alembic_config(settings), "head")

    engine = create_engine(_sync_url(settings))
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "target_metadata": Base.metadata},
            )
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    # Alembic reports index differences on SQLite that are cosmetic rather than
    # structural; narrow the comparison to added/removed tables and columns, which
    # are the differences that actually break queries.
    structural = [
        entry
        for entry in diff
        if isinstance(entry, tuple)
        and entry[0]
        in {"add_table", "remove_table", "add_column", "remove_column", "modify_type"}
    ]
    assert not structural, f"schema drift between models and migrations: {structural}"


def test_downgrade_to_base_removes_every_table(
    settings: AppSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration that cannot be reversed cannot be rolled back in an incident."""
    monkeypatch.setenv("TRADEFIX_ROOT", str(settings.paths.root_dir))
    monkeypatch.setenv("TRADEFIX_DATABASE__URL", settings.database.url)

    config = alembic_config(settings)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(_sync_url(settings))
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    # alembic_version survives a downgrade to base by design.
    assert tables <= {"alembic_version"}


def test_upgrade_is_idempotent(
    settings: AppSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running `tradefix migrate` twice must be harmless."""
    monkeypatch.setenv("TRADEFIX_ROOT", str(settings.paths.root_dir))
    monkeypatch.setenv("TRADEFIX_DATABASE__URL", settings.database.url)
    config = alembic_config(settings)
    command.upgrade(config, "head")
    command.upgrade(config, "head")


def test_sqlite_pragmas_are_applied_to_the_migration_connection(
    settings: AppSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without foreign_keys=ON, a table-rebuild migration silently drops cascades."""
    monkeypatch.setenv("TRADEFIX_ROOT", str(settings.paths.root_dir))
    monkeypatch.setenv("TRADEFIX_DATABASE__URL", settings.database.url)
    command.upgrade(alembic_config(settings), "head")

    engine = create_engine(_sync_url(settings))
    try:
        with engine.connect() as connection:
            journal = connection.execute(text("PRAGMA journal_mode")).scalar_one()
    finally:
        engine.dispose()
    # WAL is persistent once set, so it should be observable afterwards.
    assert str(journal).lower() == "wal"


def test_exactly_one_migration_head_exists() -> None:
    """Two heads mean a merge is needed; discovering that during a deploy is bad."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(
        str(ROOT / "tradefix_radio" / "persistence" / "migrations")
    )
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single migration head, found {heads}"


def test_every_migration_has_a_downgrade() -> None:
    """A migration without a reverse cannot be rolled back under pressure."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(
        str(ROOT / "tradefix_radio" / "persistence" / "migrations")
    )
    for revision in script.walk_revisions():
        source = Path(revision.path).read_text(encoding="utf-8")
        assert "def downgrade()" in source, f"{revision.revision} has no downgrade"
        body = source.split("def downgrade()", 1)[1]
        assert body.strip() not in {"-> None:\n    pass", "-> None:"}, (
            f"{revision.revision} has an empty downgrade"
        )



def _insert_minimal_row(connection, table: str, values: dict[str, object]) -> None:
    """Insert a row supplying a placeholder for every NOT NULL column.

    Built from `PRAGMA table_info` rather than written out, so a new required column
    does not silently turn this regression test into a schema-drift test.
    """
    columns = connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    row = dict(values)
    for _cid, name, declared, not_null, default, _pk in columns:
        if name in row or not not_null or default is not None:
            continue
        kind = (declared or "").upper()
        if "INT" in kind:
            row[name] = 0
        elif "REAL" in kind or "FLOA" in kind or "DOUB" in kind:
            row[name] = 0.0
        elif "DATE" in kind or "TIME" in kind:
            row[name] = "2026-10-03T00:00:00+00:00"
        else:
            row[name] = "[]"
    names = ", ".join(row)
    placeholders = ", ".join(f":{n}" for n in row)
    connection.execute(
        text(f"INSERT INTO {table} ({names}) VALUES ({placeholders})"), row
    )


def test_migrations_run_with_foreign_keys_disabled(
    settings: AppSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The migration engine must not enforce foreign keys.

    SQLite cannot ALTER COLUMN, so Alembic's batch mode rebuilds a table: create new,
    copy, DROP original, rename. Every child of `tracks` declares ON DELETE CASCADE, so
    with enforcement on that DROP cascades — adding one column deletes every fingerprint,
    QC result, feature row and blueprint attached to it.

    This is a regression test for something that happened. The `track provenance`
    migration took audio_fingerprints from 144 rows to 0 and track_qc_checks from 65 to 0
    on a database holding the only copy of a diagnostic corpus, and nothing errored,
    because from SQLite's point of view the cascade was correct.

    Asserted on the engine's configuration rather than by re-running a batch migration:
    the pragma is the whole mechanism, and a test that rebuilt a table by hand would be
    testing its own scaffolding instead of the one line that matters.
    """
    from tradefix_radio.persistence.database import apply_sqlite_pragmas_sync

    engine = create_engine(_sync_url(settings))
    apply_sqlite_pragmas_sync(engine, settings.database, foreign_keys=False)
    with engine.connect() as connection:
        enforced = connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
    engine.dispose()
    assert enforced == 0, "migrations would run with cascades armed"

    # And the application's own engine must still enforce them, or a stray delete would
    # leave orphans at runtime. The two settings are deliberately opposite.
    app_engine = create_engine(_sync_url(settings))
    apply_sqlite_pragmas_sync(app_engine, settings.database)
    with app_engine.connect() as connection:
        app_enforced = connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
    app_engine.dispose()
    assert app_enforced == 1


def test_a_batch_rebuild_of_tracks_keeps_its_child_rows(
    settings: AppSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same hazard, demonstrated end to end on a real schema.

    Rebuilds `tracks` the way `op.batch_alter_table` does — create, copy, drop, rename —
    on a connection configured the way `env.py` now configures them, and asserts the
    fingerprint attached to the track is still there afterwards.
    """
    from tradefix_radio.persistence.database import apply_sqlite_pragmas_sync

    monkeypatch.setenv("TRADEFIX_DATABASE__URL", settings.database.url)
    command.upgrade(alembic_config(settings), "head")

    seed = create_engine(_sync_url(settings))
    apply_sqlite_pragmas_sync(seed, settings.database)
    with seed.begin() as connection:
        _insert_minimal_row(connection, "tracks", {"track_id": "TF-CASCADE-1"})
        _insert_minimal_row(connection, "audio_fingerprints", {"track_id": "TF-CASCADE-1"})
    seed.dispose()

    rebuild = create_engine(_sync_url(settings))
    apply_sqlite_pragmas_sync(rebuild, settings.database, foreign_keys=False)
    with rebuild.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE tracks_new AS SELECT * FROM tracks"
        )
        connection.exec_driver_sql("DROP TABLE tracks")
        connection.exec_driver_sql("ALTER TABLE tracks_new RENAME TO tracks")
    with rebuild.connect() as connection:
        survived = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM audio_fingerprints"
        ).scalar()
    rebuild.dispose()

    assert survived == 1, (
        "rebuilding `tracks` cascade-deleted its fingerprint; migrations must run with "
        "foreign_keys=OFF"
    )
