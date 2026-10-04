"""What was actually sent to the generation provider (§7.9, §7.10, §7.26).

Separate from the track repository because it answers a different question. ``tracks`` says
what the station decided; this says what the model was told. They are supposed to agree, and
the only way to notice when they stop agreeing is to keep both.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from tradefix_radio.persistence.models import ProviderSubmission
from tradefix_radio.persistence.repositories.base import Repository

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

_log = structlog.get_logger(__name__)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _strings(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value]


class ProviderSubmissionsRepository(Repository):
    """Records one provider submission per generation attempt."""

    async def record(
        self,
        *,
        track_id: str,
        provider: str,
        model_identifier: str,
        detail: dict[str, Any],
        attempt: int,
        now: datetime,
    ) -> ProviderSubmission | None:
        """Store the submission described by a generation result's ``detail`` block.

        Returns ``None`` rather than raising when the provider did not supply a prompt
        record. Not every provider is ACE-Step — the mock provider used by the Phase 4
        tests has no caption at all — and a missing diagnostic must not be able to fail a
        generation that otherwise succeeded.
        """
        prompt = detail.get("prompt")
        if not isinstance(prompt, dict):
            return None

        caption = _text(prompt.get("caption"))
        provider_lyrics = _text(prompt.get("lyrics"))
        if not caption:
            return None

        requested = detail.get("requested_lyrics")
        requested_lyrics = requested if isinstance(requested, str) else None

        # The spec's own flag covers the edits the prompt builder makes to the text it was
        # given. It cannot cover the case where it was given nothing — the downgrade from a
        # validated lyric to `[Instrumental]` — so that is derived here from the two texts
        # rather than trusted from one of them.
        modified = bool(prompt.get("lyrics_modified")) or (
            (requested_lyrics or "").strip() != provider_lyrics.strip()
            and requested_lyrics is not None
        )

        row = ProviderSubmission(
            track_id=track_id,
            attempt=attempt,
            provider=provider,
            model_identifier=model_identifier,
            caption=caption,
            requested_lyrics=requested_lyrics,
            provider_lyrics=provider_lyrics,
            lyrics_modified=modified,
            lyric_notes=_strings(prompt.get("lyric_notes")),
            instrumental=bool(prompt.get("instrumental")),
            seed=int(prompt.get("seed") or 0),
            inference_steps=int(prompt.get("inference_steps") or 0),
            guidance_scale=float(prompt.get("guidance_scale") or 0.0),
            profile=str(prompt.get("profile") or "unknown")[:32],
            duration_seconds=float(prompt.get("duration_seconds") or 0.0),
            bpm=int(prompt.get("bpm") or 0),
            key_scale=str(prompt.get("key_scale") or "")[:48],
            warnings=_strings(prompt.get("warnings")),
            created_at=now,
        )
        self._session.add(row)
        await self._session.flush()

        if modified:
            # Loud on purpose. A vocal track that reached the model as an instrumental is
            # the exact failure B3 exists to make impossible, and it is otherwise silent.
            _log.warning(
                "provider.lyrics_modified",
                track_id=track_id,
                attempt=attempt,
                instrumental=row.instrumental,
                requested_words=len((requested_lyrics or "").split()),
                provider_words=len(provider_lyrics.split()),
                notes=row.lyric_notes[:4],
            )
        return row

    async def for_track(self, track_id: str) -> list[ProviderSubmission]:
        """Every submission for a track, oldest attempt first."""
        result = await self._session.execute(
            select(ProviderSubmission)
            .where(ProviderSubmission.track_id == track_id)
            .order_by(ProviderSubmission.attempt, ProviderSubmission.id)
        )
        return list(result.scalars())

    async def latest_for_track(self, track_id: str) -> ProviderSubmission | None:
        """The last submission for a track — what produced the audio on disk."""
        result = await self._session.execute(
            select(ProviderSubmission)
            .where(ProviderSubmission.track_id == track_id)
            .order_by(ProviderSubmission.id.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def downgraded(self, *, limit: int = 50) -> list[ProviderSubmission]:
        """Submissions where a vocal was asked for and an instrumental was sent.

        The query the Originality and track pages use to show that the station noticed,
        instead of leaving an unexplained instrumental in a rap slot.
        """
        result = await self._session.execute(
            select(ProviderSubmission)
            .where(
                ProviderSubmission.lyrics_modified.is_(True),
                ProviderSubmission.instrumental.is_(True),
            )
            .order_by(ProviderSubmission.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars())


__all__ = ["ProviderSubmissionsRepository"]
