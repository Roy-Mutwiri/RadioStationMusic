"""SQLAlchemy 2.0 ORM models for every §37 entity.

Layout follows ADR-07's central split, which is the single most consequential
schema decision in the project:

* **Permanent** — ``tracks``, ``track_blueprints``, ``lyrics``,
  ``audio_fingerprints``, ``used_seeds``, ``similarity_results``, ``play_events``.
  Small rows, kept forever. Duplicate prevention (§21) and the §46 library keep
  working for the life of the station.
* **Expendable** — ``track_files``. Audio bytes, addressed only through these
  rows. Retention (§36) deletes files and marks rows ``deleted_at``; nothing above
  notices, because nothing above holds a path.

At ~37 MB per WAV and ~17 tracks/hour, keeping audio forever exhausts this disk in
under two weeks (ADR-07). The split is therefore a correctness requirement, not an
optimisation, and it has to exist before the first track is generated — which is
why it is in Phase 1 rather than Phase 9.

Indexing policy: every column used for a time-ordered sweep or a §12 horizon
lookup is indexed. A week of operation puts hundreds of thousands of rows in
``market_snapshots``, and an unindexed ``ORDER BY timestamp DESC LIMIT 1`` there
becomes the slowest thing in the process.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from tradefix_radio.persistence.types import PortableJson, UtcDateTime

#: Explicit constraint naming so Alembic autogenerate emits stable, droppable
#: names. Without a convention, SQLite invents anonymous constraints that a later
#: migration cannot alter — a problem that only surfaces months in.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every station table."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ============================================================ tracks


class Track(Base):
    """One generated track. The hub of the schema.

    Columns here are denormalised copies of blueprint fields *on purpose*: the §44
    queue panel, §46 library filters and §11 diversity checks all need to filter
    and sort on genre/bpm/key/topic, and doing that through the JSON blueprint
    would mean loading and parsing every row.
    """

    __tablename__ = "tracks"

    track_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(32), index=True)

    title: Mapped[str] = mapped_column(String(160))
    persona_id: Mapped[str | None] = mapped_column(String(48), index=True, default=None)

    genre: Mapped[str] = mapped_column(String(64), index=True)
    secondary_genre: Mapped[str | None] = mapped_column(String(64), default=None)
    bpm: Mapped[int] = mapped_column(Integer, index=True)
    musical_key: Mapped[str] = mapped_column(String(32), index=True)
    duration_seconds: Mapped[float] = mapped_column(Float)

    is_instrumental: Mapped[bool] = mapped_column(Boolean, index=True)
    vocal_style: Mapped[str] = mapped_column(String(32))
    primary_topic: Mapped[str | None] = mapped_column(String(96), index=True, default=None)
    secondary_topic: Mapped[str | None] = mapped_column(String(96), default=None)

    #: Where this track came from; see `TrackProvenance`.
    #:
    #: Defaulted to ``unknown`` rather than to a plausible class. Every row written before
    #: this column existed has an origin nobody recorded, and inventing one would be a
    #: guess baked into the data — the one thing a provenance column must not contain.
    provenance: Mapped[str] = mapped_column(
        String(24), index=True, default="unknown", server_default=text("'unknown'")
    )

    #: Which market the director planned this track against (routing V1).
    #:
    #: Denormalised from the blueprint for the same reason genre and BPM are: the §46
    #: library filters on it, and "all gold tracks" would otherwise mean parsing every
    #: stored blueprint. Indexed because filtering by market is the common case once the
    #: station has run across a weekend.
    #:
    #: Defaulted to XAUUSD rather than left nullable: every row written before routing
    #: existed was planned against gold, and that is a fact about them, not an unknown.
    symbol_at_generation: Mapped[str] = mapped_column(
        String(32), index=True, default="XAUUSD", server_default=text("'XAUUSD'")
    )
    regime_at_generation: Mapped[str] = mapped_column(String(48), index=True)
    #: **Market** energy on 0-100 that prompted the track.
    energy_at_generation: Mapped[float] = mapped_column(Float)
    #: **Composition** intensity on 0-1 that the director chose in response (SS6.7).
    #:
    #: A separate column rather than a reuse of the one above, because they are different
    #: quantities on different scales: one is what the market did, the other is what the
    #: director decided about it. Denormalised here for the same reason genre and BPM are --
    #: blueprint similarity reads it for every track in the comparison window, and parsing
    #: the stored blueprint JSON for each would put a deserialise on the hot path.
    composition_energy: Mapped[float] = mapped_column(
        Float, default=0.0, server_default=text("0")
    )
    session_at_generation: Mapped[str] = mapped_column(String(48))

    seed: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(64))
    model_identifier: Mapped[str] = mapped_column(String(128))
    #: Hash of MusicBlueprintV1.signature_payload(); §11 "same blueprint: never repeat".
    blueprint_signature: Mapped[str] = mapped_column(String(64), index=True)

    novelty_score: Mapped[float | None] = mapped_column(Float, default=None)
    #: Denormalised play statistics so the §12 long horizons avoid an aggregate.
    play_count: Mapped[int] = mapped_column(Integer, default=0)
    last_played_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime, index=True, default=None
    )

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
    #: Why a track ended up REJECTED/QUARANTINED. Shown on the §48 page.
    rejection_reason: Mapped[str | None] = mapped_column(String(400), default=None)

    blueprint: Mapped[TrackBlueprint | None] = relationship(
        back_populates="track", cascade="all, delete-orphan", uselist=False
    )
    files: Mapped[list[TrackFile]] = relationship(
        back_populates="track", cascade="all, delete-orphan"
    )
    lyrics: Mapped[Lyrics | None] = relationship(
        back_populates="track", cascade="all, delete-orphan", uselist=False
    )
    fingerprint: Mapped[AudioFingerprint | None] = relationship(
        back_populates="track", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (
        # The scheduler's hot query: "ready tracks, oldest first".
        Index("ix_tracks_state_created", "state", "created_at"),
        # Retention's sweep query (§36): played, oldest first.
        Index("ix_tracks_state_last_played", "state", "last_played_at"),
    )


class TrackBlueprint(Base):
    """The complete MusicBlueprintV1 as stored JSON.

    §8: "Every generated track MUST have its complete blueprint saved." Stored
    whole rather than exploded into columns so that adding a blueprint field never
    needs a migration, and so a V2 blueprint can coexist with V1 rows —
    ``schema_version`` is what a reader branches on.
    """

    __tablename__ = "track_blueprints"

    track_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tracks.track_id", ondelete="CASCADE"), primary_key=True
    )
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(PortableJson)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)

    track: Mapped[Track] = relationship(back_populates="blueprint")


class TrackFile(Base):
    """A file on disk belonging to a track — the expendable layer (ADR-07).

    ``deleted_at`` rather than row deletion: the station needs to know that a
    master *existed* and was reclaimed, so the §46 library can show "audio
    archived" instead of pretending the track never had any. It also makes the
    retention sweep idempotent and auditable.
    """

    __tablename__ = "track_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    track_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tracks.track_id", ondelete="CASCADE"), index=True
    )
    #: ``raw`` | ``master`` | ``artwork`` | ``station_id``
    role: Mapped[str] = mapped_column(String(24), index=True)
    path: Mapped[str] = mapped_column(String(1024))
    file_format: Mapped[str] = mapped_column(String(16))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sample_rate: Mapped[int | None] = mapped_column(Integer, default=None)
    channels: Mapped[int | None] = mapped_column(Integer, default=None)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    #: Set when the bytes are reclaimed by retention. The row is never removed.
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    #: Protects emergency reserve audio from the sweeper (§36).
    retain_forever: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    track: Mapped[Track] = relationship(back_populates="files")

    __table_args__ = (
        UniqueConstraint("track_id", "role", name="uq_track_files_track_id_role"),
        # The sweeper's query: live files of a role, oldest first.
        Index("ix_track_files_role_deleted_created", "role", "deleted_at", "created_at"),
    )


class Lyrics(Base):
    """Lyrics, retained permanently for §17 duplicate detection."""

    __tablename__ = "lyrics"

    track_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tracks.track_id", ondelete="CASCADE"), primary_key=True
    )
    text: Mapped[str] = mapped_column(Text)
    #: SHA-256 of the normalised word stream; exact-duplicate detection.
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    primary_topic: Mapped[str] = mapped_column(String(96), index=True)
    secondary_topic: Mapped[str | None] = mapped_column(String(96), index=True, default=None)
    lyric_format: Mapped[str] = mapped_column(String(64))
    perspective: Mapped[str] = mapped_column(String(48))
    tradefix_mentions: Mapped[int] = mapped_column(Integer)
    educational_intensity: Mapped[float] = mapped_column(Float)
    word_count: Mapped[int] = mapped_column(Integer)
    #: Word shingles for Jaccard near-duplicate comparison, stored as a JSON list.
    shingles: Mapped[list[str]] = mapped_column(PortableJson, default=list)
    concepts_used: Mapped[list[str]] = mapped_column(PortableJson, default=list)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)

    track: Mapped[Track] = relationship(back_populates="lyrics")

    __table_args__ = (
        # §12: "same primary + secondary topic combination: never intentionally repeat".
        Index("ix_lyrics_topic_pair", "primary_topic", "secondary_topic"),
    )


class AudioFingerprint(Base):
    """Content descriptors retained permanently, even after audio is reclaimed."""

    __tablename__ = "audio_fingerprints"

    track_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tracks.track_id", ondelete="CASCADE"), primary_key=True
    )
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    #: SHA-256 of the *decoded* audio (§6.3), which is what exact-duplicate detection uses.
    #: Distinct from ``file_sha256``, which changes with container and metadata and so would
    #: miss the same audio written twice in different wrappers.
    canonical_sha256: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    #: Which implementation produced ``fingerprint_value`` — ``chromaprint`` or the built-in.
    #: Recorded because fingerprints from different providers are not comparable, and a
    #: library assembled across an upgrade would otherwise mix them silently.
    fingerprint_provider: Mapped[str | None] = mapped_column(String(32), default=None)
    fingerprint_version: Mapped[str | None] = mapped_column(String(48), default=None)
    fingerprint_value: Mapped[str | None] = mapped_column(Text, default=None)
    #: Layout version of ``embedding``. Same reasoning as the provider above.
    embedding_version: Mapped[int | None] = mapped_column(Integer, default=None)
    chroma_mean: Mapped[list[float]] = mapped_column(PortableJson)
    chroma_std: Mapped[list[float]] = mapped_column(PortableJson)
    mfcc_mean: Mapped[list[float]] = mapped_column(PortableJson)
    mfcc_std: Mapped[list[float]] = mapped_column(PortableJson)
    spectral: Mapped[dict[str, float]] = mapped_column(PortableJson, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(PortableJson, default=None)
    tempo: Mapped[float] = mapped_column(Float, index=True)
    musical_key: Mapped[str] = mapped_column(String(32), index=True)
    duration_seconds: Mapped[float] = mapped_column(Float)
    blueprint_signature: Mapped[str] = mapped_column(String(64), index=True)
    lyric_hash: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    computed_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)

    track: Mapped[Track] = relationship(back_populates="fingerprint")


class SimilarityResult(Base):
    """One §22 evaluation. Kept for the §48 dashboard and rejection audit."""

    __tablename__ = "similarity_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    track_id: Mapped[str] = mapped_column(String(64), index=True)
    verdict: Mapped[str] = mapped_column(String(16), index=True)
    novelty_score: Mapped[float] = mapped_column(Float)
    max_similarity: Mapped[float] = mapped_column(Float)
    threshold: Mapped[float] = mapped_column(Float)
    closest_track_id: Mapped[str | None] = mapped_column(String(64), default=None)
    deciding_component: Mapped[str | None] = mapped_column(String(48), default=None)
    components: Mapped[list[dict[str, Any]]] = mapped_column(PortableJson, default=list)
    compared_against: Mapped[int] = mapped_column(Integer, default=0)
    evaluated_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)


class TrackQcResult(Base):
    """One QC pass over one track (SS6.1, SS6.10).

    Two rows per approved track in the normal case: the raw generator output and the mastered
    file. Keeping both is what makes "mastering introduced clipping" a findable fact rather
    than an inference from a single overwritten verdict.
    """

    __tablename__ = "track_qc_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    track_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tracks.track_id", ondelete="CASCADE"), index=True
    )
    #: ``raw`` or ``mastered``.
    stage: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(8), index=True)
    #: Count of each outcome, so the Originality page can aggregate without loading checks.
    passed_count: Mapped[int] = mapped_column(Integer, default=0)
    warned_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    analysis_backend: Mapped[str] = mapped_column(String(16), default="numpy")
    elapsed_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    evaluated_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)

    checks: Mapped[list[TrackQcCheck]] = relationship(
        back_populates="result", cascade="all, delete-orphan"
    )


class TrackQcCheck(Base):
    """One measurement within a QC pass.

    A row per check rather than a JSON blob, because SS6.1 requires every check to carry its
    own status, value and threshold, and because "how often does loudness fail" is a question
    an operator will ask and a column answers.
    """

    __tablename__ = "track_qc_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    result_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("track_qc_results.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(48), index=True)
    status: Mapped[str] = mapped_column(String(8), index=True)
    #: Null when the check could not be performed -- never a stand-in number.
    value: Mapped[float | None] = mapped_column(Float, default=None)
    unit: Mapped[str] = mapped_column(String(24), default="")
    threshold: Mapped[str] = mapped_column(String(96), default="")
    reason: Mapped[str] = mapped_column(Text, default="")

    result: Mapped[TrackQcResult] = relationship(back_populates="checks")


class AudioFeatureRow(Base):
    """Extracted features, in the compact form SS6.2 asks for.

    Separate from ``audio_fingerprints``, which Phase 1 shaped around similarity vectors. This
    holds the measurements QC judged and the Originality page displays.
    """

    __tablename__ = "audio_features"

    track_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tracks.track_id", ondelete="CASCADE"), primary_key=True
    )
    duration_seconds: Mapped[float] = mapped_column(Float)
    sample_rate: Mapped[int] = mapped_column(Integer)
    channels: Mapped[int] = mapped_column(Integer)
    peak: Mapped[float] = mapped_column(Float)
    rms: Mapped[float] = mapped_column(Float)
    crest_factor: Mapped[float] = mapped_column(Float)
    dc_offset: Mapped[float] = mapped_column(Float)
    integrated_lufs: Mapped[float | None] = mapped_column(Float, default=None)
    spectral_centroid: Mapped[float] = mapped_column(Float, default=0.0)
    spectral_bandwidth: Mapped[float] = mapped_column(Float, default=0.0)
    spectral_rolloff: Mapped[float] = mapped_column(Float, default=0.0)
    zero_crossing_rate: Mapped[float] = mapped_column(Float, default=0.0)
    stereo_correlation: Mapped[float] = mapped_column(Float, default=1.0)
    silence_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    clipped_sample_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    tempo: Mapped[float | None] = mapped_column(Float, default=None)
    musical_key: Mapped[str | None] = mapped_column(String(32), default=None)
    rms_profile: Mapped[list[float]] = mapped_column(PortableJson, default=list)
    backend: Mapped[str] = mapped_column(String(16), default="numpy")
    computed_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)


class MasteringResultRow(Base):
    """What mastering did to one track (SS6.9).

    Persisted because "why is this track quieter than that one" is a question with a real
    answer -- the energy-band target -- and because a loudness drift across the library is
    only visible as a trend.
    """

    __tablename__ = "mastering_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    track_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tracks.track_id", ondelete="CASCADE"), index=True
    )
    outcome: Mapped[str] = mapped_column(String(16), index=True)
    target_lufs: Mapped[float] = mapped_column(Float)
    measured_lufs_before: Mapped[float | None] = mapped_column(Float, default=None)
    measured_lufs_after: Mapped[float | None] = mapped_column(Float, default=None)
    true_peak_dbtp: Mapped[float | None] = mapped_column(Float, default=None)
    true_peak_ceiling_dbtp: Mapped[float | None] = mapped_column(Float, default=None)
    #: The target was missed because the limiter reached the ceiling first, not because
    #: normalisation failed. Stored so a quiet master can be explained rather than re-run.
    peak_constrained: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), index=True
    )
    gain_applied_db: Mapped[float | None] = mapped_column(Float, default=None)
    trimmed_start_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    trimmed_end_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    duration_before: Mapped[float] = mapped_column(Float, default=0.0)
    duration_after: Mapped[float] = mapped_column(Float, default=0.0)
    elapsed_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    detail: Mapped[str] = mapped_column(Text, default="")
    mastered_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)


class LyricFingerprintRow(Base):
    """Lexical fingerprint of one lyric (SS6.8).

    The ``lyrics`` table Phase 1 created holds the text and its topic metadata; this holds the
    derived forms comparison runs against, so a comparison never has to re-tokenise every
    historical lyric.
    """

    __tablename__ = "lyric_fingerprints"

    track_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tracks.track_id", ondelete="CASCADE"), primary_key=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    line_hashes: Mapped[list[str]] = mapped_column(PortableJson, default=list)
    shingles: Mapped[list[str]] = mapped_column(PortableJson, default=list)
    hook_hashes: Mapped[list[str]] = mapped_column(PortableJson, default=list)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    unique_word_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    internal_repetition: Mapped[float] = mapped_column(Float, default=0.0)
    #: How many times the station's own name appears (SS21). Counted rather than inferred so
    #: a drift towards every lyric being an advert is visible as a number.
    tradefix_mentions: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    computed_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)


class UsedSeed(Base):
    """§23 seed registry.

    Deliberately documented as metadata: "Seeds are metadata, not proof of
    uniqueness." The registry prevents *intentional* reuse and nothing more.
    """

    __tablename__ = "used_seeds"

    seed: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    track_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    used_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)


# ============================================================ market


class MarketSnapshotRow(Base):
    """Raw observations. The highest-volume table by far."""

    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    bid: Mapped[float] = mapped_column(Float)
    ask: Mapped[float] = mapped_column(Float)
    open: Mapped[float | None] = mapped_column(Float, default=None)
    high: Mapped[float | None] = mapped_column(Float, default=None)
    low: Mapped[float | None] = mapped_column(Float, default=None)
    close: Mapped[float | None] = mapped_column(Float, default=None)
    tick_volume: Mapped[float | None] = mapped_column(Float, default=None)
    #: Marks simulated data so it can never be mistaken for live history (§7).
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    __table_args__ = (
        Index("ix_market_snapshots_symbol_timestamp", "symbol", "timestamp"),
    )


class MarketStateRow(Base):
    """Classified market states, for the §43 timeline and §47 Market Lab."""

    __tablename__ = "market_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    regime: Mapped[str] = mapped_column(String(48), index=True)
    direction: Mapped[str] = mapped_column(String(16))
    session: Mapped[str] = mapped_column(String(48), index=True)
    feed_status: Mapped[str] = mapped_column(String(16), index=True)
    energy: Mapped[float] = mapped_column(Float)
    energy_velocity: Mapped[float] = mapped_column(Float)
    volatility: Mapped[float] = mapped_column(Float)
    trend_strength: Mapped[float] = mapped_column(Float)
    momentum: Mapped[float] = mapped_column(Float)
    compression: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    regime_age_seconds: Mapped[float] = mapped_column(Float)
    #: Full MarketFeaturesV1 for the §47 page, kept out of columns to avoid a
    #: 20-column table that changes shape whenever a feature is added.
    features: Mapped[dict[str, Any] | None] = mapped_column(PortableJson, default=None)

    __table_args__ = (
        Index("ix_market_states_symbol_timestamp", "symbol", "timestamp"),
    )


# ============================================================ generation & queue


class GenerationJob(Base):
    """A unit of generation work, with a lease (§70).

    ``lease_owner`` + ``lease_expires_at`` is the whole §70 answer: a worker claims
    a job by writing its identity and an expiry; a crashed worker's lease simply
    runs out and the job is reclaimed. Nothing needs to detect the crash, which is
    what makes it robust — detection is the part that fails.
    """

    __tablename__ = "generation_jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    track_id: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    priority: Mapped[str] = mapped_column(String(16), index=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    timeout_seconds: Mapped[float] = mapped_column(Float)

    lease_owner: Mapped[str | None] = mapped_column(String(96), index=True, default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime, index=True, default=None
    )

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    generation_seconds: Mapped[float | None] = mapped_column(Float, default=None)
    peak_vram_bytes: Mapped[int | None] = mapped_column(Integer, default=None)

    error_kind: Mapped[str | None] = mapped_column(String(48), index=True, default=None)
    error_message: Mapped[str | None] = mapped_column(String(2000), default=None)
    request_payload: Mapped[dict[str, Any] | None] = mapped_column(PortableJson, default=None)

    __table_args__ = (
        # The claim query: pending jobs by priority then age, with an expired or
        # absent lease.
        Index("ix_generation_jobs_claim", "state", "priority", "created_at"),
        Index("ix_generation_jobs_lease", "lease_owner", "lease_expires_at"),
    )


class QueueItem(Base):
    """A slot in the forward schedule (§27, §28)."""

    __tablename__ = "queue_items"

    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    track_id: Mapped[str] = mapped_column(String(64), index=True)
    position: Mapped[int] = mapped_column(Integer, index=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    lock_level: Mapped[str] = mapped_column(String(24), index=True)
    tier: Mapped[str] = mapped_column(String(24), index=True)

    transition_in: Mapped[str] = mapped_column(String(32))
    transition_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    generation_progress: Mapped[float] = mapped_column(Float, default=0.0)

    enqueued_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    #: Denormalised display fields so the §44 panel needs no joins.
    display: Mapped[dict[str, Any]] = mapped_column(PortableJson, default=dict)

    __table_args__ = (
        Index("ix_queue_items_state_position", "state", "position"),
    )


class StateTransitionRow(Base):
    """Audit trail of every §27 state change.

    §27 requires persisting all state transitions. Kept in its own table rather
    than an append-only JSON column on ``tracks`` so it can be queried
    ("how many tracks failed in MASTERING this week?") without reading every track.
    """

    __tablename__ = "state_transitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    track_id: Mapped[str] = mapped_column(String(64), index=True)
    from_state: Mapped[str | None] = mapped_column(String(32), default=None)
    to_state: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(String(96), index=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(PortableJson, default=None)
    at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)

    __table_args__ = (
        Index("ix_state_transitions_track_at", "track_id", "at"),
    )


class PlayEvent(Base):
    """An airing record (§37).

    ``completed`` is persisted separately from the track state so §75's rule —
    never mark an incomplete track as successfully played — is verifiable after the
    fact, not just intended.
    """

    __tablename__ = "play_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    track_id: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    played_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    tier: Mapped[str] = mapped_column(String(24), index=True)
    transition_in: Mapped[str] = mapped_column(String(32))
    end_reason: Mapped[str | None] = mapped_column(String(48), default=None)
    #: Regime at airing time, which may differ from regime at generation — useful
    #: for judging whether programming actually tracked the market.
    regime_at_play: Mapped[str | None] = mapped_column(String(48), default=None)
    #: Active market at airing time. Nullable, unlike the track's own symbol: there may
    #: genuinely be no active market when a track airs — the station keeps broadcasting
    #: from its buffer — and recording "XAUUSD" then would be an invention.
    symbol_at_play: Mapped[str | None] = mapped_column(String(32), index=True, default=None)


# ============================================================ content libraries


class Topic(Base):
    """A §14 trading topic.

    ``certainty`` is the load-bearing column. §14 forbids presenting uncertain
    market relationships as guaranteed, so every concept carries how confidently it
    may be stated, and the lyric validators read it.
    """

    __tablename__ = "topics"

    key: Mapped[str] = mapped_column(String(96), primary_key=True)
    category: Mapped[str] = mapped_column(String(48), index=True)
    label: Mapped[str] = mapped_column(String(160))
    #: ``established`` | ``contextual`` | ``speculative``
    certainty: Mapped[str] = mapped_column(String(24), index=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    #: Teaching points and phrasing guards, loaded from topics.yaml.
    detail: Mapped[dict[str, Any]] = mapped_column(PortableJson, default=dict)
    last_used_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime, index=True, default=None
    )
    use_count: Mapped[int] = mapped_column(Integer, default=0)


class StationId(Base):
    """A §31 station identifier with its own anti-repeat history."""

    __tablename__ = "station_ids"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    text: Mapped[str] = mapped_column(String(400))
    audio_path: Mapped[str | None] = mapped_column(String(1024), default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    #: ``generic`` | ``high_energy`` | ``quiet`` | ``session_open`` …
    context: Mapped[str] = mapped_column(String(48), default="generic", index=True)
    play_count: Mapped[int] = mapped_column(Integer, default=0)
    last_played_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime, index=True, default=None
    )


class TrackTitle(Base):
    """Title history for §99's uniqueness and similarity checks.

    Separate from ``tracks`` because titles must be checked against *all* titles
    ever used, including those of rejected candidates — a rejected track still
    consumed its title idea, and reusing it would make the station repeat itself in
    the one place listeners notice most.
    """

    __tablename__ = "track_titles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(160), index=True)
    #: Lowercased, punctuation-stripped form used for comparison.
    normalised: Mapped[str] = mapped_column(String(160), index=True)
    track_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)


# ============================================================ station memory


class RadioMemory(Base):
    """§96 creative memory — key/value state that must survive a restart.

    §96 is explicit: "Restarting the application should NOT reset creative memory
    and cause immediate repeats." Rotation counters, last-used timestamps per
    genre/topic/persona, and historical distributions live here.

    A key/value table rather than typed columns because the set of remembered
    things grows with the director, and each addition would otherwise be a
    migration.
    """

    __tablename__ = "radio_memory"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(PortableJson)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)


class SettingOverride(Base):
    """Runtime settings changed through ``PATCH /api/settings`` (§38, §50).

    Overrides are stored rather than written back into YAML: editing the operator's
    config file from a web request would destroy comments and make the file's
    provenance unclear. These layer above YAML and below environment variables.
    Secret-valued keys are rejected by the API — secrets stay in the environment.
    """

    __tablename__ = "setting_overrides"

    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    value: Mapped[Any] = mapped_column(PortableJson)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_by: Mapped[str] = mapped_column(String(64), default="operator")


# ============================================================ observability


class SystemEvent(Base):
    """Durable record of notable events (§37, §55).

    Not a replacement for log files — it holds the subset an operator queries from
    the UI: failures, fallbacks, restarts, rejections. Log files stay the full
    record and rotate.
    """

    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    service: Mapped[str] = mapped_column(String(32), index=True)
    level: Mapped[str] = mapped_column(String(16), index=True)
    event: Mapped[str] = mapped_column(String(96), index=True)
    track_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    job_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    market_regime: Mapped[str | None] = mapped_column(String(48), default=None)
    duration_seconds: Mapped[float | None] = mapped_column(Float, default=None)
    error: Mapped[str | None] = mapped_column(String(2000), default=None)
    detail: Mapped[dict[str, Any] | None] = mapped_column(PortableJson, default=None)

    __table_args__ = (
        Index("ix_system_events_level_at", "level", "at"),
    )


class HealthEvent(Base):
    """Health transitions (§35), so degradation history is reviewable."""

    __tablename__ = "health_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    component: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    previous_status: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str] = mapped_column(String(500), default="")
    measurements: Mapped[dict[str, float] | None] = mapped_column(PortableJson, default=None)


class MetricSample(Base):
    """Time series for the §56 metric list.

    A generic name/value/labels shape rather than a column per metric: §56 lists
    two dozen metrics and that list will grow, and a wide table would need a
    migration for each one.
    """

    __tablename__ = "metric_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    name: Mapped[str] = mapped_column(String(96), index=True)
    value: Mapped[float] = mapped_column(Float)
    labels: Mapped[dict[str, str] | None] = mapped_column(PortableJson, default=None)

    __table_args__ = (
        Index("ix_metric_samples_name_at", "name", "at"),
    )


__all__ = [
    "AudioFingerprint",
    "Base",
    "GenerationJob",
    "HealthEvent",
    "Lyrics",
    "MarketSnapshotRow",
    "MarketStateRow",
    "MetricSample",
    "PlayEvent",
    "QueueItem",
    "RadioMemory",
    "SettingOverride",
    "SimilarityResult",
    "StateTransitionRow",
    "StationId",
    "SystemEvent",
    "Topic",
    "Track",
    "TrackBlueprint",
    "TrackFile",
    "TrackTitle",
    "UsedSeed",
]
