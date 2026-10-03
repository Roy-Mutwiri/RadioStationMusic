"""Second-stage resolution of REVIEW verdicts (B2).

The first stage produces one combined score and three verdicts, and the station used to
implement two dispositions — REVIEW was mapped straight to REJECT by a setting named
``regenerate_on_review`` that never regenerated anything. That discarded 43% of everything
the station generated, and the real station test measured the result: a 7% approval rate
and an hour of procedural fallback.

REVIEW is not a weaker REJECT. It means the cheap combined score did not carry enough
evidence to decide, and something has to look at *what kind* of similarity it was.

The distinction this module exists to make
------------------------------------------
Two lo-fi tracks at 86 and 87 BPM from one generator share timbre, tempo and spectral
shape. Those are properties of the genre, not evidence that a recording was copied.
Measured over 10 440 pairs of the station's own output
(docs/status/COMPONENT_AUTHORITY.md):

===========  ==================  ==================  =================
component    all pairs           same-genre delta    same-BPM delta
===========  ==================  ==================  =================
fingerprint  0.511 +/- 0.021     +0.003              +0.002
mfcc         0.971 +/- 0.026     +0.007              +0.003
chroma       0.308 +/- 0.271     +0.078              +0.034
tempo        0.397 +/- 0.360     +0.450              +0.598
===========  ==================  ==================  =================

MFCC answers ~0.97 for every pair in the corpus, so it cannot be evidence about anything.
Tempo mostly restates the BPM band the director deliberately chose. Neither belongs in a
duplication verdict, and both remain useful to the diversity director, which exists to
vary consecutive programming.

Thresholds
----------
Every number below comes from docs/status/CHROMAPRINT_CALIBRATION.md, which fingerprinted
one recording against twelve modifications of itself and eight different recordings:

* same recording, time-aligned (re-encode, +/- gain, normalise, EQ, compression, trailing
  pad): **0.9068 – 1.0000**
* same recording, time-shifted (0.5 s lead, 5 s crop): 0.6222 – 0.6829
* different recordings: 0.4733 – **0.6404**

The aligned population clears the different-recording population by +0.2664, which is
where `CERTAIN_DUPLICATE` sits. The shifted variants overlap the different-recording
range, so agreement in that band cannot decide alone and requires corroboration — hence
`SUSPECTED_DUPLICATE`. Alignment is preserved in practice because originality runs on raw
audio before mastering trims anything.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

import structlog

from tradefix_radio.originality.similarity import (
    OriginalityVerdict,
    SimilarityOutcome,
    TrackComparison,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradefix_radio.config.schema import OriginalitySettings

_log = structlog.get_logger(__name__)

#: Bumped whenever a rule or threshold changes, and stored with every decision.
#:
#: Without it a disposition cannot be interpreted later: "approved" means something
#: different under different rules, and the §48 page would present old decisions as though
#: the current policy had made them.
RESOLVER_VERSION: Final = "1.0.0"

#: Fingerprint agreement at or above which two files are the same recording.
#:
#: 0.90, immediately below the 0.9068 floor of every time-aligned modification measured
#: and 0.26 above the 0.6404 ceiling of every different recording measured.
CERTAIN_DUPLICATE: Final = 0.90

#: Agreement above which a recording is *suspicious* but not proven.
#:
#: 0.65, just above the 0.6404 highest different-recording observation. The time-shifted
#: same-recording variants (0.6222, 0.6829) straddle this, which is exactly why this band
#: demands corroboration instead of deciding.
SUSPECTED_DUPLICATE: Final = 0.65

#: Corroboration required to turn a suspicion into a rejection.
#:
#: Both must hold together. Either alone saturates on same-genre material — embedding
#: averages 0.88 and chroma reaches 0.98 between unrelated tracks of one genre.
CORROBORATING_EMBEDDING: Final = 0.97
CORROBORATING_CHROMA: Final = 0.95

#: Creative similarity that is too high to air *right now*, against very recent output.
#:
#: This is a rotation rule, not a duplication one, so it is scoped to a short horizon and
#: says so in the reason it records.
ROTATION_SIMILARITY: Final = 0.93
ROTATION_HORIZON: Final = timedelta(hours=6)
#: How many of the most recent production tracks the rotation rule considers.
ROTATION_RECENT_TRACKS: Final = 20


class ReviewDisposition(str, enum.Enum):
    """What the resolver decided to do with a REVIEW candidate."""

    FINAL_APPROVE = "final_approve"
    FINAL_REJECT = "final_reject"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    """Reserved for a supervised workflow. Autonomous production never returns this —
    a station with nobody watching cannot leave a track undecided, and the whole defect
    being fixed here was candidates with no terminal disposition."""


class EvidenceClass(str, enum.Enum):
    """Why the resolver decided what it did, in terms of the evidence it used."""

    DEFINITIVE_DUPLICATE = "definitive_duplicate"
    """Class A: exact content. Canonical hash or byte-identical audio."""

    STRONG_RECORDING_MATCH = "strong_recording_match"
    """Class B: fingerprint agreement in the measured same-recording band."""

    CORROBORATED_RECORDING_MATCH = "corroborated_recording_match"
    """Class B: suspicious fingerprint agreement, confirmed by structure."""

    ROTATION_PRESSURE = "rotation_pressure"
    """Class C/D against very recent production output. Not a duplicate — too soon."""

    STYLE_ONLY = "style_only"
    """Class D: the similarity is timbre, tempo, key or genre. Expected, and not a reason
    to discard a recording."""

    NO_PRODUCTION_HISTORY = "no_production_history"
    """Cold start: nothing has aired, so there is nothing to repeat."""


@dataclass(frozen=True)
class ReviewResolution:
    """The resolver's answer, with the evidence that produced it.

    The initial verdict is carried alongside the final one and never overwritten: an
    operator needs to see that a track *began* as REVIEW, and an approval that hides its
    own history is the kind of silent reinterpretation this work exists to remove.
    """

    track_id: str
    initial_verdict: OriginalityVerdict
    disposition: ReviewDisposition
    evidence_class: EvidenceClass
    reason: str
    resolver_version: str = RESOLVER_VERSION
    duplication_risk: float = 0.0
    creative_similarity: float = 0.0
    closest_track_id: str | None = None
    components: dict[str, float] = field(default_factory=dict)
    production_references: int = 0

    @property
    def approved(self) -> bool:
        return self.disposition is ReviewDisposition.FINAL_APPROVE

    @property
    def is_cold_start(self) -> bool:
        return self.evidence_class is EvidenceClass.NO_PRODUCTION_HISTORY

    def as_payload(self) -> dict[str, object]:
        """For persistence and the §48 page."""
        return {
            "initial_verdict": self.initial_verdict.value,
            "disposition": self.disposition.value,
            "evidence_class": self.evidence_class.value,
            "reason": self.reason,
            "resolver_version": self.resolver_version,
            "duplication_risk": round(self.duplication_risk, 4),
            "creative_similarity": round(self.creative_similarity, 4),
            "closest_track_id": self.closest_track_id,
            "production_references": self.production_references,
            "components": {k: round(v, 4) for k, v in self.components.items()},
        }


class ReviewResolver:
    """Resolves a REVIEW verdict into a terminal disposition, deterministically.

    Pure: given the same outcome and the same clock reading it returns the same answer,
    so a disposition can be replayed and argued with.
    """

    def __init__(self, settings: OriginalitySettings) -> None:
        self._settings = settings

    def resolve(
        self,
        outcome: SimilarityOutcome,
        *,
        now: datetime,
        production_references: int,
    ) -> ReviewResolution:
        """Decide a REVIEW candidate.

        ``production_references`` is how many PRODUCTION_RADIO tracks the candidate was
        compared against. Zero means cold start, which is reported rather than presented
        as a confident approval.
        """
        comparisons = outcome.top_comparisons or (
            (outcome.closest,) if outcome.closest else ()
        )
        comparisons = tuple(c for c in comparisons if c is not None)

        def build(
            disposition: ReviewDisposition,
            evidence: EvidenceClass,
            reason: str,
            comparison: TrackComparison | None,
        ) -> ReviewResolution:
            return ReviewResolution(
                track_id=outcome.track_id,
                initial_verdict=outcome.verdict,
                disposition=disposition,
                evidence_class=evidence,
                reason=reason,
                duplication_risk=(
                    comparison.components.duplication_risk if comparison else 0.0
                ),
                creative_similarity=(
                    comparison.components.creative_similarity if comparison else 0.0
                ),
                closest_track_id=comparison.existing_track_id if comparison else None,
                components=comparison.components.as_dict() if comparison else {},
                production_references=production_references,
            )

        # -- Class A: exact content, whatever its provenance or age ------------
        exact = next((c for c in comparisons if c.is_exact_audio), None)
        if exact is not None:
            return build(
                ReviewDisposition.FINAL_REJECT,
                EvidenceClass.DEFINITIVE_DUPLICATE,
                f"the audio is the same recording as {exact.existing_track_id}",
                exact,
            )

        # -- Class B: the fingerprint says "same recording" --------------------
        certain = next(
            (
                c
                for c in comparisons
                if c.components.audio_fingerprint >= CERTAIN_DUPLICATE
            ),
            None,
        )
        if certain is not None:
            return build(
                ReviewDisposition.FINAL_REJECT,
                EvidenceClass.STRONG_RECORDING_MATCH,
                (
                    f"fingerprint agreement {certain.components.audio_fingerprint:.3f} "
                    f"with {certain.existing_track_id} is inside the measured "
                    f"same-recording band (>= {CERTAIN_DUPLICATE:.2f}; every different "
                    "recording measured scored at most 0.640)"
                ),
                certain,
            )

        # -- Class B, corroborated: suspicious agreement plus structure --------
        suspected = next(
            (
                c
                for c in comparisons
                if c.components.audio_fingerprint >= SUSPECTED_DUPLICATE
                and c.components.embedding >= CORROBORATING_EMBEDDING
                and c.components.chroma >= CORROBORATING_CHROMA
            ),
            None,
        )
        if suspected is not None:
            parts = suspected.components
            return build(
                ReviewDisposition.FINAL_REJECT,
                EvidenceClass.CORROBORATED_RECORDING_MATCH,
                (
                    f"fingerprint agreement {parts.audio_fingerprint:.3f} with "
                    f"{suspected.existing_track_id} is ambiguous on its own, but "
                    f"embedding {parts.embedding:.3f} and chroma {parts.chroma:.3f} "
                    "corroborate a time-shifted copy of the same recording"
                ),
                suspected,
            )

        # -- cold start --------------------------------------------------------
        if production_references == 0:
            closest = outcome.closest
            return build(
                ReviewDisposition.FINAL_APPROVE,
                EvidenceClass.NO_PRODUCTION_HISTORY,
                (
                    "no production track has aired yet, so there is nothing this could "
                    "repeat; no recording-level duplicate evidence either"
                ),
                closest,
            )

        # -- Class C/D against very recent output: a rotation concern ----------
        recent = [
            c
            for c in comparisons[:ROTATION_RECENT_TRACKS]
            if c.counts_toward_graded_novelty
            and c.components.creative_similarity >= ROTATION_SIMILARITY
            and _within(now, c, ROTATION_HORIZON)
        ]
        if recent:
            worst = max(recent, key=lambda c: c.components.creative_similarity)
            return build(
                ReviewDisposition.FINAL_REJECT,
                EvidenceClass.ROTATION_PRESSURE,
                (
                    f"creative similarity {worst.components.creative_similarity:.3f} to "
                    f"{worst.existing_track_id}, which aired within the last "
                    f"{int(ROTATION_HORIZON.total_seconds() // 3600)} hours — too soon to "
                    "repeat the character, though the recording is not a duplicate"
                ),
                worst,
            )

        # -- Class D: style, which is what a genre station is supposed to do ---
        closest = outcome.closest
        driver = outcome.deciding_component or "combined score"
        return build(
            ReviewDisposition.FINAL_APPROVE,
            EvidenceClass.STYLE_ONLY,
            (
                f"the similarity is {driver}, a style signal, with no recording-level "
                f"duplicate evidence (fingerprint "
                f"{closest.components.audio_fingerprint:.3f} against a "
                f"{CERTAIN_DUPLICATE:.2f} threshold)"
                if closest
                else "nothing comparable was found"
            ),
            closest,
        )


def _within(now: datetime, comparison: TrackComparison, horizon: timedelta) -> bool:
    """Whether the compared track is recent enough for the rotation rule.

    Deliberately conservative: an unknown air time is treated as *not* recent, because
    rejecting a track on a timestamp nobody recorded would be inventing a reason.
    """
    aired = comparison.existing_aired_at
    return aired is not None and (now - aired) <= horizon


__all__ = [
    "CERTAIN_DUPLICATE",
    "RESOLVER_VERSION",
    "SUSPECTED_DUPLICATE",
    "EvidenceClass",
    "ReviewDisposition",
    "ReviewResolution",
    "ReviewResolver",
]
