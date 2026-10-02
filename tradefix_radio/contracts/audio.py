"""Audio analysis, fingerprint, mastering and originality contracts (§21–§25).

A naming note that is load-bearing: nothing in this module is called
``originality_proof``, ``copyright_safe``, or similar. §21 and §86 explicitly
forbid claiming that fingerprints guarantee uniqueness. The vocabulary used
throughout is *duplication prevention* and *novelty score* — an internal,
threshold-based judgement about this station's own library.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, computed_field, field_validator

from tradefix_radio.contracts.base import Contract
from tradefix_radio.contracts.enums import NoveltyVerdict
from tradefix_radio.contracts.market import Unit


class AudioMetricsV1(Contract):
    """Objective measurements of a rendered file (§24)."""

    duration_seconds: float = Field(ge=0.0)
    sample_rate: int = Field(gt=0)
    channels: int = Field(ge=1, le=8)

    peak_dbfs: float
    true_peak_dbtp: float
    #: Integrated loudness, LUFS. Negative in all real material.
    loudness_lufs: float
    #: Loudness range. The §25 guard against crushed dynamics lives on this.
    loudness_range_lu: float = Field(ge=0.0)
    rms_dbfs: float
    #: Mean DC offset across channels, in linear amplitude.
    dc_offset: float

    #: Fraction of samples at or beyond full scale.
    clipped_sample_ratio: Unit
    #: Fraction of the file below the silence threshold.
    silence_ratio: Unit
    #: Leading/trailing silence, used by the §25 trim step.
    leading_silence_seconds: float = Field(ge=0.0)
    trailing_silence_seconds: float = Field(ge=0.0)

    detected_bpm: float | None = Field(default=None, gt=0.0)
    detected_key: str | None = Field(default=None, max_length=32)


class QualityIssueV1(Contract):
    """One §24 defect."""

    code: str = Field(min_length=1, max_length=48)
    message: str = Field(min_length=1, max_length=400)
    fatal: bool = True
    measured: float | None = None
    threshold: float | None = None


class TrackAnalysisV1(Contract):
    """Result of analysing a generated file (§24).

    Carries both the measurements and the verdict, so the §46 track detail page
    can show *why* something passed as well as that it did.
    """

    track_id: str = Field(min_length=1, max_length=64)
    audio_path: str = Field(min_length=1, max_length=1024)
    metrics: AudioMetricsV1
    passed: bool
    issues: tuple[QualityIssueV1, ...] = Field(default_factory=tuple)
    analyzed_at: datetime

    @field_validator("analyzed_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("analyzed_at must be timezone-aware (UTC)")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fatal_issues(self) -> tuple[QualityIssueV1, ...]:
        return tuple(issue for issue in self.issues if issue.fatal)


class AudioFingerprintV1(Contract):
    """Content-derived descriptors retained permanently (§21, §36).

    Retained even after the audio bytes are deleted, which is the entire point of
    ADR-07: duplication checks must keep working for the life of the station
    without keeping every WAV forever.

    Vectors are stored as plain float tuples rather than a binary blob so they
    round-trip identically through SQLite and PostgreSQL (ADR-05) and stay
    inspectable on the §48 page.
    """

    track_id: str = Field(min_length=1, max_length=64)
    #: SHA-256 of the mastered file bytes. Catches exact duplicates only.
    file_sha256: str = Field(min_length=64, max_length=64)

    #: Mean chroma energy per pitch class — 12 values, harmonic signature.
    chroma_mean: tuple[float, ...] = Field(min_length=12, max_length=12)
    #: Chroma variance, distinguishes static pads from moving harmony.
    chroma_std: tuple[float, ...] = Field(min_length=12, max_length=12)
    #: MFCC means — timbral signature. Width is configurable, hence a range.
    mfcc_mean: tuple[float, ...] = Field(min_length=8, max_length=40)
    mfcc_std: tuple[float, ...] = Field(min_length=8, max_length=40)
    #: Spectral centroid / rolloff / bandwidth / flatness / zero-crossing means.
    spectral: dict[str, float] = Field(default_factory=dict)

    tempo: float = Field(gt=0.0)
    key: str = Field(min_length=1, max_length=32)
    duration_seconds: float = Field(gt=0.0)

    #: Blueprint signature, so creative-duplicate checks need no audio at all.
    blueprint_signature: str = Field(min_length=64, max_length=64)
    #: Lyric hash when the track has words.
    lyric_hash: str | None = Field(default=None, min_length=64, max_length=64)
    #: Provider embedding when one is available. Optional by design: §22 weights
    #: it when present and redistributes to the other components when absent.
    embedding: tuple[float, ...] | None = None

    computed_at: datetime

    @field_validator("computed_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("computed_at must be timezone-aware (UTC)")
        return value

    @field_validator("mfcc_std")
    @classmethod
    def _mfcc_widths_agree(cls, value: tuple[float, ...], info: object) -> tuple[float, ...]:
        data = getattr(info, "data", {})
        mean = data.get("mfcc_mean")
        if mean is not None and len(mean) != len(value):
            raise ValueError(
                f"mfcc_std width ({len(value)}) must match mfcc_mean ({len(mean)})"
            )
        return value


class SimilarityComponentV1(Contract):
    """One weighted term of the §22 novelty score."""

    name: str = Field(min_length=1, max_length=48)
    similarity: Unit
    weight: Unit
    #: ``False`` when the input was unavailable (e.g. no embedding, instrumental
    #: track has no lyrics). Weight is redistributed across available components
    #: so a missing input cannot silently inflate novelty.
    available: bool = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def weighted(self) -> float:
        return self.similarity * self.weight if self.available else 0.0


class SimilarityResultV1(Contract):
    """Comparison of a candidate against the library (§22, §48).

    Explicitly includes ``threshold`` and ``closest_track_id`` so the §48 page can
    render the required explanation verbatim, e.g.::

        Rejected: audio embedding similarity = 0.91
        closest track = TF-20261001-00917
        threshold = 0.84
    """

    track_id: str = Field(min_length=1, max_length=64)
    verdict: NoveltyVerdict
    #: 1.0 means maximally unlike anything in the library.
    novelty_score: Unit
    #: The single highest similarity observed, across all components and tracks.
    max_similarity: Unit
    threshold: Unit
    closest_track_id: str | None = Field(default=None, max_length=64)
    #: Which component drove the decision — the headline on the §48 page.
    deciding_component: str | None = Field(default=None, max_length=48)
    components: tuple[SimilarityComponentV1, ...] = Field(default_factory=tuple)
    compared_against: int = Field(ge=0)
    evaluated_at: datetime

    @field_validator("evaluated_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware (UTC)")
        return value

    @property
    def explanation(self) -> str:
        """Operator-facing one-liner, worded to avoid overclaiming (§21)."""
        if self.verdict is NoveltyVerdict.APPROVE:
            return (
                f"Approved: novelty {self.novelty_score:.2f}, highest internal "
                f"similarity {self.max_similarity:.2f} (threshold {self.threshold:.2f}), "
                f"compared against {self.compared_against} tracks."
            )
        component = self.deciding_component or "combined"
        closest = self.closest_track_id or "unknown"
        return (
            f"{self.verdict.value.capitalize()}: {component} similarity "
            f"{self.max_similarity:.2f} vs closest track {closest}, "
            f"threshold {self.threshold:.2f}."
        )


class MasteringResultV1(Contract):
    """Outcome of the §25 pipeline."""

    track_id: str = Field(min_length=1, max_length=64)
    source_path: str = Field(min_length=1, max_length=1024)
    output_path: str = Field(min_length=1, max_length=1024)
    #: Ordered list of stages actually applied, for reproducibility.
    stages_applied: tuple[str, ...] = Field(default_factory=tuple)
    before: AudioMetricsV1
    after: AudioMetricsV1
    target_lufs: float
    true_peak_ceiling_dbtp: float
    mastered_at: datetime
    elapsed_seconds: float = Field(ge=0.0)

    @field_validator("mastered_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("mastered_at must be timezone-aware (UTC)")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def loudness_error_lu(self) -> float:
        """Signed miss against the loudness target. Asserted on in §6.5 tests."""
        return self.after.loudness_lufs - self.target_lufs

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dynamics_retained_ratio(self) -> float:
        """Post/pre loudness range — the §25 guard against over-compression.

        A value near 1.0 means dynamics survived; a small value means the limiter
        flattened the track, which the brief explicitly warns against.
        """
        if self.before.loudness_range_lu <= 0:
            return 1.0
        return self.after.loudness_range_lu / self.before.loudness_range_lu


__all__ = [
    "AudioFingerprintV1",
    "AudioMetricsV1",
    "MasteringResultV1",
    "QualityIssueV1",
    "SimilarityComponentV1",
    "SimilarityResultV1",
    "TrackAnalysisV1",
]
