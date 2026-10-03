"""The V8 Camera Director.

Two tiers, and the split mirrors the behaviour director's:

**Safety is absolute.** Never cut mid-blend, never during an object acquisition, never
within 2.5 s of a reaction, never twice inside 15 s, never below the 30 s floor, never to
`CAM_6` without a visible interaction. These assert hard and the soak reports zero.

**Pacing is a judgement.** Hold lengths, camera shares, motivation mix. Wide thresholds,
because the honest claim is "this does not read as a rotation", not "this exact number
is correct".
"""

from __future__ import annotations

import random
from datetime import datetime

import pytest

from tradefix_radio.core.clock import UTC
from tradefix_radio.visual.camera import (
    CAMERA_METADATA,
    DEFAULT_CAMERA,
    TRANSITION_CROSSFADE,
    TRANSITION_CUT,
    TRANSITION_PUSH_CONTINUE,
    CameraState,
)
from tradefix_radio.visual.camera_director import (
    ACQUISITION_STEPS,
    CAMERA_FAMILY,
    DESK_ANCHORS,
    DOUBLE_CUT_WINDOW,
    HOLD_DISTRIBUTION,
    HOME_BIAS_MAX,
    HOME_RETURN_SECONDS,
    MINIMUM_HOLD_FLOOR,
    MOTIVATION_PROBABILITY,
    PRIMARY_CAMERA,
    PUSH_CONTINUE_MAX_PER_HOUR,
    REACTION_LOCKOUT,
    TRANSITION_MS,
    ActionView,
    CameraDirector,
    CameraFamily,
    CutMotivation,
    CutVeto,
)
from tradefix_radio.visual.catalog import CHAINS, spec
from tradefix_radio.visual.contracts import (
    ActionCategory,
    CharacterActionV1,
    CharacterState,
    FeedTrust,
    IntensityBand,
    Interruptibility,
    TransitionState,
    VisualStateV1,
)
from tradefix_radio.visual.simulate import run_scenario


def _state(
    band: IntensityBand = IntensityBand.B2_STEADY,
    *,
    transition: TransitionState = TransitionState.NONE,
    music_energy: float | None = 0.5,
) -> VisualStateV1:
    return VisualStateV1(
        at=datetime(2026, 10, 3, 22, 0, tzinfo=UTC),
        source_age_seconds=0.4,
        feed_trust=FeedTrust.LIVE,
        active_symbol="XAUUSD",
        market_regime="normal_range",
        intensity_band=band,
        market_energy=50.0,
        music_bpm=110,
        music_energy=music_energy,
        transition_state=transition,
        broadcasting=True,
        behavior_energy=0.5,
    )


def _action(
    action_id: str = "mouse_move",
    *,
    blend_in_ms: int = 160,
    blend_out_ms: int = 220,
    duration_ms: int = 1_200,
    anchor: str | None = None,
    chain_id: str | None = None,
) -> CharacterActionV1:
    definition = spec(action_id)
    return CharacterActionV1(
        sequence=1,
        action_id=action_id,
        category=definition.category,
        started_at=datetime(2026, 10, 3, 22, 0, tzinfo=UTC),
        duration_ms=duration_ms,
        blend_in_ms=blend_in_ms,
        blend_out_ms=blend_out_ms,
        interruptibility=Interruptibility.ALWAYS,
        anchor=anchor,
        character_state=CharacterState.IDLE_FOCUS,
        chain_id=chain_id,
    )


def _view(action: CharacterActionV1, *, started: float, now: float = 0.0) -> ActionView:
    return ActionView(
        action=action,
        started_monotonic=started,
        ends_monotonic=started + action.total_ms / 1000.0,
    )


def _director(seed: int = 3, camera: str = PRIMARY_CAMERA) -> CameraDirector:
    return CameraDirector(
        rng=random.Random(seed), state=CameraState(camera_id=camera, auto=True)
    )


def _tick(
    director: CameraDirector,
    now: float,
    *,
    state: VisualStateV1 | None = None,
    character_state: CharacterState = CharacterState.IDLE_FOCUS,
    running: tuple[ActionView, ...] = (),
    chain_id: str | None = None,
    chain_step: int | None = None,
    chain_committed: bool = False,
):
    return director.tick(
        now=now,
        state=state or _state(),
        character_state=character_state,
        running=running,
        chain_id=chain_id,
        chain_step=chain_step,
        chain_committed=chain_committed,
    )


# ============================================================ geometry


def test_only_the_seven_frozen_cameras_exist() -> None:
    """V8 chooses between the V1 transforms. It does not invent geometry."""
    assert set(CAMERA_FAMILY) == set(CAMERA_METADATA)
    assert len(CAMERA_FAMILY) == 7


def test_every_camera_belongs_to_a_family() -> None:
    for camera_id in CAMERA_METADATA:
        assert isinstance(CAMERA_FAMILY[camera_id], CameraFamily)


def test_the_primary_camera_is_the_hero_front() -> None:
    """Acceptance criterion: `CAM_1` remains primary.

    It is also the composition every painted plate is graded against
    (`CAMERA_PLAN.md` §3), so the home shot and the reference shot are the same frame.
    """
    assert PRIMARY_CAMERA == "CAM_1"
    assert DEFAULT_CAMERA == "CAM_1"


def test_the_home_shot_has_the_largest_share_cap() -> None:
    caps = {
        camera_id: meta.hour_share_cap for camera_id, meta in CAMERA_METADATA.items()
    }
    assert max(caps, key=lambda camera_id: caps[camera_id]) == PRIMARY_CAMERA
    # And not by so much that it is the only shot.
    assert caps[PRIMARY_CAMERA] < 0.4


def test_the_home_bias_is_a_return_not_a_constant() -> None:
    """A flat bonus makes the home shot win every contest and the stream stops exploring."""
    director = _director(camera="CAM_3")
    director._last_used = {PRIMARY_CAMERA: 0.0}

    just_left = director._home_bias(5.0)
    halfway = director._home_bias(HOME_RETURN_SECONDS / 2)
    long_away = director._home_bias(HOME_RETURN_SECONDS * 3)

    assert just_left < halfway < long_away, "the bias does not grow with time away"
    assert long_away == pytest.approx(HOME_BIAS_MAX)
    assert just_left == pytest.approx(1.0, abs=0.05)


def test_the_home_bias_is_inert_while_the_home_shot_is_live() -> None:
    """It argues for coming back, never for staying — that is what the hold is for."""
    director = _director(camera=PRIMARY_CAMERA)
    director._last_used = {PRIMARY_CAMERA: 0.0}
    assert director._home_bias(10_000.0) == 1.0


def test_an_unvisited_home_shot_pulls_at_full_strength() -> None:
    director = _director(camera="CAM_3")
    director._last_used = {}
    assert director._home_bias(0.0) == HOME_BIAS_MAX


def test_hold_distributions_shorten_as_the_market_loudens() -> None:
    medians = [HOLD_DISTRIBUTION[band][1] for band in IntensityBand]
    assert medians == sorted(medians, reverse=True)
    for low, _, high in HOLD_DISTRIBUTION.values():
        assert low >= MINIMUM_HOLD_FLOOR
        assert high > low * 1.5, "the range is too narrow to read as varied"


def test_no_band_permits_cutting_below_the_floor() -> None:
    """The brief's absolute: no market condition justifies faster cutting."""
    for band in IntensityBand:
        assert HOLD_DISTRIBUTION[band][0] >= MINIMUM_HOLD_FLOOR


# ============================================================ holds


def test_holds_are_sampled_not_constant() -> None:
    director = _director()
    samples = {round(director._sample_hold(IntensityBand.B2_STEADY), 3) for _ in range(40)}
    assert len(samples) > 30, "hold lengths are not varying"


def test_sampled_holds_respect_the_band_range() -> None:
    director = _director()
    for band in IntensityBand:
        low, _, high = HOLD_DISTRIBUTION[band]
        for _ in range(200):
            held = director._sample_hold(band)
            assert held >= MINIMUM_HOLD_FLOOR
            assert low <= held <= high


def test_energy_scales_holds_but_never_below_the_floor() -> None:
    """An operator slider must not be able to produce music-video cutting."""
    for energy in (0.1, 0.5, 1.0, 1.6, 99.0):
        director = CameraDirector(rng=random.Random(7), energy=energy)
        for band in IntensityBand:
            for _ in range(80):
                assert director._sample_hold(band) >= MINIMUM_HOLD_FLOOR


# ============================================================ safety vetoes


def test_no_cut_below_the_absolute_floor() -> None:
    director = _director()
    _tick(director, 0.0)
    for now in (1.0, 10.0, 29.0):
        decision = _tick(director, now)
        assert not decision.changed
        assert CutVeto.BELOW_FLOOR.value in decision.vetoes.values()


def test_the_floor_subsumes_the_double_cut_window() -> None:
    """The 30 s floor already forbids anything the 15 s window would.

    Both are measured from the last cut, so the double-cut check can never be the veto
    that fires. It is kept as a guard against a future tuning pass lowering the floor,
    and this asserts the relationship rather than pretending the veto is reachable.
    """
    assert MINIMUM_HOLD_FLOOR > DOUBLE_CUT_WINDOW


def test_a_second_cut_cannot_follow_immediately() -> None:
    """The property itself, whichever veto enforces it."""
    director = _director()
    _tick(director, 0.0)
    director.request("CAM_1")
    assert _tick(director, 100.0).changed
    director.request("CAM_3")
    for offset in (1.0, 5.0, DOUBLE_CUT_WINDOW - 1.0, DOUBLE_CUT_WINDOW + 1.0):
        decision = _tick(director, 100.0 + offset)
        assert not decision.changed, f"cut again after {offset:.0f}s"
        assert decision.vetoes


def test_no_cut_inside_a_blend_window() -> None:
    """Two blends in flight on one joint is the visible pop the brief forbids."""
    director = _director()
    _tick(director, 0.0)
    action = _action(blend_in_ms=400)
    mid_blend = (_view(action, started=100.0),)
    director.request("CAM_1")
    decision = _tick(director, 100.1, running=mid_blend)
    assert not decision.changed
    assert CutVeto.MID_BLEND.value in decision.vetoes.values()


def test_a_cut_is_permitted_once_the_blend_has_elapsed() -> None:
    """The converse. The first implementation vetoed on `blend_in_ms` alone and so
    refused every cut while any blending action ran — which is nearly always."""
    director = _director()
    _tick(director, 0.0)
    action = _action(blend_in_ms=400, blend_out_ms=0, duration_ms=5_000)
    settled = (_view(action, started=100.0),)
    director.request("CAM_1")
    decision = _tick(director, 101.0, running=settled)
    assert decision.changed, decision.vetoes


def test_no_cut_during_an_object_acquisition() -> None:
    """A cut here shows a hand arriving at nothing, or a mug changing hands."""
    chain = CHAINS["COFFEE_DRINK"]
    for step, step_id in enumerate(chain.steps):
        if step_id not in ACQUISITION_STEPS:
            continue
        director = _director()
        _tick(director, 0.0)
        director.request("CAM_1")
        decision = _tick(
            director, 200.0, chain_id=chain.chain_id, chain_step=step, chain_committed=True
        )
        assert not decision.changed, f"cut during {step_id}"
        assert CutVeto.OBJECT_ACQUISITION.value in decision.vetoes.values()


def test_no_cut_within_the_reaction_lockout() -> None:
    """A cut on the reaction frame turns the moment into an edit."""
    director = _director()
    _tick(director, 0.0)
    _tick(director, 200.0, character_state=CharacterState.MARKET_REACTION)
    director.request("CAM_4")
    decision = _tick(
        director,
        200.0 + REACTION_LOCKOUT - 0.5,
        character_state=CharacterState.MARKET_REACTION,
    )
    assert not decision.changed
    assert CutVeto.REACTION_LOCKOUT.value in decision.vetoes.values()


def test_cam6_is_gated_not_weighted() -> None:
    """The acceptance criterion: the hands shot only when close interaction is visible.

    A gate rather than a small weight, because a weight however small eventually fires
    on an empty desk.
    """
    director = _director(camera="CAM_1")
    _tick(director, 0.0)
    chosen, _, vetoes = director._choose(
        300.0, _state(), CharacterState.EXECUTING, (), None, False
    )
    assert chosen != "CAM_6"
    assert vetoes.get("CAM_6") == CutVeto.CAM6_NO_INTERACTION.value


def test_cam6_becomes_available_with_a_desk_interaction() -> None:
    director = _director(camera="CAM_1")
    _tick(director, 0.0)
    typing = _view(
        _action("typing_long", anchor="ANCHOR_KEYBOARD_HOME_R", duration_ms=9_000),
        started=250.0,
    )
    _, _, vetoes = director._choose(
        300.0, _state(), CharacterState.EXECUTING, (typing,), None, False
    )
    assert "CAM_6" not in vetoes


def test_every_desk_anchor_is_a_real_blockout_anchor() -> None:
    from tradefix_radio.visual.geometry import default_blockout

    known = set(default_blockout().anchors)
    assert known >= DESK_ANCHORS, DESK_ANCHORS - known


def test_acquisition_steps_are_real_chain_steps() -> None:
    all_steps = {step for chain in CHAINS.values() for step in chain.steps}
    assert all_steps >= ACQUISITION_STEPS, ACQUISITION_STEPS - all_steps


def test_a_committed_chain_cannot_be_cut_away_from() -> None:
    """If the target cannot show it, it is vetoed rather than down-weighted."""
    director = _director(camera="CAM_1")
    _tick(director, 0.0)
    sip = _view(_action("sip", chain_id="COFFEE_DRINK", duration_ms=1_800), started=250.0)
    _, _, vetoes = director._choose(
        300.0, _state(), CharacterState.CAFFEINE_BREAK, (sip,), "COFFEE_DRINK", True
    )
    blocked = {
        camera
        for camera, reason in vetoes.items()
        if reason == CutVeto.COMMITTED_CHAIN_INVISIBLE.value
    }
    assert blocked, "a committed chain did not restrict the target set"
    affinity = set(spec("sip").camera_affinity)
    assert not (blocked & affinity), "a camera that CAN show the sip was vetoed"


# ============================================================ motivation


def test_a_cut_needs_a_motivation() -> None:
    """No motivation, no cut. A timer alone is the predictable rotation."""
    director = _director()
    _tick(director, 0.0)
    # Well past the floor but before the hold expires, with nothing happening.
    decisions = [_tick(director, now) for now in (40.0, 45.0, 50.0)]
    assert not any(d.changed for d in decisions)


def test_hold_expiry_eventually_cuts() -> None:
    director = _director()
    _tick(director, 0.0)
    target = director.hold_seconds
    decision = _tick(director, target + 1.0)
    assert decision.changed
    assert decision.motivation is CutMotivation.HOLD_EXPIRED


def test_licensed_motivations_are_rationed() -> None:
    """Licences rather than triggers. A guaranteed cut on every event is a rotation."""
    for motivation, probability in MOTIVATION_PROBABILITY.items():
        if motivation in (CutMotivation.HOLD_EXPIRED, CutMotivation.OPERATOR):
            assert probability == 1.0
        else:
            assert 0.0 < probability < 0.05, f"{motivation.value} at {probability}"


def test_a_track_transition_does_not_guarantee_a_cut() -> None:
    """Music is weaker than the market, and must not teach viewers to read a cut."""
    cuts = 0
    for seed in range(30):
        director = _director(seed)
        _tick(director, 0.0)
        half = director.hold_seconds * 0.7
        decision = _tick(
            director, half, state=_state(transition=TransitionState.OUTBOUND)
        )
        cuts += int(decision.changed)
    assert cuts < 10, f"{cuts}/30 track transitions cut immediately"


def test_no_cut_before_the_cameras_own_declared_minimum() -> None:
    """A licensed motivation may cut early, but never before the camera's own minimum.

    Driven over many ticks rather than one, because a licensed motivation is rationed to
    well under a percent per consideration — a single call almost never fires it, which
    is the point.
    """
    minimum = CAMERA_METADATA["CAM_5"].minimum_hold_seconds
    for seed in range(12):
        director = _director(seed, camera="CAM_5")
        _tick(director, 0.0)
        now = MINIMUM_HOLD_FLOOR + 1.0
        while now < minimum:
            decision = _tick(
                director, now, state=_state(IntensityBand.B5_PEAK)
            )
            assert not decision.changed, (
                f"cut at {now:.0f}s, before CAM_5's {minimum:.0f}s minimum"
            )
            now += 0.5


def test_a_licensed_motivation_does_eventually_cut_early() -> None:
    """The converse: rationed is not disabled."""
    cut_early = 0
    for seed in range(24):
        director = _director(seed, camera="CAM_1")
        _tick(director, 0.0)
        target = director.hold_seconds
        now = CAMERA_METADATA["CAM_1"].minimum_hold_seconds + 1.0
        while now < target - 1.0:
            if _tick(director, now, state=_state(IntensityBand.B5_PEAK)).changed:
                cut_early += 1
                break
            now += 0.5
    assert cut_early > 0, "licensed motivations never cut early in 24 runs"


# ============================================================ anti-repetition


def test_the_camera_just_left_is_not_returned_to() -> None:
    """A -> B -> A is the alternation the brief rules out."""
    director = _director()
    _tick(director, 0.0)
    # Deliberately not the home shot: requesting the camera already live is a no-op, and
    # this test needs a real cut to have happened before the second one.
    director.request("CAM_3")
    _tick(director, 100.0)
    first = director.state.camera_id
    previous = director.state.recent[-1]
    assert first == "CAM_3"
    # Now cut again without a request; it must not go back.
    for now in range(200, 900, 20):
        decision = _tick(director, float(now))
        if decision.changed:
            assert decision.camera_id != previous
            return
    pytest.fail("no second cut occurred")


def test_family_penalty_discourages_three_hero_shots_running() -> None:
    """`CAM_1 -> CAM_7 -> CAM_1` is three hero shots even though no camera repeated."""
    director = _director(camera="CAM_1")
    director.state.recent = ["CAM_7", "CAM_1", "CAM_7"]
    hero = director._family_penalty("CAM_7")
    other = director._family_penalty("CAM_5")
    assert hero < other * 0.3


def test_recency_penalty_favours_a_starved_camera() -> None:
    director = _director()
    director._last_used = {"CAM_3": 0.0, "CAM_1": 3_000.0}
    assert director._recency_penalty("CAM_3", 3_100.0) > director._recency_penalty(
        "CAM_1", 3_100.0
    )


def test_diversity_does_not_fire_at_startup() -> None:
    """With `_last_used` empty every camera reads as starved.

    That produced a fifth of all cuts from DIVERSITY in the first two hours, which is a
    startup artefact and not a narrowing stream.
    """
    director = _director()
    _tick(director, 0.0)
    assert director._starved_cameras(10.0) == ()


# ============================================================ transitions


def test_only_three_transitions_exist() -> None:
    """No wipes, no zoom-spin, no streamer effects."""
    assert set(TRANSITION_MS) == {
        TRANSITION_CUT,
        TRANSITION_CROSSFADE,
        TRANSITION_PUSH_CONTINUE,
    }
    assert TRANSITION_MS[TRANSITION_CUT] == 0
    assert TRANSITION_MS[TRANSITION_CROSSFADE] <= 500
    assert TRANSITION_MS[TRANSITION_PUSH_CONTINUE] <= 1_500


def test_the_push_continuation_is_capped_per_hour() -> None:
    """Its entire value is being rare."""
    director = _director(camera="CAM_1")
    director._push_times = [0.0, 10.0]
    assert len(director._push_times) >= PUSH_CONTINUE_MAX_PER_HOUR
    assert director._transition("CAM_4", 100.0) != TRANSITION_PUSH_CONTINUE


# ============================================================ operator


def test_an_operator_request_queues_behind_the_vetoes() -> None:
    """One click must not be able to produce a broken frame, live, with no undo."""
    director = _director()
    _tick(director, 0.0)
    director.request("CAM_3")
    blocked = _tick(director, 5.0)  # below the floor
    assert not blocked.changed
    allowed = _tick(director, 200.0)
    assert allowed.changed
    assert allowed.camera_id == "CAM_3"
    assert allowed.motivation is CutMotivation.OPERATOR


def test_an_unknown_camera_request_fails_loudly() -> None:
    director = _director()
    with pytest.raises(KeyError, match="CAM_9"):
        director.request("CAM_9")


def test_auto_off_stops_the_director_entirely() -> None:
    from tradefix_radio.core.clock import VirtualClock
    from tradefix_radio.visual.director import BehaviorDirector

    clock = VirtualClock(start=datetime(2026, 10, 3, 22, 0, tzinfo=UTC))
    director = BehaviorDirector(clock=clock, rng=random.Random(5), camera_auto=False)
    before = director.camera.camera_id
    director.advance_to(_state(), 900.0)
    assert director.camera.camera_id == before
    assert director.last_camera_decision is None


# ============================================================ soak


def test_two_hour_camera_soak_is_safe() -> None:
    report, _ = run_scenario(
        "range", "2h", seed=5, salience_spike_every_seconds=420.0, tick_seconds=0.25
    )
    camera = report.camera
    assert not camera.unsafe_cuts, camera.unsafe_cuts
    assert not camera.cam6_without_interaction, camera.cam6_without_interaction
    assert camera.min_hold >= MINIMUM_HOLD_FLOOR - 1e-6


def test_holds_are_mostly_long() -> None:
    """The brief: 30 s to several minutes, most shots long. Not 10 s cutting."""
    report, _ = run_scenario(
        "range", "2h", seed=5, salience_spike_every_seconds=420.0, tick_seconds=0.25
    )
    camera = report.camera
    assert camera.median_hold >= 60.0, f"median hold {camera.median_hold:.0f}s"
    assert camera.cuts_per_hour <= 45.0, f"{camera.cuts_per_hour:.1f} cuts/h"
    long_holds = [h for h in camera.holds if h >= 60.0]
    assert len(long_holds) / len(camera.holds) > 0.6, "most shots are not long"


@pytest.mark.slow
def test_no_short_deterministic_rotation() -> None:
    """The brief's `1 -> 2 -> 3 -> 1 -> 2 -> 3` test."""
    report, _ = run_scenario(
        "range", "8h", seed=5, salience_spike_every_seconds=420.0, tick_seconds=0.25
    )
    camera = report.camera
    assert camera.alternations == 0, f"{camera.alternations} A-B-A patterns"
    assert camera.top_sequence_share < 0.12, (
        f"one 3-sequence takes {camera.top_sequence_share * 100:.1f} % of all"
    )
    assert len(camera.repeated_sequences()) > 25


@pytest.mark.slow
def test_all_seven_cameras_are_used() -> None:
    report, _ = run_scenario(
        "range", "8h", seed=5, salience_spike_every_seconds=420.0, tick_seconds=0.25
    )
    assert len(report.camera.camera_shares) == 7, report.camera.camera_shares


@pytest.mark.slow
def test_cam_1_remains_primary() -> None:
    """The acceptance criterion, asserted as *primary* rather than as a floor.

    The earlier form of this test required only `>= 0.15` for the primary camera and
    `>= 0.28` for the hero family. Both passed while `CAM_1` sat third in airtime behind
    `CAM_7` (22.1 %) and `CAM_3` (19.8 %) — a floor is not primacy, and the test said
    nothing about the thing it was named for. `CAM_1` must hold the single largest share,
    and hold it clearly enough that a viewer would call it the stream's camera.
    """
    report, _ = run_scenario(
        "range", "8h", seed=5, salience_spike_every_seconds=420.0, tick_seconds=0.25
    )
    shares = report.camera.camera_shares
    ranked = sorted(shares.items(), key=lambda item: -item[1])
    leader, leader_share = ranked[0]
    runner_up, runner_share = ranked[1]

    assert leader == PRIMARY_CAMERA, (
        f"{leader} leads with {leader_share * 100:.1f} %; "
        f"{PRIMARY_CAMERA} has {shares.get(PRIMARY_CAMERA, 0.0) * 100:.1f} %"
    )
    assert leader_share >= runner_share * 1.15, (
        f"{leader} {leader_share * 100:.1f} % barely leads {runner_up} "
        f"{runner_share * 100:.1f} % — no clear master shot"
    )
    # Primary, not exclusive. The share cap is what holds this side of the bound.
    assert leader_share <= CAMERA_METADATA[PRIMARY_CAMERA].hour_share_cap + 0.06


@pytest.mark.slow
def test_the_home_shot_leads_in_every_market_condition() -> None:
    """Primacy must not be an artefact of one scenario's band mix."""
    for scenario, seed in (("quiet", 11), ("breakout", 7), ("extreme_volatility", 13)):
        report, _ = run_scenario(
            scenario, "2h", seed=seed, salience_spike_every_seconds=420.0,
            tick_seconds=0.25,
        )
        shares = report.camera.camera_shares
        leader = max(shares, key=lambda camera_id: shares[camera_id])
        assert leader == PRIMARY_CAMERA, f"{scenario}: {leader} leads, not {PRIMARY_CAMERA}"


@pytest.mark.slow
def test_cam_5_provides_breathing_room() -> None:
    report, _ = run_scenario(
        "range", "8h", seed=5, salience_spike_every_seconds=420.0, tick_seconds=0.25
    )
    assert report.camera.camera_shares.get("CAM_5", 0.0) >= 0.08


@pytest.mark.slow
def test_cam_6_is_rationed() -> None:
    """Used, but only for close interaction, and capped."""
    report, _ = run_scenario(
        "range", "8h", seed=5, salience_spike_every_seconds=420.0, tick_seconds=0.25
    )
    share = report.camera.camera_shares.get("CAM_6", 0.0)
    assert 0.0 < share <= CAMERA_METADATA["CAM_6"].hour_share_cap + 0.02, share


@pytest.mark.slow
def test_hard_cuts_dominate() -> None:
    report, _ = run_scenario(
        "range", "8h", seed=5, salience_spike_every_seconds=420.0, tick_seconds=0.25
    )
    counts = report.camera.transition_counts
    total = sum(counts.values())
    assert counts.get(TRANSITION_CUT, 0) / total > 0.6


@pytest.mark.slow
def test_hold_expiry_is_the_dominant_motivation() -> None:
    """Most cuts should be ordinary. Licensed motivations are the exception."""
    report, _ = run_scenario(
        "range", "8h", seed=5, salience_spike_every_seconds=420.0, tick_seconds=0.25
    )
    counts = report.camera.motivation_counts
    total = sum(counts.values())
    assert counts.get(CutMotivation.HOLD_EXPIRED.value, 0) / total > 0.5, counts


@pytest.mark.slow
def test_market_energy_shortens_holds() -> None:
    """QUIET: longer hero and wide shots. EXTREME: more active framing. Still restrained."""
    quiet, _ = run_scenario("quiet", "8h", seed=9, tick_seconds=0.25)
    peak, _ = run_scenario("extreme_volatility", "8h", seed=9, tick_seconds=0.25)
    assert peak.camera.median_hold < quiet.camera.median_hold
    assert peak.camera.cuts_per_hour > quiet.camera.cuts_per_hour
    # Restrained: even at peak this is not esports cutting.
    assert peak.camera.median_hold >= 45.0
    assert peak.camera.cuts_per_hour <= 60.0


@pytest.mark.slow
def test_quiet_markets_favour_the_wide_and_hero_shots() -> None:
    quiet, _ = run_scenario("quiet", "8h", seed=9, tick_seconds=0.25)
    peak, _ = run_scenario("extreme_volatility", "8h", seed=9, tick_seconds=0.25)
    calm_shares = quiet.camera.camera_shares
    loud_shares = peak.camera.camera_shares
    assert calm_shares.get("CAM_5", 0.0) > loud_shares.get("CAM_5", 0.0)


@pytest.mark.slow
def test_breakouts_favour_the_over_shoulder_and_close_shots() -> None:
    quiet, _ = run_scenario("quiet", "8h", seed=9, tick_seconds=0.25)
    peak, _ = run_scenario("extreme_volatility", "8h", seed=9, tick_seconds=0.25)
    detail_quiet = quiet.camera.camera_shares.get("CAM_3", 0.0)
    detail_peak = peak.camera.camera_shares.get("CAM_3", 0.0)
    assert detail_peak > detail_quiet


def test_camera_behaviour_is_reproducible_from_a_seed() -> None:
    first, _ = run_scenario("range", "2h", seed=77, tick_seconds=0.25)
    second, _ = run_scenario("range", "2h", seed=77, tick_seconds=0.25)
    assert first.camera.camera_sequence == second.camera.camera_sequence
    assert first.camera.holds == second.camera.holds


@pytest.mark.slow
def test_the_camera_director_does_not_disturb_behaviour_invariants() -> None:
    """V8 must not break V6. The structural invariants still hold with cameras live."""
    report, _ = run_scenario(
        "range", "8h", seed=13, salience_spike_every_seconds=420.0, tick_seconds=0.25
    )
    assert report.is_structurally_sound, report.summary()
    assert not report.has_visible_loop
    assert report.category_share(ActionCategory.POSTURE) < 0.08
