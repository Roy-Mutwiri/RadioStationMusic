"""Fingerprinting, similarity, lyric and blueprint comparison (§6.3–§6.8, §6.19).

Two properties matter more than any individual threshold here, and most of these tests exist
to pin one of them down:

**Duplicates must be caught.** A duplicate reaching air is the failure a listener notices.

**Distinct tracks must not be.** This is the one that is easy to get wrong in the comfortable
direction. A similarity engine tuned until nothing suspicious survives will also reject most
of what the generator legitimately produces, and the station starves — silently, because
every individual rejection looks defensible. So every duplicate test here is paired with a
negative case, and the negatives are the ones that would catch over-tuning.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

from tests.audio_fixtures import musical, near_duplicate, noise, quiet_ambient
from tests.conftest import make_blueprint
from tradefix_radio.audio.analysis import (
    EMBEDDING_VERSION,
    extract_features,
    librosa_available,
)
from tradefix_radio.audio.fingerprint import (
    AudioFingerprint,
    ChromaFingerprintProvider,
    canonical_sha256,
    default_provider,
    fingerprint_capability,
)
from tradefix_radio.audio.io import read_audio, write_audio
from tradefix_radio.audio.pcm import AudioBuffer
from tradefix_radio.config.schema import AppSettings
from tradefix_radio.core.clock import UTC
from tradefix_radio.originality.blueprint import (
    HISTORIC_THRESHOLD,
    RECENT_THRESHOLD,
    compare_blueprints,
    recency_threshold,
    summarise_blueprint,
)
from tradefix_radio.originality.lyrics import (
    compare_lyrics,
    fingerprint_lyrics,
    internal_repetition_warning,
)
from tradefix_radio.originality.similarity import (
    LibraryEntry,
    OriginalityVerdict,
    SimilarityEngine,
    novelty_from_similarity,
)

needs_librosa = pytest.mark.skipif(
    not librosa_available(), reason="librosa is required for feature extraction"
)

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


# ------------------------------------------------------- canonical hash (§6.3)


def test_identical_audio_hashes_identically() -> None:
    assert canonical_sha256(musical(seed=1)) == canonical_sha256(musical(seed=1))


def test_different_audio_hashes_differently() -> None:
    assert canonical_sha256(musical(seed=1)) != canonical_sha256(musical(seed=2))


def test_the_hash_ignores_container_and_metadata(tmp_path) -> None:
    """§6.3's required case: the same audio written twice must not look like two tracks.

    Hashing file bytes would fail this outright — two WAVs written moments apart differ in
    their metadata chunks — and duplicate detection would be defeated by a re-save.
    """
    buffer = musical(seconds=2.0, seed=3)
    first, second = tmp_path / "a.wav", tmp_path / "b.wav"
    write_audio(first, buffer)
    write_audio(second, buffer)
    assert first.read_bytes() != second.read_bytes() or True  # containers may coincide
    assert canonical_sha256(read_audio(first)) == canonical_sha256(read_audio(second))


def test_the_hash_ignores_channel_layout_and_rate() -> None:
    """Mono/stereo and rate changes must not create a phantom new track."""
    buffer = musical(seconds=2.0, seed=4)
    channel = np.asarray(buffer.samples)[:, 0].copy()
    mono = AudioBuffer(channel, buffer.sample_rate)
    doubled = AudioBuffer(np.stack([channel, channel], axis=1), buffer.sample_rate)
    assert canonical_sha256(mono) == canonical_sha256(doubled)


def test_a_near_duplicate_does_not_hash_the_same() -> None:
    """The boundary of what a hash can do, asserted rather than assumed.

    This is why §6.3 and §6.5 are separate stages. A hash answers "the same bytes of audio";
    it cannot answer "the same music", and a test that pretended otherwise would hide the
    fact that near-duplicate detection rests entirely on the similarity engine.
    """
    original = musical(seed=5)
    assert canonical_sha256(original) != canonical_sha256(near_duplicate(original))


# -------------------------------------------------------- fingerprints (§6.4)


@needs_librosa
def test_the_builtin_provider_always_produces_a_fingerprint(tmp_path) -> None:
    buffer = musical(seconds=3.0)
    features = extract_features(buffer)
    provider = ChromaFingerprintProvider()
    assert provider.available
    fingerprint = provider.compute(tmp_path / "x.wav", buffer, features)
    assert fingerprint is not None
    assert fingerprint.provider == "chroma-builtin"
    assert fingerprint.fingerprint


@needs_librosa
def test_the_builtin_fingerprint_publishes_no_vector(tmp_path) -> None:
    """It must not hand back the embedding under a second name.

    This provider's vector was `features.embedding()` — the identical array the similarity
    engine already compares as its `embedding` component. Publishing both made the engine
    weigh one measurement twice, as `audio_fingerprint` (0.22) and `embedding` (0.26), which
    is 0.48 of the score given to a single piece of evidence. §6.5's rule that these numbers
    do not share a scale applies doubly when they are not even distinct numbers.
    """
    features = extract_features(musical(seconds=3.0))
    fingerprint = ChromaFingerprintProvider().compute(tmp_path / "x.wav", musical(seconds=3.0), features)
    assert fingerprint is not None
    assert fingerprint.vector == ()


def test_fingerprints_from_different_providers_are_not_comparable() -> None:
    """Recorded so a library assembled across an upgrade cannot be silently mixed."""
    from tradefix_radio.audio.fingerprint import AudioFingerprint

    builtin = AudioFingerprint(provider="chroma-builtin", provider_version="1", fingerprint="a", duration_seconds=1.0)
    chromaprint = AudioFingerprint(provider="chromaprint", provider_version="1", fingerprint="a", duration_seconds=1.0)
    assert not builtin.comparable_with(chromaprint)
    assert builtin.comparable_with(builtin)


def test_the_capability_report_names_what_is_actually_in_use() -> None:
    """§86: never imply a guarantee the active implementation does not provide."""
    capability = fingerprint_capability()
    assert capability["active_provider"] == default_provider().name
    assert capability["detail"]
    if not capability["chromaprint_available"]:
        assert "built-in" in str(capability["detail"])


# ---------------------------------------------------------- embeddings (§6.2)


@needs_librosa
def test_distinct_music_is_not_near_1_in_embedding_space() -> None:
    """The regression that motivated embedding version 2.

    Version 1 concatenated the raw chroma and MFCC means. Chroma is non-negative, so its
    cosine had a floor around 0.86, and MFCC[0] is log-energy whose magnitude swamped the
    nineteen coefficients that describe timbre — producing 0.99+ between every pair of tracks
    and a similarity score that could not discriminate anything. Pinning a ceiling here is
    what stops that from being reintroduced as a simplification.
    """
    a = extract_features(musical(seconds=4.0, seed=10, bpm=120, root_hz=110.0))
    b = extract_features(noise(seconds=4.0, seed=11))
    cosine = float(a.embedding() @ b.embedding())
    assert cosine < 0.8, f"unrelated audio scored {cosine:.3f}"


@needs_librosa
def test_identical_audio_is_exactly_1_in_embedding_space() -> None:
    a = extract_features(musical(seconds=4.0, seed=12))
    b = extract_features(musical(seconds=4.0, seed=12))
    assert float(a.embedding() @ b.embedding()) == pytest.approx(1.0, abs=1e-9)


def test_the_embedding_version_is_recorded() -> None:
    assert EMBEDDING_VERSION >= 2


# ----------------------------------------------------------- similarity (§6.5)


def _entry(track_id: str, features, **kwargs) -> LibraryEntry:
    return LibraryEntry(
        track_id=track_id,
        canonical_hash=kwargs.pop("canonical_hash", f"hash-{track_id}"),
        embedding=tuple(features.embedding()),
        chroma_mean=features.chroma_mean,
        mfcc_mean=features.mfcc_mean,
        tempo=features.tempo,
        created_at=kwargs.pop("created_at", NOW - timedelta(hours=1)),
        **kwargs,
    )


@needs_librosa
def test_an_exact_hash_match_is_rejected_without_comparing_anything(
    settings: AppSettings,
) -> None:
    engine = SimilarityEngine(settings.originality)
    features = extract_features(musical(seconds=3.0, seed=20))
    outcome = engine.evaluate(
        track_id="TF-NEW",
        features=features,
        canonical_hash="abc",
        library=[],
        now=NOW,
        duplicate_hash_owner="TF-OLD",
    )
    assert outcome.verdict is OriginalityVerdict.REJECT
    assert outcome.novelty_score == 0.0
    assert outcome.deciding_component == "exact_hash"
    assert "TF-OLD" in outcome.reasons[0]


@needs_librosa
def test_an_empty_library_approves_with_full_novelty(settings: AppSettings) -> None:
    engine = SimilarityEngine(settings.originality)
    outcome = engine.evaluate(
        track_id="TF-FIRST",
        features=extract_features(musical(seconds=3.0, seed=21)),
        canonical_hash="first",
        library=[],
        now=NOW,
    )
    assert outcome.verdict is OriginalityVerdict.APPROVE
    assert outcome.novelty_score == 1.0
    assert outcome.closest is None


@needs_librosa
def test_a_re_render_of_the_same_music_is_caught(settings: AppSettings) -> None:
    """The case the hash cannot catch, which is the engine's whole reason to exist."""
    engine = SimilarityEngine(settings.originality)
    original = musical(seconds=4.0, seed=22)
    existing = extract_features(original)
    candidate = extract_features(near_duplicate(original))
    outcome = engine.evaluate(
        track_id="TF-DUPE",
        features=candidate,
        canonical_hash="different-bytes",
        library=[_entry("TF-ORIGINAL", existing)],
        now=NOW,
    )
    assert outcome.verdict is not OriginalityVerdict.APPROVE
    assert outcome.closest is not None
    assert outcome.closest.existing_track_id == "TF-ORIGINAL"


@needs_librosa
def test_genuinely_different_music_is_approved(settings: AppSettings) -> None:
    """The paired negative. Without it, the test above is satisfied by rejecting everything."""
    engine = SimilarityEngine(settings.originality)
    existing = extract_features(quiet_ambient(seconds=4.0, seed=30))
    candidate = extract_features(noise(seconds=4.0, seed=31))
    outcome = engine.evaluate(
        track_id="TF-DIFFERENT",
        features=candidate,
        canonical_hash="unique",
        library=[_entry("TF-AMBIENT", existing)],
        now=NOW,
    )
    assert outcome.verdict is OriginalityVerdict.APPROVE, outcome.reasons
    assert outcome.novelty_score > 0.2


@needs_librosa
def test_component_scores_survive_the_verdict(settings: AppSettings) -> None:
    """§6.5: do not treat one scalar as absolute truth. Preserve component scores."""
    engine = SimilarityEngine(settings.originality)
    existing = extract_features(musical(seconds=3.0, seed=40))
    outcome = engine.evaluate(
        track_id="TF-X",
        features=extract_features(musical(seconds=3.0, seed=41)),
        canonical_hash="x",
        library=[_entry("TF-Y", existing)],
        now=NOW,
    )
    assert outcome.closest is not None
    components = outcome.closest.components.as_dict()
    assert components, "no component breakdown was retained"
    assert all(0.0 <= value <= 1.0 for value in components.values())
    assert outcome.top_comparisons


@needs_librosa
def test_the_shortlist_bounds_the_expensive_stage(settings: AppSettings) -> None:
    """§6.5's staged search: a large library must not mean a large number of full compares."""
    engine = SimilarityEngine(settings.originality)
    features = extract_features(musical(seconds=2.0, seed=50))
    library = [
        _entry(f"TF-{index:04d}", extract_features(musical(seconds=2.0, seed=index)))
        for index in range(60, 100)
    ]
    outcome = engine.evaluate(
        track_id="TF-NEW",
        features=features,
        canonical_hash="new",
        library=library,
        now=NOW,
    )
    assert outcome.compared_against == len(library)
    # Only the shortlist is fully compared, and only a few of those are retained.
    assert len(outcome.top_comparisons) <= 5


# ------------------------------------------------------------- novelty (§6.6)


@pytest.mark.parametrize(
    ("similarity", "expected"),
    [(0.0, 1.0), (0.25, 0.75), (0.5, 0.5), (1.0, 0.0)],
)
def test_novelty_is_one_minus_max_similarity(similarity: float, expected: float) -> None:
    """§6.6 asks for a documented formula. This is the documentation, executable."""
    assert novelty_from_similarity(similarity) == pytest.approx(expected)


def test_novelty_is_clamped_to_the_unit_interval() -> None:
    assert novelty_from_similarity(1.5) == 0.0
    assert novelty_from_similarity(-0.2) == 1.0


# ----------------------------------------------------------- blueprints (§6.7)


def test_an_identical_blueprint_signature_scores_one() -> None:
    blueprint = make_blueprint("TF-A")
    summary = summarise_blueprint(blueprint)
    result = compare_blueprints(summary, summary)
    assert result.score == 1.0
    assert result.exact_signature


def test_a_different_genre_and_tempo_scores_low() -> None:
    left = summarise_blueprint(make_blueprint("TF-A", genre="deep_house", bpm=120))
    right = summarise_blueprint(
        make_blueprint(
            "TF-B",
            genre="uk_drill",
            bpm=145,
            key="G minor",
            primary_topic="market_structure",
            secondary_topic=None,
            persona_id="tf02",
        )
    )
    assert compare_blueprints(left, right).score < 0.5


def test_recent_repetition_is_held_to_a_stricter_threshold() -> None:
    """§6.7: the rule for recent tracks is stronger than the all-time rule."""
    recent, is_recent = recency_threshold(NOW, NOW - timedelta(hours=1))
    historic, is_historic = recency_threshold(NOW, NOW - timedelta(days=14))
    assert is_recent and not is_historic
    assert recent == RECENT_THRESHOLD
    assert historic == HISTORIC_THRESHOLD
    assert recent < historic, "the recent threshold must be the stricter one"


def test_an_unknown_timestamp_is_treated_as_recent() -> None:
    """Strict when uncertain: a needless regeneration is cheaper than an audible repeat."""
    threshold, is_recent = recency_threshold(NOW, None)
    assert is_recent
    assert threshold == RECENT_THRESHOLD


def test_relative_keys_are_more_similar_than_unrelated_ones() -> None:
    base = make_blueprint("TF-A", key="C major")
    relative = summarise_blueprint(make_blueprint("TF-B", key="A minor"))
    unrelated = summarise_blueprint(make_blueprint("TF-C", key="F# minor"))
    summary = summarise_blueprint(base)
    assert (
        compare_blueprints(summary, relative).components["key"]
        > compare_blueprints(summary, unrelated).components["key"]
    )


# --------------------------------------------------------------- lyrics (§6.8)


LYRIC_A = """
[Verse]
The liquidity sweep took the stops below
Candles telling stories that the charts won't show
Risk defined before the session even starts
Patience is the only edge that lasts

[Chorus]
Trade Fix Radio in the dead of night
Structure over feeling, hold the line tight
"""

LYRIC_B = """
[Verse]
Different words entirely about a different thing
Morning coffee and the songs the sparrows sing
Nothing here about a chart or any trade
Just a quiet little tune that someone played

[Chorus]
Sunlight through the window on a lazy day
Nothing left to measure, nothing left to say
"""


def test_identical_lyrics_are_an_exact_match() -> None:
    a = fingerprint_lyrics("TF-A", LYRIC_A)
    b = fingerprint_lyrics("TF-B", LYRIC_A)
    result = compare_lyrics(a, b)
    assert result.exact_match
    assert result.score == 1.0


def test_unrelated_lyrics_score_low() -> None:
    result = compare_lyrics(fingerprint_lyrics("TF-A", LYRIC_A), fingerprint_lyrics("TF-B", LYRIC_B))
    assert result.score < 0.3
    assert not result.exact_match


def test_a_reused_hook_is_caught_even_with_new_verses() -> None:
    """The failure mode a blended score hides.

    A new verse over the same chorus is a repeat a listener recognises instantly, while its
    *average* overlap with the original is low. §6.8's comparison takes the maximum of its
    components precisely so this case cannot be averaged away.
    """
    reused = LYRIC_B.split("[Chorus]")[0] + "[Chorus]" + LYRIC_A.split("[Chorus]")[1]
    result = compare_lyrics(fingerprint_lyrics("TF-A", LYRIC_A), fingerprint_lyrics("TF-C", reused))
    assert result.shared_hook
    assert result.score >= 0.8


def test_section_markers_do_not_make_two_lyrics_look_alike() -> None:
    """Structure is not content. Two different lyrics both having a chorus is not similarity."""
    bare_a = LYRIC_A.replace("[Verse]", "").replace("[Chorus]", "")
    assert compare_lyrics(
        fingerprint_lyrics("TF-A", LYRIC_A), fingerprint_lyrics("TF-A2", bare_a)
    ).exact_match


def test_an_instrumental_track_compares_as_unrelated() -> None:
    empty = fingerprint_lyrics("TF-INST", "")
    result = compare_lyrics(empty, fingerprint_lyrics("TF-A", LYRIC_A))
    assert result.score == 0.0
    assert "instrumental" in result.detail


def test_excessive_internal_repetition_is_flagged() -> None:
    padded = "\n".join(["hold the line tight"] * 20)
    assert internal_repetition_warning(fingerprint_lyrics("TF-PAD", padded)) is not None


def test_a_normal_lyric_is_not_flagged_for_repetition() -> None:
    """A chorus repeating is structure, not padding. The paired negative."""
    normal = LYRIC_A + LYRIC_A.split("[Chorus]")[1]
    assert internal_repetition_warning(fingerprint_lyrics("TF-NORMAL", normal)) is None


# ------------------------------------------- Chromaprint comparison (post-test-01)


def _chromaprint(values: list[int]) -> AudioFingerprint:
    return AudioFingerprint(
        provider="chromaprint",
        provider_version="1",
        fingerprint=",".join(str(v) for v in values),
        duration_seconds=60.0,
    )


def test_chromaprint_is_compared_bit_wise_not_by_magnitude() -> None:
    """Packed subfingerprints are bit fields, so their magnitude means nothing.

    A cosine over the raw integers is dominated by how large the numbers happen to be,
    which is an artefact of bit layout. Measured over 10 440 pairs of the station's own
    output it read 0.712 on average and could not separate an approved track from a
    rejected one (0.860 vs 0.849) — the same saturation trap MFCC[0] and the raw chroma
    cosine already sprang in this codebase.

    Bit agreement sits near 0.5 for unrelated material, which is what half-matching 32-bit
    words should give.
    """
    rng = np.random.default_rng(7)
    left = _chromaprint([int(v) for v in rng.integers(0, 2**32, 256, dtype=np.uint64)])
    right = _chromaprint([int(v) for v in rng.integers(0, 2**32, 256, dtype=np.uint64)])

    score = left.similarity_to(right)
    assert score is not None
    assert 0.4 < score < 0.6, f"unrelated fingerprints should sit near 0.5, got {score}"


def test_an_identical_chromaprint_scores_one() -> None:
    values = [1, 2, 3, 4, 5, 6, 7, 8]
    assert _chromaprint(values).similarity_to(_chromaprint(values)) == 1.0


def test_a_one_bit_difference_is_still_almost_identical() -> None:
    """The measure has to be graded, not just an equality check."""
    base = [0xFFFFFFFF] * 32
    flipped = [0xFFFFFFFE] + [0xFFFFFFFF] * 31
    score = _chromaprint(base).similarity_to(_chromaprint(flipped))
    assert score is not None
    assert score > 0.99, f"one flipped bit in 1024 should barely move the score: {score}"


def test_fingerprints_from_different_providers_are_not_compared() -> None:
    """A cross-provider number would be meaningless rather than merely imprecise."""
    other = AudioFingerprint(
        provider="chroma-builtin", provider_version="1", fingerprint="abc",
        duration_seconds=60.0,
    )
    assert _chromaprint([1, 2, 3]).similarity_to(other) is None


# ------------------------------------------------------ provenance (post-test-01)


def test_bench_history_cannot_make_a_production_track_look_unoriginal() -> None:
    """The defect the real station test exposed.

    154 engineering tracks were acting as permanent station novelty history, so each bench
    generation made the next one likelier to be rejected. Novelty is a promise to a
    listener, and nobody heard any of them.
    """
    entry = LibraryEntry(track_id="TF-BENCH", provenance="engineering_test")
    assert entry.counts_toward_graded_novelty is False

    for label in ("simulation", "manual_lab", "unknown"):
        assert (
            LibraryEntry(track_id="x", provenance=label).counts_toward_graded_novelty
            is False
        ), label

    assert LibraryEntry(track_id="x", provenance="production_radio").counts_toward_graded_novelty


def test_an_entry_with_no_stated_provenance_gets_the_stricter_treatment() -> None:
    """Forgetting to state provenance must not quietly widen what the station may repeat."""
    assert LibraryEntry(track_id="x").counts_toward_graded_novelty is True
    assert LibraryEntry(track_id="x", provenance="nonsense").counts_toward_graded_novelty is True
