"""Response models for the Control Center API.

These are a **boundary**, not a re-export. The runtime's own contracts
(`MarketStateV1`, `QueueEntry`, `BufferAssessment`, …) are internal shapes that exist to serve
the station; serving them raw would make every future change to a domain model a breaking
change for the browser, and would leak things the UI has no business with — absolute file
paths among them, which §68 forbids exposing unnecessarily.

So every field here is chosen, named for a reader rather than for the runtime, and derived in
one place (`snapshot.py`). Three rules shape the design:

**Nothing optional is faked.** A figure the runtime cannot supply is ``None`` and the UI
renders its absence. There is no zero-as-unknown anywhere in this file, because a zero renders
as a confident "0" and is therefore a lie — §86's "no static fake metrics in production UI"
starts at the serialiser, not at the component.

**Derived figures are computed server-side.** "Starts in 4:12", "critical in ~31 min",
"+6.2 min/hour" are all computed here. A browser recomputing them from a timestamp would drift
against the station's own clock, and two surfaces disagreeing about how long until dead air is
worse than either being slightly stale.

**Status is never colour.** Every health-ish field carries an explicit enum plus a sentence, so
the UI never has to infer severity from a number — and so a screen reader gets the same
information a colour conveys.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ActiveMarketV1",
    "AlertV1",
    "AudioFeaturesV1",
    "BufferV1",
    "CapabilityV1",
    "EmergencyV1",
    "FingerprintV1",
    "GenerationHealthV1",
    "GenerationJobV1",
    "HealthComponentV1",
    "LiveStateV1",
    "LyricFingerprintV1",
    "MarketAvailabilityV1",
    "MarketPointV1",
    "MarketV1",
    "MasteringV1",
    "NowPlayingV1",
    "OriginalityResultV1",
    "OriginalitySummaryV1",
    "ProgrammingReasonV1",
    "ProviderStatusV1",
    "QcCheckV1",
    "QcResultV1",
    "QueueItemV1",
    "ReviewResolutionV1",
    "SimilarityComponentV1",
    "StationStatusV1",
    "SystemResourcesV1",
    "TrackEvidenceV1",
    "TrackSummaryV1",
]


class _Dto(BaseModel):
    """Frozen, strict, camel-free. The browser gets exactly these keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")


HealthState = Literal["healthy", "degraded", "critical", "recovering", "offline"]
BufferLevelName = Literal["healthy", "low", "critical", "empty"]
TrajectoryName = Literal["filling", "holding", "draining", "stalled"]
TierName = Literal["scheduled", "emergency_reserve", "procedural"]
LockName = Literal["locked", "semi_locked", "replaceable", "operator_pinned"]


# ----------------------------------------------------------------- capability


class CapabilityV1(_Dto):
    """Whether a subsystem exists, and what to say when it does not."""

    capability: str
    state: Literal["ready", "degraded", "planned", "unavailable"]
    detail: str
    arrives_in_phase: int | None = None


# ----------------------------------------------------------------- market


class MarketV1(_Dto):
    """XAUUSD as the station currently understands it.

    ``price`` is ``None`` when the feed has not supplied one, and the UI must render that as
    "no price" rather than as zero. §21 is categorical: a price may only be displayed from a
    validated, non-stale market state, so an absent price is a *required* outcome, not an edge
    case to paper over.
    """

    symbol: str
    timestamp: datetime
    price: float | None = None
    price_change: float | None = None
    price_change_percent: float | None = None

    regime: str
    regime_label: str
    regime_confidence: float = Field(ge=0.0, le=1.0)
    regime_age_seconds: float = Field(ge=0.0)
    direction: str
    session: str

    energy: float = Field(ge=0.0, le=100.0)
    energy_velocity: float
    volatility: float = Field(ge=0.0, le=100.0)
    trend_strength: float = Field(ge=0.0, le=100.0)
    momentum: float = Field(ge=0.0, le=100.0)
    compression: float = Field(ge=0.0, le=100.0)

    feed_status: str
    data_age_seconds: float = Field(ge=0.0)
    #: True when the state is too old to show a price against (§21).
    is_stale: bool
    #: Set when the feed is simulated, so the UI can never present it as live (§72).
    is_simulated: bool


class MarketAvailabilityV1(_Dto):
    """One configured symbol's availability, as the Market page renders it.

    ``state`` and ``feed_degraded`` are deliberately separate fields rather than one
    combined status. The requirement they serve is that an operator can tell a closed
    market from a broken feed at a glance; collapsing them into a single badge is exactly
    the confusion the routing subsystem exists to prevent — the station treats the two
    differently, so the UI must show them differently.
    """

    symbol: str
    #: ``open`` | ``closed`` | ``stale`` | ``unavailable`` | ``unknown``.
    state: str
    #: A sentence naming *why*, e.g. "no tick for 412s" or "feed reports the market closed".
    reason: str
    #: True when the data path looks broken rather than the market being shut. A closed
    #: market is not degraded; a silent feed on a trading day is.
    feed_degraded: bool
    #: ``None`` before the first tick — never zero, which would read as "fresh".
    data_age_seconds: float | None = None
    feed_status: str
    #: What the calendar says, independently of what the feed is doing. ``None`` when no
    #: calendar applies (a 24/7 instrument).
    calendar_open: bool | None = None
    is_active: bool
    bars_processed: int = Field(ge=0)
    #: Subject to the same §21 rule as ``MarketV1.price``: absent rather than stale.
    last_price: float | None = None
    assessed_at: datetime


class ActiveMarketV1(_Dto):
    """Which market the station is planning against, and why.

    Present on every live frame because the answer changes the meaning of everything beside
    it: an energy reading of 80 is a different statement about gold than about Bitcoin.
    """

    #: The live symbol, or ``NO_ACTIVE_MARKET`` when neither is usable. The sentinel is
    #: surfaced rather than translated to ``None`` so the UI can say so explicitly instead
    #: of rendering a blank where a symbol belongs.
    active_symbol: str
    primary_symbol: str
    is_primary: bool
    #: False when ``active_symbol`` is the sentinel. The station keeps broadcasting from its
    #: buffer; only live-data planning stops.
    has_active_market: bool
    active_since: datetime | None = None
    #: Why the station is on this symbol, e.g. ``primary_market_closed``.
    switch_reason: str | None = None
    switch_count: int = Field(ge=0)
    #: A switch that is being *confirmed* but has not happened yet — the hysteresis window.
    #: Shown so an operator watching a closure does not think the station is stuck.
    pending_symbol: str | None = None
    pending_seconds_remaining: float | None = None
    symbols: tuple[MarketAvailabilityV1, ...] = ()


class MarketPointV1(_Dto):
    """One sample on the energy timeline."""

    at: datetime
    market_energy: float
    #: The station's own energy, which follows the market rather than tracking it exactly.
    radio_energy: float | None = None
    regime: str
    price: float | None = None


# ----------------------------------------------------------------- now playing


class ProgrammingReasonV1(_Dto):
    """Why this track, in terms the operator can check.

    Assembled from the blueprint's **stored** rationale and its recorded market context —
    deterministic factors the director wrote down when it decided. There is no model
    introspection here and there must not be: the brief says to show explicit system factors
    and stored decision metadata only, never hidden chain-of-thought.
    """

    #: One line per factor, in the director's own words, e.g. "genre uk_trap".
    factors: tuple[str, ...] = ()
    market_regime: str
    #: Both 0–100. The blueprint stores intensity as 0–1; it is scaled at the boundary.
    market_energy: float
    target_energy: float
    #: Diversity pressure the director was acting under, when it recorded one.
    diversity_note: str | None = None
    creative_temperature: float | None = None


class NowPlayingV1(_Dto):
    """What is on air, and everything the dashboard shows about it.

    ``elapsed_seconds`` comes from the playout engine's frame counter, not from a timestamp
    difference — the engine counts frames written to the sink, so the figure cannot drift
    against the audio the way a clock-derived one would.
    """

    track_id: str
    title: str
    artist: str | None = None
    tier: TierName
    is_station_id: bool

    genre: str | None = None
    secondary_genre: str | None = None
    bpm: int | None = None
    musical_key: str | None = None
    is_instrumental: bool | None = None
    vocal_style: str | None = None

    duration_seconds: float = Field(gt=0.0)
    elapsed_seconds: float = Field(ge=0.0)
    remaining_seconds: float = Field(ge=0.0)
    progress: float = Field(ge=0.0, le=1.0)

    planned_regime: str | None = None
    #: Which market this track was planned against.
    #:
    #: Carried per-track rather than read off the header, because after a switch the two
    #: disagree for as long as the queue still holds programming from the previous market —
    #: and that gap is the thing an operator most needs to see, not the thing to paper over.
    planned_symbol: str | None = None
    planned_energy: float | None = None
    novelty_target: float | None = None
    transition_in: str | None = None
    #: Which tier supplied it: the queue, the reserve, or the synthesiser.
    origin: str

    started_at: datetime | None = None
    reason: ProgrammingReasonV1 | None = None
    #: Peak sample of the block currently going out. Real amplitude, for an honest meter.
    output_peak: float | None = Field(default=None, ge=0.0)


# ----------------------------------------------------------------- queue


class QueueItemV1(_Dto):
    """One upcoming slot."""

    position: int = Field(ge=0)
    track_id: str
    title: str
    artist: str | None = None
    genre: str | None = None
    bpm: int | None = None
    #: 0–100, normalised from the blueprint's 0–1 intensity so the UI has one energy scale.
    energy: float | None = Field(default=None, ge=0.0, le=100.0)
    planned_regime: str | None = None
    #: Which market this slot was planned against. See ``NowPlayingV1.planned_symbol``.
    planned_symbol: str | None = None

    lock: LockName
    lock_label: Literal["HARD", "SOFT", "FLEXIBLE", "PINNED"]
    lock_reason: str | None = None
    #: Whether the operator may replace or remove this slot. Mirrors §28, computed here so the
    #: UI never has to re-derive the rule and never has to guess.
    is_protected: bool

    readiness: Literal["pending", "ready", "unavailable"]
    generation_state: str
    generation_progress: float | None = Field(default=None, ge=0.0, le=1.0)

    duration_seconds: float = Field(ge=0.0)
    #: Seconds until this slot starts, from the queue ahead of it plus what is on air now.
    starts_in_seconds: float | None = Field(default=None, ge=0.0)


class BufferV1(_Dto):
    """How much audio the station has, and which way that is going.

    The figure that matters is ``seconds_to_failure``, and it is deliberately ``None`` unless
    the buffer is actually shrinking — a countdown that is always present stops being read.
    """

    level: BufferLevelName
    trajectory: TrajectoryName
    reason: str

    ready_minutes: float = Field(ge=0.0)
    pending_minutes: float = Field(ge=0.0)
    minimum_minutes: float = Field(gt=0.0)
    target_minutes: float = Field(gt=0.0)
    maximum_minutes: float = Field(gt=0.0)
    fill_ratio: float = Field(ge=0.0)

    ready_tracks: int = Field(ge=0)
    pending_tracks: int = Field(ge=0)

    #: §93's ratio: audio generated per second of wall time.
    capacity_ratio: float = Field(ge=0.0)
    #: Buffer minutes gained or lost per hour at the current rate. Negative means draining.
    trend_minutes_per_hour: float
    seconds_to_failure: float | None = Field(default=None, ge=0.0)


class EmergencyV1(_Dto):
    """Which tier is carrying the output (§33)."""

    tier: TierName
    tier_label: Literal["NORMAL", "TIER 2 RESERVE", "TIER 3 PROCEDURAL"]
    is_degraded: bool
    reserve_minutes: float = Field(ge=0.0)
    reserve_tracks_remaining: int = Field(ge=0)
    tier2_activations: int = Field(ge=0)
    tier3_activations: int = Field(ge=0)
    seconds_in_tier2: float = Field(ge=0.0)
    seconds_in_tier3: float = Field(ge=0.0)
    recoveries: int = Field(ge=0)
    last_reason: str | None = None


# ----------------------------------------------------------------- health


class HealthComponentV1(_Dto):
    """One row of the System Health panel.

    ``detail`` is not decoration. The brief's standard is that an operator must not have to
    read logs to understand a basic problem, so a degraded component has to say what is wrong
    *and* whether the broadcast is at risk — "2 timeouts in last 10 min, buffer remains safe".
    """

    name: str
    label: str
    state: HealthState
    detail: str
    #: Shown beside the state when there is a number worth seeing.
    metric: str | None = None
    checked_at: datetime | None = None


class GenerationHealthV1(_Dto):
    """§93's figures, which drive both the buffer prediction and the risk controller."""

    provider: str
    capacity_ratio: float = Field(ge=0.0)
    latency_p50_seconds: float | None = Field(default=None, ge=0.0)
    latency_p95_seconds: float | None = Field(default=None, ge=0.0)
    samples: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    retries: int = Field(ge=0)
    timeouts: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    in_flight: int = Field(ge=0)
    pending_leases: int = Field(ge=0)
    #: Mean audio seconds per completed job, for reading the ratio in context.
    mean_audio_seconds: float | None = Field(default=None, ge=0.0)


class GenerationJobV1(_Dto):
    """One row of the job monitor."""

    job_id: str
    track_id: str
    state: str
    priority: str
    provider: str
    attempt: int = Field(ge=1)
    max_attempts: int = Field(ge=1)
    age_seconds: float = Field(ge=0.0)
    lease_owner: str | None = None
    lease_expires_in_seconds: float | None = None
    generation_seconds: float | None = Field(default=None, ge=0.0)
    error_kind: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class SystemResourcesV1(_Dto):
    """The engineering cockpit's numbers.

    Every GPU field is ``None`` without one, rather than zero — a 0 °C GPU reads as a working
    sensor, and §86 forbids presenting a fabricated figure as real state.
    """

    cpu_percent: float | None = Field(default=None, ge=0.0)
    memory_used_mb: float | None = Field(default=None, ge=0.0)
    memory_total_mb: float | None = Field(default=None, ge=0.0)
    disk_free_gb: float | None = Field(default=None, ge=0.0)
    disk_total_gb: float | None = Field(default=None, ge=0.0)

    gpu_name: str | None = None
    gpu_percent: float | None = Field(default=None, ge=0.0)
    vram_used_mb: float | None = Field(default=None, ge=0.0)
    vram_total_mb: float | None = Field(default=None, ge=0.0)
    gpu_temperature_c: float | None = None

    process_uptime_seconds: float = Field(ge=0.0)
    process_memory_mb: float = Field(ge=0.0)
    event_loop_lag_ms: float | None = Field(default=None, ge=0.0)
    active_tasks: int = Field(ge=0)
    database_size_mb: float | None = Field(default=None, ge=0.0)


# ----------------------------------------------------------------- station


class AlertV1(_Dto):
    """Something the operator should look at."""

    key: str
    severity: Literal["info", "warning", "critical"]
    message: str
    raised_at: datetime
    #: What to do about it, when there is a clear answer.
    remediation: str | None = None


class StationStatusV1(_Dto):
    """The top bar, and the answer to "is it broadcasting?"."""

    is_broadcasting: bool
    #: ``playing`` / ``fallback`` / ``silent`` / ``stopped`` — what the engine is doing.
    playout_state: str
    #: Operator mute: the engine keeps running and writes zeroed blocks to the sink.
    muted: bool = False
    #: Operator volume, linear gain 0..1 applied before the sink.
    volume: float = Field(default=1.0, ge=0.0, le=1.0)
    station_name: str = "TRADE FIX RADIO"
    tagline: str = "THE MARKET COMPOSES THE RADIO"
    version: str
    environment: str
    mode: str
    #: True when this process is an interactive test run with lowered buffer targets.
    #:
    #: A persistent chip rather than something tucked into a panel, for the same reason the
    #: simulation badge is: a 12-minute buffer target where production uses 45 is not wrong,
    #: but it is only not-wrong if the reader knows why.
    test_mode: bool = False
    started_at: datetime | None = None
    uptime_seconds: float = Field(ge=0.0)

    tracks_played: int = Field(ge=0)
    tracks_generated: int = Field(ge=0)
    seconds_on_air: float = Field(ge=0.0)
    unintended_silence_seconds: float = Field(ge=0.0)
    underruns: int = Field(ge=0)
    #: Fraction of broadcast time covered by audio — the honest continuity metric (ADR-12).
    audio_coverage: float | None = Field(default=None, ge=0.0, le=1.0)


class TrackSummaryV1(_Dto):
    """A library row."""

    track_id: str
    title: str
    artist: str | None = None
    genre: str | None = None
    secondary_genre: str | None = None
    bpm: int | None = None
    musical_key: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0.0)
    is_instrumental: bool | None = None
    state: str
    planned_regime: str | None = None
    #: Market the track was planned against, from ``tracks.symbol_at_generation``.
    planned_symbol: str | None = None
    planned_energy: float | None = None
    novelty_score: float | None = None
    play_count: int = Field(ge=0)
    created_at: datetime | None = None
    last_played_at: datetime | None = None


class LiveStateV1(_Dto):
    """One coherent frame of live state, as pushed over the WebSocket.

    Sent whole rather than as a dozen independent streams because the pieces are only
    meaningful together: a buffer reading of four minutes means something different beside a
    healthy generator than beside a dead one, and two sockets racing would let the dashboard
    show that pair inconsistently. The frequently-changing part — audio position — is a
    separate, smaller message, so a progress tick does not re-send the whole station.
    """

    status: StationStatusV1
    market: MarketV1 | None = None
    #: Which symbol ``market`` describes, and the other symbols' health. ``None`` only when
    #: no routing subsystem is attached (a bare view in tests).
    routing: ActiveMarketV1 | None = None
    now_playing: NowPlayingV1 | None = None
    queue: tuple[QueueItemV1, ...] = ()
    buffer: BufferV1 | None = None
    emergency: EmergencyV1 | None = None
    generation: GenerationHealthV1 | None = None
    health: tuple[HealthComponentV1, ...] = ()
    alerts: tuple[AlertV1, ...] = ()
    capabilities: tuple[CapabilityV1, ...] = ()
    at: datetime


# ---------------------------------------------------------------- originality


class ReviewResolutionV1(_Dto):
    """How a REVIEW candidate was resolved, and on what evidence.

    Carries the initial verdict beside the final disposition on purpose: an approval that
    hides having begun as REVIEW is the silent reinterpretation the second stage was
    built to replace.
    """

    initial_verdict: str
    disposition: str
    evidence_class: str
    reason: str
    resolver_version: str
    #: Fingerprint agreement — the only signal calibrated against real duplicates.
    duplication_risk: float = Field(ge=0.0, le=1.0)
    #: Timbre, tempo, key and structure. Drives rotation, never duplication.
    creative_similarity: float = Field(ge=0.0, le=1.0)
    closest_track_id: str | None = None
    production_references: int = Field(default=0, ge=0)


class QcCheckV1(_Dto):
    """One QC measurement (§6.1).

    Every field here exists because §6.1 requires a check to report more than a verdict: the
    measured value, the threshold it was held to, and a sentence saying what that means. A
    status alone would tell an operator that something failed and nothing about what to do.
    """

    name: str
    status: Literal["pass", "warn", "fail"]
    #: ``None`` when the check could not be performed — never a stand-in number.
    value: float | None = None
    unit: str = ""
    threshold: str = ""
    reason: str = ""


class QcResultV1(_Dto):
    """One QC pass over one file."""

    stage: Literal["raw", "mastered"]
    status: Literal["pass", "warn", "fail"]
    summary: str
    passed_count: int = Field(ge=0)
    warned_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    analysis_backend: str
    elapsed_seconds: float = Field(ge=0.0)
    evaluated_at: datetime
    checks: tuple[QcCheckV1, ...] = ()


class SimilarityComponentV1(_Dto):
    """One comparison against one library track, with its parts intact (§6.5).

    The components are sent individually rather than collapsed, because the brief is explicit
    that one scalar must not be treated as absolute truth — and because "0.86 overall" and
    "0.86 overall, all of it lyrics" call for different operator responses.
    """

    track_id: str
    score: float = Field(ge=0.0, le=1.0)
    components: dict[str, float] = Field(default_factory=dict)
    is_exact_audio: bool = False
    is_exact_lyrics: bool = False
    blueprint_threshold: float | None = None
    blueprint_is_recent: bool | None = None
    detail: str = ""


class OriginalityResultV1(_Dto):
    """The originality verdict for one track."""

    track_id: str
    verdict: Literal["approve", "review", "reject"]
    novelty_score: float = Field(ge=0.0, le=1.0)
    max_similarity: float = Field(ge=0.0, le=1.0)
    threshold: float = Field(ge=0.0, le=1.0)
    closest_track_id: str | None = None
    deciding_component: str | None = None
    compared_against: int = Field(ge=0)
    comparisons: tuple[SimilarityComponentV1, ...] = ()
    evaluated_at: datetime


class MasteringV1(_Dto):
    """What mastering did to one track (§6.9)."""

    outcome: Literal["mastered", "skipped", "failed"]
    target_lufs: float
    measured_lufs_before: float | None = None
    measured_lufs_after: float | None = None
    true_peak_dbtp: float | None = None
    true_peak_ceiling_dbtp: float | None = None
    #: The target was missed because the limiter hit the ceiling, not because anything failed.
    peak_constrained: bool = False
    gain_applied_db: float | None = None
    trimmed_seconds: float = Field(default=0.0, ge=0.0)
    duration_after: float | None = Field(default=None, ge=0.0)
    elapsed_seconds: float = Field(default=0.0, ge=0.0)
    detail: str = ""
    mastered_at: datetime


class AudioFeaturesV1(_Dto):
    """The measured character of one file (§6.2)."""

    duration_seconds: float = Field(ge=0.0)
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    peak: float
    rms: float
    crest_factor: float
    integrated_lufs: float | None = None
    spectral_centroid: float | None = None
    tempo: float | None = None
    musical_key: str | None = None
    silence_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    clipped_sample_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    backend: str
    computed_at: datetime


class FingerprintV1(_Dto):
    """Identity hashes and which implementation produced them (§6.4).

    The provider is part of the payload because fingerprints from different providers are not
    comparable, and because the built-in fallback is weaker than Chromaprint. A UI that showed
    a fingerprint without saying which kind would imply a guarantee that does not exist.
    """

    canonical_sha256: str | None = None
    file_sha256: str | None = None
    provider: str | None = None
    provider_version: str | None = None
    embedding_version: int | None = None
    tempo: float | None = None
    musical_key: str | None = None
    blueprint_signature: str | None = None
    computed_at: datetime | None = None


class LyricFingerprintV1(_Dto):
    """Derived lyric statistics (§6.8)."""

    content_hash: str
    word_count: int = Field(ge=0)
    unique_word_ratio: float = Field(ge=0.0, le=1.0)
    internal_repetition: float = Field(ge=0.0, le=1.0)
    tradefix_mentions: int = Field(default=0, ge=0)
    line_count: int = Field(ge=0)
    computed_at: datetime


class TrackEvidenceV1(_Dto):
    """Everything recorded about one track's trip through post-production.

    Returned as one document because the Originality page shows the chain, not a verdict: the
    question an operator arrives with is "why did this not make it", and that answer spans QC,
    similarity and mastering together.
    """

    track_id: str
    qc_results: tuple[QcResultV1, ...] = ()
    features: AudioFeaturesV1 | None = None
    originality: OriginalityResultV1 | None = None
    mastering: MasteringV1 | None = None
    fingerprint: FingerprintV1 | None = None
    lyrics: LyricFingerprintV1 | None = None


class OriginalitySummaryV1(_Dto):
    """The Originality page's header row.

    ``novelty_histogram`` is ten buckets spanning 0–1. Sent as counts rather than percentages
    so a chart with four tracks in it cannot be mistaken for one with four thousand.
    """

    library_size: int = Field(ge=0)
    evaluated_count: int = Field(ge=0)
    verdict_counts: dict[str, int] = Field(default_factory=dict)
    novelty_histogram: tuple[int, ...] = ()
    #: What the built-in similarity actually rests on, stated rather than implied.
    fingerprint_provider: str
    fingerprint_detail: str
    #: §86: the station never claims a track has never existed before.
    scope_note: str

    # -- the two questions, kept apart on the page as well (B2) ------------
    #
    # One novelty number could not say both "this is not a copy" and "this sounds like
    # the last thing we played", and the station spent an hour on procedural audio partly
    # because nobody could see the difference.

    #: How many PRODUCTION_RADIO tracks exist to compare against.
    production_references: int = Field(default=0, ge=0)
    #: True when nothing has aired yet, so graded novelty has no basis.
    #:
    #: Surfaced explicitly because the alternative is a 100% approval rate that looks like
    #: quality and is actually an empty library. §86 forbids exactly that kind of
    #: impressive-looking metric.
    cold_start: bool = False
    #: Dispositions of candidates that entered REVIEW, by the evidence that decided them.
    resolution_counts: dict[str, int] = Field(default_factory=dict)
    evidence_counts: dict[str, int] = Field(default_factory=dict)
    #: Which resolver policy produced the stored dispositions. ``None`` before any ran.
    resolver_version: str | None = None


# ------------------------------------------------------------------ provider


class ProviderStatusV1(_Dto):
    """Live generation-provider state (§7.24).

    Every numeric field is optional, and that is load-bearing rather than defensive. A
    `latency_p95_seconds` of 0.0 before any track has been generated renders as "instant",
    which is the fabricated metric §86 forbids; `null` renders as absent. The same reasoning
    governs every VRAM field — a station with no GPU reports nothing, not zero.
    """

    provider: str
    #: `unavailable` | `loading` | `ready` | `generating` | `unloading` | `failed`
    status: str
    model: str
    lm_model: str | None = None
    version: str | None = None
    loaded: bool
    load_seconds: float | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    generations: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)
    oom_events: int = Field(default=0, ge=0)
    latency_p50_seconds: float | None = None
    latency_p95_seconds: float | None = None
    current_track_id: str | None = None
    #: Seconds the in-flight generation has been running. `null` when nothing is running.
    current_elapsed_seconds: float | None = None
    #: VRAM in MB, as measured. Absent on a host with no GPU.
    vram_total_mb: float | None = None
    vram_used_mb: float | None = None
    vram_free_mb: float | None = None
    peak_vram_mb: float | None = None
    gpu_temperature_c: float | None = None
    #: §7.25: ACE-Step reports pending/done and nothing between, so a percentage would be
    #: invented. The UI renders an indeterminate state instead.
    supports_progress: bool = False
