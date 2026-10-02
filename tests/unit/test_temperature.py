"""CreativeGovernor (§94, §95, milestone 3.9).

§94: "When queue is low: only safe proven generation. When queue is healthy: allow more
experimental genres/styles. This means artistic risk automatically falls when operational
risk rises."

The property worth testing is the *coupling*, not the individual thresholds: artistic
risk must be a monotone function of operational safety, and no single input may be able to
override that. The adversarial cases are at the end — a full-but-draining buffer, and
§11's diversity pressure trying to buy risk the station cannot afford.
"""

from __future__ import annotations

import pytest

from tradefix_radio.config.schema import GenerationSettings
from tradefix_radio.contracts.enums import GenerationPriority
from tradefix_radio.contracts.queue import BufferHealthV1
from tradefix_radio.director.temperature import (
    CAPACITY_BREAK_EVEN,
    FAILURE_LOCKOUT,
    MAX_TEMPERATURE,
    MIN_TEMPERATURE,
    CreativeGovernor,
)

TARGET_MINUTES = 40.0


def buffer(minutes_ready: float, *, minimum: float = 15.0) -> BufferHealthV1:
    """A buffer with ``minutes_ready`` against a 40-minute target.

    ``minutes_in_flight`` is deliberately zero throughout. Optimistic in-flight work must
    not be able to buy creative risk, and a fixture that quietly supplied some would hide
    that if the implementation ever started counting it.
    """
    return BufferHealthV1(
        minutes_ready=minutes_ready,
        minutes_in_flight=0.0,
        minimum_minutes=minimum,
        target_minutes=TARGET_MINUTES,
        maximum_minutes=90.0,
        ready_count=max(1, int(minutes_ready // 4)),
        in_flight_count=0,
    )


def governor(**overrides: float) -> CreativeGovernor:
    return CreativeGovernor(GenerationSettings(**overrides))  # type: ignore[arg-type]


# ---------------------------------------------------------------- §94 priority


def test_a_critical_buffer_forces_critical_priority() -> None:
    stance = governor().stance(buffer=buffer(3.0))
    assert stance.priority is GenerationPriority.CRITICAL
    assert stance.is_conservative
    assert not stance.allow_experimental
    assert not stance.allow_structural_novelty
    assert "buffer critical" in stance.reason


def test_an_unhealthy_provider_forces_critical_priority_whatever_the_buffer() -> None:
    """A ninety-minute buffer is worthless if nothing can be generated to replace it."""
    stance = governor().stance(buffer=buffer(90.0), provider_healthy=False)
    assert stance.priority is GenerationPriority.CRITICAL
    assert not stance.allow_experimental
    assert "provider unhealthy" in stance.reason


def test_a_buffer_below_minimum_is_high_priority() -> None:
    stance = governor().stance(buffer=buffer(10.0, minimum=15.0))
    assert stance.priority is GenerationPriority.HIGH
    assert stance.is_conservative
    assert not stance.allow_experimental


def test_a_healthy_buffer_earns_experimental_priority() -> None:
    """§94's reward half: a comfortable buffer is permission to be interesting."""
    stance = governor().stance(buffer=buffer(36.0), capacity_ratio=1.4)
    assert stance.priority is GenerationPriority.EXPERIMENTAL
    assert stance.allow_experimental
    assert stance.allow_structural_novelty
    assert not stance.is_conservative


def test_a_mid_buffer_is_normal_priority() -> None:
    stance = governor().stance(buffer=buffer(24.0))
    assert stance.priority is GenerationPriority.NORMAL
    assert stance.allow_experimental  # permitted, just not actively sought
    assert not stance.is_conservative


def test_consecutive_failures_withdraw_experimentation_at_a_full_buffer() -> None:
    """A provider that has failed three times running is not a safe place to experiment.

    This is the case a buffer-only view gets wrong: the queue looks perfect precisely
    because nothing has been consumed yet, while generation is already broken.
    """
    stance = governor().stance(
        buffer=buffer(40.0), capacity_ratio=1.5, consecutive_failures=FAILURE_LOCKOUT
    )
    assert stance.priority is GenerationPriority.HIGH
    assert not stance.allow_experimental
    assert "consecutive provider failures" in stance.reason


def test_one_failure_does_not_withdraw_experimentation() -> None:
    """A single failure is a coincidence; the lockout is about an established pattern."""
    stance = governor().stance(
        buffer=buffer(40.0), capacity_ratio=1.5, consecutive_failures=1
    )
    assert stance.allow_experimental


# ---------------------------------------------------------------- §93 capacity


def test_a_full_but_draining_buffer_does_not_earn_experimental_priority() -> None:
    """§93's direction, not just §26's level.

    A level-only view reports a 40-minute buffer as maximally safe even while generation
    is losing to playback and the buffer is on its way to zero.
    """
    stance = governor().stance(buffer=buffer(40.0), capacity_ratio=0.7)
    assert stance.priority is GenerationPriority.NORMAL
    assert not stance.allow_experimental
    assert "buffer draining" in stance.reason


def test_a_draining_buffer_is_colder_than_the_same_buffer_filling() -> None:
    gov = governor()
    filling = gov.stance(buffer=buffer(32.0), capacity_ratio=1.3)
    draining = gov.stance(buffer=buffer(32.0), capacity_ratio=0.5)
    assert draining.temperature < filling.temperature


def test_break_even_capacity_still_allows_experimentation() -> None:
    """The gate is "losing", not "not winning". Exactly break-even is not a problem."""
    stance = governor().stance(buffer=buffer(36.0), capacity_ratio=CAPACITY_BREAK_EVEN)
    assert stance.priority is GenerationPriority.EXPERIMENTAL
    assert stance.allow_experimental


# ---------------------------------------------------------------- §95 temperature


def test_temperature_rises_monotonically_with_buffer_fill() -> None:
    """§94's core claim, stated as a property rather than a threshold check."""
    gov = governor()
    temperatures = [
        gov.stance(buffer=buffer(minutes)).temperature
        for minutes in (0.0, 5.0, 10.0, 16.0, 22.0, 28.0, 34.0, 40.0)
    ]
    assert temperatures == sorted(temperatures)
    assert temperatures[0] < temperatures[-1]


def test_temperature_stays_within_its_bounds_across_the_whole_input_space() -> None:
    """Brute force over every combination, because the adjustments compose.

    Each individual adjustment is obviously bounded; what is not obvious is that
    draining + failures + diversity pressure applied in sequence cannot walk outside the
    range. A negative temperature would make the weighted sampler meaningless.
    """
    gov = governor()
    for minutes in (0.0, 4.0, 12.0, 20.0, 30.0, 40.0, 80.0):
        for capacity in (0.0, 0.5, 1.0, 2.0):
            for failures in (0, 1, 3, 10):
                for pressure in (0.0, 0.3, 1.0, 2.0):
                    for healthy in (True, False):
                        stance = gov.stance(
                            buffer=buffer(minutes),
                            capacity_ratio=capacity,
                            consecutive_failures=failures,
                            diversity_pressure=pressure,
                            provider_healthy=healthy,
                        )
                        assert MIN_TEMPERATURE <= stance.temperature <= MAX_TEMPERATURE
                        assert 0.0 < stance.duration_reach <= 1.0


def test_temperature_never_reaches_zero() -> None:
    """A fully deterministic director repeats itself forever.

    At temperature zero the strongest candidate always wins, so identical market state
    yields an identical blueprint — and §11's "same blueprint: never repeat" would then
    have to veto every decision rather than occasionally.
    """
    stance = governor().stance(
        buffer=buffer(0.0), capacity_ratio=0.0, consecutive_failures=20
    )
    assert stance.temperature >= MIN_TEMPERATURE
    assert stance.temperature > 0.0


def test_diversity_pressure_raises_temperature() -> None:
    """§11 is the one input that argues *for* risk."""
    gov = governor()
    calm = gov.stance(buffer=buffer(28.0))
    stale = gov.stance(buffer=buffer(28.0), diversity_pressure=0.8)
    assert stale.temperature > calm.temperature
    assert "diversity pressure" in stale.reason


def test_diversity_pressure_cannot_buy_risk_the_station_cannot_afford() -> None:
    """The adversarial case for §94's "artistic risk falls when operational risk rises".

    A stale station with a nearly empty queue has §11 shouting for variety. It must still
    play something safe: pressure is allowed to consume the headroom the other signals
    leave, never to create headroom of its own.
    """
    gov = governor()
    safe_and_stale = gov.stance(
        buffer=buffer(40.0), capacity_ratio=1.4, diversity_pressure=1.0
    )
    starving_and_stale = gov.stance(
        buffer=buffer(2.0), capacity_ratio=0.3, diversity_pressure=1.0
    )
    assert starving_and_stale.temperature < safe_and_stale.temperature
    # And the hard gates are untouched by pressure, which is what makes them gates.
    assert not starving_and_stale.allow_experimental
    assert starving_and_stale.priority is GenerationPriority.CRITICAL


def test_maximum_pressure_at_maximum_safety_reaches_the_ceiling() -> None:
    stance = governor().stance(
        buffer=buffer(40.0), capacity_ratio=2.0, diversity_pressure=1.0
    )
    assert stance.temperature == pytest.approx(MAX_TEMPERATURE)


def test_temperature_is_continuous_across_the_priority_thresholds() -> None:
    """§95 must not make a visible creative jump when the buffer crosses a band edge.

    The four §94 priority bands are coarse; temperature is sampled on a continuum, and
    deriving it from the band would make the station audibly change character the moment
    the buffer ticked past a threshold.
    """
    gov = governor()
    step = 0.25
    previous = gov.stance(buffer=buffer(0.0)).temperature
    worst_jump = 0.0
    minutes = step
    while minutes <= 44.0:
        current = gov.stance(buffer=buffer(minutes)).temperature
        worst_jump = max(worst_jump, abs(current - previous))
        previous = current
        minutes += step
    # A quarter-minute of buffer must not move temperature more than a few percent of
    # its full range.
    assert worst_jump < (MAX_TEMPERATURE - MIN_TEMPERATURE) * 0.05, (
        f"temperature jumps {worst_jump:.3f} over a {step}-minute buffer change; "
        "it is being derived from the discrete priority band"
    )


# ---------------------------------------------------------------- duration reach


def test_duration_reach_contracts_with_the_buffer() -> None:
    """A long track is a commitment: longer to generate, and it holds a slot for longer."""
    gov = governor()
    critical = gov.stance(buffer=buffer(3.0)).duration_reach
    high = gov.stance(buffer=buffer(10.0)).duration_reach
    healthy = gov.stance(buffer=buffer(40.0), capacity_ratio=1.3).duration_reach
    assert critical < high < healthy
    assert healthy == pytest.approx(1.0)


def test_duration_reach_never_collapses_to_zero() -> None:
    """Zero reach would mean the shortest permitted track forever, which is its own fault."""
    assert governor().stance(buffer=buffer(0.0)).duration_reach > 0.0


# ---------------------------------------------------------------- novelty gates


def test_structural_novelty_survives_a_thinner_buffer_than_experimental_genres() -> None:
    """An unusual section order is a smaller bet than a genre with no track record."""
    stance = governor().stance(buffer=buffer(20.0), capacity_ratio=0.8)
    assert stance.allow_structural_novelty
    assert not stance.allow_experimental


def test_a_critical_buffer_blocks_structural_novelty_too() -> None:
    stance = governor().stance(buffer=buffer(2.0))
    assert not stance.allow_structural_novelty


def test_settings_reject_a_critical_ratio_above_the_experimental_one() -> None:
    """Otherwise the bands invert and a thin buffer would license experimentation."""
    with pytest.raises(ValueError, match="critical_fill_ratio must be below"):
        GenerationSettings(critical_fill_ratio=0.9, experimental_fill_ratio=0.5)
