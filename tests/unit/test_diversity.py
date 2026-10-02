"""DiversityDirector — the anti-boredom system (§11, §12, milestone 3.4).

§11 opens with "This requirement is extremely important", so every rule it names gets its
own test. The exit criterion is that each §11 rule is **independently enforced** and that
the diversity score falls on monotonous history and recovers after forced divergence.

The tests deliberately construct history directly rather than going through the director.
A rule that only works when the director happens to produce the right history is not
enforced; it is lucky.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradefix_radio.config.schema import DiversitySettings
from tradefix_radio.director.diversity import (
    TARGET_INSTRUMENTAL_RATIO,
    DiversityDirector,
)
from tradefix_radio.director.history import (
    HistoryEntry,
    ProgrammingHistory,
    shannon_entropy,
    spread_score,
    variety_score,
)

START = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def settings(**overrides: object) -> DiversitySettings:
    return DiversitySettings.model_validate(overrides)


def director(**overrides: object) -> DiversityDirector:
    return DiversityDirector(settings(**overrides))


def entry(
    index: int,
    *,
    genre: str = "trap",
    bpm: int = 140,
    key: str = "F# minor",
    duration: float = 200.0,
    instrumental: bool = False,
    vocal_style: str = "rap",
    topic: str | None = "discipline",
    secondary: str | None = None,
    persona: str | None = "tf01",
    signature: str | None = None,
    energy: float = 60.0,
) -> HistoryEntry:
    """A history entry. ``index`` 0 is the most recent once passed through history()."""
    return HistoryEntry(
        track_id=f"TF-{index:05d}",
        genre=genre,
        bpm=bpm,
        musical_key=key,
        duration_seconds=duration,
        is_instrumental=instrumental,
        vocal_style=vocal_style,
        primary_topic=topic,
        secondary_topic=secondary,
        persona_id=persona,
        blueprint_signature=signature or f"sig{index:060d}",
        energy_at_generation=energy,
        regime_at_generation="normal_range",
        created_at=START - timedelta(minutes=4 * index),
        played_at=START - timedelta(minutes=4 * index),
    )


def history(*entries: HistoryEntry) -> ProgrammingHistory:
    return ProgrammingHistory(list(entries))


# ---------------------------------------------------------------- §11 genre rules


def test_the_genre_run_limit_is_enforced() -> None:
    """§11: "same genre maximum: 2 consecutive tracks"."""
    past = history(entry(0, genre="trap"), entry(1, genre="trap"), entry(2, genre="lofi"))
    constraints = director(max_same_genre_consecutive=2).genre_constraints(past)
    run = next(c for c in constraints if c.name == "genre_run")
    assert not run.permits("trap")
    assert run.permits("lofi")


def test_the_genre_run_limit_does_not_fire_below_the_threshold() -> None:
    past = history(entry(0, genre="trap"), entry(1, genre="lofi"))
    constraints = director(max_same_genre_consecutive=2).genre_constraints(past)
    assert not [c for c in constraints if c.name == "genre_run"]


def test_a_broken_run_resets_the_count() -> None:
    """"Consecutive" means consecutive from the most recent, as a listener hears it."""
    past = history(
        entry(0, genre="trap"), entry(1, genre="lofi"), entry(2, genre="trap"),
        entry(3, genre="trap"),
    )
    constraints = director(max_same_genre_consecutive=2).genre_constraints(past)
    assert not [c for c in constraints if c.name == "genre_run"]


def test_the_genre_share_ceiling_catches_alternating_monotony() -> None:
    """A,B,A,B,A,B breaks no run rule and is still monotonous.

    The share ceiling is what catches the slow collapse that consecutive-run limits miss.
    """
    entries = [
        entry(index, genre="trap" if index % 2 == 0 else "lofi") for index in range(20)
    ]
    constraints = director(
        horizon_medium=20, max_genre_share_medium=0.35
    ).genre_constraints(history(*entries))
    share = next(c for c in constraints if c.name == "genre_share")
    assert not share.permits("trap")
    assert not share.permits("lofi")
    assert share.permits("dnb")


def test_the_share_ceiling_needs_enough_history_to_be_meaningful() -> None:
    """Two tracks in, every genre is 50% of history; that is not a collapse."""
    constraints = director(horizon_medium=20).genre_constraints(
        history(entry(0, genre="trap"), entry(1, genre="trap"))
    )
    assert not [c for c in constraints if c.name == "genre_share"]


def test_an_empty_history_produces_no_genre_constraints() -> None:
    assert director().genre_constraints(history()) == []


# ---------------------------------------------------------------- §11 BPM / key


def test_the_bpm_tolerance_window_is_enforced() -> None:
    """§11: "same BPM +/- 4: not within previous 4 tracks"."""
    past = history(entry(0, bpm=140))
    constraint = director(bpm_tolerance=4, bpm_repeat_horizon=4).bpm_constraint(past)
    for bpm in (136, 138, 140, 142, 144):
        assert not constraint.permits(bpm), bpm
    assert constraint.permits(135)
    assert constraint.permits(145)


def test_the_bpm_window_only_looks_back_the_configured_horizon() -> None:
    past = history(*[entry(index, bpm=140 if index == 5 else 100) for index in range(8)])
    constraint = director(bpm_tolerance=4, bpm_repeat_horizon=4).bpm_constraint(past)
    assert constraint.permits(140)


def test_the_key_recency_rule_is_enforced() -> None:
    """§11: "same key: avoid within previous 4 tracks"."""
    past = history(entry(0, key="F# minor"), entry(1, key="A minor"))
    constraint = director(key_repeat_horizon=4).key_constraint(past)
    assert not constraint.permits("F# minor")
    assert not constraint.permits("A minor")
    assert constraint.permits("C major")


def test_the_duration_recency_rule_is_enforced() -> None:
    past = history(entry(0, duration=200.0))
    constraint = director(
        duration_tolerance_seconds=10, duration_repeat_horizon=3
    ).duration_constraint(past)
    assert not constraint.permits(205)
    assert constraint.permits(230)


# ---------------------------------------------------------------- §11 topics


def test_the_topic_recency_rule_is_enforced() -> None:
    """§11: "same lyric topic: avoid within previous 10 tracks"."""
    past = history(entry(0, topic="discipline"), entry(1, topic="patience"))
    constraint = director(topic_repeat_horizon=10).topic_constraint(past)
    assert not constraint.permits("discipline")
    assert not constraint.permits("patience")
    assert constraint.permits("leverage")


def test_a_secondary_topic_also_blocks_recency() -> None:
    """A topic used as the second subject still aired, and the listener heard it."""
    past = history(entry(0, topic="discipline", secondary="leverage"))
    constraint = director(topic_repeat_horizon=10).topic_constraint(past)
    assert not constraint.permits("leverage")


def test_a_topic_pair_is_never_repeated() -> None:
    """§11: "same primary + secondary topic combination: never intentionally repeat"."""
    past = history(entry(0, topic="discipline", secondary="patience"))
    constraint = director().topic_pair_constraint(past)
    assert not constraint.permits(("discipline", "patience"))
    assert constraint.permits(("discipline", "leverage"))


def test_topic_pairs_are_order_insensitive() -> None:
    """A listener does not distinguish (A,B) from (B,A)."""
    past = history(entry(0, topic="discipline", secondary="patience"))
    constraint = director().topic_pair_constraint(past)
    assert not constraint.permits(("patience", "discipline"))


def test_the_topic_pair_rule_checks_all_history_not_a_window() -> None:
    """The rule says *never*, so it cannot be horizon-limited."""
    entries = [entry(index, topic=f"t{index}", secondary=f"s{index}") for index in range(200)]
    entries.append(entry(500, topic="discipline", secondary="patience"))
    constraint = director(topic_repeat_horizon=10).topic_pair_constraint(history(*entries))
    assert not constraint.permits(("discipline", "patience"))


# ---------------------------------------------------------------- §11 vocals


def test_the_instrumental_run_limit_is_enforced() -> None:
    """§11: "instrumental maximum: 4 consecutive tracks"."""
    past = history(*[entry(index, instrumental=True) for index in range(4)])
    constraints = director(max_instrumental_consecutive=4).vocal_constraints(past)
    run = next(c for c in constraints if c.name == "instrumental_run")
    assert not run.permits(True)
    assert run.permits(False)


def test_the_instrumental_run_limit_does_not_fire_early() -> None:
    past = history(*[entry(index, instrumental=True) for index in range(3)])
    constraints = director(max_instrumental_consecutive=4).vocal_constraints(past)
    assert not [c for c in constraints if c.name == "instrumental_run"]


def test_a_long_vocal_run_eventually_asks_for_an_instrumental() -> None:
    """Contrast cuts both ways; an unbroken run of vocals is also monotonous."""
    past = history(*[entry(index, instrumental=False) for index in range(10)])
    constraints = director(max_same_vocal_type_consecutive=2).vocal_constraints(past)
    run = next(c for c in constraints if c.name == "vocal_run")
    assert not run.permits(False)
    assert run.permits(True)


def test_the_vocal_style_run_limit_is_enforced() -> None:
    """§11: "same vocal type maximum: 2 consecutive tracks"."""
    past = history(
        entry(0, vocal_style="rap"), entry(1, vocal_style="rap"), entry(2, vocal_style="sung")
    )
    constraint = director(max_same_vocal_type_consecutive=2).vocal_style_constraint(past)
    assert constraint.reason
    assert not constraint.permits("rap")
    assert constraint.permits("sung")


def test_the_vocal_style_constraint_is_a_no_op_after_an_instrumental() -> None:
    """An instrumental breaks any vocal-style run, and a no-op must be recognisable."""
    past = history(entry(0, instrumental=True), entry(1, vocal_style="rap"))
    constraint = director().vocal_style_constraint(past)
    assert constraint.reason == ""
    assert constraint.permits("rap")


def test_the_vocal_style_constraint_is_a_no_op_on_empty_history() -> None:
    constraint = director().vocal_style_constraint(history())
    assert constraint.reason == ""
    assert constraint.permits("rap")


# ---------------------------------------------------------------- §11 identity


def test_a_blueprint_signature_is_never_repeated() -> None:
    """§11: "same blueprint: never repeat"."""
    past = history(entry(0, signature="a" * 64))
    constraint = director().signature_constraint(past)
    assert not constraint.permits("a" * 64)
    assert constraint.permits("b" * 64)


def test_the_signature_rule_has_the_highest_severity() -> None:
    """It must be the last rule the relaxation ladder gives up."""
    past = history(entry(0))
    signature = director().signature_constraint(past)
    bpm = director().bpm_constraint(past)
    key = director().key_constraint(past)
    assert signature.severity > bpm.severity
    assert signature.severity > key.severity


def test_the_persona_recency_rule_is_enforced() -> None:
    past = history(entry(0, persona="tf01"), entry(1, persona="tf02"))
    constraint = director(persona_repeat_horizon=3).persona_constraint(past)
    assert not constraint.permits("tf01")
    assert constraint.permits("tf05")


# ---------------------------------------------------------------- soft penalties


def test_a_recently_used_genre_is_penalised_not_blocked() -> None:
    """The graded preference that keeps 100 tracks interesting where no rule is broken."""
    past = history(entry(0, genre="trap"), *[entry(i, genre="lofi") for i in range(1, 10)])
    engine = director(horizon_medium=20)
    assert engine.genre_penalty(past, "trap") < 1.0
    assert engine.genre_penalty(past, "dnb") == 1.0


def test_the_penalty_weakens_with_distance() -> None:
    recent = history(entry(0, genre="trap"))
    distant = history(*[entry(i, genre="lofi") for i in range(15)], entry(15, genre="trap"))
    engine = director(horizon_medium=20)
    assert engine.genre_penalty(recent, "trap") < engine.genre_penalty(distant, "trap")


def test_topic_penalties_use_the_long_horizon() -> None:
    """§12: lyrical concept repetition is checked over the last 100, not the last 5."""
    past = history(
        *[entry(i, topic="patience") for i in range(50)], entry(50, topic="discipline")
    )
    engine = director(horizon_long=100)
    assert engine.topic_penalty(past, "discipline") < 1.0


def test_persona_and_key_penalties_apply() -> None:
    past = history(entry(0, persona="tf01", key="F# minor"))
    engine = director(horizon_medium=20)
    assert engine.persona_penalty(past, "tf01") < 1.0
    assert engine.key_penalty(past, "F# minor") < 1.0


# ---------------------------------------------------------------- the score


def test_a_short_history_scores_full_marks() -> None:
    """A freshly started station has not repeated itself."""
    assert director().score(history()).score == 100.0
    assert director().score(history(entry(0), entry(1))).score == 100.0


def test_monotonous_history_scores_badly() -> None:
    """Everything identical: the collapse §11 exists to prevent."""
    identical = [
        entry(index, genre="trap", bpm=140, key="F# minor", topic="discipline",
              persona="tf01", energy=60.0)
        for index in range(30)
    ]
    report = director().score(history(*identical))
    assert report.score < 45.0, report.as_dict()


def test_varied_history_scores_well() -> None:
    genres = ("trap", "lofi", "dnb", "techno", "rnb", "garage", "ambient", "hiphop")
    keys = ("F# minor", "A minor", "C major", "G minor", "D major", "E minor")
    topics = ("discipline", "patience", "leverage", "breakout", "volatility", "fear")
    varied = [
        entry(
            index,
            genre=genres[index % len(genres)],
            bpm=80 + (index * 11) % 90,
            key=keys[index % len(keys)],
            topic=topics[index % len(topics)],
            persona=f"tf{index % 8 + 1:02d}",
            instrumental=index % 3 == 0,
            energy=float(10 + (index * 13) % 80),
        )
        for index in range(30)
    ]
    report = director().score(history(*varied))
    assert report.score > 80.0, report.as_dict()


def test_the_score_reports_its_breakdown() -> None:
    """A bare number tells an operator programming is stale, not in which dimension."""
    identical = [entry(index) for index in range(30)]
    report = director().score(history(*identical))
    names = {component.name for component in report.components}
    assert names == {
        "genre_variety", "bpm_spread", "key_variety", "topic_variety",
        "vocal_balance", "persona_variety", "energy_spread",
    }
    assert sum(component.weight for component in report.components) == pytest.approx(1.0)


def test_the_weakest_dimensions_are_identifiable() -> None:
    """What to diversify next."""
    entries = [
        entry(index, genre="trap", bpm=80 + index * 3, key=f"{'CDEFGAB'[index % 7]} minor",
              topic=f"topic{index}", persona=f"tf{index % 8 + 1:02d}")
        for index in range(30)
    ]
    report = director().score(history(*entries))
    weakest = [component.name for component in report.weakest(2)]
    assert "genre_variety" in weakest


def test_vocal_balance_peaks_at_the_target_ratio() -> None:
    def ratio_score(instrumental_every: int) -> float:
        entries = [
            entry(index, instrumental=index % instrumental_every == 0,
                  genre=("trap", "lofi", "dnb")[index % 3],
                  key=("A minor", "C major", "G minor")[index % 3],
                  bpm=90 + index * 3)
            for index in range(30)
        ]
        report = director().score(history(*entries))
        return next(
            c.score for c in report.components if c.name == "vocal_balance"
        )

    # Every third track instrumental is ~0.33, essentially the target.
    assert ratio_score(3) > ratio_score(10)
    assert ratio_score(3) > 0.9


def test_both_vocal_extremes_are_penalised() -> None:
    """A fully instrumental station has stopped being a station with something to say."""
    for instrumental in (True, False):
        entries = [
            entry(index, instrumental=instrumental,
                  genre=("trap", "lofi", "dnb")[index % 3], bpm=90 + index * 3)
            for index in range(30)
        ]
        report = director().score(history(*entries))
        balance = next(c.score for c in report.components if c.name == "vocal_balance")
        assert balance < 0.6, instrumental


def test_the_target_instrumental_ratio_is_documented_not_implicit() -> None:
    assert 0.2 < TARGET_INSTRUMENTAL_RATIO < 0.5


# ---------------------------------------------------------------- divergence


def test_divergence_is_inactive_above_the_floor() -> None:
    genres = ("trap", "lofi", "dnb", "techno", "rnb", "garage")
    varied = [
        entry(index, genre=genres[index % len(genres)], bpm=80 + (index * 11) % 90,
              key=f"{'CDEFGAB'[index % 7]} minor", topic=f"topic{index}",
              persona=f"tf{index % 8 + 1:02d}", instrumental=index % 3 == 0,
              energy=float(10 + (index * 13) % 80))
        for index in range(30)
    ]
    pressure = director(diversity_floor=55.0).divergence_pressure(history(*varied))
    assert not pressure.is_active
    assert pressure.strength == 0.0


def test_divergence_activates_below_the_floor() -> None:
    """§11: "push the next song toward a different creative region"."""
    identical = [entry(index) for index in range(30)]
    pressure = director(diversity_floor=70.0).divergence_pressure(history(*identical))
    assert pressure.is_active
    assert 0.0 < pressure.strength <= 1.0
    assert pressure.dimensions


def test_divergence_strength_scales_with_how_bad_it_is() -> None:
    identical = [entry(index) for index in range(30)]
    mild = director(diversity_floor=45.0).divergence_pressure(history(*identical))
    severe = director(diversity_floor=95.0).divergence_pressure(history(*identical))
    assert severe.strength > mild.strength


def test_divergence_names_the_weakest_dimensions() -> None:
    identical = [entry(index) for index in range(30)]
    pressure = director(diversity_floor=90.0).divergence_pressure(history(*identical))
    assert len(pressure.dimensions) <= 3
    assert all(isinstance(name, str) for name in pressure.dimensions)


def test_the_score_recovers_after_forced_divergence() -> None:
    """The feedback loop must close: diverging has to actually help."""
    stale = [entry(index) for index in range(20)]
    engine = director(diversity_floor=70.0)
    before = engine.score(history(*stale)).score
    assert engine.is_below_floor(history(*stale))

    genres = ("lofi", "dnb", "techno", "rnb", "garage", "ambient", "amapiano", "drill")
    diverged = [
        entry(100 + index, genre=genres[index % len(genres)], bpm=70 + index * 7,
              key=f"{'CDEFGAB'[index % 7]} major", topic=f"fresh{index}",
              persona=f"tf{index % 8 + 1:02d}", instrumental=index % 3 == 0,
              energy=float(15 + index * 4))
        for index in range(20)
    ]
    after = engine.score(history(*diverged, *stale)).score
    assert after > before, (before, after)


# ---------------------------------------------------------------- collapse audit


def test_collapse_warnings_are_silent_on_healthy_history() -> None:
    genres = ("trap", "lofi", "dnb", "techno", "rnb", "garage")
    varied = [
        entry(index, genre=genres[index % len(genres)], topic=f"topic{index % 9}",
              instrumental=index % 3 == 0)
        for index in range(40)
    ]
    assert director().collapse_warnings(history(*varied)) == []


def test_collapse_warnings_name_a_dominant_genre() -> None:
    """§81-18's evidence, in prose, because a person reads it."""
    dominated = [
        entry(index, genre="trap" if index % 4 else "lofi", topic=f"topic{index % 9}")
        for index in range(40)
    ]
    warnings = director().collapse_warnings(history(*dominated))
    assert any("trap" in warning for warning in warnings)


def test_collapse_warnings_name_topic_narrowing() -> None:
    narrow = [
        entry(index, genre=("trap", "lofi", "dnb", "techno")[index % 4],
              topic=("discipline", "patience")[index % 2])
        for index in range(40)
    ]
    warnings = director().collapse_warnings(history(*narrow))
    assert any("lyric topics" in warning for warning in warnings)


def test_collapse_warnings_name_a_drifted_instrumental_ratio() -> None:
    drifted = [
        entry(index, genre=("trap", "lofi", "dnb", "techno")[index % 4],
              topic=f"topic{index % 9}", instrumental=True)
        for index in range(40)
    ]
    warnings = director().collapse_warnings(history(*drifted))
    assert any("instrumental ratio" in warning for warning in warnings)


def test_collapse_warnings_name_repeated_signatures() -> None:
    repeated = [
        entry(index, genre=("trap", "lofi", "dnb", "techno")[index % 4],
              topic=f"topic{index % 9}", signature="x" * 64)
        for index in range(40)
    ]
    warnings = director().collapse_warnings(history(*repeated))
    assert any("blueprint signature" in warning for warning in warnings)


# ---------------------------------------------------------------- helpers


def test_entropy_is_one_for_an_even_distribution() -> None:
    assert shannon_entropy([5, 5, 5, 5]) == pytest.approx(1.0)


def test_entropy_falls_for_a_skewed_distribution() -> None:
    assert shannon_entropy([97, 1, 1, 1]) < 0.3


def test_entropy_of_a_single_bucket_is_zero() -> None:
    """Everything in one place is total concentration, not perfect evenness.

    An earlier version returned 1.0 here, and the consequence was that thirty consecutive
    tracks of the same genre scored as *maximally varied*.
    """
    assert shannon_entropy([7]) == 0.0
    assert shannon_entropy([]) == 0.0


def test_entropy_ignores_empty_buckets() -> None:
    assert shannon_entropy([5, 5, 0, 0]) == pytest.approx(shannon_entropy([5, 5]))


def test_variety_combines_evenness_with_coverage() -> None:
    """Evenness alone is not variety: 15/15 is perfectly even and still narrow."""
    narrow_but_even = variety_score([15, 15], target_distinct=7)
    broad_and_even = variety_score([5] * 7, target_distinct=7)
    assert narrow_but_even < broad_and_even
    assert broad_and_even == pytest.approx(1.0)


def test_variety_is_zero_for_total_concentration() -> None:
    assert variety_score([30], target_distinct=7) == 0.0
    assert variety_score([], target_distinct=7) == 0.0


def test_variety_coverage_saturates_at_the_target() -> None:
    at_target = variety_score([5] * 7, target_distinct=7)
    beyond = variety_score([5] * 14, target_distinct=7)
    assert at_target == pytest.approx(beyond)


def test_variety_rejects_a_non_positive_target() -> None:
    with pytest.raises(ValueError, match="target_distinct"):
        variety_score([1, 2], target_distinct=0)


def test_spread_score_rises_with_variance() -> None:
    assert spread_score([100.0] * 10, full_scale=20.0) == pytest.approx(0.0)
    assert spread_score([80.0, 100.0, 120.0, 140.0], full_scale=20.0) > 0.9


def test_spread_score_saturates() -> None:
    assert spread_score([0.0, 1000.0], full_scale=20.0) == 1.0


def test_spread_score_of_a_single_value_is_one() -> None:
    """Nothing to be monotonous about yet."""
    assert spread_score([50.0], full_scale=20.0) == 1.0


def test_spread_score_rejects_a_non_positive_scale() -> None:
    with pytest.raises(ValueError, match="full_scale"):
        spread_score([1.0, 2.0], full_scale=0.0)
