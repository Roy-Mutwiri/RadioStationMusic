"""Lyric storage (§17).

Split from the track repository because the two have different lifetimes. ADR-07 makes
audio expendable and metadata permanent, and lyrics are firmly on the permanent side: they
are the corpus §17's duplicate detection compares against, so deleting them would make the
station able to repeat itself the moment a file was swept.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from tradefix_radio.persistence.models import Lyrics
from tradefix_radio.persistence.repositories.base import Repository

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

    from tradefix_radio.contracts.lyrics import LyricsV1

_log = structlog.get_logger(__name__)

#: How many recent lyrics the duplicate check compares against.
#:
#: 200. Far enough back that a listener could not have forgotten, bounded because the
#: comparison runs inside the generation deadline and an unbounded read would grow into it.
DEFAULT_HISTORY_LIMIT = 200


class LyricsRepository(Repository):
    """Stores composed lyrics and the history the originality check reads."""

    async def create(self, lyrics: LyricsV1, *, now: datetime) -> Lyrics:
        """Persist one lyric, replacing any previous attempt for the same track.

        Upserted rather than inserted because a track whose first lyric was rejected and
        recomposed must not leave the rejected words behind as history — they were never
        sung, and counting them would make the station avoid phrasing nobody heard.
        """
        existing = await self._session.get(Lyrics, lyrics.track_id)
        if existing is not None:
            await self._session.delete(existing)
            await self._session.flush()

        row = Lyrics(
            track_id=lyrics.track_id,
            text=lyrics.text,
            content_hash=lyrics.content_hash,
            primary_topic=lyrics.primary_topic,
            secondary_topic=lyrics.secondary_topic,
            lyric_format=lyrics.format,
            perspective=lyrics.perspective,
            tradefix_mentions=lyrics.tradefix_mentions,
            educational_intensity=lyrics.educational_intensity,
            word_count=len(lyrics.text.split()),
            # `shingles()` is a method taking a window size, not a property. Stored at
            # the default window so every later comparison shingles the same way — two
            # window sizes would make Jaccard scores quietly incomparable.
            shingles=sorted(lyrics.shingles()),
            concepts_used=list(lyrics.concepts_used),
            created_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        _log.info(
            "lyrics.stored",
            track_id=lyrics.track_id,
            words=row.word_count,
            format=lyrics.format,
            topic=lyrics.primary_topic,
            mentions=lyrics.tradefix_mentions,
        )
        return row

    async def get(self, track_id: str) -> Lyrics | None:
        return await self._session.get(Lyrics, track_id)

    async def recent(self, *, limit: int = DEFAULT_HISTORY_LIMIT) -> list[Lyrics]:
        """The most recent lyrics, newest first."""
        result = await self._session.execute(
            select(Lyrics).order_by(Lyrics.created_at.desc()).limit(limit)
        )
        return list(result.scalars())

    async def recent_hashes(self, *, limit: int = DEFAULT_HISTORY_LIMIT) -> frozenset[str]:
        """Content hashes for exact-duplicate detection.

        A separate query from :meth:`recent` because the exact check wants only hashes and
        loading the text of two hundred lyrics to compare sixty-four characters each would
        put a few megabytes on the generation path for nothing.
        """
        result = await self._session.execute(
            select(Lyrics.content_hash)
            .order_by(Lyrics.created_at.desc())
            .limit(limit)
        )
        return frozenset(row for row in result.scalars() if row)

    async def recent_shingles(
        self, *, limit: int = DEFAULT_HISTORY_LIMIT
    ) -> list[tuple[str, frozenset[str]]]:
        """Shingle sets for near-duplicate detection, newest first."""
        result = await self._session.execute(
            select(Lyrics.track_id, Lyrics.shingles)
            .order_by(Lyrics.created_at.desc())
            .limit(limit)
        )
        return [
            (track_id, frozenset(shingles or ()))
            for track_id, shingles in result.all()
        ]


__all__ = ["DEFAULT_HISTORY_LIMIT", "LyricsRepository"]
