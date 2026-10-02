"""Shared fixtures.

Two principles here, both load-bearing for a project meant to run unattended:

**No test may read the developer's real configuration.** Every settings fixture
passes ``env_file=None`` and an explicit ``environ``, so a stray ``.env`` or a
``TRADEFIX_*`` variable in the shell cannot change a result. A suite that passes
on one machine and fails on another is worse than no suite.

**No test may touch the real data directories.** Paths are redirected into
``tmp_path``, so a test cannot delete generated audio or corrupt the station's
database — which matters especially for the retention tests, whose whole job is
deleting files.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tradefix_radio.config.loader import load_settings
from tradefix_radio.config.schema import AppSettings
from tradefix_radio.contracts.enums import (
    GenerationPriority,
    MarketDirection,
    MarketRegime,
    RunMode,
    VocalStyle,
)
from tradefix_radio.contracts.music import (
    BlueprintMarketContextV1,
    CompositionSpecV1,
    LyricsSpecV1,
    MusicBlueprintV1,
    NoveltySpecV1,
    VocalSpecV1,
)
from tradefix_radio.core.clock import UTC, VirtualClock
from tradefix_radio.monitoring.logging import reset_logging
from tradefix_radio.persistence.database import Database

#: Fixed reference instant. Every time-dependent assertion is relative to this, so
#: nothing in the suite depends on the real clock.
FIXED_NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def clean_environ() -> dict[str, str]:
    """An empty environment, so the developer's shell cannot influence a test."""
    return {}


@pytest.fixture
def settings(tmp_path: Path, clean_environ: dict[str, str]) -> AppSettings:
    """Development settings with every path redirected into ``tmp_path``."""
    return make_settings(tmp_path, clean_environ)


def make_settings(
    tmp_path: Path,
    environ: dict[str, str] | None = None,
    *,
    mode_override: RunMode | None = None,
    **overrides: Any,
) -> AppSettings:
    """Build isolated settings, merging ``overrides`` at the highest precedence.

    ``mode_override`` is separate from ``overrides`` because the mode must be known
    *before* layering, so the right ``<mode>.yaml`` overlay is chosen; passing it as
    a plain override would apply the development overlay and then relabel it.
    """
    base: dict[str, Any] = {
        "paths": {
            "root_dir": str(tmp_path),
            "data_dir": "data",
            "generated_dir": "generated",
            "emergency_dir": "emergency",
            "log_dir": "logs",
            "artwork_dir": "artwork",
            "models_dir": "models",
            "report_dir": "reports",
        },
        "database": {"url": "sqlite+aiosqlite:///test.db"},
    }
    merged = _deep_merge(base, overrides)
    return load_settings(
        env_file=None,
        environ=environ or {},
        mode=mode_override,
        overrides=merged,
    )


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        existing = out.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            out[key] = _deep_merge(existing, value)
        else:
            out[key] = value
    return out


@pytest.fixture
def virtual_clock() -> VirtualClock:
    """A deterministic clock starting at :data:`FIXED_NOW`."""
    return VirtualClock(start=FIXED_NOW)


@pytest.fixture
async def database(settings: AppSettings) -> AsyncIterator[Database]:
    """A connected database with the schema created from the models.

    ``create_all`` rather than Alembic: the migration path is verified once in
    ``tests/integration/test_migrations.py``, and running it per-test would
    dominate the suite's runtime for no additional coverage.
    """
    settings.paths.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.database)
    await db.connect()
    await db.create_all()
    try:
        yield db
    finally:
        await db.disconnect()


@pytest.fixture(autouse=True)
def _reset_logging_between_tests() -> Iterator[None]:
    """Prevent handler accumulation and context leakage across tests."""
    yield
    reset_logging()


@pytest.fixture(scope="session", autouse=True)
def _close_stray_event_loop() -> Iterator[None]:
    """Close any event loop left behind at the end of the session.

    pytest-asyncio installs a default loop for the session and does not always
    close it. On Windows that loop's self-pipe is a ``socket.socketpair()``, so the
    garbage collector emits ``ResourceWarning: unclosed event loop`` at an
    arbitrary later moment — which pytest then attributes to whichever unrelated
    test was running. Closing it here keeps the warning summary clean and, more
    importantly, keeps the suite's pass/fail independent of GC timing.
    """
    yield
    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        return
    if not loop.is_closed():
        loop.close()


# ---------------------------------------------------------------- builders


def make_blueprint(
    track_id: str = "TF-20261002-00001",
    *,
    genre: str = "uk_trap",
    secondary_genre: str | None = "dnb",
    bpm: int = 148,
    key: str = "F# minor",
    duration_seconds: int = 215,
    instrumental: bool = False,
    primary_topic: str | None = "liquidity_breakout",
    secondary_topic: str | None = "risk_management",
    regime: MarketRegime = MarketRegime.BULLISH_BREAKOUT,
    energy: float = 86.0,
    seed: int = 1234,
    title: str = "Liquidity After Midnight",
    persona_id: str | None = "tf01",
    priority: GenerationPriority = GenerationPriority.NORMAL,
) -> MusicBlueprintV1:
    """Construct a valid blueprint, overridable field by field.

    Centralised so that adding a required blueprint field breaks one builder
    instead of forty tests.
    """
    # The contract forbids a secondary genre equal to the primary. Tests vary
    # `genre` freely, so drop the default secondary rather than making every
    # caller remember to clear it.
    if secondary_genre == genre:
        secondary_genre = None
    vocal = (
        VocalSpecV1(enabled=False, style=VocalStyle.NONE, density=0.0)
        if instrumental
        else VocalSpecV1(enabled=True, style=VocalStyle.RAP, density=0.68)
    )
    lyrics = (
        LyricsSpecV1(
            enabled=False,
            primary_topic=None,
            secondary_topic=None,
            tradefix_mentions=0,
            educational_intensity=0.0,
        )
        if instrumental
        else LyricsSpecV1(
            enabled=True,
            primary_topic=primary_topic,
            secondary_topic=secondary_topic,
            tradefix_mentions=1,
            educational_intensity=0.55,
            format="full rap",
            perspective="first_person",
        )
    )
    return MusicBlueprintV1(
        track_id=track_id,
        created_at_iso=FIXED_NOW.isoformat(),
        market=BlueprintMarketContextV1(
            regime=regime,
            direction=MarketDirection.BULLISH,
            energy=energy,
            trend_strength=90.0,
            volatility=82.0,
            confidence=0.89,
            session="london_new_york_overlap",
        ),
        composition=CompositionSpecV1(
            genre=genre,
            secondary_genre=secondary_genre,
            bpm=bpm,
            key=key,
            duration_seconds=duration_seconds,
            energy=0.91,
            rhythm_density=0.86,
            bass_intensity=0.90,
            drum_intensity=0.94,
            melodic_complexity=0.62,
            structure=("intro", "verse", "hook", "verse", "hook", "outro"),
            instrumentation=("808", "hi-hats", "detuned pad"),
            mood=("tense", "driving"),
        ),
        vocal=vocal,
        lyrics=lyrics,
        novelty=NoveltySpecV1(target=0.92),
        persona_id=persona_id,
        title=title,
        priority=priority,
        creative_temperature=0.6,
        seed=seed,
        rationale=("breakout regime", "energy above 80"),
    )


__all__ = ["FIXED_NOW", "UTC", "make_blueprint", "make_settings"]
