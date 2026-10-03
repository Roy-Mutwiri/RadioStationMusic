"""The behaviour director's invariants.

Three tiers, and the division is deliberate:

**Unit** — the mechanics in isolation. Locks, cooldowns, history, blink, gaze, bands.
**Property** — invariants over randomised input, via Hypothesis. The lock system is the
one that really needs this: "no two in-flight actions share a lock" has to hold over
sequences nobody thought to write down, and three of the bugs this suite caught during
development were found exactly there.
**Simulation** — the invariants over simulated hours. Structural failures are bugs, not
tuning questions, so they assert rather than report.

The simulation tests are the slow ones and carry real wall-clock cost. The 24-hour soak
lives in `tests/endurance/` so an ordinary run stays fast.
"""

from __future__ import annotations

import random
from datetime import datetime
from itertools import pairwise

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tradefix_radio.core.clock import UTC, VirtualClock
from tradefix_radio.visual.camera import CAMERA_METADATA, CameraState, validate_metadata
from tradefix_radio.visual.catalog import (
    CATALOG,
    CATEGORY_HOUR_SHARE_CEILING,
    CHAINS,
    GROUP_COOLDOWN_SECONDS,
    SCHEDULABLE,
    spec,
    validate_catalog,
)
from tradefix_radio.visual.contracts import (
    ActionCategory,
    BandBiasV1,
    CharacterState,
    FeedTrust,
    GazeTarget,
    IntensityBand,
    InteractionLock,
    Interruptibility,
    VisualStateV1,
)
from tradefix_radio.visual.director import (
    EXECUTING_PREDECESSORS,
    STATE_TRANSITIONS,
    BehaviorDirector,
)
from tradefix_radio.visual.gaze import (
    BLINK_MAX_SECONDS,
    BLINK_MIN_SECONDS,
    VARIANT_BLINK_MIN_GAP_SECONDS,
    BlinkDriver,
)
from tradefix_radio.visual.geometry import default_blockout
from tradefix_radio.visual.modulation import (
    action_rate_multiplier,
    behavior_energy,
    music_weight_factor,
    profile_for,
    rhythm_policy,
)
from tradefix_radio.visual.scheduler import (
    ActionHistory,
    CooldownTable,
    HistoryEntry,
    LockConflict,
    LockTable,
)
from tradefix_radio.visual.rhythm import WorkRhythm
from tradefix_radio.visual.simulate import SCENARIOS, run_scenario


def _state(
    band: IntensityBand = IntensityBand.B2_STEADY,
    *,
    bpm: int | None = 110,
    music_energy: float | None = 0.5,
    symbol: str = "XAUUSD",
    trust: FeedTrust = FeedTrust.LIVE,
    salience: float = 0.0,
    broadcasting: bool = True,
) -> VisualStateV1:
    return VisualStateV1(
        at=datetime(2026, 10, 3, 22, 0, tzinfo=UTC),
        source_age_seconds=0.4,
        feed_trust=trust,
        active_symbol=symbol,
        market_regime=None if trust is FeedTrust.STALE else "normal_range",
        intensity_band=band,
        market_energy=None if trust is FeedTrust.STALE else 50.0,
        market_confidence=0.75,
        music_bpm=bpm,
        music_energy=music_energy,
        broadcasting=broadcasting,
        behavior_energy=0.5,
        reaction_salience=salience,
    )


# ============================================================ catalogue


def test_catalogue_covers_every_brief_action() -> None:
    """Every action the brief names must exist, under that name."""
    required = {
        # MICRO
        "blink", "double_blink", "slow_blink", "eye_left", "eye_right", "eye_down",
        "eye_main_monitor", "micro_brow_raise", "micro_frown", "small_head_tilt",
        "small_head_turn", "shoulder_shift", "finger_tap", "hand_reposition",
        # WORK
        "mouse_move", "mouse_click", "mouse_double_click", "mouse_scroll",
        "typing_short", "typing_medium", "typing_long", "hotkey", "chart_pan",
        "chart_inspect", "monitor_left_glance", "monitor_right_glance", "lean_forward",
        "lean_back", "hand_to_chin", "hand_to_mouth", "note_write_short",
        "note_write_long", "watch_check",
        # HEADPHONES
        "adjust_left", "adjust_right", "press_earcup", "settle_headphones",
        # CAFFEINE
        "reach_cup", "pick_cup", "sip", "hold_cup", "place_cup", "coffee_reset",
        # FATIGUE
        "neck_stretch", "deep_exhale", "brief_head_down",
        # POSTURE — the brief's "posture reset" is a family of seven, not one action.
        # A single `posture_reset` fired zero times across two simulated hours; see
        # `tests/unit/test_visual_rhythm.py` for why and what replaced it.
        "chair_reposition", "spine_straighten", "posture_lean_back", "shoulder_roll",
        "neck_reset", "elbow_reposition", "hand_rest_reset",
        # REACTION
        "quick_chart_glance", "lean_forward_reaction", "small_nod", "subtle_smirk",
        "controlled_exhale", "rapid_mouse", "quick_note",
        # MUSIC
        "micro_head_nod", "finger_rhythm", "small_shoulder_rhythm",
    }
    missing = sorted(required - set(CATALOG))
    assert not missing, f"catalogue is missing {missing}"


def test_catalogue_agrees_with_frozen_geometry() -> None:
    blockout = default_blockout()
    problems = validate_catalog(
        frozenset(blockout.anchors), frozenset(t.value for t in blockout.gaze)
    )
    assert not problems, problems


def test_camera_metadata_agrees_with_frozen_geometry() -> None:
    assert not validate_metadata(default_blockout())


def test_no_action_names_a_market_regime() -> None:
    """The seam that keeps market logic out of behaviour data.

    A regime name anywhere in the catalogue would mean a new regime requires a
    behaviour change, which is exactly the coupling the architecture is drawn to avoid.
    """
    regimes = {
        "quiet", "low_volatility_range", "normal_range", "compression",
        "breakout_buildup", "bullish_breakout", "bearish_breakout", "bullish_trend",
        "bearish_trend", "high_volatility_range", "extreme_volatility", "reversal",
        "post_event_normalization",
    }
    for action_id, action in CATALOG.items():
        haystack = f"{action_id} {' '.join(action.tags)} {action.anti_repeat_key}"
        for regime in regimes:
            assert regime not in haystack, f"{action_id} names the regime {regime!r}"


def test_object_locks_imply_non_interruptible() -> None:
    """An interruptible grip is the floating-mug bug waiting to happen."""
    for action in CATALOG.values():
        objects = {InteractionLock.COFFEE, InteractionLock.PEN}
        if objects & set(action.interaction_locks):
            assert action.interruptibility is Interruptibility.NEVER, action.action_id


def test_cooldown_ranges_are_ranges() -> None:
    """A fixed cooldown is a visible period. Every schedulable action must vary."""
    for action_id in SCHEDULABLE:
        low, high = spec(action_id).cooldown_range_seconds
        if low == 0.0 and high == 0.0:
            continue  # reaction composites are triggered, not scheduled
        assert high > low, f"{action_id} has a constant cooldown of {low} s"


def test_group_cooldowns_can_bind_over_member_cooldowns() -> None:
    """A group cooldown that can never exceed a member's own floor does nothing.

    The headphone group was a flat 150 s against member floors of 240 s, so it was inert
    and a quiet half-hour produced 18 adjustments per hour against the brief's
    4-20 minutes.

    The condition is on the group's *high* end, not its low end, and the first version of
    this test got that wrong. A group spanning 420-1800 s over members with 900 s floors
    is doing its job: after member A fires, A waits 900 s on its own cooldown while B
    waits on whatever the group sampled. What makes a group inert is being unable to
    reach a member's floor at all.
    """
    for key, (_, group_high) in GROUP_COOLDOWN_SECONDS.items():
        members = [a for a in CATALOG.values() if a.anti_repeat_key == key]
        assert members, f"group cooldown {key!r} has no members"
        shortest = min(a.cooldown_range_seconds[0] for a in members)
        assert group_high >= shortest, (
            f"group {key!r} tops out at {group_high} s, below its shortest member floor "
            f"{shortest} s; the group rule could never bind"
        )


def test_frequent_and_rare_actions_do_not_share_a_key() -> None:
    """A frequent member starves a rare one through the shared group cooldown.

    `shoulder_shift` (35-130 s) shared the key "posture" with `posture_reset`
    (5-25 min) and the major reset fired zero times across two simulated hours.
    """
    for key in GROUP_COOLDOWN_SECONDS:
        members = [a for a in CATALOG.values() if a.anti_repeat_key == key]
        floors = [a.cooldown_range_seconds[0] for a in members if a.cooldown_range_seconds[0] > 0]
        if len(floors) < 2:
            continue
        assert max(floors) / min(floors) <= 12.0, (
            f"group {key!r} mixes a {min(floors):.0f} s action with a {max(floors):.0f} s "
            "one; the frequent member will starve the rare one"
        )


def test_chains_reserve_every_lock_their_steps_need() -> None:
    """A chain must reserve up front what a later step will need.

    NOTE_WRITE opens on HEAD and acquires the pen two steps later. Checking only the
    first step's locks let it start with the right hand on the mouse, and it raised
    `LockConflict` mid-chain inside two simulated hours.
    """
    for chain in CHAINS.values():
        union = chain.required_locks()
        for step in chain.steps:
            for lock in CATALOG[step].effective_locks:
                assert lock in union, f"{chain.chain_id} omits {lock.value} needed by {step}"


def test_chain_steps_are_not_schedulable() -> None:
    """A `sip` without a preceding `pick_cup` is a mug teleporting to his mouth."""
    for chain in CHAINS.values():
        for step in chain.steps[1:]:
            assert step not in SCHEDULABLE or CATALOG[step].chain_only is False


def test_category_ceilings_sum_above_one() -> None:
    """Ceilings must leave the scheduler somewhere to go.

    If they summed below 1.0 every category could hit its cap simultaneously and the
    only remaining candidate would be stillness, indefinitely.
    """
    assert sum(CATEGORY_HOUR_SHARE_CEILING.values()) > 1.0


# ============================================================ locks


def test_both_hands_expands_to_both() -> None:
    """The naive version treats BOTH_HANDS as its own resource and lets a one-handed
    action start during a two-handed one."""
    typing = spec("typing_short")
    assert InteractionLock.LEFT_HAND in typing.effective_locks
    assert InteractionLock.RIGHT_HAND in typing.effective_locks
    assert InteractionLock.BOTH_HANDS not in typing.effective_locks


def test_lock_table_refuses_a_double_claim() -> None:
    table = LockTable()
    table.claim(spec("mouse_move"), until_monotonic=10.0)
    assert not table.permits(spec("mouse_click"))
    with pytest.raises(LockConflict, match="right_hand"):
        table.claim(spec("mouse_click"), until_monotonic=11.0)


def test_coffee_blocks_typing() -> None:
    """The brief's first named impossible combination."""
    table = LockTable()
    table.claim_locks(
        CHAINS["COFFEE_DRINK"].required_locks(),
        action_id="COFFEE_DRINK",
        until_monotonic=20.0,
        chain_id="COFFEE_DRINK",
    )
    assert table.holds_object()
    assert not table.permits(spec("typing_short"))
    assert not table.permits(spec("typing_long"))


def test_pen_blocks_the_same_hand_on_the_mouse() -> None:
    """The brief's second: while writing, no mouse action with that hand."""
    table = LockTable()
    table.claim_locks(
        CHAINS["NOTE_WRITE"].required_locks(),
        action_id="NOTE_WRITE",
        until_monotonic=20.0,
        chain_id="NOTE_WRITE",
    )
    assert not table.permits(spec("mouse_move"))
    # The left hand is still free, which is correct: the constraint is per hand.
    assert table.permits(spec("hand_to_chin"))


def test_release_chain_frees_everything_at_once() -> None:
    table = LockTable()
    table.claim_locks(
        CHAINS["COFFEE_DRINK"].required_locks(),
        action_id="COFFEE_DRINK", until_monotonic=20.0, chain_id="COFFEE_DRINK",
    )
    table.release_chain("COFFEE_DRINK")
    assert table.is_free


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    actions=st.lists(st.sampled_from(sorted(SCHEDULABLE)), min_size=1, max_size=40),
)
def test_property_no_two_held_actions_share_a_lock(actions: list[str]) -> None:
    """The lock invariant, over sequences nobody thought to write down."""
    table = LockTable()
    held: dict[str, frozenset[InteractionLock]] = {}
    for action_id in actions:
        action = spec(action_id)
        if not table.permits(action):
            continue
        table.claim(action, until_monotonic=1000.0)
        held[action_id] = action.effective_locks
        seen: set[InteractionLock] = set()
        for locks in held.values():
            assert not (seen & locks), f"{action_id} double-claimed {seen & locks}"
            seen |= locks


# ============================================================ cooldowns


def test_cooldowns_are_sampled_not_constant() -> None:
    rng = random.Random(4)
    table = CooldownTable()
    samples = {table.arm("mouse_move", float(i) * 1000.0, rng) for i in range(20)}
    assert len(samples) > 15, "cooldowns are not varying"


def test_group_cooldown_blocks_every_member() -> None:
    rng = random.Random(4)
    table = CooldownTable()
    table.arm("adjust_left", 0.0, rng)
    for sibling in ("adjust_right", "press_earcup", "settle_headphones"):
        assert not table.ready(sibling, 10.0), f"{sibling} escaped the group cooldown"


def test_force_ready_clears_both_levels() -> None:
    rng = random.Random(4)
    table = CooldownTable()
    table.arm("adjust_left", 0.0, rng)
    table.force_ready("adjust_left")
    assert table.ready("adjust_left", 0.1)


# ============================================================ anti-repetition


def _entry(action_id: str, at: float) -> HistoryEntry:
    action = spec(action_id)
    return HistoryEntry(
        action_id=action_id,
        category=action.category,
        anti_repeat_key=action.anti_repeat_key,
        started_monotonic=at,
        duration_seconds=1.0,
    )


def test_penalty_is_graded_not_a_veto() -> None:
    """The brief: a penalty rather than only hard exclusions, so actions can recur."""
    history = ActionHistory()
    history.record(_entry("mouse_move", 0.0))
    penalty, factors = history.penalty("mouse_move")
    assert 0.0 < penalty < 1.0, "an immediate repeat must be penalised, not forbidden"
    assert "recency_short" in factors


def test_penalty_decays_with_distance() -> None:
    history = ActionHistory()
    history.record(_entry("watch_check", 0.0))
    near, _ = history.penalty("watch_check")
    for i in range(12):
        history.record(_entry("blink" if i % 2 else "finger_tap", float(i)))
    far, _ = history.penalty("watch_check")
    assert far > near, "the penalty must decay as the action recedes"


def test_trigram_memory_penalises_a_repeated_triple() -> None:
    """The rule that actually stops a stream reading as looped."""
    history = ActionHistory()
    for _ in range(2):
        for action_id in ("lean_forward", "chart_inspect", "typing_short"):
            history.record(_entry(action_id, 0.0))
    history.record(_entry("lean_forward", 10.0))
    history.record(_entry("chart_inspect", 11.0))
    assert history.would_repeat_trigram("typing_short")
    penalty, factors = history.penalty("typing_short")
    assert "trigram" in factors
    assert penalty < 0.2


def test_category_ceiling_zeroes_a_saturated_family() -> None:
    history = ActionHistory()
    for i in range(40):
        history.record(_entry("adjust_left", float(i)))
    penalty, factors = history.penalty("adjust_right")
    assert factors.get("category_ceiling") == 0.0
    assert penalty == 0.0


# ============================================================ blink


def test_blink_intervals_stay_inside_their_bounds_and_vary() -> None:
    rng = random.Random(9)
    driver = BlinkDriver()
    fatigue = WorkRhythm()
    intervals = [
        driver.schedule(float(i) * 10.0, rng, state=CharacterState.IDLE_FOCUS, fatigue=fatigue)
        for i in range(400)
    ]
    assert all(BLINK_MIN_SECONDS <= i <= BLINK_MAX_SECONDS for i in intervals)
    assert len({round(i, 3) for i in intervals}) > 350, "blink intervals are not varying"
    # No two consecutive intervals equal: the brief forbids a fixed period.
    assert all(a != b for a, b in pairwise(intervals))


def test_concentration_suppresses_blinking() -> None:
    """People hold their eyes open while reading something closely."""
    rng = random.Random(1)
    fatigue = WorkRhythm()
    idle = BlinkDriver()
    analysing = BlinkDriver()
    idle_mean = sum(
        idle.schedule(float(i), random.Random(i), state=CharacterState.IDLE_FOCUS, fatigue=fatigue)
        for i in range(300)
    ) / 300
    analysing_mean = sum(
        analysing.schedule(
            float(i), random.Random(i), state=CharacterState.ANALYZING, fatigue=fatigue
        )
        for i in range(300)
    ) / 300
    assert analysing_mean > idle_mean
    assert rng is not None  # keep the seeded rng referenced


def test_blink_variants_cannot_cluster() -> None:
    """Independent 8 % draws let two double blinks land two seconds apart."""
    driver = BlinkDriver()
    rng = random.Random(0)
    doubles = [
        now for now in (float(i) * 2.0 for i in range(400))
        if driver.kind(rng, now) == "double_blink"
    ]
    gaps = [b - a for a, b in pairwise(doubles)]
    assert all(gap >= VARIANT_BLINK_MIN_GAP_SECONDS["double_blink"] for gap in gaps)


def test_blink_is_suppressed_while_the_lid_is_closing() -> None:
    driver = BlinkDriver()
    driver.next_at = 0.0
    driver.mark_performed(0.0, 400)
    assert not driver.due(0.1)
    assert driver.due(0.5)


# ============================================================ bands and modulation


def test_band_rate_span_is_narrow() -> None:
    """A man moving 4x as often reads as panicking, not as alert."""
    rates = [profile_for(band).rate for band in IntensityBand]
    assert max(rates) / min(rates) < 3.2


def test_dormant_band_cannot_fire_a_reaction() -> None:
    """Expressed as an unreachable threshold rather than a special case."""
    assert profile_for(IntensityBand.B0_DORMANT).reaction_threshold > 1.0


def test_behavior_energy_rises_with_the_band() -> None:
    previous = -1.0
    for band in IntensityBand:
        energy = behavior_energy(band=band, market_energy=50.0, energy_velocity=0.0)
        assert energy > previous
        previous = energy


def test_absent_market_energy_is_neutral_not_zero() -> None:
    """The brief forbids replacing unavailable values with fake zeros."""
    absent = behavior_energy(band=IntensityBand.B2_STEADY, market_energy=None)
    zero = behavior_energy(band=IntensityBand.B2_STEADY, market_energy=0.0)
    assert absent > zero


def test_activity_damps_energy() -> None:
    """Negative feedback is what makes work arrive in bouts, not at a constant drip."""
    calm = behavior_energy(band=IntensityBand.B3_FOCUSED, market_energy=60.0, recent_activity=0.0)
    busy = behavior_energy(band=IntensityBand.B3_FOCUSED, market_energy=60.0, recent_activity=1.0)
    assert busy < calm


def test_adjacent_band_rates_overlap() -> None:
    """Energy must bridge every band boundary, so no classification flip is a gear change.

    Overlap, not proximity: B2 at full energy must reach at least B3 at zero energy, so
    a regime change that moves the band one step cannot move the action rate at all
    unless the energy moved too.
    """
    bands = list(IntensityBand)
    for lower, upper in pairwise(bands):
        assert action_rate_multiplier(lower, 1.0) >= action_rate_multiplier(upper, 0.0), (
            f"{lower.value} at full energy does not reach {upper.value} at zero"
        )


def test_music_reaches_only_the_music_actions() -> None:
    """Why music is structurally weaker than the market, not just tuned weaker."""
    state = _state(bpm=174, music_energy=1.0)
    for action_id, action in CATALOG.items():
        factor = music_weight_factor(action.music_bias, state, reactivity=1.5)
        if action.category is ActionCategory.MUSIC:
            assert factor != 1.0, action_id
        else:
            assert factor == 1.0, f"{action_id} is affected by music"


def test_music_nod_ceiling_cannot_be_raised() -> None:
    """No reactivity setting may exceed the amplitude clamp."""
    for reactivity in (0.0, 1.0, 1.5, 99.0):
        policy = rhythm_policy(_state(bpm=174, music_energy=1.0), reactivity=reactivity)
        assert policy.max_nod_degrees <= 1.1
        assert policy.nod_probability <= 1.0


def test_fast_and_slow_tempi_both_halve_the_subdivision() -> None:
    assert rhythm_policy(_state(bpm=174)).beat_subdivision == 2
    assert rhythm_policy(_state(bpm=72)).beat_subdivision == 2
    assert rhythm_policy(_state(bpm=110)).beat_subdivision == 1


def test_silence_yields_no_rhythm_policy() -> None:
    """Nodding to silence is not a thing."""
    policy = rhythm_policy(_state(bpm=None))
    assert policy.bpm is None
    assert policy.nod_probability == 0.0


# The long-run rhythm moved to `tradefix_radio/visual/rhythm.py` after the V6 soak found
# the one-directional accumulator pinning at its ceiling. Its own tests live in
# `tests/unit/test_visual_rhythm.py`, which cover the cycle, the bounds, the aperiodicity
# and the recovery influences. What stays here is only what this module is about: that
# the rhythm still reaches the gaze and blink systems.


def test_fatigue_still_reaches_blink_and_dwell() -> None:
    rhythm = WorkRhythm()
    rhythm.fatigue_level = 0.05
    fresh_blink, fresh_dwell = rhythm.blink_rate_scale(), rhythm.dwell_scale()
    rhythm.fatigue_level = 0.85
    assert rhythm.blink_rate_scale() < fresh_blink, "fatigue does not affect blinking"
    assert rhythm.dwell_scale() > fresh_dwell, "fatigue does not lengthen holds"


# ============================================================ state machine


def test_executing_is_reachable_only_from_deliberation() -> None:
    """Nobody types an order out of idle."""
    for origin, options in STATE_TRANSITIONS.items():
        targets = {target for target, _ in options}
        if CharacterState.EXECUTING in targets:
            assert origin in EXECUTING_PREDECESSORS, (
                f"{origin.value} can reach EXECUTING but is not a permitted predecessor"
            )


def test_every_state_can_be_left() -> None:
    """A state with no outgoing edge is a deadlock."""
    for state in CharacterState:
        if state is CharacterState.MARKET_REACTION:
            continue  # entered by trigger, listed in the graph for its exits
        assert STATE_TRANSITIONS.get(state), f"{state.value} has no transitions"
        others = {t for t, _ in STATE_TRANSITIONS[state] if t is not state}
        assert others, f"{state.value} can only transition to itself"


def test_reaction_mostly_resolves_into_analysis() -> None:
    """Resolving straight to idle implies he saw something and dismissed it."""
    options = dict(STATE_TRANSITIONS[CharacterState.MARKET_REACTION])
    assert options[CharacterState.ANALYZING] > options[CharacterState.IDLE_FOCUS]


# ============================================================ director


def _director(seed: int = 5) -> tuple[BehaviorDirector, VirtualClock]:
    clock = VirtualClock(start=datetime(2026, 10, 3, 22, 0, tzinfo=UTC))
    return BehaviorDirector(clock=clock, rng=random.Random(seed)), clock


def test_director_runs_without_artwork() -> None:
    """The headline acceptance criterion: behaviour simulates with no art at all."""
    director, _ = _director()
    outputs = director.advance_to(_state(), 120.0)
    assert sum(len(o.actions) for o in outputs) > 20


def test_force_idle_pins_the_state() -> None:
    director, _ = _director()
    director.force_idle = True
    director.advance_to(_state(IntensityBand.B5_PEAK), 60.0)
    assert director.character_state is CharacterState.IDLE_FOCUS


def test_trigger_respects_locks_even_though_it_bypasses_cooldowns() -> None:
    """An operator click must not be able to produce the floating mug."""
    director, _ = _director()
    first = director.trigger("mouse_move")
    assert first is not None
    assert director.trigger("mouse_click") is None, "a held lock was bypassed"


def test_stale_feed_fires_no_reactions() -> None:
    """A reaction with no market event behind it is a fabricated market event."""
    director, _ = _director()
    stale = _state(IntensityBand.B0_DORMANT, trust=FeedTrust.STALE, salience=0.0)
    outputs = director.advance_to(stale, 600.0)
    fired = [
        action for out in outputs for action in out.actions
        if action.category is ActionCategory.REACTION
    ]
    assert not fired


def test_symbol_change_does_not_reset_behaviour() -> None:
    """The brief: when the symbol changes, do NOT reset the character."""
    director, clock = _director()
    director.advance_to(_state(symbol="XAUUSD"), 300.0)
    before_state = director.character_state
    before_count = director.history.total_recorded
    before_fatigue = director.rhythm.fatigue_level

    director.tick(_state(symbol="BTCUSD"))

    assert director.character_state is before_state, "state was reset on a symbol change"
    assert director.history.total_recorded >= before_count, "history was cleared"
    # The phase accrues one tick's worth; what must not happen is a drop, which is what
    # a reset would look like.
    assert before_fatigue <= director.rhythm.fatigue_level < before_fatigue + 1e-4
    assert clock is not None


def test_market_energy_changes_action_density() -> None:
    """Acceptance criterion 4, measured rather than asserted."""
    quiet, _ = run_scenario("quiet", 1800.0, seed=7)
    peak, _ = run_scenario("extreme_volatility", 1800.0, seed=7)
    assert peak.deliberate_total > quiet.deliberate_total * 1.4


def test_bpm_subtly_affects_rhythmic_actions() -> None:
    """Acceptance criterion 5: subtle, and only on the rhythmic actions."""
    low, _ = run_scenario("low_bpm", 3600.0, seed=11)
    high, _ = run_scenario("high_bpm", 3600.0, seed=11)
    low_music = low.category_counts.get(ActionCategory.MUSIC.value, 0)
    high_music = high.category_counts.get(ActionCategory.MUSIC.value, 0)
    assert high_music > low_music, "BPM had no effect on rhythmic actions"
    # And subtle: music must never dominate.
    assert high.category_share(ActionCategory.MUSIC) < 0.12


# ============================================================ gaze


def test_gaze_mapping_is_injective() -> None:
    """Two targets resolving to one surface silently doubles that screen's share."""
    from tradefix_radio.visual.geometry import _GAZE_SURFACE

    surfaces = list(_GAZE_SURFACE.values())
    assert len(surfaces) == len(set(surfaces)), "two gaze targets share a surface"


def test_characters_left_is_east() -> None:
    """He faces -Y, so his left is +X. Getting this backwards is invisible as a bug."""
    blockout = default_blockout()
    left = blockout.gaze_target(GazeTarget.MONITOR_LEFT)
    right = blockout.gaze_target(GazeTarget.MONITOR_RIGHT)
    assert left.yaw_degrees > 0 > right.yaw_degrees


def test_gaze_never_snaps() -> None:
    """A floor on transit, including inside a reaction."""
    _, simulator = run_scenario("breakout", 900.0, seed=2, salience_spike_every_seconds=60.0)
    assert not simulator.run(0.0).overlap_violations


def test_camera_glance_is_rationed_over_an_hour() -> None:
    report, _ = run_scenario("range", 3600.0, seed=4)
    assert report.camera_gaze_share <= 0.006


def test_most_gaze_is_on_screens() -> None:
    report, _ = run_scenario("range", 3600.0, seed=4)
    assert report.gaze_screen_share > 0.80


# ============================================================ cameras


def test_camera_minimum_holds_respect_the_floor() -> None:
    """No market condition justifies cutting faster than every 30 seconds."""
    for camera_id, meta in CAMERA_METADATA.items():
        assert meta.minimum_hold_seconds >= 30.0, camera_id
        assert meta.minimum_hold_seconds < meta.maximum_hold_seconds, camera_id


def test_camera_state_is_a_stub_with_no_automatic_selection() -> None:
    """The camera director is a later phase; this must not quietly become one."""
    state = CameraState()
    assert not hasattr(state, "select")
    assert state.auto is False


def test_camera_cut_rejects_an_unknown_id() -> None:
    state = CameraState()
    with pytest.raises(KeyError, match="CAM_9"):
        state.cut_to("CAM_9", 0.0)


def test_every_camera_affinity_names_a_real_camera() -> None:
    for action_id, action in CATALOG.items():
        for camera_id in action.camera_affinity:
            assert camera_id in CAMERA_METADATA, f"{action_id} -> {camera_id}"


# ============================================================ contracts


def test_band_bias_none_means_unavailable_not_zero() -> None:
    bias = BandBiasV1(b0=None, b1=0.0, b2=1.0)
    assert bias.for_band(IntensityBand.B0_DORMANT) is None
    assert bias.for_band(IntensityBand.B1_QUIET) == 0.0


def test_stale_state_cannot_carry_an_active_band() -> None:
    """Made unrepresentable rather than merely discouraged."""
    with pytest.raises(ValueError, match="stale feed cannot drive"):
        VisualStateV1(
            at=datetime(2026, 10, 3, tzinfo=UTC),
            source_age_seconds=90.0,
            feed_trust=FeedTrust.STALE,
            active_symbol="XAUUSD",
            intensity_band=IntensityBand.B4_ALERT,
            behavior_energy=0.5,
        )


def test_neutral_state_is_safe() -> None:
    state = VisualStateV1.neutral(
        at=datetime(2026, 10, 3, tzinfo=UTC), reason="station unreachable"
    )
    assert not state.market_reactive
    assert state.reaction_salience == 0.0
    assert state.market_energy is None


def test_every_scenario_builds_a_valid_state() -> None:
    at = datetime(2026, 10, 3, tzinfo=UTC)
    for scenario in SCENARIOS.values():
        state = scenario.state(at)
        assert state.intensity_band is scenario.band
