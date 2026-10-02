"""Persistence: engine, schema, migrations and repositories (§37, ADR-05).

One SQLAlchemy model layer serves both SQLite (default) and PostgreSQL. Schema
changes go through Alembic — ``tradefix migrate`` — never through
``create_all``, which exists only for throwaway test databases.
"""

from tradefix_radio.persistence.database import Database
from tradefix_radio.persistence.models import Base
from tradefix_radio.persistence.types import PortableJson, UtcDateTime

__all__ = ["Base", "Database", "PortableJson", "UtcDateTime"]
