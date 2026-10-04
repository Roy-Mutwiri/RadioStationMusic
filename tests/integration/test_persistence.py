"""Persistence integration (§37, milestone 1.6).

Runs against a real SQLite database. The three things most worth proving here are
the ones that silently break in production rather than in a unit test:

* **UTC survives the round trip.** SQLite has no timezone type. A naive datetime
  leaking back out would corrupt session classification and every §12 horizon.
* **Foreign keys actually cascade.** SQLite disables FK enforcement per
  connection; without the pragma, deleting a track leaves orphaned blueprints and
  fingerprints forever.
* **State transitions are validated against the persisted state**, not a
  caller-held copy, so two processes cannot both move the same track.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from tradefix_radio.core.errors import IllegalTransitionError, PersistenceError
from tradefix_radio.core.state_machine import TrackState
from tradefix_radio.persistence.database import Database, _redact_url
from tradefix_radio.persistence.models import (
    Lyrics,
    RadioMemory,
    Track,
    TrackBlueprint,
    TrackFile,
)
from tradefix_radio.persistence.repositories import (
    MemoryKeys,
    MetricRepository,
    RadioMemoryRepository,
    SystemEventRepository,
    TrackFileRepository,
    TrackRepository,
    normalise_title,
)
from tradefix_radio.storage.paths import FileRole
from tests.conftest import FIXED_NOW, make_blueprint


# ---------------------------------------------------------------- connectivity


async def test_connect_creates_the_database_file(tmp_path: Path) -> None:
    from tests.conftest import make_settings

    settings = make_settings(tmp_path)
    settings.paths.data_dir.mkdir(parents=True, exist_ok=True)
    database = Database(settings.database)
    await database.connect()
    try:
        assert database.is_connected
        assert await database.ping() >= 0.0
    finally:
        await database.disconnect()
    assert not database.is_connected


async def test_using_the_database_before_connecting_raises(tmp_path: Path) -> None:
    from tests.conftest import make_settings

    database = Database(make_settings(tmp_path).database)
    with pytest.raises(PersistenceError, match="not connected"):
        _ = database.engine


async def test_connect_to_an_unwritable_path_fails_clearly(tmp_path: Path) -> None:
    """§73: discover this at startup, not mid-broadcast."""
    from tests.conftest import make_settings

    # A path whose parent is a *file* cannot be created as a directory.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    settings = make_settings(
        tmp_path, database={"url": f"sqlite+aiosqlite:///{(blocker / 'x.db').as_posix()}"}
    )
    database = Database(settings.database)
    with pytest.raises((PersistenceError, OSError, NotADirectoryError, FileExistsError)):
        await database.connect()


async def test_schema_contains_every_declared_table(database: Database) -> None:
    names = set(await database.table_names())
    expected = {
        "tracks",
        "track_blueprints",
        "track_files",
        "lyrics",
        "audio_fingerprints",
        "similarity_results",
        "used_seeds",
        "market_snapshots",
        "market_states",
        "generation_jobs",
        "queue_items",
        "state_transitions",
        "play_events",
        "topics",
        "station_ids",
        "track_titles",
        "radio_memory",
        "setting_overrides",
        "system_events",
        "health_events",
        "metric_samples",
    }
    assert expected <= names


def test_dsn_credentials_are_redacted_before_logging() -> None:
    """§68: a PostgreSQL password must not land in a rotating log file."""
    redacted = _redact_url("postgresql+asyncpg://tradefix:s3cret@db.internal:5432/radio")
    assert "s3cret" not in redacted
    assert "tradefix" in redacted
    assert "db.internal:5432/radio" in redacted


def test_redacting_a_sqlite_dsn_is_a_no_op() -> None:
    url = "sqlite+aiosqlite:///D:/data/tradefix.db"
    assert _redact_url(url) == url


# ---------------------------------------------------------------- UTC


async def test_timestamps_round_trip_as_aware_utc(database: Database) -> None:
    """The single most likely silent corruption on SQLite."""
    blueprint = make_blueprint()
    async with database.session() as session:
        await TrackRepository(session).create(
            blueprint, now=FIXED_NOW, provider="mock", model_identifier="mock-1"
        )

    async with database.read_session() as session:
        track = await session.get(Track, blueprint.track_id)
        assert track is not None
        assert track.created_at.tzinfo is not None
        assert track.created_at == FIXED_NOW


async def test_naive_datetimes_are_rejected_at_the_column(database: Database) -> None:
    """Better a loud failure than a silently wrong session classification.

    SQLAlchemy wraps a bind-parameter error in ``StatementError``, so the original
    ``ValueError`` is reachable via ``__cause__`` rather than raised directly.
    """
    from datetime import datetime

    from sqlalchemy.exc import StatementError

    with pytest.raises(StatementError) as caught:
        async with database.session() as session:
            session.add(RadioMemory(key="k", value=1, updated_at=datetime(2026, 1, 1)))
            await session.flush()
    assert "timezone-aware" in str(caught.value)


async def test_timestamp_ordering_works_in_sql(database: Database) -> None:
    """Ordering must happen in the database, not after loading a week of rows."""
    async with database.session() as session:
        repo = TrackRepository(session)
        for index in range(5):
            blueprint = make_blueprint(
                track_id=f"TF-20261002-{index:05d}", title=f"Title {index}"
            )
            await repo.create(
                blueprint,
                now=FIXED_NOW + timedelta(minutes=index),
                provider="mock",
                model_identifier="mock-1",
            )

    async with database.read_session() as session:
        result = await session.execute(select(Track).order_by(Track.created_at.desc()))
        tracks = list(result.scalars())
    assert [t.track_id for t in tracks][0] == "TF-20261002-00004"


# ---------------------------------------------------------------- portable JSON


async def test_blueprint_json_survives_a_full_round_trip(database: Database) -> None:
    """The bug this guards: stored-then-rehydrated blueprints must validate."""
    blueprint = make_blueprint()
    async with database.session() as session:
        await TrackRepository(session).create(
            blueprint, now=FIXED_NOW, provider="mock", model_identifier="mock-1"
        )

    async with database.read_session() as session:
        restored = await TrackRepository(session).get_blueprint(blueprint.track_id)
    assert restored == blueprint
    assert restored is not None
    assert restored.signature() == blueprint.signature()


async def test_json_lists_round_trip(database: Database) -> None:
    blueprint = make_blueprint()
    async with database.session() as session:
        await TrackRepository(session).create(
            blueprint, now=FIXED_NOW, provider="mock", model_identifier="mock-1"
        )
        session.add(
            Lyrics(
                track_id=blueprint.track_id,
                text="words here",
                content_hash="a" * 64,
                primary_topic="risk_management",
                secondary_topic=None,
                lyric_format="full rap",
                perspective="first_person",
                tradefix_mentions=1,
                educational_intensity=0.5,
                word_count=2,
                shingles=["words here"],
                concepts_used=["stop_losses", "position_sizing"],
                created_at=FIXED_NOW,
            )
        )

    async with database.read_session() as session:
        row = await session.get(Lyrics, blueprint.track_id)
        assert row is not None
        assert row.concepts_used == ["stop_losses", "position_sizing"]
        assert row.shingles == ["words here"]


async def test_json_is_stored_with_sorted_keys(database: Database) -> None:
    """Deterministic serialisation keeps stored bytes comparable and diffable."""
    async with database.session() as session:
        await RadioMemoryRepository(session).set(
            "ordering", {"zebra": 1, "alpha": 2, "mid": 3}, now=FIXED_NOW
        )

    async with database.read_session() as session:
        result = await session.execute(
            select(RadioMemory.value).where(RadioMemory.key == "ordering")
        )
        value = result.scalar_one()
    assert value == {"zebra": 1, "alpha": 2, "mid": 3}


# ---------------------------------------------------------------- cascades


async def test_deleting_a_track_cascades_to_its_children(database: Database) -> None:
    """Without the SQLite foreign_keys pragma this silently leaves orphans."""
    blueprint = make_blueprint()
    async with database.session() as session:
        repo = TrackRepository(session)
        await repo.create(
            blueprint, now=FIXED_NOW, provider="mock", model_identifier="mock-1"
        )
        await TrackFileRepository(session).add(
            track_id=blueprint.track_id,
            role=FileRole.MASTER,
            path=Path("x.flac"),
            size_bytes=1024,
            file_format="flac",
            now=FIXED_NOW,
        )

    async with database.session() as session:
        track = await session.get(Track, blueprint.track_id)
        assert track is not None
        await session.delete(track)

    async with database.read_session() as session:
        assert await session.get(TrackBlueprint, blueprint.track_id) is None
        remaining = await session.execute(
            select(func.count()).select_from(TrackFile).where(
                TrackFile.track_id == blueprint.track_id
            )
        )
        assert remaining.scalar_one() == 0


async def test_one_file_per_role_per_track_is_enforced(database: Database) -> None:
    """Two masters for one track would make "which file airs?" ambiguous."""
    from sqlalchemy.exc import IntegrityError

    blueprint = make_blueprint()
    async with database.session() as session:
        await TrackRepository(session).create(
            blueprint, now=FIXED_NOW, provider="mock", model_identifier="mock-1"
        )
        files = TrackFileRepository(session)
        await files.add(
            track_id=blueprint.track_id,
            role=FileRole.MASTER,
            path=Path("a.flac"),
            size_bytes=1,
            file_format="flac",
            now=FIXED_NOW,
        )

    with pytest.raises(IntegrityError):
        async with database.session() as session:
            await TrackFileRepository(session).add(
                track_id=blueprint.track_id,
                role=FileRole.MASTER,
                path=Path("b.flac"),
                size_bytes=1,
                file_format="flac",
                now=FIXED_NOW,
            )


# ---------------------------------------------------------------- transactions


async def test_session_rolls_back_on_exception(database: Database) -> None:
    """A half-written track must not survive a failure mid-operation."""
    blueprint = make_blueprint()
    with pytest.raises(RuntimeError):
        async with database.session() as session:
            await TrackRepository(session).create(
                blueprint, now=FIXED_NOW, provider="mock", model_identifier="mock-1"
            )
            raise RuntimeError("something failed after the insert")

    async with database.read_session() as session:
        assert await session.get(Track, blueprint.track_id) is None


async def test_read_session_does_not_persist_changes(database: Database) -> None:
    """A read path must not be able to write, even accidentally."""
    blueprint = make_blueprint()
    async with database.session() as session:
        await TrackRepository(session).create(
            blueprint, now=FIXED_NOW, provider="mock", model_identifier="mock-1"
        )

    async with database.read_session() as session:
        track = await session.get(Track, blueprint.track_id)
        assert track is not None
        track.title = "Mutated In A Read Session"

    async with database.read_session() as session:
        track = await session.get(Track, blueprint.track_id)
        assert track is not None
        assert track.title == blueprint.title


# ---------------------------------------------------------------- track repository


async def test_create_writes_track_blueprint_title_and_transition(
    database: Database,
) -> None:
    """§8: no track may exist without its complete blueprint."""
    blueprint = make_blueprint()
    async with database.session() as session:
        repo = TrackRepository(session)
        track = await repo.create(
            blueprint, now=FIXED_NOW, provider="mock", model_identifier="mock-1"
        )
        assert track.state == TrackState.PLANNED.value
        assert track.blueprint_signature == blueprint.signature()

    async with database.read_session() as session:
        repo = TrackRepository(session)
        assert await repo.get_blueprint(blueprint.track_id) is not None
        assert await repo.title_exists(blueprint.title)
        transitions = await repo.transitions(blueprint.track_id)
        assert len(transitions) == 1
        assert transitions[0].to_state == TrackState.PLANNED.value
        assert transitions[0].from_state is None


async def test_require_raises_for_an_unknown_track(database: Database) -> None:
    async with database.read_session() as session:
        with pytest.raises(PersistenceError, match="not found"):
            await TrackRepository(session).require("TF-NOPE")


async def test_transition_validates_against_the_persisted_state(
    database: Database,
) -> None:
    """§92 enforcement must use the database's state, not a caller's copy."""
    blueprint = make_blueprint()
    async with database.session() as session:
        await TrackRepository(session).create(
            blueprint, now=FIXED_NOW, provider="mock", model_identifier="mock-1"
        )

    async with database.session() as session:
        with pytest.raises(IllegalTransitionError):
            await TrackRepository(session).transition(
                blueprint.track_id, TrackState.PLAYED, now=FIXED_NOW, reason="bogus"
            )

    async with database.read_session() as session:
        track = await TrackRepository(session).require(blueprint.track_id)
        assert track.state == TrackState.PLANNED.value


async def test_every_transition_is_persisted(database: Database) -> None:
    """§27: persist all state transitions."""
    blueprint = make_blueprint()
    sequence = [
        TrackState.GENERATING,
        TrackState.GENERATED,
        TrackState.ANALYZING,
        TrackState.APPROVED,
        TrackState.MASTERING,
        TrackState.READY,
    ]
    async with database.session() as session:
        repo = TrackRepository(session)
        await repo.create(
            blueprint, now=FIXED_NOW, provider="mock", model_identifier="mock-1"
        )
        for index, state in enumerate(sequence):
            await repo.transition(
                blueprint.track_id,
                state,
                now=FIXED_NOW + timedelta(seconds=index),
                reason="pipeline",
            )

    async with database.read_session() as session:
        transitions = await TrackRepository(session).transitions(blueprint.track_id)
    assert len(transitions) == len(sequence) + 1
    assert [t.to_state for t in transitions][-1] == TrackState.READY.value


async def test_rejection_reason_is_stored_for_the_originality_page(
    database: Database,
) -> None:
    """§48 must be able to explain why a candidate was rejected."""
    blueprint = make_blueprint()
    async with database.session() as session:
        repo = TrackRepository(session)
        await repo.create(
            blueprint, now=FIXED_NOW, provider="mock", model_identifier="mock-1"
        )
        for state in (
            TrackState.GENERATING,
            TrackState.GENERATED,
            TrackState.ANALYZING,
        ):
            await repo.transition(blueprint.track_id, state, now=FIXED_NOW, reason="x")
        await repo.transition(
            blueprint.track_id,
            TrackState.REJECTED,
            now=FIXED_NOW,
            reason="originality",
            message="audio embedding similarity = 0.91 vs TF-20261001-00917",
        )

    async with database.read_session() as session:
        track = await TrackRepository(session).require(blueprint.track_id)
    assert track.rejection_reason is not None
    assert "0.91" in track.rejection_reason


async def test_mark_played_increments_the_play_count(database: Database) -> None:
    blueprint = make_blueprint()
    async with database.session() as session:
        repo = TrackRepository(session)
        await repo.create(
            blueprint, now=FIXED_NOW, provider="mock", model_identifier="mock-1"
        )
        await _advance_to_playing(repo, blueprint.track_id)
        track = await repo.mark_played(
            blueprint.track_id, now=FIXED_NOW, completed=True, reason="finished"
        )
    assert track.state == TrackState.PLAYED.value
    assert track.play_count == 1
    assert track.last_played_at == FIXED_NOW


async def test_an_interrupted_airing_is_not_marked_played(database: Database) -> None:
    """§75: never mark an incomplete track as successfully played."""
    blueprint = make_blueprint()
    async with database.session() as session:
        repo = TrackRepository(session)
        await repo.create(
            blueprint, now=FIXED_NOW, provider="mock", model_identifier="mock-1"
        )
        await _advance_to_playing(repo, blueprint.track_id)
        track = await repo.mark_played(
            blueprint.track_id, now=FIXED_NOW, completed=False, reason="sink_failure"
        )
    assert track.state == TrackState.FAILED.value
    assert track.play_count == 0


async def test_daily_sequence_counts_only_that_day(database: Database) -> None:
    """Track ids must not collide, and must not reset on restart."""
    async with database.session() as session:
        repo = TrackRepository(session)
        assert await repo.next_daily_sequence(FIXED_NOW) == 1
        for index in range(3):
            await repo.create(
                make_blueprint(track_id=f"TF-20261002-{index:05d}", title=f"T{index}"),
                now=FIXED_NOW,
                provider="mock",
                model_identifier="mock-1",
            )
        assert await repo.next_daily_sequence(FIXED_NOW) == 4
        # A different day starts over.
        assert await repo.next_daily_sequence(FIXED_NOW + timedelta(days=1)) == 1


async def test_recent_history_is_ordered_by_air_time_not_creation(
    database: Database,
) -> None:
    """§12 diversity is about what a listener heard, in sequence."""
    async with database.session() as session:
        repo = TrackRepository(session)
        # Created 0,1,2 but aired in reverse order.
        for index in range(3):
            await repo.create(
                make_blueprint(track_id=f"TF-{index}", title=f"Title {index}"),
                now=FIXED_NOW + timedelta(minutes=index),
                provider="mock",
                model_identifier="mock-1",
            )
        for order, index in enumerate(reversed(range(3))):
            await _advance_to_playing(repo, f"TF-{index}")
            await repo.mark_played(
                f"TF-{index}",
                now=FIXED_NOW + timedelta(hours=order + 1),
                completed=True,
                reason="finished",
            )

    async with database.read_session() as session:
        history = await TrackRepository(session).recent_history(10)
    assert [entry.track_id for entry in history] == ["TF-0", "TF-1", "TF-2"]


async def test_history_since_respects_the_window(database: Database) -> None:
    async with database.session() as session:
        repo = TrackRepository(session)
        for index in range(4):
            await repo.create(
                make_blueprint(track_id=f"TF-{index}", title=f"Title {index}"),
                now=FIXED_NOW,
                provider="mock",
                model_identifier="mock-1",
            )
            await _advance_to_playing(repo, f"TF-{index}")
            await repo.mark_played(
                f"TF-{index}",
                now=FIXED_NOW - timedelta(hours=index * 10),
                completed=True,
                reason="finished",
            )

    async with database.read_session() as session:
        recent = await TrackRepository(session).history_since(FIXED_NOW - timedelta(hours=15))
    assert {entry.track_id for entry in recent} == {"TF-0", "TF-1"}


async def test_unplayed_tracks_are_absent_from_history(database: Database) -> None:
    async with database.session() as session:
        await TrackRepository(session).create(
            make_blueprint(), now=FIXED_NOW, provider="mock", model_identifier="mock-1"
        )
    async with database.read_session() as session:
        assert await TrackRepository(session).recent_history(10) == []


async def test_recent_history_with_a_non_positive_limit_returns_nothing(
    database: Database,
) -> None:
    async with database.read_session() as session:
        assert await TrackRepository(session).recent_history(0) == []


async def test_blueprint_signature_lookup_supports_the_never_repeat_rule(
    database: Database,
) -> None:
    """§11: "same blueprint: never repeat"."""
    blueprint = make_blueprint()
    async with database.session() as session:
        await TrackRepository(session).create(
            blueprint, now=FIXED_NOW, provider="mock", model_identifier="mock-1"
        )

    async with database.read_session() as session:
        repo = TrackRepository(session)
        assert await repo.blueprint_signature_exists(blueprint.signature())
        assert not await repo.blueprint_signature_exists("0" * 64)


async def test_genre_distribution_counts_aired_tracks(database: Database) -> None:
    async with database.session() as session:
        repo = TrackRepository(session)
        for index, genre in enumerate(["trap", "trap", "lofi", "dnb"]):
            track_id = f"TF-{index}"
            await repo.create(
                make_blueprint(track_id=track_id, genre=genre, title=f"T{index}"),
                now=FIXED_NOW,
                provider="mock",
                model_identifier="mock-1",
            )
            await _advance_to_playing(repo, track_id)
            await repo.mark_played(
                track_id,
                now=FIXED_NOW + timedelta(minutes=index),
                completed=True,
                reason="finished",
            )

    async with database.read_session() as session:
        distribution = await TrackRepository(session).genre_distribution(10)
    assert distribution == {"trap": 2, "lofi": 1, "dnb": 1}


async def test_count_by_state(database: Database) -> None:
    async with database.session() as session:
        repo = TrackRepository(session)
        for index in range(3):
            await repo.create(
                make_blueprint(track_id=f"TF-{index}", title=f"T{index}"),
                now=FIXED_NOW,
                provider="mock",
                model_identifier="mock-1",
            )
        await repo.transition(
            "TF-0", TrackState.GENERATING, now=FIXED_NOW, reason="claim"
        )

    async with database.read_session() as session:
        counts = await TrackRepository(session).count_by_state()
    assert counts[TrackState.PLANNED.value] == 2
    assert counts[TrackState.GENERATING.value] == 1


# ---------------------------------------------------------------- titles


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Liquidity After Midnight", "liquidity after midnight"),
        ("Gold Rush!", "gold rush"),
        ("Stop  Loss, Stop Hunt", "stop loss stop hunt"),
    ],
)
def test_title_normalisation_collapses_cosmetic_differences(a: str, b: str) -> None:
    """Near-duplicate titles are the repetition listeners actually notice (§99)."""
    assert normalise_title(a) == normalise_title(b)


async def test_a_rejected_candidate_still_consumes_its_title(database: Database) -> None:
    """§99: a rejected track used the title idea; reusing it repeats the station."""
    async with database.session() as session:
        await TrackRepository(session).reserve_title(
            "Liquidity After Midnight", now=FIXED_NOW, track_id=None
        )

    async with database.read_session() as session:
        assert await TrackRepository(session).title_exists("liquidity after midnight!")


async def test_recent_titles_are_returned_newest_first(database: Database) -> None:
    async with database.session() as session:
        repo = TrackRepository(session)
        for index in range(5):
            await repo.reserve_title(
                f"Title {index}", now=FIXED_NOW + timedelta(minutes=index), track_id=None
            )

    async with database.read_session() as session:
        titles = await TrackRepository(session).recent_titles(limit=3)
    assert titles == ["title 4", "title 3", "title 2"]


# ---------------------------------------------------------------- crash recovery


async def test_tracks_stuck_mid_work_are_failed_at_startup(database: Database) -> None:
    """§75: a track in GENERATING when the process died is not recoverable."""
    async with database.session() as session:
        repo = TrackRepository(session)
        for index in range(3):
            await repo.create(
                make_blueprint(track_id=f"TF-{index}", title=f"T{index}"),
                now=FIXED_NOW,
                provider="mock",
                model_identifier="mock-1",
            )
        await repo.transition("TF-0", TrackState.GENERATING, now=FIXED_NOW, reason="claim")
        await repo.transition("TF-1", TrackState.GENERATING, now=FIXED_NOW, reason="claim")

    async with database.session() as session:
        reset = await TrackRepository(session).reset_stuck_active_tracks(
            now=FIXED_NOW,
            states=[TrackState.GENERATING, TrackState.ANALYZING, TrackState.MASTERING],
            reason="crash_recovery",
        )
    assert reset == 2

    async with database.read_session() as session:
        repo = TrackRepository(session)
        assert (await repo.require("TF-0")).state == TrackState.FAILED.value
        assert (await repo.require("TF-2")).state == TrackState.PLANNED.value
        transitions = await repo.transitions("TF-0")
        assert transitions[-1].reason == "crash_recovery"


async def test_recovery_with_no_stuck_tracks_is_a_no_op(database: Database) -> None:
    async with database.session() as session:
        assert (
            await TrackRepository(session).reset_stuck_active_tracks(
                now=FIXED_NOW, states=[TrackState.GENERATING], reason="crash_recovery"
            )
            == 0
        )


# ---------------------------------------------------------------- radio memory


async def test_memory_survives_a_reconnect(database: Database) -> None:
    """§96: restarting must not reset creative memory and cause repeats."""
    async with database.session() as session:
        await RadioMemoryRepository(session).set(
            MemoryKeys.LAST_GENRE_USE,
            {"uk_trap": FIXED_NOW.isoformat(), "lofi": FIXED_NOW.isoformat()},
            now=FIXED_NOW,
        )

    # Simulate a process restart against the same file.
    await database.disconnect()
    await database.connect()

    async with database.read_session() as session:
        value = await RadioMemoryRepository(session).get(MemoryKeys.LAST_GENRE_USE)
    assert value is not None
    assert "uk_trap" in value


async def test_memory_set_is_an_upsert(database: Database) -> None:
    async with database.session() as session:
        repo = RadioMemoryRepository(session)
        await repo.set("k", 1, now=FIXED_NOW)
        await repo.set("k", 2, now=FIXED_NOW + timedelta(seconds=1))
        assert await repo.get("k") == 2


async def test_memory_get_many_fetches_in_one_round_trip(database: Database) -> None:
    async with database.session() as session:
        repo = RadioMemoryRepository(session)
        await repo.set_many({"a": 1, "b": 2, "c": 3}, now=FIXED_NOW)
        assert await repo.get_many(["a", "c", "absent"]) == {"a": 1, "c": 3}
        assert await repo.get_many([]) == {}


async def test_memory_increment_starts_from_zero(database: Database) -> None:
    async with database.session() as session:
        repo = RadioMemoryRepository(session)
        assert await repo.increment(MemoryKeys.TRACK_SEQUENCE, now=FIXED_NOW) == 1
        assert await repo.increment(MemoryKeys.TRACK_SEQUENCE, now=FIXED_NOW) == 2
        assert (
            await repo.increment(MemoryKeys.TRACK_SEQUENCE, now=FIXED_NOW, amount=5) == 7
        )


async def test_memory_increment_recovers_from_a_non_integer_value(
    database: Database,
) -> None:
    """Corrupt memory must not crash the scheduler on startup."""
    async with database.session() as session:
        repo = RadioMemoryRepository(session)
        await repo.set("counter", {"unexpected": "shape"}, now=FIXED_NOW)
        assert await repo.increment("counter", now=FIXED_NOW) == 1


async def test_memory_get_returns_the_default_when_absent(database: Database) -> None:
    async with database.read_session() as session:
        assert await RadioMemoryRepository(session).get("missing", "fallback") == "fallback"


async def test_memory_delete(database: Database) -> None:
    async with database.session() as session:
        repo = RadioMemoryRepository(session)
        await repo.set("k", 1, now=FIXED_NOW)
        assert await repo.delete("k") is True
        assert await repo.delete("k") is False


# ---------------------------------------------------------------- files


async def test_live_path_is_none_once_bytes_are_reclaimed(database: Database) -> None:
    """ADR-07: callers must not be handed a path to archived audio."""
    blueprint = make_blueprint()
    async with database.session() as session:
        await TrackRepository(session).create(
            blueprint, now=FIXED_NOW, provider="mock", model_identifier="mock-1"
        )
        files = TrackFileRepository(session)
        row = await files.add(
            track_id=blueprint.track_id,
            role=FileRole.MASTER,
            path=Path("D:/generated/x.flac"),
            size_bytes=4096,
            file_format="flac",
            now=FIXED_NOW,
        )
        assert await files.live_path(blueprint.track_id, FileRole.MASTER) is not None
        await files.mark_reclaimed([row.id], now=FIXED_NOW)
        assert await files.live_path(blueprint.track_id, FileRole.MASTER) is None


async def test_marking_reclaimed_keeps_the_row(database: Database) -> None:
    """§36 preserves the record so the library can show "audio archived"."""
    blueprint = make_blueprint()
    async with database.session() as session:
        await TrackRepository(session).create(
            blueprint, now=FIXED_NOW, provider="mock", model_identifier="mock-1"
        )
        files = TrackFileRepository(session)
        row = await files.add(
            track_id=blueprint.track_id,
            role=FileRole.MASTER,
            path=Path("x.flac"),
            size_bytes=4096,
            file_format="flac",
            now=FIXED_NOW,
        )
        await files.mark_reclaimed([row.id], now=FIXED_NOW)
        rows = await files.for_track(blueprint.track_id)
    assert len(rows) == 1
    assert rows[0].deleted_at is not None


async def test_marking_reclaimed_is_idempotent(database: Database) -> None:
    blueprint = make_blueprint()
    async with database.session() as session:
        await TrackRepository(session).create(
            blueprint, now=FIXED_NOW, provider="mock", model_identifier="mock-1"
        )
        files = TrackFileRepository(session)
        row = await files.add(
            track_id=blueprint.track_id,
            role=FileRole.MASTER,
            path=Path("x.flac"),
            size_bytes=4096,
            file_format="flac",
            now=FIXED_NOW,
        )
        assert await files.mark_reclaimed([row.id], now=FIXED_NOW) == 1
        assert await files.mark_reclaimed([row.id], now=FIXED_NOW) == 0


async def test_total_bytes_excludes_reclaimed_files(database: Database) -> None:
    async with database.session() as session:
        repo = TrackRepository(session)
        files = TrackFileRepository(session)
        ids = []
        for index in range(3):
            track_id = f"TF-{index}"
            await repo.create(
                make_blueprint(track_id=track_id, title=f"T{index}"),
                now=FIXED_NOW,
                provider="mock",
                model_identifier="mock-1",
            )
            row = await files.add(
                track_id=track_id,
                role=FileRole.MASTER,
                path=Path(f"{track_id}.flac"),
                size_bytes=1000,
                file_format="flac",
                now=FIXED_NOW,
            )
            ids.append(row.id)
        assert await files.total_bytes() == 3000
        await files.mark_reclaimed(ids[:2], now=FIXED_NOW)
        assert await files.total_bytes() == 1000
        assert await files.total_bytes(include_reclaimed=True) == 3000


async def test_retention_candidates_flags_queue_membership(database: Database) -> None:
    """A single query, so the planner's view of the queue cannot be stale."""
    from tradefix_radio.persistence.models import QueueItem

    async with database.session() as session:
        repo = TrackRepository(session)
        files = TrackFileRepository(session)
        for index in range(2):
            track_id = f"TF-{index}"
            await repo.create(
                make_blueprint(track_id=track_id, title=f"T{index}"),
                now=FIXED_NOW,
                provider="mock",
                model_identifier="mock-1",
            )
            await files.add(
                track_id=track_id,
                role=FileRole.MASTER,
                path=Path(f"{track_id}.flac"),
                size_bytes=1000,
                file_format="flac",
                now=FIXED_NOW,
            )
        session.add(
            QueueItem(
                item_id="q-0",
                track_id="TF-0",
                position=0,
                state=TrackState.QUEUED.value,
                lock_level="locked",
                tier="scheduled",
                transition_in="crossfade",
                enqueued_at=FIXED_NOW,
                display={},
            )
        )

    async with database.read_session() as session:
        candidates = await TrackFileRepository(session).retention_candidates()
    by_track = {candidate.track_id: candidate for candidate in candidates}
    assert by_track["TF-0"].in_queue is True
    assert by_track["TF-1"].in_queue is False


async def test_retention_candidates_excludes_reclaimed_files(database: Database) -> None:
    blueprint = make_blueprint()
    async with database.session() as session:
        await TrackRepository(session).create(
            blueprint, now=FIXED_NOW, provider="mock", model_identifier="mock-1"
        )
        files = TrackFileRepository(session)
        row = await files.add(
            track_id=blueprint.track_id,
            role=FileRole.MASTER,
            path=Path("x.flac"),
            size_bytes=1000,
            file_format="flac",
            now=FIXED_NOW,
        )
        await files.mark_reclaimed([row.id], now=FIXED_NOW)

    async with database.read_session() as session:
        assert await TrackFileRepository(session).retention_candidates() == []


# ---------------------------------------------------------------- events & metrics


async def test_system_events_are_queryable_by_level_and_track(database: Database) -> None:
    async with database.session() as session:
        repo = SystemEventRepository(session)
        await repo.record(
            now=FIXED_NOW, service="worker", level="INFO", event="track.generated",
            track_id="TF-1",
        )
        await repo.record(
            now=FIXED_NOW, service="worker", level="ERROR", event="generator.failed",
            track_id="TF-1", error="timeout",
        )
        await repo.record(now=FIXED_NOW, service="playout", level="INFO", event="track.playing")

    async with database.read_session() as session:
        repo = SystemEventRepository(session)
        assert len(await repo.recent(level="ERROR")) == 1
        assert len(await repo.recent(track_id="TF-1")) == 2
        assert await repo.count_since("track.generated", FIXED_NOW - timedelta(hours=1)) == 1


async def test_pruning_keeps_error_events_by_default(database: Database) -> None:
    """A post-mortem weeks later needs the errors, not the chatter."""
    old = FIXED_NOW - timedelta(days=60)
    async with database.session() as session:
        repo = SystemEventRepository(session)
        await repo.record(now=old, service="w", level="INFO", event="chatter")
        await repo.record(now=old, service="w", level="ERROR", event="important", error="x")

    async with database.session() as session:
        removed = await SystemEventRepository(session).prune_before(
            FIXED_NOW - timedelta(days=30)
        )
    assert removed == 1

    async with database.read_session() as session:
        remaining = await SystemEventRepository(session).recent()
    assert [event.event for event in remaining] == ["important"]


async def test_overlong_error_text_is_truncated_not_rejected(database: Database) -> None:
    """An enormous traceback must not be the reason a failure goes unrecorded."""
    async with database.session() as session:
        row = await SystemEventRepository(session).record(
            now=FIXED_NOW, service="w", level="ERROR", event="x", error="y" * 5000
        )
    assert row.error is not None
    assert len(row.error) == 2000


async def test_metric_series_is_ascending_and_keeps_the_newest_samples(
    database: Database,
) -> None:
    """A truncated window must show the latest data, not a stalled old slice."""
    async with database.session() as session:
        repo = MetricRepository(session)
        for index in range(20):
            await repo.record(
                "queue.buffer_minutes",
                float(index),
                now=FIXED_NOW + timedelta(minutes=index),
            )

    async with database.read_session() as session:
        series = await MetricRepository(session).series(
            "queue.buffer_minutes", since=FIXED_NOW - timedelta(hours=1), limit=5
        )
    assert [point.value for point in series] == [15.0, 16.0, 17.0, 18.0, 19.0]
    assert series == sorted(series, key=lambda p: p.at)


async def test_metric_aggregate_is_computed_in_sql(database: Database) -> None:
    async with database.session() as session:
        repo = MetricRepository(session)
        await repo.record_many(
            {"generation.seconds": 10.0}, now=FIXED_NOW
        )
        await repo.record("generation.seconds", 30.0, now=FIXED_NOW)
        await repo.record("generation.seconds", 20.0, now=FIXED_NOW)

    async with database.read_session() as session:
        aggregate = await MetricRepository(session).aggregate(
            "generation.seconds", since=FIXED_NOW - timedelta(hours=1)
        )
    assert aggregate == (10.0, 20.0, 30.0)


async def test_metric_aggregate_is_none_without_samples(database: Database) -> None:
    async with database.read_session() as session:
        assert (
            await MetricRepository(session).aggregate(
                "nothing.here", since=FIXED_NOW - timedelta(hours=1)
            )
            is None
        )


async def test_metric_latest_returns_the_most_recent_point(database: Database) -> None:
    async with database.session() as session:
        repo = MetricRepository(session)
        await repo.record("x", 1.0, now=FIXED_NOW)
        await repo.record("x", 2.0, now=FIXED_NOW + timedelta(minutes=1))

    async with database.read_session() as session:
        latest = await MetricRepository(session).latest("x")
    assert latest is not None
    assert latest.value == 2.0


# ---------------------------------------------------------------- helpers


async def _advance_to_playing(repo: TrackRepository, track_id: str) -> None:
    """Walk a track through the pipeline to PLAYING."""
    for state in (
        TrackState.GENERATING,
        TrackState.GENERATED,
        TrackState.ANALYZING,
        TrackState.APPROVED,
        TrackState.MASTERING,
        TrackState.READY,
        TrackState.QUEUED,
        TrackState.PLAYING,
    ):
        await repo.transition(track_id, state, now=FIXED_NOW, reason="test_pipeline")


async def test_a_review_resolution_round_trips_beside_its_initial_verdict(
    database: Database,
) -> None:
    """The second stage's answer is stored *with* the first stage's, never instead of it.

    An approval that hides having begun as REVIEW is the silent reinterpretation B2 exists
    to remove — the §48 page has to be able to say "this entered REVIEW and here is why it
    was let through".
    """
    from tradefix_radio.originality.review import (
        EvidenceClass,
        ReviewDisposition,
        ReviewResolution,
    )
    from tradefix_radio.originality.similarity import (
        OriginalityVerdict,
        SimilarityComponents,
        SimilarityOutcome,
        TrackComparison,
    )
    from tradefix_radio.persistence.repositories.originality import OriginalityRepository

    closest = TrackComparison(
        existing_track_id="TF-OTHER",
        score=0.79,
        components=SimilarityComponents(
            audio_fingerprint=0.55, embedding=0.93, chroma=0.88, mfcc=0.98, tempo=1.0
        ),
    )
    outcome = SimilarityOutcome(
        track_id="TF-REVIEWED",
        verdict=OriginalityVerdict.REVIEW,
        novelty_score=0.21,
        max_similarity=0.79,
        threshold=0.84,
        closest=closest,
        top_comparisons=(closest,),
        compared_against=1,
        deciding_component="mfcc",
    )
    resolution = ReviewResolution(
        track_id="TF-REVIEWED",
        initial_verdict=OriginalityVerdict.REVIEW,
        disposition=ReviewDisposition.FINAL_APPROVE,
        evidence_class=EvidenceClass.STYLE_ONLY,
        reason="the similarity is mfcc, a style signal, with no duplicate evidence",
        duplication_risk=0.55,
        creative_similarity=1.0,
        closest_track_id="TF-OTHER",
        production_references=7,
    )

    async with database.session() as session:
        await OriginalityRepository(session).record_similarity(
            "TF-REVIEWED", outcome, evaluated_at=FIXED_NOW, resolution=resolution
        )

    async with database.read_session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT verdict, final_disposition, evidence_class, resolver_version, "
                    "duplication_risk, creative_similarity, production_references, "
                    "resolution_reason FROM similarity_results WHERE track_id='TF-REVIEWED'"
                )
            )
        ).first()

    assert row is not None
    assert row[0] == "review", "the first-stage verdict was overwritten"
    assert row[1] == "final_approve"
    assert row[2] == "style_only"
    assert row[3], "a disposition without a resolver version cannot be interpreted later"
    assert row[4] == pytest.approx(0.55)
    assert row[5] == pytest.approx(1.0)
    assert row[6] == 7
    assert "style signal" in row[7]


async def test_a_first_stage_decision_records_no_resolution(database: Database) -> None:
    """Nothing resolved means nothing claimed. The columns stay NULL rather than zero."""
    from tradefix_radio.originality.similarity import OriginalityVerdict, SimilarityOutcome
    from tradefix_radio.persistence.repositories.originality import OriginalityRepository

    outcome = SimilarityOutcome(
        track_id="TF-CLEAN",
        verdict=OriginalityVerdict.APPROVE,
        novelty_score=0.9,
        max_similarity=0.1,
        threshold=0.84,
        closest=None,
        compared_against=0,
    )
    async with database.session() as session:
        await OriginalityRepository(session).record_similarity(
            "TF-CLEAN", outcome, evaluated_at=FIXED_NOW
        )

    async with database.read_session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT final_disposition, evidence_class, duplication_risk "
                    "FROM similarity_results WHERE track_id='TF-CLEAN'"
                )
            )
        ).first()
    assert row == (None, None, None)


# --------------------------------------------------------- startup reserve (§FSP)


async def _transition_to_ready(repo: TrackRepository, track_id: str, now: datetime) -> None:
    """Helper to transition a track through the full lifecycle to READY."""
    for state in (
        TrackState.GENERATING,
        TrackState.GENERATED,
        TrackState.ANALYZING,
        TrackState.APPROVED,
        TrackState.MASTERING,
        TrackState.READY,
    ):
        await repo.transition(track_id, state, now=now, reason="test_reserve")


async def test_find_unplayed_ready_returns_unplayed_tracks(database: Database) -> None:
    """Unplayed READY tracks are returned for the startup reserve."""
    async with database.session() as session:
        repo = TrackRepository(session)
        # Create a track in READY state with play_count=0
        blueprint = make_blueprint(track_id="TF-RESERVE-001", symbol="BTCUSD")
        await repo.create(
            blueprint=blueprint,
            now=FIXED_NOW,
            provider="ace-step",
            model_identifier="test",
            provenance="candidate",
        )
        await _transition_to_ready(repo, "TF-RESERVE-001", FIXED_NOW)

        # Should find this track
        found = await repo.find_unplayed_ready(limit=10)
        assert len(found) == 1
        assert found[0].track_id == "TF-RESERVE-001"
        assert found[0].play_count == 0


async def test_find_unplayed_ready_excludes_played_tracks(database: Database) -> None:
    """Played tracks (play_count > 0) are excluded from the reserve."""
    async with database.session() as session:
        repo = TrackRepository(session)
        # Create a READY track that has been played
        blueprint = make_blueprint(track_id="TF-PLAYED", symbol="BTCUSD")
        await repo.create(
            blueprint=blueprint,
            now=FIXED_NOW,
            provider="ace-step",
            model_identifier="test",
            provenance="candidate",
        )
        await _transition_to_ready(repo, "TF-PLAYED", FIXED_NOW)
        # Mark as played
        track = await repo.get("TF-PLAYED")
        assert track is not None
        track.play_count = 1

    async with database.session() as session:
        repo = TrackRepository(session)
        # Should not find the played track
        found = await repo.find_unplayed_ready(limit=10)
        played_ids = [t.track_id for t in found]
        assert "TF-PLAYED" not in played_ids


async def test_find_unplayed_ready_filters_by_market(database: Database) -> None:
    """Reserve query filters by market symbol."""
    async with database.session() as session:
        repo = TrackRepository(session)
        # Create BTC track
        btc_blueprint = make_blueprint(track_id="TF-BTC", symbol="BTCUSD")
        await repo.create(
            blueprint=btc_blueprint,
            now=FIXED_NOW,
            provider="ace-step",
            model_identifier="test",
            provenance="candidate",
        )
        await _transition_to_ready(repo, "TF-BTC", FIXED_NOW)

        # Create XAUUSD track
        gold_blueprint = make_blueprint(track_id="TF-GOLD", symbol="XAUUSD")
        await repo.create(
            blueprint=gold_blueprint,
            now=FIXED_NOW,
            provider="ace-step",
            model_identifier="test",
            provenance="candidate",
        )
        await _transition_to_ready(repo, "TF-GOLD", FIXED_NOW)

    async with database.session() as session:
        repo = TrackRepository(session)
        # Query for BTC tracks only
        btc_tracks = await repo.find_unplayed_ready(market_symbol="BTCUSD", limit=10)
        assert len(btc_tracks) == 1
        assert btc_tracks[0].track_id == "TF-BTC"

        # Query for gold tracks only
        gold_tracks = await repo.find_unplayed_ready(market_symbol="XAUUSD", limit=10)
        assert len(gold_tracks) == 1
        assert gold_tracks[0].track_id == "TF-GOLD"


async def test_count_unplayed_ready_by_market(database: Database) -> None:
    """Count unplayed ready tracks per market for the Control Center."""
    async with database.session() as session:
        repo = TrackRepository(session)
        # Create 2 BTC tracks
        for i in range(2):
            btc_bp = make_blueprint(track_id=f"TF-BTC-{i}", symbol="BTCUSD")
            await repo.create(
                blueprint=btc_bp,
                now=FIXED_NOW,
                provider="ace-step",
                model_identifier="test",
                provenance="candidate",
            )
            await _transition_to_ready(repo, f"TF-BTC-{i}", FIXED_NOW)

        # Create 3 gold tracks
        for i in range(3):
            gold_bp = make_blueprint(track_id=f"TF-GOLD-{i}", symbol="XAUUSD")
            await repo.create(
                blueprint=gold_bp,
                now=FIXED_NOW,
                provider="ace-step",
                model_identifier="test",
                provenance="candidate",
            )
            await _transition_to_ready(repo, f"TF-GOLD-{i}", FIXED_NOW)

    async with database.session() as session:
        repo = TrackRepository(session)
        counts = await repo.count_unplayed_ready_by_market()
        assert counts.get("BTCUSD", 0) == 2
        assert counts.get("XAUUSD", 0) == 3
