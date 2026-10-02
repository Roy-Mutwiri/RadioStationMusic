"""Rolling windows and percentile normalisation (§6, milestone 2.3).

These are the primitives every market feature is built on, so their edge cases are
the edge cases of the whole engine. Two deserve special attention:

* the **midpoint percentile convention**, without which a flat market reads as a
  record low and drags energy to zero;
* **plateau and degenerate windows**, which occur constantly — a quiet Asian session
  produces long runs of identical ATR.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tradefix_radio.market.rolling import (
    ExponentialSmoother,
    RollingWindow,
    clamp,
    safe_divide,
)

finite_floats = st.floats(
    min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False
)


# ---------------------------------------------------------------- window basics


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        RollingWindow(0)


def test_window_evicts_oldest_at_capacity() -> None:
    window = RollingWindow(3, [1.0, 2.0, 3.0])
    window.push(4.0)
    assert window.values == (2.0, 3.0, 4.0)
    assert len(window) == 3
    assert window.is_full


def test_window_rejects_non_finite_values() -> None:
    """A NaN in a rolling window silently poisons every later statistic."""
    window = RollingWindow(5)
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="non-finite"):
            window.push(bad)
    assert len(window) == 0


def test_empty_window_statistics_are_safe() -> None:
    window = RollingWindow(5)
    assert window.mean == 0.0
    assert window.variance == 0.0
    assert window.stdev == 0.0
    assert window.minimum is None
    assert window.maximum is None
    assert window.median() is None
    assert window.quantile(0.5) is None
    assert window.latest is None
    assert not window


def test_clear_resets_every_accumulator() -> None:
    """Running sums must be reset too, not just the deque."""
    window = RollingWindow(5, [1.0, 2.0, 3.0])
    window.clear()
    assert len(window) == 0
    assert window.mean == 0.0
    window.push(10.0)
    assert window.mean == 10.0


def test_eviction_keeps_running_sums_correct() -> None:
    """The sorted mirror and the running sums must stay in step through eviction."""
    window = RollingWindow(3)
    for value in [5.0, 1.0, 9.0, 2.0, 7.0]:
        window.push(value)
    assert window.values == (9.0, 2.0, 7.0)
    assert window.mean == pytest.approx(6.0)
    assert window.minimum == 2.0
    assert window.maximum == 9.0


def test_duplicate_values_evict_correctly() -> None:
    """The sorted mirror must remove the right instance of a repeated value."""
    window = RollingWindow(3)
    for value in [4.0, 4.0, 4.0, 1.0]:
        window.push(value)
    assert window.values == (4.0, 4.0, 1.0)
    assert window.minimum == 1.0
    assert window.maximum == 4.0


@given(st.lists(finite_floats, min_size=1, max_size=200))
def test_mean_matches_a_direct_computation(values: list[float]) -> None:
    window = RollingWindow(len(values), values)
    assert window.mean == pytest.approx(sum(values) / len(values), rel=1e-9, abs=1e-6)


@given(st.lists(finite_floats, min_size=2, max_size=120))
def test_variance_is_never_negative(values: list[float]) -> None:
    """Running sums can cancel catastrophically; sqrt of a negative must not happen."""
    window = RollingWindow(len(values), values)
    assert window.variance >= 0.0
    assert math.isfinite(window.stdev)


def test_variance_of_a_constant_series_is_zero() -> None:
    window = RollingWindow(50, [7.5] * 50)
    assert window.variance == 0.0
    assert window.stdev == 0.0


# ---------------------------------------------------------------- percentiles


def test_percentile_rank_uses_the_midpoint_convention() -> None:
    """A value sitting among equals must rank as typical, not as an extreme.

    This is the behaviour that keeps a flat market from reading as a record low.
    """
    window = RollingWindow(10, [5.0] * 10)
    assert window.percentile_rank(5.0) == pytest.approx(50.0)


def test_percentile_rank_at_the_extremes() -> None:
    window = RollingWindow(10, [float(i) for i in range(10)])
    assert window.percentile_rank(-100.0) == pytest.approx(0.0)
    assert window.percentile_rank(100.0) == pytest.approx(100.0)


def test_percentile_rank_is_monotone() -> None:
    window = RollingWindow(100, [float(i) for i in range(100)])
    ranks = [window.percentile_rank(float(i)) for i in range(0, 100, 5)]
    assert ranks == sorted(ranks)


def test_percentile_rank_of_an_empty_window_is_neutral() -> None:
    """"No opinion" must not read as "extreme"."""
    assert RollingWindow(10).percentile_rank(42.0) == 50.0


@given(st.lists(finite_floats, min_size=1, max_size=100), finite_floats)
def test_percentile_rank_is_always_in_range(values: list[float], probe: float) -> None:
    window = RollingWindow(len(values), values)
    assert 0.0 <= window.percentile_rank(probe) <= 100.0


@given(st.lists(finite_floats, min_size=1, max_size=100))
def test_percentile_rank_is_invariant_under_positive_affine_rescaling(
    values: list[float],
) -> None:
    """The property §6 depends on: a rank must not care about units.

    Multiplying every observation and the probe by the same positive constant must
    leave the rank unchanged. This is the mechanism by which gold at 1 800 and gold
    at 4 000 produce identical music.
    """
    scale = 10.0
    probe = values[len(values) // 2]
    plain = RollingWindow(len(values), values)
    scaled = RollingWindow(len(values), [value * scale for value in values])
    assert plain.percentile_rank(probe) == pytest.approx(
        scaled.percentile_rank(probe * scale)
    )


def test_stable_rank_is_neutral_on_a_nearly_empty_window() -> None:
    """A rank from four samples is a different quantity, not a weak estimate.

    Without blending, a cold start ranks its first observations as extremes and the
    station opens every run at the wrong energy.
    """
    window = RollingWindow(100, [1.0, 2.0, 3.0, 4.0])
    raw = window.percentile_rank(4.0)
    blended = window.percentile_rank_stable(4.0, min_samples=60)
    assert raw > 80.0
    assert abs(blended - 50.0) < abs(raw - 50.0)
    assert 50.0 < blended < raw


def test_stable_rank_converges_to_the_raw_rank_once_populated() -> None:
    window = RollingWindow(200, [float(i) for i in range(60)])
    assert window.percentile_rank_stable(59.0, min_samples=60) == pytest.approx(
        window.percentile_rank(59.0)
    )


def test_stable_rank_blending_is_monotone_in_sample_count() -> None:
    """Each additional sample must move the rank toward the measured value."""
    previous = 50.0
    for count in range(2, 61, 6):
        window = RollingWindow(100, [float(i) for i in range(count)])
        blended = window.percentile_rank_stable(float(count - 1), min_samples=60)
        assert blended >= previous - 1e-9
        previous = blended


def test_stable_rank_ignores_blending_when_min_samples_is_trivial() -> None:
    window = RollingWindow(100, [1.0, 2.0])
    assert window.percentile_rank_stable(2.0, min_samples=1) == window.percentile_rank(2.0)


@given(st.lists(finite_floats, min_size=1, max_size=60))
def test_stable_rank_is_invariant_under_positive_affine_rescaling(
    values: list[float],
) -> None:
    """Blending must depend only on the sample *count*, never on magnitudes (§6)."""
    scale = 25.0
    probe = values[0]
    plain = RollingWindow(100, values)
    scaled = RollingWindow(100, [value * scale for value in values])
    assert plain.percentile_rank_stable(probe, 60) == pytest.approx(
        scaled.percentile_rank_stable(probe * scale, 60)
    )


def test_quantile_interpolates() -> None:
    window = RollingWindow(5, [0.0, 10.0, 20.0, 30.0, 40.0])
    assert window.quantile(0.0) == pytest.approx(0.0)
    assert window.quantile(0.5) == pytest.approx(20.0)
    assert window.quantile(1.0) == pytest.approx(40.0)
    assert window.quantile(0.25) == pytest.approx(10.0)


def test_quantile_rejects_out_of_range_fractions() -> None:
    window = RollingWindow(3, [1.0, 2.0, 3.0])
    for fraction in (-0.1, 1.1):
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            window.quantile(fraction)


def test_quantile_of_a_single_value() -> None:
    window = RollingWindow(3, [9.0])
    assert window.quantile(0.0) == 9.0
    assert window.quantile(1.0) == 9.0


def test_median_for_even_and_odd_counts() -> None:
    assert RollingWindow(3, [3.0, 1.0, 2.0]).median() == pytest.approx(2.0)
    assert RollingWindow(4, [4.0, 1.0, 3.0, 2.0]).median() == pytest.approx(2.5)


# ---------------------------------------------------------------- position & slope


def test_normalised_position_spans_min_to_max() -> None:
    window = RollingWindow(5, [10.0, 20.0, 30.0])
    assert window.normalised_position(10.0) == pytest.approx(0.0)
    assert window.normalised_position(30.0) == pytest.approx(1.0)
    assert window.normalised_position(20.0) == pytest.approx(0.5)


def test_normalised_position_is_neutral_for_a_degenerate_window() -> None:
    """One bar into a session, or a dead-flat market. 0 or 1 would be a false signal."""
    assert RollingWindow(5, [5.0]).normalised_position(5.0) == 0.5
    assert RollingWindow(5, [5.0, 5.0, 5.0]).normalised_position(5.0) == 0.5


def test_normalised_position_clamps_outside_the_window() -> None:
    window = RollingWindow(5, [10.0, 20.0])
    assert window.normalised_position(-50.0) == 0.0
    assert window.normalised_position(500.0) == 1.0


def test_slope_is_positive_for_a_rising_series() -> None:
    window = RollingWindow(10, [float(i) for i in range(10)])
    assert window.slope_per_step() == pytest.approx(1.0)


def test_slope_is_negative_for_a_falling_series() -> None:
    window = RollingWindow(10, [float(10 - i) for i in range(10)])
    assert window.slope_per_step() == pytest.approx(-1.0)


def test_slope_is_zero_for_a_flat_series() -> None:
    assert RollingWindow(10, [4.0] * 10).slope_per_step() == pytest.approx(0.0)


def test_slope_resists_a_single_endpoint_spike() -> None:
    """Least squares rather than (last - first): one spike must not define a trend."""
    flat_with_spike = [1.0] * 19 + [100.0]
    endpoint_estimate = (flat_with_spike[-1] - flat_with_spike[0]) / (len(flat_with_spike) - 1)
    least_squares = RollingWindow(20, flat_with_spike).slope_per_step()
    assert least_squares < endpoint_estimate


def test_slope_of_a_short_window_is_zero() -> None:
    assert RollingWindow(5, [3.0]).slope_per_step() == 0.0
    assert RollingWindow(5).slope_per_step() == 0.0


# ---------------------------------------------------------------- smoother


def test_smoother_rejects_an_invalid_alpha() -> None:
    for alpha in (0.0, -0.5, 1.5):
        with pytest.raises(ValueError, match=r"\(0, 1\]"):
            ExponentialSmoother(alpha)


def test_smoother_adopts_the_first_value_verbatim() -> None:
    """Seeding from zero would make the station open every run at minimum energy."""
    smoother = ExponentialSmoother(0.1)
    assert smoother.push(80.0) == pytest.approx(80.0)


def test_smoother_converges_toward_a_constant_input() -> None:
    smoother = ExponentialSmoother(0.3, initial=0.0)
    for _ in range(200):
        smoother.push(50.0)
    assert smoother.value == pytest.approx(50.0, abs=1e-6)


def test_smaller_alpha_smooths_more() -> None:
    gentle = ExponentialSmoother(0.05, initial=0.0)
    sharp = ExponentialSmoother(0.5, initial=0.0)
    for _ in range(5):
        gentle.push(100.0)
        sharp.push(100.0)
    assert gentle.value is not None
    assert sharp.value is not None
    assert gentle.value < sharp.value


def test_alpha_one_is_a_passthrough() -> None:
    smoother = ExponentialSmoother(1.0, initial=0.0)
    assert smoother.push(37.0) == pytest.approx(37.0)
    assert smoother.push(12.0) == pytest.approx(12.0)


def test_smoother_rejects_non_finite_input() -> None:
    smoother = ExponentialSmoother(0.2)
    with pytest.raises(ValueError, match="non-finite"):
        smoother.push(float("nan"))


def test_smoother_reset_clears_state() -> None:
    smoother = ExponentialSmoother(0.2)
    smoother.push(10.0)
    smoother.reset()
    assert smoother.value is None
    assert smoother.push(90.0) == pytest.approx(90.0)


# ---------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("value", "expected"),
    [(-10.0, 0.0), (0.0, 0.0), (50.0, 50.0), (100.0, 100.0), (150.0, 100.0)],
)
def test_clamp_constrains_to_the_range(value: float, expected: float) -> None:
    assert clamp(value) == expected


def test_clamp_maps_non_finite_to_the_floor() -> None:
    """Conservative programming beats crashing the playout process."""
    assert clamp(float("nan")) == 0.0
    assert clamp(float("inf")) == 0.0
    assert clamp(float("nan"), low=10.0, high=20.0) == 10.0


def test_safe_divide_handles_zero_and_non_finite_denominators() -> None:
    """A flat series has zero range; division by it is the normal path."""
    assert safe_divide(1.0, 0.0) == 0.0
    assert safe_divide(1.0, 0.0, default=5.0) == 5.0
    assert safe_divide(1.0, float("nan")) == 0.0
    assert safe_divide(6.0, 3.0) == pytest.approx(2.0)


def test_safe_divide_rejects_an_overflowing_result() -> None:
    assert safe_divide(1e308, 1e-308, default=-1.0) == -1.0
