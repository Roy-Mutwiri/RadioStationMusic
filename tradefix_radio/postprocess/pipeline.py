"""The post-generation pipeline (§6.13, §6.14).

```
GENERATED → QC → FEATURES → ORIGINALITY → MASTERING → FINAL QC → APPROVED → READY
```

**Generator success is not a playable track.** §6.14 states it and this module is where it
becomes true: before Phase 6 the station walked a track straight from GENERATED to READY, so
anything the provider returned went on air. Now `PostProductionPipeline.process` is the only
path to READY, and it can stop at five different points.

Stages, and why they are in this order
--------------------------------------
Each stage is ordered to fail as cheaply as possible. Analysis comes first because it is the
input to everything else and catches a broken file for the price of one FFT. Originality comes
next because a duplicate should not consume a mastering pass. Mastering is last because it is
the only stage that writes a file and shells out twice.

The final QC pass exists because §6.10 is right: mastering returning success does not mean the
output is sound. A limiter can introduce clipping, a filter can shift loudness out of range, a
format conversion can go wrong. The mastered file is judged on its own merits.

Nothing here raises for a rejection
-----------------------------------
A rejected track is an expected outcome in a station designed to run for weeks — the brief asks
for a 20 % defect rate to be survivable. So `process` returns a `PipelineOutcome` describing
what happened, and only genuinely exceptional conditions raise. Phase 4's `GenerationOutcome`
made the same choice for the same reason.
"""

from __future__ import annotations

import asyncio
import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from tradefix_radio.audio.analysis import AudioFeatures, extract_features, warm_up
from tradefix_radio.audio.fingerprint import (
    AudioFingerprint,
    AudioFingerprintProvider,
    canonical_sha256,
    default_provider,
    file_sha256,
)
from tradefix_radio.audio.io import read_audio
from tradefix_radio.audio.mastering import (
    MasteringOutcome,
    MasteringResult,
    master_track,
)
from tradefix_radio.audio.qc import QcStage, QcStatus, TrackQcResult, run_audio_qc
from tradefix_radio.core.clock import UTC, Clock, SystemClock
from tradefix_radio.originality.blueprint import summarise_blueprint
from tradefix_radio.originality.lyrics import (
    LyricFingerprint,
    fingerprint_lyrics,
    internal_repetition_warning,
)
from tradefix_radio.originality.review import (
    ReviewResolution,
    ReviewResolver,
)
from tradefix_radio.originality.similarity import (
    LibraryEntry,
    OriginalityVerdict,
    SimilarityEngine,
    SimilarityOutcome,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradefix_radio.config.schema import AppSettings
    from tradefix_radio.contracts.music import MusicBlueprintV1

_log = structlog.get_logger(__name__)

#: An async callable mapping a canonical hash to the track that already owns it.
DuplicateHashLookup = Callable[[str], Awaitable[str | None]]

__all__ = [
    "PipelineOutcome",
    "PipelineStage",
    "PostProductionPipeline",
    "RejectionReason",
]


class PipelineStage(str, enum.Enum):
    """§6.13's state machine, at the resolution the pipeline actually works at.

    Deliberately **not** a replacement for `TrackState`. Phase 1's lifecycle is the station's
    truth and Phase 4's tests depend on it; these are the finer-grained stages *within*
    `ANALYZING` → `APPROVED` → `MASTERING` → `READY`, recorded for reporting. Introducing a
    second authoritative state machine would give two subsystems different ideas about what a
    track is.
    """

    QC_PENDING = "qc_pending"
    QC_PASSED = "qc_passed"
    ORIGINALITY_PENDING = "originality_pending"
    ORIGINALITY_PASSED = "originality_passed"
    MASTERING = "mastering"
    FINAL_QC = "final_qc"
    APPROVED = "approved"

    QC_REJECTED = "qc_rejected"
    ORIGINALITY_REJECTED = "originality_rejected"
    MASTERING_FAILED = "mastering_failed"
    FINAL_QC_REJECTED = "final_qc_rejected"
    QUARANTINED = "quarantined"

    @property
    def is_failure(self) -> bool:
        return self in _FAILURE_STAGES

    @property
    def is_terminal(self) -> bool:
        return self is PipelineStage.APPROVED or self.is_failure


_FAILURE_STAGES: Final[frozenset[PipelineStage]] = frozenset(
    {
        PipelineStage.QC_REJECTED,
        PipelineStage.ORIGINALITY_REJECTED,
        PipelineStage.MASTERING_FAILED,
        PipelineStage.FINAL_QC_REJECTED,
        PipelineStage.QUARANTINED,
    }
)

#: The only transitions the pipeline may make. An illegal one raises (§6.13).
_TRANSITIONS: Final[dict[PipelineStage, frozenset[PipelineStage]]] = {
    PipelineStage.QC_PENDING: frozenset(
        {PipelineStage.QC_PASSED, PipelineStage.QC_REJECTED, PipelineStage.QUARANTINED}
    ),
    PipelineStage.QC_PASSED: frozenset({PipelineStage.ORIGINALITY_PENDING}),
    PipelineStage.ORIGINALITY_PENDING: frozenset(
        {PipelineStage.ORIGINALITY_PASSED, PipelineStage.ORIGINALITY_REJECTED}
    ),
    PipelineStage.ORIGINALITY_PASSED: frozenset({PipelineStage.MASTERING}),
    PipelineStage.MASTERING: frozenset(
        {PipelineStage.FINAL_QC, PipelineStage.MASTERING_FAILED}
    ),
    PipelineStage.FINAL_QC: frozenset(
        {PipelineStage.APPROVED, PipelineStage.FINAL_QC_REJECTED}
    ),
    PipelineStage.APPROVED: frozenset(),
}


class IllegalPipelineTransition(RuntimeError):
    """Raised when the pipeline is asked to make a transition that cannot happen."""

    def __init__(self, from_stage: PipelineStage, to_stage: PipelineStage) -> None:
        allowed = sorted(stage.value for stage in _TRANSITIONS.get(from_stage, frozenset()))
        super().__init__(
            f"cannot move from {from_stage.value} to {to_stage.value}; "
            f"allowed: {allowed or ['none (terminal)']}"
        )
        self.from_stage = from_stage
        self.to_stage = to_stage


class RejectionReason(str, enum.Enum):
    """Why a track did not reach READY. Drives the Originality page's breakdown."""

    EXACT_DUPLICATE = "exact_duplicate"
    AUDIO_NEAR_DUPLICATE = "audio_near_duplicate"
    LYRIC_SIMILARITY = "lyric_similarity"
    BLUEPRINT_REPETITION = "blueprint_repetition"
    QC_DEFECT = "qc_defect"
    MASTER_FAILURE = "master_failure"
    FINAL_QC_DEFECT = "final_qc_defect"
    REVIEW_UNRESOLVED = "review_unresolved"


@dataclass
class PipelineOutcome:
    """What happened to one candidate, with the evidence behind it."""

    track_id: str
    stage: PipelineStage
    approved: bool
    master_path: Path | None = None
    raw_qc: TrackQcResult | None = None
    final_qc: TrackQcResult | None = None
    features: AudioFeatures | None = None
    fingerprint: AudioFingerprint | None = None
    canonical_hash: str | None = None
    #: SHA-256 of the file bytes. Bookkeeping only — ``canonical_hash`` is what duplicate
    #: detection uses, because this one changes with the container (§6.3).
    file_hash: str | None = None
    lyrics: LyricFingerprint | None = None
    similarity: SimilarityOutcome | None = None
    #: Set when the candidate entered REVIEW and the resolver decided it. Carries the
    #: initial verdict alongside the final one, so the §48 page can show that a track
    #: began as REVIEW rather than presenting the approval as if it were first-pass.
    review: ReviewResolution | None = None
    mastering: MasteringResult | None = None
    rejection_reason: RejectionReason | None = None
    #: One sentence an operator can act on, without opening a log.
    detail: str = ""
    #: Per-stage wall time, for §6.22's throughput report.
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def novelty_score(self) -> float | None:
        return self.similarity.novelty_score if self.similarity else None

    @property
    def total_seconds(self) -> float:
        return sum(self.timings.values())


class PostProductionPipeline:
    """Runs a generated file through to approval, or stops it with a reason."""

    def __init__(
        self,
        settings: AppSettings,
        *,
        clock: Clock | None = None,
        fingerprint_provider: AudioFingerprintProvider | None = None,
        master_dir: Path | None = None,
        warm_analysis: bool = True,
    ) -> None:
        self._settings = settings
        self._clock = clock or SystemClock()
        self._provider = fingerprint_provider or default_provider()
        self._engine = SimilarityEngine(settings.originality)
        self._review_resolver = ReviewResolver(settings.originality)
        self._master_dir = master_dir or (settings.paths.generated_dir / "mastered")
        if warm_analysis:
            # Paid once here rather than on the first real track — see
            # `audio.analysis.warm_up` for the measurement that motivates it.
            warm_up()

    @property
    def fingerprint_provider(self) -> AudioFingerprintProvider:
        return self._provider

    def _advance(self, current: PipelineStage, target: PipelineStage) -> PipelineStage:
        if target not in _TRANSITIONS.get(current, frozenset()):
            raise IllegalPipelineTransition(current, target)
        return target

    async def process(
        self,
        *,
        track_id: str,
        source: Path,
        blueprint: MusicBlueprintV1 | None,
        library: list[LibraryEntry],
        lyric_text: str | None = None,
        tradefix_mentions: int = 0,
        duplicate_hash_owner_lookup: DuplicateHashLookup | None = None,
        now: datetime | None = None,
    ) -> PipelineOutcome:
        """Run the whole pipeline. Never raises for a rejection.

        ``duplicate_hash_owner_lookup`` is an async callable taking a canonical hash and
        returning the track id that already owns it, or ``None``. Injected rather than taken
        from a repository so the engine stays database-free and the lookup can be an indexed
        query.

        ``now`` overrides the evaluation timestamp. It exists for §6.7's recency rule, which
        compares the candidate's time against each library entry's: under an accelerated soak
        the station's virtual clock and wall-clock time differ by hours, and taking the wall
        clock here would make every historical track look ancient and silently disable the
        stricter recent-repeat threshold.
        """
        import time  # noqa: PLC0415

        timings: dict[str, float] = {}
        stage = PipelineStage.QC_PENDING
        moment = now or self._clock.now()

        # -- decode ---------------------------------------------------------
        started = time.perf_counter()
        try:
            buffer = await asyncio.to_thread(read_audio, source)
        except Exception as error:  # noqa: BLE001 - an unreadable file is a quarantine case
            timings["decode"] = time.perf_counter() - started
            return PipelineOutcome(
                track_id=track_id,
                stage=PipelineStage.QUARANTINED,
                approved=False,
                rejection_reason=RejectionReason.QC_DEFECT,
                detail=f"the generated file could not be decoded: {error}",
                timings=timings,
            )
        timings["decode"] = time.perf_counter() - started

        # -- features -------------------------------------------------------
        started = time.perf_counter()
        features = await asyncio.to_thread(extract_features, buffer)
        timings["features"] = time.perf_counter() - started

        # -- raw QC ---------------------------------------------------------
        started = time.perf_counter()
        expected = float(blueprint.composition.duration_seconds) if blueprint else None
        raw_qc = run_audio_qc(
            track_id=track_id,
            features=features,
            settings=self._settings.qc,
            blueprint=blueprint,
            stage=QcStage.RAW,
            expected_duration_seconds=expected,
        )
        timings["qc"] = time.perf_counter() - started

        if raw_qc.status is QcStatus.FAIL:
            stage = self._advance(stage, PipelineStage.QC_REJECTED)
            return PipelineOutcome(
                track_id=track_id,
                stage=stage,
                approved=False,
                raw_qc=raw_qc,
                features=features,
                rejection_reason=RejectionReason.QC_DEFECT,
                detail=raw_qc.summary(),
                timings=timings,
            )
        stage = self._advance(stage, PipelineStage.QC_PASSED)

        # -- identity -------------------------------------------------------
        started = time.perf_counter()
        canonical = canonical_sha256(buffer)
        file_hash = await asyncio.to_thread(file_sha256, source)
        fingerprint = await asyncio.to_thread(
            self._provider.compute, source, buffer, features
        )
        timings["fingerprint"] = time.perf_counter() - started

        lyric_fingerprint = (
            fingerprint_lyrics(track_id, lyric_text, tradefix_mentions=tradefix_mentions)
            if lyric_text
            else None
        )

        duplicate_owner: str | None = None
        if duplicate_hash_owner_lookup is not None:
            duplicate_owner = await duplicate_hash_owner_lookup(canonical)

        # -- originality ----------------------------------------------------
        stage = self._advance(stage, PipelineStage.ORIGINALITY_PENDING)
        started = time.perf_counter()
        similarity = self._engine.evaluate(
            track_id=track_id,
            features=features,
            canonical_hash=canonical,
            library=library,
            fingerprint=fingerprint,
            lyrics=lyric_fingerprint,
            blueprint=summarise_blueprint(blueprint) if blueprint else None,
            now=moment,
            duplicate_hash_owner=duplicate_owner,
        )
        timings["similarity"] = time.perf_counter() - started

        verdict = similarity.verdict
        resolution: ReviewResolution | None = None
        if verdict is OriginalityVerdict.REVIEW:
            # §6.6 said autonomous mode must not leave a track in limbo, and the previous
            # implementation satisfied that by rejecting every REVIEW outright — behind a
            # setting named `regenerate_on_review` that never regenerated anything. The
            # real station test measured the cost: 43% of all generated audio discarded,
            # a 7% approval rate, and an hour of procedural fallback.
            #
            # REVIEW now goes to a resolver that looks at *what kind* of similarity it
            # was. Two lo-fi tracks at 86 and 87 BPM are not a duplicate; the same
            # recording re-encoded is. The initial verdict is kept, never overwritten.
            production_references = sum(
                1 for entry in library if entry.counts_toward_graded_novelty
            )
            resolution = self._review_resolver.resolve(
                similarity, now=moment, production_references=production_references
            )
            verdict = (
                OriginalityVerdict.APPROVE
                if resolution.approved
                else OriginalityVerdict.REJECT
            )
            _log.info(
                "originality.review_resolved",
                track_id=track_id,
                initial="review",
                disposition=resolution.disposition.value,
                evidence=resolution.evidence_class.value,
                duplication_risk=round(resolution.duplication_risk, 3),
                creative_similarity=round(resolution.creative_similarity, 3),
                production_references=production_references,
                resolver_version=resolution.resolver_version,
                reason=resolution.reason,
            )

        if verdict is OriginalityVerdict.REJECT:
            stage = self._advance(stage, PipelineStage.ORIGINALITY_REJECTED)
            return PipelineOutcome(
                track_id=track_id,
                stage=stage,
                approved=False,
                raw_qc=raw_qc,
                features=features,
                fingerprint=fingerprint,
                canonical_hash=canonical,
                file_hash=file_hash,
                lyrics=lyric_fingerprint,
                similarity=similarity,
                review=resolution,
                rejection_reason=_rejection_reason_for(similarity),
                detail=similarity.explain(),
                timings=timings,
            )
        stage = self._advance(stage, PipelineStage.ORIGINALITY_PASSED)

        # -- mastering ------------------------------------------------------
        stage = self._advance(stage, PipelineStage.MASTERING)
        self._master_dir.mkdir(parents=True, exist_ok=True)
        destination = self._master_dir / f"{track_id}.wav"
        started = time.perf_counter()
        mastering = await asyncio.to_thread(
            master_track,
            source,
            destination,
            settings=self._settings.mastering,
            blueprint=blueprint,
        )
        timings["mastering"] = time.perf_counter() - started

        if mastering.outcome is MasteringOutcome.FAILED:
            stage = self._advance(stage, PipelineStage.MASTERING_FAILED)
            return PipelineOutcome(
                track_id=track_id,
                stage=stage,
                approved=False,
                raw_qc=raw_qc,
                features=features,
                fingerprint=fingerprint,
                canonical_hash=canonical,
                file_hash=file_hash,
                lyrics=lyric_fingerprint,
                similarity=similarity,
                review=resolution,
                mastering=mastering,
                rejection_reason=RejectionReason.MASTER_FAILURE,
                detail=mastering.detail,
                timings=timings,
            )

        master_path = mastering.output_path or source

        # -- final QC (§6.10) ------------------------------------------------
        stage = self._advance(stage, PipelineStage.FINAL_QC)
        started = time.perf_counter()
        final_features = features
        final_qc = raw_qc
        if mastering.outcome is MasteringOutcome.MASTERED:
            mastered_buffer = await asyncio.to_thread(read_audio, master_path)
            final_features = await asyncio.to_thread(extract_features, mastered_buffer)
            final_qc = run_audio_qc(
                track_id=track_id,
                features=final_features,
                settings=self._settings.qc,
                blueprint=blueprint,
                stage=QcStage.MASTERED,
                expected_duration_seconds=None,
            )
        timings["final_qc"] = time.perf_counter() - started

        loudness_problem = _loudness_outside_tolerance(final_features, mastering)
        if final_qc.status is QcStatus.FAIL or loudness_problem:
            stage = self._advance(stage, PipelineStage.FINAL_QC_REJECTED)
            return PipelineOutcome(
                track_id=track_id,
                stage=stage,
                approved=False,
                raw_qc=raw_qc,
                final_qc=final_qc,
                features=final_features,
                fingerprint=fingerprint,
                canonical_hash=canonical,
                file_hash=file_hash,
                lyrics=lyric_fingerprint,
                similarity=similarity,
                review=resolution,
                mastering=mastering,
                rejection_reason=RejectionReason.FINAL_QC_DEFECT,
                detail=loudness_problem or final_qc.summary(),
                timings=timings,
            )

        stage = self._advance(stage, PipelineStage.APPROVED)
        repetition = (
            internal_repetition_warning(lyric_fingerprint) if lyric_fingerprint else None
        )
        notes = [note for note in (repetition, _peak_constrained_note(mastering)) if note]
        outcome = PipelineOutcome(
            track_id=track_id,
            stage=stage,
            approved=True,
            master_path=master_path,
            raw_qc=raw_qc,
            final_qc=final_qc,
            features=final_features,
            fingerprint=fingerprint,
            canonical_hash=canonical,
            file_hash=file_hash,
            lyrics=lyric_fingerprint,
            similarity=similarity,
            review=resolution,
            mastering=mastering,
            detail=(
                f"approved; novelty {similarity.novelty_score:.2f}"
                + ("".join(f"; note: {note}" for note in notes))
            ),
            timings=timings,
        )
        _log.info(
            "pipeline.approved",
            track_id=track_id,
            novelty=round(similarity.novelty_score, 3),
            seconds=round(outcome.total_seconds, 2),
            stages={name: round(value, 3) for name, value in timings.items()},
        )
        return outcome


def _rejection_reason_for(similarity: SimilarityOutcome) -> RejectionReason:
    """Map the engine's deciding component onto the UI's breakdown categories."""
    deciding = similarity.deciding_component or ""
    if deciding == "exact_hash":
        return RejectionReason.EXACT_DUPLICATE
    if deciding == "lyrics":
        return RejectionReason.LYRIC_SIMILARITY
    if deciding == "blueprint":
        return RejectionReason.BLUEPRINT_REPETITION
    if similarity.verdict is OriginalityVerdict.REVIEW:
        return RejectionReason.REVIEW_UNRESOLVED
    return RejectionReason.AUDIO_NEAR_DUPLICATE


def _peak_constrained_note(mastering: MasteringResult) -> str | None:
    """Say so on the approval when the target was traded away for the ceiling.

    An approved track that is quieter than its band is worth surfacing even though it is not a
    defect: a station where many tracks are peak-constrained is telling the operator that the
    configured target is too ambitious for what the generator produces, and that is a tuning
    decision a person should make rather than a number the pipeline quietly absorbs.
    """
    if not mastering.peak_constrained or mastering.measured_lufs_after is None:
        return None
    return (
        f"peak-constrained at {mastering.measured_lufs_after:.1f} LUFS against a "
        f"{mastering.target_lufs:.1f} LUFS target; the true-peak ceiling was reached first"
    )


def _loudness_outside_tolerance(
    features: AudioFeatures, mastering: MasteringResult
) -> str | None:
    """Whether the mastered file actually hit its target (§6.10).

    Checked separately from QC's own loudness range because the question is different: QC asks
    "is this audible and not crushed", this asks "did mastering do what it said". A file that
    is comfortably inside QC's wide range can still have missed its target by 6 LU, which
    means the normalisation silently failed.

    The test is deliberately **asymmetric**, and the asymmetry is the point:

    * Louder than target is always a defect. Nothing physical forces a master to overshoot, so
      an overshoot means the normalisation misbehaved, and a track that is 2 LU hotter than the
      rest of the station is the exact thing §6.9 exists to prevent.
    * Quieter than target is a defect *only when there was room to go louder*. When the limiter
      is already sitting on the true-peak ceiling, the undershoot is the material's crest
      factor, not a fault — and failing the track would mean rejecting every dynamic recording
      while passing every crushed one, which inverts what mastering is for.

    This is not a relaxed threshold. The tolerance is unchanged; the undershoot branch is
    excused only on *measured evidence* that the ceiling was reached, so a master that is quiet
    with headroom to spare still fails.
    """
    from tradefix_radio.audio.mastering import LOUDNESS_TOLERANCE_LU  # noqa: PLC0415

    if mastering.outcome is not MasteringOutcome.MASTERED:
        return None
    if features.integrated_lufs is None:
        # No meter installed. Not a failure — the mastering stage used FFmpeg's own
        # measurement, which is authoritative — but nothing to verify against here.
        return None
    drift = features.integrated_lufs - mastering.target_lufs
    if drift > LOUDNESS_TOLERANCE_LU:
        return (
            f"mastered to {features.integrated_lufs:.1f} LUFS, "
            f"{drift:.1f} LU louder than its {mastering.target_lufs:.1f} LUFS target"
        )
    if drift < -LOUDNESS_TOLERANCE_LU and not mastering.peak_constrained:
        headroom = (
            ""
            if mastering.true_peak_dbtp is None or mastering.true_peak_ceiling_dbtp is None
            else (
                f" with {mastering.true_peak_ceiling_dbtp - mastering.true_peak_dbtp:.1f} dB "
                "of headroom left unused"
            )
        )
        return (
            f"mastered to {features.integrated_lufs:.1f} LUFS, "
            f"{-drift:.1f} LU quieter than its {mastering.target_lufs:.1f} LUFS target"
            f"{headroom}"
        )
    return None


def utc_now() -> datetime:
    return datetime.now(tz=UTC)
