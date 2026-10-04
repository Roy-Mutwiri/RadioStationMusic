"""Runtime state → API DTOs. The only place that conversion happens.

Centralised for one reason: **the station must not learn about the UI.** Phase 4's runtime is
accepted and proven, and the instruction for Phase 5 is explicit — the UI adapts to the backend
contracts, not the reverse. So nothing in `radio/`, `generation/` or `market/` gained a method,
a field or a callback for the Control Center's benefit. Every figure below is read through
accessors the station already exposed, and every derived value — "starts in 4:12",
"-12.4 min/hour", "critical in ~31 min" — is computed here.

That has a pleasant consequence for correctness: a snapshot is a pure function of the runtime's
public surface at one instant, which makes it testable against a station built in a fixture and
impossible to get subtly out of step with a second surface computing the same thing differently.

**Read-only, and that is enforced by what this module imports.** Nothing here can mutate the
station: it takes the objects, reads properties, and returns pydantic models. The operator
controls that *do* mutate live state are in `routes/radio.py`, separately, where they are
few, named, and individually justified.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

from tradefix_radio import __version__ as package_version
from tradefix_radio.api.capabilities import Capability, CapabilityReport
from tradefix_radio.api.dto import (
    ActiveMarketV1,
    AlertV1,
    BufferV1,
    CapabilityV1,
    EmergencyV1,
    GenerationHealthV1,
    GenerationJobV1,
    HealthComponentV1,
    LiveStateV1,
    MarketAvailabilityV1,
    MarketV1,
    NowPlayingV1,
    ProgrammingReasonV1,
    QueueItemV1,
    StationStatusV1,
)
from tradefix_radio.contracts.enums import PlayoutTier
from tradefix_radio.contracts.queue import QueueLockLevel
from tradefix_radio.core.clock import UTC

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradefix_radio.contracts.market import MarketStateV1
    from tradefix_radio.contracts.music import MusicBlueprintV1
    from tradefix_radio.market.active import ActiveMarketService
    from tradefix_radio.persistence.repositories.jobs import JobRecord
    from tradefix_radio.radio.buffer import BufferAssessment
    from tradefix_radio.radio.queue import QueueEntry

__all__ = ["RuntimeView", "build_live_state", "job_to_dto", "routing_to_dto"]

#: Seconds of silence from the feed after which a price must not be shown (§21).
#:
#: Not a display preference. §21 forbids presenting a price that is not from a validated,
#: current market state, and the only safe reading of "current" is a bounded age.
STALE_FEED_SECONDS: Final = 30.0

#: Human labels for the lock levels, so the UI shows §28's vocabulary rather than enum names.
_LOCK_LABELS: Final[dict[QueueLockLevel, str]] = {
    QueueLockLevel.LOCKED: "HARD",
    QueueLockLevel.SEMI_LOCKED: "SOFT",
    QueueLockLevel.REPLACEABLE: "FLEXIBLE",
    QueueLockLevel.OPERATOR_PINNED: "PINNED",
}

_TIER_LABELS: Final[dict[PlayoutTier, str]] = {
    PlayoutTier.SCHEDULED: "NORMAL",
    PlayoutTier.EMERGENCY_RESERVE: "TIER 2 RESERVE",
    PlayoutTier.PROCEDURAL: "TIER 3 PROCEDURAL",
}

_TIER_ORIGINS: Final[dict[PlayoutTier, str]] = {
    PlayoutTier.SCHEDULED: "Scheduled queue",
    PlayoutTier.EMERGENCY_RESERVE: "Emergency reserve (Tier 2)",
    PlayoutTier.PROCEDURAL: "Procedural synthesis (Tier 3)",
}


def _package_version() -> str:
    """The installed package version. One source of truth, not a settings field."""
    return package_version


def _titlecase(value: str) -> str:
    """``bullish_breakout`` → ``Bullish Breakout``, for badges."""
    return value.replace("_", " ").title()


@dataclass
class RuntimeView:
    """Everything the API is allowed to see, and nothing more.

    A deliberate narrowing. The API is handed this rather than the application's object graph,
    so a route cannot reach into the database, cancel a job or start a generation by accident —
    the two controls that *do* act on the station are explicit methods on the runtime objects,
    called from one module.
    """

    settings: object
    station: object | None = None
    market_service: object | None = None
    #: The `ActiveMarketService` when market routing is wired, which is every real run.
    #:
    #: Held separately from ``market_service`` even though the runner currently passes the
    #: same object for both: ``market_service`` is "whatever supplies the active state" and
    #: predates routing, while this one answers "which markets exist and how are they".
    #: Keeping them distinct means a deployment with a single hard-wired feed still works.
    routing: object | None = None
    generation: object | None = None
    health: object | None = None
    database: object | None = None
    capabilities: dict[Capability, CapabilityReport] = field(default_factory=dict)
    started_at: datetime | None = None
    process_started_monotonic: float = field(default_factory=time.monotonic)
    #: Rolling market history for the energy timeline. Bounded; see ``record_market``.
    market_history: list[tuple[datetime, float, float | None, str, float | None]] = field(
        default_factory=list
    )
    #: Last price seen, so a change can be reported without the feed having to supply one.
    _previous_price: float | None = None

    #: How many timeline samples to keep. 4 hours at one sample every 5 s.
    history_limit: int = 2_880

    def record_market(self, state: MarketStateV1, radio_energy: float | None) -> None:
        """Append one timeline sample. Called by the live-state broadcaster.

        Kept in memory rather than queried from `market_states` on every request: the timeline
        is a *display* of recent movement, the station already has the authoritative rows, and
        a chart redraw must not put a query on the hot path.
        """
        self.market_history.append(
            (state.timestamp, state.energy, radio_energy, state.regime.value, state.price)
        )
        if len(self.market_history) > self.history_limit:
            del self.market_history[: len(self.market_history) - self.history_limit]

    def note_price(self, price: float | None) -> float | None:
        """Track the previous price and return the change, if both are known."""
        previous = self._previous_price
        if price is not None:
            self._previous_price = price
        if price is None or previous is None:
            return None
        return price - previous


# ----------------------------------------------------------------- market


def market_to_dto(
    state: MarketStateV1 | None,
    *,
    price_change: float | None,
    simulated: bool,
) -> MarketV1 | None:
    if state is None:
        return None
    stale = state.data_age_seconds > STALE_FEED_SECONDS
    price = state.price
    change_percent: float | None = None
    if price_change is not None and price is not None and price != 0.0:
        change_percent = price_change / price * 100.0
    return MarketV1(
        symbol=state.symbol,
        timestamp=state.timestamp,
        # Withheld when stale, not merely flagged. A number on screen is read as current
        # however it is styled, and §21 does not permit that.
        price=None if stale else price,
        price_change=None if stale else price_change,
        price_change_percent=None if stale else change_percent,
        regime=state.regime.value,
        regime_label=_titlecase(state.regime.value),
        regime_confidence=state.confidence,
        regime_age_seconds=state.regime_age_seconds,
        direction=state.direction.value,
        session=state.session.value,
        energy=state.energy,
        energy_velocity=state.energy_velocity,
        volatility=state.volatility,
        trend_strength=state.trend_strength,
        momentum=state.momentum,
        compression=state.compression,
        feed_status=state.feed_status.value,
        data_age_seconds=state.data_age_seconds,
        is_stale=stale,
        is_simulated=simulated,
    )


# ----------------------------------------------------------------- now playing


def _reason_from_blueprint(blueprint: MusicBlueprintV1) -> ProgrammingReasonV1:
    """The "Why this track?" panel, from what the director recorded when it decided.

    Stored metadata only. The director writes a ``rationale`` as it works — "critical priority
    (buffer empty)", "genre uk_trap", "target energy 84 from market 86" — and that, plus the
    market context it captured, is the whole content of this panel. Nothing is inferred and
    nothing is generated: the brief rules out exposing hidden model reasoning, and the
    deterministic factors are both more trustworthy and more useful.
    """
    diversity = next(
        (line for line in blueprint.rationale if line.startswith("diversity")), None
    )
    return ProgrammingReasonV1(
        factors=blueprint.rationale,
        market_regime=blueprint.market.regime.value,
        market_energy=blueprint.market.energy,
        # Scaled to 0–100 to match every other energy figure in the UI.
        #
        # The composition's energy is a 0–1 intensity while the market's is a 0–100 score,
        # and the dashboard calls both "energy". Rendered raw, the Why panel read
        # "50 → 1", which looks like the director ignored the market. Normalising here keeps
        # one definition of the word rather than asking each component to remember which
        # scale it is holding.
        target_energy=blueprint.composition.energy * 100.0,
        diversity_note=diversity,
        creative_temperature=blueprint.creative_temperature,
    )


def now_playing_to_dto(station: object) -> NowPlayingV1 | None:
    playout = station.playout  # type: ignore[attr-defined]
    item = playout.current
    if item is None:
        return None

    duration = max(item.duration_seconds, 0.001)
    elapsed = min(max(playout.position_seconds, 0.0), duration)
    entry = item.entry
    blueprint = entry.blueprint if entry is not None else None
    composition = blueprint.composition if blueprint is not None else None

    title = (
        blueprint.title
        if blueprint is not None
        else (
            "Station identification"
            if item.is_station_id
            else _titlecase(item.tier.value)
        )
    )

    return NowPlayingV1(
        track_id=item.track_id,
        title=title,
        artist=(blueprint.persona_id if blueprint is not None else None),
        tier=item.tier.value,
        is_station_id=item.is_station_id,
        genre=composition.genre if composition else None,
        secondary_genre=composition.secondary_genre if composition else None,
        bpm=composition.bpm if composition else None,
        musical_key=composition.key if composition else None,
        is_instrumental=(
            blueprint.is_instrumental if blueprint is not None else None
        ),
        vocal_style=(
            blueprint.vocal.style.value
            if blueprint is not None and blueprint.vocal.enabled
            else None
        ),
        duration_seconds=duration,
        elapsed_seconds=elapsed,
        remaining_seconds=max(0.0, duration - elapsed),
        progress=min(1.0, elapsed / duration),
        planned_regime=entry.planned_regime.value if entry is not None else None,
        planned_symbol=entry.blueprint.market.symbol if entry is not None else None,
        planned_energy=entry.planned_energy if entry is not None else None,
        novelty_target=blueprint.novelty.target if blueprint is not None else None,
        transition_in=entry.transition_in.value if entry is not None else None,
        origin=_TIER_ORIGINS[item.tier],
        started_at=entry.started_at if entry is not None else None,
        reason=_reason_from_blueprint(blueprint) if blueprint is not None else None,
        # The engine's measured peak, so a level meter shows real amplitude rather than an
        # animation. §51 and the brief both rule out a decorative waveform.
        output_peak=playout.stats.peak_sample or None,
    )


# ----------------------------------------------------------------- queue


def queue_to_dtos(station: object) -> tuple[QueueItemV1, ...]:
    """The upcoming queue, with "starts in" accumulated from what is on air now."""
    playout = station.playout  # type: ignore[attr-defined]
    snapshot = station.queue.snapshot()  # type: ignore[attr-defined]

    current = playout.current
    running = 0.0
    if current is not None:
        running = max(0.0, current.duration_seconds - playout.position_seconds)

    items: list[QueueItemV1] = []
    for position, entry in enumerate(snapshot.entries):
        items.append(_queue_entry_to_dto(entry, position=position, starts_in=running))
        running += max(0.0, entry.duration_seconds)
    return tuple(items)


def _queue_entry_to_dto(
    entry: QueueEntry, *, position: int, starts_in: float
) -> QueueItemV1:
    composition = entry.blueprint.composition
    return QueueItemV1(
        position=position,
        track_id=entry.track_id,
        title=entry.blueprint.title,
        artist=entry.blueprint.persona_id,
        genre=composition.genre,
        bpm=composition.bpm,
        # 0–100, like every other energy in the UI. See ``_reason_from_blueprint``.
        energy=composition.energy * 100.0,
        planned_regime=entry.planned_regime.value,
        planned_symbol=entry.blueprint.market.symbol,
        lock=entry.lock_level.value,
        lock_label=_LOCK_LABELS[entry.lock_level],
        lock_reason=entry.lock_reason or None,
        is_protected=entry.is_protected,
        readiness=entry.readiness.value,
        generation_state=entry.state.value,
        generation_progress=(
            entry.generation_progress if entry.generation_progress > 0 else None
        ),
        duration_seconds=entry.duration_seconds,
        starts_in_seconds=starts_in,
    )


# ----------------------------------------------------------------- buffer


def buffer_to_dto(
    assessment: BufferAssessment, *, ready_tracks: int, pending_tracks: int
) -> BufferV1:
    """Buffer health, plus the trend figure the dashboard leads with.

    ``trend_minutes_per_hour`` is ``drain_rate`` expressed per hour: the monitor works in
    buffer-minutes per minute, which is correct and unreadable. Sixty times a small number is
    a figure an operator can act on — "-12.4 min/hour" says how long they have in a way
    "-0.207" does not.
    """
    return BufferV1(
        level=assessment.level.value,
        trajectory=assessment.trajectory.value,
        reason=assessment.reason,
        ready_minutes=assessment.ready_minutes,
        pending_minutes=assessment.pending_minutes,
        minimum_minutes=assessment.minimum_minutes,
        target_minutes=assessment.target_minutes,
        maximum_minutes=assessment.maximum_minutes,
        fill_ratio=assessment.fill_ratio,
        ready_tracks=ready_tracks,
        pending_tracks=pending_tracks,
        capacity_ratio=assessment.capacity_ratio,
        trend_minutes_per_hour=assessment.drain_rate * 60.0,
        seconds_to_failure=assessment.seconds_to_failure,
    )


def emergency_to_dto(station: object) -> EmergencyV1:
    emergency = station.emergency  # type: ignore[attr-defined]
    stats = emergency.stats
    last_reason = None
    if stats.by_reason:
        last_reason = max(stats.by_reason.items(), key=lambda pair: pair[1])[0]
    return EmergencyV1(
        tier=emergency.tier.value,
        tier_label=_TIER_LABELS[emergency.tier],
        is_degraded=emergency.is_degraded,
        reserve_minutes=emergency.reserve_minutes,
        reserve_tracks_remaining=emergency.reserve_remaining,
        tier2_activations=stats.tier2_activations,
        tier3_activations=stats.tier3_activations,
        seconds_in_tier2=stats.seconds_in_tier2,
        seconds_in_tier3=stats.seconds_in_tier3,
        recoveries=stats.recoveries,
        last_reason=last_reason,
    )


# ----------------------------------------------------------------- generation


def generation_to_dto(view: RuntimeView) -> GenerationHealthV1 | None:
    manager = view.generation
    if manager is None:
        return None
    capacity = manager.capacity_snapshot()  # type: ignore[attr-defined]
    stats = manager.stats  # type: ignore[attr-defined]
    provider = getattr(view.settings, "generation", None)
    return GenerationHealthV1(
        provider=getattr(provider, "provider", "unknown"),
        capacity_ratio=capacity.capacity_ratio,
        # ``None`` rather than 0.0 before the first measurement: a p95 of zero seconds would
        # read as an impossibly fast generator rather than as "not measured yet".
        latency_p50_seconds=capacity.latency_p50_seconds if capacity.samples else None,
        latency_p95_seconds=capacity.latency_p95_seconds if capacity.samples else None,
        samples=capacity.samples,
        completed=stats.completed,
        failed=stats.failed,
        retries=stats.retries,
        timeouts=stats.timeouts,
        cancelled=stats.cancelled,
        in_flight=manager.in_flight,  # type: ignore[attr-defined]
        # Same figure, named for what it means operationally: every in-flight job holds a
        # lease, and a lease that outlives its job is what §70's reclaim path exists for.
        pending_leases=manager.in_flight,  # type: ignore[attr-defined]
        mean_audio_seconds=capacity.mean_audio_seconds if capacity.samples else None,
    )


def job_to_dto(job: JobRecord, *, now: datetime) -> GenerationJobV1:
    lease_in: float | None = None
    if job.lease_expires_at is not None:
        lease_in = (job.lease_expires_at - now).total_seconds()
    return GenerationJobV1(
        job_id=job.job_id,
        track_id=job.track_id,
        state=job.state.value,
        priority=job.priority.value,
        provider=job.provider,
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        age_seconds=max(0.0, (now - job.created_at).total_seconds()),
        lease_owner=job.lease_owner,
        lease_expires_in_seconds=lease_in,
        generation_seconds=job.generation_seconds,
        error_kind=job.error_kind,
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


# ----------------------------------------------------------------- health


def _health_row(
    name: str,
    label: str,
    state: str,
    detail: str,
    *,
    metric: str | None = None,
    checked_at: datetime | None = None,
) -> HealthComponentV1:
    return HealthComponentV1(
        name=name,
        label=label,
        state=state,
        detail=detail,
        metric=metric,
        checked_at=checked_at,
    )


def health_rows(view: RuntimeView) -> tuple[HealthComponentV1, ...]:
    """The System Health panel, assembled from real subsystem state.

    Each row answers two questions, because a state alone is not actionable: what is wrong, and
    **is the broadcast at risk**. A degraded generator above a 38-minute buffer is a different
    situation from the same generator above four minutes, and the operator should not have to
    cross-reference two panels to tell them apart.
    """
    rows: list[HealthComponentV1] = []
    station = view.station

    if station is None:
        rows.append(
            _health_row(
                "playout", "Playout", "offline", "No station attached to this process."
            )
        )
        return tuple(rows)

    playout = station.playout  # type: ignore[attr-defined]
    stats = playout.stats
    emergency = station.emergency  # type: ignore[attr-defined]
    assessment = station.assess_buffer()  # type: ignore[attr-defined]

    # -- playout
    if stats.unintended_silence_seconds > 0:
        rows.append(
            _health_row(
                "playout",
                "Playout",
                "critical",
                f"{stats.unintended_silence_seconds:.1f}s of unintended silence has aired.",
                metric=f"{stats.underruns} underruns",
            )
        )
    elif playout.state.value == "playing":
        rows.append(
            _health_row(
                "playout",
                "Playout",
                "healthy",
                f"On air, {stats.tracks_completed} tracks completed, no dead air.",
                metric=f"{stats.seconds_on_air / 60:.0f} min on air",
            )
        )
    else:
        rows.append(
            _health_row(
                "playout",
                "Playout",
                "offline" if playout.state.value == "stopped" else "recovering",
                f"Playout engine is {playout.state.value}.",
            )
        )

    # -- generator
    manager = view.generation
    if manager is None:
        rows.append(
            _health_row("generator", "Generator", "offline", "No generation manager attached.")
        )
    else:
        gen_stats = manager.stats  # type: ignore[attr-defined]
        capacity = manager.capacity_snapshot()  # type: ignore[attr-defined]
        safety = (
            f"buffer remains safe at {assessment.ready_minutes:.0f} min"
            if not assessment.level.is_degraded
            else f"buffer is {assessment.level.value} at {assessment.ready_minutes:.0f} min"
        )
        if gen_stats.timeouts or gen_stats.failed:
            state = "degraded" if not assessment.level.is_urgent else "critical"
            rows.append(
                _health_row(
                    "generator",
                    "Generator",
                    state,
                    f"{gen_stats.failed} failed and {gen_stats.timeouts} timed out; {safety}.",
                    metric=f"{capacity.capacity_ratio:.2f}x capacity",
                )
            )
        elif capacity.samples == 0:
            rows.append(
                _health_row(
                    "generator",
                    "Generator",
                    "recovering",
                    "No completed jobs yet; capacity is unmeasured.",
                )
            )
        else:
            rows.append(
                _health_row(
                    "generator",
                    "Generator",
                    "healthy" if capacity.capacity_ratio >= 1.0 else "degraded",
                    f"{gen_stats.completed} generated, {safety}.",
                    metric=f"{capacity.capacity_ratio:.2f}x capacity",
                )
            )

    # -- market feed
    service = view.market_service
    if service is None:
        rows.append(
            _health_row("market_feed", "Market Feed", "offline", "No market feed attached.")
        )
    else:
        state_obj = service.current_state  # type: ignore[attr-defined]
        age = state_obj.data_age_seconds if state_obj is not None else None
        if state_obj is None:
            rows.append(
                _health_row(
                    "market_feed",
                    "Market Feed",
                    "offline",
                    "The feed has not produced a state yet.",
                )
            )
        elif age is not None and age > STALE_FEED_SECONDS:
            rows.append(
                _health_row(
                    "market_feed",
                    "Market Feed",
                    "degraded",
                    f"Last update {age:.0f}s ago; prices are withheld while stale.",
                    metric=f"{age:.0f}s old",
                )
            )
        else:
            rows.append(
                _health_row(
                    "market_feed",
                    "Market Feed",
                    "healthy",
                    f"{service.feed_status.value} feed, "  # type: ignore[attr-defined]
                    f"{service.bars_processed} bars processed.",
                    metric=f"{age:.0f}s old" if age is not None else None,
                )
            )

    # -- queue / buffer
    queue_state = {
        "healthy": "healthy",
        "low": "degraded",
        "critical": "critical",
        "empty": "critical",
    }[assessment.level.value]
    rows.append(
        _health_row(
            "queue",
            "Queue",
            queue_state,
            assessment.reason,
            metric=f"{assessment.ready_minutes:.0f} min ready",
        )
    )

    # -- emergency
    rows.append(
        _health_row(
            "emergency",
            "Emergency System",
            "healthy" if not emergency.is_degraded else "degraded",
            f"Tier 1 is carrying the output; {emergency.reserve_minutes:.0f} min of reserve."
            if not emergency.is_degraded
            else f"{_TIER_LABELS[emergency.tier]} is carrying the output.",
            metric=_TIER_LABELS[emergency.tier],
        )
    )

    # -- database
    if view.database is None:
        rows.append(_health_row("database", "Database", "offline", "No database attached."))
    else:
        rows.append(
            _health_row(
                "database",
                "Database",
                "healthy",
                "Connected.",
            )
        )

    # -- capability-gated rows. Reported as planned rather than invented (§86).
    for capability, label in (
        (Capability.GPU, "GPU"),
        (Capability.OBS, "OBS"),
    ):
        report = view.capabilities.get(capability)
        if report is None:
            continue
        rows.append(
            _health_row(
                capability.value,
                label,
                "offline",
                report.detail,
            )
        )
    return tuple(rows)


def alerts_from(view: RuntimeView, *, now: datetime) -> tuple[AlertV1, ...]:
    """Only things worth interrupting the operator for, each with a remediation."""
    station = view.station
    if station is None:
        return ()
    alerts: list[AlertV1] = []
    playout = station.playout  # type: ignore[attr-defined]
    assessment = station.assess_buffer()  # type: ignore[attr-defined]
    emergency = station.emergency  # type: ignore[attr-defined]

    if playout.stats.unintended_silence_seconds > 0:
        alerts.append(
            AlertV1(
                key="dead_air",
                severity="critical",
                message=(
                    f"{playout.stats.unintended_silence_seconds:.1f}s of unintended silence "
                    "has aired."
                ),
                raised_at=now,
                remediation="Check the audio sink and the System page's recent failures.",
            )
        )
    if emergency.is_degraded:
        alerts.append(
            AlertV1(
                key="emergency_tier",
                severity="warning",
                message=f"{_TIER_LABELS[emergency.tier]} is carrying the output.",
                raised_at=now,
                remediation="The station recovers automatically once a track is ready.",
            )
        )
    if assessment.level.is_urgent:
        alerts.append(
            AlertV1(
                key="buffer",
                severity="critical",
                message=(
                    f"Buffer is {assessment.level.value} at "
                    f"{assessment.ready_minutes:.1f} minutes."
                ),
                raised_at=now,
                remediation="Check the generator; experimentation is already withheld.",
            )
        )
    elif assessment.is_failing_soon and assessment.seconds_to_failure is not None:
        alerts.append(
            AlertV1(
                key="buffer_trend",
                severity="warning",
                message=(
                    "At the current rate the buffer runs out in "
                    f"{assessment.seconds_to_failure / 60:.0f} minutes."
                ),
                raised_at=now,
                remediation="Generation capacity is below playback rate.",
            )
        )
    return tuple(alerts)


# ----------------------------------------------------------------- assembly


def capabilities_to_dtos(
    reports: Sequence[CapabilityReport] | dict[Capability, CapabilityReport],
) -> tuple[CapabilityV1, ...]:
    values = reports.values() if isinstance(reports, dict) else reports
    return tuple(
        CapabilityV1(
            capability=report.capability.value,
            state=report.state.value,
            detail=report.detail,
            arrives_in_phase=report.arrives_in_phase,
        )
        for report in values
    )


def status_to_dto(view: RuntimeView) -> StationStatusV1:
    settings = view.settings
    station = view.station
    version = _package_version()
    mode = getattr(getattr(settings, "mode", None), "value", str(getattr(settings, "mode", "")))

    if station is None:
        return StationStatusV1(
            is_broadcasting=False,
            playout_state="stopped",
            version=str(version),
            environment=str(mode),
            mode=str(mode),
        test_mode=bool(getattr(settings, "test_mode", False)),
            uptime_seconds=max(0.0, time.monotonic() - view.process_started_monotonic),
            tracks_played=0,
            tracks_generated=0,
            seconds_on_air=0.0,
            unintended_silence_seconds=0.0,
            underruns=0,
        )

    playout = station.playout  # type: ignore[attr-defined]
    stats = playout.stats
    station_stats = station.stats  # type: ignore[attr-defined]
    uptime = max(0.0, time.monotonic() - view.process_started_monotonic)
    coverage: float | None = None
    if uptime > 0 and stats.seconds_on_air > 0:
        coverage = min(1.0, stats.seconds_on_air / max(stats.seconds_on_air, uptime))

    return StationStatusV1(
        is_broadcasting=playout.state.value == "playing",
        playout_state=playout.state.value,
        version=str(version),
        environment=str(mode),
        mode=str(mode),
        test_mode=bool(getattr(settings, "test_mode", False)),
        started_at=view.started_at,
        uptime_seconds=uptime,
        tracks_played=stats.tracks_completed,
        tracks_generated=station_stats.tracks_ready,
        seconds_on_air=stats.seconds_on_air,
        unintended_silence_seconds=stats.unintended_silence_seconds,
        underruns=stats.underruns,
        audio_coverage=coverage,
    )


def routing_to_dto(view: RuntimeView) -> ActiveMarketV1 | None:
    """Render the routing subsystem for the header and the Market page.

    Returns ``None`` when no routing service is attached rather than inventing a
    single-symbol stand-in: a UI told "XAUUSD, open, healthy" by a view that in fact knows
    nothing about availability is worse than one that shows no routing panel at all.

    This is the only rendering of routing state. `ActiveMarketService` deliberately has no
    `as_payload()` of its own — a second dict-shaped view of the same facts would drift from
    this one the first time a field was added to just one of them.
    """
    service = cast("ActiveMarketService | None", view.routing)
    if service is None:
        return None

    router = service.router
    active = router.active
    symbols = tuple(
        MarketAvailabilityV1(
            symbol=assessment.symbol,
            state=assessment.state.value,
            reason=assessment.reason,
            feed_degraded=assessment.feed_degraded,
            data_age_seconds=_finite(assessment.data_age_seconds),
            feed_status=assessment.feed_status.value,
            calendar_open=assessment.calendar_open,
            is_active=symbol == service.active_symbol,
            bars_processed=service.bars_for(symbol),
            last_price=service.last_price(symbol),
            assessed_at=assessment.assessed_at,
        )
        for symbol, assessment in router.assessments.items()
    )

    return ActiveMarketV1(
        active_symbol=service.active_symbol,
        primary_symbol=router.primary,
        is_primary=bool(active and active.is_primary),
        has_active_market=bool(active and active.is_active),
        active_since=None if active is None else active.since,
        switch_reason=None if active is None else active.reason.value,
        switch_count=router.switch_count,
        pending_symbol=router.pending_symbol,
        pending_seconds_remaining=router.pending_seconds_remaining(),
        symbols=symbols,
    )


def _finite(value: float | None) -> float | None:
    """``inf`` and ``nan`` render as absent, never as a number.

    A symbol with no service has an infinite data age, and ``Infinity`` is not valid JSON —
    some parsers accept it, others reject the whole frame. "No answer" is the honest
    rendering regardless of which.
    """
    if value is None or value != value or value in (float("inf"), float("-inf")):
        return None
    return float(value)


def build_live_state(view: RuntimeView, *, now: datetime | None = None) -> LiveStateV1:
    """One coherent frame. The WebSocket's payload and `/api/status`'s body."""
    moment = now or datetime.now(tz=UTC)
    station = view.station
    service = view.market_service

    state = None
    simulated = False
    if service is not None:
        state = service.current_state  # type: ignore[attr-defined]
        simulated = str(getattr(service.feed_status, "value", "")) == "simulated"  # type: ignore[attr-defined]

    market_dto = market_to_dto(
        state, price_change=view.note_price(state.price if state else None), simulated=simulated
    )

    buffer_dto = None
    emergency_dto = None
    queue_dtos: tuple[QueueItemV1, ...] = ()
    now_playing = None
    if station is not None:
        snapshot = station.queue.snapshot()  # type: ignore[attr-defined]
        assessment = station.assess_buffer()  # type: ignore[attr-defined]
        buffer_dto = buffer_to_dto(
            assessment,
            ready_tracks=snapshot.ready_count,
            pending_tracks=max(0, snapshot.depth - snapshot.ready_count),
        )
        emergency_dto = emergency_to_dto(station)
        queue_dtos = queue_to_dtos(station)
        now_playing = now_playing_to_dto(station)

    return LiveStateV1(
        status=status_to_dto(view),
        market=market_dto,
        routing=routing_to_dto(view),
        now_playing=now_playing,
        queue=queue_dtos,
        buffer=buffer_dto,
        emergency=emergency_dto,
        generation=generation_to_dto(view),
        health=health_rows(view),
        alerts=alerts_from(view, now=moment),
        capabilities=capabilities_to_dtos(view.capabilities),
        at=moment,
    )


def market_history_dtos(
    view: RuntimeView, *, window: timedelta | None = None
) -> list[dict[str, object]]:
    """Timeline samples, newest last, optionally limited to a trailing window."""
    cutoff: datetime | None = None
    if window is not None and view.market_history:
        cutoff = view.market_history[-1][0] - window
    out: list[dict[str, object]] = []
    for at, energy, radio_energy, regime, price in view.market_history:
        if cutoff is not None and at < cutoff:
            continue
        out.append(
            {
                "at": at,
                "market_energy": energy,
                "radio_energy": radio_energy,
                "regime": regime,
                "price": price,
            }
        )
    return out
