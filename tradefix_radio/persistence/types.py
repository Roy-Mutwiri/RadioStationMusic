"""Portable column types (ADR-05).

ADR-05 commits to one schema that runs identically on SQLite and PostgreSQL. Two
places that otherwise silently diverge are fixed here.

**Timezones.** SQLite has no timezone-aware type: writing an aware datetime and
reading it back yields a naive one, in an unspecified zone. Every timestamp in
this system is UTC (see :mod:`tradefix_radio.core.clock`), and a naive datetime
leaking into the market engine would corrupt session classification and all the
§12 "within 24 hours" horizons. :class:`UtcDateTime` normalises on the way in and
re-attaches UTC on the way out, so application code only ever sees aware UTC.

**Structured values.** ADR-05 bans PostgreSQL-only types, so no ``JSONB`` and no
native ``ARRAY``. :class:`PortableJson` stores canonical JSON text, which
round-trips byte-identically on both engines. The cost is no server-side JSON
querying — acceptable, because every query this station runs filters on scalar
columns that are indexed, and the JSON payloads are read whole.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Dialect, Text
from sqlalchemy.types import DateTime, TypeDecorator

UTC = timezone.utc


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetime that survives SQLite.

    Rejects naive datetimes outright rather than guessing their zone. Guessing is
    how a station ends up claiming the London session opened at 3 a.m.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "naive datetime passed to a UtcDateTime column; all timestamps must "
                "be timezone-aware UTC (use Clock.now())"
            )
        return value.astimezone(UTC)

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # SQLite path: the stored value is UTC by construction above.
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class PortableJson(TypeDecorator[Any]):
    """JSON stored as canonical text, identical on SQLite and PostgreSQL.

    ``sort_keys=True`` is not cosmetic: it makes the stored text deterministic, so
    the same blueprint always serialises to the same bytes. That is what lets a
    row be compared or hashed without re-parsing, and it keeps diffs in exported
    fixtures readable.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_fallback)

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            # Some drivers may already decode; accept it rather than double-parsing.
            return value
        return json.loads(value)


def _fallback(value: Any) -> Any:
    """Serialise the few non-JSON types that reach these columns."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"cannot serialise {type(value).__name__} into a PortableJson column")


__all__ = ["PortableJson", "UtcDateTime"]
