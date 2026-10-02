"""RadioEnergyPlanner (§98, milestone 3.5).

§98: "A station should not constantly oscillate between extremes... Smooth transitions
unless market conditions justify sharp change."

Both halves are tested. The smoothing half is obvious; the **escape hatch** half is the one
a careless implementation gets wrong, because a planner that only ever smoothed would make
the station unresponsive to the single event it exists to react to (§29).
"""

from __future__ import annotations

import pytest

from tradefix_radio.director.energy_curve import (
    DEFAULT_MAX_STEP,
    MAX_CONSECUTIVE_PEAKS,
    PEAK_THRESHOLD,
    SHARP_CHANGE_GAP,
    SHARP_CHANGE_STEP_MULTIPLIER,
    RadioEnergyPlanner,
)


def planner(**overrides: object) -> RadioEnergyPlanner:
    return RadioEnergyPlanner(**overrides)  # type: ignore[arg-type]


# ---------------------------------------------------------------- first track


def test_the_first_track_adopts_market_energy_directly() -> None:
    """Converging from a neutral 50 would open the station at the wrong energy."""
    plan = planner().plan(market_energy=85.0)
    assert plan.target_energy == pytest.approx(85.0)
    assert plan.previous_energy is None
    assert plan.step == 0.0
    assert "first track" in plan.reason


def test_no_energy_before_the_first_plan() -> None:
    assert planner().current_energy is None


def test_energy_is_tracked_after_planning() -> None:
    engine = planner()
    engine.plan(market_energy=60.0)
    assert engine.current_energy == pytest.approx(60.0)


# ---------------------------------------------------------------- smoothing


def test_a_small_move_is_followed_exactly() -> None:
    engine = planner(max_step=14.0)
    engine.plan(market_energy=50.0)
    plan = engine.plan(market_energy=58.0)
    assert plan.target_energy == pytest.approx(58.0)
    assert not plan.sharp_change
    assert "normal step allowance" in plan.reason


def test_a_large_move_is_step_limited() -> None:
    """§98: no whiplash. One track moves at most one BPM band's worth."""
    engine = planner(max_step=14.0, sharp_change_gap=40.0)
    engine.plan(market_energy=30.0)
    plan = engine.plan(market_energy=60.0)
    assert plan.target_energy == pytest.approx(44.0)
    assert plan.step == pytest.approx(14.0)
    assert "step limited" in plan.reason


def test_the_step_limit_applies_downward_too() -> None:
    engine = planner(max_step=14.0, sharp_change_gap=40.0)
    engine.plan(market_energy=70.0)
    plan = engine.plan(market_energy=40.0)
    assert plan.target_energy == pytest.approx(56.0)
    assert plan.step == pytest.approx(-14.0)


def test_sustained_market_energy_is_reached_over_several_tracks() -> None:
    """Stability must not become deafness: the station gets there, just not in one jump."""
    engine = planner(max_step=14.0, sharp_change_gap=40.0, max_consecutive_peaks=99)
    engine.plan(market_energy=20.0)
    for _ in range(8):
        plan = engine.plan(market_energy=70.0)
    assert plan.target_energy == pytest.approx(70.0, abs=1.0)


def test_energy_never_leaves_the_valid_range() -> None:
    engine = planner()
    for market in (0.0, 100.0, 0.0, 100.0, 50.0):
        for _ in range(6):
            plan = engine.plan(market_energy=market, energy_velocity=20.0)
            assert 0.0 <= plan.target_energy <= 100.0


# ---------------------------------------------------------------- sharp change


def test_a_genuine_market_shift_lifts_the_step_limit() -> None:
    """§29: gold suddenly breaks out and the programming must change."""
    engine = planner(max_step=14.0, sharp_change_gap=28.0)
    engine.plan(market_energy=25.0)
    plan = engine.plan(market_energy=90.0)
    assert plan.sharp_change
    assert plan.step > 14.0
    assert "sharp-change threshold" in plan.reason


def test_a_sharp_change_still_does_not_jump_all_the_way() -> None:
    """Even a real breakout sounds better as a decisive step than a jump cut.

    The bound is what makes §29 and §98 compatible: decisively more than an ordinary
    step, and still bounded, so a one-track spike cannot swing the station across the
    dial. See SHARP_CHANGE_STEP_MULTIPLIER.
    """
    engine = planner(max_step=14.0, sharp_change_gap=28.0)
    engine.plan(market_energy=20.0)
    plan = engine.plan(market_energy=95.0)
    assert plan.sharp_change
    assert plan.target_energy < 95.0
    assert plan.step > 14.0
    assert plan.step <= 14.0 * SHARP_CHANGE_STEP_MULTIPLIER


def test_a_sustained_move_is_fully_tracked_within_a_few_tracks() -> None:
    """Bounding the sharp step must not make the station unresponsive (§29).

    The cap limits one track, not the sequence: a market that moves and *stays* moved is
    caught up with quickly, because the remaining gap keeps firing sharp changes in the
    same direction.
    """
    engine = planner(max_step=14.0, sharp_change_gap=28.0)
    engine.plan(market_energy=20.0)
    tracks = 0
    while tracks < 10:
        tracks += 1
        plan = engine.plan(market_energy=95.0)
        if plan.target_energy >= 94.0:
            break
    assert plan.target_energy >= 94.0
    assert tracks <= 5, f"took {tracks} tracks to track a sustained 75-point move"


def test_a_sharp_change_may_not_immediately_reverse_itself() -> None:
    """The fix for the §98 violation the oscillation test found.

    Without this the escape hatch defeats the smoothing: an alternating market triggers it
    every track and the station swings harder than it would with no escape hatch at all.
    """
    engine = planner(max_step=10.0, sharp_change_gap=28.0, sharp_change_cooldown_tracks=3)
    engine.plan(market_energy=20.0)
    up = engine.plan(market_energy=90.0)
    assert up.sharp_change

    down = engine.plan(market_energy=10.0)
    assert not down.sharp_change
    assert down.sharp_change_suppressed
    assert abs(down.step) == pytest.approx(10.0)
    assert "oscillating" in down.reason


def test_a_sharp_change_in_the_same_direction_is_still_allowed() -> None:
    """Suppression is about reversal, not about frequency."""
    engine = planner(max_step=10.0, sharp_change_gap=28.0, sharp_change_cooldown_tracks=3)
    engine.plan(market_energy=5.0)
    first = engine.plan(market_energy=40.0)
    assert first.sharp_change
    second = engine.plan(market_energy=95.0)
    assert second.sharp_change


def test_a_reversal_is_allowed_once_the_cooldown_passes() -> None:
    """A genuine reversal must still be reachable — stability, not deafness.

    The market rises, the station catches up and sits there for longer than the cooldown,
    and only then does the market collapse. That is a real reversal, not thrashing, and
    the escape hatch must fire.
    """
    engine = planner(max_step=10.0, sharp_change_gap=28.0, sharp_change_cooldown_tracks=2)
    engine.plan(market_energy=20.0)
    # Hold at 90 until the station has arrived and no sharp change has fired for longer
    # than the cooldown. Each same-direction sharp change restarts the cooldown, so the
    # settled tracks have to come after the station stops climbing.
    quiet = 0
    for _ in range(20):
        plan = engine.plan(market_energy=90.0)
        quiet = 0 if plan.sharp_change else quiet + 1
        if quiet > 2:
            break
    assert quiet > 2, "station never settled at the elevated market energy"

    reversal = engine.plan(market_energy=10.0)
    assert reversal.sharp_change
    assert not reversal.sharp_change_suppressed
    assert reversal.step < -10.0


def test_a_gap_just_below_the_threshold_is_not_sharp() -> None:
    engine = planner(max_step=14.0, sharp_change_gap=30.0)
    engine.plan(market_energy=40.0)
    plan = engine.plan(market_energy=69.0)
    assert not plan.sharp_change
    assert plan.step == pytest.approx(14.0)


def test_the_sharp_change_gap_must_exceed_the_step_limit() -> None:
    """Otherwise every step qualifies as sharp and the limit never applies."""
    with pytest.raises(ValueError, match="sharp_change_gap must exceed max_step"):
        RadioEnergyPlanner(max_step=20.0, sharp_change_gap=20.0)


# ---------------------------------------------------------------- velocity


def test_rising_velocity_pushes_energy_above_the_market() -> None:
    """§6: "music can prepare for rising market intensity"."""
    engine = planner()
    with_velocity = engine.plan(market_energy=50.0, energy_velocity=3.0)
    engine.reset()
    without = engine.plan(market_energy=50.0, energy_velocity=0.0)
    assert with_velocity.target_energy > without.target_energy
    assert with_velocity.velocity_contribution > 0


def test_falling_velocity_pulls_energy_below_the_market() -> None:
    engine = planner()
    plan = engine.plan(market_energy=50.0, energy_velocity=-3.0)
    assert plan.velocity_contribution < 0
    assert plan.target_energy < 50.0


def test_velocity_influence_is_bounded() -> None:
    """Velocity is a leading indicator; overweighting it would lurch on noise."""
    engine = planner()
    plan = engine.plan(market_energy=50.0, energy_velocity=1000.0)
    assert plan.velocity_contribution <= 12.0


# ---------------------------------------------------------------- peak relief


def test_a_run_of_peaks_is_eventually_broken() -> None:
    """§98 forbids sitting at an extreme; a long violent session is not an hour at 95."""
    engine = planner(max_consecutive_peaks=3, peak_threshold=78.0)
    engine.plan(market_energy=95.0)
    plans = [engine.plan(market_energy=95.0) for _ in range(4)]
    assert any(plan.peak_relief for plan in plans)
    relieved = next(plan for plan in plans if plan.peak_relief)
    assert relieved.target_energy < 78.0
    assert "consecutive peak" in relieved.reason


def test_the_peak_counter_resets_below_the_threshold() -> None:
    engine = planner(max_consecutive_peaks=3, peak_threshold=78.0)
    engine.plan(market_energy=95.0)
    engine.plan(market_energy=95.0)
    assert engine.consecutive_peaks == 2
    engine.plan(market_energy=20.0)
    assert engine.consecutive_peaks == 0


def test_peak_relief_does_not_fire_below_the_threshold() -> None:
    engine = planner(max_consecutive_peaks=2, peak_threshold=78.0)
    for _ in range(8):
        plan = engine.plan(market_energy=50.0)
        assert not plan.peak_relief


def test_max_consecutive_peaks_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_consecutive_peaks"):
        RadioEnergyPlanner(max_consecutive_peaks=0)


def test_max_step_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_step"):
        RadioEnergyPlanner(max_step=0.0)


# ---------------------------------------------------------------- divergence


def test_divergence_pressure_widens_the_step_allowance() -> None:
    """§11: an unusually large energy move is itself a form of variety."""
    engine = planner(max_step=10.0, sharp_change_gap=40.0)
    engine.plan(market_energy=30.0)
    normal = engine.plan(market_energy=60.0)
    engine.reset()
    engine.plan(market_energy=30.0)
    diverging = engine.plan(market_energy=60.0, divergence_strength=1.0)
    assert diverging.step > normal.step
    assert "divergence pressure" in diverging.reason


def test_zero_divergence_leaves_the_allowance_unchanged() -> None:
    engine = planner(max_step=10.0, sharp_change_gap=40.0)
    engine.plan(market_energy=30.0)
    plan = engine.plan(market_energy=60.0, divergence_strength=0.0)
    assert plan.step == pytest.approx(10.0)


# ---------------------------------------------------------------- §98 property


def test_an_oscillating_market_does_not_produce_oscillating_programming() -> None:
    """§98's headline requirement, tested adversarially.

    The market alternates between 5 and 95 every single track. Following it would swing
    station energy by 90 points per track; the planner must not.
    """
    engine = planner()
    targets: list[float] = []
    for index in range(40):
        targets.append(engine.plan(market_energy=5.0 if index % 2 else 95.0).target_energy)

    steps = [abs(targets[i] - targets[i - 1]) for i in range(1, len(targets))]
    worst = max(steps)
    assert worst <= SHARP_CHANGE_GAP, f"largest step {worst:.1f} exceeds the sharp-change gap"

    # Once the planner has recognised the market as thrashing, the escape hatch stays shut
    # and only the ordinary step limit applies. Two tracks is enough to recognise it: one
    # sharp change, then the first suppressed reversal.
    settled = targets[3:]
    settled_steps = [abs(settled[i] - settled[i - 1]) for i in range(1, len(settled))]
    assert max(settled_steps) <= DEFAULT_MAX_STEP + 1e-9, (
        f"steady-state step {max(settled_steps):.1f} exceeds the ordinary limit; the "
        "escape hatch is still firing on a thrashing market"
    )

    # And it holds a band rather than tracking the extremes. The market spans 90 points;
    # the station must span far less and reach neither end.
    span = max(settled) - min(settled)
    assert span <= DEFAULT_MAX_STEP + 1e-9, f"station spans {span:.1f} points"
    assert min(settled) > 20.0, "station tracked the quiet extreme"
    assert max(settled) < 90.0, "station tracked the violent extreme"


@pytest.mark.parametrize("starts_high", [True, False])
def test_a_thrashing_market_is_handled_symmetrically(starts_high: bool) -> None:
    """The planner must not be biased by which way the thrashing happened to start.

    A real defect, found by comparing the two orderings: because a suppressed reversal
    originally left ``_last_sharp_direction`` untouched, every later move in the first
    sharp change's direction matched it and fired at the sharp magnitude, while moves the
    other way were held to the ordinary step. The station therefore ratcheted toward
    whichever extreme it happened to visit first — tracking one end of a market whose mean
    was dead centre.
    """
    engine = planner()
    targets = [
        engine.plan(
            market_energy=95.0 if (index % 2 == 0) == starts_high else 5.0
        ).target_energy
        for index in range(30)
    ]
    settled = targets[3:]
    centre = (max(settled) + min(settled)) / 2
    # Starting high settles high and starting low settles low — the planner holds where it
    # was when the thrashing began. What it must not do is *drift* toward an extreme.
    drift = abs(settled[-1] - settled[0])
    assert drift <= DEFAULT_MAX_STEP + 1e-9, f"station drifted {drift:.1f} points"
    assert 5.0 < centre < 95.0


def test_station_energy_lags_a_step_change_rather_than_matching_it() -> None:
    engine = planner()
    engine.plan(market_energy=20.0)
    first = engine.plan(market_energy=80.0)
    assert first.target_energy < 80.0
    assert first.market_energy == pytest.approx(80.0)


# ---------------------------------------------------------------- persistence


def test_state_can_be_restored_after_a_restart() -> None:
    """§96: restarting must not reset creative memory."""
    engine = planner()
    engine.plan(market_energy=70.0)
    engine.plan(market_energy=72.0)
    saved = engine.current_energy
    peaks = engine.consecutive_peaks

    restored = planner()
    restored.restore(saved, peaks)
    assert restored.current_energy == pytest.approx(saved or 0.0)
    assert restored.consecutive_peaks == peaks
    # And the next plan continues the sequence rather than starting over.
    plan = restored.plan(market_energy=74.0)
    assert plan.previous_energy is not None


def test_restoring_nothing_behaves_like_a_fresh_start() -> None:
    engine = planner()
    engine.restore(None)
    plan = engine.plan(market_energy=66.0)
    assert plan.previous_energy is None
    assert plan.target_energy == pytest.approx(66.0)


def test_restore_clamps_a_negative_peak_count() -> None:
    engine = planner()
    engine.restore(50.0, -5)
    assert engine.consecutive_peaks == 0


def test_reset_clears_all_state() -> None:
    engine = planner()
    engine.plan(market_energy=90.0)
    engine.plan(market_energy=90.0)
    engine.reset()
    assert engine.current_energy is None
    assert engine.consecutive_peaks == 0


# ---------------------------------------------------------------- constants


def test_the_documented_constants_are_coherent() -> None:
    assert SHARP_CHANGE_GAP > DEFAULT_MAX_STEP
    assert 0.0 < PEAK_THRESHOLD < 100.0
    assert MAX_CONSECUTIVE_PEAKS >= 1
