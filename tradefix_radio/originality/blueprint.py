"""Blueprint similarity (§6.7).

A separate check because it catches something the audio comparison structurally cannot: two
tracks whose *creative decision* is the same, rendered differently. The generator introduces
enough variation that two tracks from near-identical blueprints sound distinguishable to a
fingerprint while being, to a listener, the same idea twice.

Phase 3 already defines `MusicBlueprintV1.signature()` — a hash over genre, BPM, key, structure,
instrumentation, mood and bucketed energy — and Phase 4's scheduler already refuses to plan a
batch containing a repeat of one. This module does the thing a hash cannot: measure *how close*
two blueprints are when they are not identical.

Recency matters more than history here
--------------------------------------
§6.7 asks for stronger rules against recent repetition than against all-time, and the reasoning
is about what a listener experiences. The same blueprint twice in an hour is obvious; the same
blueprint twice in a fortnight is a station with a consistent sound. So the threshold a
candidate is held to tightens for recent tracks and relaxes for old ones.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradefix_radio.contracts.music import MusicBlueprintV1

__all__ = [
    "BlueprintSimilarity",
    "BlueprintSummary",
    "compare_blueprints",
    "recency_threshold",
    "summarise_blueprint",
]

#: Component weights. They sum to 1.0 and are ordered by how much each shapes what a listener
#: hears: genre and tempo define the track, key and structure colour it, topic and persona
#: matter least because the same subject in a different genre is a different track.
_WEIGHTS: Final[dict[str, float]] = {
    "genre": 0.26,
    "secondary_genre": 0.06,
    "bpm": 0.18,
    "key": 0.10,
    "energy": 0.12,
    "duration": 0.04,
    "vocal": 0.08,
    "topic": 0.10,
    "persona": 0.06,
}

#: BPM difference at which tempo similarity reaches zero.
#:
#: 40 BPM. Two tracks 40 BPM apart are in different tempo families — 90 and 130 are not the
#: same track at any level of description — while 4 BPM apart is the same groove.
_BPM_SPAN: Final = 40.0

#: Duration difference at which duration similarity reaches zero, in seconds.
_DURATION_SPAN: Final = 120.0

#: How long "recent" lasts for the stricter threshold.
RECENCY_WINDOW: Final = timedelta(hours=6)

#: Blueprint similarity tolerated inside the recency window, and outside it.
#:
#: The gap is deliberate and sizeable. Within six hours a near-identical blueprint is a
#: listener hearing the same idea twice in one sitting; a fortnight later it is the station
#: having a recognisable sound, which is the point of §10's genre library.
RECENT_THRESHOLD: Final = 0.86
HISTORIC_THRESHOLD: Final = 0.95


@dataclass(frozen=True)
class BlueprintSummary:
    """The comparable fields of a blueprint, flattened.

    Extracted so comparison never reaches into the nested contract, and so a stored summary
    can be compared without rehydrating a full `MusicBlueprintV1`.
    """

    track_id: str
    signature: str
    genre: str
    secondary_genre: str | None
    bpm: int
    musical_key: str
    duration_seconds: int
    energy: float
    is_instrumental: bool
    vocal_style: str | None
    primary_topic: str | None
    secondary_topic: str | None
    persona_id: str | None
    created_at: datetime | None = None


def summarise_blueprint(
    blueprint: MusicBlueprintV1, *, created_at: datetime | None = None
) -> BlueprintSummary:
    composition = blueprint.composition
    return BlueprintSummary(
        track_id=blueprint.track_id,
        signature=blueprint.signature(),
        genre=composition.genre,
        secondary_genre=composition.secondary_genre,
        bpm=composition.bpm,
        musical_key=composition.key,
        duration_seconds=composition.duration_seconds,
        energy=composition.energy,
        is_instrumental=blueprint.is_instrumental,
        vocal_style=None if blueprint.is_instrumental else blueprint.vocal.style.value,
        primary_topic=blueprint.lyrics.primary_topic,
        secondary_topic=blueprint.lyrics.secondary_topic,
        persona_id=blueprint.persona_id,
        created_at=created_at,
    )


@dataclass(frozen=True)
class BlueprintSimilarity:
    """How close two creative decisions are, and which parts drove it."""

    score: float
    exact_signature: bool
    components: dict[str, float]
    detail: str

    def strongest(self, limit: int = 3) -> list[tuple[str, float]]:
        """The components contributing most, for the UI's explanation."""
        return sorted(self.components.items(), key=lambda item: item[1], reverse=True)[:limit]


def _key_similarity(left: str, right: str) -> float:
    """1.0 for the same key, 0.5 for a relative major/minor pair, 0.25 for the same tonic.

    Musically motivated rather than a string match: A minor and C major share a key signature
    and sound related, so treating them as entirely different overstates novelty.
    """
    if left == right:
        return 1.0
    left_parts, right_parts = left.split(), right.split()
    if len(left_parts) == 2 and len(right_parts) == 2:
        left_tonic, left_mode = left_parts
        right_tonic, right_mode = right_parts
        if left_tonic == right_tonic:
            # Same tonic, different mode — C major against C minor.
            return 0.25
        if left_mode != right_mode:
            semitones = {
                "C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "F": 5,
                "F#": 6, "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11,
            }
            if left_tonic in semitones and right_tonic in semitones:
                distance = (semitones[left_tonic] - semitones[right_tonic]) % 12
                # A minor is three semitones below C major, and vice versa.
                if (left_mode.startswith("min") and distance == 9) or (
                    right_mode.startswith("min") and distance == 3
                ):
                    return 0.5
    return 0.0


def _linear_closeness(difference: float, span: float) -> float:
    """1.0 at no difference, 0.0 at ``span`` or beyond."""
    if span <= 0:
        return 0.0
    return max(0.0, 1.0 - abs(difference) / span)


def compare_blueprints(
    candidate: BlueprintSummary, existing: BlueprintSummary
) -> BlueprintSimilarity:
    """Weighted similarity between two creative decisions. 0–1, higher meaning more alike."""
    if candidate.signature == existing.signature:
        return BlueprintSimilarity(
            score=1.0,
            exact_signature=True,
            components=dict.fromkeys(_WEIGHTS, 1.0),
            detail="the blueprint signatures are identical",
        )

    components: dict[str, float] = {
        "genre": 1.0 if candidate.genre == existing.genre else 0.0,
        "secondary_genre": (
            1.0
            if candidate.secondary_genre
            and candidate.secondary_genre == existing.secondary_genre
            else 0.0
        ),
        "bpm": _linear_closeness(candidate.bpm - existing.bpm, _BPM_SPAN),
        "key": _key_similarity(candidate.musical_key, existing.musical_key),
        "energy": _linear_closeness(candidate.energy - existing.energy, 1.0),
        "duration": _linear_closeness(
            candidate.duration_seconds - existing.duration_seconds, _DURATION_SPAN
        ),
        "vocal": _vocal_similarity(candidate, existing),
        "topic": _topic_similarity(candidate, existing),
        "persona": (
            1.0
            if candidate.persona_id and candidate.persona_id == existing.persona_id
            else 0.0
        ),
    }

    score = sum(_WEIGHTS[name] * value for name, value in components.items())
    score = max(0.0, min(1.0, score))

    leaders = sorted(
        ((name, _WEIGHTS[name] * value) for name, value in components.items()),
        key=lambda item: item[1],
        reverse=True,
    )[:2]
    detail = (
        "same " + " and ".join(name.replace("_", " ") for name, _ in leaders)
        if leaders and leaders[0][1] > 0
        else "little in common"
    )

    return BlueprintSimilarity(
        score=score, exact_signature=False, components=components, detail=detail
    )


def _vocal_similarity(candidate: BlueprintSummary, existing: BlueprintSummary) -> float:
    if candidate.is_instrumental != existing.is_instrumental:
        return 0.0
    if candidate.is_instrumental:
        # Both instrumental: the same decision, and there is no style to differ on.
        return 1.0
    return 1.0 if candidate.vocal_style == existing.vocal_style else 0.3


def _topic_similarity(candidate: BlueprintSummary, existing: BlueprintSummary) -> float:
    left = {t for t in (candidate.primary_topic, candidate.secondary_topic) if t}
    right = {t for t in (existing.primary_topic, existing.secondary_topic) if t}
    if not left or not right:
        return 0.0
    overlap = left & right
    if not overlap:
        return 0.0
    # Both topics shared is a stronger signal than one, and the primary matching matters
    # more than the secondary.
    if candidate.primary_topic and candidate.primary_topic == existing.primary_topic:
        return 1.0 if len(overlap) > 1 else 0.7
    return 0.4


def recency_threshold(
    candidate_time: datetime, existing_time: datetime | None
) -> tuple[float, bool]:
    """The blueprint-similarity threshold to hold this pair to, and whether it is recent.

    §6.7's stronger-for-recent rule, made concrete. An unknown timestamp is treated as recent:
    being strict about something unknown risks a regeneration, while being lax risks airing a
    repeat, and the first is much cheaper than the second.
    """
    if existing_time is None:
        return RECENT_THRESHOLD, True
    # Both sides may be naive or aware depending on how they were stored; compare on a common
    # footing rather than raising.
    left = candidate_time.replace(tzinfo=None) if candidate_time.tzinfo else candidate_time
    right = existing_time.replace(tzinfo=None) if existing_time.tzinfo else existing_time
    age = abs(left - right)
    if age <= RECENCY_WINDOW:
        return RECENT_THRESHOLD, True
    return HISTORIC_THRESHOLD, False


def blueprint_distance(candidate: BlueprintSummary, existing: BlueprintSummary) -> float:
    """1 − similarity, for callers that think in distances."""
    return 1.0 - compare_blueprints(candidate, existing).score


def is_finite(value: float) -> bool:
    return math.isfinite(value)
