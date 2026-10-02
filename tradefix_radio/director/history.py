"""Programming history and multi-horizon views (§12).

§12 asks for repetition checks at six different horizons — last 5, last 20, last 100,
last 24 hours, last 7 days, all time — and notes that "different anti-repetition checks
should operate at different horizons". This module is the windowed view that makes those
queries cheap and uniform.

The history is read through a :class:`ProgrammedTrack` protocol rather than a concrete
class. That keeps the director decoupled from persistence: the real implementation is
``TrackHistoryEntry`` from the repository layer, while tests construct trivial fakes.
A director that could only be tested with a database would not get tested thoroughly.

**Air order, not creation order.** Everything here is ordered by when a listener heard
it. After any §28 replan those two orders differ, and §11 is about what the listener
experienced in sequence.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class ProgrammedTrack(Protocol):
    """The attributes the diversity rules need from a previously aired track.

    Structurally satisfied by
    :class:`~tradefix_radio.persistence.repositories.tracks.TrackHistoryEntry` and by
    :class:`HistoryEntry`.

    Declared as **read-only properties** rather than plain annotations. A bare
    ``track_id: str`` in a Protocol means a *mutable* attribute, which a frozen dataclass
    cannot provide — so the obvious spelling would have rejected exactly the immutable
    value objects this protocol exists to accept.
    """

    @property
    def track_id(self) -> str: ...
    @property
    def genre(self) -> str: ...
    @property
    def secondary_genre(self) -> str | None: ...
    @property
    def bpm(self) -> int: ...
    @property
    def musical_key(self) -> str: ...
    @property
    def duration_seconds(self) -> float: ...
    @property
    def is_instrumental(self) -> bool: ...
    @property
    def vocal_style(self) -> str: ...
    @property
    def primary_topic(self) -> str | None: ...
    @property
    def secondary_topic(self) -> str | None: ...
    @property
    def persona_id(self) -> str | None: ...
    @property
    def blueprint_signature(self) -> str: ...
    @property
    def energy_at_generation(self) -> float: ...
    @property
    def regime_at_generation(self) -> str: ...
    @property
    def played_at(self) -> datetime | None: ...
    @property
    def created_at(self) -> datetime: ...


@dataclass(frozen=True)
class HistoryEntry:
    """A concrete :class:`ProgrammedTrack`, for tests and in-memory use."""

    track_id: str
    genre: str
    bpm: int
    musical_key: str
    duration_seconds: float
    is_instrumental: bool
    vocal_style: str
    blueprint_signature: str
    energy_at_generation: float
    regime_at_generation: str
    created_at: datetime
    secondary_genre: str | None = None
    primary_topic: str | None = None
    secondary_topic: str | None = None
    persona_id: str | None = None
    played_at: datetime | None = None


class ProgrammingHistory:
    """A windowed, queryable view of what the station has aired.

    Constructed fresh per scheduling decision from the repository's recent history, so
    there is no long-lived mutable state to drift out of sync with the database.
    """

    __slots__ = ("_entries",)

    def __init__(self, entries: Sequence[ProgrammedTrack]) -> None:
        # Most recent first. The repository already returns this order; sorting here
        # makes the class safe to construct from any source.
        self._entries = tuple(
            sorted(
                entries,
                key=lambda entry: entry.played_at or entry.created_at,
                reverse=True,
            )
        )

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)

    @property
    def entries(self) -> tuple[ProgrammedTrack, ...]:
        """Most recent first."""
        return self._entries

    @property
    def most_recent(self) -> ProgrammedTrack | None:
        return self._entries[0] if self._entries else None

    # -- windows -----------------------------------------------------------

    def recent(self, count: int) -> tuple[ProgrammedTrack, ...]:
        """The last ``count`` aired tracks, most recent first."""
        if count < 1:
            return ()
        return self._entries[:count]

    def since(self, moment: datetime) -> tuple[ProgrammedTrack, ...]:
        """Tracks aired at or after ``moment`` (§12's 24-hour and 7-day horizons)."""
        if moment.tzinfo is None:
            raise ValueError("moment must be timezone-aware")
        return tuple(
            entry
            for entry in self._entries
            if (entry.played_at or entry.created_at) >= moment
        )

    def within(self, now: datetime, span: timedelta) -> tuple[ProgrammedTrack, ...]:
        return self.since(now - span)

    # -- position queries --------------------------------------------------

    def positions_ago(self, predicate: Callable[[ProgrammedTrack], bool]) -> int | None:
        """How many tracks back the most recent match is, or ``None``.

        0 means the immediately previous track. Used by
        :func:`~tradefix_radio.director.selection.recency_penalty`.
        """
        for index, entry in enumerate(self._entries):
            if predicate(entry):
                return index
        return None

    def consecutive_leading(self, predicate: Callable[[ProgrammedTrack], bool]) -> int:
        """How many of the most recent tracks in a row satisfy ``predicate``.

        This is what §11's "same genre maximum 2 consecutive" counts. Note it counts
        from the *most recent* backwards and stops at the first non-match, which is
        what "consecutive" means for a listener.
        """
        count = 0
        for entry in self._entries:
            if not predicate(entry):
                break
            count += 1
        return count

    def appears_within(
        self, predicate: Callable[[ProgrammedTrack], bool], horizon: int
    ) -> bool:
        """Whether anything in the last ``horizon`` tracks matches."""
        if horizon < 1:
            return False
        return any(predicate(entry) for entry in self._entries[:horizon])

    # -- distributions -----------------------------------------------------

    def genre_counts(self, horizon: int | None = None) -> Counter[str]:
        window = self._entries if horizon is None else self._entries[:horizon]
        return Counter(entry.genre for entry in window)

    def key_counts(self, horizon: int | None = None) -> Counter[str]:
        window = self._entries if horizon is None else self._entries[:horizon]
        return Counter(entry.musical_key for entry in window)

    def topic_counts(self, horizon: int | None = None) -> Counter[str]:
        window = self._entries if horizon is None else self._entries[:horizon]
        return Counter(
            entry.primary_topic for entry in window if entry.primary_topic is not None
        )

    def persona_counts(self, horizon: int | None = None) -> Counter[str]:
        window = self._entries if horizon is None else self._entries[:horizon]
        return Counter(
            entry.persona_id for entry in window if entry.persona_id is not None
        )

    def topic_pairs(self, horizon: int | None = None) -> set[tuple[str, str]]:
        """Primary+secondary topic pairs, order-insensitive.

        §11: "same primary + secondary topic combination: never intentionally repeat".
        Order-insensitive because (discipline, patience) and (patience, discipline) are
        the same creative pairing from a listener's point of view.
        """
        window = self._entries if horizon is None else self._entries[:horizon]
        pairs: set[tuple[str, str]] = set()
        for entry in window:
            if entry.primary_topic and entry.secondary_topic:
                pairs.add(
                    (
                        min(entry.primary_topic, entry.secondary_topic),
                        max(entry.primary_topic, entry.secondary_topic),
                    )
                )
        return pairs

    def signatures(self, horizon: int | None = None) -> set[str]:
        window = self._entries if horizon is None else self._entries[:horizon]
        return {entry.blueprint_signature for entry in window}

    def bpm_values(self, horizon: int | None = None) -> tuple[int, ...]:
        window = self._entries if horizon is None else self._entries[:horizon]
        return tuple(entry.bpm for entry in window)

    def energy_values(self, horizon: int | None = None) -> tuple[float, ...]:
        window = self._entries if horizon is None else self._entries[:horizon]
        return tuple(entry.energy_at_generation for entry in window)

    def instrumental_ratio(self, horizon: int | None = None) -> float:
        """Fraction of recent tracks that were instrumental. 0.5 when empty."""
        window = self._entries if horizon is None else self._entries[:horizon]
        if not window:
            return 0.5
        return sum(1 for entry in window if entry.is_instrumental) / len(window)

    # -- helpers for specific §11 rules ------------------------------------

    def bpm_within(self, bpm: int, tolerance: int, horizon: int) -> bool:
        """§11: "same BPM +/- 4: not within previous 4 tracks"."""
        return self.appears_within(
            lambda entry: abs(entry.bpm - bpm) <= tolerance, horizon
        )

    def duration_within(
        self, duration_seconds: float, tolerance_seconds: float, horizon: int
    ) -> bool:
        return self.appears_within(
            lambda entry: abs(entry.duration_seconds - duration_seconds)
            <= tolerance_seconds,
            horizon,
        )

    def uses_any_genre(self, genre: str, horizon: int) -> bool:
        """Whether a genre appears as primary *or* secondary within the horizon.

        Secondary genres count: four consecutive tracks that all list ``dnb`` as a
        secondary genre would sound repetitive even though no primary genre repeated.
        """
        return self.appears_within(
            lambda entry: entry.genre == genre or entry.secondary_genre == genre,
            horizon,
        )


def shannon_entropy(counts: Iterable[int]) -> float:
    """Normalised Shannon **evenness** of a count distribution, 0–1.

    1.0 means the observed buckets are used equally; low values mean one dominates.
    Normalised by ``log(k)`` over the *observed* buckets, so this measures evenness only —
    it deliberately says nothing about how many options were available.

    A single bucket returns **0.0**: everything in one place is total concentration. An
    earlier version returned 1.0 here, reasoning that one observation has no unevenness to
    report — and the consequence was that thirty consecutive tracks of the same genre
    scored as *maximally varied*, which is precisely backwards. The genuinely-nothing-yet
    case is handled by the caller, which short-circuits on a history shorter than three
    tracks.

    Evenness alone is not variety: 15/15 across two genres is perfectly even and still
    narrow. :func:`variety_score` combines this with coverage.
    """
    values = [count for count in counts if count > 0]
    if not values:
        return 0.0
    if len(values) == 1:
        return 0.0
    total = sum(values)
    entropy = -sum((count / total) * math.log(count / total) for count in values)
    return min(1.0, entropy / math.log(len(values)))


def variety_score(counts: Iterable[int], *, target_distinct: int) -> float:
    """Variety as evenness **times** coverage, 0–1.

    Two different failures need to be caught, and either alone is insufficient:

    * **Unevenness** — one genre at 80 % of the window. Caught by
      :func:`shannon_entropy`.
    * **Narrowness** — two genres at exactly 50 % each. Perfectly even, and still a
      station that has collapsed to two genres.

    ``target_distinct`` is how many distinct values a healthily varied window would show.
    It is intentionally modest (a handful, not the whole library): §97 sessions are
    *supposed* to narrow programming, and demanding library-wide coverage would report a
    correctly-focused quiet session as broken.
    """
    values = [count for count in counts if count > 0]
    if not values:
        return 0.0
    if target_distinct < 1:
        raise ValueError("target_distinct must be at least 1")
    coverage = min(1.0, len(values) / target_distinct)
    return shannon_entropy(values) * coverage


def spread_score(values: Sequence[float], *, full_scale: float) -> float:
    """How spread out a set of values is, 0–1, saturating at ``full_scale``.

    Used for BPM and energy variety. Standard deviation rather than range, because a
    single outlier should not make an otherwise monotonous stretch look varied.
    """
    if len(values) < 2:
        return 1.0
    if full_scale <= 0:
        raise ValueError("full_scale must be positive")
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return min(1.0, math.sqrt(variance) / full_scale)


__all__ = [
    "HistoryEntry",
    "ProgrammedTrack",
    "ProgrammingHistory",
    "shannon_entropy",
    "spread_score",
    "variety_score",
]
