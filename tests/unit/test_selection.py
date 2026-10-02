"""Constrained weighted selection (§9, milestone 3.2).

§9: "Do NOT select songs randomly with simple ``random.choice()``. Use constrained
weighted selection."

Three properties matter and are tested as properties rather than examples:

* **weights are respected** over many draws — otherwise the market has no influence;
* **constraints are hard** — otherwise §11's "maximum 2 consecutive" becomes a
  probability, and over a week of unattended operation it *will* be violated;
* **the relaxation ladder always produces something** — silence is worse than a repeat,
  and a selector that could return nothing would stall the queue.
"""

from __future__ import annotations

import random

import pytest

from tradefix_radio.director.selection import (
    NEGLIGIBLE_WEIGHT,
    Candidate,
    Constraint,
    WeightedSelector,
    band_fit,
    recency_penalty,
)


def selector(seed: int = 7) -> WeightedSelector:
    return WeightedSelector(random.Random(seed))


def candidates(*pairs: tuple[str, float]) -> list[Candidate[str]]:
    return [Candidate(value=value, weight=weight) for value, weight in pairs]


def never(value: str) -> Constraint[str]:
    def predicate(candidate: str) -> bool:
        return candidate != value

    return Constraint(name=f"not_{value}", predicate=predicate, reason=f"{value} blocked")


# ---------------------------------------------------------------- weighting


def test_weights_are_respected_over_many_draws() -> None:
    """A 9:1 weighting must produce roughly 9:1 outcomes."""
    picker = selector()
    counts = {"heavy": 0, "light": 0}
    for _ in range(4_000):
        chosen = picker.select(candidates(("heavy", 9.0), ("light", 1.0))).chosen
        counts[chosen] += 1
    ratio = counts["heavy"] / counts["light"]
    assert 6.5 < ratio < 13.0, counts


def test_a_zero_weight_candidate_is_never_chosen_when_others_exist() -> None:
    picker = selector()
    for _ in range(300):
        chosen = picker.select(candidates(("real", 1.0), ("dead", 0.0))).chosen
        assert chosen == "real"


def test_a_single_candidate_is_returned_without_sampling() -> None:
    assert selector().select(candidates(("only", 1.0))).chosen == "only"


def test_an_empty_candidate_list_raises() -> None:
    """A caller with nothing to choose from has a bug; inventing an option would hide it."""
    with pytest.raises(ValueError, match="empty candidate list"):
        selector().select([])


def test_factors_are_recorded_for_explainability() -> None:
    """§47/§48 need to show *why* a candidate had the weight it did."""
    candidate = Candidate(value="trap")
    candidate.multiply("energy_fit", 0.8).multiply("regime", 1.5)
    assert candidate.weight == pytest.approx(1.2)
    assert candidate.factors == {"energy_fit": 0.8, "regime": 1.5}
    assert "energy_fit=0.8" in candidate.explain()


def test_a_negative_factor_is_rejected() -> None:
    with pytest.raises(ValueError, match="negative"):
        Candidate(value="x").multiply("broken", -1.0)


# ---------------------------------------------------------------- constraints


def test_a_constraint_is_a_hard_veto_not_a_penalty() -> None:
    """The distinction §11 depends on: a rule is not a probability."""
    picker = selector()
    for _ in range(500):
        result = picker.select(
            candidates(("blocked", 1000.0), ("allowed", 0.001)), [never("blocked")]
        )
        assert result.chosen == "allowed"


def test_a_vetoed_candidate_records_the_reason() -> None:
    result = selector().select(
        candidates(("blocked", 1.0), ("allowed", 1.0)), [never("blocked")]
    )
    blocked = next(c for c in result.candidates if c.value == "blocked")
    assert blocked.is_vetoed
    assert "blocked" in blocked.vetoes[0]
    assert "vetoed" in blocked.explain()


def test_multiple_constraints_all_apply() -> None:
    result = selector().select(
        candidates(("a", 1.0), ("b", 1.0), ("c", 1.0)),
        [never("a"), never("b")],
    )
    assert result.chosen == "c"
    assert result.viable_count == 1


def test_viable_count_excludes_vetoed_and_negligible() -> None:
    result = selector().select(
        candidates(("a", 1.0), ("b", 1.0), ("tiny", NEGLIGIBLE_WEIGHT / 2)),
        [never("a")],
    )
    assert result.viable_count == 1


# ---------------------------------------------------------------- relaxation


def test_the_least_severe_constraint_relaxes_first() -> None:
    """Breaking one rule beats broadcasting with none."""
    soft = Constraint(
        name="soft", predicate=lambda value: value != "a", reason="soft", severity=1
    )
    hard = Constraint(
        name="hard", predicate=lambda value: value != "b", reason="hard", severity=10
    )
    result = selector().select(candidates(("a", 1.0), ("b", 1.0)), [soft, hard])
    # Relaxing the soft rule leaves "a" viable, so "b" (the hard-blocked one) stays out.
    assert result.chosen == "a"
    assert result.relaxed == ["soft"]
    assert not result.was_forced


def test_relaxation_stops_as_soon_as_something_survives() -> None:
    first = Constraint(
        name="first", predicate=lambda value: value != "a", reason="r", severity=1
    )
    second = Constraint(
        name="second", predicate=lambda value: value != "b", reason="r", severity=2
    )
    third = Constraint(
        name="third", predicate=lambda value: value != "c", reason="r", severity=3
    )
    result = selector().select(
        candidates(("a", 1.0), ("b", 1.0), ("c", 1.0)), [first, second, third]
    )
    assert len(result.relaxed) == 1
    assert result.chosen == "a"


def test_every_candidate_blocked_still_yields_a_decision() -> None:
    """The property the queue depends on: a decision always comes back.

    Note it is *not* forced. Relaxing the least severe rule makes its victim viable again
    — "a" is blocked only by ``not_a`` — so one rule gives way and the rest still hold.
    That is the ladder working as intended, and strictly better than abandoning every rule
    at once.
    """
    result = selector().select(
        candidates(("a", 1.0), ("b", 5.0)), [never("a"), never("b")]
    )
    assert result.chosen == "a"
    assert result.relaxed == ["not_a"]
    assert not result.was_forced


def test_a_single_blocked_candidate_is_recovered_by_relaxation() -> None:
    result = selector().select(candidates(("a", 1.0)), [never("a")])
    assert result.chosen == "a"
    assert result.relaxed == ["not_a"]
    assert not result.was_forced


def test_forcing_happens_only_when_no_candidate_has_usable_weight() -> None:
    """``was_forced`` is the last resort, reached only when relaxation cannot help.

    Relaxing a constraint can always rescue the candidate that constraint blocked, so the
    only way to exhaust the ladder is for every weight to be negligible — which means the
    weighting itself has gone wrong, not that the rules conflicted.
    """
    result = selector().select(
        candidates(("a", NEGLIGIBLE_WEIGHT / 10), ("b", NEGLIGIBLE_WEIGHT / 10))
    )
    assert result.chosen in {"a", "b"}
    assert result.was_forced
    assert "forced" in result.explain().lower()


# ---------------------------------------------------------------- temperature


def test_low_temperature_concentrates_on_the_strongest() -> None:
    """§95: a conservative stance should pick the obvious answer."""
    picker = selector()
    counts = {"strong": 0, "weak": 0}
    for _ in range(2_000):
        chosen = picker.select(
            candidates(("strong", 3.0), ("weak", 1.0)), temperature=0.3
        ).chosen
        counts[chosen] += 1
    assert counts["strong"] / counts["weak"] > 10.0, counts


def test_high_temperature_flattens_toward_uniform() -> None:
    """§95: an adventurous stance should reach further down the list."""
    picker = selector()
    counts = {"strong": 0, "weak": 0}
    for _ in range(2_000):
        chosen = picker.select(
            candidates(("strong", 3.0), ("weak", 1.0)), temperature=1.45
        ).chosen
        counts[chosen] += 1
    assert 1.2 < counts["strong"] / counts["weak"] < 2.6, counts


def test_temperature_does_not_reorder_preferences() -> None:
    """It changes how decisively the best option wins, never which one is best."""
    for temperature in (0.3, 1.0, 1.45):
        picker = selector(seed=3)
        counts = {"strong": 0, "weak": 0}
        for _ in range(1_500):
            counts[
                picker.select(
                    candidates(("strong", 4.0), ("weak", 1.0)), temperature=temperature
                ).chosen
            ] += 1
        assert counts["strong"] > counts["weak"], (temperature, counts)


def test_a_non_positive_temperature_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        selector().select(candidates(("a", 1.0)), temperature=0.0)


# ---------------------------------------------------------------- determinism


def test_the_same_seed_produces_the_same_sequence() -> None:
    """§3.12's report and §64's endurance runs both depend on this."""
    def draw() -> list[str]:
        picker = selector(seed=99)
        return [
            picker.select(candidates(("a", 1.0), ("b", 1.0), ("c", 1.0))).chosen
            for _ in range(40)
        ]

    assert draw() == draw()


def test_different_seeds_diverge() -> None:
    def draw(seed: int) -> list[str]:
        picker = selector(seed=seed)
        return [
            picker.select(candidates(("a", 1.0), ("b", 1.0), ("c", 1.0))).chosen
            for _ in range(40)
        ]

    assert draw(1) != draw(2)


def test_the_selector_does_not_touch_the_global_rng() -> None:
    random.seed(4321)
    expected = [random.random() for _ in range(3)]
    random.seed(4321)
    picker = selector()
    for _ in range(50):
        picker.select(candidates(("a", 1.0), ("b", 1.0)))
    assert [random.random() for _ in range(3)] == expected


# ---------------------------------------------------------------- helpers


def test_choose_from_applies_weights() -> None:
    picker = selector()
    counts = {"a": 0, "b": 0}
    for _ in range(2_000):
        counts[picker.choose_from(("a", "b"), weight_of=lambda v: 4.0 if v == "a" else 1.0)] += 1
    assert counts["a"] > counts["b"] * 2


def test_choose_from_rejects_an_empty_sequence() -> None:
    with pytest.raises(ValueError, match="empty sequence"):
        selector().choose_from(())


def test_shuffled_returns_a_copy() -> None:
    original = ["a", "b", "c", "d", "e"]
    shuffled = selector().shuffled(original)
    assert sorted(shuffled) == sorted(original)
    assert original == ["a", "b", "c", "d", "e"]


# ---------------------------------------------------------------- band_fit


def test_band_fit_is_full_inside_the_band() -> None:
    """This is the function §1's energy mapping emerges from."""
    for energy in (0.0, 20.0, 38.0):
        assert band_fit(energy, 0.0, 38.0) == 1.0


def test_band_fit_decays_smoothly_outside() -> None:
    """Smooth, so programming does not jump discontinuously on a one-point energy move."""
    inside = band_fit(38.0, 0.0, 38.0)
    near = band_fit(44.0, 0.0, 38.0, falloff=11.0)
    far = band_fit(60.0, 0.0, 38.0, falloff=11.0)
    assert inside > near > far


def test_band_fit_is_monotone_in_distance() -> None:
    previous = 1.1
    for energy in range(38, 90, 4):
        value = band_fit(float(energy), 0.0, 38.0, falloff=11.0)
        assert value <= previous
        previous = value


def test_band_fit_never_reaches_zero() -> None:
    """The relaxation ladder needs something to fall back to."""
    assert band_fit(1000.0, 0.0, 10.0, floor=0.01) == 0.01
    assert band_fit(1000.0, 0.0, 10.0) > 0.0


def test_band_fit_is_symmetric_about_the_band() -> None:
    below = band_fit(20.0, 40.0, 60.0, falloff=11.0)
    above = band_fit(80.0, 40.0, 60.0, falloff=11.0)
    assert below == pytest.approx(above)


def test_band_fit_rejects_invalid_parameters() -> None:
    with pytest.raises(ValueError, match="falloff"):
        band_fit(50.0, 0.0, 10.0, falloff=0.0)
    with pytest.raises(ValueError, match="exceeds high"):
        band_fit(50.0, 100.0, 10.0)


# ---------------------------------------------------------------- recency


def test_recency_penalty_is_neutral_outside_the_horizon() -> None:
    assert recency_penalty(None, horizon=10) == 1.0
    assert recency_penalty(10, horizon=10) == 1.0
    assert recency_penalty(50, horizon=10) == 1.0


def test_recency_penalty_is_strongest_for_the_most_recent() -> None:
    immediate = recency_penalty(0, horizon=10, strength=0.8)
    older = recency_penalty(8, horizon=10, strength=0.8)
    assert immediate < older < 1.0


def test_recency_penalty_respects_its_strength_floor() -> None:
    assert recency_penalty(0, horizon=10, strength=0.8) == pytest.approx(0.2)


def test_recency_penalty_is_graded_not_binary() -> None:
    """§11's absolute rules are Constraints; this handles the softer preference."""
    values = [recency_penalty(ago, horizon=10, strength=0.7) for ago in range(10)]
    assert values == sorted(values)
    assert len(set(values)) > 5


def test_recency_penalty_rejects_invalid_parameters() -> None:
    with pytest.raises(ValueError, match="horizon"):
        recency_penalty(1, horizon=0)
    with pytest.raises(ValueError, match="strength"):
        recency_penalty(1, horizon=5, strength=1.0)
