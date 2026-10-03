"""The long-run work rhythm and body-maintenance behaviour.

Both systems were redesigned after the V6 soak found them failing in opposite ways:
posture resets fired **zero** times across two simulated hours, and fatigue pinned at its
ceiling after about sixteen. Neither was a tuning problem, so neither fix was a constant.

These tests assert the *properties* that make each design work rather than the numbers it
currently produces, so a later tuning pass cannot quietly reintroduce either failure.
"""

from __future__ import annotations

import random
from itertools import pairwise

import pytest

from tradefix_radio.visual.catalog import (
    CATALOG,
    GROUP_COOLDOWN_SECONDS,
    POSTURE_MAINTENANCE_INTERVAL_SECONDS,
    POSTURE_MAJOR,
    POSTURE_MINOR,
    POSTURE_MINOR_INTERVAL_SECONDS,
    is_body_maintenance,
    is_major_posture,
    spec,
)
from tradefix_radio.visual.contracts import (
    ActionCategory,
    CharacterState,
    IntensityBand,
    InteractionLock,
)
from tradefix_radio.visual.rhythm import (
    DEEP_WORK_EXIT_RANGE,
    FATIGUE_CEILING,
    FATIGUE_FLOOR,
    INFLUENCES,
    RECOVERY_TRIGGER_RANGE,
    RhythmTrace,
    WorkPhase,
    WorkRhythm,
)
from tradefix_radio.visual.simulate import run_scenario

# ============================================================ the posture family


def test_posture_is_its_own_category_not_fatigue() -> None:
    """The structural fix. Body maintenance happens whether or not he is tired.

    In V6 `posture_reset` sat in FATIGUE behind a fatigue ramp a quiet market never
    reached, which is half of why it never fired.
    """
    for action_id in POSTURE_MAJOR + POSTURE_MINOR:
        action = spec(action_id)
        assert action.category is ActionCategory.POSTURE, action_id
        assert action.min_fatigue_phase == 0.0, (
            f"{action_id} is gated on fatigue; body maintenance must not be"
        )


def test_posture_is_a_family_not_one_animation() -> None:
    """The brief: do not make all of these one animation. Create a family."""
    assert len(POSTURE_MAJOR) >= 5, POSTURE_MAJOR
    assert len(POSTURE_MINOR) >= 2, POSTURE_MINOR
    # And they must be distinguishable: different locks or different durations, or the
    # family is one animation under seven names.
    signatures = {
        (spec(a).effective_locks, spec(a).duration_ms) for a in POSTURE_MAJOR
    }
    assert len(signatures) >= 4, "the major family members are not meaningfully distinct"


def test_posture_is_admissible_from_the_stable_working_states() -> None:
    """V6 tied it to POSTURE_RESET alone — a state entered rarely and exited in 3-10 s.

    A man shifts in his chair while reading a chart, not only during a dedicated
    interlude.
    """
    required = {
        CharacterState.IDLE_FOCUS,
        CharacterState.WAITING,
        CharacterState.ANALYZING,
    }
    for action_id in POSTURE_MAJOR + POSTURE_MINOR:
        permitted = set(spec(action_id).allowed_character_states)
        assert required <= permitted, f"{action_id} is not admissible from {required - permitted}"


def test_posture_tiers_have_separate_anti_repeat_keys() -> None:
    """Sharing a key makes the frequent tier starve the rare one.

    Exactly how V6's `posture_reset` died: `shoulder_shift`, which fires every 35-130 s,
    armed the 5-25 minute group cooldown they shared.
    """
    major_keys = {spec(a).anti_repeat_key for a in POSTURE_MAJOR}
    minor_keys = {spec(a).anti_repeat_key for a in POSTURE_MINOR}
    assert not (major_keys & minor_keys), "the two posture tiers share an anti-repeat key"
    for key in major_keys | minor_keys:
        assert key in GROUP_COOLDOWN_SECONDS, f"{key!r} has no group cooldown"


def test_posture_intervals_are_sampled_ranges() -> None:
    """A fixed interval is a visible period. Over eight hours a viewer learns it."""
    for low, high in (
        POSTURE_MAINTENANCE_INTERVAL_SECONDS,
        POSTURE_MINOR_INTERVAL_SECONDS,
    ):
        assert high > low * 1.5, f"interval ({low}, {high}) is too narrow to read as varied"


def test_micro_shoulder_shift_no_longer_shares_the_posture_key() -> None:
    assert spec("shoulder_shift").anti_repeat_key != spec("chair_reposition").anti_repeat_key


# ============================================================ posture, measured


@pytest.mark.parametrize("duration", ["2h", "8h"])
def test_posture_occurs_over_a_long_horizon(duration: str) -> None:
    """The headline fix: it happens at all, and at a human rate.

    The brief's target is "roughly several posture resets per hour, not dozens". Major
    resets are the ones that read as events; the minor tier is small hand and elbow
    adjustments and is allowed to be more frequent.
    """
    report, _ = run_scenario(
        "range", duration, seed=19, salience_spike_every_seconds=420.0, tick_seconds=0.25
    )
    assert report.posture_major_total > 0, "body maintenance never happened"
    assert 1.5 <= report.posture_major_per_hour <= 8.0, (
        f"{report.posture_major_per_hour:.2f} major resets/h is outside 'several'"
    )
    assert report.posture_per_hour <= 14.0, (
        f"{report.posture_per_hour:.2f} total/h is approaching 'dozens'"
    )


def test_posture_never_interrupts_an_object_chain() -> None:
    """A condition of the gate, so a hit means the gate was bypassed."""
    report, _ = run_scenario(
        "range", "8h", seed=23, salience_spike_every_seconds=300.0, tick_seconds=0.25
    )
    assert not report.posture_during_chain, report.posture_during_chain


def test_posture_never_lands_inside_a_market_reaction() -> None:
    """He does not stretch while something is moving."""
    report, _ = run_scenario(
        "breakout", "8h", seed=23, salience_spike_every_seconds=240.0, tick_seconds=0.25
    )
    assert not report.posture_during_reaction, report.posture_during_reaction


def test_posture_respects_its_cooldown() -> None:
    """Checked by the simulator's own cooldown audit, over a long run."""
    report, _ = run_scenario(
        "range", "8h", seed=29, salience_spike_every_seconds=420.0, tick_seconds=0.25
    )
    offenders = [
        violation
        for violation in report.cooldown_violations
        if any(action in violation for action in POSTURE_MAJOR + POSTURE_MINOR)
    ]
    assert not offenders, offenders


def test_posture_uses_more_than_one_member() -> None:
    """A family that always picks the same member is one animation with extra steps."""
    report, _ = run_scenario(
        "range", "8h", seed=31, salience_spike_every_seconds=420.0, tick_seconds=0.25
    )
    used = {a for a in POSTURE_MAJOR if report.action_counts.get(a, 0) > 0}
    assert len(used) >= 4, f"only {sorted(used)} of the major family were used"


def test_posture_stays_under_its_category_ceiling() -> None:
    report, _ = run_scenario(
        "range", "8h", seed=31, salience_spike_every_seconds=420.0, tick_seconds=0.25
    )
    assert report.category_share(ActionCategory.POSTURE) < 0.08


# ============================================================ the work rhythm


def _rhythm(seed: int = 1) -> WorkRhythm:
    rhythm = WorkRhythm()
    rhythm.seed(random.Random(seed))
    return rhythm


def _run(rhythm: WorkRhythm, hours: float, *, workload: float, step: float = 60.0,
         band: IntensityBand = IntensityBand.B2_STEADY) -> RhythmTrace:
    trace = RhythmTrace(interval_seconds=step)
    now = 0.0
    rhythm.advance(now, workload=workload, band=band)
    while now < hours * 3600.0:
        now += step
        rhythm.advance(now, workload=workload, band=band)
        trace.observe(rhythm, now)
    return trace


def test_phases_cycle_in_order() -> None:
    """FOCUS_BUILD → DEEP_WORK → FATIGUE_RISE → RECOVERY → FOCUS_BUILD."""
    rhythm = _rhythm(3)
    seen: list[WorkPhase] = []
    now = 0.0
    rhythm.advance(now, workload=0.6)
    while now < 20 * 3600.0:
        now += 30.0
        rhythm.advance(now, workload=0.6)
        if not seen or seen[-1] is not rhythm.phase:
            seen.append(rhythm.phase)

    expected = [
        WorkPhase.FOCUS_BUILD, WorkPhase.DEEP_WORK,
        WorkPhase.FATIGUE_RISE, WorkPhase.RECOVERY,
    ]
    assert len(seen) >= 5, f"fewer than one full cycle in 20 h: {seen}"
    for index, phase in enumerate(seen):
        assert phase is expected[index % 4], f"phase {index} was {phase} in {seen}"


def test_fatigue_both_accumulates_and_recovers() -> None:
    """The V6 accumulator could only do the first."""
    rhythm = _rhythm(5)
    trace = _run(rhythm, 20.0, workload=0.6)
    assert trace.maximum > 0.5, "fatigue never accumulated"
    assert trace.turning_points() >= 2, "fatigue never came back down"
    assert rhythm.cycles_completed >= 1


def test_fatigue_never_pins_at_the_ceiling() -> None:
    """The V6 failure, as a direct assertion.

    `FATIGUE_RISE` exits into `RECOVERY` at a threshold drawn below the clamp, so the
    clamp is a guard against a pathological workload rather than a destination.
    """
    rhythm = _rhythm(7)
    trace = _run(rhythm, 24.0, workload=1.0)  # maximum sustained workload
    assert not trace.pinned_at_ceiling
    assert trace.maximum < FATIGUE_CEILING, f"reached the clamp at {trace.maximum:.3f}"
    assert trace.time_near_ceiling < 0.35, (
        f"{trace.time_near_ceiling * 100:.1f} % of the run was near the ceiling"
    )


def test_fatigue_never_pins_at_the_floor() -> None:
    """The opposite failure: a recovery so strong the cycle cannot advance.

    V6 hit this too, with a 0.25-per-coffee step decrement.
    """
    rhythm = _rhythm(11)
    trace = _run(rhythm, 24.0, workload=0.15, band=IntensityBand.B1_QUIET)
    assert not trace.pinned_at_floor
    assert trace.maximum > 0.25, (
        f"a quiet day never developed any fatigue at all (max {trace.maximum:.3f})"
    )


def test_fatigue_stays_bounded() -> None:
    for workload in (0.0, 0.5, 1.0):
        rhythm = _rhythm(13)
        trace = _run(rhythm, 24.0, workload=workload)
        assert trace.minimum >= FATIGUE_FLOOR - 1e-9
        assert trace.maximum <= FATIGUE_CEILING + 1e-9
        assert 0.0 <= rhythm.focus_level <= 1.0


def test_cycle_lengths_vary() -> None:
    """Not a metronome. Sampled thresholds plus workload-dependent rates.

    A fixed-period oscillation is as mechanical as a pinned ceiling, and min/max/mean
    cannot tell the two apart — which is why this checks the spread.
    """
    rhythm = _rhythm(17)
    _run(rhythm, 30.0, workload=0.55)
    assert len(rhythm.cycle_durations) >= 3, rhythm.cycle_durations
    hours = [d / 3600.0 for d in rhythm.cycle_durations]
    assert max(hours) - min(hours) > 0.15, f"cycle lengths are near-identical: {hours}"


def test_phases_set_rates_not_values() -> None:
    """Smooth by construction. No step may exceed what one interval's rate allows."""
    rhythm = _rhythm(19)
    now = 0.0
    rhythm.advance(now, workload=0.7)
    levels = [rhythm.fatigue_level]
    while now < 12 * 3600.0:
        now += 10.0
        rhythm.advance(now, workload=0.7)
        levels.append(rhythm.fatigue_level)
    jumps = [abs(b - a) for a, b in pairwise(levels)]
    assert max(jumps) < 0.01, f"a step of {max(jumps):.4f} in 10 s is a jump, not a rate"


def test_recovery_influences_change_the_rate_not_the_level() -> None:
    """The brief: do not instantly reset fatigue to zero. Use gradual decay."""
    rhythm = _rhythm(23)
    _run(rhythm, 3.0, workload=0.8)
    before_level = rhythm.fatigue_level
    before_rate = rhythm.recovery_rate

    rhythm.note("coffee", 3.0 * 3600.0)
    rhythm.advance(3.0 * 3600.0 + 1.0, workload=0.8)

    assert rhythm.fatigue_level >= before_level - 0.002, "coffee subtracted from the level"
    assert rhythm.recovery_rate > before_rate, "coffee did not raise the recovery rate"


def test_recovery_influences_decay() -> None:
    rhythm = _rhythm(29)
    rhythm.advance(0.0, workload=0.5)
    rhythm.note("coffee", 0.0)
    rhythm.advance(10.0, workload=0.5)
    fresh = rhythm.recovery_rate
    decay = INFLUENCES["coffee"].decay_seconds
    rhythm.advance(decay * 0.9, workload=0.5)
    faded = rhythm.recovery_rate
    rhythm.advance(decay + 60.0, workload=0.5)
    assert faded < fresh, "the influence did not decay"
    assert "coffee" not in rhythm.active_influences, "the influence never expired"


def test_recovery_influences_do_not_fully_determine_the_rhythm() -> None:
    """The brief's exact requirement.

    Fatigue must still accumulate under a continuous stream of recovery influences —
    otherwise the influences *are* the rhythm, which is the V6 failure in a new costume.
    """
    rhythm = _rhythm(31)
    now = 0.0
    rhythm.advance(now, workload=0.75)
    while now < 6 * 3600.0:
        now += 60.0
        # Coffee every ten minutes: far more than the measured 2.1/hour.
        if int(now) % 600 == 0:
            rhythm.note("coffee", now)
            rhythm.note("posture_reset", now)
        rhythm.advance(now, workload=0.75)
    assert rhythm.fatigue_level > 0.25, (
        f"continuous recovery flattened the cycle (fatigue {rhythm.fatigue_level:.3f})"
    )


def test_quiet_markets_recover_faster_than_loud_ones() -> None:
    quiet = _rhythm(37)
    loud = _rhythm(37)
    _run(quiet, 2.0, workload=0.2, band=IntensityBand.B1_QUIET)
    _run(loud, 2.0, workload=0.9, band=IntensityBand.B5_PEAK)
    assert quiet.recovery_rate > loud.recovery_rate
    assert quiet.fatigue_level < loud.fatigue_level


def test_workload_drives_the_accumulation_rate() -> None:
    """Measured as time-to-threshold, not as level at a fixed instant.

    Comparing levels at a fixed time is wrong and the first version of this test did it:
    a heavy workload reaches the recovery trigger *sooner*, so three hours in it can be
    mid-recovery and reading lower than a light workload still accumulating. The rate is
    what workload drives; the level at an arbitrary moment depends on phase.
    """

    def hours_to_reach(workload: float, target: float) -> float:
        rhythm = _rhythm(41)
        now = 0.0
        rhythm.advance(now, workload=workload)
        while now < 24 * 3600.0:
            now += 60.0
            rhythm.advance(now, workload=workload)
            if rhythm.fatigue_level >= target:
                return now / 3600.0
        return float("inf")

    light = hours_to_reach(0.15, 0.40)
    heavy = hours_to_reach(1.0, 0.40)
    assert heavy < light / 1.5, f"heavy {heavy:.2f} h vs light {light:.2f} h to reach 0.40"


def test_time_in_phase_floor_prevents_thrash() -> None:
    """No sudden state jumps: a workload spike must not bounce the phase twice a minute."""
    rhythm = _rhythm(43)
    now = 0.0
    rhythm.advance(now, workload=0.5)
    changes = 0
    previous = rhythm.phase
    while now < 2 * 3600.0:
        now += 5.0
        # Alternate between extremes every step.
        rhythm.advance(now, workload=1.0 if int(now) % 10 else 0.0)
        if rhythm.phase is not previous:
            changes += 1
            previous = rhythm.phase
    assert changes <= 3, f"{changes} phase changes in two hours under a thrashing workload"


def test_unknown_influence_fails_loudly() -> None:
    rhythm = _rhythm()
    with pytest.raises(KeyError, match="unknown recovery influence"):
        rhythm.note("a_nap", 0.0)


def test_rhythm_is_reproducible_from_a_seed() -> None:
    first = _rhythm(53)
    second = _rhythm(53)
    trace_a = _run(first, 12.0, workload=0.6)
    trace_b = _run(second, 12.0, workload=0.6)
    assert trace_a.samples == trace_b.samples
    assert first.cycles_completed == second.cycles_completed


# ============================================================ effects on behaviour


def test_fatigue_gate_opens_above_its_floor_and_is_closed_below() -> None:
    rhythm = _rhythm()
    rhythm.fatigue_level = 0.10
    assert rhythm.fatigue_action_gate() == 0.0
    rhythm.fatigue_level = 0.75
    assert rhythm.fatigue_action_gate() > 0.5


def test_blink_rate_change_stays_subtle() -> None:
    """The brief allows a slight increase and asks for it to stay subtle."""
    rhythm = _rhythm()
    rhythm.fatigue_level = FATIGUE_CEILING
    assert rhythm.blink_rate_scale() >= 0.74


def test_focus_makes_him_work_harder() -> None:
    """Why focus is tracked separately: output varies for reasons the market did not cause."""
    rhythm = _rhythm()
    rhythm.focus_level = 0.15
    low = rhythm.work_intensity_scale()
    rhythm.focus_level = 0.95
    assert rhythm.work_intensity_scale() > low


def test_coffee_becomes_more_attractive_when_tired() -> None:
    rhythm = _rhythm()
    rhythm.fatigue_level, rhythm.focus_level = 0.05, 0.95
    fresh = rhythm.coffee_weight_scale()
    rhythm.fatigue_level, rhythm.focus_level = 0.80, 0.30
    assert rhythm.coffee_weight_scale() > fresh


def test_thresholds_are_ordered_so_the_cycle_can_complete() -> None:
    """`DEEP_WORK` must be able to exit below the recovery trigger, or it never does."""
    assert DEEP_WORK_EXIT_RANGE[1] < RECOVERY_TRIGGER_RANGE[0]
    assert RECOVERY_TRIGGER_RANGE[1] < FATIGUE_CEILING


# ============================================================ integration


def test_the_director_drives_the_rhythm_and_records_influences() -> None:
    report, simulator = run_scenario(
        "range", "8h", seed=59, salience_spike_every_seconds=420.0, tick_seconds=0.25
    )
    rhythm = simulator.director.rhythm
    assert rhythm.cycles_completed >= 1
    assert report.fatigue_turning_points >= 2
    assert report.rhythm_is_healthy, (
        f"min {report.fatigue_min:.3f} max {report.fatigue_max:.3f} "
        f"turns {report.fatigue_turning_points} ceil {report.fatigue_time_near_ceiling:.3f}"
    )
    assert set(report.work_phase_shares) >= {"deep_work", "recovery"}


def test_coffee_and_posture_register_as_recovery_influences() -> None:
    """The wiring, not just the mechanism."""
    _, simulator = run_scenario(
        "quiet", "2h", seed=61, salience_spike_every_seconds=600.0, tick_seconds=0.25
    )
    director = simulator.director
    # Both influences fire during a two-hour quiet run; the rhythm must have seen them.
    assert director.rhythm.recovery_rate > 0.30, "no influence ever reached the rhythm"


def test_fatigue_family_now_actually_fires() -> None:
    """V6 measured 24 fatigue actions in 24 hours because the phase never rose."""
    report, _ = run_scenario(
        "range", "8h", seed=67, salience_spike_every_seconds=420.0, tick_seconds=0.25
    )
    assert report.category_per_hour(ActionCategory.FATIGUE) > 1.0
    # And still restrained.
    assert report.category_share(ActionCategory.FATIGUE) < 0.04


def test_object_conflict_is_enforced_on_every_start_path() -> None:
    """Enforced in the scheduler only, the reaction path bypassed it within eight hours.

    A reaction composed `quick_note`, which took the pen while the mug was already held.
    """
    for scenario, seed in (("extreme_volatility", 11), ("breakout", 71), ("trend", 73)):
        report, _ = run_scenario(
            scenario, "8h", seed=seed,
            salience_spike_every_seconds=240.0, tick_seconds=0.25,
        )
        assert not report.overlap_violations, (scenario, report.overlap_violations)


def test_body_maintenance_helpers_agree_with_the_catalogue() -> None:
    for action_id, action in CATALOG.items():
        expected = action.category is ActionCategory.POSTURE
        assert is_body_maintenance(action_id) is expected, action_id
    for action_id in POSTURE_MAJOR:
        assert is_major_posture(action_id)
    for action_id in POSTURE_MINOR:
        assert not is_major_posture(action_id)


def test_posture_actions_claim_body_locks_not_object_locks() -> None:
    """Body maintenance must never need an object, or it becomes an object chain."""
    objects = {InteractionLock.COFFEE, InteractionLock.PEN}
    for action_id in POSTURE_MAJOR + POSTURE_MINOR:
        assert not (spec(action_id).effective_locks & objects), action_id
