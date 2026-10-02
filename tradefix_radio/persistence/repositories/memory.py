"""Radio memory repository (§96).

§96: "Restarting the application should NOT reset creative memory and cause
immediate repeats." This is the store behind that. It holds the director's
rotation state — last time each genre/topic/persona/station-ID was used, running
distributions, counters — as JSON under stable keys.

Why key/value rather than typed tables: the set of remembered things grows every
time the director gains a dimension, and each addition would otherwise be a
migration. The keys are namespaced constants declared here so they cannot drift
into typos, which is the real failure mode of a key/value store.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from sqlalchemy import select

from tradefix_radio.persistence.models import RadioMemory
from tradefix_radio.persistence.repositories.base import Repository
from tradefix_radio.persistence.upsert import upsert_statement


class MemoryKeys:
    """Canonical memory keys. Referenced rather than spelled out at call sites."""

    LAST_GENRE_USE: Final = "director.last_use.genre"
    LAST_TOPIC_USE: Final = "director.last_use.topic"
    LAST_PERSONA_USE: Final = "director.last_use.persona"
    LAST_KEY_USE: Final = "director.last_use.musical_key"
    LAST_STATION_ID_USE: Final = "radio.last_use.station_id"

    BPM_HISTOGRAM: Final = "director.histogram.bpm"
    ENERGY_HISTOGRAM: Final = "director.histogram.energy"
    GENRE_HISTOGRAM: Final = "director.histogram.genre"
    TOPIC_PAIR_HISTORY: Final = "director.history.topic_pairs"

    DIVERSITY_SCORE: Final = "director.diversity_score"
    CREATIVE_TEMPERATURE: Final = "director.creative_temperature"
    RADIO_ENERGY: Final = "radio.energy_level"

    TRACK_SEQUENCE: Final = "radio.track_sequence"
    DAILY_TRACK_COUNTER: Final = "radio.daily_track_counter"
    TRACKS_SINCE_STATION_ID: Final = "radio.tracks_since_station_id"
    TRACKS_SINCE_DJ: Final = "radio.tracks_since_dj"

    LAST_SHUTDOWN: Final = "station.last_shutdown"
    LAST_KNOWN_MARKET_STATE: Final = "market.last_known_state"


class RadioMemoryRepository(Repository):
    """Persistent key/value store for creative and rotation state."""

    async def get(self, key: str, default: Any = None) -> Any:
        row = await self._session.get(RadioMemory, key)
        return default if row is None else row.value

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        """Fetch several keys in one round trip.

        Used at director startup, which needs a dozen keys at once; issuing a
        dozen queries there is the difference between an instant and a visible
        pause on every restart.
        """
        if not keys:
            return {}
        result = await self._session.execute(
            select(RadioMemory).where(RadioMemory.key.in_(keys))
        )
        return {row.key: row.value for row in result.scalars()}

    async def set(self, key: str, value: Any, *, now: datetime) -> None:
        """Upsert a single key.

        One statement, not SELECT-then-write: the worker and playout processes both
        touch rotation counters, and the read-modify-write version races between
        them. :func:`~tradefix_radio.persistence.upsert.upsert_statement` picks the
        right dialect construct so this works on SQLite and PostgreSQL alike.
        """
        statement = upsert_statement(
            self._session,
            RadioMemory,
            {"key": key, "value": value, "updated_at": now},
            index_elements=[RadioMemory.key],
            update_columns=["value", "updated_at"],
        )
        await self._session.execute(statement)

    async def set_many(self, values: dict[str, Any], *, now: datetime) -> None:
        for key, value in values.items():
            await self.set(key, value, now=now)

    async def increment(self, key: str, *, now: datetime, amount: int = 1) -> int:
        """Read-modify-write an integer counter, returning the new value.

        Not atomic across processes. Acceptable because every counter stored this
        way (tracks since last station ID, daily sequence) is written by exactly
        one process — the playout engine — and a lost increment would only shift a
        station ID by one track. Counters that must be exact use a database
        sequence instead.
        """
        current = await self.get(key, 0)
        if not isinstance(current, int):
            current = 0
        updated = current + amount
        await self.set(key, updated, now=now)
        return updated

    async def delete(self, key: str) -> bool:
        row = await self._session.get(RadioMemory, key)
        if row is None:
            return False
        await self._session.delete(row)
        # Flush so the deletion is visible to later reads in this same session.
        # Without it, `get` would still return the object from the identity map
        # and a second delete would report success twice.
        await self._session.flush()
        return True

    async def all_keys(self) -> list[str]:
        result = await self._session.execute(select(RadioMemory.key).order_by(RadioMemory.key))
        return list(result.scalars())


__all__ = ["MemoryKeys", "RadioMemoryRepository"]
