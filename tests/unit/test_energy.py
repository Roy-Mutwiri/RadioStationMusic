"""Market energy score (§6, milestone 2.4).

Exit criteria: always within [0, 100]; monotone in each weighted input; velocity
sign matches the direction of change. The energy score drives BPM, intensity and
vocal probability, so an out-of-range or non-monotone value is not a cosmetic bug —
it produces music that contradicts the market.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tradefix_radio.config.schema import EnergySettings, EnergyWeights, MarketSettings
from tradefix_radio.contracts.market import MarketFeaturesV1
from tradefix_radio.market.energy import (
    MOMENTUM_FULL_SCALE,
    VELOCITY_FULL_SCALE,
    EnergyCalculator,
    energy_band,
)
from tradefix_radio.market.features import FeatureEngine
from tradefix_radio.market.simulation import MarketSimulationEngine, Scenario

START = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)

unit = st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False)
small = st.floats(min_value=-0.05, max_value=0.05, allow_nan=False, allow_infinity=False)


def features(
    *,
    sufficient: bool = True,
    atr_percentile: float = 50.0,
    realized_volatility: float = 0.0005,
    returns_1m: float = 0.0,
    trend_strength: float = 0.0,
    volume_percentile: float = 50.0,
    breakout_strength: float = 0.0,
    expansion_score: float = 0.0,
    compression_score: float = 0.0,
    momentum: float = 0.0,
) -> MarketFeaturesV1:
    return MarketFeaturesV1(
        symbol="XAUUSD",
        timestamp=START,
        sufficient_history=sufficient,
        samples_observed=500,
        returns_1m=returns_1m,
        returns_5m=momentum,
        returns_15m=0.0,
        atr=1.4,
        atr_percentile=atr_percentile,
        realized_volatility=realized_volatility,
        range_percentile=50.0,
        adx=20.0,
        rsi=50.0,
        moving_average_slope=0.0,
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


def calculator(**overrides: object) -> EnergyCalculator:
    return EnergyCalculator(EnergySettings.model_validate(overrides))


# ---------------------------------------------------------------- bounds


@given(
    atr=unit,
    trend=unit,
    volume=unit,
    breakout=unit,
    expansion=unit,
    returns=small,
    momentum=small,
)
def test_energy_is_always_within_bounds(
    atr: float,
    trend: float,
    volume: float,
    breakout: float,
    expansion: float,
    returns: float,
    momentum: float,
) -> None:
    """§6: 0-100, for every representable feature combination."""
    result = calculator().compute(
        features(
            atr_percentile=atr,
            trend_strength=trend,
            volume_percentile=volume,
            breakout_strength=breakout,
            expansion_score=expansion,
            returns_1m=returns,
            momentum=momentum,
        )
    )
    assert 0.0 <= result.raw_energy <= 100.0
    assert 0.0 <= result.smoothed_energy <= 100.0


def test_all_inputs_at_minimum_gives_near_zero_energy() -> None:
    result = calculator().compute(
        features(
            atr_percentile=0.0,
            realized_volatility=0.0,
            trend_strength=0.0,
            volume_percentile=0.0,
            breakout_strength=0.0,
            expansion_score=0.0,
            returns_1m=0.0,
            momentum=0.0,
        )
    )
    assert result.raw_energy == pytest.approx(0.0, abs=0.01)


def test_all_inputs_at_maximum_gives_full_energy() -> None:
    result = calculator().compute(
        features(
            atr_percentile=100.0,
            realized_volatility=1.0,
            trend_strength=100.0,
            volume_percentile=100.0,
            breakout_strength=100.0,
            expansion_score=100.0,
            returns_1m=1.0,
            momentum=1.0,
        )
    )
    assert result.raw_energy == pytest.approx(100.0, abs=0.01)


# ---------------------------------------------------------------- monotonicity


@pytest.mark.parametrize(
    "field",
    [
        "atr_percentile",
        "trend_strength",
        "volume_percentile",
        "breakout_strength",
        "expansion_score",
    ],
)
def test_energy_is_monotone_in_each_percentile_input(field: str) -> None:
    """Raising any weighted input must not lower energy."""
    previous = -1.0
    for value in (0.0, 25.0, 50.0, 75.0, 100.0):
        result = calculator().compute(features(**{field: value}))
        assert result.raw_energy >= previous, f"{field} is not monotone"
        previous = result.raw_energy


def test_energy_is_monotone_in_price_velocity_magnitude() -> None:
    """Direction must not matter: a fast fall is as energetic as a fast rise."""
    up = calculator().compute(features(returns_1m=VELOCITY_FULL_SCALE)).raw_energy
    down = calculator().compute(features(returns_1m=-VELOCITY_FULL_SCALE)).raw_energy
    still = calculator().compute(features(returns_1m=0.0)).raw_energy
    assert up == pytest.approx(down)
    assert up > still


def test_momentum_contributes_symmetrically() -> None:
    up = calculator().compute(features(momentum=MOMENTUM_FULL_SCALE)).raw_energy
    down = calculator().compute(features(momentum=-MOMENTUM_FULL_SCALE)).raw_energy
    assert up == pytest.approx(down)


def test_inputs_saturate_rather_than_overflowing() -> None:
    """A flash crash must not pin energy at 100 for an hour afterwards."""
    at_scale = calculator().compute(features(returns_1m=VELOCITY_FULL_SCALE)).raw_energy
    far_beyond = calculator().compute(features(returns_1m=VELOCITY_FULL_SCALE * 50)).raw_energy
    assert far_beyond == pytest.approx(at_scale)


# ---------------------------------------------------------------- weights


def test_a_zero_weight_removes_a_contributor_entirely() -> None:
    """§6: weights are configuration, and zero must mean "ignore"."""
    weights = EnergyWeights(
        atr_percentile=0.0,
        realized_volatility=0.0,
        price_velocity=0.0,
        trend_strength=1.0,
        volume_percentile=0.0,
        breakout_strength=0.0,
        range_expansion=0.0,
        momentum=0.0,
    )
    engine = EnergyCalculator(EnergySettings(weights=weights))
    low = engine.compute(features(atr_percentile=0.0, trend_strength=80.0)).raw_energy
    engine.reset()
    high = engine.compute(features(atr_percentile=100.0, trend_strength=80.0)).raw_energy
    assert low == pytest.approx(high)
    assert low == pytest.approx(80.0, abs=0.01)


def test_weights_need_not_sum_to_one() -> None:
    """They are normalised at use, so an operator need not do the arithmetic.

    Two equal weights of 0.4 sum to 0.8, not 1.0; after normalisation each
    contributes half, so a single input at 100 must yield exactly 50.
    """
    weights = EnergyWeights(
        atr_percentile=0.4,
        realized_volatility=0.0,
        price_velocity=0.0,
        trend_strength=0.4,
        volume_percentile=0.0,
        breakout_strength=0.0,
        range_expansion=0.0,
        momentum=0.0,
    )
    engine = EnergyCalculator(EnergySettings(weights=weights))
    result = engine.compute(features(atr_percentile=100.0, trend_strength=0.0))
    assert result.raw_energy == pytest.approx(50.0, abs=0.01)


def test_contributions_sum_to_the_raw_energy() -> None:
    """The §47 breakdown must reconcile with the headline number."""
    engine = calculator()
    vector = features(atr_percentile=70.0, trend_strength=60.0, breakout_strength=40.0)
    contributions = engine.contributions(vector)
    result = engine.compute(vector)
    assert sum(c.points for c in contributions) == pytest.approx(result.raw_energy, abs=1e-9)


def test_contribution_weights_sum_to_one() -> None:
    engine = calculator()
    contributions = engine.contributions(features())
    assert sum(c.weight for c in contributions) == pytest.approx(1.0)


def test_components_are_reported_for_the_market_lab() -> None:
    result = calculator().compute(features())
    assert set(result.components) == {
        "atr_percentile",
        "realized_volatility",
        "price_velocity",
        "trend_strength",
        "volume_percentile",
        "breakout_strength",
        "range_expansion",
        "momentum",
    }


# ---------------------------------------------------------------- warm-up


def test_warm_up_reports_neutral_smoothed_energy() -> None:
    """Seeding the smoother from unreliable features biases the first minutes."""
    result = calculator().compute(features(sufficient=False, atr_percentile=95.0))
    assert result.smoothed_energy == 50.0
    assert result.energy_velocity == 0.0
    # The raw figure is still computed for the §47 page, and still responds to the
    # inputs — compared against a quiet vector rather than an absolute threshold,
    # since one high input cannot by itself exceed 50 with the default weights.
    quiet = calculator().compute(features(sufficient=False, atr_percentile=5.0))
    assert result.raw_energy > quiet.raw_energy


def test_warm_up_does_not_poison_the_smoother() -> None:
    """The first post-warm-up value must be adopted cleanly."""
    engine = calculator()
    for _ in range(20):
        engine.compute(features(sufficient=False, atr_percentile=100.0))
    result = engine.compute(features(sufficient=True, atr_percentile=10.0,
                                     realized_volatility=0.0, volume_percentile=10.0))
    assert result.smoothed_energy == pytest.approx(result.raw_energy)


# ---------------------------------------------------------------- smoothing


def test_smoothing_lags_a_step_change() -> None:
    engine = calculator(smoothing_alpha=0.1)
    engine.compute(features(atr_percentile=0.0, realized_volatility=0.0,
                            volume_percentile=0.0))
    stepped = engine.compute(
        features(atr_percentile=100.0, realized_volatility=1.0, volume_percentile=100.0,
                 trend_strength=100.0, breakout_strength=100.0, expansion_score=100.0,
                 returns_1m=1.0, momentum=1.0)
    )
    assert stepped.smoothed_energy < stepped.raw_energy


def test_smoothing_converges_on_a_sustained_level() -> None:
    engine = calculator(smoothing_alpha=0.3)
    vector = features(atr_percentile=70.0, trend_strength=50.0)
    for _ in range(200):
        result = engine.compute(vector)
    assert result.smoothed_energy == pytest.approx(result.raw_energy, abs=0.5)


# ---------------------------------------------------------------- velocity


def test_velocity_is_zero_before_the_window_fills() -> None:
    """The station must not pre-empt a move it has not seen."""
    engine = calculator()
    first = engine.compute(features(atr_percentile=50.0))
    assert first.energy_velocity == 0.0


def test_velocity_is_positive_while_energy_rises() -> None:
    """§6: music can prepare for rising market intensity."""
    engine = calculator(smoothing_alpha=0.5, velocity_window_bars=5)
    result = None
    for level in (0.0, 20.0, 40.0, 60.0, 80.0, 100.0):
        result = engine.compute(features(atr_percentile=level, trend_strength=level))
    assert result is not None
    assert result.energy_velocity > 0


def test_velocity_is_negative_while_energy_falls() -> None:
    engine = calculator(smoothing_alpha=0.5, velocity_window_bars=5)
    result = None
    for level in (100.0, 80.0, 60.0, 40.0, 20.0, 0.0):
        result = engine.compute(features(atr_percentile=level, trend_strength=level))
    assert result is not None
    assert result.energy_velocity < 0


def test_velocity_is_near_zero_on_a_plateau() -> None:
    engine = calculator(velocity_window_bars=6)
    result = None
    for _ in range(40):
        result = engine.compute(features(atr_percentile=60.0, trend_strength=40.0))
    assert result is not None
    assert abs(result.energy_velocity) < 0.5


def test_velocity_resists_single_bar_jitter() -> None:
    """A least-squares slope, not a difference: one bar is not a trend in intensity."""
    engine = calculator(smoothing_alpha=1.0, velocity_window_bars=8)
    for _ in range(12):
        engine.compute(features(atr_percentile=50.0))
    spiked = engine.compute(features(atr_percentile=100.0, trend_strength=100.0))
    # A simple difference would be very large; the slope is damped by the window.
    assert abs(spiked.energy_velocity) < 20.0


def test_reset_clears_smoother_and_velocity() -> None:
    engine = calculator()
    for _ in range(10):
        engine.compute(features(atr_percentile=90.0))
    engine.reset()
    assert engine.last is None
    fresh = engine.compute(features(atr_percentile=20.0, realized_volatility=0.0,
                                    volume_percentile=20.0))
    assert fresh.smoothed_energy == pytest.approx(fresh.raw_energy)
    assert fresh.energy_velocity == 0.0


# ---------------------------------------------------------------- scenarios


def test_quiet_and_violent_scenarios_separate_clearly() -> None:
    """The product requirement: a dead market and a breakout must not sound alike."""
    quiet = _scenario_energy(Scenario.FLAT, bars=200)
    violent = _scenario_energy(Scenario.VIOLENT_BREAKOUT, bars=200)
    assert quiet < 30.0, f"flat market energy {quiet:.1f} is too high"
    assert violent > 55.0, f"violent breakout energy {violent:.1f} is too low"
    assert violent > quiet * 2.0


def test_scenario_energies_are_ordered_sensibly() -> None:
    """§1's mapping, verified as an ordering rather than as fixed numbers."""
    flat = _scenario_energy(Scenario.FLAT, bars=200)
    low = _scenario_energy(Scenario.LOW_VOLATILITY_RANGE, bars=200)
    trend = _scenario_energy(Scenario.SLOW_BULL_TREND, bars=200)
    breakout = _scenario_energy(Scenario.BREAKOUT_UP, bars=200)
    assert flat <= low <= trend <= breakout, (
        f"flat={flat:.1f} low={low:.1f} trend={trend:.1f} breakout={breakout:.1f}"
    )


def _scenario_energy(scenario: Scenario, *, bars: int, seed: int = 23) -> float:
    """Mean smoothed energy over the last quarter of a scenario run."""
    settings = MarketSettings.model_validate(
        {"symbol": "XAUUSD", "feed": "simulated", "warmup_bars": 30,
         "percentile_window_bars": 240}
    )
    features_engine = FeatureEngine(settings)
    energy_engine = EnergyCalculator(EnergySettings())
    simulator = MarketSimulationEngine(start_price=4_000.0, seed=seed, ticks_per_bar=10)
    simulator.set_scenario(scenario)
    interval = timedelta(seconds=6)
    moment = START
    values: list[float] = []
    for _ in range(bars * 10):
        vector = features_engine.push_snapshot(simulator.next_tick(moment))
        if vector is not None:
            values.append(energy_engine.compute(vector).smoothed_energy)
        moment += interval
    tail = values[-(len(values) // 4) :]
    return sum(tail) / len(tail)


# ---------------------------------------------------------------- bands


@pytest.mark.parametrize(
    ("energy", "expected"),
    [(0.0, 0), (24.9, 0), (25.0, 1), (44.9, 1), (45.0, 2), (65.0, 3), (82.0, 4), (100.0, 4)],
)
def test_energy_band_is_a_step_function(energy: float, expected: int) -> None:
    assert energy_band(energy) == expected


def test_energy_band_matches_the_configured_bpm_bands() -> None:
    """The band helper and the config's BPM bands must use the same boundaries.

    If they drift, the §43 panel shows one band while the director uses another.
    """
    from tradefix_radio.config.schema import MusicSettings

    music = MusicSettings()
    bounds = tuple(sorted(b for b in music.bpm_bands if b > 0))
    for energy in (0.0, 10.0, 30.0, 50.0, 70.0, 90.0, 100.0):
        band = energy_band(energy, bounds)
        expected_bound = max(b for b in music.bpm_bands if b <= energy)
        assert sorted(music.bpm_bands)[band] == expected_bound
