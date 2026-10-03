"""Station identifiers (§31, milestone 4.7).

§31's examples — "You're listening to Trade Fix Radio", "XAUUSD is waking up, we're turning the
energy up" — are *examples*. Nothing here is built around those sentences: identifiers are
records with a category, an audio file and a recurrence rule, loaded from configuration, and the
station works identically with a completely different set.

Two decisions worth stating.

**Identifiers participate in anti-repetition, with their own history.** §31 says so, and sharing
the music history would be wrong in both directions: a six-identifier library cycles far faster
than a 28-genre one, so a shared horizon would either exhaust the identifiers or barely
constrain the music. They get their own window.

**The scheduler decides when, not a counter.** "One after every song" is what §31 rules out, and
the useful rule is conditional — a market-transition identifier is worth airing *because the
market just moved*, and worth nothing ten minutes later. So selection here takes the context and
returns the best fit, or nothing.
"""

from __future__ import annotations

import enum
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import structlog

from tradefix_radio.contracts.enums import MarketRegime, TradingSession

_log = structlog.get_logger(__name__)


class StationIdCategory(str, enum.Enum):
    """What an identifier is *for*, which decides when it fits."""

    BRANDING = "branding"
    """Plain station identification. Always appropriate, never urgent."""

    MARKET_TRANSITION = "market_transition"
    """The regime changed. Worth airing immediately or not at all."""

    SESSION_TRANSITION = "session_transition"
    """London opened, New York closed (§97)."""

    MARKET_SWITCH = "market_switch"
    """The station moved between XAUUSD and BTCUSD.

    Distinct from ``MARKET_TRANSITION``, which is about a regime change *within* one market.
    This one tells the listener the subject has changed — the next songs are about a
    different instrument — and the branding does not: it is still Trade Fix Radio.
    """

    ENERGY_CHANGE = "energy_change"
    """The station's energy moved materially — §98's sharp change, made explicit."""

    GENERAL = "general"
    """Filler that fits anywhere. The fallback when nothing specific applies."""


@dataclass(frozen=True)
class StationIdRecord:
    """One identifier, as configuration rather than as code."""

    key: str
    category: StationIdCategory
    audio_path: Path
    duration_seconds: float
    #: Shortest gap, in tracks, before this exact identifier may air again.
    #:
    #: Per record rather than global: a generic branding line can recur every twenty tracks
    #: without anyone noticing, while "gold is waking up" said twice in an hour sounds broken.
    minimum_recurrence_tracks: int = 20
    #: Display text, for the §42 card and the §46 detail page. Not used for playback.
    text: str = ""

    def __post_init__(self) -> None:
        if not self.key.strip():
            # The key is the identity: history, recurrence and §96's restored state are all
            # filed under it. Two records with a blank key would share one history entry and
            # suppress each other for reasons no log would explain.
            raise ValueError("a station id needs a non-empty key")
        if self.duration_seconds <= 0:
            raise ValueError(f"station id {self.key!r} has a non-positive duration")
        if self.minimum_recurrence_tracks < 1:
            raise ValueError(f"station id {self.key!r} needs a recurrence of at least 1")


@dataclass
class StationIdPlay:
    """When an identifier last aired, and how often it has."""

    key: str
    play_count: int = 0
    last_played_at: datetime | None = None
    #: Value of the station's track counter when this last aired. The recurrence rule is in
    #: tracks rather than in time, because that is what a listener experiences.
    last_played_at_track: int | None = None


@dataclass
class StationIdStats:
    requested: int = 0
    selected: int = 0
    suppressed_by_recurrence: int = 0
    no_candidate: int = 0
    by_category: dict[str, int] = field(default_factory=dict)


class StationIdLibrary:
    """Chooses identifiers, and remembers what it chose (§31).

    The anti-repetition history is separate from the music's and is restored across a restart
    (§96) — otherwise the station opens every restart by repeating whatever it said last.
    """

    def __init__(
        self,
        records: list[StationIdRecord],
        *,
        repeat_horizon: int = 8,
        rng: random.Random | None = None,
    ) -> None:
        if repeat_horizon < 1:
            raise ValueError("repeat_horizon must be at least 1")
        self._records = {record.key: record for record in records}
        if len(self._records) != len(records):
            raise ValueError("station id keys must be unique")
        # Capped at one short of the library size, so there is always something eligible.
        #
        # The recent-plays window is a *global* "do not repeat the last N" guard, and it
        # deadlocks a library no larger than the window: with one record and a horizon of eight,
        # that record enters the window on its first play and is never evicted, so the station
        # loses its identity permanently. A one-record library is a perfectly reasonable
        # configuration, and the per-record recurrence rule already enforces distance, so the
        # window only needs to break ties between records that exist.
        self._horizon = max(0, min(repeat_horizon, len(self._records) - 1))
        self._rng = rng or random.Random()  # noqa: S311 - creative choice, not security
        self._plays: dict[str, StationIdPlay] = {
            record.key: StationIdPlay(key=record.key) for record in records
        }
        self._recent: deque[str] = deque(maxlen=self._horizon)
        self._stats = StationIdStats()
        self._track_counter = 0

    # -- introspection -----------------------------------------------------

    @property
    def stats(self) -> StationIdStats:
        return self._stats

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def get(self, key: str) -> StationIdRecord | None:
        return self._records.get(key)

    def plays(self) -> dict[str, StationIdPlay]:
        return dict(self._plays)

    def note_track_aired(self) -> None:
        """Advance the track counter the recurrence rule is measured in."""
        self._track_counter += 1

    # -- selection ---------------------------------------------------------

    def select(
        self,
        *,
        category: StationIdCategory | None = None,
        regime: MarketRegime | None = None,  # noqa: ARG002 - see note below
        session: TradingSession | None = None,  # noqa: ARG002 - see note below
    ) -> StationIdRecord | None:
        """Best identifier for the moment, or ``None`` if nothing is eligible.

        ``regime`` and ``session`` are accepted and currently unused. They are in the signature
        because the caller genuinely has them and because per-regime identifiers are the obvious
        next step — but nothing here reads them yet, and wiring them into selection before any
        record declares a regime affinity would be inventing a mechanism with no content. Stated
        rather than quietly dropped.

        ``None`` is a normal answer, not a failure. Every identifier in a small library can
        legitimately be inside its recurrence window, and the right response is to play music —
        airing a repeat would be worse than airing nothing.

        Falls back from the requested category to ``GENERAL`` and then ``BRANDING``, because a
        library with no market-transition line should still be able to identify the station.
        """
        self._stats.requested += 1
        for candidate_category in self._category_preference(category):
            chosen = self._pick(candidate_category)
            if chosen is not None:
                self._stats.selected += 1
                self._stats.by_category[chosen.category.value] = (
                    self._stats.by_category.get(chosen.category.value, 0) + 1
                )
                return chosen
        self._stats.no_candidate += 1
        _log.debug(
            "station_id.none_eligible",
            requested=None if category is None else category.value,
            library_size=len(self._records),
            detail="every identifier is inside its recurrence window",
        )
        return None

    @staticmethod
    def _category_preference(
        requested: StationIdCategory | None,
    ) -> tuple[StationIdCategory, ...]:
        if requested is None:
            return (StationIdCategory.GENERAL, StationIdCategory.BRANDING)
        if requested in {StationIdCategory.GENERAL, StationIdCategory.BRANDING}:
            return (requested, StationIdCategory.BRANDING, StationIdCategory.GENERAL)
        return (requested, StationIdCategory.GENERAL, StationIdCategory.BRANDING)

    def _pick(self, category: StationIdCategory) -> StationIdRecord | None:
        eligible = [
            record
            for record in self._records.values()
            if record.category is category and self._is_eligible(record)
        ]
        if not eligible:
            return None
        # Least recently played first, so a library is used evenly rather than favouring
        # whichever record the RNG likes. Ties broken randomly, so the order is not fixed.
        self._rng.shuffle(eligible)
        eligible.sort(key=lambda record: self._plays[record.key].play_count)
        return eligible[0]

    def _is_eligible(self, record: StationIdRecord) -> bool:
        if not record.audio_path.is_file():
            # Checked here rather than at load time: the retention sweeper (§36) and an
            # operator both move these files, so "it existed at startup" is not a guarantee.
            # Silently skipping beats offering a record the playout engine will fail to read
            # and log about on every rotation.
            return False
        if record.key in self._recent:
            self._stats.suppressed_by_recurrence += 1
            return False
        play = self._plays[record.key]
        if play.last_played_at_track is None:
            return True
        elapsed = self._track_counter - play.last_played_at_track
        if elapsed < record.minimum_recurrence_tracks:
            self._stats.suppressed_by_recurrence += 1
            return False
        return True

    # -- recording ---------------------------------------------------------

    def note_played(self, key: str, *, now: datetime) -> None:
        record = self._records.get(key)
        if record is None:
            _log.warning("station_id.unknown_played", key=key)
            return
        play = self._plays[key]
        play.play_count += 1
        play.last_played_at = now
        play.last_played_at_track = self._track_counter
        self._recent.append(key)
        _log.info(
            "station_id.played",
            key=key,
            category=record.category.value,
            play_count=play.play_count,
        )

    # -- persistence (§96) -------------------------------------------------

    def export_state(self) -> dict[str, object]:
        """Serialisable history, for radio memory."""
        return {
            "track_counter": self._track_counter,
            "recent": list(self._recent),
            "plays": {
                key: {
                    "play_count": play.play_count,
                    "last_played_at": (
                        None if play.last_played_at is None else play.last_played_at.isoformat()
                    ),
                    "last_played_at_track": play.last_played_at_track,
                }
                for key, play in self._plays.items()
            },
        }

    def restore_state(self, state: dict[str, object]) -> None:
        """Reload history after a restart (§96).

        Tolerant of anything malformed: a creative hint that cannot be parsed is worth a warning
        and a fresh start, not an outage. The worst case is the station repeating an identifier
        once, which is a cosmetic problem — unlike refusing to boot, which is not.
        """
        counter = state.get("track_counter")
        if isinstance(counter, int) and counter >= 0:
            self._track_counter = counter

        recent = state.get("recent")
        if isinstance(recent, list):
            self._recent = deque(
                (key for key in recent if isinstance(key, str) and key in self._records),
                maxlen=self._horizon,
            )

        plays = state.get("plays")
        if not isinstance(plays, dict):
            return
        for key, raw in plays.items():
            if key not in self._plays or not isinstance(raw, dict):
                continue
            play = self._plays[key]
            count = raw.get("play_count")
            if isinstance(count, int) and count >= 0:
                play.play_count = count
            at_track = raw.get("last_played_at_track")
            if isinstance(at_track, int):
                play.last_played_at_track = at_track
            stamp = raw.get("last_played_at")
            if isinstance(stamp, str):
                try:
                    play.last_played_at = datetime.fromisoformat(stamp)
                except ValueError:
                    _log.warning("station_id.bad_timestamp", key=key, value=stamp)


def default_library(station_id_dir: Path) -> list[StationIdRecord]:
    """The records the station ships with, pointing at files in ``station_id_dir``.

    A function rather than a constant because the paths depend on configuration. The *text* is
    here only as documentation of what each clip says — nothing reads it for playback, so
    replacing the audio with different wording needs no code change, which is what §31's "do
    not hard-code the system around those exact sentences" asks for.
    """
    return [
        StationIdRecord(
            key="branding_listening",
            category=StationIdCategory.BRANDING,
            audio_path=station_id_dir / "branding_listening.flac",
            duration_seconds=4.0,
            minimum_recurrence_tracks=14,
            text="You're listening to Trade Fix Radio.",
        ),
        StationIdRecord(
            key="branding_markets_shape",
            category=StationIdCategory.BRANDING,
            audio_path=station_id_dir / "branding_markets_shape.flac",
            duration_seconds=5.0,
            minimum_recurrence_tracks=14,
            text="Trade Fix Radio — where markets shape the sound.",
        ),
        StationIdRecord(
            key="market_waking_up",
            category=StationIdCategory.MARKET_TRANSITION,
            audio_path=station_id_dir / "market_waking_up.flac",
            duration_seconds=5.0,
            minimum_recurrence_tracks=25,
            text="XAUUSD is waking up. We're turning the energy up.",
        ),
        StationIdRecord(
            key="market_settling",
            category=StationIdCategory.MARKET_TRANSITION,
            audio_path=station_id_dir / "market_settling.flac",
            duration_seconds=5.0,
            minimum_recurrence_tracks=25,
            text="The tape is settling. Easing back down with it.",
        ),
        # Market-switch identifiers.
        #
        # Shipped without audio in V1. The library skips any record whose file is missing,
        # so these cost nothing until someone records them — and they document the exact
        # wording the category is for, which §31 asks the code not to assume.
        #
        # Note what they do *not* say: no claim that gold will reopen at a particular time,
        # and no suggestion that either market is the better one to trade. §14 applies to a
        # four-second clip as much as to a verse.
        StationIdRecord(
            key="market_switch_to_bitcoin",
            category=StationIdCategory.MARKET_SWITCH,
            audio_path=station_id_dir / "market_switch_to_bitcoin.flac",
            duration_seconds=5.0,
            minimum_recurrence_tracks=25,
            text="Gold's closed for now. We're following Bitcoin. Trade Fix Radio.",
        ),
        StationIdRecord(
            key="market_switch_to_gold",
            category=StationIdCategory.MARKET_SWITCH,
            audio_path=station_id_dir / "market_switch_to_gold.flac",
            duration_seconds=5.0,
            minimum_recurrence_tracks=25,
            text="Gold is back open. Back on XAUUSD. Trade Fix Radio.",
        ),
        StationIdRecord(
            key="session_london",
            category=StationIdCategory.SESSION_TRANSITION,
            audio_path=station_id_dir / "session_london.flac",
            duration_seconds=4.0,
            minimum_recurrence_tracks=30,
            text="London is open. Trade Fix Radio.",
        ),
        StationIdRecord(
            key="session_new_york",
            category=StationIdCategory.SESSION_TRANSITION,
            audio_path=station_id_dir / "session_new_york.flac",
            duration_seconds=4.0,
            minimum_recurrence_tracks=30,
            text="New York is in. Trade Fix Radio.",
        ),
        StationIdRecord(
            key="energy_lifting",
            category=StationIdCategory.ENERGY_CHANGE,
            audio_path=station_id_dir / "energy_lifting.flac",
            duration_seconds=4.0,
            minimum_recurrence_tracks=20,
            text="Lifting the energy. Trade Fix Radio.",
        ),
        StationIdRecord(
            key="general_keep_it_here",
            category=StationIdCategory.GENERAL,
            audio_path=station_id_dir / "general_keep_it_here.flac",
            duration_seconds=3.0,
            minimum_recurrence_tracks=12,
            text="Keep it here. Trade Fix Radio.",
        ),
    ]


__all__ = [
    "StationIdCategory",
    "StationIdLibrary",
    "StationIdPlay",
    "StationIdRecord",
    "StationIdStats",
    "default_library",
]
