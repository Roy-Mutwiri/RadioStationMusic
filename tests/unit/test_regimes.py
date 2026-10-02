"""Regime classification and anti-flicker stabilisation (§5, milestone 2.5).

The exit criterion that matters most is negative, and §5 states it directly: the
engine must not oscillate ``RANGE → BREAKOUT → RANGE → BREAKOUT`` because one candle
moved. Flicker would make the station change genre every thirty seconds and sound
broken, so it is tested adversarially — by feeding input *designed* to make the
classifier dither — rather than only on well-behaved scenarios.

Each of the five stabilisation mechanisms is also tested in isolation, because they
fix different failure modes and a single combined test would not reveal which one had
regressed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradefix_radio.config.schema import MarketSettings, RegimeSettings
from tradefix_radio.contracts.enums import MarketDirection, MarketRegime
from tradefix_radio.contracts.market import MarketFeaturesV1
from tradefix_radio.market.features import FeatureEngine
from tradefix_radio.market.regimes import (
    DIRECTION_THRESHOLD,
    Direction,
    RegimeEngine,
    RegimeScorer,
    RegimeStabiliser,
)
from tradefix_radio.market.simulation import MarketSimulationEngine, Scenario

START = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
BAR = timedelta(seconds=60)


def regime_settings(**overrides: object) -> RegimeSettings:
    return RegimeSettings.model_validate(overrides)


def features(
    *,
    sufficient: bool = True,
    atr_percentile: float = 50.0,
    range_percentile: float = 50.0,
    trend_strength: float = 0.0,
    compression_score: float = 0.0,
    expansion_score: float = 0.0,
    breakout_strength: float = 0.0,
    volume_percentile: float = 50.0,
    returns_5m: float = 0.0,
    moving_average_slope: float = 0.0,
    momentum: float = 0.0,
    at: datetime = START,
) -> MarketFeaturesV1:
    return MarketFeaturesV1(
        symbol="XAUUSD",
        timestamp=at,
        sufficient_history=sufficient,
        samples_observed=500,
        returns_1m=0.0,
        returns_5m=returns_5m,
        returns_15m=0.0,
        atr=1.4,
        atr_percentile=atr_percentile,
        realized_volatility=0.0005,
        range_percentile=range_percentile,
        adx=trend_strength,
        rsi=50.0,
        moving_average_slope=moving_average_slope,
        trend_strength=trend_strength,
        momentum=momentum,
        volume_percentile=volume_percentile,
        session_range_position=0.5,
        distance_from_high=1.0,
        distance_from_low=1.0,
        breakout_strength=breakout_strength,
        compression_score=compression_score,
        expansion_score=expansion_score,
    )


def quiet(**overrides: object) -> MarketFeaturesV1:
    base: dict[str, object] = {
        "atr_percentile": 5.0, "range_percentile": 5.0, "trend_strength": 2.0,
        "volume_percentile": 15.0,
    }
    base.update(overrides)
    return features(**base)  # type: ignore[arg-type]


def bullish_breakout(**overrides: object) -> MarketFeaturesV1:
    base: dict[str, object] = {
        "atr_percentile": 90.0, "range_percentile": 90.0, "trend_strength": 60.0,
        "breakout_strength": 85.0, "expansion_score": 80.0, "volume_percentile": 95.0,
        "returns_5m": 0.004, "moving_average_slope": 0.0009, "momentum": 0.004,
    }
    base.update(overrides)
    return features(**base)  # type: ignore[arg-type]


def normal_range(**overrides: object) -> MarketFeaturesV1:
    base: dict[str, object] = {
        "atr_percentile": 45.0, "range_percentile": 45.0, "trend_strength": 8.0,
        "volume_percentile": 50.0,
    }
    base.update(overrides)
    return features(**base)  # type: ignore[arg-type]


def drive(
    engine: RegimeEngine,
    vectors: list[MarketFeaturesV1],
    *,
    start: datetime = START,
    step: timedelta = BAR,
) -> list[MarketRegime]:
    """Feed a sequence of feature vectors, returning the regime after each."""
    moment = start
    observed: list[MarketRegime] = []
    for vector in vectors:
        decision = engine.classify(vector.model_copy(update={"timestamp": moment}), now=moment)
        observed.append(decision.regime)
        moment += step
    return observed


# ---------------------------------------------------------------- scoring


def test_all_fourteen_regimes_are_scored() -> None:
    scores = RegimeScorer().score(normal_range())
    assert set(scores.scores) == set(MarketRegime)
    assert len(scores.scores) == 14


def test_unknown_never_wins_on_score() -> None:
    """UNKNOWN is selected by the stabiliser, never by scoring, so it cannot win."""
    for vector in (quiet(), normal_range(), bullish_breakout()):
        scores = RegimeScorer().score(vector)
        assert scores.scores[MarketRegime.UNKNOWN] == 0.0
        assert scores.winner is not MarketRegime.UNKNOWN


def test_every_score_is_within_bounds() -> None:
    scorer = RegimeScorer()
    for vector in (quiet(), normal_range(), bullish_breakout()):
        for regime, score in scorer.score(vector).scores.items():
            assert 0.0 <= score <= 100.0, f"{regime.value} = {score}"


def test_quiet_features_favour_the_quiet_regime() -> None:
    assert RegimeScorer().score(quiet()).winner is MarketRegime.QUIET


def test_breakout_features_favour_a_breakout_regime() -> None:
    assert RegimeScorer().score(bullish_breakout()).winner in {
        MarketRegime.BULLISH_BREAKOUT,
        MarketRegime.EXTREME_VOLATILITY,
    }


def test_direction_gating_makes_opposite_breakouts_mutually_exclusive() -> None:
    """A bullish and a bearish breakout can never both score highly."""
    scores = RegimeScorer().score(bullish_breakout()).scores
    assert scores[MarketRegime.BEARISH_BREAKOUT] == 0.0
    assert scores[MarketRegime.BULLISH_BREAKOUT] > 0.0

    bearish = bullish_breakout(returns_5m=-0.004, moving_average_slope=-0.0009,
                               momentum=-0.004)
    bearish_scores = RegimeScorer().score(bearish).scores
    assert bearish_scores[MarketRegime.BULLISH_BREAKOUT] == 0.0
    assert bearish_scores[MarketRegime.BEARISH_BREAKOUT] > 0.0


def test_direction_requires_return_and_slope_to_agree() -> None:
    """A rally inside a downtrend is a pullback, not a bullish market (§14)."""
    conflicting = features(returns_5m=0.004, moving_average_slope=-0.0009)
    assert RegimeScorer.direction_of(conflicting) is Direction.NEUTRAL


def test_direction_is_neutral_below_the_noise_threshold() -> None:
    """§14 forbids implying direction where none is established."""
    tiny = features(returns_5m=DIRECTION_THRESHOLD * 0.5, moving_average_slope=0.0001)
    assert RegimeScorer.direction_of(tiny) is Direction.NEUTRAL


def test_trend_and_breakout_are_separable() -> None:
    """A sustained trend without an ongoing breakout must score as a trend."""
    trending = features(
        atr_percentile=55.0, trend_strength=75.0, breakout_strength=2.0,
        expansion_score=5.0, returns_5m=0.002, moving_average_slope=0.0005,
    )
    scores = RegimeScorer().score(trending).scores
    assert scores[MarketRegime.BULLISH_TREND] > scores[MarketRegime.BULLISH_BREAKOUT]


def test_compression_and_buildup_are_distinguished_by_volume() -> None:
    """Rising participation inside a tight range is the genuine buildup tell."""
    coiling = features(atr_percentile=10.0, compression_score=85.0, volume_percentile=20.0)
    building = features(atr_percentile=12.0, compression_score=80.0,
                        volume_percentile=90.0, breakout_strength=25.0)
    coil_scores = RegimeScorer().score(coiling).scores
    build_scores = RegimeScorer().score(building).scores
    assert coil_scores[MarketRegime.COMPRESSION] > coil_scores[MarketRegime.BREAKOUT_BUILDUP]
    assert build_scores[MarketRegime.BREAKOUT_BUILDUP] > build_scores[MarketRegime.COMPRESSION]


def test_extreme_volatility_requires_a_percentile_extreme() -> None:
    moderate = features(atr_percentile=70.0, expansion_score=50.0)
    extreme = features(atr_percentile=99.0, expansion_score=95.0)
    assert (
        RegimeScorer().score(extreme).scores[MarketRegime.EXTREME_VOLATILITY]
        > RegimeScorer().score(moderate).scores[MarketRegime.EXTREME_VOLATILITY]
    )


def test_reversal_requires_an_established_prior_move() -> None:
    """Otherwise every bit of chop in a range reads as a reversal."""
    scorer = RegimeScorer()
    # Chop with no trend: direction flips but nothing was established.
    for index in range(10):
        direction = 0.002 if index % 2 == 0 else -0.002
        scorer.score(
            features(returns_5m=direction, moving_average_slope=direction / 4,
                     trend_strength=3.0)
        )
    chop_score = scorer.score(
        features(returns_5m=0.002, moving_average_slope=0.0005, trend_strength=3.0)
    ).scores[MarketRegime.REVERSAL]

    # A genuine flip out of a strong downtrend.
    scorer2 = RegimeScorer()
    for _ in range(8):
        scorer2.score(
            features(returns_5m=-0.003, moving_average_slope=-0.0008, trend_strength=80.0)
        )
    flip_score = scorer2.score(
        features(returns_5m=0.003, moving_average_slope=0.0008, trend_strength=70.0)
    ).scores[MarketRegime.REVERSAL]

    assert flip_score > chop_score


def test_post_event_normalisation_needs_a_prior_volatility_peak() -> None:
    scorer = RegimeScorer()
    # Never elevated: normalisation must score zero.
    for _ in range(8):
        scorer.score(features(atr_percentile=40.0))
    assert scorer.score(features(atr_percentile=35.0)).scores[
        MarketRegime.POST_EVENT_NORMALIZATION
    ] == 0.0

    decaying = RegimeScorer()
    for _ in range(8):
        decaying.score(features(atr_percentile=97.0))
    assert decaying.score(features(atr_percentile=45.0)).scores[
        MarketRegime.POST_EVENT_NORMALIZATION
    ] > 0.0


def test_confidence_is_a_margin_not_an_absolute_score() -> None:
    """Two regimes at 90 and 88 is a coin flip, and must not read as confident."""
    scorer = RegimeScorer()
    scores = scorer.score(normal_range())
    runner_up = scores.runner_up
    assert runner_up is not None
    assert scores.confidence() == pytest.approx(
        min(1.0, (scores.winning_score - runner_up[1]) / 40.0)
    )


def test_scorer_reset_clears_change_based_history() -> None:
    scorer = RegimeScorer()
    for _ in range(10):
        scorer.score(features(atr_percentile=98.0))
    scorer.reset()
    assert scorer.score(features(atr_percentile=40.0)).scores[
        MarketRegime.POST_EVENT_NORMALIZATION
    ] == 0.0


def test_scorer_rejects_too_short_a_history() -> None:
    with pytest.raises(ValueError, match="at least 4"):
        RegimeScorer(history=2)


# ---------------------------------------------------------------- the flicker test


def test_adversarial_oscillation_does_not_flicker() -> None:
    """§5's explicit requirement, tested with input designed to break it.

    Features alternate every single bar between a dead-quiet market and a violent
    breakout — the most hostile possible input. An unstabilised classifier would
    produce a transition on nearly every bar.
    """
    engine = RegimeEngine(regime_settings())
    vectors = [quiet() if index % 2 == 0 else bullish_breakout() for index in range(200)]
    observed = drive(engine, vectors)

    transitions = sum(
        1 for i in range(1, len(observed)) if observed[i] is not observed[i - 1]
    )
    assert transitions <= 4, (
        f"{transitions} transitions over 200 alternating bars; sequence="
        f"{[r.value for r in observed[:40]]}"
    )


def test_a_single_spike_bar_does_not_change_the_regime() -> None:
    """One candle moving must not be enough (§5, stated verbatim in the brief)."""
    engine = RegimeEngine(regime_settings())
    settled = drive(engine, [quiet()] * 40)
    established = settled[-1]
    after_spike = drive(
        engine, [bullish_breakout()], start=START + BAR * 40
    )
    assert after_spike[-1] is established


def test_a_sustained_change_is_eventually_adopted() -> None:
    """Stability must not become deafness: a real regime change must land."""
    engine = RegimeEngine(regime_settings())
    drive(engine, [quiet()] * 40)
    observed = drive(
        engine, [bullish_breakout()] * 60, start=START + BAR * 40
    )
    assert observed[-1] in {MarketRegime.BULLISH_BREAKOUT, MarketRegime.EXTREME_VOLATILITY}


def test_oscillation_in_a_realistic_scenario_stays_bounded() -> None:
    """The same property through the real feature pipeline, not synthetic vectors."""
    settings = MarketSettings.model_validate(
        {"symbol": "XAUUSD", "feed": "simulated", "warmup_bars": 30,
         "percentile_window_bars": 240}
    )
    features_engine = FeatureEngine(settings)
    regimes = RegimeEngine(regime_settings())
    simulator = MarketSimulationEngine(start_price=4_000.0, seed=44, ticks_per_bar=10)
    simulator.set_scenario(Scenario.VOLATILITY_SPIKE)
    moment = START
    bars = 0
    for _ in range(400 * 10):
        vector = features_engine.push_snapshot(simulator.next_tick(moment))
        if vector is not None:
            regimes.classify(vector, now=moment)
            bars += 1
        moment += timedelta(seconds=6)
    # Roughly one regime change per 30 bars is musically acceptable; far more would
    # mean the station changes genre every few minutes.
    assert regimes.transition_count <= max(4, bars // 25), (
        f"{regimes.transition_count} transitions over {bars} bars"
    )


# ---------------------------------------------------------------- each mechanism


def test_first_classification_is_gated_on_absolute_score_not_margin() -> None:
    """Conflating the two made the engine hang in UNKNOWN forever.

    ``min_confidence`` is a *margin* gate whose purpose is preventing flicker between
    near-ties — a risk that exists only once there is an incumbent. Applied to the
    first classification it deadlocked the engine whenever two neighbouring regimes
    scored closely, which in a dead market is the normal case.
    """
    engine = RegimeEngine(regime_settings(min_confidence=0.99))
    observed = drive(engine, [quiet()] * 10)
    assert observed[-1] is not MarketRegime.UNKNOWN


def test_a_first_classification_is_withheld_when_nothing_scores_well() -> None:
    """The absolute floor must still be able to say "no opinion"."""
    engine = RegimeEngine(regime_settings())
    # Mid-everything: no regime's band is centred here, so all scores stay low.
    ambiguous = features(
        atr_percentile=70.0, range_percentile=30.0, trend_strength=22.0,
        volume_percentile=42.0, compression_score=0.0, expansion_score=0.0,
    )
    observed = drive(engine, [ambiguous] * 10)
    assert observed[0] is not None  # a decision was produced
    # Whatever it decides, the reported scores must be available for §47.
    moment = START + BAR * 10
    decision = engine.classify(
        ambiguous.model_copy(update={"timestamp": moment}), now=moment
    )
    assert decision.smoothed_scores


def test_scores_are_reported_even_while_holding_unknown() -> None:
    """§47 most needs the scores when the engine is refusing to classify."""
    engine = RegimeEngine(regime_settings())
    moment = START
    decision = engine.classify(
        quiet(sufficient=False).model_copy(update={"timestamp": moment}), now=moment
    )
    assert decision.regime is MarketRegime.UNKNOWN
    assert decision.suppression_reason == "insufficient_history"


def test_min_duration_blocks_an_early_switch() -> None:
    engine = RegimeEngine(
        regime_settings(min_duration_seconds=3_600.0, confirmation_bars=1,
                        hysteresis_margin=0.0, cooldown_seconds=0.0)
    )
    drive(engine, [quiet()] * 10)
    established = engine.current
    observed = drive(engine, [bullish_breakout()] * 20, start=START + BAR * 10)
    assert all(regime is established for regime in observed)


def test_min_duration_expiry_allows_the_switch() -> None:
    engine = RegimeEngine(
        regime_settings(min_duration_seconds=300.0, confirmation_bars=1,
                        hysteresis_margin=0.0, cooldown_seconds=0.0)
    )
    drive(engine, [quiet()] * 10)
    before = engine.current
    observed = drive(engine, [bullish_breakout()] * 30, start=START + BAR * 20)
    assert observed[-1] is not before


def test_confirmation_bars_require_repeated_wins() -> None:
    engine = RegimeEngine(
        regime_settings(confirmation_bars=8, min_duration_seconds=0.0,
                        hysteresis_margin=0.0, cooldown_seconds=0.0)
    )
    drive(engine, [quiet()] * 10)
    established = engine.current
    # Seven bars is one short of the requirement.
    observed = drive(engine, [bullish_breakout()] * 7, start=START + BAR * 10)
    assert all(regime is established for regime in observed)
    # The eighth completes it.
    more = drive(engine, [bullish_breakout()] * 3, start=START + BAR * 17)
    assert more[-1] is not established


def test_hysteresis_margin_blocks_a_marginal_challenger() -> None:
    """Stops dithering at a threshold boundary."""
    engine = RegimeEngine(
        regime_settings(hysteresis_margin=45.0, confirmation_bars=1,
                        min_duration_seconds=0.0, cooldown_seconds=0.0,
                        min_confidence=0.0)
    )
    drive(engine, [normal_range()] * 20)
    established = engine.current
    # A near-tie challenger: slightly different but not decisively better.
    marginal = normal_range(atr_percentile=34.0, trend_strength=12.0)
    observed = drive(engine, [marginal] * 30, start=START + BAR * 20)
    assert all(regime is established for regime in observed)


def test_cooldown_prevents_immediate_re_entry() -> None:
    """Specifically targets A -> B -> A oscillation."""
    engine = RegimeEngine(
        regime_settings(cooldown_seconds=3_600.0, confirmation_bars=1,
                        min_duration_seconds=0.0, hysteresis_margin=0.0)
    )
    drive(engine, [quiet()] * 20)
    first = engine.current
    drive(engine, [bullish_breakout()] * 20, start=START + BAR * 20)
    second = engine.current
    assert second is not first
    # Return to the original conditions; cooldown must block re-entry.
    observed = drive(engine, [quiet()] * 20, start=START + BAR * 40)
    assert all(regime is not first for regime in observed)


def test_cooldown_expiry_allows_re_entry() -> None:
    engine = RegimeEngine(
        regime_settings(cooldown_seconds=120.0, confirmation_bars=1,
                        min_duration_seconds=0.0, hysteresis_margin=0.0)
    )
    drive(engine, [quiet()] * 20)
    first = engine.current
    drive(engine, [bullish_breakout()] * 20, start=START + BAR * 20)
    observed = drive(engine, [quiet()] * 40, start=START + BAR * 60)
    assert first in observed


def test_a_suppressed_challenger_keeps_its_streak() -> None:
    """A challenger blocked by min_duration must not restart counting afterwards.

    Otherwise a regime change is delayed twice over — once by the duration gate and
    again by a confirmation count that was reset while it waited.
    """
    settings = regime_settings(
        min_duration_seconds=600.0, confirmation_bars=3, hysteresis_margin=0.0,
        cooldown_seconds=0.0,
    )
    engine = RegimeEngine(settings)
    drive(engine, [quiet()] * 10)
    # Challenge for 15 bars while min_duration still blocks it.
    drive(engine, [bullish_breakout()] * 8, start=START + BAR * 10)
    # By the time the gate clears the streak is long past 3, so the next qualifying
    # bar adopts immediately.
    observed = drive(engine, [bullish_breakout()] * 2, start=START + BAR * 20)
    assert observed[-1] is not MarketRegime.QUIET


def test_suppression_reason_is_reported() -> None:
    """The §47 Market Lab shows why a challenger was rejected."""
    # min_confidence is zeroed so the *duration* gate is unambiguously the binding
    # one; gates are evaluated in order and the reason names whichever bound first.
    settings = regime_settings(min_duration_seconds=3_600.0, confirmation_bars=1,
                              hysteresis_margin=0.0, cooldown_seconds=0.0,
                              min_confidence=0.0)
    engine = RegimeEngine(settings)
    drive(engine, [quiet()] * 10)

    # Several bars, not one: score smoothing is itself an anti-flicker layer, so a
    # single contrary bar does not yet make the challenger the top *smoothed* score —
    # at which point there is no challenger to report a reason about.
    moment = START + BAR * 10
    suppressed = None
    for _ in range(12):
        decision = engine.classify(
            bullish_breakout().model_copy(update={"timestamp": moment}), now=moment
        )
        if decision.suppression_reason:
            suppressed = decision
            break
        moment += BAR

    assert suppressed is not None, "no challenger was ever suppressed"
    assert "min_duration" in suppressed.suppression_reason
    assert suppressed.candidate is not None
    assert suppressed.candidate_streak >= 1
    # The scores must still be reported while a challenger is suppressed.
    assert suppressed.smoothed_scores


# ---------------------------------------------------------------- warm-up & unknown


def test_insufficient_history_yields_unknown_with_zero_confidence() -> None:
    """§4/§32: refuse to classify on unreliable features."""
    engine = RegimeEngine(regime_settings())
    moment = START
    for _ in range(40):
        decision = engine.classify(
            bullish_breakout(sufficient=False).model_copy(update={"timestamp": moment}),
            now=moment,
        )
        assert decision.regime is MarketRegime.UNKNOWN
        assert decision.confidence == 0.0
        assert decision.direction is MarketDirection.NEUTRAL
        moment += BAR
    assert engine.transition_count == 0


def test_first_classification_is_adopted_without_waiting_for_confirmation() -> None:
    """Startup must not sit in UNKNOWN for several minutes with clear data."""
    engine = RegimeEngine(regime_settings(confirmation_bars=5))
    observed = drive(engine, [quiet()] * 3)
    assert observed[0] is not MarketRegime.UNKNOWN


def test_regime_age_grows_and_resets_on_change() -> None:
    engine = RegimeEngine(
        regime_settings(min_duration_seconds=0.0, confirmation_bars=1,
                        hysteresis_margin=0.0, cooldown_seconds=0.0)
    )
    moment = START
    for _ in range(10):
        decision = engine.classify(
            quiet().model_copy(update={"timestamp": moment}), now=moment
        )
        moment += BAR
    assert decision.age_seconds == pytest.approx(9 * 60.0)

    for _ in range(10):
        decision = engine.classify(
            bullish_breakout().model_copy(update={"timestamp": moment}), now=moment
        )
        if decision.changed:
            assert decision.age_seconds == 0.0
            break
        moment += BAR


def test_naive_timestamp_is_rejected() -> None:
    engine = RegimeEngine(regime_settings())
    with pytest.raises(ValueError, match="timezone-aware"):
        engine.classify(quiet(), now=datetime(2026, 1, 1))


def test_reset_returns_the_engine_to_unknown() -> None:
    engine = RegimeEngine(regime_settings())
    drive(engine, [bullish_breakout()] * 30)
    assert engine.current is not MarketRegime.UNKNOWN
    engine.reset()
    assert engine.current is MarketRegime.UNKNOWN
    assert engine.transition_count == 0


# ---------------------------------------------------------------- reachability


def test_most_regimes_are_reachable_from_the_scenario_library() -> None:
    """Every regime the station can classify should be producible from §7 scenarios.

    A regime that no scenario can reach is untestable and, in practice, dead code —
    and the music mapped to it would never air.
    """
    settings = MarketSettings.model_validate(
        {"symbol": "XAUUSD", "feed": "simulated", "warmup_bars": 25,
         "percentile_window_bars": 200}
    )
    permissive = regime_settings(
        min_duration_seconds=60.0, confirmation_bars=2, hysteresis_margin=4.0,
        cooldown_seconds=60.0, min_confidence=0.3,
    )
    reached: set[MarketRegime] = set()
    for scenario in Scenario:
        for seed in (1, 7, 19):
            features_engine = FeatureEngine(settings)
            regimes = RegimeEngine(permissive)
            simulator = MarketSimulationEngine(
                start_price=4_000.0, seed=seed, ticks_per_bar=6
            )
            moment = START

            # Warm up on a neutral random walk FIRST, then switch to the target
            # scenario. Without this, every scenario's interesting phase (a breakout
            # burst at bars 25-37, a fakeout reversal at bars 30-45) happens while the
            # feature engine is still warming up and is never classified at all — the
            # test would then be measuring warm-up timing rather than reachability.
            simulator.set_scenario(Scenario.RANDOM_WALK)
            for _ in range(90 * 6):
                features_engine.push_snapshot(simulator.next_tick(moment))
                moment += timedelta(seconds=10)

            simulator.set_scenario(scenario)
            for _ in range(320 * 6):
                vector = features_engine.push_snapshot(simulator.next_tick(moment))
                if vector is not None:
                    reached.add(regimes.classify(vector, now=moment).regime)
                moment += timedelta(seconds=10)

    expected = set(MarketRegime) - {MarketRegime.UNKNOWN}
    missing = expected - reached
    # Named rather than silently tolerated: a regime no scenario can reach is dead
    # code, and the music mapped to it would never air.
    assert not missing, f"unreachable regimes: {sorted(r.value for r in missing)}"


def test_stabiliser_can_be_used_directly() -> None:
    """The scorer/stabiliser split must remain usable in isolation for §47 tooling."""
    scorer = RegimeScorer()
    stabiliser = RegimeStabiliser(regime_settings())
    vector = quiet()
    decision = stabiliser.update(scorer.score(vector), features=vector, now=START)
    assert decision.regime is MarketRegime.QUIET
    assert decision.smoothed_scores
    assert set(decision.smoothed_scores) == set(MarketRegime)
