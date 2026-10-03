"""MusicDirector (§1, §8, §9, §11, milestones 3.2–3.8).

§1 is the whole brief: "THE MARKET COMPOSES THE RADIO." It also forbids the obvious
implementation — "do not hard-code these as simplistic if/else rules. Build a weighted,
configurable music-director system."

So the tests here cannot check "regime X always gives genre Y"; a system that satisfied
that assertion would be the if/else §1 rules out. What they check instead is **statistical
influence**: that a quiet market and a violent market produce distinguishable distributions,
that every blueprint is internally coherent, and that §11's rules actually bind. The
single-decision tests are about validity; the bulk tests are about character.
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import datetime, timedelta

import pytest

from tradefix_radio.config.schema import AppSettings
from tradefix_radio.contracts.enums import (
    FeedStatus,
    MarketDirection,
    MarketRegime,
    TradingSession,
)
from tradefix_radio.contracts.market import MarketStateV1
from tradefix_radio.contracts.queue import BufferHealthV1
from tradefix_radio.core.clock import UTC
from tradefix_radio.director.history import HistoryEntry, ProgrammingHistory
from tradefix_radio.director.library import ContentLibrary, load_content_library
from tradefix_radio.director.music_director import (
    MAX_ENERGY_DISTANCE,
    DirectorDecision,
    MusicDirector,
)
from tradefix_radio.director.selection import WeightedSelector
from tradefix_radio.director.titles import normalise_title

from tests.unit.test_library import CONFIG_DIR

MOMENT = datetime(2026, 10, 2, 13, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def library() -> ContentLibrary:
    return load_content_library(config_dir=CONFIG_DIR)


def director(library: ContentLibrary, seed: int = 5) -> MusicDirector:
    return MusicDirector(
        AppSettings(),
        library,
        selector=WeightedSelector(random.Random(seed)),
    )


def state(
    *,
    regime: MarketRegime = MarketRegime.NORMAL_RANGE,
    energy: float = 50.0,
    direction: MarketDirection = MarketDirection.NEUTRAL,
    session: TradingSession = TradingSession.LONDON,
    velocity: float = 0.0,
    now: datetime = MOMENT,
    symbol: str = "XAUUSD",
) -> MarketStateV1:
    return MarketStateV1(
        symbol=symbol,
        timestamp=now,
        regime=regime,
        direction=direction,
        session=session,
        feed_status=FeedStatus.SIMULATED,
        energy=energy,
        energy_velocity=velocity,
        volatility=energy,
        trend_strength=70.0 if direction is not MarketDirection.NEUTRAL else 12.0,
        momentum=50.0,
        compression=80.0 if regime is MarketRegime.COMPRESSION else 12.0,
        confidence=0.3 if regime is MarketRegime.UNKNOWN else 0.82,
        regime_age_seconds=900.0,
        data_age_seconds=1.0,
    )


def buffer(fill: float = 0.9) -> BufferHealthV1:
    return BufferHealthV1(
        minutes_ready=45.0 * fill,
        minutes_in_flight=6.0,
        minimum_minutes=20.0,
        target_minutes=45.0,
        maximum_minutes=90.0,
        ready_count=max(1, int(12 * fill)),
        in_flight_count=2,
    )


#: A spread of market conditions, cycled by :func:`run`.
#:
#: Most bulk tests want a *realistic* sequence rather than one frozen market: §11's rules
#: interact with regime changes, and a single market state would make the energy planner's
#: ramp the dominant effect. Tests that specifically need the adversarial constant market
#: pass ``market=`` instead.
_SPREAD: tuple[MarketStateV1, ...] = (
    state(regime=MarketRegime.QUIET, energy=10.0, session=TradingSession.ASIAN),
    state(regime=MarketRegime.NORMAL_RANGE, energy=45.0),
    state(
        regime=MarketRegime.BULLISH_TREND,
        energy=66.0,
        direction=MarketDirection.BULLISH,
        session=TradingSession.LONDON_NEW_YORK_OVERLAP,
    ),
    state(regime=MarketRegime.COMPRESSION, energy=32.0),
    state(
        regime=MarketRegime.BEARISH_BREAKOUT,
        energy=88.0,
        direction=MarketDirection.BEARISH,
        session=TradingSession.NEW_YORK,
    ),
    state(
        regime=MarketRegime.LOW_VOLATILITY_RANGE,
        energy=24.0,
        session=TradingSession.NEW_YORK_LATE,
    ),
    state(
        regime=MarketRegime.HIGH_VOLATILITY_RANGE,
        energy=92.0,
        session=TradingSession.NEW_YORK,
    ),
    state(
        regime=MarketRegime.BEARISH_TREND,
        energy=58.0,
        direction=MarketDirection.BEARISH,
        session=TradingSession.LONDON,
    ),
)


def run(
    engine: MusicDirector,
    count: int,
    *,
    market: MarketStateV1 | None = None,
    fill: float = 0.9,
    alternate: tuple[MarketStateV1, ...] = (),
) -> list[DirectorDecision]:
    """Drive ``count`` decisions, maintaining history the way the scheduler does.

    Returns the decisions rather than the history entries. A ``HistoryEntry`` records
    ``energy_at_generation`` as the **market** energy, which is correct for §11's purposes
    but is not the station energy the director selected against — asserting on it reported
    a genre as 27 points out of band when it was in fact 2 points out. Decisions carry
    both, so tests can name the one they mean.
    """
    decisions: list[DirectorDecision] = []
    entries: list[HistoryEntry] = []
    signatures: set[str] = set()
    titles: set[str] = set()
    moment = MOMENT
    for index in range(count):
        current = alternate[index % len(alternate)] if alternate else (market or state())
        decision = engine.create_blueprint(
            track_id=f"TF-{index:05d}",
            state=current.model_copy(update={"timestamp": moment}),
            history=ProgrammingHistory(entries),
            buffer=buffer(fill),
            now=moment,
            capacity_ratio=1.4,
            used_titles=tuple(titles),
            recent_titles=tuple(entry.track_id for entry in entries[:40]),
            used_signatures=frozenset(signatures),
        )
        decisions.append(decision)
        blueprint = decision.blueprint
        composition = blueprint.composition
        signatures.add(blueprint.signature())
        titles.add(normalise_title(blueprint.title))
        entries.insert(
            0,
            HistoryEntry(
                track_id=blueprint.track_id,
                genre=composition.genre,
                secondary_genre=composition.secondary_genre,
                bpm=composition.bpm,
                musical_key=composition.key,
                duration_seconds=float(composition.duration_seconds),
                is_instrumental=blueprint.is_instrumental,
                vocal_style=blueprint.vocal.style.value,
                primary_topic=blueprint.lyrics.primary_topic,
                secondary_topic=blueprint.lyrics.secondary_topic,
                persona_id=blueprint.persona_id,
                blueprint_signature=blueprint.signature(),
                energy_at_generation=blueprint.market.energy,
                regime_at_generation=current.regime.value,
                created_at=moment,
                played_at=moment,
            ),
        )
        moment += timedelta(minutes=4)
    return decisions


def aired(decisions: list[DirectorDecision]) -> list[HistoryEntry]:
    """The decisions as history entries, oldest first — the order a listener heard."""
    return [
        HistoryEntry(
            track_id=d.blueprint.track_id,
            genre=d.blueprint.composition.genre,
            secondary_genre=d.blueprint.composition.secondary_genre,
            bpm=d.blueprint.composition.bpm,
            musical_key=d.blueprint.composition.key,
            duration_seconds=float(d.blueprint.composition.duration_seconds),
            is_instrumental=d.blueprint.is_instrumental,
            vocal_style=d.blueprint.vocal.style.value,
            primary_topic=d.blueprint.lyrics.primary_topic,
            secondary_topic=d.blueprint.lyrics.secondary_topic,
            persona_id=d.blueprint.persona_id,
            blueprint_signature=d.blueprint.signature(),
            energy_at_generation=d.blueprint.market.energy,
            regime_at_generation=d.blueprint.market.regime.value,
            created_at=MOMENT,
        )
        for d in decisions
    ]


def station_energy(decision: DirectorDecision) -> float:
    """The energy the director actually selected against, on the 0–100 scale."""
    return decision.blueprint.composition.energy * 100.0


# ---------------------------------------------------------------- one blueprint


def test_a_single_decision_produces_a_valid_blueprint(library: ContentLibrary) -> None:
    decision = director(library).create_blueprint(
        track_id="TF-00001",
        state=state(),
        history=ProgrammingHistory([]),
        buffer=buffer(),
        now=MOMENT,
    )
    blueprint = decision.blueprint
    assert blueprint.track_id == "TF-00001"
    assert blueprint.title
    assert blueprint.composition.genre in library.genre_keys
    assert 40 <= blueprint.composition.bpm <= 220
    assert blueprint.composition.duration_seconds > 0
    assert blueprint.composition.structure
    assert blueprint.signature()


def test_the_blueprint_round_trips_through_its_own_json(library: ContentLibrary) -> None:
    """A blueprint that cannot be re-read is a blueprint that cannot be persisted."""
    blueprint = director(library).create_blueprint(
        track_id="TF-00001",
        state=state(),
        history=ProgrammingHistory([]),
        buffer=buffer(),
        now=MOMENT,
    ).blueprint
    restored = type(blueprint).model_validate_json(blueprint.model_dump_json())
    assert restored.signature() == blueprint.signature()


def test_the_decision_explains_itself(library: ContentLibrary) -> None:
    """§47/§48: the control centre has to be able to show why, not just what."""
    decision = director(library).create_blueprint(
        track_id="TF-00001",
        state=state(regime=MarketRegime.BULLISH_BREAKOUT, energy=88.0),
        history=ProgrammingHistory([]),
        buffer=buffer(),
        now=MOMENT,
    )
    explanation = decision.explain()
    assert decision.blueprint.composition.genre in explanation
    assert "bullish_breakout" in explanation
    assert decision.selections, "no candidate tables recorded for the explainability pane"


def test_the_same_seed_produces_the_same_blueprint(library: ContentLibrary) -> None:
    """Required for §64 endurance replays to be diagnosable at all."""
    first = director(library, seed=42).create_blueprint(
        track_id="TF-1", state=state(), history=ProgrammingHistory([]),
        buffer=buffer(), now=MOMENT,
    )
    second = director(library, seed=42).create_blueprint(
        track_id="TF-1", state=state(), history=ProgrammingHistory([]),
        buffer=buffer(), now=MOMENT,
    )
    assert first.blueprint.signature() == second.blueprint.signature()
    assert first.blueprint.title == second.blueprint.title


def test_different_seeds_produce_different_blueprints(library: ContentLibrary) -> None:
    signatures = {
        director(library, seed=seed)
        .create_blueprint(
            track_id="TF-1", state=state(), history=ProgrammingHistory([]),
            buffer=buffer(), now=MOMENT,
        )
        .blueprint.signature()
        for seed in range(12)
    }
    assert len(signatures) > 6, "the director is nearly deterministic across seeds"


# ---------------------------------------------------------------- §8 coherence


def test_every_blueprint_is_internally_coherent(library: ContentLibrary) -> None:
    """The §8 contract's coherence rules, checked across a long run.

    These are the invariants a §19 provider call depends on: an instrumental track with a
    lyric topic, or a BPM outside its genre's band, produces a prompt that contradicts
    itself.
    """
    engine = director(library)
    decisions = run(engine, 200, alternate=_SPREAD)
    assert len(decisions) == 200
    for entry in aired(decisions):
        genre = library.genre(entry.genre)
        assert genre.bpm.contains(float(entry.bpm)), (
            f"{entry.genre} BPM {entry.bpm} outside {genre.bpm.min}-{genre.bpm.max}"
        )
        if entry.is_instrumental:
            assert entry.primary_topic is None
            assert entry.secondary_topic is None
        else:
            assert entry.primary_topic is not None
            assert entry.persona_id is not None
        if entry.secondary_genre is not None:
            assert entry.secondary_genre != entry.genre
            assert entry.secondary_genre in library.genre_keys
        if entry.secondary_topic is not None:
            assert entry.secondary_topic != entry.primary_topic


def test_personas_only_record_in_their_own_genres(library: ContentLibrary) -> None:
    """A persona credited on a genre it does not record in is a §100 contradiction."""
    for entry in aired(run(director(library), 200, alternate=_SPREAD)):
        if entry.persona_id:
            assert entry.genre in library.persona(entry.persona_id).genres


def test_vocal_styles_come_from_the_genre(library: ContentLibrary) -> None:
    for entry in aired(run(director(library), 200, alternate=_SPREAD)):
        if not entry.is_instrumental:
            styles = {style.value for style in library.genre(entry.genre).vocal_styles}
            assert entry.vocal_style in styles, f"{entry.genre}: {entry.vocal_style}"


def test_a_quiet_market_and_a_violent_market_sound_different(
    library: ContentLibrary,
) -> None:
    """§1, stated as the property it actually is.

    Not "quiet implies lofi" — that assertion would be satisfied by the if/else §1
    forbids, and would break the moment the library changed. What must hold is that the
    two markets produce *distinguishable distributions*: different genres, slower BPM,
    lower station energy.
    """
    quiet = aired(run(
        director(library, seed=3),
        120,
        market=state(regime=MarketRegime.QUIET, energy=8.0, session=TradingSession.ASIAN),
    ))
    violent = aired(run(
        director(library, seed=3),
        120,
        market=state(
            regime=MarketRegime.HIGH_VOLATILITY_RANGE,
            energy=94.0,
            session=TradingSession.NEW_YORK,
        ),
    ))

    quiet_bpm = sum(entry.bpm for entry in quiet) / len(quiet)
    violent_bpm = sum(entry.bpm for entry in violent) / len(violent)
    assert violent_bpm > quiet_bpm + 20, f"{quiet_bpm:.0f} vs {violent_bpm:.0f} BPM"

    # Measured as a distribution, not as set membership. Set overlap treats one play the
    # same as twenty, and some overlap is correct: a genre with a wide band (cinematic
    # spans 30–100) legitimately belongs in both, and §98's peak relief deliberately dips
    # station energy during a sustained violent stretch. What must differ is where the
    # programming *sits*.
    def mean_band_centre(entries: list[HistoryEntry]) -> float:
        return sum(library.genre(e.genre).energy.centre for e in entries) / len(entries)

    assert mean_band_centre(violent) > mean_band_centre(quiet) + 25, (
        f"genre bands centre at {mean_band_centre(quiet):.0f} vs "
        f"{mean_band_centre(violent):.0f} — the market is barely influencing genre choice"
    )

    # And the staples of each must be disjoint: whatever the tails share, a violent market
    # must not be *mostly* playing the same records as a dead one.
    quiet_top = {genre for genre, _ in Counter(e.genre for e in quiet).most_common(5)}
    violent_top = {genre for genre, _ in Counter(e.genre for e in violent).most_common(5)}
    assert not quiet_top & violent_top, f"shared staples: {sorted(quiet_top & violent_top)}"


def test_a_compressed_market_is_not_simply_a_quiet_one(library: ContentLibrary) -> None:
    """§29's buildup has its own character; collapsing it into "quiet" loses §1's point."""
    compressed = aired(run(
        director(library, seed=4),
        120,
        market=state(regime=MarketRegime.COMPRESSION, energy=34.0),
    ))
    quiet = aired(run(
        director(library, seed=4),
        120,
        market=state(regime=MarketRegime.QUIET, energy=10.0),
    ))
    assert {entry.genre for entry in compressed} != {entry.genre for entry in quiet}


def test_station_energy_tracks_market_energy_across_regimes(
    library: ContentLibrary,
) -> None:
    """The §1 mapping must be monotone even though no rule states it directly.

    It emerges from ``band_fit`` over each genre's energy band plus §98's planner. Being
    emergent is exactly why it needs testing: nothing in the code asserts it.
    """
    means = []
    for energy in (5.0, 25.0, 50.0, 75.0, 95.0):
        decisions = run(director(library, seed=8), 80, market=state(energy=energy))
        settled = decisions[10:]  # drop the §98 ramp at the start of the run
        means.append(sum(station_energy(d) for d in settled) / len(settled))
    assert means == sorted(means), f"station energy not monotone in market energy: {means}"


def test_an_energy_mismatch_beyond_the_hard_limit_never_happens(
    library: ContentLibrary,
) -> None:
    """172 BPM drum & bass in a dead-quiet market, which the first bulk run produced.

    Weighted selection alone could not prevent it: a sufficiently unlucky sample from a
    long tail still picks the tail. §9's hard-veto layer is what makes it impossible.
    """
    for market_energy in (3.0, 50.0, 97.0):
        for decision in run(
            director(library, seed=6), 80, market=state(energy=market_energy)
        ):
            key = decision.blueprint.composition.genre
            genre = library.genre(key)
            distance = genre.energy.distance_to(station_energy(decision))
            assert distance <= MAX_ENERGY_DISTANCE, (
                f"{key} (band {genre.energy.min}-{genre.energy.max}) chosen at "
                f"station energy {station_energy(decision):.0f}"
            )


def test_an_unknown_regime_still_produces_programming(library: ContentLibrary) -> None:
    """§63-E: a dead feed must make the station unremarkable, not silent."""
    entries = aired(run(
        director(library),
        40,
        market=state(regime=MarketRegime.UNKNOWN, energy=50.0),
    ))
    assert len(entries) == 40
    assert len({entry.genre for entry in entries}) > 3


# ---------------------------------------------------------------- §11 enforcement


def test_no_genre_runs_more_than_twice_consecutively(library: ContentLibrary) -> None:
    """§11: "same genre maximum 2 consecutive".

    The rule that silently never fired: the constraint was typed over strings while the
    selection ran over genre *objects*, so every comparison was ``object != str`` — always
    true, so nothing was ever vetoed. mypy found it; this keeps it found.
    """
    sequence = aired(run(director(library), 300, alternate=_SPREAD))
    worst = 1
    streak = 1
    for index in range(1, len(sequence)):
        streak = streak + 1 if sequence[index].genre == sequence[index - 1].genre else 1
        worst = max(worst, streak)
    assert worst <= 2, f"a genre ran {worst} times consecutively"


def test_consecutive_tracks_never_share_a_blueprint_signature(
    library: ContentLibrary,
) -> None:
    entries = aired(run(director(library), 300, alternate=_SPREAD))
    signatures = [entry.blueprint_signature for entry in entries]
    assert len(set(signatures)) == len(signatures), "a blueprint signature repeated"


def test_titles_never_repeat_across_a_long_run(library: ContentLibrary) -> None:
    """§99's title history, exercised through the director rather than directly."""
    entries = aired(run(director(library), 300, alternate=_SPREAD))
    assert len(entries) == 300


def test_programming_does_not_collapse_onto_one_genre(library: ContentLibrary) -> None:
    """§81-18: no single genre may dominate.

    Run under a *constant* market, which is the adversarial case — a varying market
    produces variety for free, so a constant one is the only honest test of §11.
    """
    entries = aired(run(director(library), 300, market=state(energy=60.0)))
    counts = Counter(entry.genre for entry in entries)
    share = counts.most_common(1)[0][1] / len(entries)
    assert share < 0.35, f"top genre {counts.most_common(1)[0][0]} at {share:.0%}"
    assert len(counts) >= 4, f"only {len(counts)} genres under a constant market"


def test_keys_and_topics_rotate(library: ContentLibrary) -> None:
    entries = aired(run(director(library), 300, alternate=_SPREAD))
    assert len({entry.musical_key for entry in entries}) >= 6
    topics = {entry.primary_topic for entry in entries if entry.primary_topic}
    assert len(topics) >= 12, f"only {len(topics)} topics across 300 tracks"


def test_the_instrumental_ratio_stays_near_its_configuration(
    library: ContentLibrary,
) -> None:
    """§16. Measured because it drifted to 67% when the genre factor was mis-centred."""
    entries = aired(run(director(library), 300, alternate=_SPREAD))
    ratio = sum(1 for entry in entries if entry.is_instrumental) / len(entries)
    assert 0.2 <= ratio <= 0.6, f"instrumental ratio {ratio:.0%}"


def test_personas_rotate_rather_than_one_voice_dominating(
    library: ContentLibrary,
) -> None:
    entries = aired(run(director(library), 300, alternate=_SPREAD))
    counts = Counter(entry.persona_id for entry in entries if entry.persona_id)
    assert len(counts) >= 4
    assert counts.most_common(1)[0][1] / sum(counts.values()) < 0.45


# ---------------------------------------------------------------- §94 coupling


def test_a_critical_buffer_suppresses_experimental_genres(
    library: ContentLibrary,
) -> None:
    """§94: artistic risk falls when operational risk rises, end to end."""
    starving = aired(run(director(library), 120, market=state(energy=55.0), fill=0.05))
    experimental = [
        entry.genre
        for entry in starving
        if library.genre(entry.genre).experimental
    ]
    assert not experimental, f"experimental genres on a starving queue: {experimental}"


def test_a_healthy_buffer_eventually_reaches_experimental_genres(
    library: ContentLibrary,
) -> None:
    """The reward half. Without this the §94 gate could simply be "never"."""
    healthy = aired(run(director(library), 400, market=state(energy=55.0), fill=1.0))
    assert any(library.genre(entry.genre).experimental for entry in healthy), (
        "no experimental genre in 400 decisions on a full queue; the §94 reward is dead"
    )


def test_a_thin_buffer_shortens_tracks(library: ContentLibrary) -> None:
    """Shorter tracks mean more decision points and faster recovery."""
    thin = aired(run(director(library), 100, market=state(energy=55.0), fill=0.1))
    full = aired(run(director(library), 100, market=state(energy=55.0), fill=1.0))
    thin_mean = sum(entry.duration_seconds for entry in thin) / len(thin)
    full_mean = sum(entry.duration_seconds for entry in full) / len(full)
    assert thin_mean < full_mean, f"{thin_mean:.0f}s vs {full_mean:.0f}s"


def test_an_unhealthy_provider_does_not_stop_the_director(
    library: ContentLibrary,
) -> None:
    """§86: one model crash must not stop the radio.

    The director still has to produce a blueprint — conservative, but a blueprint — so
    the fallback tiers have something to work with.
    """
    decision = director(library).create_blueprint(
        track_id="TF-1",
        state=state(),
        history=ProgrammingHistory([]),
        buffer=buffer(0.02),
        now=MOMENT,
        provider_healthy=False,
        consecutive_failures=5,
    )
    assert decision.blueprint.composition.genre in library.genre_keys
    assert not library.genre(decision.blueprint.composition.genre).experimental


# ---------------------------------------------------------------- §11 reporting


def test_relaxations_are_reported_rather_than_hidden(library: ContentLibrary) -> None:
    """The honest answer to "is §11 enforced?" is a rate, not a boolean.

    A narrow genre's handful of permitted BPMs cannot always satisfy "no BPM within ±4 of
    the last four tracks". What matters is that relaxation is *visible*, so the §3.12
    report can say how often it happens instead of implying it never does.
    """
    engine = director(library)
    relaxed_any = False
    entries: list[HistoryEntry] = []
    moment = MOMENT
    for index in range(120):
        decision = engine.create_blueprint(
            track_id=f"TF-{index}",
            state=state(energy=55.0, now=moment),
            history=ProgrammingHistory(entries),
            buffer=buffer(),
            now=moment,
        )
        if decision.relaxed_constraints:
            relaxed_any = True
            # Every reported name must be a real rule, not a stale label.
            for name in decision.relaxed_constraints:
                assert name and not name.startswith("_")
        blueprint = decision.blueprint
        entries.insert(
            0,
            HistoryEntry(
                track_id=blueprint.track_id,
                genre=blueprint.composition.genre,
                bpm=blueprint.composition.bpm,
                musical_key=blueprint.composition.key,
                duration_seconds=float(blueprint.composition.duration_seconds),
                is_instrumental=blueprint.is_instrumental,
                vocal_style=blueprint.vocal.style.value,
                blueprint_signature=blueprint.signature(),
                energy_at_generation=blueprint.market.energy,
                regime_at_generation="normal_range",
                created_at=moment,
                played_at=moment,
                primary_topic=blueprint.lyrics.primary_topic,
                persona_id=blueprint.persona_id,
            ),
        )
        moment += timedelta(minutes=4)
    assert relaxed_any, (
        "no constraint relaxed in 120 decisions under a constant market — either the "
        "rules are not binding or relaxation is not being reported"
    )


def test_last_decision_is_exposed_for_the_api(library: ContentLibrary) -> None:
    engine = director(library)
    assert engine.last_decision is None
    engine.create_blueprint(
        track_id="TF-1", state=state(), history=ProgrammingHistory([]),
        buffer=buffer(), now=MOMENT,
    )
    assert engine.last_decision is not None
    assert engine.last_decision.blueprint.track_id == "TF-1"


# ------------------------------------------------- market scoping (routing V1)
#
# The requirement these cover: "Do NOT allow a BTCUSD song to describe itself as a live
# Gold market track." Scoping is enforced as a filter rather than a weight, so these are
# bulk tests — a weighting bug would still pass a single-decision check now and then, and
# "now and then" is precisely the failure mode.


def _topics(decisions: list[DirectorDecision]) -> set[str]:
    return {
        decision.blueprint.lyrics.primary_topic
        for decision in decisions
        if decision.blueprint.lyrics.primary_topic
    }


def test_bitcoin_tracks_never_reach_a_gold_scoped_topic(
    library: ContentLibrary,
) -> None:
    gold_only = {
        key
        for key, topic in library.topics.items()
        if topic.markets and not topic.suits_market("BTCUSD")
    }
    assert gold_only, "the fixture library has no market-scoped topics to exclude"

    engine = director(library)
    decisions = run(engine, 80, market=state(symbol="BTCUSD"))
    leaked = _topics(decisions) & gold_only
    assert not leaked, f"gold-scoped topics surfaced over Bitcoin: {sorted(leaked)}"


def test_gold_tracks_never_reach_a_bitcoin_scoped_topic(
    library: ContentLibrary,
) -> None:
    btc_only = {
        key
        for key, topic in library.topics.items()
        if topic.markets and not topic.suits_market("XAUUSD")
    }
    assert btc_only, "the fixture library has no Bitcoin-scoped topics to exclude"

    engine = director(library)
    decisions = run(engine, 80, market=state(symbol="XAUUSD"))
    leaked = _topics(decisions) & btc_only
    assert not leaked, f"Bitcoin-scoped topics surfaced over gold: {sorted(leaked)}"


def test_bitcoin_tracks_do_reach_the_bitcoin_topics(library: ContentLibrary) -> None:
    """The filter must not merely exclude — the BTC catalogue has to be reachable.

    A filter that removed the gold topics but never selected a Bitcoin one would pass the
    exclusion tests above while leaving the station with nothing market-specific to say.
    """
    btc_only = {
        key
        for key, topic in library.topics.items()
        if topic.markets and not topic.suits_market("XAUUSD")
    }
    engine = director(library)
    decisions = run(engine, 80, market=state(symbol="BTCUSD"))
    assert _topics(decisions) & btc_only, "no Bitcoin-scoped topic was ever selected"


def test_the_blueprint_records_the_market_it_was_planned_against(
    library: ContentLibrary,
) -> None:
    """A queued track keeps its own symbol after the station has moved on."""
    engine = director(library)
    gold = engine.create_blueprint(
        track_id="TF-G", state=state(symbol="XAUUSD"), history=ProgrammingHistory([]),
        buffer=buffer(), now=MOMENT,
    ).blueprint
    bitcoin = engine.create_blueprint(
        track_id="TF-B", state=state(symbol="BTCUSD"), history=ProgrammingHistory([]),
        buffer=buffer(), now=MOMENT,
    ).blueprint
    assert gold.market.symbol == "XAUUSD"
    assert bitcoin.market.symbol == "BTCUSD"
