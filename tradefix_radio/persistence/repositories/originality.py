"""Persistence for the post-production pipeline (§6.3–§6.10, §6.12).

Two jobs that pull in opposite directions, kept in one place because they share a table:

**Writing evidence.** Every approval and every rejection records the numbers behind it — the
individual QC checks, the extracted features, what mastering did, which tracks the candidate
was compared against. §6.1 and §6.5 both insist the components survive, not just the verdict,
because "why was this rejected" is the question the Originality page exists to answer and a
single Boolean cannot answer it.

**Reading the library.** The similarity engine needs every comparable track as a
:class:`LibraryEntry`, and it needs them fast enough to run inside the generation deadline.
That is a wide read over a growing table, so it is deliberately narrow in columns and ordered
newest-first — §6.5's staged search shortlists on an embedding matrix, and the entries it never
shortlists are never touched again.

Why the library read is bounded
-------------------------------
`load_library` takes a limit and applies one by default. An unbounded read is fine at a hundred
tracks and is a multi-second stall at fifty thousand, which would land inside the generation
deadline and manifest as dead air rather than as a slow query. The bound is on *recency*, which
is also where repetition matters most (§6.7); the exact-duplicate hash check is a separate
indexed lookup that remains exhaustive, so nothing older than the window can slip through as a
byte-identical copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import delete, func, select

from tradefix_radio.audio.qc import QcStatus
from tradefix_radio.originality.blueprint import BlueprintSummary
from tradefix_radio.persistence.models import (
    AudioFeatureRow,
    AudioFingerprint,
    LyricFingerprintRow,
    MasteringResultRow,
    SimilarityResult,
    Track,
    TrackQcCheck,
    TrackQcResult,
)
from tradefix_radio.persistence.repositories.base import Repository, affected_rows

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from tradefix_radio.audio.analysis import AudioFeatures
    from tradefix_radio.audio.fingerprint import AudioFingerprint as FingerprintValue
    from tradefix_radio.audio.mastering import MasteringResult
    from tradefix_radio.audio.qc import TrackQcResult as QcResult
    from tradefix_radio.originality.lyrics import LyricFingerprint
    from tradefix_radio.originality.similarity import LibraryEntry, SimilarityOutcome

__all__ = [
    "DEFAULT_LIBRARY_LIMIT",
    "OriginalityRepository",
    "StoredQcCheck",
    "StoredQcResult",
    "TrackEvidence",
]

#: How many recent tracks the similarity engine compares against by default.
#:
#: 2 000. At the measured per-entry cost this keeps the vectorised shortlist stage in the low
#: milliseconds, and it spans weeks of a station producing a track every few minutes — far
#: beyond §6.7's six-hour recency window, which is where repetition is actually audible.
DEFAULT_LIBRARY_LIMIT: Final = 2_000


@dataclass(frozen=True)
class StoredQcCheck:
    """One persisted check, in the shape the API returns."""

    name: str
    status: str
    value: float | None
    unit: str
    threshold: str
    reason: str


@dataclass(frozen=True)
class StoredQcResult:
    """One persisted QC pass with its checks."""

    stage: str
    status: str
    summary: str
    passed_count: int
    warned_count: int
    failed_count: int
    analysis_backend: str
    elapsed_seconds: float
    evaluated_at: datetime
    checks: tuple[StoredQcCheck, ...]


@dataclass(frozen=True)
class TrackEvidence:
    """Everything recorded about one track's trip through the pipeline.

    Assembled for the Originality page, which shows the whole chain rather than the verdict:
    an operator asking why a track was rejected needs the check that failed and the track it
    resembled, not a status word.
    """

    track_id: str
    qc_results: tuple[StoredQcResult, ...]
    features: dict[str, Any] | None
    mastering: dict[str, Any] | None
    similarity: dict[str, Any] | None
    fingerprint: dict[str, Any] | None
    lyrics: dict[str, Any] | None


class OriginalityRepository(Repository):
    """Reads and writes everything the post-production pipeline produces.

    Like every repository here it never commits; the caller owns the transaction so that a
    track's QC result, features, mastering record and state transition land together or not at
    all. A half-written evidence trail is worse than none — it reads as though a stage ran.
    """

    # ---------------------------------------------------------------- writes

    async def record_qc(
        self,
        track_id: str,
        result: QcResult,
        *,
        elapsed_seconds: float = 0.0,
        evaluated_at: datetime,
    ) -> int:
        """Persist one QC pass and each of its checks.

        Appended rather than replaced. §6.1's rule that a raw pass and a mastered pass are
        different questions means both must survive; overwriting would leave the record
        claiming the mastered file passed checks that were in fact run on the raw one.
        """
        row = TrackQcResult(
            track_id=track_id,
            stage=result.stage.value,
            status=result.status.value,
            passed_count=sum(1 for check in result.checks if check.status is QcStatus.PASS),
            warned_count=len(result.warnings),
            failed_count=len(result.failures),
            summary=result.summary(),
            # `features` is optional on the QC result because a check set can be built
            # without one in tests; the backend is then simply unknown rather than guessed.
            analysis_backend="unknown" if result.features is None else result.features.backend,
            elapsed_seconds=elapsed_seconds,
            evaluated_at=evaluated_at,
        )
        row.checks = [
            TrackQcCheck(
                name=check.name,
                status=check.status.value,
                value=check.value,
                unit=check.unit,
                threshold=check.threshold,
                reason=check.reason,
            )
            for check in result.checks
        ]
        self._session.add(row)
        # Flushed because the caller may want the id, and because a constraint violation
        # should surface here rather than at an unrelated commit later.
        await self._session.flush()
        return int(row.id)

    async def record_features(
        self, track_id: str, features: AudioFeatures, *, computed_at: datetime
    ) -> None:
        """Persist the compact feature summary (§6.2).

        Upserted by primary key: a track re-analysed after a re-master has one current set of
        features, and keeping both would make "what does this track measure" ambiguous. The
        *judgements* are what accumulate, not the measurements.
        """
        existing = await self._session.get(AudioFeatureRow, track_id)
        values = {
            "duration_seconds": features.duration_seconds,
            "sample_rate": features.sample_rate,
            "channels": features.channels,
            "peak": features.peak,
            "rms": features.rms,
            "crest_factor": features.crest_factor,
            "dc_offset": features.dc_offset,
            "integrated_lufs": features.integrated_lufs,
            "spectral_centroid": features.spectral_centroid,
            "spectral_bandwidth": features.spectral_bandwidth,
            "spectral_rolloff": features.spectral_rolloff,
            "zero_crossing_rate": features.zero_crossing_rate,
            "stereo_correlation": features.stereo_correlation,
            "silence_ratio": features.silence_ratio,
            "clipped_sample_ratio": features.clipped_sample_ratio,
            "tempo": features.tempo,
            "musical_key": features.musical_key,
            "rms_profile": list(features.rms_profile),
            "backend": features.backend,
            "computed_at": computed_at,
        }
        if existing is None:
            self._session.add(AudioFeatureRow(track_id=track_id, **values))
        else:
            for name, value in values.items():
                setattr(existing, name, value)

    async def record_fingerprint(
        self,
        track_id: str,
        *,
        features: AudioFeatures,
        canonical_hash: str,
        file_sha256: str,
        fingerprint: FingerprintValue | None,
        blueprint_signature: str,
        lyric_hash: str | None,
        embedding_version: int,
        computed_at: datetime,
    ) -> None:
        """Persist the comparison vectors and hashes (§6.3, §6.4).

        One row per track, upserted. This is what `load_library` reads back, so the embedding
        stored here and the embedding the engine computes must come from the same code — hence
        ``embedding_version``, which makes a mismatch detectable instead of silently producing
        cosines between incompatible layouts.
        """
        existing = await self._session.get(AudioFingerprint, track_id)
        values: dict[str, Any] = {
            "file_sha256": file_sha256,
            "canonical_sha256": canonical_hash,
            "fingerprint_provider": None if fingerprint is None else fingerprint.provider,
            "fingerprint_version": (
                None if fingerprint is None else fingerprint.provider_version
            ),
            "fingerprint_value": None if fingerprint is None else fingerprint.fingerprint,
            "embedding_version": embedding_version,
            "chroma_mean": list(features.chroma_mean),
            "chroma_std": list(features.chroma_std),
            "mfcc_mean": list(features.mfcc_mean),
            "mfcc_std": list(features.mfcc_std),
            "spectral": {
                "centroid": features.spectral_centroid,
                "bandwidth": features.spectral_bandwidth,
                "rolloff": features.spectral_rolloff,
                "zero_crossing_rate": features.zero_crossing_rate,
            },
            "embedding": [float(value) for value in features.embedding()],
            "tempo": features.tempo or 0.0,
            "musical_key": features.musical_key or "unknown",
            "duration_seconds": features.duration_seconds,
            "blueprint_signature": blueprint_signature,
            "lyric_hash": lyric_hash,
            "computed_at": computed_at,
        }
        if existing is None:
            self._session.add(AudioFingerprint(track_id=track_id, **values))
        else:
            for name, value in values.items():
                setattr(existing, name, value)

    async def record_mastering(
        self, track_id: str, result: MasteringResult, *, mastered_at: datetime
    ) -> None:
        """Persist what mastering did (§6.9)."""
        self._session.add(
            MasteringResultRow(
                track_id=track_id,
                outcome=result.outcome.value,
                target_lufs=result.target_lufs,
                measured_lufs_before=result.measured_lufs_before,
                measured_lufs_after=result.measured_lufs_after,
                true_peak_dbtp=result.true_peak_dbtp,
                true_peak_ceiling_dbtp=result.true_peak_ceiling_dbtp,
                peak_constrained=result.peak_constrained,
                gain_applied_db=result.gain_applied_db,
                trimmed_start_seconds=result.trimmed_start_seconds,
                trimmed_end_seconds=result.trimmed_end_seconds,
                duration_before=result.duration_before,
                duration_after=result.duration_after,
                elapsed_seconds=result.elapsed_seconds,
                detail=result.detail,
                mastered_at=mastered_at,
            )
        )

    async def record_similarity(
        self, track_id: str, outcome: SimilarityOutcome, *, evaluated_at: datetime
    ) -> None:
        """Persist the originality evaluation with its component breakdown (§6.5).

        The per-comparison components are stored, not just the winning score. §6.5 is explicit
        that one scalar must not be treated as absolute truth, and an operator disputing a
        rejection needs to see *which* component drove it.
        """
        self._session.add(
            SimilarityResult(
                track_id=track_id,
                verdict=outcome.verdict.value,
                novelty_score=outcome.novelty_score,
                max_similarity=outcome.max_similarity,
                threshold=outcome.threshold,
                closest_track_id=(
                    None if outcome.closest is None else outcome.closest.existing_track_id
                ),
                deciding_component=outcome.deciding_component,
                components=[
                    {
                        "track_id": comparison.existing_track_id,
                        "score": comparison.score,
                        "components": comparison.components.as_dict(),
                        "is_exact_audio": comparison.is_exact_audio,
                        "is_exact_lyrics": comparison.is_exact_lyrics,
                        "blueprint_threshold": comparison.blueprint_threshold,
                        "blueprint_is_recent": comparison.blueprint_is_recent,
                        "detail": comparison.detail,
                    }
                    for comparison in outcome.top_comparisons
                ],
                compared_against=outcome.compared_against,
                evaluated_at=evaluated_at,
            )
        )

    async def record_lyric_fingerprint(
        self, fingerprint: LyricFingerprint, *, computed_at: datetime
    ) -> None:
        """Persist the derived lyric forms (§6.8), upserted by track."""
        existing = await self._session.get(LyricFingerprintRow, fingerprint.track_id)
        values: dict[str, Any] = {
            "content_hash": fingerprint.content_hash,
            "line_hashes": sorted(fingerprint.line_hashes),
            "shingles": sorted(fingerprint.shingles),
            "hook_hashes": sorted(fingerprint.hook_hashes),
            "word_count": fingerprint.word_count,
            "unique_word_ratio": fingerprint.unique_word_ratio,
            "internal_repetition": fingerprint.internal_repetition,
            "tradefix_mentions": fingerprint.tradefix_mentions,
            "computed_at": computed_at,
        }
        if existing is None:
            self._session.add(
                LyricFingerprintRow(track_id=fingerprint.track_id, **values)
            )
        else:
            for name, value in values.items():
                setattr(existing, name, value)

    # ----------------------------------------------------------------- reads

    async def canonical_hash_owner(self, canonical_hash: str) -> str | None:
        """Which track already has this exact audio, if any (§6.3).

        An indexed point lookup over the whole table rather than over the loaded library
        window, so exact duplication is caught regardless of age. This is the one originality
        check that stays exhaustive, because it is the one that is cheap.
        """
        statement = (
            select(AudioFingerprint.track_id)
            .where(AudioFingerprint.canonical_sha256 == canonical_hash)
            .limit(1)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def load_library(
        self, *, limit: int = DEFAULT_LIBRARY_LIMIT, exclude_track_id: str | None = None
    ) -> list[LibraryEntry]:
        """The comparable corpus, newest first.

        Lyrics are joined in because §6.8's comparison is part of the same evaluation and a
        second round-trip per candidate would double the query cost for no benefit. Blueprint
        summaries are *not* loaded here — they live on the blueprint rows and are passed in by
        the caller, which already holds them.
        """
        from tradefix_radio.originality.similarity import LibraryEntry  # noqa: PLC0415

        statement = (
            select(AudioFingerprint, LyricFingerprintRow, Track)
            .join(
                LyricFingerprintRow,
                LyricFingerprintRow.track_id == AudioFingerprint.track_id,
                isouter=True,
            )
            .join(Track, Track.track_id == AudioFingerprint.track_id, isouter=True)
            .order_by(AudioFingerprint.computed_at.desc())
            .limit(limit)
        )
        if exclude_track_id is not None:
            statement = statement.where(AudioFingerprint.track_id != exclude_track_id)

        entries: list[LibraryEntry] = []
        for fingerprint_row, lyric_row, track_row in (
            await self._session.execute(statement)
        ).all():
            entries.append(
                LibraryEntry(
                    track_id=fingerprint_row.track_id,
                    canonical_hash=fingerprint_row.canonical_sha256 or "",
                    embedding=tuple(fingerprint_row.embedding or ()),
                    chroma_mean=tuple(fingerprint_row.chroma_mean or ()),
                    mfcc_mean=tuple(fingerprint_row.mfcc_mean or ()),
                    tempo=fingerprint_row.tempo,
                    fingerprint=_rehydrate_fingerprint(fingerprint_row),
                    lyrics=_rehydrate_lyrics(lyric_row),
                    blueprint=_rehydrate_blueprint(track_row, fingerprint_row),
                    created_at=fingerprint_row.computed_at,
                    # Production when the join found no track row: a fingerprint whose
                    # track has been deleted still deserves the stricter treatment.
                    provenance=(
                        getattr(track_row, "provenance", None) or "production_radio"
                    ),
                )
            )
        return entries

    async def library_size(self) -> int:
        """How many tracks are comparable at all. Shown on the Originality page."""
        statement = select(func.count()).select_from(AudioFingerprint)
        return int((await self._session.execute(statement)).scalar_one())

    async def evidence_for(self, track_id: str) -> TrackEvidence:
        """Everything recorded about one track, for the Originality detail view."""
        qc_statement = (
            select(TrackQcResult)
            .where(TrackQcResult.track_id == track_id)
            .order_by(TrackQcResult.evaluated_at.asc(), TrackQcResult.id.asc())
        )
        qc_rows = list((await self._session.scalars(qc_statement)).all())
        qc_results: list[StoredQcResult] = []
        for row in qc_rows:
            check_statement = (
                select(TrackQcCheck)
                .where(TrackQcCheck.result_id == row.id)
                .order_by(TrackQcCheck.id.asc())
            )
            checks = list((await self._session.scalars(check_statement)).all())
            qc_results.append(
                StoredQcResult(
                    stage=row.stage,
                    status=row.status,
                    summary=row.summary,
                    passed_count=row.passed_count,
                    warned_count=row.warned_count,
                    failed_count=row.failed_count,
                    analysis_backend=row.analysis_backend,
                    elapsed_seconds=row.elapsed_seconds,
                    evaluated_at=row.evaluated_at,
                    checks=tuple(
                        StoredQcCheck(
                            name=check.name,
                            status=check.status,
                            value=check.value,
                            unit=check.unit,
                            threshold=check.threshold,
                            reason=check.reason,
                        )
                        for check in checks
                    ),
                )
            )

        feature_row = await self._session.get(AudioFeatureRow, track_id)
        fingerprint_row = await self._session.get(AudioFingerprint, track_id)
        lyric_row = await self._session.get(LyricFingerprintRow, track_id)

        mastering_statement = (
            select(MasteringResultRow)
            .where(MasteringResultRow.track_id == track_id)
            .order_by(MasteringResultRow.mastered_at.desc(), MasteringResultRow.id.desc())
            .limit(1)
        )
        mastering_row = (await self._session.scalars(mastering_statement)).first()

        similarity_statement = (
            select(SimilarityResult)
            .where(SimilarityResult.track_id == track_id)
            .order_by(SimilarityResult.evaluated_at.desc(), SimilarityResult.id.desc())
            .limit(1)
        )
        similarity_row = (await self._session.scalars(similarity_statement)).first()

        return TrackEvidence(
            track_id=track_id,
            qc_results=tuple(qc_results),
            features=_feature_payload(feature_row),
            mastering=_mastering_payload(mastering_row),
            similarity=_similarity_payload(similarity_row),
            fingerprint=_fingerprint_payload(fingerprint_row),
            lyrics=_lyric_payload(lyric_row),
        )

    async def recent_evaluations(self, *, limit: int = 50) -> Sequence[SimilarityResult]:
        """The most recent originality verdicts, for the Originality page's list."""
        statement = (
            select(SimilarityResult)
            .order_by(SimilarityResult.evaluated_at.desc(), SimilarityResult.id.desc())
            .limit(limit)
        )
        return list((await self._session.scalars(statement)).all())

    async def verdict_counts(self) -> dict[str, int]:
        """How many tracks reached each verdict. The Originality page's summary row."""
        statement = select(SimilarityResult.verdict, func.count()).group_by(
            SimilarityResult.verdict
        )
        return {
            str(verdict): int(count)
            for verdict, count in (await self._session.execute(statement)).all()
        }

    async def novelty_distribution(self, *, buckets: int = 10) -> list[int]:
        """Novelty scores binned 0–1, for the distribution chart (§6.16).

        Binned in Python rather than SQL because the expression differs between SQLite and
        PostgreSQL and the row count here is small enough that it does not matter.
        """
        scores = list(
            (await self._session.scalars(select(SimilarityResult.novelty_score))).all()
        )
        histogram = [0] * buckets
        for score in scores:
            index = min(buckets - 1, max(0, int(float(score) * buckets)))
            histogram[index] += 1
        return histogram

    async def purge_track(self, track_id: str) -> int:
        """Remove every evidence row for one track. Used by fixtures and by §6.26's CLI.

        Returns the number of rows removed, so a caller can tell a real deletion from a no-op
        rather than assuming one happened.
        """
        removed = 0
        qc_ids = list(
            (
                await self._session.scalars(
                    select(TrackQcResult.id).where(TrackQcResult.track_id == track_id)
                )
            ).all()
        )
        if qc_ids:
            removed += affected_rows(
                await self._session.execute(
                    delete(TrackQcCheck).where(TrackQcCheck.result_id.in_(qc_ids))
                )
            )
        for model in (
            TrackQcResult,
            AudioFeatureRow,
            MasteringResultRow,
            SimilarityResult,
            LyricFingerprintRow,
            AudioFingerprint,
        ):
            removed += affected_rows(
                await self._session.execute(
                    delete(model).where(model.track_id == track_id)
                )
            )
        return removed


def _rehydrate_fingerprint(row: AudioFingerprint) -> FingerprintValue | None:
    """Rebuild the stored perceptual fingerprint.

    Without this the engine compared every historical track with its fingerprint component
    absent, and the weight that component would have carried was redistributed across the
    remaining ones — including the timbre comparison, which saturates on material from a
    single generator. The data was being written and then ignored.

    No ``vector`` is stored, and none needs to be. The builtin publishes none by design
    (its vector duplicates the embedding), and Chromaprint's comparison reads the stored
    signature directly — `AudioFingerprint.similarity_to` compares those packed integers
    bit-wise, which is both the correct measure and the reason the raw string is worth
    keeping. A separate vector column would store the same numbers twice.
    """
    if row.fingerprint_provider is None or row.fingerprint_value is None:
        return None
    from tradefix_radio.audio.fingerprint import AudioFingerprint as Value  # noqa: PLC0415

    return Value(
        provider=row.fingerprint_provider,
        provider_version=row.fingerprint_version or "",
        fingerprint=row.fingerprint_value,
        duration_seconds=row.duration_seconds,
    )


def _rehydrate_blueprint(
    track: Track | None, fingerprint: AudioFingerprint
) -> BlueprintSummary | None:
    """Rebuild the comparable blueprint fields from the denormalised track row (§6.7).

    From ``tracks`` rather than by parsing ``track_blueprints``: the columns needed are
    already denormalised there for exactly this kind of query, and deserialising a JSON
    blueprint for every entry in the comparison window would put that cost inside the
    generation deadline.

    Returns ``None`` when the track row is missing — a fingerprint without a track is a
    retention artefact, and inventing a blueprint for it would mean comparing a candidate
    against a creative decision nobody made.
    """
    if track is None:
        return None
    return BlueprintSummary(
        track_id=track.track_id,
        signature=track.blueprint_signature or fingerprint.blueprint_signature,
        genre=track.genre,
        secondary_genre=track.secondary_genre,
        bpm=track.bpm,
        musical_key=track.musical_key,
        duration_seconds=int(track.duration_seconds),
        energy=track.composition_energy,
        is_instrumental=track.is_instrumental,
        vocal_style=None if track.is_instrumental else track.vocal_style,
        primary_topic=track.primary_topic,
        secondary_topic=track.secondary_topic,
        persona_id=track.persona_id,
        created_at=track.created_at,
    )


def _rehydrate_lyrics(row: LyricFingerprintRow | None) -> LyricFingerprint | None:
    if row is None:
        return None
    from tradefix_radio.originality.lyrics import LyricFingerprint  # noqa: PLC0415

    return LyricFingerprint(
        track_id=row.track_id,
        content_hash=row.content_hash,
        line_hashes=frozenset(row.line_hashes or ()),
        shingles=frozenset(row.shingles or ()),
        hook_hashes=frozenset(row.hook_hashes or ()),
        word_count=row.word_count,
        unique_word_ratio=row.unique_word_ratio,
        internal_repetition=row.internal_repetition,
        tradefix_mentions=row.tradefix_mentions,
    )


def _feature_payload(row: AudioFeatureRow | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "duration_seconds": row.duration_seconds,
        "sample_rate": row.sample_rate,
        "channels": row.channels,
        "peak": row.peak,
        "rms": row.rms,
        "crest_factor": row.crest_factor,
        "integrated_lufs": row.integrated_lufs,
        "spectral_centroid": row.spectral_centroid,
        "tempo": row.tempo,
        "musical_key": row.musical_key,
        "silence_ratio": row.silence_ratio,
        "clipped_sample_ratio": row.clipped_sample_ratio,
        "backend": row.backend,
        "computed_at": row.computed_at,
    }


def _mastering_payload(row: MasteringResultRow | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "outcome": row.outcome,
        "target_lufs": row.target_lufs,
        "measured_lufs_before": row.measured_lufs_before,
        "measured_lufs_after": row.measured_lufs_after,
        "true_peak_dbtp": row.true_peak_dbtp,
        "true_peak_ceiling_dbtp": row.true_peak_ceiling_dbtp,
        "peak_constrained": row.peak_constrained,
        "gain_applied_db": row.gain_applied_db,
        "trimmed_seconds": row.trimmed_start_seconds + row.trimmed_end_seconds,
        "duration_after": row.duration_after,
        "elapsed_seconds": row.elapsed_seconds,
        "detail": row.detail,
        "mastered_at": row.mastered_at,
    }


def _similarity_payload(row: SimilarityResult | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "verdict": row.verdict,
        "novelty_score": row.novelty_score,
        "max_similarity": row.max_similarity,
        "threshold": row.threshold,
        "closest_track_id": row.closest_track_id,
        "deciding_component": row.deciding_component,
        "comparisons": list(row.components or ()),
        "compared_against": row.compared_against,
        "evaluated_at": row.evaluated_at,
    }


def _fingerprint_payload(row: AudioFingerprint | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "canonical_sha256": row.canonical_sha256,
        "file_sha256": row.file_sha256,
        "provider": row.fingerprint_provider,
        "provider_version": row.fingerprint_version,
        "embedding_version": row.embedding_version,
        "tempo": row.tempo,
        "musical_key": row.musical_key,
        "blueprint_signature": row.blueprint_signature,
        "computed_at": row.computed_at,
    }


def _lyric_payload(row: LyricFingerprintRow | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "content_hash": row.content_hash,
        "word_count": row.word_count,
        "unique_word_ratio": row.unique_word_ratio,
        "internal_repetition": row.internal_repetition,
        "tradefix_mentions": row.tradefix_mentions,
        "line_count": len(row.line_hashes or ()),
        "computed_at": row.computed_at,
    }
