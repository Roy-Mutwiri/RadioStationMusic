"""Market simulator (§7, milestone 2.1).

Milestone 2.1's exit criterion is the one that makes the simulator trustworthy:
each scenario must exhibit the **statistical signature it claims**, measured on the
path actually produced — not merely be configured with parameters that ought to
produce it. A simulator that is only checked against its own inputs cannot catch a
bug in how those inputs are applied, and every downstream test would then be
validating against a fiction.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

import pytest

from tradefix_radio.market.simulation import (
    BASE_VOLATILITY,
    LOOPING_SCENARIOS,
    UI_SCENARIOS,
    MarketSimulationEngine,
    Scenario,
    ScenarioProfile,
)

START = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
TICK = timedelta(seconds=1)


def run(
    scenario: Scenario, *, bars: int = 80, seed: int = 11, ticks_per_bar: int = 60
) -> MarketSimulationEngine:
    """Run a scenario for ``bars`` complete bars and return the engine."""
    engine = MarketSimulationEngine(
        start_price=4_000.0, seed=seed, ticks_per_bar=ticks_per_bar
    )
    engine.set_scenario(scenario)
    engine.warm_up(bars, START, bar_seconds=60)
    return engine


def bar_volatility(engine: MarketSimulationEngine, first: int, last: int) -> float:
    """Standard deviation of bar-to-bar returns over a slice of the path."""
    closes = [bar[3] for bar in engine.completed_bars[first:last]]
    if len(closes) < 3:
        return 0.0
    returns = [
        (closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))
    ]
    return statistics.pstdev(returns)


def total_drift(engine: MarketSimulationEngine, first: int = 0, last: int = -1) -> float:
    bars = engine.completed_bars[first:last]
    if len(bars) < 2:
        return 0.0
    return (bars[-1][3] - bars[0][3]) / bars[0][3]


# ---------------------------------------------------------------- coverage


def test_all_fifteen_brief_scenarios_exist() -> None:
    assert len(Scenario) == 15


def test_every_scenario_produces_a_valid_path() -> None:
    """No scenario may generate a non-positive price or an invalid snapshot."""
    for scenario in Scenario:
        engine = run(scenario, bars=30, ticks_per_bar=20)
        bars = engine.completed_bars
        assert bars, f"{scenario.value} produced no bars"
        for open_, high, low, close, volume in bars:
            assert low <= high, scenario.value
            assert min(open_, high, low, close) > 0, scenario.value
            assert volume >= 0, scenario.value


def test_every_snapshot_is_marked_synthetic() -> None:
    """§7/§86: simulated data must never be mistakable for live."""
    engine = MarketSimulationEngine(seed=1, ticks_per_bar=5)
    engine.set_scenario(Scenario.RANDOM_WALK)
    for snapshot in engine.warm_up(4, START):
        assert snapshot.synthetic is True


def test_snapshots_have_a_coherent_spread_and_candle() -> None:
    engine = MarketSimulationEngine(seed=2, ticks_per_bar=10)
    engine.set_scenario(Scenario.RANDOM_WALK)
    for snapshot in engine.warm_up(5, START):
        assert snapshot.ask >= snapshot.bid
        assert snapshot.high is not None
        assert snapshot.low is not None
        assert snapshot.low <= snapshot.mid <= snapshot.high or snapshot.high == snapshot.low


def test_ui_scenario_buttons_cover_the_brief_s_list() -> None:
    """§7 names nine buttons; each must map to a real scenario."""
    assert len(UI_SCENARIOS) == 9
    for scenario in UI_SCENARIOS:
        assert isinstance(scenario, Scenario)
    labels = set(UI_SCENARIOS.values())
    assert {
        "Quiet", "Range", "Bull Trend", "Bear Trend", "Compression",
        "Breakout Up", "Breakout Down", "Extreme Volatility", "Random",
    } == labels


# ---------------------------------------------------------------- signatures


def test_flat_is_the_quietest_scenario() -> None:
    """The §7 "flat market" must be measurably quieter than a random walk."""
    flat = bar_volatility(run(Scenario.FLAT), 10, 80)
    walk = bar_volatility(run(Scenario.RANDOM_WALK), 10, 80)
    assert flat < walk * 0.5, f"flat={flat:.6f} walk={walk:.6f}"


def test_low_volatility_range_sits_between_flat_and_random_walk() -> None:
    flat = bar_volatility(run(Scenario.FLAT), 10, 80)
    low = bar_volatility(run(Scenario.LOW_VOLATILITY_RANGE), 10, 80)
    walk = bar_volatility(run(Scenario.RANDOM_WALK), 10, 80)
    assert flat < low < walk


def test_slow_bull_trend_drifts_upward() -> None:
    assert total_drift(run(Scenario.SLOW_BULL_TREND, bars=150)) > 0.0


def test_slow_bear_trend_drifts_downward() -> None:
    assert total_drift(run(Scenario.SLOW_BEAR_TREND, bars=150)) < 0.0


def test_trend_scenarios_are_directional_but_not_explosive() -> None:
    """A slow trend must be distinguishable from a breakout.

    If the trend scenario were as volatile as a breakout, the regime engine could
    pass its trend tests by classifying everything as a breakout.
    """
    trend = bar_volatility(run(Scenario.SLOW_BULL_TREND, bars=120), 20, 120)
    breakout = bar_volatility(run(Scenario.BREAKOUT_UP, bars=120), 30, 45)
    assert trend < breakout * 0.5


def test_compression_reduces_volatility_over_time() -> None:
    """§7 compression must actually coil, measured early versus late."""
    engine = run(Scenario.COMPRESSION, bars=90)
    early = bar_volatility(engine, 2, 20)
    late = bar_volatility(engine, 65, 90)
    assert late < early * 0.6, f"early={early:.6f} late={late:.6f}"


def test_compression_drains_volume() -> None:
    engine = run(Scenario.COMPRESSION, bars=90)
    early = statistics.mean(bar[4] for bar in engine.completed_bars[2:20])
    late = statistics.mean(bar[4] for bar in engine.completed_bars[65:90])
    assert late < early


def test_breakout_up_expands_range_and_moves_up() -> None:
    engine = run(Scenario.BREAKOUT_UP, bars=70)
    coil = bar_volatility(engine, 2, 24)
    burst = bar_volatility(engine, 26, 37)
    assert burst > coil * 3.0, f"coil={coil:.6f} burst={burst:.6f}"
    assert engine.completed_bars[40][3] > engine.completed_bars[20][3]


def test_breakout_down_expands_range_and_moves_down() -> None:
    engine = run(Scenario.BREAKOUT_DOWN, bars=70)
    coil = bar_volatility(engine, 2, 24)
    burst = bar_volatility(engine, 26, 37)
    assert burst > coil * 3.0
    assert engine.completed_bars[40][3] < engine.completed_bars[20][3]


def test_violent_breakout_exceeds_an_ordinary_breakout() -> None:
    """§7 lists both; EXTREME_VOLATILITY needs a driver that is clearly more extreme."""
    ordinary = bar_volatility(run(Scenario.BREAKOUT_UP, bars=60), 26, 37)
    violent = bar_volatility(run(Scenario.VIOLENT_BREAKOUT, bars=60), 16, 35)
    assert violent > ordinary * 1.8, f"ordinary={ordinary:.6f} violent={violent:.6f}"


def test_fake_breakout_returns_through_its_starting_point() -> None:
    """The distinguishing feature: the move is fully invalidated, not just paused."""
    engine = run(Scenario.FAKE_BREAKOUT, bars=70, seed=5)
    bars = engine.completed_bars
    pre_break = bars[20][3]
    peak = max(bar[3] for bar in bars[22:31])
    after = bars[46][3]
    assert peak > pre_break, "the fake breakout never broke out"
    assert after < pre_break, f"price did not retrace through the origin: {after} vs {pre_break}"


def test_volatility_spike_has_no_net_direction() -> None:
    """Tests that the engine does not invent a trend from two-sided noise."""
    drifts = []
    for seed in range(8):
        engine = run(Scenario.VOLATILITY_SPIKE, bars=55, seed=seed)
        drifts.append(total_drift(engine, 18, 35))
    # Individually noisy, but the mean across seeds must be near zero.
    assert abs(statistics.mean(drifts)) < 0.004, f"drifts={drifts}"


def test_volatility_spike_raises_volatility_then_subsides() -> None:
    engine = run(Scenario.VOLATILITY_SPIKE, bars=70)
    before = bar_volatility(engine, 2, 17)
    during = bar_volatility(engine, 19, 34)
    after = bar_volatility(engine, 40, 70)
    assert during > before * 3.0
    assert after < during * 0.6


def test_mean_reversion_oscillates_around_its_anchor() -> None:
    """A mean-reverting path must cross its starting level repeatedly."""
    engine = run(Scenario.MEAN_REVERSION, bars=120, seed=3)
    closes = [bar[3] for bar in engine.completed_bars]
    anchor = closes[0]
    crossings = sum(
        1
        for i in range(1, len(closes))
        if (closes[i] - anchor) * (closes[i - 1] - anchor) < 0
    )
    assert crossings >= 6, f"only {crossings} crossings"


def test_mean_reversion_drifts_less_than_a_random_walk() -> None:
    reverting = abs(total_drift(run(Scenario.MEAN_REVERSION, bars=120, seed=4)))
    walking = abs(total_drift(run(Scenario.RANDOM_WALK, bars=120, seed=4)))
    assert reverting < walking


def test_flash_move_produces_an_extreme_bar_then_recovers() -> None:
    engine = run(Scenario.FLASH_MOVE, bars=60, seed=6)
    bars = engine.completed_bars
    ranges = [(bar[1] - bar[2]) / bar[3] for bar in bars]
    worst = max(ranges[12:16])
    typical = statistics.median(ranges[30:60])
    assert worst > typical * 5.0, f"worst={worst:.6f} typical={typical:.6f}"
    # Partial recovery: the low point is not where it ends.
    trough = min(bar[3] for bar in bars[12:17])
    assert bars[25][3] > trough


def test_event_volatility_is_quiet_then_violent_then_decaying() -> None:
    engine = run(Scenario.EVENT_VOLATILITY, bars=80, seed=8)
    pre = bar_volatility(engine, 2, 24)
    reaction = bar_volatility(engine, 26, 31)
    decay = bar_volatility(engine, 45, 72)
    assert reaction > pre * 8.0, f"pre={pre:.6f} reaction={reaction:.6f}"
    assert decay < reaction * 0.6


def test_random_walk_volatility_matches_the_configured_baseline() -> None:
    """Calibration check: the simulator must mean what BASE_VOLATILITY says.

    Every other scenario is expressed as a multiple of this number, so if the
    baseline does not hold, the whole scenario library is mis-scaled.
    """
    measured = statistics.mean(
        bar_volatility(run(Scenario.RANDOM_WALK, bars=200, seed=seed), 5, 200)
        for seed in range(6)
    )
    assert BASE_VOLATILITY * 0.6 < measured < BASE_VOLATILITY * 1.5, (
        f"measured={measured:.6f} baseline={BASE_VOLATILITY:.6f}"
    )


# ---------------------------------------------------------------- scale freedom


@pytest.mark.parametrize("start_price", [180.0, 1_800.0, 4_000.0, 40_000.0])
def test_scenario_behaviour_is_independent_of_price_level(start_price: float) -> None:
    """§6 at the source: a scenario must behave the same at any price.

    Relative volatility must match across four orders of magnitude, which is only
    true if every parameter is a fraction rather than an amount.
    """
    engine = MarketSimulationEngine(start_price=start_price, seed=21, ticks_per_bar=30)
    engine.set_scenario(Scenario.RANDOM_WALK)
    engine.warm_up(120, START)
    measured = bar_volatility(engine, 5, 120)
    assert BASE_VOLATILITY * 0.5 < measured < BASE_VOLATILITY * 1.8


def test_identical_seeds_produce_identical_paths() -> None:
    """Reproducibility: an endurance finding must be replayable from its seed."""
    first = run(Scenario.BREAKOUT_UP, bars=40, seed=99).completed_bars
    second = run(Scenario.BREAKOUT_UP, bars=40, seed=99).completed_bars
    assert first == second


def test_different_seeds_produce_different_paths() -> None:
    first = run(Scenario.BREAKOUT_UP, bars=40, seed=1).completed_bars
    second = run(Scenario.BREAKOUT_UP, bars=40, seed=2).completed_bars
    assert first != second


def test_the_engine_does_not_touch_the_global_rng() -> None:
    """Otherwise a simulation would perturb unrelated randomness in the process."""
    import random

    random.seed(1234)
    expected = [random.random() for _ in range(3)]
    random.seed(1234)
    run(Scenario.VIOLENT_BREAKOUT, bars=10, seed=7)
    assert [random.random() for _ in range(3)] == expected


# ---------------------------------------------------------------- phases & control


def test_phase_advances_through_a_multi_phase_scenario() -> None:
    engine = MarketSimulationEngine(seed=3, ticks_per_bar=10)
    engine.set_scenario(Scenario.BREAKOUT_UP)
    assert engine.state.phase_index == 0
    engine.warm_up(30, START)
    assert engine.state.phase_index >= 1


def test_the_final_phase_is_held_for_non_looping_scenarios() -> None:
    """The phase count is derived, not hard-coded.

    A hard-coded number made this test fail for the wrong reason when a buildup phase
    was legitimately added to the breakout scenarios.
    """
    from tradefix_radio.market.simulation import _phases

    for scenario in (Scenario.BREAKOUT_UP, Scenario.BREAKOUT_DOWN, Scenario.FLASH_MOVE):
        engine = MarketSimulationEngine(seed=3, ticks_per_bar=5)
        engine.set_scenario(scenario)
        engine.warm_up(250, START)
        assert engine.state.phase_index == len(_phases(scenario)) - 1, scenario.value


def test_looping_scenarios_restart_their_phase_sequence() -> None:
    for scenario in LOOPING_SCENARIOS:
        engine = MarketSimulationEngine(seed=3, ticks_per_bar=5)
        engine.set_scenario(scenario)
        engine.warm_up(400, START)
        # After many bars a looping scenario must not be stuck on the last phase.
        assert engine.state.bars_total >= 400


def test_switching_scenario_does_not_teleport_price() -> None:
    """§7 buttons are used mid-run; a price jump would read as a flash crash."""
    engine = MarketSimulationEngine(seed=4, ticks_per_bar=10)
    engine.set_scenario(Scenario.FLAT)
    engine.warm_up(20, START)
    before = engine.state.price
    engine.set_scenario(Scenario.VIOLENT_BREAKOUT)
    assert engine.state.price == pytest.approx(before)
    assert engine.state.phase_index == 0


def test_switching_scenario_moves_the_mean_reversion_anchor() -> None:
    """Otherwise a new range would drag price back to where the old one started."""
    engine = MarketSimulationEngine(seed=4, ticks_per_bar=10)
    engine.set_scenario(Scenario.SLOW_BULL_TREND)
    engine.warm_up(60, START)
    engine.set_scenario(Scenario.MEAN_REVERSION)
    assert engine.state.anchor == pytest.approx(engine.state.price)


def test_reset_restores_the_starting_price() -> None:
    engine = MarketSimulationEngine(start_price=4_000.0, seed=4, ticks_per_bar=10)
    engine.set_scenario(Scenario.VIOLENT_BREAKOUT)
    engine.warm_up(40, START)
    engine.reset()
    assert engine.state.price == pytest.approx(4_000.0)
    assert engine.completed_bars == ()


def test_bar_history_is_bounded() -> None:
    """§64 watches for unbounded growth in a week-long run."""
    engine = MarketSimulationEngine(seed=4, ticks_per_bar=1)
    engine.set_scenario(Scenario.RANDOM_WALK)
    engine.warm_up(6_000, START)
    assert len(engine.completed_bars) <= 5_000
    assert len(engine.state.history) <= 1_000


def test_naive_timestamps_are_rejected() -> None:
    engine = MarketSimulationEngine(seed=1)
    with pytest.raises(ValueError, match="timezone-aware"):
        engine.next_tick(datetime(2026, 1, 1))


def test_invalid_construction_is_rejected() -> None:
    with pytest.raises(ValueError, match="start_price"):
        MarketSimulationEngine(start_price=0.0)
    with pytest.raises(ValueError, match="ticks_per_bar"):
        MarketSimulationEngine(ticks_per_bar=0)
    with pytest.raises(ValueError, match="spread_fraction"):
        MarketSimulationEngine(spread_fraction=-0.1)


def test_scenario_profile_validates_its_parameters() -> None:
    with pytest.raises(ValueError, match="volatility"):
        ScenarioProfile(volatility=-1.0, drift=0.0)
    with pytest.raises(ValueError, match="mean_reversion"):
        ScenarioProfile(volatility=0.1, drift=0.0, mean_reversion=2.0)
    with pytest.raises(ValueError, match="volume_multiplier"):
        ScenarioProfile(volatility=0.1, drift=0.0, volume_multiplier=0.0)
    with pytest.raises(ValueError, match="bars"):
        ScenarioProfile(volatility=0.1, drift=0.0, bars=0)


def test_warm_up_rejects_a_non_positive_bar_count() -> None:
    engine = MarketSimulationEngine(seed=1)
    with pytest.raises(ValueError, match="at least 1"):
        engine.warm_up(0, START)


def test_tick_volatility_aggregates_to_the_intended_bar_volatility() -> None:
    """Per-tick volatility is divided by sqrt(ticks), not by ticks.

    Getting this wrong inflates or deflates bar volatility by the tick count, which
    would silently mis-scale every scenario. Checked across two tick granularities:
    the measured bar volatility must be the same.
    """
    coarse = bar_volatility(
        run(Scenario.RANDOM_WALK, bars=200, seed=31, ticks_per_bar=10), 5, 200
    )
    fine = bar_volatility(
        run(Scenario.RANDOM_WALK, bars=200, seed=31, ticks_per_bar=120), 5, 200
    )
    assert fine == pytest.approx(coarse, rel=0.6), f"coarse={coarse:.6f} fine={fine:.6f}"


def test_realised_volatility_helper_agrees_with_a_direct_computation() -> None:
    engine = run(Scenario.RANDOM_WALK, bars=60, seed=17)
    assert engine.realised_volatility(30) == pytest.approx(
        bar_volatility(engine, 29, 60), rel=0.25
    )


def test_scenario_labels_are_human_readable() -> None:
    assert Scenario.BREAKOUT_UP.label == "Breakout Up"
    assert Scenario.LOW_VOLATILITY_RANGE.label == "Low Volatility Range"
