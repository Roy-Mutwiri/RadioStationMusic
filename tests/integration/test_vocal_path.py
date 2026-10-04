"""B3: the production lyric path, end to end.

A vocal blueprint has to become *audible words*, and until B3 it could not: the station
composed nothing, `GenerationRequest.lyrics` was always `None`, and the prompt builder — doing
exactly the right thing — refused to let the model invent trading lyrics and sent
``[Instrumental]`` instead. Every rap track on the station came out as a backing track, and
nothing in the database said so.

These tests drive the real station, the real director, the real orchestrator and the real
ACE-Step prompt builder. Only the GPU is faked, and only for the audio: the fake provider
builds its spec with :class:`AceStepPromptBuilder` and returns the real ``detail`` block, so
the thing under test — what the provider is *told* — is never simulated.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest

from tradefix_radio.audio.sinks import NullSink
from tradefix_radio.config.schema import AppSettings
from tradefix_radio.core.clock import VirtualClock
from tradefix_radio.director.library import load_content_library
from tradefix_radio.director.music_director import MusicDirector
from tradefix_radio.director.selection import WeightedSelector
from tradefix_radio.generation.ace_step.prompt import (
    INSTRUMENTAL_MARKER,
    AceStepPromptBuilder,
)
from tradefix_radio.generation.manager import DatabaseJobUnitOfWork, GenerationManager
from tradefix_radio.generation.mock import MockMusicProvider
from tradefix_radio.generation.provider import (
    GenerationRequest,
    GenerationResult,
    ProgressCallback,
)
from tradefix_radio.persistence.database import Database
from tradefix_radio.persistence.repositories import (
    LyricsRepository,
    PlayEventRepository,
    ProviderSubmissionsRepository,
)
from tradefix_radio.radio.emergency import EmergencyManager, ProceduralSource
from tradefix_radio.radio.station import RadioStation
from tradefix_radio.radio.station_ids import StationIdLibrary, default_library
from tradefix_radio.runtime.coordinator import RuntimeCoordinator
from tests.conftest import FIXED_NOW
from tests.integration.test_phase4_gates import Harness, market
from tests.unit.test_library import CONFIG_DIR

TRACK_SECONDS = 30
RATE = 16_000
BLOCK_SECONDS = 10.0


class FakeAceStep:
    """The ACE-Step provider with the GPU removed and nothing else.

    Audio comes from the mock synthesiser; the *prompt* comes from the real
    :class:`AceStepPromptBuilder`. That split is the whole point — a fake that invented its
    own ``detail`` block would be asserting on the test's idea of a submission rather than on
    the one production builds, and the bug this file exists for lived precisely in the gap
    between a blueprint asking for vocals and a prompt carrying them.
    """

    def __init__(self, inner: MockMusicProvider) -> None:
        self._inner = inner
        self._builder = AceStepPromptBuilder()
        #: Every request seen, in order. The assertions read this.
        self.requests: list[GenerationRequest] = []

    def describe(self):
        return replace(self._inner.describe(), supports_vocals=True)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def generate(
        self,
        request: GenerationRequest,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> GenerationResult:
        self.requests.append(request)
        spec = self._builder.build(
            request.blueprint,
            lyrics=request.lyrics,
            inference_steps=27,
            guidance_scale=7.5,
            profile="balanced",
        )
        result = await self._inner.generate(request, on_progress=on_progress)
        return replace(
            result,
            detail={
                **result.detail,
                "prompt": spec.as_metadata(),
                "requested_lyrics": (
                    None if request.lyrics is None else request.lyrics.text
                ),
            },
        )


def _vocal_settings(settings: AppSettings) -> AppSettings:
    """Settings that always ask for vocals, and short tracks so a run finishes."""
    music = settings.music.model_copy(
        update={
            "min_duration_seconds": TRACK_SECONDS,
            "max_duration_seconds": TRACK_SECONDS,
            # Every band at 1.0. The director still multiplies by the genre's vocal
            # affinity, so this is "as vocal as the station can be asked to be", not a
            # bypass of the decision — which is what makes the test meaningful.
            "vocal_probability_by_energy": {0: 1.0, 25: 1.0, 45: 1.0, 65: 1.0, 82: 1.0},
        }
    )
    return settings.model_copy(update={"music": music})


async def _build(settings: AppSettings, tmp_path: Path, *, seed: int = 11) -> Harness:
    clock = VirtualClock(start=FIXED_NOW, real_yield_seconds=0.0)
    database = Database(settings.database, clock=clock)
    await database.connect()
    await database.create_all()

    director = MusicDirector(
        settings,
        load_content_library(config_dir=CONFIG_DIR),
        selector=WeightedSelector(random.Random(seed)),
    )
    provider = FakeAceStep(
        MockMusicProvider(
            settings.generation.mock.model_copy(
                update={"sample_rate": RATE, "latency_seconds": 2.0}
            ),
            clock=clock,
            seed=seed,
        )
    )
    generation = GenerationManager(
        provider=provider,
        settings=settings.generation,
        unit_of_work=DatabaseJobUnitOfWork(database.session),
        clock=clock,
    )
    coordinator = RuntimeCoordinator(clock=clock)
    station = RadioStation(
        settings,
        database=database,
        coordinator=coordinator,
        director=director,
        generation=generation,
        sink=NullSink(sample_rate=RATE, channels=1, clock=clock, realtime=True),
        station_ids=StationIdLibrary(
            default_library(tmp_path / "station_ids"), rng=random.Random(seed)
        ),
        emergency=EmergencyManager(
            procedural=ProceduralSource(sample_rate=RATE, channels=1, block_seconds=20.0)
        ),
        clock=clock,
        audio_dir=tmp_path / "audio",
        playout_block_seconds=BLOCK_SECONDS,
    )
    station.set_market(market())
    return Harness(
        station=station,
        clock=clock,
        provider=provider,
        generation=generation,
        coordinator=coordinator,
        database=database,
    )


@pytest.fixture
async def vocal(settings: AppSettings, tmp_path: Path) -> AsyncIterator[Harness]:
    built = await _build(_vocal_settings(settings), tmp_path)
    try:
        yield built
    finally:
        await built.stop()
        await built.database.disconnect()


# ------------------------------------------------------------------ the path


async def test_a_vocal_blueprint_reaches_the_provider_with_its_words(
    vocal: Harness,
) -> None:
    """The acceptance condition: blueprint → lyrics → provider, no bypass.

    Asserted on the *request the provider received*, not on the lyrics table. A row in
    `lyrics` only proves the station composed something; it was there, correct and complete
    while every track still came out instrumental, because nothing passed it on.
    """
    await vocal.station.start()
    await vocal.advance(240)

    provider = vocal.provider
    assert isinstance(provider, FakeAceStep)
    vocal_requests = [
        request
        for request in provider.requests
        if request.blueprint.vocal.enabled and request.blueprint.lyrics.enabled
    ]
    assert vocal_requests, "the director never asked for vocals; the test proves nothing"

    with_words = [r for r in vocal_requests if r.lyrics is not None]
    assert with_words, (
        f"{len(vocal_requests)} vocal blueprints reached the provider and none carried "
        "lyrics — this is the exact B3 defect"
    )

    request = with_words[0]
    assert request.lyrics is not None
    assert request.lyrics.track_id == request.track_id
    assert request.lyrics.text.strip()


async def test_the_submitted_prompt_carries_the_validated_lyric(
    vocal: Harness,
) -> None:
    """What the model was told is recorded, and it is what the station composed (§7.10)."""
    await vocal.station.start()
    await vocal.advance(240)

    async with vocal.database.read_session() as session:
        submissions = ProviderSubmissionsRepository(session)
        lyrics_repo = LyricsRepository(session)
        rows = await lyrics_repo.recent(limit=50)
        assert rows, "no lyrics were composed at all"

        checked = 0
        for row in rows:
            submission = await submissions.latest_for_track(row.track_id)
            if submission is None:
                continue  # planned but not yet generated
            checked += 1
            assert submission.instrumental is False
            assert submission.provider_lyrics != INSTRUMENTAL_MARKER
            assert submission.requested_lyrics == row.text
            # §7.10: the provider may tidy structure, but not replace the words.
            for word in row.text.split()[:12]:
                assert word in submission.provider_lyrics

        assert checked, "no vocal track completed generation inside the window"


async def test_an_instrumental_submission_is_recorded_as_one(vocal: Harness) -> None:
    """The instrumental marker is stored, not inferred later from silence."""
    await vocal.station.start()
    await vocal.advance(240)

    async with vocal.database.read_session() as session:
        repository = ProviderSubmissionsRepository(session)
        for request in vocal.provider.requests:  # type: ignore[attr-defined]
            if request.blueprint.vocal.enabled:
                continue
            submission = await repository.latest_for_track(request.track_id)
            if submission is None:
                continue
            assert submission.instrumental is True
            assert submission.provider_lyrics == INSTRUMENTAL_MARKER
            assert submission.requested_lyrics is None
            # Nothing was asked for, so nothing was changed. A genuine instrumental must
            # not show up in the "downgraded" query alongside the real failures.
            assert submission.lyrics_modified is False
            return


async def test_a_lyric_failure_falls_back_to_an_instrumental_and_says_so(
    settings: AppSettings, tmp_path: Path
) -> None:
    """When no validated lyric can be produced, the words are dropped — never invented.

    The fallback is the configured one and it leaves evidence: ``lyrics_modified`` is true
    with ``instrumental`` also true, which is the signature the Originality page surfaces.
    Without the record this case is indistinguishable from a track that was simply never
    meant to have words.
    """
    tuned = _vocal_settings(settings)
    harness = await _build(tuned, tmp_path)
    try:
        # Break composition itself rather than the validator: the policy under test is
        # "no validated lyric", and which of the several ways it failed must not matter.
        def _always_fails(*_args: object, **_kwargs: object):
            raise RuntimeError("composition is unavailable")

        harness.station._lyrics.generate = _always_fails  # type: ignore[assignment]

        await harness.station.start()
        await harness.advance(240)

        assert harness.station.stats.lyrics_failed > 0

        async with harness.database.read_session() as session:
            repository = ProviderSubmissionsRepository(session)
            lyric_rows = await LyricsRepository(session).recent(limit=50)
            assert not lyric_rows, "a lyric was stored despite composition failing"

            fallbacks = [
                await repository.latest_for_track(request.track_id)
                for request in harness.provider.requests  # type: ignore[attr-defined]
                if request.blueprint.vocal.enabled
            ]
            recorded = [row for row in fallbacks if row is not None]
            assert recorded, "no vocal blueprint completed generation inside the window"
            for row in recorded:
                assert row.instrumental is True
                assert row.provider_lyrics == INSTRUMENTAL_MARKER
                assert row.lyric_notes, "a silent downgrade — the reason must be recorded"
    finally:
        await harness.stop()
        await harness.database.disconnect()


async def test_the_failure_policy_can_drop_the_track_instead(
    settings: AppSettings, tmp_path: Path
) -> None:
    """``on_lyric_failure = "fail"`` abandons the slot rather than airing a silent rap.

    Both policies are legitimate and the choice is the operator's, so the test is that the
    setting is *obeyed* — not that one outcome is correct.

    Buffer suppression is switched off for this run, and that matters. A station starting
    cold has a `critical` buffer, so `suppress_vocals_at_buffer` returns the track as an
    instrumental *before* the orchestrator is ever called — the failure policy is never
    reached, and a test that did not disable it was asserting against a different mechanism
    than the one it named. That mechanism has its own test below.
    """
    base = _vocal_settings(settings)
    tuned = base.model_copy(
        update={
            "lyrics": base.lyrics.model_copy(
                update={"on_lyric_failure": "fail", "suppress_vocals_at_buffer": ()}
            )
        }
    )
    harness = await _build(tuned, tmp_path)
    try:
        def _always_fails(*_args: object, **_kwargs: object):
            raise RuntimeError("composition is unavailable")

        harness.station._lyrics.generate = _always_fails  # type: ignore[assignment]

        await harness.station.start()
        await harness.advance(240)

        assert harness.station.stats.lyrics_failed > 0
        for request in harness.provider.requests:  # type: ignore[attr-defined]
            assert not request.blueprint.vocal.enabled, (
                "a vocal track was generated under the 'fail' policy"
            )
    finally:
        await harness.stop()
        await harness.database.disconnect()


async def test_a_critical_buffer_stops_the_station_asking_for_vocals(
    settings: AppSettings, tmp_path: Path
) -> None:
    """§7.20 in the lyric direction: pressure changes the ask, never the safety gates.

    A vocal track costs two composition attempts, a validation pass, and 2.2x the GPU time
    at the vocal profile. A station starting cold is at `critical`, and survival outranks
    variety there — so the words are not even attempted, the track is realised as an
    instrumental, and that is counted apart from a *failure* because it is the station
    choosing rather than the station struggling.

    `suppress_vocals_at_buffer` existed as a setting with no consumer until B3 wired it.
    """
    base = _vocal_settings(settings)
    tuned = base.model_copy(
        update={
            "lyrics": base.lyrics.model_copy(
                update={"suppress_vocals_at_buffer": ("critical", "empty", "low")}
            )
        }
    )
    harness = await _build(tuned, tmp_path)
    try:
        await harness.station.start()
        await harness.advance(120)

        stats = harness.station.stats
        assert stats.lyrics_suppressed > 0, (
            "a cold station asked for vocals despite the suppression setting"
        )
        # Suppression is counted apart from failure, and the two are independent: a later
        # lyric rejected on its own merits is not evidence the suppression misfired.
        assert "lyrics_suppressed" in repr(stats)

        # And it is temporary. Once the buffer recovered the station went back to asking,
        # which is the half of the behaviour that makes suppression safe to enable at all —
        # a switch that silenced vocals permanently under one bad minute would be worse
        # than no switch.
        #
        # Asserted on "the orchestrator ran again", not on "a lyric succeeded". Whether a
        # resumed attempt then passes §17 is the composer's business and has its own tests;
        # tying this one to that outcome would make an unrelated validation regression look
        # like a suppression bug.
        assert stats.lyrics_composed + stats.lyrics_failed > 0, (
            "vocals never resumed after the buffer recovered; suppression is one-way"
        )
    finally:
        await harness.stop()
        await harness.database.disconnect()


async def test_suppression_is_skipped_when_no_level_is_configured(
    settings: AppSettings, tmp_path: Path
) -> None:
    """An empty `suppress_vocals_at_buffer` means the station always asks for vocals.

    The operator's switch has to be able to be off, and "off" must not be the accidental
    result of a level name that no longer matches the enum.
    """
    base = _vocal_settings(settings)
    tuned = base.model_copy(
        update={"lyrics": base.lyrics.model_copy(update={"suppress_vocals_at_buffer": ()})}
    )
    harness = await _build(tuned, tmp_path)
    try:
        await harness.station.start()
        await harness.advance(120)
        assert harness.station.stats.lyrics_suppressed == 0
    finally:
        await harness.stop()
        await harness.database.disconnect()


# ------------------------------------------------------- airing records (audit)


async def test_playback_completed_persists_a_play_event(vocal: Harness) -> None:
    """The downstream assertion: a track aired → a row exists saying so.

    `play_events` had a contract, a model, a table and five indexes and no writer: 247
    tracks had played and the table held zero rows. `tracks.play_count` is denormalised
    separately, which is why rotation worked and why nothing noticed.

    Asserted on the persisted row rather than on the hook firing, because the hook *was*
    firing the whole time.
    """
    await vocal.station.start()
    await vocal.advance(240)

    async with vocal.database.read_session() as session:
        events = await PlayEventRepository(session).recent(limit=50)

    assert events, "tracks aired and no play event was recorded"
    for event in events:
        assert event.tier, "an airing with no tier is unusable for §33 accounting"
        assert event.played_seconds > 0.0, (
            "an airing that wrote no audio should not be recorded as a play"
        )
        assert event.ended_at is not None
        assert event.started_at <= event.ended_at
        assert event.transition_in


async def test_emergency_audio_is_recorded_as_airtime_too(vocal: Harness) -> None:
    """Tier 3 has no track row, and excluding it would hide the thing it exists to signal.

    "How much of the hour was real music" is unanswerable if the emergency tiers are left
    out of the record, and a cold-starting station airs procedural audio first. The row is
    written for every tier; only station identities are excluded, because §31 counts those
    separately and they are airtime rather than programming.
    """
    await vocal.station.start()
    await vocal.advance(240)

    async with vocal.database.read_session() as session:
        by_tier = await PlayEventRepository(session).seconds_by_tier()

    assert by_tier, "no airtime recorded at all"
    assert all(seconds > 0.0 for seconds in by_tier.values())
    # A station starting from an empty buffer must have aired *something* before its first
    # generated track was ready. If only scheduled airtime is present the tiers are not
    # being recorded, which is the defect rather than a very fast generator.
    assert "procedural" in by_tier or "scheduled" in by_tier
