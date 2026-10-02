"""Versioned data contracts (§90, milestone 1.3).

The exit test for 1.3 is JSON round-tripping plus rejection of forbidden extras.
Beyond that, these tests pin the *semantic* invariants that exist to stop the
station lying — above all §32/§86's rule that a price may never be presented when
the feed is not live. That rule is enforced at the contract level specifically so
it is unrepresentable rather than merely discouraged, and this is where that
claim gets verified.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from tradefix_radio.contracts.audio import (
    AudioMetricsV1,
    MasteringResultV1,
    SimilarityComponentV1,
    SimilarityResultV1,
)
from tradefix_radio.contracts.enums import (
    FeedStatus,
    GenerationPriority,
    HealthStatus,
    MarketDirection,
    MarketRegime,
    NoveltyVerdict,
    PlayoutTier,
    TradingSession,
    TransitionType,
    VocalStyle,
)
from tradefix_radio.contracts.generation import GenerationRequestV1, GenerationResultV1
from tradefix_radio.contracts.lyrics import LyricsV1
from tradefix_radio.contracts.market import MarketSnapshotV1, MarketStateV1
from tradefix_radio.contracts.music import (
    CompositionSpecV1,
    LyricsSpecV1,
    MusicBlueprintV1,
    VocalSpecV1,
)
from tradefix_radio.contracts.queue import (
    BufferHealthV1,
    NowPlayingV1,
    PlayEventV1,
    QueueItemV1,
    QueueLockLevel,
)
from tradefix_radio.core.state_machine import TrackState
from tests.conftest import FIXED_NOW, make_blueprint

UTC = timezone.utc


def make_state(
    *,
    feed_status: FeedStatus = FeedStatus.LIVE,
    price: float | None = 4120.55,
    regime: MarketRegime = MarketRegime.BULLISH_BREAKOUT,
    confidence: float = 0.89,
) -> MarketStateV1:
    return MarketStateV1(
        symbol="XAUUSD",
        timestamp=FIXED_NOW,
        regime=regime,
        direction=MarketDirection.BULLISH,
        session=TradingSession.LONDON_NEW_YORK_OVERLAP,
        feed_status=feed_status,
        energy=84.2,
        energy_velocity=1.4,
        volatility=79.4,
        trend_strength=87.1,
        momentum=74.8,
        compression=9.1,
        confidence=confidence,
        regime_age_seconds=420.0,
        data_age_seconds=0.8,
        price=price,
    )


# ---------------------------------------------------------------- round trips


def test_market_state_round_trips_through_json() -> None:
    state = make_state()
    restored = MarketStateV1.model_validate(state.to_json_dict())
    assert restored == state


def test_blueprint_round_trips_through_json() -> None:
    blueprint = make_blueprint()
    restored = MusicBlueprintV1.model_validate(blueprint.to_json_dict())
    assert restored == blueprint
    assert restored.signature() == blueprint.signature()


def test_snapshot_round_trips_and_preserves_timezone() -> None:
    snapshot = MarketSnapshotV1(
        symbol="XAUUSD", timestamp=FIXED_NOW, bid=4120.1, ask=4120.4
    )
    restored = MarketSnapshotV1.model_validate(snapshot.to_json_dict())
    assert restored.timestamp == FIXED_NOW
    assert restored.timestamp.tzinfo is not None


def test_contracts_forbid_unknown_fields() -> None:
    """A typo must be an error, not a silently dropped value (§90)."""
    payload = make_state().to_json_dict()
    payload["enrgy"] = 50.0
    with pytest.raises(ValidationError, match="Extra inputs"):
        MarketStateV1.model_validate(payload)


def test_contracts_are_immutable() -> None:
    """Contracts are facts in transit; three consumers must see the same object."""
    state = make_state()
    with pytest.raises(ValidationError):
        state.energy = 10.0  # type: ignore[misc]


def test_model_copy_is_the_sanctioned_way_to_change_a_contract() -> None:
    state = make_state()
    updated = state.model_copy(update={"energy": 10.0})
    assert state.energy == 84.2
    assert updated.energy == 10.0


# ---------------------------------------------------------------- price honesty


@pytest.mark.parametrize(
    "status", [FeedStatus.STALE, FeedStatus.DISCONNECTED, FeedStatus.SIMULATED]
)
def test_price_is_unrepresentable_when_the_feed_is_not_live(status: FeedStatus) -> None:
    """§32/§86: never present non-live data as a current price.

    Enforced in the contract rather than the UI, so no code path anywhere can
    construct a state that would mislead.
    """
    with pytest.raises(ValidationError, match="price must be None"):
        make_state(feed_status=status, price=4120.55)


@pytest.mark.parametrize(
    "status", [FeedStatus.STALE, FeedStatus.DISCONNECTED, FeedStatus.SIMULATED]
)
def test_non_live_state_without_a_price_is_valid(status: FeedStatus) -> None:
    state = make_state(feed_status=status, price=None)
    assert state.price is None


def test_only_live_feed_status_permits_price_display() -> None:
    assert FeedStatus.LIVE.is_trustworthy_for_price_display
    for status in (FeedStatus.STALE, FeedStatus.DISCONNECTED, FeedStatus.SIMULATED):
        assert not status.is_trustworthy_for_price_display


def test_unknown_regime_cannot_carry_high_confidence() -> None:
    """Claiming confidence in "we don't know" is incoherent."""
    with pytest.raises(ValidationError, match="UNKNOWN regime"):
        make_state(regime=MarketRegime.UNKNOWN, confidence=0.9)


def test_neutralised_state_makes_no_market_claims() -> None:
    """§63-E: a dead feed must produce programming that cannot be wrong."""
    neutral = make_state().neutralised()
    assert neutral.regime is MarketRegime.UNKNOWN
    assert neutral.direction is MarketDirection.NEUTRAL
    assert neutral.confidence == 0.0
    assert neutral.price is None
    assert neutral.energy == 50.0
    assert neutral.trend_strength == 0.0


def test_simulated_state_is_flagged_and_usable_for_programming() -> None:
    """Simulation must drive the real engines (§7) while staying distinguishable."""
    state = make_state(feed_status=FeedStatus.SIMULATED, price=None)
    assert state.is_simulated
    assert state.is_usable_for_programming


def test_disconnected_state_is_not_usable_for_programming() -> None:
    state = make_state(feed_status=FeedStatus.DISCONNECTED, price=None)
    assert not state.is_usable_for_programming


def test_naive_timestamps_are_rejected() -> None:
    """A naive datetime would silently corrupt session classification."""
    with pytest.raises(ValidationError, match="timezone-aware"):
        MarketSnapshotV1(
            symbol="XAUUSD", timestamp=datetime(2026, 10, 2, 12, 0, 0), bid=1.0, ask=1.1
        )


# ---------------------------------------------------------------- snapshot maths


def test_mid_and_spread_are_computed_not_stored() -> None:
    """Computed so they cannot disagree with bid/ask."""
    snapshot = MarketSnapshotV1(
        symbol="XAUUSD", timestamp=FIXED_NOW, bid=4120.00, ask=4120.40
    )
    assert snapshot.mid == pytest.approx(4120.20)
    assert snapshot.spread == pytest.approx(0.40)


def test_inverted_spread_is_rejected() -> None:
    with pytest.raises(ValidationError, match="below bid"):
        MarketSnapshotV1(symbol="XAUUSD", timestamp=FIXED_NOW, bid=4120.5, ask=4120.0)


def test_inverted_candle_is_rejected() -> None:
    with pytest.raises(ValidationError, match="below low"):
        MarketSnapshotV1(
            symbol="XAUUSD",
            timestamp=FIXED_NOW,
            bid=4120.0,
            ask=4120.2,
            open=4120.0,
            high=4119.0,
            low=4121.0,
            close=4120.1,
        )


def test_candle_open_outside_range_is_rejected() -> None:
    with pytest.raises(ValidationError, match="outside"):
        MarketSnapshotV1(
            symbol="XAUUSD",
            timestamp=FIXED_NOW,
            bid=4120.0,
            ask=4120.2,
            open=4200.0,
            high=4130.0,
            low=4110.0,
            close=4120.0,
        )


def test_tick_only_snapshot_is_valid() -> None:
    """A tick feed must not have to fabricate candles (§86)."""
    snapshot = MarketSnapshotV1(
        symbol="XAUUSD", timestamp=FIXED_NOW, bid=4120.0, ask=4120.2
    )
    assert snapshot.open is None
    assert snapshot.close is None


# ---------------------------------------------------------------- blueprint


def test_lyrics_without_vocals_is_rejected() -> None:
    """Stored metadata that lies about a track's content poisons topic history."""
    with pytest.raises(ValidationError, match="requires vocal.enabled"):
        MusicBlueprintV1.model_validate(
            {
                **make_blueprint().model_dump(),
                "vocal": {"enabled": False, "style": "none", "density": 0.0},
            }
        )


def test_instrumental_blueprint_is_coherent() -> None:
    blueprint = make_blueprint(instrumental=True)
    assert blueprint.is_instrumental
    assert not blueprint.lyrics.enabled
    assert blueprint.vocal.style is VocalStyle.NONE


def test_vocal_enabled_with_none_style_is_rejected() -> None:
    with pytest.raises(ValidationError, match="style is NONE"):
        VocalSpecV1(enabled=True, style=VocalStyle.NONE, density=0.5)


def test_disabled_vocal_must_have_zero_density() -> None:
    with pytest.raises(ValidationError, match="density must be 0"):
        VocalSpecV1(enabled=False, style=VocalStyle.NONE, density=0.4)


def test_lyrics_enabled_requires_a_primary_topic() -> None:
    with pytest.raises(ValidationError, match="primary_topic is required"):
        LyricsSpecV1(
            enabled=True,
            primary_topic=None,
            secondary_topic=None,
            tradefix_mentions=1,
            educational_intensity=0.4,
        )


def test_identical_primary_and_secondary_topics_are_rejected() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        LyricsSpecV1(
            enabled=True,
            primary_topic="discipline",
            secondary_topic="discipline",
            tradefix_mentions=0,
            educational_intensity=0.4,
        )


def test_more_than_two_tradefix_mentions_is_rejected() -> None:
    """§16 forbids brand spam; three mentions is not representable."""
    with pytest.raises(ValidationError):
        LyricsSpecV1(
            enabled=True,
            primary_topic="discipline",
            secondary_topic=None,
            tradefix_mentions=3,
            educational_intensity=0.4,
        )


def test_secondary_genre_must_differ_from_primary() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        CompositionSpecV1(
            genre="trap",
            secondary_genre="trap",
            bpm=140,
            key="F# minor",
            duration_seconds=200,
            energy=0.8,
            rhythm_density=0.8,
            bass_intensity=0.8,
            drum_intensity=0.8,
            melodic_complexity=0.5,
        )


@pytest.mark.parametrize("key", ["F# minor", "Bb major", "C dorian", "A harmonic minor"])
def test_valid_keys_are_accepted(key: str) -> None:
    spec = CompositionSpecV1(
        genre="trap",
        secondary_genre=None,
        bpm=140,
        key=key,
        duration_seconds=200,
        energy=0.8,
        rhythm_density=0.8,
        bass_intensity=0.8,
        drum_intensity=0.8,
        melodic_complexity=0.5,
    )
    assert spec.key == key


@pytest.mark.parametrize(
    "key", ["F#minor", "H minor", "F# mynor", "F#", "minor", "F# minor extra"]
)
def test_malformed_keys_are_rejected(key: str) -> None:
    """A nonsense key degrades every prompt invisibly; catch it at the boundary."""
    with pytest.raises(ValidationError):
        CompositionSpecV1(
            genre="trap",
            secondary_genre=None,
            bpm=140,
            key=key,
            duration_seconds=200,
            energy=0.8,
            rhythm_density=0.8,
            bass_intensity=0.8,
            drum_intensity=0.8,
            melodic_complexity=0.5,
        )


@pytest.mark.parametrize("bpm", [39, 221])
def test_bpm_outside_the_representable_band_is_rejected(bpm: int) -> None:
    with pytest.raises(ValidationError):
        CompositionSpecV1(
            genre="trap",
            secondary_genre=None,
            bpm=bpm,
            key="F# minor",
            duration_seconds=200,
            energy=0.8,
            rhythm_density=0.8,
            bass_intensity=0.8,
            drum_intensity=0.8,
            melodic_complexity=0.5,
        )


# ---------------------------------------------------------------- signatures


def test_signature_ignores_incidentals() -> None:
    """§11's "same blueprint" is about the creative decision, not the paperwork."""
    first = make_blueprint(track_id="TF-A", seed=1, title="One")
    second = make_blueprint(track_id="TF-B", seed=999, title="Two")
    assert first.signature() == second.signature()


def test_signature_changes_with_genre() -> None:
    a = make_blueprint(genre="uk_trap")
    b = make_blueprint(genre="deep_house", secondary_genre="dnb")
    assert a.signature() != b.signature()


def test_signature_changes_with_topic() -> None:
    a = make_blueprint(primary_topic="liquidity_breakout")
    b = make_blueprint(primary_topic="position_sizing")
    assert a.signature() != b.signature()


def test_signature_buckets_small_bpm_changes_apart() -> None:
    """BPM is part of the creative identity, so it must not be bucketed away."""
    assert make_blueprint(bpm=148).signature() != make_blueprint(bpm=149).signature()


def test_signature_is_stable_across_processes() -> None:
    """Deterministic hashing: the worker computes it, the API reads it."""
    blueprint = make_blueprint()
    assert blueprint.signature() == blueprint.signature()
    assert len(blueprint.signature()) == 64


def test_signature_ignores_market_context() -> None:
    """Two identical creative decisions minutes apart are the same decision."""
    a = make_blueprint(regime=MarketRegime.BULLISH_BREAKOUT, energy=86.0)
    b = make_blueprint(regime=MarketRegime.BULLISH_TREND, energy=71.0)
    assert a.signature() == b.signature()


def test_signature_ignores_priority_and_temperature() -> None:
    a = make_blueprint(priority=GenerationPriority.CRITICAL)
    b = make_blueprint(priority=GenerationPriority.EXPERIMENTAL)
    assert a.signature() == b.signature()


# ---------------------------------------------------------------- lyrics


def test_lyric_normalisation_strips_tags_case_and_punctuation() -> None:
    lyrics = LyricsV1(
        track_id="TF-1",
        text="[Verse]\nTrade Fix!  Risk FIRST,\n[Hook]\nrisk first",
        primary_topic="risk_management",
        format="full rap",
        perspective="first_person",
        tradefix_mentions=1,
        educational_intensity=0.4,
    )
    assert lyrics.normalised_text == "trade fix risk first risk first"


def test_identical_lyrics_hash_identically_despite_formatting() -> None:
    """§17 duplicate detection must survive trivial reformatting."""
    first = LyricsV1(
        track_id="TF-1",
        text="[Verse]\nStops are the plan.\n",
        primary_topic="stop_losses",
        format="educational bars",
        perspective="mentor",
        tradefix_mentions=0,
        educational_intensity=0.6,
    )
    second = LyricsV1(
        track_id="TF-2",
        text="Stops   are the PLAN!!!",
        primary_topic="stop_losses",
        format="educational bars",
        perspective="mentor",
        tradefix_mentions=0,
        educational_intensity=0.6,
    )
    assert first.content_hash == second.content_hash


def test_different_lyrics_hash_differently() -> None:
    first = LyricsV1(
        track_id="TF-1",
        text="Stops are the plan",
        primary_topic="stop_losses",
        format="educational bars",
        perspective="mentor",
        tradefix_mentions=0,
        educational_intensity=0.6,
    )
    second = LyricsV1(
        track_id="TF-2",
        text="Position size is the plan",
        primary_topic="position_sizing",
        format="educational bars",
        perspective="mentor",
        tradefix_mentions=0,
        educational_intensity=0.6,
    )
    assert first.content_hash != second.content_hash


def test_lyric_shingles_overlap_for_reworded_text() -> None:
    """Near-duplicate detection needs partial overlap, not all-or-nothing."""
    base = LyricsV1(
        track_id="TF-1",
        text="patience is the edge when the market goes quiet before london opens",
        primary_topic="patience",
        format="storytelling",
        perspective="observer",
        tradefix_mentions=0,
        educational_intensity=0.3,
    )
    similar = LyricsV1(
        track_id="TF-2",
        text="patience is the edge when the market goes quiet before new york opens",
        primary_topic="patience",
        format="storytelling",
        perspective="observer",
        tradefix_mentions=0,
        educational_intensity=0.3,
    )
    shared = base.shingles() & similar.shingles()
    assert shared, "reworded couplets must still share shingles"
    union = base.shingles() | similar.shingles()
    assert len(shared) / len(union) > 0.4


def test_unrelated_lyrics_share_few_shingles() -> None:
    """Common trading phrases must not create false duplicate positives."""
    first = LyricsV1(
        track_id="TF-1",
        text="discipline over everything when the drawdown starts to bite my account",
        primary_topic="discipline",
        format="motivational",
        perspective="first_person",
        tradefix_mentions=0,
        educational_intensity=0.2,
    )
    second = LyricsV1(
        track_id="TF-2",
        text="london volume arrives and the range finally breaks into clean expansion",
        primary_topic="session_highs",
        format="storytelling",
        perspective="observer",
        tradefix_mentions=0,
        educational_intensity=0.2,
    )
    union = first.shingles() | second.shingles()
    assert len(first.shingles() & second.shingles()) / len(union) < 0.1


def test_blank_lyrics_are_rejected() -> None:
    with pytest.raises(ValidationError):
        LyricsV1(
            track_id="TF-1",
            text="   \n  ",
            primary_topic="x",
            format="f",
            perspective="p",
            tradefix_mentions=0,
            educational_intensity=0.0,
        )


# ---------------------------------------------------------------- generation


def test_generation_request_rejects_mismatched_lyrics() -> None:
    """Pairing a blueprint with another track's lyrics would corrupt topic history."""
    blueprint = make_blueprint(track_id="TF-A")
    wrong = LyricsV1(
        track_id="TF-B",
        text="words",
        primary_topic="x",
        format="f",
        perspective="p",
        tradefix_mentions=0,
        educational_intensity=0.0,
    )
    with pytest.raises(ValidationError, match="does not match"):
        GenerationRequestV1(
            job_id="job-1",
            track_id="TF-A",
            blueprint=blueprint,
            lyrics=wrong,
            timeout_seconds=300.0,
            attempt=1,
        )


def test_generation_request_requires_lyrics_when_the_blueprint_wants_them() -> None:
    blueprint = make_blueprint(track_id="TF-A")
    with pytest.raises(ValidationError, match="requires lyrics"):
        GenerationRequestV1(
            job_id="job-1",
            track_id="TF-A",
            blueprint=blueprint,
            lyrics=None,
            timeout_seconds=300.0,
            attempt=1,
        )


def test_instrumental_request_needs_no_lyrics() -> None:
    blueprint = make_blueprint(track_id="TF-A", instrumental=True)
    request = GenerationRequestV1(
        job_id="job-1",
        track_id="TF-A",
        blueprint=blueprint,
        lyrics=None,
        timeout_seconds=300.0,
        attempt=1,
    )
    assert request.lyrics is None


def test_generation_request_must_have_a_timeout() -> None:
    """§19: generation without a deadline is not representable."""
    blueprint = make_blueprint(instrumental=True)
    with pytest.raises(ValidationError):
        GenerationRequestV1(
            job_id="job-1",
            track_id=blueprint.track_id,
            blueprint=blueprint,
            timeout_seconds=0.0,
            attempt=1,
        )


def test_realtime_factor_signals_sustainable_throughput() -> None:
    """Above 1.0 the buffer can grow; below it, starvation is inevitable (§93)."""
    fast = GenerationResultV1(
        job_id="j",
        track_id="t",
        audio_path="x.wav",
        provider="mock",
        model_identifier="m",
        seed_used=1,
        generation_seconds=10.0,
        audio_duration_seconds=200.0,
        sample_rate=44_100,
        channels=2,
        completed_at=FIXED_NOW,
    )
    slow = fast.model_copy(update={"generation_seconds": 400.0})
    assert fast.realtime_factor == pytest.approx(20.0)
    assert slow.realtime_factor < 1.0


# ---------------------------------------------------------------- queue


def test_buffer_health_separates_ready_from_in_flight() -> None:
    """Counting optimistic work as buffer is how a station walks into silence."""
    buffer = BufferHealthV1(
        minutes_ready=8.0,
        minutes_in_flight=40.0,
        minimum_minutes=20.0,
        target_minutes=45.0,
        maximum_minutes=90.0,
        ready_count=2,
        in_flight_count=11,
    )
    assert buffer.is_below_minimum
    assert not buffer.is_critical
    assert buffer.fill_ratio == pytest.approx(8.0 / 45.0)


def test_buffer_health_critical_below_five_minutes() -> None:
    buffer = BufferHealthV1(
        minutes_ready=4.0,
        minutes_in_flight=0.0,
        minimum_minutes=20.0,
        target_minutes=45.0,
        maximum_minutes=90.0,
        ready_count=1,
        in_flight_count=0,
    )
    assert buffer.is_critical


def test_buffer_fill_ratio_is_capped_at_one() -> None:
    buffer = BufferHealthV1(
        minutes_ready=120.0,
        minutes_in_flight=0.0,
        minimum_minutes=20.0,
        target_minutes=45.0,
        maximum_minutes=90.0,
        ready_count=30,
        in_flight_count=0,
    )
    assert buffer.fill_ratio == 1.0


def test_hard_cut_must_have_zero_crossfade() -> None:
    with pytest.raises(ValidationError, match="HARD_CUT"):
        QueueItemV1(
            item_id="q1",
            track_id="TF-1",
            position=0,
            state=TrackState.QUEUED,
            title="t",
            genre="trap",
            bpm=140,
            duration_seconds=200.0,
            is_instrumental=False,
            regime_at_generation="normal_range",
            transition_in=TransitionType.HARD_CUT,
            transition_seconds=3.0,
            enqueued_at=FIXED_NOW,
        )


def test_locked_and_operator_pinned_levels_are_protected() -> None:
    """§28/§44: automatic replanning must not touch these."""
    assert QueueLockLevel.LOCKED.is_protected
    assert QueueLockLevel.OPERATOR_PINNED.is_protected
    assert not QueueLockLevel.REPLACEABLE.is_protected
    assert not QueueLockLevel.SEMI_LOCKED.is_protected


def test_completed_play_event_requires_an_end_time() -> None:
    """§75: a completed airing without an end time is not a real record."""
    with pytest.raises(ValidationError, match="ended_at"):
        PlayEventV1(
            track_id="TF-1",
            started_at=FIXED_NOW,
            ended_at=None,
            played_seconds=200.0,
            completed=True,
            tier=PlayoutTier.SCHEDULED,
            transition_in=TransitionType.CROSSFADE,
        )


def test_incomplete_play_event_may_lack_an_end_time() -> None:
    event = PlayEventV1(
        track_id="TF-1",
        started_at=FIXED_NOW,
        played_seconds=42.0,
        completed=False,
        tier=PlayoutTier.SCHEDULED,
        transition_in=TransitionType.CROSSFADE,
        end_reason="sink_failure",
    )
    assert not event.completed


def test_now_playing_progress_and_remaining() -> None:
    now_playing = NowPlayingV1(
        track_id="TF-1",
        title="Liquidity After Midnight",
        persona="TF-03",
        genre="uk_trap",
        bpm=148,
        key="F# minor",
        duration_seconds=200.0,
        elapsed_seconds=50.0,
        is_instrumental=False,
        vocal_style="rap",
        regime_at_generation="bullish_breakout",
        energy_at_generation=84.0,
        generation_model="mock-1",
        novelty_score=0.93,
        tier=PlayoutTier.SCHEDULED,
        started_at=FIXED_NOW,
    )
    assert now_playing.progress == pytest.approx(0.25)
    assert now_playing.remaining_seconds == pytest.approx(150.0)


def test_now_playing_progress_is_capped_when_elapsed_overshoots() -> None:
    """Clock drift must not produce 103% progress on the §42 card."""
    now_playing = NowPlayingV1(
        track_id="TF-1",
        title="t",
        genre="trap",
        bpm=140,
        key="F# minor",
        duration_seconds=200.0,
        elapsed_seconds=260.0,
        is_instrumental=True,
        vocal_style="none",
        regime_at_generation="quiet",
        energy_at_generation=10.0,
        generation_model="mock-1",
        novelty_score=0.9,
        tier=PlayoutTier.PROCEDURAL,
        started_at=FIXED_NOW,
    )
    assert now_playing.progress == 1.0
    assert now_playing.remaining_seconds == 0.0


# ---------------------------------------------------------------- audio


def _metrics(**overrides: float) -> AudioMetricsV1:
    base: dict[str, float | int] = {
        "duration_seconds": 200.0,
        "sample_rate": 44_100,
        "channels": 2,
        "peak_dbfs": -1.5,
        "true_peak_dbtp": -1.0,
        "loudness_lufs": -14.0,
        "loudness_range_lu": 7.0,
        "rms_dbfs": -18.0,
        "dc_offset": 0.001,
        "clipped_sample_ratio": 0.0,
        "silence_ratio": 0.02,
        "leading_silence_seconds": 0.1,
        "trailing_silence_seconds": 0.4,
    }
    base.update(overrides)
    return AudioMetricsV1.model_validate(base)


def test_mastering_reports_loudness_error_and_dynamics_retained() -> None:
    """§25 warns against destroying dynamics; the ratio makes it measurable."""
    result = MasteringResultV1(
        track_id="TF-1",
        source_path="raw.wav",
        output_path="master.flac",
        stages_applied=("trim", "loudnorm", "limiter"),
        before=_metrics(loudness_lufs=-21.0, loudness_range_lu=10.0),
        after=_metrics(loudness_lufs=-13.8, loudness_range_lu=7.0),
        target_lufs=-14.0,
        true_peak_ceiling_dbtp=-1.0,
        mastered_at=FIXED_NOW,
        elapsed_seconds=2.4,
    )
    assert result.loudness_error_lu == pytest.approx(0.2)
    assert result.dynamics_retained_ratio == pytest.approx(0.7)


def test_similarity_component_weight_is_zero_when_unavailable() -> None:
    """A missing input must not silently inflate novelty (§22)."""
    available = SimilarityComponentV1(
        name="embedding", similarity=0.9, weight=0.26, available=True
    )
    missing = SimilarityComponentV1(
        name="embedding", similarity=0.9, weight=0.26, available=False
    )
    assert available.weighted == pytest.approx(0.234)
    assert missing.weighted == 0.0


def test_similarity_explanation_matches_the_brief_format() -> None:
    """§48 requires a rejection to say which component, which track, which threshold."""
    result = SimilarityResultV1(
        track_id="TF-20261002-00100",
        verdict=NoveltyVerdict.REJECT,
        novelty_score=0.09,
        max_similarity=0.91,
        threshold=0.84,
        closest_track_id="TF-20261001-00917",
        deciding_component="embedding",
        components=(
            SimilarityComponentV1(name="embedding", similarity=0.91, weight=0.26),
        ),
        compared_against=4_312,
        evaluated_at=FIXED_NOW,
    )
    explanation = result.explanation
    assert "0.91" in explanation
    assert "TF-20261001-00917" in explanation
    assert "0.84" in explanation
    assert "embedding" in explanation


def test_similarity_explanation_avoids_overclaiming_on_approval() -> None:
    """§21/§86: never imply a score proves originality."""
    result = SimilarityResultV1(
        track_id="TF-1",
        verdict=NoveltyVerdict.APPROVE,
        novelty_score=0.95,
        max_similarity=0.21,
        threshold=0.84,
        compared_against=100,
        evaluated_at=FIXED_NOW,
    )
    explanation = result.explanation.lower()
    assert "internal similarity" in explanation
    for forbidden in ("copyright", "unique", "original", "proven", "guarantee"):
        assert forbidden not in explanation


# ---------------------------------------------------------------- enums


def test_health_severity_ordering_lets_the_worst_component_win() -> None:
    assert HealthStatus.HEALTHY.severity < HealthStatus.UNKNOWN.severity
    assert HealthStatus.UNKNOWN.severity < HealthStatus.RECOVERING.severity
    assert HealthStatus.RECOVERING.severity < HealthStatus.DEGRADED.severity
    assert HealthStatus.DEGRADED.severity < HealthStatus.CRITICAL.severity


def test_generation_priority_ordering_and_experimentation_gate() -> None:
    """§94: artistic risk falls as operational risk rises."""
    ranks = [p.rank for p in GenerationPriority]
    assert ranks == sorted(ranks)
    assert not GenerationPriority.CRITICAL.allows_experimentation
    assert not GenerationPriority.HIGH.allows_experimentation
    assert GenerationPriority.NORMAL.allows_experimentation
    assert GenerationPriority.EXPERIMENTAL.allows_experimentation


def test_directional_regimes_are_exactly_the_four_trend_and_breakout_states() -> None:
    """§14 forbids implying direction where none is established."""
    directional = {regime for regime in MarketRegime if regime.is_directional}
    assert directional == {
        MarketRegime.BULLISH_BREAKOUT,
        MarketRegime.BEARISH_BREAKOUT,
        MarketRegime.BULLISH_TREND,
        MarketRegime.BEARISH_TREND,
    }


def test_quiet_and_compression_regimes_are_not_high_energy() -> None:
    assert not MarketRegime.QUIET.is_high_energy
    assert not MarketRegime.COMPRESSION.is_high_energy
    assert MarketRegime.EXTREME_VOLATILITY.is_high_energy


def test_all_fourteen_regimes_from_the_brief_exist() -> None:
    assert len(MarketRegime) == 14


def test_play_event_rejects_end_before_start() -> None:
    with pytest.raises(ValidationError, match="precedes"):
        QueueItemV1(
            item_id="q1",
            track_id="TF-1",
            position=0,
            state=TrackState.PLAYED,
            title="t",
            genre="trap",
            bpm=140,
            duration_seconds=200.0,
            is_instrumental=False,
            regime_at_generation="normal_range",
            enqueued_at=FIXED_NOW,
            started_at=FIXED_NOW,
            finished_at=FIXED_NOW - timedelta(seconds=10),
        )
