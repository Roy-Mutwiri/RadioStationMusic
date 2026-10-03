"""The similarity engine and the novelty score (§6.5, §6.6).

Two rules govern everything here.

**Component scores survive.** The brief is explicit: an operator must be able to tell whether
similarity came from audio, lyrics, blueprint, tempo or exact content. A single scalar cannot
express "this is a different song with the same chorus", and collapsing to one would make every
rejection unexplainable. So every comparison carries its parts, they are persisted, and the
Originality page shows them.

**Each metric is normalised deliberately.** §6.5 warns against pretending these numbers share a
scale, and they do not: cosine similarity over an embedding is already 0–1, tempo proximity is a
ratio of differences, lyric overlap is a Jaccard index, blueprint similarity is a weighted sum.
Each is mapped to 0–1 with a stated meaning before any weight is applied.

Staged search (§6.5)
--------------------
Comparing every candidate against every historical track with every metric does not survive
contact with a library of tens of thousands. So:

``Stage 1``  exact hash, and a coarse metadata gate. O(1) per track, rejects duplicates outright.
``Stage 2``  cosine over the stored embedding — one dot product per track, vectorised. Produces
             a shortlist.
``Stage 3``  the expensive comparison, run against the shortlist only.

Stage 2 is the one that matters for scale. It is a single matrix multiply against a stacked
array, which handles tens of thousands of tracks in milliseconds, and the repository interface
is shaped so it can be replaced by a vector index without touching this file.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Final, Protocol

import numpy as np
import structlog

from tradefix_radio.core.clock import UTC
from tradefix_radio.originality.blueprint import (
    BlueprintSummary,
    compare_blueprints,
    recency_threshold,
)
from tradefix_radio.originality.lyrics import LyricFingerprint, compare_lyrics

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradefix_radio.audio.analysis import AudioFeatures
    from tradefix_radio.audio.fingerprint import AudioFingerprint
    from tradefix_radio.config.schema import OriginalitySettings

_log = structlog.get_logger(__name__)

__all__ = [
    "LibraryEntry",
    "OriginalityVerdict",
    "SimilarityComponents",
    "SimilarityEngine",
    "SimilarityOutcome",
    "TrackComparison",
]

#: Shortlist size for stage 3.
#:
#: 24. Large enough that the true nearest neighbour is virtually never outside it — the stage-2
#: embedding ranks the same features stage 3 weighs — and small enough that the expensive pass
#: stays bounded regardless of library size.
SHORTLIST_SIZE: Final = 24

#: How many comparisons are kept on the outcome for display and storage.
#:
#: Five. Enough to show an operator whether a candidate is close to one track or to a cluster,
#: and few enough that the stored JSON stays small on a table with a row per generated track.
_RETAINED_COMPARISONS: Final = 5

#: Audio similarity at or above which a track is treated as the same recording.
#:
#: 0.995. This is the gap the canonical hash cannot close: a re-encode of an existing track
#: perturbs samples below audibility but changes the digest, and this threshold catches it.
#: Set this high because below it lies legitimately similar music, not duplication.
EXACT_AUDIO_THRESHOLD: Final = 0.995

#: Tempo difference at which tempo similarity reaches zero, in BPM.
_TEMPO_SPAN: Final = 30.0


class OriginalityVerdict(str, enum.Enum):
    """What the engine decided."""

    APPROVE = "approve"
    REVIEW = "review"
    REJECT = "reject"


@dataclass(frozen=True)
class SimilarityComponents:
    """Every component score for one pair. All 0–1, all independently meaningful."""

    audio_fingerprint: float = 0.0
    embedding: float = 0.0
    chroma: float = 0.0
    mfcc: float = 0.0
    tempo: float = 0.0
    lyrics: float = 0.0
    blueprint: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "audio_fingerprint": self.audio_fingerprint,
            "embedding": self.embedding,
            "chroma": self.chroma,
            "mfcc": self.mfcc,
            "tempo": self.tempo,
            "lyrics": self.lyrics,
            "blueprint": self.blueprint,
        }

    def strongest(self) -> tuple[str, float]:
        """The component that contributed most, which is what a rejection names."""
        name, value = max(self.as_dict().items(), key=lambda item: item[1])
        return name, value


@dataclass(frozen=True)
class TrackComparison:
    """One candidate against one existing track."""

    existing_track_id: str
    score: float
    components: SimilarityComponents
    #: Set when the audio is the same recording, by hash or by near-unity audio similarity.
    is_exact_audio: bool = False
    is_exact_lyrics: bool = False
    blueprint_threshold: float = 1.0
    blueprint_is_recent: bool = False
    detail: str = ""


@dataclass(frozen=True)
class SimilarityOutcome:
    """The engine's answer for one candidate."""

    track_id: str
    verdict: OriginalityVerdict
    novelty_score: float
    max_similarity: float
    threshold: float
    closest: TrackComparison | None
    compared_against: int
    #: The nearest few comparisons, closest first, including :attr:`closest`.
    #:
    #: Kept because §6.5 forbids treating one scalar as absolute truth, and because the second
    #: and third nearest tracks are what distinguish "this candidate resembles one track" from
    #: "this candidate resembles the whole library" — two situations that want opposite
    #: responses and look identical through a single score.
    top_comparisons: tuple[TrackComparison, ...] = ()
    #: Which component drove the decision — the thing an operator needs to know first.
    deciding_component: str | None = None
    reasons: tuple[str, ...] = ()
    elapsed_seconds: float = 0.0
    extras: dict[str, object] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.verdict is OriginalityVerdict.APPROVE

    def explain(self) -> str:
        """The sentence the Originality page and the rejection log both show."""
        if self.closest is None:
            return "nothing to compare against; the library is empty"
        if self.verdict is OriginalityVerdict.APPROVE:
            return (
                f"novelty {self.novelty_score:.2f}; closest is "
                f"{self.closest.existing_track_id} at {self.max_similarity:.2f}"
            )
        return "; ".join(self.reasons) or "similarity exceeded the configured threshold"


@dataclass(frozen=True)
class LibraryEntry:
    """One historical track, in the form comparison needs.

    Deliberately flat and self-contained: the engine never touches the database, so it can be
    tested against a list and so the repository can be replaced by a vector index later.
    """

    track_id: str
    canonical_hash: str | None = None
    embedding: tuple[float, ...] = ()
    chroma_mean: tuple[float, ...] = ()
    mfcc_mean: tuple[float, ...] = ()
    tempo: float | None = None
    fingerprint: AudioFingerprint | None = None
    lyrics: LyricFingerprint | None = None
    blueprint: BlueprintSummary | None = None
    created_at: datetime | None = None


class LibraryRepository(Protocol):
    """Where historical tracks come from.

    A protocol so V1 can answer from SQL and a later version from a vector store without the
    engine knowing (§6.5).
    """

    async def recent_entries(self, *, limit: int) -> list[LibraryEntry]: ...

    async def find_by_hash(self, canonical_hash: str) -> str | None: ...


def _profile_similarity(left: np.ndarray, right: np.ndarray) -> float:
    """Chroma similarity as a *correlation*, not a raw cosine.

    Chroma vectors are non-negative, so the cosine of any two of them sits in a narrow band
    near the top of the scale — measured across four tracks of different genres, tempos and
    keys, every pair scored 0.86–0.91. On that scale a 0.84 rejection threshold rejects
    essentially everything, which is what it did: a two-hour soak rejected 241 of 252 clean
    tracks as near-duplicates of one another.

    Subtracting each vector's own mean first turns it into a zero-sum profile of *which pitch
    classes stand out*, and the cosine of that is a correlation spanning the full −1…1. The
    result is mapped back onto 0–1 because every other component lives there and §6.5's whole
    point is that the components must be comparable before they are combined.

    This is the same correction `AudioFeatures.embedding` carries, applied where it was
    missed. Fixing one and not the other left the engine with one calibrated component and
    two saturated ones outvoting it.
    """
    if left.size == 0 or right.size == 0 or left.size != right.size:
        return 0.0
    centred_left = left - left.mean()
    centred_right = right - right.mean()
    correlation = _cosine(centred_left, centred_right, clamp=False)
    # Clamped at zero rather than rescaled from −1…1, for the reason `_cosine` already
    # states: an uncorrelated profile is *unrelated*, and rescaling would record it as 0.5 —
    # half-way to a duplicate — on a scale where the rejection threshold is 0.84. An
    # anti-correlated profile is no more similar than an unrelated one, so it maps there too.
    return max(0.0, min(1.0, correlation))


def _timbre_similarity(left: np.ndarray, right: np.ndarray) -> float:
    """MFCC similarity with the log-energy coefficient dropped.

    MFCC[0] is overall loudness, an order of magnitude larger than the coefficients that
    describe spectral shape; including it made the cosine 0.99+ between every pair of tracks
    regardless of timbre. Loudness is already measured directly, and judging two tracks
    similar because they are equally loud is precisely the mistake to avoid.
    """
    if left.size < 2 or right.size < 2 or left.size != right.size:
        return 0.0
    shape_left = left[1:]
    shape_right = right[1:]
    correlation = _cosine(
        shape_left - shape_left.mean(), shape_right - shape_right.mean(), clamp=False
    )
    return max(0.0, min(1.0, correlation))


def _cosine(left: np.ndarray, right: np.ndarray, *, clamp: bool = True) -> float:
    """Cosine similarity, mapped to 0–1 by default.

    The map matters. Raw cosine runs -1 to 1, and a negative value means "opposed", which for
    non-negative feature vectors is no more similar than orthogonal. Clamping at zero rather
    than rescaling keeps "unrelated" at 0 instead of putting it at 0.5.

    ``clamp=False`` returns the raw -1…1 value, for callers that have already centred their
    vectors and are therefore computing a correlation, where -1 is meaningful rather than an
    artefact.
    """
    if left.size == 0 or right.size == 0 or left.size != right.size:
        return 0.0
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denominator <= 0:
        return 0.0
    value = float(left @ right) / denominator
    if not math.isfinite(value):
        return 0.0
    if not clamp:
        return max(-1.0, min(1.0, value))
    return max(0.0, min(1.0, value))


def _tempo_similarity(left: float | None, right: float | None) -> float:
    """1.0 at the same tempo, 0.0 at ``_TEMPO_SPAN`` apart.

    Half and double time count as close: a 140 BPM track and a 70 BPM track share a pulse, and
    a detector that reports one as the other — which they routinely do — should not make two
    renders of the same groove look unrelated.
    """
    if not left or not right or left <= 0 or right <= 0:
        return 0.0
    candidates = [abs(left - right), abs(left - right * 2), abs(left * 2 - right)]
    closest = min(candidates)
    return max(0.0, 1.0 - closest / _TEMPO_SPAN)


class SimilarityEngine:
    """Compares one candidate against the station's library.

    Stateless apart from its settings: the library is passed in, so the engine is a pure
    function of its inputs and testable without a database.
    """

    def __init__(self, settings: OriginalitySettings) -> None:
        self._settings = settings
        self._weights = settings.weights.as_dict()

    # -- staged search ----------------------------------------------------

    def _stage_two_shortlist(
        self, candidate_embedding: np.ndarray, library: list[LibraryEntry]
    ) -> list[tuple[LibraryEntry, float]]:
        """Rank the whole library by embedding cosine, vectorised, and keep the top slice.

        One matrix multiply rather than a Python loop: at ten thousand tracks the loop costs
        seconds and this costs milliseconds, and the comparison is identical.
        """
        usable = [entry for entry in library if entry.embedding]
        if not usable or candidate_embedding.size == 0:
            return [(entry, 0.0) for entry in library[:SHORTLIST_SIZE]]

        width = candidate_embedding.size
        # Entries whose embedding was computed by a different feature backend can have a
        # different width. Comparing those would be meaningless, so they are excluded here
        # and fall through to stage 3 only if the shortlist is short.
        matching = [entry for entry in usable if len(entry.embedding) == width]
        if not matching:
            return [(entry, 0.0) for entry in library[:SHORTLIST_SIZE]]

        matrix = np.asarray([entry.embedding for entry in matching], dtype=np.float64)
        norms = np.linalg.norm(matrix, axis=1)
        candidate_norm = float(np.linalg.norm(candidate_embedding))
        if candidate_norm <= 0:
            return [(entry, 0.0) for entry in matching[:SHORTLIST_SIZE]]

        safe = np.where(norms > 0, norms, 1.0)
        scores = (matrix @ candidate_embedding) / (safe * candidate_norm)
        scores = np.clip(np.nan_to_num(scores, nan=0.0), 0.0, 1.0)

        order = np.argsort(scores)[::-1][:SHORTLIST_SIZE]
        return [(matching[int(index)], float(scores[int(index)])) for index in order]

    def _compare(
        self,
        *,
        entry: LibraryEntry,
        embedding_score: float,
        candidate_features: AudioFeatures,
        candidate_fingerprint: AudioFingerprint | None,
        candidate_lyrics: LyricFingerprint | None,
        candidate_blueprint: BlueprintSummary | None,
        candidate_time: datetime,
    ) -> TrackComparison:
        """Stage 3: the full comparison for one pair."""
        chroma = _profile_similarity(
            np.asarray(candidate_features.chroma_mean, dtype=np.float64),
            np.asarray(entry.chroma_mean, dtype=np.float64),
        )
        mfcc = _timbre_similarity(
            np.asarray(candidate_features.mfcc_mean, dtype=np.float64),
            np.asarray(entry.mfcc_mean, dtype=np.float64),
        )
        tempo = _tempo_similarity(candidate_features.tempo, entry.tempo)

        fingerprint_score = 0.0
        fingerprint_comparable = False
        if (
            candidate_fingerprint is not None
            and entry.fingerprint is not None
            # Never compare across providers: the numbers are not on the same scale and the
            # result would be meaningless rather than merely imprecise.
            and candidate_fingerprint.comparable_with(entry.fingerprint)
        ):
            if candidate_fingerprint.fingerprint == entry.fingerprint.fingerprint:
                fingerprint_score = 1.0
                fingerprint_comparable = True
            elif candidate_fingerprint.vector and entry.fingerprint.vector:
                fingerprint_score = _cosine(
                    np.asarray(candidate_fingerprint.vector, dtype=np.float64),
                    np.asarray(entry.fingerprint.vector, dtype=np.float64),
                )
                fingerprint_comparable = True

        lyric_score = 0.0
        exact_lyrics = False
        if candidate_lyrics is not None and entry.lyrics is not None:
            comparison = compare_lyrics(candidate_lyrics, entry.lyrics)
            lyric_score = comparison.score
            exact_lyrics = comparison.exact_match

        blueprint_score = 0.0
        blueprint_limit = 1.0
        recent = False
        if candidate_blueprint is not None and entry.blueprint is not None:
            blueprint_score = compare_blueprints(candidate_blueprint, entry.blueprint).score
            blueprint_limit, recent = recency_threshold(candidate_time, entry.created_at)

        components = SimilarityComponents(
            audio_fingerprint=fingerprint_score,
            embedding=embedding_score,
            chroma=chroma,
            mfcc=mfcc,
            tempo=tempo,
            lyrics=lyric_score,
            blueprint=blueprint_score,
        )

        # Weights are renormalised over the components that actually have an input, which is
        # what the Phase 1 config means by "redistributed when an input is missing". Without
        # it, an instrumental track would score low on every comparison purely because the
        # lyric weight contributed zero — making every instrumental look more novel than it is.
        present = {
            # Present only when a score could actually be computed. A provider with no
            # vector contributes an equality check and nothing else, so a pair that merely
            # fails to match must not be scored 0.0 against a weight — that would record
            # "definitely unalike" where the truth is "this provider cannot say".
            "audio_fingerprint": fingerprint_comparable,
            "embedding": bool(entry.embedding),
            "chroma": bool(entry.chroma_mean),
            "mfcc": bool(entry.mfcc_mean),
            "tempo": bool(entry.tempo) and bool(candidate_features.tempo),
            "lyrics": candidate_lyrics is not None and entry.lyrics is not None,
            "blueprint": candidate_blueprint is not None and entry.blueprint is not None,
        }
        active = {name: self._weights[name] for name, ok in present.items() if ok}
        total_weight = sum(active.values())
        values = components.as_dict()
        score = (
            sum(weight * values[name] for name, weight in active.items()) / total_weight
            if total_weight > 0
            else 0.0
        )

        is_exact_audio = (
            components.embedding >= EXACT_AUDIO_THRESHOLD
            and components.chroma >= EXACT_AUDIO_THRESHOLD
        ) or components.audio_fingerprint >= 1.0

        return TrackComparison(
            existing_track_id=entry.track_id,
            score=max(0.0, min(1.0, score)),
            components=components,
            is_exact_audio=is_exact_audio,
            is_exact_lyrics=exact_lyrics,
            blueprint_threshold=blueprint_limit,
            blueprint_is_recent=recent,
            detail=f"closest on {components.strongest()[0].replace('_', ' ')}",
        )

    # -- the public entry point -------------------------------------------

    def evaluate(
        self,
        *,
        track_id: str,
        features: AudioFeatures,
        canonical_hash: str,  # noqa: ARG002 - stage 1 runs in the repository; see below
        library: list[LibraryEntry],
        fingerprint: AudioFingerprint | None = None,
        lyrics: LyricFingerprint | None = None,
        blueprint: BlueprintSummary | None = None,
        now: datetime | None = None,
        duplicate_hash_owner: str | None = None,
    ) -> SimilarityOutcome:
        """Judge one candidate against the library.

        ``duplicate_hash_owner`` is the track id an identical canonical hash already belongs
        to, looked up by the caller's repository — stage 1, kept outside the engine so the
        lookup can be an indexed query rather than a scan.
        """
        import time  # noqa: PLC0415

        started = time.perf_counter()
        moment = now or datetime.now(tz=UTC)

        # -- stage 1: exact content --------------------------------------
        if self._settings.reject_exact_hash and duplicate_hash_owner:
            identical = TrackComparison(
                existing_track_id=duplicate_hash_owner,
                score=1.0,
                components=SimilarityComponents(
                    audio_fingerprint=1.0, embedding=1.0, chroma=1.0, mfcc=1.0
                ),
                is_exact_audio=True,
                detail="identical audio",
            )
            return SimilarityOutcome(
                track_id=track_id,
                verdict=OriginalityVerdict.REJECT,
                novelty_score=0.0,
                max_similarity=1.0,
                threshold=self._settings.reject_similarity,
                closest=identical,
                top_comparisons=(identical,),
                compared_against=len(library),
                deciding_component="exact_hash",
                reasons=(
                    f"the audio is byte-for-byte identical to {duplicate_hash_owner}",
                ),
                elapsed_seconds=time.perf_counter() - started,
            )

        if not library:
            return SimilarityOutcome(
                track_id=track_id,
                verdict=OriginalityVerdict.APPROVE,
                novelty_score=1.0,
                max_similarity=0.0,
                threshold=self._settings.reject_similarity,
                closest=None,
                compared_against=0,
                elapsed_seconds=time.perf_counter() - started,
            )

        # -- stage 2: cheap shortlist ------------------------------------
        shortlist = self._stage_two_shortlist(features.embedding(), library)

        # -- stage 3: full comparison against the shortlist --------------
        comparisons = [
            self._compare(
                entry=entry,
                embedding_score=embedding_score,
                candidate_features=features,
                candidate_fingerprint=fingerprint,
                candidate_lyrics=lyrics,
                candidate_blueprint=blueprint,
                candidate_time=moment,
            )
            for entry, embedding_score in shortlist
        ]
        if not comparisons:
            return SimilarityOutcome(
                track_id=track_id,
                verdict=OriginalityVerdict.APPROVE,
                novelty_score=1.0,
                max_similarity=0.0,
                threshold=self._settings.reject_similarity,
                closest=None,
                compared_against=len(library),
                elapsed_seconds=time.perf_counter() - started,
            )

        closest = max(comparisons, key=lambda comparison: comparison.score)
        max_similarity = closest.score

        reasons: list[str] = []
        verdict = OriginalityVerdict.APPROVE
        deciding: str | None = None

        # Independent rejection rules, each checked against every comparison rather than only
        # against the highest-scoring one. A track can be an exact lyric match to one track
        # and a near-audio match to another, and the overall score hides both.
        exact_audio = next((c for c in comparisons if c.is_exact_audio), None)
        if exact_audio is not None:
            verdict = OriginalityVerdict.REJECT
            deciding = "audio"
            reasons.append(
                f"the audio is effectively identical to {exact_audio.existing_track_id} "
                f"(similarity {exact_audio.components.embedding:.3f})"
            )

        exact_lyrics = next((c for c in comparisons if c.is_exact_lyrics), None)
        if exact_lyrics is not None:
            verdict = OriginalityVerdict.REJECT
            deciding = deciding or "lyrics"
            reasons.append(
                f"the lyrics are identical to {exact_lyrics.existing_track_id}"
            )

        blueprint_repeat = next(
            (
                c
                for c in comparisons
                if c.components.blueprint >= c.blueprint_threshold
            ),
            None,
        )
        if blueprint_repeat is not None:
            verdict = OriginalityVerdict.REJECT
            deciding = deciding or "blueprint"
            window = "recently" if blueprint_repeat.blueprint_is_recent else "previously"
            reasons.append(
                f"the creative blueprint repeats {blueprint_repeat.existing_track_id} "
                f"({blueprint_repeat.components.blueprint:.2f} against a "
                f"{blueprint_repeat.blueprint_threshold:.2f} limit for a track aired {window})"
            )

        if verdict is OriginalityVerdict.APPROVE:
            if max_similarity >= self._settings.reject_similarity:
                verdict = OriginalityVerdict.REJECT
                deciding = closest.components.strongest()[0]
                reasons.append(
                    f"overall similarity {max_similarity:.2f} to "
                    f"{closest.existing_track_id} exceeds the "
                    f"{self._settings.reject_similarity:.2f} rejection threshold"
                )
            elif max_similarity >= self._settings.review_similarity:
                verdict = OriginalityVerdict.REVIEW
                deciding = closest.components.strongest()[0]
                reasons.append(
                    f"similarity {max_similarity:.2f} to {closest.existing_track_id} is above "
                    f"the {self._settings.review_similarity:.2f} review threshold"
                )

        ranked = sorted(comparisons, key=lambda comparison: comparison.score, reverse=True)

        outcome = SimilarityOutcome(
            track_id=track_id,
            verdict=verdict,
            novelty_score=novelty_from_similarity(max_similarity),
            max_similarity=max_similarity,
            threshold=self._settings.reject_similarity,
            closest=closest,
            top_comparisons=tuple(ranked[:_RETAINED_COMPARISONS]),
            compared_against=len(library),
            deciding_component=deciding,
            reasons=tuple(reasons),
            elapsed_seconds=time.perf_counter() - started,
        )
        _log.info(
            "originality.evaluated",
            track_id=track_id,
            verdict=verdict.value,
            novelty=round(outcome.novelty_score, 3),
            max_similarity=round(max_similarity, 3),
            closest=closest.existing_track_id,
            deciding=deciding,
            compared=len(library),
            shortlisted=len(comparisons),
            seconds=round(outcome.elapsed_seconds, 3),
        )
        return outcome


def novelty_from_similarity(max_similarity: float) -> float:
    """The novelty score (§6.6). Documented, not mysterious.

    .. code-block:: text

        novelty = 1 − max_similarity

    That is the whole formula, and the simplicity is the point. §6.6 asks for a clear number
    on 0–1 where higher means more distinct, and any curve applied here — a power, a sigmoid —
    would make the score easier to tune and impossible to explain. "Novelty 0.73 means the
    closest track in the library scores 0.27" is a sentence an operator can check.

    It is the **maximum** similarity rather than the mean, because novelty is a property of the
    nearest neighbour. A track that is unlike nine hundred tracks and identical to one is not
    novel, and averaging would say it was.
    """
    return max(0.0, min(1.0, 1.0 - max_similarity))
