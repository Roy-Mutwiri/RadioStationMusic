"""Track repository.

The methods here are shaped by the queries the station actually runs, not by CRUD
symmetry. In particular :meth:`TrackRepository.recent_history` exists because the
§11/§12 diversity checks need the last N tracks *in air order with their creative
attributes* and nothing else — fetching full ORM objects with relationships for a
100-track horizon on every scheduling decision would be the slowest thing in the
director.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy import func, select, update

from tradefix_radio.contracts.enums import TrackProvenance
from tradefix_radio.contracts.music import MusicBlueprintV1
from tradefix_radio.core.errors import PersistenceError
from tradefix_radio.core.state_machine import (
    TrackState,
    assert_transition,
)
from tradefix_radio.persistence.models import (
    StateTransitionRow,
    Track,
    TrackBlueprint,
    TrackTitle,
)
from tradefix_radio.persistence.repositories.base import Repository

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class TrackHistoryEntry:
    """A lightweight projection for diversity checks (§11, §12).

    Deliberately a flat value object, not an ORM row: the director reads hundreds
    of these per decision and must not hold a session open or trigger lazy loads
    while doing so.
    """

    track_id: str
    genre: str
    secondary_genre: str | None
    bpm: int
    musical_key: str
    duration_seconds: float
    is_instrumental: bool
    vocal_style: str
    primary_topic: str | None
    secondary_topic: str | None
    persona_id: str | None
    blueprint_signature: str
    energy_at_generation: float
    regime_at_generation: str
    played_at: datetime | None
    created_at: datetime


class TrackRepository(Repository):
    """Reads and writes tracks, blueprints, titles and state transitions."""

    # -- creation ----------------------------------------------------------

    async def promote_to_production(self, track_id: str, *, now: datetime) -> bool:
        """Mark a track as PRODUCTION_RADIO because it has begun airing for real.

        This is the only way a track enters production provenance, and the trigger is
        deliberately narrow: **playout actually starting, outside simulation and test
        mode**. Not generated, not approved, not queued, not previewed.

        The looser triggers were all tempting and all wrong. A station generates far more
        candidates than it airs, and counting an approved-but-never-played track as
        production history means the novelty library fills with music nobody heard — which
        is precisely the defect that made 154 bench tracks age real output.

        Returns whether the row changed, so a caller can log a promotion once rather than
        on every repeat play.
        """
        row = await self._session.get(Track, track_id)
        if row is None or row.provenance == TrackProvenance.PRODUCTION_RADIO.value:
            return False
        previous = row.provenance
        row.provenance = TrackProvenance.PRODUCTION_RADIO.value
        row.updated_at = now
        _log.info(
            "track.promoted_to_production",
            track_id=track_id,
            previous=previous,
            detail="began airing outside simulation/test mode",
        )
        return True

    async def create(
        self,
        blueprint: MusicBlueprintV1,
        *,
        now: datetime,
        provider: str,
        model_identifier: str,
        state: TrackState = TrackState.PLANNED,
        provenance: str = "production_radio",
    ) -> Track:
        """Insert a track plus its complete blueprint (§8).

        Both rows are written together because §8 requires that every track have
        its blueprint saved — allowing a track to exist without one would make
        that an aspiration rather than an invariant.
        """
        composition = blueprint.composition
        track = Track(
            track_id=blueprint.track_id,
            state=state.value,
            title=blueprint.title,
            persona_id=blueprint.persona_id,
            genre=composition.genre,
            secondary_genre=composition.secondary_genre,
            bpm=composition.bpm,
            musical_key=composition.key,
            duration_seconds=float(composition.duration_seconds),
            is_instrumental=blueprint.is_instrumental,
            vocal_style=blueprint.vocal.style.value,
            primary_topic=blueprint.lyrics.primary_topic,
            secondary_topic=blueprint.lyrics.secondary_topic,
            provenance=provenance,
            symbol_at_generation=blueprint.market.symbol,
            regime_at_generation=blueprint.market.regime.value,
            energy_at_generation=blueprint.market.energy,
            composition_energy=blueprint.composition.energy,
            session_at_generation=blueprint.market.session,
            seed=blueprint.seed,
            provider=provider,
            model_identifier=model_identifier,
            blueprint_signature=blueprint.signature(),
            created_at=now,
            updated_at=now,
        )
        self._session.add(track)
        self._session.add(
            TrackBlueprint(
                track_id=blueprint.track_id,
                schema_version=MusicBlueprintV1.schema_version,
                payload=blueprint.to_json_dict(),
                created_at=now,
            )
        )
        self._session.add(
            TrackTitle(
                title=blueprint.title,
                normalised=normalise_title(blueprint.title),
                track_id=blueprint.track_id,
                created_at=now,
            )
        )
        self._session.add(
            StateTransitionRow(
                track_id=blueprint.track_id,
                from_state=None,
                to_state=state.value,
                reason="created",
                at=now,
            )
        )
        await self._session.flush()
        return track

    # -- lookup ------------------------------------------------------------

    async def get(self, track_id: str) -> Track | None:
        return await self._session.get(Track, track_id)

    async def require(self, track_id: str) -> Track:
        track = await self.get(track_id)
        if track is None:
            raise PersistenceError("track not found", track_id=track_id)
        return track

    async def get_blueprint(self, track_id: str) -> MusicBlueprintV1 | None:
        """Rehydrate the stored blueprint into its contract type.

        Validated on read rather than trusted: a row written by an older build
        might not satisfy the current model, and discovering that here — with the
        track id in hand — is far better than a confusing failure later.
        """
        row = await self._session.get(TrackBlueprint, track_id)
        if row is None:
            return None
        return MusicBlueprintV1.model_validate(row.payload)

    async def count_by_state(self) -> dict[str, int]:
        result = await self._session.execute(
            select(Track.state, func.count()).group_by(Track.state)
        )
        return dict(result.all())  # type: ignore[arg-type]

    async def next_daily_sequence(self, when: datetime) -> int:
        """1-based count of tracks created on ``when``'s UTC date, plus one.

        Used to build the human-readable track id. Derived from the table rather
        than a counter so a restart cannot reuse an id — §75 requires recovery not
        to corrupt identity, and a lost in-memory counter would do exactly that.
        """
        day_start = when.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        result = await self._session.execute(
            select(func.count())
            .select_from(Track)
            .where(Track.created_at >= day_start, Track.created_at < day_end)
        )
        by_count = int(result.scalar_one() or 0) + 1
        # The id itself is the authority. A count by ``created_at`` and an id built from
        # ``when``'s date disagree whenever the two clocks straddle midnight or a stored
        # timestamp is not in the zone the query assumes, and then the count came back
        # *lower* than the ids already issued: a restart reissued TF-...-00186 to a new
        # plan, which collided with the aired track of that id — the regenerated copy kept
        # the old one's lyrics row, ran with the new blueprint, and replayed every restart.
        # Continuing from the highest numeric suffix already used for the date cannot
        # reissue an id, whatever the timestamps say.
        prefix = f"TF-{when:%Y%m%d}-"
        ids = await self._session.execute(
            select(Track.track_id).where(Track.track_id.like(f"{prefix}%"))
        )
        highest = 0
        for (track_id,) in ids:
            suffix = str(track_id)[len(prefix):]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        return max(by_count, highest + 1)

    # -- state transitions -------------------------------------------------

    async def transition(
        self,
        track_id: str,
        to_state: TrackState,
        *,
        now: datetime,
        reason: str,
        **detail: object,
    ) -> Track:
        """Move a track to a new state, validating and recording the change.

        The legality check runs against the *persisted* state, not a caller-held
        value, so two processes cannot both believe they are moving the same track
        out of READY. §92 requires illegal transitions to raise; this is where that
        happens for real rather than only in memory.
        """
        track = await self.require(track_id)
        current = TrackState(track.state)
        assert_transition(current, to_state)

        track.state = to_state.value
        track.updated_at = now
        if to_state is TrackState.PLAYING:
            track.last_played_at = now
        if to_state in {TrackState.REJECTED, TrackState.QUARANTINED}:
            text = str(detail.get("message") or reason)
            track.rejection_reason = text[:400]

        self._session.add(
            StateTransitionRow(
                track_id=track_id,
                from_state=current.value,
                to_state=to_state.value,
                reason=reason,
                detail=dict(detail) or None,
                at=now,
            )
        )
        await self._session.flush()
        return track

    async def transitions(self, track_id: str) -> list[StateTransitionRow]:
        result = await self._session.execute(
            select(StateTransitionRow)
            .where(StateTransitionRow.track_id == track_id)
            .order_by(StateTransitionRow.at, StateTransitionRow.id)
        )
        return list(result.scalars())

    async def mark_played(
        self, track_id: str, *, now: datetime, completed: bool, reason: str
    ) -> Track:
        """Finish a playing track.

        §75 forbids marking an incomplete track as successfully played, so an
        interrupted airing goes to FAILED and its play count is not incremented.
        The distinction is enforced here, at the single place it is decided.
        """
        if completed:
            track = await self.transition(
                track_id, TrackState.PLAYED, now=now, reason=reason
            )
            track.play_count += 1
            track.last_played_at = now
            await self._session.flush()
            return track
        return await self.transition(
            track_id, TrackState.FAILED, now=now, reason=reason, incomplete_airing=True
        )

    # -- diversity history -------------------------------------------------

    async def recent_history(self, limit: int) -> list[TrackHistoryEntry]:
        """The last ``limit`` aired tracks, most recent first (§12).

        Ordered by air time, not creation time: the diversity rules are about what
        a *listener* heard in sequence, and generation order can differ from air
        order after any replan.
        """
        if limit < 1:
            return []
        result = await self._session.execute(
            select(Track)
            .where(Track.last_played_at.is_not(None))
            .order_by(Track.last_played_at.desc())
            .limit(limit)
        )
        return [_to_history(track) for track in result.scalars()]

    async def history_since(self, since: datetime) -> list[TrackHistoryEntry]:
        """Tracks aired since ``since`` (§12's 24-hour and 7-day horizons)."""
        result = await self._session.execute(
            select(Track)
            .where(Track.last_played_at.is_not(None), Track.last_played_at >= since)
            .order_by(Track.last_played_at.desc())
        )
        return [_to_history(track) for track in result.scalars()]

    async def blueprint_signature_exists(self, signature: str) -> bool:
        """§11: "same blueprint: never repeat"."""
        result = await self._session.execute(
            select(Track.track_id).where(Track.blueprint_signature == signature).limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def genre_distribution(self, limit: int) -> dict[str, int]:
        """Genre counts over the most recent ``limit`` aired tracks (§56, §11)."""
        subquery = (
            select(Track.genre)
            .where(Track.last_played_at.is_not(None))
            .order_by(Track.last_played_at.desc())
            .limit(limit)
            .subquery()
        )
        result = await self._session.execute(
            select(subquery.c.genre, func.count()).group_by(subquery.c.genre)
        )
        return dict(result.all())  # type: ignore[arg-type]

    # -- titles ------------------------------------------------------------

    async def title_exists(self, title: str) -> bool:
        """Exact (normalised) title collision check (§99)."""
        result = await self._session.execute(
            select(TrackTitle.id).where(TrackTitle.normalised == normalise_title(title)).limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def recent_titles(self, limit: int = 500) -> list[str]:
        """Recent normalised titles, for §99's similarity rejection."""
        result = await self._session.execute(
            select(TrackTitle.normalised)
            .order_by(TrackTitle.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars())

    async def reserve_title(self, title: str, *, now: datetime, track_id: str | None) -> None:
        """Record a title as used even if its track is later rejected (§99).

        A rejected candidate still consumed the title idea; letting it return would
        make the station repeat itself in the one field listeners read.
        """
        self._session.add(
            TrackTitle(
                title=title,
                normalised=normalise_title(title),
                track_id=track_id,
                created_at=now,
            )
        )
        await self._session.flush()

    # -- startup reserve (§FSP) --------------------------------------------

    async def find_unplayed_ready(
        self,
        *,
        market_symbol: str | None = None,
        limit: int = 10,
    ) -> list[Track]:
        """Find ready tracks that have never been played (§FSP reserve).

        These are real ACE-Step tracks that passed Phase 6 but have never aired.
        They form the instant-start reserve for fresh startup programming.

        If ``market_symbol`` is provided, only tracks generated for that market
        (or neutral tracks) are returned. This ensures BTC tracks don't play
        during a gold session.
        """
        # READY or QUEUED tracks that have never played
        statement = (
            select(Track)
            .where(
                Track.state.in_([TrackState.READY.value, TrackState.QUEUED.value]),
                Track.play_count == 0,
            )
            .order_by(Track.created_at.desc())
            .limit(limit)
        )
        if market_symbol:
            # Match the exact market or neutral (though currently no tracks are neutral)
            statement = statement.where(
                Track.symbol_at_generation.in_([market_symbol, "NEUTRAL"])
            )
        result = await self._session.execute(statement)
        return list(result.scalars())

    async def count_unplayed_ready_by_market(self) -> dict[str, int]:
        """Count unplayed ready tracks grouped by market symbol.

        For the Control Center startup panel: shows reserve depth per market.
        """
        statement = (
            select(Track.symbol_at_generation, func.count())
            .where(
                Track.state.in_([TrackState.READY.value, TrackState.QUEUED.value]),
                Track.play_count == 0,
            )
            .group_by(Track.symbol_at_generation)
        )
        result = await self._session.execute(statement)
        return dict(result.all())  # type: ignore[arg-type]

    # -- maintenance -------------------------------------------------------

    async def reset_stuck_active_tracks(
        self, *, now: datetime, states: list[TrackState], reason: str
    ) -> int:
        """Fail tracks left mid-work by a crash (§75).

        Called at startup. A track in GENERATING when the process died is not
        recoverable — the provider's output is gone or unverifiable — so it is
        failed explicitly rather than left to occupy a queue slot forever.
        """
        if not states:
            return 0
        values = [state.value for state in states]
        result = await self._session.execute(
            select(Track.track_id).where(Track.state.in_(values))
        )
        track_ids = list(result.scalars())
        if not track_ids:
            return 0
        await self._session.execute(
            update(Track)
            .where(Track.track_id.in_(track_ids))
            .values(state=TrackState.FAILED.value, updated_at=now)
        )
        for track_id in track_ids:
            self._session.add(
                StateTransitionRow(
                    track_id=track_id,
                    from_state=None,
                    to_state=TrackState.FAILED.value,
                    reason=reason,
                    detail={"recovered_at_startup": True},
                    at=now,
                )
            )
        await self._session.flush()
        return len(track_ids)


def normalise_title(title: str) -> str:
    """Comparison form for titles: lowercase, alphanumeric words only.

    "Liquidity After Midnight" and "liquidity, after midnight!" are the same title
    for uniqueness purposes, and treating them as different is how a station ends
    up with near-duplicate names in its library.
    """
    words = "".join(char if char.isalnum() else " " for char in title.lower()).split()
    return " ".join(words)


def _to_history(track: Track) -> TrackHistoryEntry:
    return TrackHistoryEntry(
        track_id=track.track_id,
        genre=track.genre,
        secondary_genre=track.secondary_genre,
        bpm=track.bpm,
        musical_key=track.musical_key,
        duration_seconds=track.duration_seconds,
        is_instrumental=track.is_instrumental,
        vocal_style=track.vocal_style,
        primary_topic=track.primary_topic,
        secondary_topic=track.secondary_topic,
        persona_id=track.persona_id,
        blueprint_signature=track.blueprint_signature,
        energy_at_generation=track.energy_at_generation,
        regime_at_generation=track.regime_at_generation,
        played_at=track.last_played_at,
        created_at=track.created_at,
    )


__all__ = ["TrackHistoryEntry", "TrackRepository", "normalise_title"]
