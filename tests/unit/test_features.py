"""Feature engine and bar aggregation (§4, §6, milestones 2.2–2.3).

The headline test here is :func:`test_energy_is_invariant_under_price_rescaling` and
its siblings. §6 says the engine must "not make assumptions about absolute XAUUSD
price values", and the only way to know that is to run the same market at two price
levels and assert the outputs are identical. Everything else in the module supports
that claim or guards the warm-up honesty rule.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings as hypothesis_settings
from hypothesis import strategies as st

from tradefix_radio.config.schema import EnergySettings, MarketSettings
from tradefix_radio.contracts.market import MarketSnapshotV1
from tradefix_radio.market.energy import EnergyCalculator
from tradefix_radio.market.features import BarAggregator, FeatureEngine
from tradefix_radio.market.indicators import Bar
from tradefix_radio.market.simulation import MarketSimulationEngine, Scenario

START = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)


def market_settings(**overrides: object) -> MarketSettings:
    base: dict[str, object] = {
        "symbol": "XAUUSD",
        "feed": "simulated",
        "bar_seconds": 60,
        "warmup_bars": 30,
        "percentile_window_bars": 240,
    }
    base.update(overrides)
    return MarketSettings.model_validate(base)


def feed_scenario(
    engine: FeatureEngine,
    scenario: Scenario,
    *,
    bars: int,
    seed: int = 13,
    price_scale: float = 1.0,
    ticks_per_bar: int = 10,
) -> list:
    """Drive a feature engine with a simulated scenario, returning every vector."""
    simulator = MarketSimulationEngine(
        start_price=4_000.0 * price_scale, seed=seed, ticks_per_bar=ticks_per_bar
    )
    simulator.set_scenario(scenario)
    interval = timedelta(seconds=60 / ticks_per_bar)
    moment = START
    produced = []
    for _ in range(bars * ticks_per_bar):
        snapshot = simulator.next_tick(moment)
        features = engine.push_snapshot(snapshot)
        if features is not None:
            produced.append(features)
        moment += interval
    return produced


# ---------------------------------------------------------------- bar aggregation


def test_aggregator_closes_a_bar_on_the_wall_clock_boundary() -> None:
    """Alignment matters: a first-tick-relative grid shifts on every restart."""
    aggregator = BarAggregator(60)
    base = datetime(2026, 10, 2, 12, 0, 30, tzinfo=timezone.utc)
    assert aggregator.push(_snapshot(base, 100.0)) is None
    assert aggregator.push(_snapshot(base + timedelta(seconds=20), 101.0)) is None
    result = aggregator.push(_snapshot(base + timedelta(seconds=40), 102.0))
    assert result is not None
    bar, opened_at = result
    assert opened_at == datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
    assert bar.open == pytest.approx(100.0)
    assert bar.close == pytest.approx(101.0)


def test_aggregator_tracks_high_and_low() -> None:
    aggregator = BarAggregator(60)
    base = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
    for offset, price in enumerate([100.0, 105.0, 95.0, 101.0]):
        aggregator.push(_snapshot(base + timedelta(seconds=offset * 5), price))
    current = aggregator.current
    assert current is not None
    assert current.high == pytest.approx(105.0)
    assert current.low == pytest.approx(95.0)


def test_aggregator_treats_volume_as_a_running_total() -> None:
    """Feeds report tick_volume cumulatively within their own bar.

    Treating it as a per-tick delta would inflate volume by the tick count and make
    every volume percentile meaningless.
    """
    aggregator = BarAggregator(60)
    base = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
    for offset, volume in enumerate([10.0, 25.0, 40.0]):
        aggregator.push(_snapshot(base + timedelta(seconds=offset * 5), 100.0, volume))
    current = aggregator.current
    assert current is not None
    assert current.volume == pytest.approx(40.0)


def test_aggregator_handles_a_provider_bar_roll() -> None:
    """A decreasing cumulative volume means the provider rolled its own bar."""
    aggregator = BarAggregator(60)
    base = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
    aggregator.push(_snapshot(base, 100.0, 50.0))
    aggregator.push(_snapshot(base + timedelta(seconds=10), 100.0, 5.0))
    current = aggregator.current
    assert current is not None
    assert current.volume == pytest.approx(55.0)


def test_aggregator_counts_ticks_when_volume_is_absent() -> None:
    aggregator = BarAggregator(60)
    base = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
    for offset in range(4):
        aggregator.push(_snapshot(base + timedelta(seconds=offset * 5), 100.0, None))
    current = aggregator.current
    assert current is not None
    assert current.volume == pytest.approx(4.0)


def test_aggregator_folds_in_an_out_of_order_tick() -> None:
    """Late ticks are real observations; dropping them understates range."""
    aggregator = BarAggregator(60)
    base = datetime(2026, 10, 2, 12, 0, 30, tzinfo=timezone.utc)
    aggregator.push(_snapshot(base, 100.0))
    aggregator.push(_snapshot(base - timedelta(seconds=10), 120.0))
    current = aggregator.current
    assert current is not None
    assert current.high == pytest.approx(120.0)


def test_aggregator_rejects_an_invalid_bar_duration() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        BarAggregator(0)


# ---------------------------------------------------------------- warm-up honesty


def test_features_report_insufficient_history_during_warm_up() -> None:
    """§4: publish honest uncertainty rather than zeros that look like data."""
    engine = FeatureEngine(market_settings(warmup_bars=40))
    produced = feed_scenario(engine, Scenario.RANDOM_WALK, bars=20)
    assert produced, "no bars closed"
    assert all(not features.sufficient_history for features in produced)


def test_features_become_sufficient_after_warm_up() -> None:
    engine = FeatureEngine(market_settings(warmup_bars=30))
    produced = feed_scenario(engine, Scenario.RANDOM_WALK, bars=140)
    assert produced[-1].sufficient_history
    assert engine.sufficient_history


def test_sufficiency_also_waits_for_the_slowest_indicator() -> None:
    """A tiny warmup_bars must not declare readiness while ADX is still blind.

    5 is the configuration floor; ADX and the compression ratio need far more bars
    than that, so readiness must still be withheld.
    """
    engine = FeatureEngine(market_settings(warmup_bars=5))
    produced = feed_scenario(engine, Scenario.RANDOM_WALK, bars=8)
    assert produced
    assert engine.bars_observed > 5
    assert not produced[-1].sufficient_history


def test_return_features_are_zero_until_their_horizon_exists() -> None:
    """A "15-minute return" from three bars is a different quantity, not an estimate."""
    engine = FeatureEngine(market_settings(warmup_bars=5))
    produced = feed_scenario(engine, Scenario.SLOW_BULL_TREND, bars=4)
    assert produced
    assert produced[0].returns_15m == 0.0


# ---------------------------------------------------------------- no garbage


@pytest.mark.parametrize(
    "scenario",
    [
        Scenario.FLAT,
        Scenario.COMPRESSION,
        Scenario.VIOLENT_BREAKOUT,
        Scenario.FLASH_MOVE,
        Scenario.EVENT_VOLATILITY,
        Scenario.MEAN_REVERSION,
    ],
)
def test_no_feature_is_ever_non_finite(scenario: Scenario) -> None:
    """Including the pathological scenarios, which is where division by zero lurks."""
    engine = FeatureEngine(market_settings())
    for features in feed_scenario(engine, scenario, bars=120):
        for name, value in features.model_dump().items():
            if isinstance(value, float):
                assert math.isfinite(value), f"{scenario.value}: {name} = {value}"


def test_percentile_features_stay_within_bounds() -> None:
    engine = FeatureEngine(market_settings())
    for features in feed_scenario(engine, Scenario.FLASH_MOVE, bars=120):
        for value in (
            features.atr_percentile,
            features.range_percentile,
            features.volume_percentile,
            features.trend_strength,
            features.breakout_strength,
            features.compression_score,
            features.expansion_score,
            features.adx,
            features.rsi,
        ):
            assert 0.0 <= value <= 100.0
        assert 0.0 <= features.session_range_position <= 1.0


def test_a_completely_flat_market_does_not_divide_by_zero() -> None:
    """Zero range and zero ATR are the normal path for a dead market."""
    engine = FeatureEngine(market_settings(warmup_bars=5))
    moment = START
    last = None
    for _ in range(80):
        last = engine.push_bar(Bar(open=4000.0, high=4000.0, low=4000.0,
                                   close=4000.0, volume=5.0), moment)
        moment += timedelta(seconds=60)
    assert last is not None
    assert last.atr == pytest.approx(0.0)
    assert last.realized_volatility == pytest.approx(0.0)
    assert math.isfinite(last.moving_average_slope)
    assert last.breakout_strength == 0.0


# ---------------------------------------------------------------- scale invariance


@pytest.mark.parametrize("scale", [0.05, 0.45, 10.0, 100.0])
def test_percentile_features_are_invariant_under_price_rescaling(scale: float) -> None:
    """§6: identical relative behaviour must produce identical normalised features.

    The same seeded scenario is run at two price levels. Every scale-free feature
    must match; ``atr`` itself is expected to differ because it is in price units and
    is only ever consumed as a percentile.
    """
    base_engine = FeatureEngine(market_settings())
    scaled_engine = FeatureEngine(market_settings())
    base = feed_scenario(base_engine, Scenario.BREAKOUT_UP, bars=140, seed=77)
    scaled = feed_scenario(
        scaled_engine, Scenario.BREAKOUT_UP, bars=140, seed=77, price_scale=scale
    )
    assert len(base) == len(scaled)

    for first, second in zip(base[-40:], scaled[-40:], strict=True):
        for field in (
            "returns_1m",
            "returns_5m",
            "returns_15m",
            "atr_percentile",
            "realized_volatility",
            "range_percentile",
            "adx",
            "rsi",
            "moving_average_slope",
            "trend_strength",
            "momentum",
            "volume_percentile",
            "session_range_position",
            "distance_from_high",
            "distance_from_low",
            "breakout_strength",
            "compression_score",
            "expansion_score",
        ):
            assert getattr(first, field) == pytest.approx(
                getattr(second, field), rel=1e-6, abs=1e-9
            ), f"{field} differs at scale {scale}"


@pytest.mark.parametrize("scale", [0.05, 10.0, 250.0])
def test_energy_is_invariant_under_price_rescaling(scale: float) -> None:
    """The headline §6 property, stated in the terms the brief uses.

    "Do not make assumptions about absolute XAUUSD price values." Multiply every
    price by 250 and the station must make the same musical decisions.
    """
    results = []
    for price_scale in (1.0, scale):
        features_engine = FeatureEngine(market_settings())
        energy = EnergyCalculator(EnergySettings())
        values = []
        for features in feed_scenario(
            features_engine, Scenario.VIOLENT_BREAKOUT, bars=140, seed=91,
            price_scale=price_scale,
        ):
            values.append(energy.compute(features).smoothed_energy)
        results.append(values)

    assert len(results[0]) == len(results[1])
    for plain, rescaled in zip(results[0], results[1], strict=True):
        assert plain == pytest.approx(rescaled, rel=1e-6, abs=1e-9)


def test_atr_itself_does_scale_with_price() -> None:
    """Confirms the invariance above is real rather than a frozen pipeline.

    ATR is in price units, so it *must* change. If it did not, the test above would
    be passing because nothing was being computed.
    """
    plain = feed_scenario(FeatureEngine(market_settings()), Scenario.RANDOM_WALK,
                          bars=100, seed=5)
    scaled = feed_scenario(FeatureEngine(market_settings()), Scenario.RANDOM_WALK,
                           bars=100, seed=5, price_scale=10.0)
    assert scaled[-1].atr == pytest.approx(plain[-1].atr * 10.0, rel=1e-6)


# ---------------------------------------------------------------- feature meaning


def test_compression_scenario_raises_the_compression_score() -> None:
    engine = FeatureEngine(market_settings())
    produced = feed_scenario(engine, Scenario.COMPRESSION, bars=180, seed=3)
    late = [f.compression_score for f in produced[-30:]]
    early = [f.compression_score for f in produced[40:70]]
    assert max(late) > max(early), f"early={max(early):.1f} late={max(late):.1f}"


def test_breakout_scenario_raises_breakout_strength_and_expansion() -> None:
    engine = FeatureEngine(market_settings(warmup_bars=20))
    produced = feed_scenario(engine, Scenario.BREAKOUT_UP, bars=90, seed=4)
    assert max(f.breakout_strength for f in produced) > 25.0
    assert max(f.expansion_score for f in produced) > 25.0


def test_trend_scenario_raises_trend_strength_above_a_flat_market() -> None:
    trending = feed_scenario(
        FeatureEngine(market_settings()), Scenario.SLOW_BULL_TREND, bars=200, seed=6
    )
    flat = feed_scenario(FeatureEngine(market_settings()), Scenario.FLAT, bars=200, seed=6)
    assert max(f.trend_strength for f in trending[-60:]) > max(
        f.trend_strength for f in flat[-60:]
    )


def test_trend_strength_vetoes_adx_when_the_slope_is_noise() -> None:
    """ADX rises in a directionless chop; trend_strength must not follow it.

    Otherwise the station plays trending programming through a volatile mess.
    """
    spike = feed_scenario(
        FeatureEngine(market_settings()), Scenario.VOLATILITY_SPIKE, bars=140, seed=8
    )
    trend = feed_scenario(
        FeatureEngine(market_settings()), Scenario.SLOW_BULL_TREND, bars=200, seed=8
    )
    spike_ratio = max(
        f.trend_strength / f.adx for f in spike[-40:] if f.adx > 10.0
    )
    trend_ratio = max(
        f.trend_strength / f.adx for f in trend[-40:] if f.adx > 10.0
    )
    assert spike_ratio < trend_ratio


def test_bullish_and_bearish_trends_produce_opposite_slope_signs() -> None:
    """Averaged over a stretch, not sampled at one bar.

    The 20-bar MA slope is a local measurement and legitimately flips sign on
    individual bars inside a trend — that is noise, not a trend reversal. Asserting
    on a single bar would make this test flaky for a reason that says nothing about
    the engine.
    """
    bull = feed_scenario(
        FeatureEngine(market_settings()), Scenario.SLOW_BULL_TREND, bars=200, seed=9
    )
    bear = feed_scenario(
        FeatureEngine(market_settings()), Scenario.SLOW_BEAR_TREND, bars=200, seed=9
    )
    bull_mean = sum(f.moving_average_slope for f in bull[-60:]) / 60
    bear_mean = sum(f.moving_average_slope for f in bear[-60:]) / 60
    assert bull_mean > 0, f"bull mean slope {bull_mean:.3e}"
    assert bear_mean < 0, f"bear mean slope {bear_mean:.3e}"
    # And the 15-bar return, which is the unambiguous directional statement.
    assert sum(f.returns_15m for f in bull[-60:]) > 0
    assert sum(f.returns_15m for f in bear[-60:]) < 0


def test_distances_are_expressed_in_atr_units() -> None:
    """Scale-free and directly meaningful at any price level."""
    produced = feed_scenario(
        FeatureEngine(market_settings()), Scenario.RANDOM_WALK, bars=120, seed=10
    )
    last = produced[-1]
    # In ATR units a 40-bar lookback should keep both distances in single digits.
    assert 0.0 <= last.distance_from_high < 40.0
    assert 0.0 <= last.distance_from_low < 40.0


def test_volume_percentile_responds_to_a_volume_surge() -> None:
    produced = feed_scenario(
        FeatureEngine(market_settings()), Scenario.EVENT_VOLATILITY, bars=160, seed=12
    )
    assert max(f.volume_percentile for f in produced) > 80.0


def test_session_range_position_resets_with_the_session() -> None:
    """Otherwise the figure drifts toward 0.5 forever and tells nobody anything."""
    engine = FeatureEngine(market_settings(warmup_bars=5))
    # Walk across the London open, which is a session boundary.
    moment = datetime(2026, 10, 2, 6, 55, 0, tzinfo=timezone.utc)
    for index in range(30):
        price = 4000.0 + index
        engine.push_bar(
            Bar(open=price, high=price + 1, low=price - 1, close=price, volume=10.0),
            moment,
        )
        moment += timedelta(minutes=1)
    assert engine.session_tracker.bars_in_session < 30


# ---------------------------------------------------------------- gaps


def test_a_cold_start_does_not_read_as_extreme_volatility() -> None:
    """The station must not open every run playing breakout music.

    With raw percentile ranks, the first handful of ATR observations are by definition
    the highest ever seen, so a quiet coil at startup reported ~97th-percentile
    volatility and the regime engine classified it as HIGH_VOLATILITY_RANGE.
    """
    engine = FeatureEngine(market_settings(warmup_bars=25))
    produced = feed_scenario(engine, Scenario.LOW_VOLATILITY_RANGE, bars=40, seed=55)
    first_reliable = next((f for f in produced if f.sufficient_history), None)
    assert first_reliable is not None
    assert first_reliable.atr_percentile < 85.0, (
        f"cold start reported {first_reliable.atr_percentile:.1f}th-percentile volatility"
    )


def test_percentile_blending_does_not_suppress_a_genuine_extreme() -> None:
    """Stabilisation must not become deafness once the window is populated.

    Uses one continuous simulator and switches scenario mid-run. Calling
    ``feed_scenario`` twice would restart timestamps at START, so the bar aggregator
    would see the second run as one long out-of-order bucket and close no bars at all.
    """
    engine = FeatureEngine(market_settings(warmup_bars=25))
    simulator = MarketSimulationEngine(start_price=4_000.0, seed=56, ticks_per_bar=10)
    simulator.set_scenario(Scenario.FLAT)
    moment = START
    interval = timedelta(seconds=6)

    for _ in range(140 * 10):
        engine.push_snapshot(simulator.next_tick(moment))
        moment += interval

    simulator.set_scenario(Scenario.VIOLENT_BREAKOUT)
    produced = []
    for _ in range(60 * 10):
        vector = engine.push_snapshot(simulator.next_tick(moment))
        if vector is not None:
            produced.append(vector)
        moment += interval

    assert produced, "no bars closed after the scenario switch"
    assert max(f.atr_percentile for f in produced) > 90.0


def test_a_data_gap_is_counted_and_not_filled() -> None:
    """§86: never invent bars to paper over a weekend or an outage."""
    engine = FeatureEngine(market_settings(warmup_bars=5))
    moment = START
    for _ in range(10):
        engine.push_bar(_flat_bar(), moment)
        moment += timedelta(seconds=60)
    assert engine.gap_count == 0
    # Jump forward an hour.
    engine.push_bar(_flat_bar(), moment + timedelta(hours=1))
    assert engine.gap_count >= 59
    assert engine.bars_observed == 11  # not 71


def test_reset_restores_the_warm_up_state() -> None:
    engine = FeatureEngine(market_settings(warmup_bars=20))
    feed_scenario(engine, Scenario.RANDOM_WALK, bars=140)
    assert engine.sufficient_history
    engine.reset()
    assert engine.bars_observed == 0
    assert not engine.sufficient_history
    assert engine.last_features is None
    assert engine.gap_count == 0


def test_push_bar_rejects_a_naive_timestamp() -> None:
    engine = FeatureEngine(market_settings())
    with pytest.raises(ValueError, match="timezone-aware"):
        engine.push_bar(_flat_bar(), datetime(2026, 1, 1))


# ---------------------------------------------------------------- properties


@given(
    opens=st.lists(
        st.floats(min_value=100.0, max_value=5_000.0, allow_nan=False, allow_infinity=False),
        min_size=40,
        max_size=90,
    )
)
@hypothesis_settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
def test_arbitrary_price_paths_never_produce_invalid_features(opens: list[float]) -> None:
    """Fuzzed input must still satisfy every contract bound.

    Covers paths no scenario generates: instant 50× gaps, repeated identical prices,
    sawtooths. The contracts do the asserting — construction fails if a bound is
    violated.
    """
    engine = FeatureEngine(market_settings(warmup_bars=10))
    moment = START
    for price in opens:
        spread = max(price * 0.0005, 0.01)
        engine.push_bar(
            Bar(open=price, high=price + spread, low=price - spread,
                close=price, volume=10.0),
            moment,
        )
        moment += timedelta(seconds=60)
    features = engine.last_features
    assert features is not None
    assert 0.0 <= features.atr_percentile <= 100.0
    assert math.isfinite(features.moving_average_slope)


# ---------------------------------------------------------------- helpers


def _snapshot(
    at: datetime, price: float, volume: float | None = 10.0
) -> MarketSnapshotV1:
    half = price * 0.00005 / 2.0
    return MarketSnapshotV1(
        symbol="XAUUSD",
        timestamp=at,
        bid=price - half,
        ask=price + half,
        tick_volume=volume,
        synthetic=True,
    )


def _flat_bar() -> Bar:
    return Bar(open=4000.0, high=4001.0, low=3999.0, close=4000.0, volume=10.0)
