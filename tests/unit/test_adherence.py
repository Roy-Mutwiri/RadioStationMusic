"""Blueprint adherence measurement (§7.16, §7.30).

The governing rule for this file is §7.16's: *"Do not fake genre-confidence numbers without
a real classifier. Create an adherence report containing only properties we can measure
honestly."* Several of these tests exist specifically to pin down what the module refuses to
claim — those are the ones that would fail if someone later added a plausible-looking score.
"""

from __future__ import annotations

import pytest

from tests.audio_fixtures import musical, quiet_ambient
from tests.conftest import make_blueprint
from tradefix_radio.audio.analysis import extract_features, librosa_available
from tradefix_radio.generation.adherence import (
    AdherenceStatus,
    measure_adherence,
)

pytestmark = pytest.mark.skipif(
    not librosa_available(), reason="librosa is required for feature extraction"
)


def check(report, name: str):
    found = next((c for c in report.checks if c.name == name), None)
    assert found is not None, f"no check named {name!r}"
    return found


# ------------------------------------------------------------------- tempo


def test_a_matching_tempo_is_reported_as_a_match() -> None:
    features = extract_features(musical(seconds=12.0, bpm=120.0))
    blueprint = make_blueprint("TF-A", bpm=120, duration_seconds=30)
    result = check(measure_adherence(blueprint, features), "tempo")
    assert result.status in (AdherenceStatus.MATCH, AdherenceStatus.CLOSE), result.detail


def test_an_octave_error_is_not_counted_as_drift() -> None:
    """Beat trackers report half and double time constantly.

    A 70 BPM estimate for a 140 BPM track is the estimator's well-known behaviour, not the
    model ignoring the request. Counting it as drift would fill the report with false
    positives and bury the real ones.
    """
    features = extract_features(musical(seconds=12.0, bpm=120.0))
    # Ask for half what the fixture renders. The estimator will say ~120; the octave
    # correction has to recognise 60 as the same pulse. (Double-time would be the other
    # direction, but the blueprint contract caps BPM at 220.)
    blueprint = make_blueprint("TF-B", bpm=60, duration_seconds=30)
    result = check(measure_adherence(blueprint, features), "tempo")
    assert result.status is not AdherenceStatus.DRIFT
    assert "octave-corrected" in result.detail


def test_a_genuinely_wrong_tempo_is_drift() -> None:
    """The paired positive: the octave allowance must not excuse everything."""
    features = extract_features(musical(seconds=12.0, bpm=120.0))
    blueprint = make_blueprint("TF-C", bpm=93, duration_seconds=30)
    result = check(measure_adherence(blueprint, features), "tempo")
    assert result.status is AdherenceStatus.DRIFT


# ---------------------------------------------------------------- duration


def test_duration_compares_measured_against_requested() -> None:
    features = extract_features(musical(seconds=30.0))
    blueprint = make_blueprint("TF-D", duration_seconds=30)
    result = check(measure_adherence(blueprint, features), "duration")
    assert result.status is AdherenceStatus.MATCH
    assert result.measured == pytest.approx(30.0, abs=0.5)


def test_a_short_render_is_reported_as_drift_not_hidden() -> None:
    """§7.12: *"do not fake exact compliance."*"""
    features = extract_features(musical(seconds=12.0))
    blueprint = make_blueprint("TF-E", duration_seconds=60)
    result = check(measure_adherence(blueprint, features), "duration")
    assert result.status is AdherenceStatus.DRIFT
    assert "-80" in result.detail or "80" in result.detail


# ------------------------------------------------------------------ energy


def test_a_quiet_render_against_a_high_energy_blueprint_drifts() -> None:
    features = extract_features(quiet_ambient(seconds=12.0))
    blueprint = make_blueprint("TF-F", composition_energy=0.95, duration_seconds=30)
    result = check(measure_adherence(blueprint, features), "energy")
    assert result.status is AdherenceStatus.DRIFT


def test_a_quiet_render_against_a_low_energy_blueprint_is_fine() -> None:
    """The paired negative. Without it, the band could be narrowed to nothing."""
    features = extract_features(quiet_ambient(seconds=12.0))
    blueprint = make_blueprint("TF-G", composition_energy=0.1, duration_seconds=30)
    result = check(measure_adherence(blueprint, features), "energy")
    assert result.status is not AdherenceStatus.DRIFT


# ------------------------------------------------- what is NOT claimed


def test_there_is_no_genre_score() -> None:
    """§7.16, verbatim: *"Do not fake genre-confidence numbers without a real classifier."*

    There is no genre classifier in this project. A 0.82 on the detail page would be
    believed, and it would be invented. This test fails the moment someone adds one.
    """
    features = extract_features(musical(seconds=8.0))
    report = measure_adherence(make_blueprint("TF-H", duration_seconds=30), features)
    names = {c.name for c in report.checks}
    assert "genre" not in names
    assert "mood" not in names
    assert "musicality" not in names


def test_there_is_no_single_adherence_score() -> None:
    """Averaging a tempo match with a duration drift produces a number that hides both."""
    features = extract_features(musical(seconds=8.0))
    report = measure_adherence(make_blueprint("TF-I", duration_seconds=30), features)
    assert not hasattr(report, "score")
    assert not hasattr(report, "overall")


def test_vocal_presence_is_declared_unmeasurable_for_vocal_tracks() -> None:
    """The absence of a vocal-shaped spectrum does not establish the absence of vocals.

    Reporting a vocal track as "matched" because the centroid looked right would be exactly
    the fabricated measurement §7.16 forbids. §7.15's human listen is the real check.
    """
    features = extract_features(musical(seconds=8.0))
    blueprint = make_blueprint("TF-J", instrumental=False, duration_seconds=30)
    result = check(measure_adherence(blueprint, features), "vocal_presence")
    assert result.status is AdherenceStatus.UNMEASURED
    assert "no vocal detector" in result.detail


def test_an_instrumental_proxy_never_claims_certainty() -> None:
    """Even when it agrees, the status is CLOSE — agreement is not proof."""
    features = extract_features(quiet_ambient(seconds=8.0))
    blueprint = make_blueprint("TF-K", instrumental=True, duration_seconds=30)
    result = check(measure_adherence(blueprint, features), "vocal_presence")
    assert result.status is not AdherenceStatus.MATCH
    assert "proxy" in result.detail or "consistent with" in result.detail


def test_a_low_confidence_key_estimate_is_unmeasured_not_a_mismatch() -> None:
    """A low-confidence disagreement says more about the estimator than the track."""
    features = extract_features(musical(seconds=8.0))
    # The fixture is A minor; ask for something else and rely on confidence gating.
    blueprint = make_blueprint("TF-L", key="D# major", duration_seconds=30)
    result = check(measure_adherence(blueprint, features), "key")
    if result.status is AdherenceStatus.DRIFT:
        # Only permitted when the estimator was actually confident.
        assert features.key_confidence is not None
        assert features.key_confidence >= 0.55
    else:
        assert result.status is AdherenceStatus.UNMEASURED


def test_unmeasured_properties_are_excluded_from_the_measured_count() -> None:
    """An unmeasured property must not read as a pass."""
    features = extract_features(musical(seconds=8.0))
    blueprint = make_blueprint("TF-M", instrumental=False, duration_seconds=30)
    report = measure_adherence(blueprint, features)
    assert len(report.measured_checks) < len(report.checks)
    assert all(
        c.status is not AdherenceStatus.UNMEASURED for c in report.measured_checks
    )


def test_the_report_serialises_for_the_track_detail_page() -> None:
    features = extract_features(musical(seconds=8.0))
    payload = measure_adherence(
        make_blueprint("TF-N", duration_seconds=30), features
    ).as_metadata()
    assert payload["track_id"] == "TF-N"
    assert isinstance(payload["checks"], list)
    assert payload["measured_count"] >= 1
