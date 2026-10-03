"""Property-based tests for the post-production maths (§6.20).

Example-based tests check the cases someone thought of. These check the *invariants* — the
statements that must hold for every input, including the ones nobody thought of. That matters
most for scoring functions, where the dangerous failure is not a wrong answer on a known case
but a number that escapes 0–1, flips sign, or disagrees with itself depending on argument
order. Any of those would quietly corrupt every threshold comparison downstream.
"""

from __future__ import annotations

import math

from hypothesis import assume, given, settings as hypothesis_settings
from hypothesis import strategies as st

from tradefix_radio.originality.blueprint import (
    BlueprintSummary,
    compare_blueprints,
    recency_threshold,
)
from tradefix_radio.originality.lyrics import compare_lyrics, fingerprint_lyrics
from tradefix_radio.originality.similarity import novelty_from_similarity

# Hypothesis defaults are generous; these functions are pure and fast, so a tighter budget
# keeps the suite quick without losing coverage of the space that matters.
FAST = hypothesis_settings(max_examples=120, deadline=None)

_KEYS = st.sampled_from(
    [f"{tonic} {mode}" for tonic in ("C", "D", "E", "F#", "G", "A", "B") for mode in ("major", "minor")]
)
_GENRES = st.sampled_from(["uk_drill", "deep_house", "uk_trap", "lofi", "dnb", "amapiano"])
_WORDS = st.sampled_from(
    ["market", "candle", "liquidity", "night", "signal", "risk", "patience",
     "structure", "level", "trend", "wait", "hold", "line", "chart", "entry"]
)


def _summary(draw_id: str, **kwargs) -> BlueprintSummary:
    base = {
        "track_id": draw_id,
        "signature": f"sig-{draw_id}",
        "genre": "uk_drill",
        "secondary_genre": None,
        "bpm": 140,
        "musical_key": "F# minor",
        "duration_seconds": 200,
        "energy": 0.8,
        "is_instrumental": False,
        "vocal_style": "rap",
        "primary_topic": "liquidity",
        "secondary_topic": None,
        "persona_id": "tf01",
        "created_at": None,
    }
    base.update(kwargs)
    return BlueprintSummary(**base)  # type: ignore[arg-type]


# ------------------------------------------------------------- novelty (§6.6)


@FAST
@given(st.floats(min_value=-10.0, max_value=10.0, allow_nan=False, allow_infinity=False))
def test_novelty_always_lands_in_the_unit_interval(similarity: float) -> None:
    """Even for inputs that should never occur.

    A novelty score outside 0–1 would be compared against thresholds that assume otherwise,
    and would serialise into a DTO whose validator rejects it — turning a bad number into a
    500 on the Originality page rather than an obviously wrong figure.
    """
    novelty = novelty_from_similarity(similarity)
    assert 0.0 <= novelty <= 1.0
    assert math.isfinite(novelty)


@FAST
@given(
    st.floats(min_value=0.0, max_value=1.0),
    st.floats(min_value=0.0, max_value=1.0),
)
def test_novelty_decreases_as_similarity_increases(a: float, b: float) -> None:
    """Monotonic, which is the only property that makes the score orderable.

    If this failed, "rank tracks by novelty" and "rank tracks by how unlike the library they
    are" would be different orderings, and the Originality page would be sorting by nothing.
    """
    assume(a < b)
    assert novelty_from_similarity(a) >= novelty_from_similarity(b)


# ---------------------------------------------------------- blueprints (§6.7)


@FAST
@given(_GENRES, _GENRES, st.integers(60, 200), st.integers(60, 200), _KEYS, _KEYS)
def test_blueprint_similarity_is_bounded_and_symmetric(
    genre_a: str, genre_b: str, bpm_a: int, bpm_b: int, key_a: str, key_b: str
) -> None:
    """Bounded, finite and order-independent.

    Symmetry is not decoration: the engine compares a candidate against the library in one
    direction only, so an asymmetric score would make "is A a duplicate of B" depend on which
    one happened to be generated first.
    """
    left = _summary("A", genre=genre_a, bpm=bpm_a, musical_key=key_a, signature="sig-A")
    right = _summary("B", genre=genre_b, bpm=bpm_b, musical_key=key_b, signature="sig-B")
    forward = compare_blueprints(left, right)
    backward = compare_blueprints(right, left)
    assert 0.0 <= forward.score <= 1.0
    assert math.isfinite(forward.score)
    assert forward.score == backward.score


@FAST
@given(_GENRES, st.integers(60, 200), _KEYS)
def test_a_blueprint_is_maximally_similar_to_itself(
    genre: str, bpm: int, key: str
) -> None:
    summary = _summary("A", genre=genre, bpm=bpm, musical_key=key)
    assert compare_blueprints(summary, summary).score == 1.0


@FAST
@given(_GENRES, _GENRES, st.integers(60, 200), st.integers(60, 200))
def test_no_pair_scores_above_an_identical_pair(
    genre_a: str, genre_b: str, bpm_a: int, bpm_b: int
) -> None:
    """Nothing is more similar to A than A is.

    The invariant a weighted sum breaks the moment one weight is wrong or the weights stop
    summing to 1 — and it would break *upwards*, pushing ordinary tracks past the rejection
    threshold while nothing in the example tests moved.
    """
    left = _summary("A", genre=genre_a, bpm=bpm_a, signature="sig-A")
    right = _summary("B", genre=genre_b, bpm=bpm_b, signature="sig-B")
    assert compare_blueprints(left, right).score <= compare_blueprints(left, left).score


@FAST
@given(st.integers(0, 60 * 24 * 30))
def test_the_recency_threshold_never_loosens_for_newer_tracks(minutes: int) -> None:
    """§6.7: recent repetition is held to the stricter rule, at every age."""
    from datetime import timedelta

    from tradefix_radio.core.clock import UTC

    now = __import__("datetime").datetime(2026, 10, 3, tzinfo=UTC)
    older, _ = recency_threshold(now, now - timedelta(minutes=minutes))
    newer, _ = recency_threshold(now, now - timedelta(minutes=minutes // 2))
    assert newer <= older


# -------------------------------------------------------------- lyrics (§6.8)


@FAST
@given(st.lists(_WORDS, min_size=6, max_size=60), st.lists(_WORDS, min_size=6, max_size=60))
def test_lyric_similarity_is_bounded_and_symmetric(
    left_words: list[str], right_words: list[str]
) -> None:
    left = fingerprint_lyrics("A", " ".join(left_words))
    right = fingerprint_lyrics("B", " ".join(right_words))
    forward = compare_lyrics(left, right)
    backward = compare_lyrics(right, left)
    assert 0.0 <= forward.score <= 1.0
    assert forward.score == backward.score


@FAST
@given(st.lists(_WORDS, min_size=6, max_size=60))
def test_a_lyric_is_an_exact_match_to_itself(words: list[str]) -> None:
    text = " ".join(words)
    result = compare_lyrics(fingerprint_lyrics("A", text), fingerprint_lyrics("B", text))
    assert result.exact_match
    assert result.score == 1.0


@FAST
@given(st.lists(_WORDS, min_size=6, max_size=40))
def test_whitespace_and_case_do_not_change_a_lyric_fingerprint(words: list[str]) -> None:
    """Normalisation is what makes the content hash a hash of *content*.

    Without it, the same lyric re-wrapped by a different formatter would read as a new one and
    duplicate detection would miss the most common way a repeat actually arrives.
    """
    plain = " ".join(words)
    noisy = "\n  ".join(word.upper() for word in words) + "   \n"
    assert fingerprint_lyrics("A", plain).content_hash == fingerprint_lyrics("B", noisy).content_hash


@FAST
@given(st.text(max_size=200))
def test_fingerprinting_never_raises_on_arbitrary_text(text: str) -> None:
    """Lyrics come from a language model. Robustness here is not hypothetical.

    Control characters, lone surrogate-adjacent codepoints, markup, an empty string — all
    reachable, and a crash in fingerprinting would fail a track for a reason that has nothing
    to do with the track.
    """
    fingerprint = fingerprint_lyrics("A", text)
    assert fingerprint.word_count >= 0
    assert 0.0 <= fingerprint.unique_word_ratio <= 1.0
    assert 0.0 <= fingerprint.internal_repetition <= 1.0
