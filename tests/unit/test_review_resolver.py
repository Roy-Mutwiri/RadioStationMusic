"""The REVIEW resolver (B2).

The corpus the real station test produced contains no duplicates at all — the maximum
fingerprint agreement across 10 440 pairs was 0.643, against a same-recording floor of
0.907. So a resolver that approved everything would look perfect on that data and be
worthless.

These tests therefore work the other way round: they construct the duplicate variants a
real one would arrive as, and assert the resolver rejects those *while* accepting
genuinely different material that merely shares a genre and a tempo. Getting only one of
those right is the failure mode.

Thresholds under test come from docs/status/CHROMAPRINT_CALIBRATION.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tradefix_radio.config.schema import AppSettings
from tradefix_radio.core.clock import UTC
from tradefix_radio.originality.review import (
    CERTAIN_DUPLICATE,
    EvidenceClass,
    ReviewDisposition,
    ReviewResolver,
)
from tradefix_radio.originality.similarity import (
    OriginalityVerdict,
    SimilarityComponents,
    SimilarityOutcome,
    TrackComparison,
)

NOW = datetime(2026, 10, 3, 18, 0, tzinfo=UTC)


@pytest.fixture
def resolver() -> ReviewResolver:
    return ReviewResolver(AppSettings().originality)


def comparison(
    track_id: str = "TF-OTHER",
    *,
    fingerprint: float = 0.51,
    embedding: float = 0.88,
    chroma: float = 0.78,
    mfcc: float = 0.97,
    tempo: float = 0.90,
    blueprint: float = 0.40,
    exact: bool = False,
    aired_at: datetime | None = None,
    graded: bool = True,
) -> TrackComparison:
    """One comparison, with the component values a real pair would carry.

    Defaults are the corpus means: a different recording in the same genre. Tests move
    only the component they are about.
    """
    return TrackComparison(
        existing_track_id=track_id,
        score=max(fingerprint, embedding, chroma, mfcc, tempo),
        components=SimilarityComponents(
            audio_fingerprint=fingerprint,
            embedding=embedding,
            chroma=chroma,
            mfcc=mfcc,
            tempo=tempo,
            blueprint=blueprint,
        ),
        is_exact_audio=exact,
        existing_aired_at=aired_at,
        counts_toward_graded_novelty=graded,
    )


def review_outcome(*comparisons: TrackComparison, deciding: str = "mfcc") -> SimilarityOutcome:
    closest = max(comparisons, key=lambda c: c.score) if comparisons else None
    return SimilarityOutcome(
        track_id="TF-CANDIDATE",
        verdict=OriginalityVerdict.REVIEW,
        novelty_score=0.2,
        max_similarity=closest.score if closest else 0.0,
        threshold=0.84,
        closest=closest,
        top_comparisons=tuple(comparisons),
        compared_against=len(comparisons),
        deciding_component=deciding,
    )


# ----------------------------------------------------- duplicates must be caught


def test_an_exact_recording_is_rejected(resolver: ReviewResolver) -> None:
    resolution = resolver.resolve(
        review_outcome(comparison(exact=True)), now=NOW, production_references=10
    )
    assert resolution.disposition is ReviewDisposition.FINAL_REJECT
    assert resolution.evidence_class is EvidenceClass.DEFINITIVE_DUPLICATE


@pytest.mark.parametrize(
    ("label", "agreement"),
    [
        ("exact copy", 1.0),
        ("WAV rewrite", 1.0),
        ("FLAC round trip", 1.0),
        ("MP3 192k", 0.9984),
        ("gain +3 dB", 1.0),
        ("peak normalised", 1.0),
        ("EQ shelf", 0.9196),
        ("dynamics compressed", 0.9068),
    ],
)
def test_every_measured_same_recording_variant_is_rejected(
    resolver: ReviewResolver, label: str, agreement: float
) -> None:
    """The variants a duplicate actually arrives as, at their measured agreements.

    These are the real numbers from the calibration run, not invented ones — so this test
    fails if the threshold ever drifts above the quietest same-recording variant.
    """
    resolution = resolver.resolve(
        review_outcome(comparison(fingerprint=agreement)),
        now=NOW,
        production_references=10,
    )
    assert resolution.disposition is ReviewDisposition.FINAL_REJECT, label
    assert resolution.evidence_class is EvidenceClass.STRONG_RECORDING_MATCH, label


def test_a_time_shifted_copy_is_rejected_when_structure_corroborates(
    resolver: ReviewResolver,
) -> None:
    """A 5 s crop scored 0.622 — inside the different-recording range.

    The fingerprint cannot decide that alone, which is the whole reason the suspicion band
    exists. Near-identical embedding and chroma are what make it a copy rather than a
    coincidence.
    """
    resolution = resolver.resolve(
        review_outcome(comparison(fingerprint=0.68, embedding=0.985, chroma=0.97)),
        now=NOW,
        production_references=10,
    )
    assert resolution.disposition is ReviewDisposition.FINAL_REJECT
    assert resolution.evidence_class is EvidenceClass.CORROBORATED_RECORDING_MATCH


# ------------------------------------------- genre neighbours must be kept


def test_two_lofi_tracks_at_the_same_tempo_are_not_duplicates(
    resolver: ReviewResolver,
) -> None:
    """The case that produced the 7% approval rate.

    Same genre, one BPM apart, saturated timbre — and a fingerprint that says these are
    unrelated recordings. Measured: no different-recording pair exceeded 0.643.
    """
    resolution = resolver.resolve(
        review_outcome(
            comparison(fingerprint=0.61, embedding=0.98, chroma=0.96, mfcc=0.99, tempo=1.0),
            deciding="tempo",
        ),
        now=NOW,
        production_references=10,
    )
    assert resolution.disposition is ReviewDisposition.FINAL_APPROVE
    assert resolution.evidence_class is EvidenceClass.STYLE_ONLY
    assert "style signal" in resolution.reason


def test_saturated_mfcc_alone_never_rejects(resolver: ReviewResolver) -> None:
    """MFCC reads 0.971 +/- 0.026 across every pair in the corpus.

    A component that answers the same thing for everything must not be able to discard a
    recording on its own.
    """
    resolution = resolver.resolve(
        review_outcome(comparison(mfcc=0.999, fingerprint=0.52), deciding="mfcc"),
        now=NOW,
        production_references=10,
    )
    assert resolution.disposition is ReviewDisposition.FINAL_APPROVE


def test_identical_tempo_alone_never_rejects(resolver: ReviewResolver) -> None:
    """Two different songs at 148 BPM are two different songs."""
    resolution = resolver.resolve(
        review_outcome(comparison(tempo=1.0, fingerprint=0.50), deciding="tempo"),
        now=NOW,
        production_references=10,
    )
    assert resolution.disposition is ReviewDisposition.FINAL_APPROVE


def test_a_fingerprint_just_below_the_threshold_does_not_reject_unaided(
    resolver: ReviewResolver,
) -> None:
    """0.64 is the highest agreement any different recording reached."""
    resolution = resolver.resolve(
        review_outcome(comparison(fingerprint=0.6404, embedding=0.90, chroma=0.80)),
        now=NOW,
        production_references=10,
    )
    assert resolution.disposition is ReviewDisposition.FINAL_APPROVE


# ------------------------------------------------------------- rotation


def test_very_similar_and_very_recent_is_held_back_for_rotation(
    resolver: ReviewResolver,
) -> None:
    """Not a duplicate — too soon. The reason must say so rather than claim duplication."""
    resolution = resolver.resolve(
        review_outcome(
            comparison(
                embedding=0.96, chroma=0.95, mfcc=0.99, tempo=1.0, fingerprint=0.55,
                aired_at=NOW - timedelta(minutes=30),
            )
        ),
        now=NOW,
        production_references=10,
    )
    assert resolution.disposition is ReviewDisposition.FINAL_REJECT
    assert resolution.evidence_class is EvidenceClass.ROTATION_PRESSURE
    assert "not a duplicate" in resolution.reason


def test_the_same_similarity_long_ago_is_allowed(resolver: ReviewResolver) -> None:
    """Recency applies to creative repetition and to nothing else."""
    resolution = resolver.resolve(
        review_outcome(
            comparison(
                embedding=0.96, chroma=0.95, mfcc=0.99, tempo=1.0, fingerprint=0.55,
                aired_at=NOW - timedelta(days=4),
            )
        ),
        now=NOW,
        production_references=10,
    )
    assert resolution.disposition is ReviewDisposition.FINAL_APPROVE


def test_an_exact_duplicate_is_rejected_however_old_it_is(resolver: ReviewResolver) -> None:
    """A duplicated recording stays duplicated forever; recency must not rescue it."""
    resolution = resolver.resolve(
        review_outcome(comparison(exact=True, aired_at=NOW - timedelta(days=900))),
        now=NOW,
        production_references=10,
    )
    assert resolution.disposition is ReviewDisposition.FINAL_REJECT


def test_rotation_ignores_history_nobody_heard(resolver: ReviewResolver) -> None:
    """A bench track cannot create rotation pressure — it never aired."""
    resolution = resolver.resolve(
        review_outcome(
            comparison(
                embedding=0.96, chroma=0.95, mfcc=0.99, tempo=1.0,
                aired_at=NOW - timedelta(minutes=5), graded=False,
            )
        ),
        now=NOW,
        production_references=10,
    )
    assert resolution.disposition is ReviewDisposition.FINAL_APPROVE


# ------------------------------------------------------------- cold start


def test_cold_start_is_reported_not_disguised_as_confidence(
    resolver: ReviewResolver,
) -> None:
    """With nothing aired there is nothing to repeat, and the reason must say exactly that."""
    resolution = resolver.resolve(
        review_outcome(comparison()), now=NOW, production_references=0
    )
    assert resolution.disposition is ReviewDisposition.FINAL_APPROVE
    assert resolution.evidence_class is EvidenceClass.NO_PRODUCTION_HISTORY
    assert resolution.is_cold_start
    assert resolution.production_references == 0


def test_cold_start_still_rejects_a_real_duplicate(resolver: ReviewResolver) -> None:
    """Having no history is not a reason to ship the same recording twice."""
    resolution = resolver.resolve(
        review_outcome(comparison(fingerprint=0.99)), now=NOW, production_references=0
    )
    assert resolution.disposition is ReviewDisposition.FINAL_REJECT


# --------------------------------------------------------------- bookkeeping


def test_the_initial_verdict_is_preserved(resolver: ReviewResolver) -> None:
    """An approval must not hide that it began as REVIEW."""
    resolution = resolver.resolve(
        review_outcome(comparison()), now=NOW, production_references=5
    )
    assert resolution.initial_verdict is OriginalityVerdict.REVIEW
    payload = resolution.as_payload()
    assert payload["initial_verdict"] == "review"
    assert payload["disposition"] == "final_approve"
    assert payload["resolver_version"]
    assert payload["components"]


def test_every_review_reaches_a_terminal_disposition(resolver: ReviewResolver) -> None:
    """The property the whole of B2 exists to guarantee.

    No REVIEW may simply disappear. Driven across a spread of component values rather
    than one case, because the defect being fixed was a branch nobody noticed.
    """
    terminal = {ReviewDisposition.FINAL_APPROVE, ReviewDisposition.FINAL_REJECT}
    for fingerprint in (0.0, 0.4, 0.6404, 0.65, 0.80, 0.9068, 1.0):
        for references in (0, 1, 50):
            for aired in (None, NOW - timedelta(minutes=10), NOW - timedelta(days=30)):
                resolution = resolver.resolve(
                    review_outcome(comparison(fingerprint=fingerprint, aired_at=aired)),
                    now=NOW,
                    production_references=references,
                )
                assert resolution.disposition in terminal, (
                    f"fingerprint={fingerprint} refs={references} aired={aired}"
                )


def test_a_candidate_with_nothing_to_compare_against_is_approved(
    resolver: ReviewResolver,
) -> None:
    resolution = resolver.resolve(
        review_outcome(), now=NOW, production_references=0
    )
    assert resolution.disposition is ReviewDisposition.FINAL_APPROVE


def test_the_certain_threshold_sits_between_the_measured_populations() -> None:
    """Guards the calibration itself.

    Lowest time-aligned same-recording variant 0.9068; highest different recording 0.6404.
    If someone moves the threshold outside that gap, the separation evidence no longer
    supports it.
    """
    assert 0.6404 < CERTAIN_DUPLICATE <= 0.9068
