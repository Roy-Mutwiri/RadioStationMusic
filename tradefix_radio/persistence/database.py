"""Async engine and session management (ADR-05).

The SQLite pragmas below are not tuning preferences — they are what makes the
three-process layout (ADR-08) work on one file:

``journal_mode=WAL``
    Readers do not block the writer and vice versa. Without it, the API reading
    the queue would periodically lock out the playout engine recording a play
    event, and ``database is locked`` would surface as a dropped track.

``busy_timeout``
    On contention, wait rather than fail immediately. Configurable, defaulting to
    10 s, which is far longer than any write this station performs.

``synchronous=NORMAL``
    With WAL this is durable against application crashes (the case that matters)
    while avoiding an fsync per commit. ``FULL`` would protect against OS crashes
    too, at a cost paid thousands of times an hour by the market snapshot writer.

``foreign_keys=ON``
    SQLite disables FK enforcement by default — per connection. Our cascades
    (``ondelete="CASCADE"``) are silently ignored without this, so a deleted track
    would leave orphaned blueprints and fingerprints behind forever.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tradefix_radio.config.schema import DatabaseSettings
from tradefix_radio.core.clock import Clock, SystemClock
from tradefix_radio.core.errors import PersistenceError
from tradefix_radio.persistence.models import Base

_log = structlog.get_logger(__name__)


#: Async driver prefix -> sync equivalent. Alembic runs migrations synchronously
#: while the application uses async drivers; translating one DSN avoids keeping two
#: that can disagree about which database is the real one.
ASYNC_TO_SYNC_DRIVERS = {
    "sqlite+aiosqlite": "sqlite",
    "postgresql+asyncpg": "postgresql+psycopg2",
}

#: Directory holding the Alembic migration environment.
#:
#: Derived from the package rather than from ``paths.root_dir``: the data root is
#: relocatable per deployment, while migrations ship *with the code*. Using the
#: data root would break ``tradefix migrate`` for any operator who points
#: TRADEFIX_ROOT at a separate data volume.
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def sync_database_url(url: str) -> str:
    """Rewrite an async DSN to its synchronous equivalent."""
    for async_prefix, sync_prefix in ASYNC_TO_SYNC_DRIVERS.items():
        if url.startswith(async_prefix + ":"):
            return sync_prefix + url[len(async_prefix) :]
    return url


def _sqlite_path(url: str) -> Path | None:
    """Extract the filesystem path from a SQLite URL, or ``None`` for in-memory."""
    marker = ":///"
    index = url.find(marker)
    if index == -1:
        return None
    raw = url[index + len(marker) :]
    if not raw or raw == ":memory:":
        return None
    return Path(raw)


def _register_sqlite_pragmas(engine: AsyncEngine, settings: DatabaseSettings) -> None:
    """Attach a connect hook applying the pragmas to every new connection.

    Per-connection, not once at startup: SQLite pragmas like ``foreign_keys`` are
    connection-scoped, and the pool opens new connections over the life of the
    process.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
            # Keep the temp store in memory: the market feature engine issues
            # window queries that would otherwise spill to disk every bar.
            cursor.execute("PRAGMA temp_store=MEMORY")
        finally:
            cursor.close()


class Database:
    """Owns the engine and session factory for one process.

    Constructed once per process and passed explicitly. There is no global engine:
    with three processes and a test suite that creates throwaway databases, a
    module-level singleton would be the first thing to break.
    """

    def __init__(self, settings: DatabaseSettings, *, clock: Clock | None = None) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._clock: Clock = clock or SystemClock()

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        """Create the engine and verify the database is actually reachable.

        Verification is deliberate: ``create_async_engine`` is lazy, so without a
        probe a misconfigured DSN would first fail deep inside the scheduler.
        §73 requires discovering this at startup.
        """
        if self._engine is not None:
            return

        settings = self._settings
        kwargs: dict[str, Any] = {"echo": settings.echo, "future": True}

        if settings.is_sqlite:
            path = _sqlite_path(settings.url)
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
            # SQLite ignores pool sizing; aiosqlite serialises per connection.
        else:
            kwargs["pool_size"] = settings.pool_size
            kwargs["max_overflow"] = settings.max_overflow
            kwargs["pool_pre_ping"] = True

        engine = create_async_engine(settings.url, **kwargs)
        if settings.is_sqlite:
            _register_sqlite_pragmas(engine, settings)

        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            await engine.dispose()
            raise PersistenceError(
                f"cannot connect to the database: {exc.__class__.__name__}",
                url=_redact_url(settings.url),
                detail=str(exc),
            ) from exc

        self._engine = engine
        self._session_factory = async_sessionmaker(
            engine,
            expire_on_commit=False,
            autoflush=False,
            class_=AsyncSession,
        )
        _log.info(
            "database.connected",
            dialect="sqlite" if settings.is_sqlite else "postgresql",
            url=_redact_url(settings.url),
        )

    async def disconnect(self) -> None:
        """Dispose the pool. Part of §74 graceful shutdown."""
        if self._engine is None:
            return
        engine, self._engine = self._engine, None
        self._session_factory = None
        await engine.dispose()
        _log.info("database.disconnected")

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise PersistenceError("database is not connected; call connect() first")
        return self._engine

    @property
    def is_connected(self) -> bool:
        return self._engine is not None

    # -- sessions ----------------------------------------------------------

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session that is committed on success and rolled back on failure.

        The default unit of work. Callers should not commit inside the block — on
        an exception the whole block is rolled back, which is the behaviour that
        keeps a half-written track out of the database.
        """
        if self._session_factory is None:
            raise PersistenceError("database is not connected; call connect() first")
        session = self._session_factory()
        # Held on the injected clock for the life of the unit of work.
        #
        # A no-op under :class:`~tradefix_radio.core.clock.SystemClock`, which is what
        # production uses. Under a virtual clock it is what stops simulated time running past
        # a database round trip: aiosqlite hands every statement to a worker thread, so the
        # calling task is *runnable* rather than sleeping and holds no clock waiter. An
        # accelerated run would then advance to some other task's deadline while this query
        # was still in flight — and it did: with the clock free to jump, a generation claim
        # that takes three real milliseconds consumed hundreds of virtual seconds, every job
        # hit its deadline, and a fifteen-minute gate run aired nothing but Tier 3 cover.
        #
        # Safe as a hold because a unit of work is a *leaf*: nothing inside it sleeps on the
        # clock or waits on another task that needs time to advance.
        with self._clock.hold():
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
            finally:
                await session.close()

    @contextlib.asynccontextmanager
    async def read_session(self) -> AsyncIterator[AsyncSession]:
        """A read-only session that never commits.

        Used by the API's GET routes and by the dashboard WebSocket. Separate from
        :meth:`session` so a read path cannot accidentally persist state — a real
        risk when a query triggers lazy loading on a mutated instance.

        ``expunge_all`` before the rollback is deliberate and load-bearing: a plain
        ``rollback`` *expires* every loaded instance, so any ORM object returned
        from the block would raise ``DetachedInstanceError`` on first attribute
        access. Expunging first detaches the instances while they still hold their
        loaded values, so callers get usable read-only snapshots. Without this,
        every read path would have to consume its results inside the ``async with``
        — a footgun that fails at runtime, far from the cause.
        """
        if self._session_factory is None:
            raise PersistenceError("database is not connected; call connect() first")
        session = self._session_factory()
        try:
            with self._clock.hold():
                yield session
        finally:
            session.expunge_all()
            await session.rollback()
            await session.close()

    # -- schema ------------------------------------------------------------

    async def create_all(self) -> None:
        """Create the schema directly from the models.

        **Tests and throwaway databases only.** §37 requires migrations for real
        deployments, and ``tradefix migrate`` is the supported path. This exists
        because running Alembic for every one of hundreds of unit tests would
        dominate the suite's runtime.
        """
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        """Drop every table. Tests only."""
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)

    async def table_names(self) -> list[str]:
        """Tables present in the live database, for the §73 schema check."""

        def _inspect(sync_connection: Any) -> list[str]:
            from sqlalchemy import inspect  # noqa: PLC0415 - sync-only helper

            return list(inspect(sync_connection).get_table_names())

        async with self.engine.connect() as connection:
            names: list[str] = await connection.run_sync(_inspect)
        return names

    async def ping(self) -> float:
        """Round-trip a trivial query, returning elapsed milliseconds."""
        import time  # noqa: PLC0415 - local to keep the module import light

        started = time.perf_counter()
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return (time.perf_counter() - started) * 1000


def _redact_url(url: str) -> str:
    """Strip credentials from a DSN before it reaches a log line (§68).

    PostgreSQL DSNs embed the password. Logging one verbatim would put it in a
    rotating file that lives for weeks.
    """
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    credentials, _, host = rest.rpartition("@")
    if not credentials:
        return url
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


def apply_sqlite_pragmas_sync(engine: Engine, settings: DatabaseSettings) -> None:
    """Pragma hook for the synchronous engine Alembic uses.

    Alembic runs migrations on a sync connection; without ``foreign_keys=ON`` there
    too, a migration that rebuilds a table would drop its FK behaviour silently.
    """

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
        finally:
            cursor.close()


__all__ = [
    "ASYNC_TO_SYNC_DRIVERS",
    "MIGRATIONS_DIR",
    "Database",
    "apply_sqlite_pragmas_sync",
    "sync_database_url",
]
