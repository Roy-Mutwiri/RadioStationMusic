"""Alembic environment.

Two things make this different from the generated template:

1. **The URL comes from validated settings**, not ``alembic.ini``. That keeps a
   PostgreSQL password out of a tracked file (§68) and guarantees migrations run
   against the same database the station uses — the classic failure being a
   migration applied to ``./alembic.db`` while the app reads ``data/tradefix.db``.

2. **The async driver is translated to its sync equivalent.** Alembic runs
   migrations synchronously; our DSNs name async drivers. Rather than maintaining
   two URLs that can disagree, the one DSN is rewritten here.

``render_as_batch`` is enabled for SQLite: SQLite cannot ``ALTER COLUMN``, so
Alembic must recreate the table. Without batch mode, the first migration that
alters a column fails on the default development database.
"""

from __future__ import annotations

from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from tradefix_radio.config.loader import load_settings
from tradefix_radio.persistence.database import (
    apply_sqlite_pragmas_sync,
    sync_database_url,
)
from tradefix_radio.persistence.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

def _resolve_url() -> tuple[str, bool]:
    """Return ``(sync_url, is_sqlite)`` from settings or an ``-x url=`` override."""
    overrides = context.get_x_argument(as_dictionary=True)
    url = overrides.get("url")
    if url is None:
        settings = load_settings()
        url = settings.database.url
    url = sync_database_url(url)
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        # Alembic connects without going through Database.connect(), which is
        # where the application creates the data directory. A first-run `alembic
        # upgrade head` on a clean checkout would otherwise fail with the
        # unhelpful "unable to open database file".
        _, marker, raw_path = url.partition(":///")
        if marker and raw_path and raw_path != ":memory:":
            Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
    return url, is_sqlite


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting. Used to review a migration."""
    url, is_sqlite = _resolve_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=is_sqlite,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and apply migrations."""
    url, is_sqlite = _resolve_url()
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = url
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    if is_sqlite:
        settings = load_settings()
        # foreign_keys=OFF for migrations; see the helper for why this is required
        # rather than merely convenient.
        apply_sqlite_pragmas_sync(connectable, settings.database, foreign_keys=False)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=is_sqlite,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()

        if is_sqlite:
            # Report rather than repair. A migration that left a dangling child row has a
            # bug, and with enforcement off nothing else will catch it — but deleting the
            # offending rows here would destroy the evidence of the very defect this check
            # exists to surface.
            violations = list(
                connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            )
            if violations:
                raise RuntimeError(
                    "migration left foreign-key violations: "
                    f"{violations[:10]}{' ...' if len(violations) > 10 else ''}"
                )
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
