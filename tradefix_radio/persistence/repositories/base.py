"""Repository base.

Repositories take an :class:`AsyncSession` and never own one. The caller controls
the transaction boundary, because a single logical operation ("generation
finished: write the track, its blueprint, its fingerprint, its file row, and the
state transition") spans several repositories and must commit atomically. A
repository that opened its own session would make that impossible and would leave
half-written tracks behind on failure.

Consequently no repository method commits. They flush when they need a generated
primary key, and nothing more.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import CursorResult
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncSession


class Repository:
    """Common base: holds the session, nothing else."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session


def affected_rows(result: Result[Any]) -> int:
    """Rows affected by an UPDATE or DELETE.

    ``Session.execute`` is typed as returning :class:`Result`, which has no
    ``rowcount``; for DML statements it actually returns a
    :class:`CursorResult`, which does. This narrows the type in one place instead
    of scattering ``type: ignore`` across every prune and update method, and
    returns 0 rather than ``-1`` when the driver cannot report a count, so callers
    can treat the value as a plain non-negative tally.
    """
    if isinstance(result, CursorResult):
        count = result.rowcount
        return int(count) if count is not None and count >= 0 else 0
    return 0


__all__ = ["Repository", "affected_rows"]
