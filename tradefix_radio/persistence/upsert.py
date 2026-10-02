"""Dialect-aware upsert (ADR-05).

``INSERT ... ON CONFLICT DO UPDATE`` exists on both SQLite and PostgreSQL with the
same semantics, but SQLAlchemy exposes it through dialect-specific constructs. A
single-statement upsert matters here rather than SELECT-then-INSERT because
several station processes touch the same rotation counters and memory keys
concurrently (ADR-08), and the read-then-write version races.

This helper picks the right construct from the session's actual bind, so the same
repository code works on either engine without a branch at every call site.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tradefix_radio.core.errors import PersistenceError


def upsert_statement(
    session: AsyncSession,
    table: Any,
    values: dict[str, Any],
    *,
    index_elements: list[Any],
    update_columns: list[str],
) -> Insert:
    """Build an upsert for the session's dialect.

    Parameters
    ----------
    index_elements:
        Columns forming the conflict target — normally the primary key.
    update_columns:
        Columns to overwrite on conflict. Deliberately explicit rather than "all
        of them": a created-at column must survive a conflict, and listing columns
        is what keeps that from silently breaking.
    """
    bind = session.get_bind()
    dialect = bind.dialect.name

    # Annotated as Any because the two dialect Insert classes are unrelated types
    # that happen to share the on_conflict_do_update interface.
    statement: Any
    if dialect == "sqlite":
        statement = sqlite_insert(table).values(**values)
    elif dialect == "postgresql":
        statement = pg_insert(table).values(**values)
    else:
        raise PersistenceError(
            f"upsert is not implemented for the {dialect!r} dialect; "
            "ADR-05 supports sqlite and postgresql",
            dialect=dialect,
        )

    result: Insert = statement.on_conflict_do_update(
        index_elements=index_elements,
        set_={column: statement.excluded[column] for column in update_columns},
    )
    return result


__all__ = ["upsert_statement"]
