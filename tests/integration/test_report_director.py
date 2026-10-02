"""The §3.12 director statistical report (milestone 3.12).

§81-18: programming must not collapse — no single genre dominating, no regime served by a
handful of options, no repeated blueprint. Those are *statistical* properties, so the only
honest way to check them is to run the director thousands of times and measure.

This module runs a smaller version of that report in CI (a few hundred decisions per
regime is enough to catch a collapse) and asserts the report's own assessment passes. The
full ten-thousand-decision run is a command the phase report records the output of; keeping
a scaled version in the suite means a regression is caught by ``pytest`` rather than by
someone remembering to run the command.

The second half tests the *report* rather than the director: a report that cannot detect a
collapse is worse than no report, because it reads as evidence.
"""

from __future__ import annotations

import pytest

from tradefix_radio.cli.report_director import (
    CONFIG_DIR,
    ENTROPY_FLOOR,
    REGIME_CONDITIONS,
    DirectorReport,
    RegimeStats,
    _assess,
    render,
    run_report,
)
from tradefix_radio.config.schema import AppSettings
from tradefix_radio.contracts.enums import MarketRegime
from tradefix_radio.director.history import shannon_entropy


@pytest.fixture(scope="module")
def settings() -> AppSettings:
    return AppSettings()


@pytest.fixture(scope="module")
def report(settings: AppSettings) -> DirectorReport:
    """One scaled report, shared across the assertions below.

    Module-scoped because it is the expensive fixture in the suite — roughly 400 director
    decisions — and every test here reads it without mutating it.
    """
    return run_report(settings, decisions=400, seed=20261002, config_dir=CONFIG_DIR)


# ---------------------------------------------------------------- §81-18


def test_the_report_passes_its_own_assessment(report: DirectorReport) -> None:
    """The headline. Every failure the report found is printed, not summarised."""
    assert report.passed, "\n  - " + "\n  - ".join(report.failures)


def test_every_regime_was_exercised(report: DirectorReport) -> None:
    assert set(report.per_regime) == set(REGIME_CONDITIONS)
    for regime, stats in report.per_regime.items():
        assert stats.decisions > 0, f"{regime.value} produced no decisions"


def test_no_blueprint_signature_repeated(report: DirectorReport) -> None:
    """§11: "same blueprint: never repeat"."""
    assert report.signature_collisions == 0


def test_no_title_repeated(report: DirectorReport) -> None:
    """§99's title history, over the whole run rather than a window."""
    assert report.title_collisions == 0


def test_genre_entropy_clears_the_floor(report: DirectorReport) -> None:
    assert report.overall.genre_entropy >= ENTROPY_FLOOR


def test_no_genre_dominates(report: DirectorReport, settings: AppSettings) -> None:
    share = report.overall.top_genre_share
    assert share <= settings.diversity.max_genre_share_medium, (
        f"top genre at {share:.1%}"
    )


def test_every_regime_uses_several_genres(report: DirectorReport) -> None:
    """Aggregate variety can hide a regime that only ever plays two things."""
    thin = {
        regime.value: sorted(stats.genres)
        for regime, stats in report.per_regime.items()
        if stats.decisions >= 20 and len(stats.genres) < 4
    }
    assert not thin, f"regimes with fewer than four genres: {thin}"


def test_the_instrumental_ratio_is_balanced(report: DirectorReport) -> None:
    assert 0.12 <= report.overall.instrumental_ratio <= 0.62


def test_the_market_moves_the_tempo(report: DirectorReport) -> None:
    """§1, read straight off the report: quiet regimes must be slower than violent ones."""
    quiet = report.per_regime[MarketRegime.QUIET]
    violent = report.per_regime[MarketRegime.EXTREME_VOLATILITY]
    assert quiet.mean_bpm > 0 and violent.mean_bpm > 0
    assert violent.mean_bpm > quiet.mean_bpm + 20, (
        f"quiet {quiet.mean_bpm:.0f} BPM vs extreme {violent.mean_bpm:.0f} BPM"
    )


def test_station_energy_follows_the_market_regime(report: DirectorReport) -> None:
    quiet = report.per_regime[MarketRegime.QUIET].mean_station_energy
    violent = report.per_regime[MarketRegime.EXTREME_VOLATILITY].mean_station_energy
    assert violent > quiet + 40, f"{quiet:.0f} vs {violent:.0f}"


def test_relaxations_are_counted(report: DirectorReport) -> None:
    """The honest answer to "is §11 enforced?" is a rate, and the report must carry it.

    Not asserted to be zero: a narrow genre's handful of permitted BPMs genuinely cannot
    always satisfy "no BPM within ±4 of the last four tracks". Asserted to be *reported*,
    so the rate is visible instead of the rule looking absolute.
    """
    assert isinstance(report.overall.relaxations, type(report.overall.genres))
    total = sum(report.overall.relaxations.values())
    assert total < report.overall.decisions * 3, (
        f"{total} relaxations over {report.overall.decisions} decisions — the §11 rules "
        "are being overridden more often than applied"
    )


def test_diversity_was_sampled_over_the_run(report: DirectorReport) -> None:
    """A single end-of-run number would hide a station that decayed and recovered."""
    assert report.diversity_samples
    scores = [score for _, score in report.diversity_samples]
    assert min(scores) > 40.0, f"diversity dipped to {min(scores):.1f}"


# ---------------------------------------------------------------- the report itself


def test_the_rendered_report_names_its_evidence(
    report: DirectorReport, settings: AppSettings
) -> None:
    text = render(report, settings, library_genre_count=28)
    assert "DIRECTOR STATISTICAL REPORT" in text
    assert "genre entropy" in text
    assert str(report.total_decisions) in text
    # Every regime appears, so the report cannot pass by reporting only the good ones.
    for regime in REGIME_CONDITIONS:
        assert regime.value in text


def test_the_report_renders_when_it_failed(settings: AppSettings) -> None:
    """A failing report must still render, or the failure is invisible."""
    failed = DirectorReport(total_decisions=1, failures=["invented failure"])
    failed.per_regime[MarketRegime.QUIET] = RegimeStats(regime=MarketRegime.QUIET)
    text = render(failed, settings)
    assert "invented failure" in text


def test_the_assessment_detects_a_collapsed_genre_distribution(
    settings: AppSettings,
) -> None:
    """A report that cannot detect a collapse is worse than none — it reads as evidence."""
    collapsed = DirectorReport(total_decisions=200)
    collapsed.overall.decisions = 200
    collapsed.overall.genres.update({"dnb": 190, "techno": 10})
    collapsed.overall.instrumental = 60
    _assess(collapsed, settings)
    assert not collapsed.passed
    assert any("above the" in failure for failure in collapsed.failures)


def test_the_assessment_detects_a_repeated_blueprint(settings: AppSettings) -> None:
    report = DirectorReport(total_decisions=100, signature_collisions=1)
    report.overall.decisions = 100
    report.overall.genres.update(dict.fromkeys("abcdefghij", 10))
    report.overall.instrumental = 30
    _assess(report, settings)
    assert not report.passed
    assert any("blueprint" in failure for failure in report.failures)


def test_the_assessment_detects_a_station_stuck_on_instrumentals(
    settings: AppSettings,
) -> None:
    report = DirectorReport(total_decisions=100)
    report.overall.decisions = 100
    report.overall.genres.update(dict.fromkeys("abcdefghij", 10))
    report.overall.instrumental = 95
    _assess(report, settings)
    assert not report.passed
    assert any("instrumental ratio" in failure for failure in report.failures)


def test_the_assessment_detects_a_regime_with_too_few_genres(
    settings: AppSettings,
) -> None:
    report = DirectorReport(total_decisions=100)
    report.overall.decisions = 100
    report.overall.genres.update(dict.fromkeys("abcdefghij", 10))
    report.overall.instrumental = 30
    thin = RegimeStats(regime=MarketRegime.QUIET, decisions=40)
    thin.genres.update({"ambient": 20, "lofi": 20})
    report.per_regime[MarketRegime.QUIET] = thin
    _assess(report, settings)
    assert not report.passed
    assert any("only 2 genres" in failure for failure in report.failures)


def test_a_single_genre_scores_zero_entropy(settings: AppSettings) -> None:
    """The defect behind the entropy floor being meaningful at all.

    ``shannon_entropy`` once returned 1.0 for a single bucket, so thirty consecutive
    identical tracks scored as *maximally varied* and the floor could never fire.
    """
    assert shannon_entropy([30]) == 0.0
    report = DirectorReport(total_decisions=30)
    report.overall.decisions = 30
    report.overall.genres.update({"dnb": 30})
    report.overall.instrumental = 10
    _assess(report, settings)
    assert not report.passed
    assert any("entropy" in failure for failure in report.failures)


def test_the_report_is_reproducible_for_a_seed(settings: AppSettings) -> None:
    """§64's endurance replays depend on this, and so does diagnosing a failed run."""
    first = run_report(settings, decisions=60, seed=7, config_dir=CONFIG_DIR)
    second = run_report(settings, decisions=60, seed=7, config_dir=CONFIG_DIR)
    assert first.overall.genres == second.overall.genres
    assert first.overall.bpms == second.overall.bpms
    assert first.failures == second.failures
