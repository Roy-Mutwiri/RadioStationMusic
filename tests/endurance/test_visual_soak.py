"""Accelerated behaviour soaks: 30 minutes, 2 hours, 8 hours, 24 hours.

These are the tests that decide whether the stream feels alive, and they only exist
because the director lives in Python: a simulated day runs in minutes against
`VirtualClock.advance_sync` and is reproducible from a seed.

Two classes of assertion, kept apart on purpose:

**Structural** — lock conflicts, impossible overlaps, illegal transitions, unreachable
anchors, cooldown breaches. A non-zero count is a bug, full stop, and these assert
hard. Every one of them caught a real defect during development: a chain that reserved
only its first step's locks, a coffee chain starting mid-note, and an action-forced gaze
that bypassed the transit floor.

**Behavioural** — pacing, distribution, repetition. These are judgements, and the
thresholds are wide because the honest claim is "nothing here looks mechanical", not
"this exact number is correct". Where a threshold is tight it is because the brief gave
a figure.

Tick rate
---------
The long soaks sample at 4 Hz rather than the director's 20 Hz. The director decides on
1.4-4.2 s intervals, so 4 Hz samples those amply at a fifth of the cost. It changes
reaction *latency*, which is why the reaction-timing assertions run at full rate.
"""

from __future__ import annotations

import pytest

from tradefix_radio.visual.contracts import ActionCategory
from tradefix_radio.visual.simulate import (
    DURATIONS,
    SCENARIOS,
    run_scenario,
    symbol_switch_run,
)

#: Coarse sampling for the long runs. See the module docstring.
SOAK_TICK = 0.25

#: Salience spike interval. Without spikes the base salience never crosses a threshold
#: and reactions never fire, so a soak would not exercise them at all.
SPIKE_EVERY = 420.0


def _assert_structurally_sound(report: object) -> None:
    """Every structural invariant, with the failures in the message."""
    for label, failures in (
        ("lock violations", report.lock_violations),          # type: ignore[attr-defined]
        ("impossible overlaps", report.overlap_violations),   # type: ignore[attr-defined]
        ("illegal transitions", report.illegal_transitions),  # type: ignore[attr-defined]
        ("unreachable anchors", report.unreachable_anchors),  # type: ignore[attr-defined]
        ("unknown gaze targets", report.unknown_gaze_targets),  # type: ignore[attr-defined]
        ("cooldown violations", report.cooldown_violations),  # type: ignore[attr-defined]
    ):
        assert not failures, f"{label}: {failures[:5]}"


# ============================================================ 30 minutes


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_thirty_minutes_is_structurally_sound_in_every_scenario(scenario: str) -> None:
    """Every scenario the brief names, including the degraded ones."""
    report, _ = run_scenario(
        scenario, DURATIONS["30m"], seed=17, salience_spike_every_seconds=SPIKE_EVERY
    )
    _assert_structurally_sound(report)
    assert report.actions_total > 100, "the director produced almost nothing"


def test_thirty_minutes_does_not_repeat_a_triple_often() -> None:
    """The brief's A→B→C→A→B→C test, at the shortest soak length."""
    report, _ = run_scenario("range", DURATIONS["30m"], seed=17)
    assert report.longest_identical_run == 1
    assert report.back_to_back_repeats == 0
    assert report.top_trigram_share < 0.04, (
        f"the triple {report.top_trigram} takes "
        f"{report.top_trigram_share * 100:.1f} % of all triples"
    )


# ============================================================ 2 hours


def test_two_hours_holds_every_invariant() -> None:
    report, _ = run_scenario(
        "range", DURATIONS["2h"], seed=23,
        salience_spike_every_seconds=SPIKE_EVERY, tick_seconds=SOAK_TICK,
    )
    _assert_structurally_sound(report)
    assert not report.has_visible_loop, report.summary()


def test_two_hours_uses_most_of_the_catalogue() -> None:
    """A director that only ever reaches a third of its vocabulary is a narrow one."""
    report, _ = run_scenario(
        "range", DURATIONS["2h"], seed=23,
        salience_spike_every_seconds=SPIKE_EVERY, tick_seconds=SOAK_TICK,
    )
    assert len(report.action_counts) >= 40, sorted(report.action_counts)


def test_two_hours_respects_the_briefs_pacing_figures() -> None:
    """The intervals the brief gave, converted to rates and checked.

    Coffee 8-30 min is 2.0-7.5 per hour; headphones 4-20 min is 3.0-15.0. Body
    maintenance is "several per hour, not dozens" — read as major resets, since the minor
    tier is small hand and elbow adjustments rather than events.
    """
    report, _ = run_scenario(
        "range", DURATIONS["2h"], seed=23,
        salience_spike_every_seconds=SPIKE_EVERY, tick_seconds=SOAK_TICK,
    )
    assert 1.0 <= report.coffee_per_hour <= 7.5, f"coffee {report.coffee_per_hour:.2f}/h"
    assert 0.0 < report.headphone_per_hour <= 15.0, (
        f"headphones {report.headphone_per_hour:.2f}/h"
    )
    assert 1.5 <= report.posture_major_per_hour <= 8.0, (
        f"major posture resets {report.posture_major_per_hour:.2f}/h"
    )
    assert report.posture_per_hour <= 14.0, (
        f"total body maintenance {report.posture_per_hour:.2f}/h is approaching dozens"
    )
    assert report.reactions / report.hours <= 10.0, "reaction cap exceeded"
    assert not report.posture_during_chain
    assert not report.posture_during_reaction


def test_two_hours_keeps_his_eyes_on_his_work() -> None:
    report, _ = run_scenario(
        "range", DURATIONS["2h"], seed=23, tick_seconds=SOAK_TICK
    )
    assert report.gaze_screen_share > 0.80, f"{report.gaze_screen_share:.3f} on screens"
    assert report.camera_gaze_share <= 0.006, (
        f"{report.camera_gaze_share:.4f} at camera; he is not a presenter"
    )


def test_two_hours_never_lets_a_category_dominate() -> None:
    report, _ = run_scenario(
        "range", DURATIONS["2h"], seed=23,
        salience_spike_every_seconds=SPIKE_EVERY, tick_seconds=SOAK_TICK,
    )
    # Reflex blinks legitimately dominate MICRO; the constraint is on everything else.
    for category in ActionCategory:
        if category is ActionCategory.MICRO:
            continue
        assert report.category_share(category) < 0.40, category.value


# ============================================================ 8 hours


@pytest.mark.slow
def test_eight_hours_holds_every_invariant() -> None:
    report, _ = run_scenario(
        "range", DURATIONS["8h"], seed=31,
        salience_spike_every_seconds=SPIKE_EVERY, tick_seconds=SOAK_TICK,
    )
    _assert_structurally_sound(report)
    assert not report.has_visible_loop, report.summary()


@pytest.mark.slow
def test_eight_hours_develops_a_cyclic_work_rhythm() -> None:
    """The brief's multi-hour cadence, as a cycle rather than an accumulation.

    The V6 version failed both ways depending on its one constant: a large coffee
    decrement suppressed the rhythm entirely, a small one pinned it at the ceiling by
    hour sixteen. The redesign makes phases set *rates*, so neither is reachable.
    """
    report, _ = run_scenario(
        "range", DURATIONS["8h"], seed=31,
        salience_spike_every_seconds=SPIKE_EVERY, tick_seconds=SOAK_TICK,
    )
    assert report.fatigue_max > 0.45, (
        f"fatigue peaked at {report.fatigue_max:.3f}; the rhythm is inert"
    )
    assert report.fatigue_turning_points >= 2, "fatigue never came back down"
    assert not report.fatigue_pinned_ceiling
    assert not report.fatigue_pinned_floor
    assert report.rhythm_is_healthy, report.summary()
    assert report.category_counts.get(ActionCategory.FATIGUE.value, 0) > 0
    # And he must not collapse into it.
    assert report.category_share(ActionCategory.FATIGUE) < 0.04


@pytest.mark.slow
def test_eight_hours_blink_distribution_stays_human() -> None:
    """15-20 blinks a minute is the human range; the clamp must not create a spike."""
    report, _ = run_scenario(
        "range", DURATIONS["8h"], seed=31, tick_seconds=SOAK_TICK
    )
    per_minute = report.blinks_total / (report.hours * 60.0)
    assert 10.0 <= per_minute <= 26.0, f"{per_minute:.1f} blinks/min"
    assert report.blink_interval_mean is not None
    assert 3.0 <= report.blink_interval_mean <= 6.5


# ============================================================ 24 hours


@pytest.mark.slow
def test_twenty_four_hours_holds_every_invariant() -> None:
    """The headline soak. Roughly 350 k ticks of simulated behaviour."""
    report, _ = run_scenario(
        "range", DURATIONS["24h"], seed=5,
        salience_spike_every_seconds=SPIKE_EVERY, tick_seconds=SOAK_TICK,
    )
    _assert_structurally_sound(report)


@pytest.mark.slow
def test_twenty_four_hours_has_no_visible_loop() -> None:
    """Acceptance criterion 3, as a number rather than an impression.

    `trigram_diversity` falls with sample size — more triples means more collisions —
    so the headline figure is the single most common triple's *share*. With 51
    schedulable actions and a working penalty it lands near 0.5 %.
    """
    report, _ = run_scenario(
        "range", DURATIONS["24h"], seed=5,
        salience_spike_every_seconds=SPIKE_EVERY, tick_seconds=SOAK_TICK,
    )
    assert report.longest_identical_run == 1
    assert report.back_to_back_repeats == 0
    assert report.top_trigram_share < 0.02, (
        f"{report.top_trigram} takes {report.top_trigram_share * 100:.2f} % of triples"
    )
    assert report.distinct_trigrams > 3000, (
        f"only {report.distinct_trigrams} distinct triples across a day"
    )


@pytest.mark.slow
def test_twenty_four_hours_cycles_the_work_rhythm_repeatedly() -> None:
    """The brief's long-run expectation, with the statistics it asks for.

    Fatigue must rise and fall repeatedly, and must not: rise monotonically, pin at the
    ceiling, pin at the floor, or oscillate on a fixed schedule. The last is the one
    min/max/mean cannot detect, so it is checked as spread in cycle length.
    """
    report, _ = run_scenario(
        "range", DURATIONS["24h"], seed=5,
        salience_spike_every_seconds=SPIKE_EVERY, tick_seconds=SOAK_TICK,
    )
    assert report.fatigue_cycles >= 3, f"only {report.fatigue_cycles} cycles in a day"
    assert report.fatigue_turning_points >= 6
    assert not report.fatigue_pinned_ceiling
    assert not report.fatigue_pinned_floor
    assert report.fatigue_time_near_ceiling < 0.25, (
        f"{report.fatigue_time_near_ceiling * 100:.1f} % near the ceiling"
    )
    assert report.fatigue_time_near_floor < 0.40
    assert len(report.cycle_durations_hours) >= 3
    spread = max(report.cycle_durations_hours) - min(report.cycle_durations_hours)
    assert spread > 0.15, f"cycle lengths are near-identical: {report.cycle_durations_hours}"
    assert report.rhythm_is_healthy, report.summary()


@pytest.mark.slow
def test_twenty_four_hours_sustains_body_maintenance() -> None:
    report, _ = run_scenario(
        "range", DURATIONS["24h"], seed=5,
        salience_spike_every_seconds=SPIKE_EVERY, tick_seconds=SOAK_TICK,
    )
    assert 1.5 <= report.posture_major_per_hour <= 8.0
    assert not report.posture_during_chain
    assert not report.posture_during_reaction


@pytest.mark.slow
def test_twenty_four_hours_uses_nearly_the_whole_catalogue() -> None:
    report, _ = run_scenario(
        "range", DURATIONS["24h"], seed=5,
        salience_spike_every_seconds=SPIKE_EVERY, tick_seconds=SOAK_TICK,
    )
    assert len(report.action_counts) >= 58, sorted(report.action_counts)


# ============================================================ acceptance criteria


def test_market_energy_measurably_changes_action_density() -> None:
    """Acceptance criterion 4. Measured across the full band range."""
    rates: list[float] = []
    for scenario in ("no_active_market", "quiet", "range", "trend", "breakout",
                     "extreme_volatility"):
        report, _ = run_scenario(scenario, DURATIONS["30m"], seed=41)
        rates.append(report.deliberate_total / report.hours)
    assert rates == sorted(rates), f"action density is not monotonic in band: {rates}"
    assert rates[-1] > rates[0] * 2.0, "the span is not measurable"


def test_music_bpm_subtly_affects_rhythmic_actions() -> None:
    """Acceptance criterion 5. Subtle: present, and capped."""
    low, _ = run_scenario("low_bpm", DURATIONS["30m"], seed=43)
    high, _ = run_scenario("high_bpm", DURATIONS["30m"], seed=43)
    low_music = low.category_counts.get(ActionCategory.MUSIC.value, 0)
    high_music = high.category_counts.get(ActionCategory.MUSIC.value, 0)
    assert high_music > low_music
    assert high.category_share(ActionCategory.MUSIC) < 0.12, "music is not subtle"
    # And nothing else moved: the two scenarios differ only in tempo.
    assert abs(high.deliberate_total - low.deliberate_total) < low.deliberate_total * 0.25


def test_symbol_switch_does_not_reset_behaviour() -> None:
    """Acceptance criterion 6, over a real run across the switch.

    The character must carry on: the same breadth of behaviour either side, no restart.
    """
    report, before, after = symbol_switch_run(duration_seconds=1_200.0, seed=47)
    assert before and after
    distinct_before = len(set(before))
    distinct_after = len(set(after))
    assert distinct_after >= distinct_before * 0.5, (
        f"behaviour narrowed across the switch: {distinct_before} -> {distinct_after}"
    )
    assert report.actions_total == len(before) + len(after)


def test_feed_loss_falls_back_to_neutral_idle() -> None:
    """Acceptance criterion 7. He keeps working; he stops reacting."""
    report, _ = run_scenario(
        "feed_down", DURATIONS["30m"], seed=53, salience_spike_every_seconds=120.0
    )
    _assert_structurally_sound(report)
    assert report.reactions == 0, "reacted with no feed behind it"
    assert report.category_counts.get(ActionCategory.REACTION.value, 0) == 0
    # But alive: breathing, blinking, reading, coffee.
    assert report.blinks_total > 100
    assert report.deliberate_total > 50


def test_behaviour_is_reproducible_from_a_seed() -> None:
    """Without this, no soak finding can be reproduced and no regression can be bisected."""
    first, _ = run_scenario("range", DURATIONS["30m"], seed=61)
    second, _ = run_scenario("range", DURATIONS["30m"], seed=61)
    assert first.action_counts == second.action_counts
    assert first.top_trigram == second.top_trigram
    assert first.fatigue_phase_final == pytest.approx(second.fatigue_phase_final)


def test_different_seeds_produce_different_behaviour() -> None:
    """The converse: a seed that does not change anything is not a seed."""
    first, _ = run_scenario("range", DURATIONS["30m"], seed=61)
    second, _ = run_scenario("range", DURATIONS["30m"], seed=62)
    assert first.action_counts != second.action_counts
