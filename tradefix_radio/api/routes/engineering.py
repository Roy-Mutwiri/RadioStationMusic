"""Generation, system, library, analytics and the capability-gated pages.

What these routes have in common is that they read from the **database** rather than from live
runtime objects, so they are the only ones that can be slow. That shapes two decisions:

* Every list endpoint is paginated and bounded. A library of fifty thousand tracks must not be
  able to produce a fifty-megabyte response because a UI forgot a limit.
* Nothing here is on the WebSocket's hot path. The live frame carries *summaries* — job counts,
  capacity, buffer — and a page that wants rows asks for them when the operator opens it.

The capability-gated endpoints (originality, OBS) return their report rather than data, and the
status code says so. A 200 with empty arrays would let a UI render "0 rejections" for a
subsystem that does not exist, which is exactly the fabrication §86 forbids.
"""

from __future__ import annotations

import contextlib
import time
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Select, func, select

from tradefix_radio.api.capabilities import Capability, CapabilityReport
from tradefix_radio.api.deps import get_view
from tradefix_radio.api.dto import (
    AudioFeaturesV1,
    CapabilityV1,
    FingerprintV1,
    GenerationJobV1,
    LyricFingerprintV1,
    MasteringV1,
    OriginalityResultV1,
    OriginalitySummaryV1,
    ProviderStatusV1,
    QcCheckV1,
    QcResultV1,
    SimilarityComponentV1,
    SystemResourcesV1,
    TrackEvidenceV1,
    TrackSummaryV1,
)
from tradefix_radio.api.snapshot import RuntimeView, capabilities_to_dtos, job_to_dto
from tradefix_radio.audio.fingerprint import fingerprint_capability
from tradefix_radio.contracts.enums import TrackProvenance
from tradefix_radio.core.clock import UTC
from tradefix_radio.persistence.models import (
    GenerationJob,
    Lyrics,
    ProviderSubmission,
    SimilarityResult,
    Track,
    TrackBlueprint,
    TrackFile,
)
from tradefix_radio.persistence.repositories import (
    GenerationJobRepository,
    LyricsRepository,
    OriginalityRepository,
    ProviderSubmissionsRepository,
)
from tradefix_radio.persistence.repositories.originality import TrackEvidence

_log = structlog.get_logger(__name__)

router = APIRouter()

ViewDep = Annotated[RuntimeView, Depends(get_view)]


def _require_database(view: RuntimeView) -> object:
    database = view.database
    if database is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No database is attached to this API process.",
        )
    return database


def _gate(view: RuntimeView, capability: Capability) -> CapabilityReport:
    """Refuse, with the reason, when a subsystem does not exist yet.

    409 rather than 404 or 501: the resource is real and named in the API, it simply is not
    available in this build. The body carries the phase that delivers it so the UI can say
    "awaiting Phase 6" rather than "something went wrong".
    """
    report = view.capabilities.get(capability)
    if report is None or not report.is_ready:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "capability": capability.value,
                "state": report.state.value if report else "unavailable",
                "detail": report.detail if report else "Unknown capability.",
                "arrives_in_phase": report.arrives_in_phase if report else None,
            },
        )
    return report


# ----------------------------------------------------------------- capabilities


@router.get("/capabilities", response_model=list[CapabilityV1])
async def get_capabilities(view: ViewDep) -> list[CapabilityV1]:
    """What this build can do. The UI asks once at boot and gates pages on it."""
    return list(capabilities_to_dtos(view.capabilities))


# ----------------------------------------------------------------- generation


class JobCountsV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    by_state: dict[str, int]
    total: int


@router.get("/generation/jobs", response_model=list[GenerationJobV1])
async def get_generation_jobs(
    view: ViewDep,
    state: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[GenerationJobV1]:
    database = _require_database(view)
    now = datetime.now(tz=UTC)
    async with database.read_session() as session:  # type: ignore[attr-defined]
        repository = GenerationJobRepository(session)
        if state:
            from tradefix_radio.core.job_states import JobState  # noqa: PLC0415

            try:
                wanted = JobState(state)
            except ValueError as error:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Unknown job state {state!r}.",
                ) from error
            jobs = await repository.in_state(wanted, limit=limit)
        else:
            # Newest-first across every state. Queried here rather than by adding a
            # ``recent()`` to the Phase 4 repository: the runtime has no use for it, and
            # Phase 4's modules do not grow methods to make a UI easier.
            rows = await session.execute(
                select(GenerationJob)
                .order_by(GenerationJob.created_at.desc())
                .limit(limit)
            )
            jobs = []
            for job_id in [row.job_id for row in rows.scalars()]:
                record = await repository.get(job_id)
                if record is not None:
                    jobs.append(record)
    return [job_to_dto(job, now=now) for job in jobs]


@router.get("/generation/provider", response_model=ProviderStatusV1)
async def get_provider_status(view: ViewDep) -> ProviderStatusV1:
    """Live provider state (§7.24).

    Served from the provider rather than the database, because every interesting field —
    model state, VRAM, in-flight track, observed latencies — is process state that was never
    written down. A provider without a `status` attribute (the mock) still answers, with the
    fields it can honestly fill and `null` for the rest.
    """
    generation = view.generation
    if generation is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No generation manager is attached to this API process.",
        )

    provider = getattr(generation, "provider", None)
    description = provider.describe() if provider is not None else None
    live = getattr(provider, "status", None)

    if live is None:
        # A provider with no status surface — the mock. Report what `describe()` gives and
        # leave the rest absent. Inventing a "ready" here would put a green light on a
        # subsystem nobody asked.
        healthy = await generation.provider_health()  # type: ignore[attr-defined]
        return ProviderStatusV1(
            provider=description.name if description else "unknown",
            status="ready" if healthy else "failed",
            model=description.model_identifier if description else "unknown",
            loaded=healthy,
            supports_progress=False,
        )

    payload = live.as_payload()
    gpu = payload.get("gpu") or {}
    assert isinstance(gpu, dict)

    elapsed: float | None = None
    started = getattr(live, "current_started_monotonic", None)
    if started is not None:
        elapsed = max(0.0, time.monotonic() - float(started))

    return ProviderStatusV1(
        provider=str(payload["provider"]),
        status=str(payload["status"]),
        model=str(payload["model"]),
        lm_model=_text(payload.get("lm_model")),
        version=_text(payload.get("version")),
        loaded=bool(payload["loaded"]),
        load_seconds=_number(payload.get("load_seconds")),
        last_success_at=_moment(payload.get("last_success_at")),
        last_error=_text(payload.get("last_error")),
        last_error_at=_moment(payload.get("last_error_at")),
        generations=int(payload.get("generations") or 0),
        failures=int(payload.get("failures") or 0),
        oom_events=int(payload.get("oom_events") or 0),
        latency_p50_seconds=_number(payload.get("latency_p50_seconds")),
        latency_p95_seconds=_number(payload.get("latency_p95_seconds")),
        current_track_id=_text(payload.get("current_track_id")),
        current_elapsed_seconds=elapsed,
        vram_total_mb=_number(gpu.get("total_mb")),
        vram_used_mb=_number(gpu.get("used_mb_after") or gpu.get("used_mb_before")),
        vram_free_mb=_number(gpu.get("free_mb_before")),
        peak_vram_mb=_number(gpu.get("peak_used_mb")),
        gpu_temperature_c=_number(
            gpu.get("temperature_c_after") or gpu.get("temperature_c_before")
        ),
        # §7.25: ACE-Step reports pending or done, nothing between.
        supports_progress=False,
    )


def _number(value: Any) -> float | None:
    """A float, or ``None``. Never a zero standing in for "not measured"."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(value)
    return None


@router.get("/generation/counts", response_model=JobCountsV1)
async def get_generation_counts(view: ViewDep) -> JobCountsV1:
    database = _require_database(view)
    async with database.read_session() as session:  # type: ignore[attr-defined]
        counts = await GenerationJobRepository(session).count_by_state()
    by_state = {state.value: count for state, count in counts.items()}
    return JobCountsV1(by_state=by_state, total=sum(by_state.values()))


# ----------------------------------------------------------------- system


@router.get("/system/resources", response_model=SystemResourcesV1)
async def get_system_resources(view: ViewDep) -> SystemResourcesV1:
    """Host and process figures, with absent hardware reported as absent.

    The event-loop lag measurement is the useful one and the easiest to get wrong: it is the
    difference between how long a zero-second sleep was asked to take and how long it did,
    which is precisely the delay a *new* callback would experience. Measuring anything else
    here — a timer, a counter — would report the loop's load rather than its latency.
    """
    import asyncio  # noqa: PLC0415

    import psutil  # noqa: PLC0415

    started = time.perf_counter()
    await asyncio.sleep(0)
    lag_ms = (time.perf_counter() - started) * 1000.0

    process = psutil.Process()
    virtual = psutil.virtual_memory()
    try:
        disk = psutil.disk_usage(str(getattr(view.settings, "paths", None).data_dir))  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - an unreadable path must not fail the whole page
        disk = None

    database_mb: float | None = None
    paths = getattr(view.settings, "paths", None)
    if paths is not None:
        candidate = getattr(paths, "data_dir", None)
        if candidate is not None:
            for file in candidate.glob("*.db"):
                database_mb = (database_mb or 0.0) + file.stat().st_size / 1_048_576

    gpu = _gpu_metrics()

    return SystemResourcesV1(
        cpu_percent=psutil.cpu_percent(interval=None),
        memory_used_mb=(virtual.total - virtual.available) / 1_048_576,
        memory_total_mb=virtual.total / 1_048_576,
        disk_free_gb=(disk.free / 1_073_741_824) if disk else None,
        disk_total_gb=(disk.total / 1_073_741_824) if disk else None,
        gpu_name=gpu.get("name"),
        gpu_percent=gpu.get("utilisation"),
        vram_used_mb=gpu.get("vram_used_mb"),
        vram_total_mb=gpu.get("vram_total_mb"),
        gpu_temperature_c=gpu.get("temperature_c"),
        process_uptime_seconds=max(0.0, time.monotonic() - view.process_started_monotonic),
        process_memory_mb=process.memory_info().rss / 1_048_576,
        event_loop_lag_ms=lag_ms,
        active_tasks=len(asyncio.all_tasks()),
        database_size_mb=database_mb,
    )


def _gpu_metrics() -> dict[str, object]:
    """NVML figures, or an empty mapping. Never zeros.

    An absent GPU yields no keys at all, so the DTO's fields stay ``None`` and the UI renders
    "no GPU" rather than a card full of zeroes that looks like a very cold, very idle card.
    """
    try:  # pragma: no cover - depends on the host
        import pynvml  # type: ignore[import-not-found]  # noqa: PLC0415
    except ImportError:
        return {}
    try:  # pragma: no cover - depends on the host
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
        rates = pynvml.nvmlDeviceGetUtilizationRates(handle)
        name = pynvml.nvmlDeviceGetName(handle)
        return {
            "name": name.decode() if isinstance(name, bytes) else str(name),
            "utilisation": float(rates.gpu),
            "vram_used_mb": memory.used / 1_048_576,
            "vram_total_mb": memory.total / 1_048_576,
            "temperature_c": float(
                pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            ),
        }
    except Exception:  # noqa: BLE001 - no GPU is a normal answer, not a fault
        return {}
    finally:  # pragma: no cover - depends on the host
        with_shutdown = getattr(pynvml, "nvmlShutdown", None)
        if with_shutdown is not None:
            # Nothing useful to do if NVML will not shut down, and raising here would mask
            # the metrics we just read successfully.
            with contextlib.suppress(Exception):
                with_shutdown()


# ----------------------------------------------------------------- library


class LibraryPageV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[TrackSummaryV1]
    total: int
    offset: int
    limit: int


@router.get("/library/tracks", response_model=LibraryPageV1)
async def get_library(
    view: ViewDep,
    search: Annotated[str | None, Query(max_length=120)] = None,
    genre: Annotated[str | None, Query(max_length=64)] = None,
    regime: Annotated[str | None, Query(max_length=64)] = None,
    symbol: Annotated[str | None, Query(max_length=32)] = None,
    state: Annotated[str | None, Query(max_length=32)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> LibraryPageV1:
    """A page of the track library, joined to its blueprint for the musical columns."""
    database = _require_database(view)
    async with database.read_session() as session:  # type: ignore[attr-defined]
        query = select(Track, TrackBlueprint).outerjoin(
            TrackBlueprint, TrackBlueprint.track_id == Track.track_id
        )
        if search:
            needle = f"%{search.lower()}%"
            query = query.where(
                func.lower(Track.track_id).like(needle)
                | func.lower(Track.title).like(needle)
            )
        if genre:
            query = query.where(Track.genre == genre)
        if regime:
            query = query.where(Track.regime_at_generation == regime)
        if symbol:
            # Absent means every market, which is the default a reader expects from a
            # library page. There is no "ALL" sentinel to get wrong.
            query = query.where(Track.symbol_at_generation == symbol.upper())
        if state:
            query = query.where(Track.state == state)

        total = await session.scalar(
            select(func.count()).select_from(query.subquery())
        )
        rows = await session.execute(
            query.order_by(Track.created_at.desc()).offset(offset).limit(limit)
        )
        items = [_track_summary(track, blueprint) for track, blueprint in rows.all()]
    return LibraryPageV1(items=items, total=int(total or 0), offset=offset, limit=limit)


def _track_summary(track: object, blueprint: object | None) -> TrackSummaryV1:
    payload = getattr(blueprint, "payload", None) if blueprint is not None else None
    composition = (payload or {}).get("composition", {}) if isinstance(payload, dict) else {}
    return TrackSummaryV1(
        track_id=track.track_id,  # type: ignore[attr-defined]
        title=getattr(track, "title", None) or track.track_id,  # type: ignore[attr-defined]
        artist=getattr(track, "persona_id", None),
        genre=getattr(track, "genre", None) or composition.get("genre"),
        secondary_genre=composition.get("secondary_genre"),
        bpm=getattr(track, "bpm", None) or composition.get("bpm"),
        musical_key=getattr(track, "musical_key", None) or composition.get("key"),
        duration_seconds=getattr(track, "duration_seconds", None),
        is_instrumental=getattr(track, "is_instrumental", None),
        state=str(getattr(track, "state", "unknown")),
        planned_regime=getattr(track, "regime_at_generation", None),
        planned_symbol=getattr(track, "symbol_at_generation", None),
        planned_energy=getattr(track, "energy_at_generation", None),
        # The real column, which Phase 6 populates. It is NULL for every track generated
        # before the originality engine exists, and the DTO carries that through as absent
        # rather than as 0.0 — the blueprint's novelty *target* is nearby and it would be a
        # fabrication to show a requested value where a measured one belongs.
        novelty_score=getattr(track, "novelty_score", None),
        play_count=int(getattr(track, "play_count", 0) or 0),
        created_at=getattr(track, "created_at", None),
        last_played_at=getattr(track, "last_played_at", None),
    )


@router.get("/library/tracks/{track_id}")
async def get_library_track(track_id: str, view: ViewDep) -> dict[str, object]:
    """One track, with its full stored blueprint.

    The blueprint is returned verbatim because it *is* the record of the decision — the
    detail panel's "why" section is that document, and summarising it here would mean the UI
    showed an interpretation rather than what the director actually recorded.
    """
    database = _require_database(view)
    async with database.read_session() as session:  # type: ignore[attr-defined]
        row = await session.execute(
            select(Track, TrackBlueprint)
            .outerjoin(TrackBlueprint, TrackBlueprint.track_id == Track.track_id)
            .where(Track.track_id == track_id)
        )
        found = row.first()
        if found is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown track {track_id!r}."
            )
        track, blueprint = found
        summary = _track_summary(track, blueprint)
        payload = getattr(blueprint, "payload", None) if blueprint is not None else None
        # Audio lives in ``track_files``, and ADR-07 makes the bytes expendable while the
        # metadata is permanent — so a track row existing says nothing about whether the file
        # does. The UI needs to know whether a player can be offered at all.
        audio_count = await session.scalar(
            select(func.count())
            .select_from(TrackFile)
            .where(TrackFile.track_id == track_id, TrackFile.deleted_at.is_(None))
        )
        lyrics = await LyricsRepository(session).get(track_id)
        submission = await ProviderSubmissionsRepository(session).latest_for_track(
            track_id
        )
    return {
        "summary": summary,
        "blueprint": payload,
        # The count, deliberately **not** the path. §68: internal file paths are not exposed
        # unnecessarily, and the browser could not read one anyway.
        "has_audio": bool(audio_count),
        "lyrics": None if lyrics is None else _lyrics_panel(lyrics),
        # What the model was actually told, beside what the station decided. A vocal track
        # that went out instrumental is invisible from the blueprint alone — the blueprint
        # records the intent, and the intent was honoured right up to the provider.
        "submission": None if submission is None else _submission_panel(submission),
    }


def _lyrics_panel(row: Lyrics) -> dict[str, object]:
    """The §46 lyric panel: the words, and the decisions behind them."""
    return {
        "text": row.text,
        "format": row.lyric_format,
        "perspective": row.perspective,
        "primary_topic": row.primary_topic,
        "secondary_topic": row.secondary_topic,
        "tradefix_mentions": row.tradefix_mentions,
        "educational_intensity": round(row.educational_intensity, 3),
        "word_count": row.word_count,
        "concepts_used": list(row.concepts_used or ()),
        "created_at": row.created_at.isoformat(),
    }


def _submission_panel(row: ProviderSubmission) -> dict[str, object]:
    """What was sent to the provider (§7.9, §7.10, §7.26).

    The lyric text is included because it is the point: the question this panel answers is
    "did the words reach the model", and a summary that said "yes, 142 words" would have
    been equally true of the run where the model received ``[Instrumental]``.
    """
    return {
        "attempt": row.attempt,
        "provider": row.provider,
        "model_identifier": row.model_identifier,
        "caption": row.caption,
        "requested_lyrics": row.requested_lyrics,
        "provider_lyrics": row.provider_lyrics,
        "lyrics_modified": row.lyrics_modified,
        "lyric_notes": list(row.lyric_notes or ()),
        "instrumental": row.instrumental,
        "profile": row.profile,
        "inference_steps": row.inference_steps,
        "guidance_scale": row.guidance_scale,
        "seed": row.seed,
        "warnings": list(row.warnings or ()),
        # The headline the panel leads on: a vocal track realised without words.
        "vocals_downgraded": bool(row.lyrics_modified and row.instrumental),
    }


# ----------------------------------------------------------------- analytics


class AnalyticsV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    window: str
    tracks_generated: int
    tracks_played: int
    generation_failures: int
    emergency_activations: int
    average_market_energy: float | None = None
    average_radio_energy: float | None = None
    genre_distribution: list[dict[str, object]]
    regime_distribution: list[dict[str, object]]
    bpm_distribution: list[dict[str, object]]


_ANALYTICS_WINDOWS: dict[str, timedelta | None] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}


@router.get("/analytics", response_model=AnalyticsV1)
async def get_analytics(
    view: ViewDep,
    window: Annotated[Literal["24h", "7d", "30d", "all"], Query()] = "24h",
    symbol: Annotated[str | None, Query(max_length=32)] = None,
) -> AnalyticsV1:
    """Aggregates over the tracks table. Counted, never estimated.

    Every figure is a ``COUNT`` or an ``AVG`` over rows that exist. An empty database yields
    zeros and empty distributions, which the UI renders as an empty state — the one thing it
    must not do is draw a plausible chart from nothing.
    """
    database = _require_database(view)
    span = _ANALYTICS_WINDOWS[window]
    since = datetime.now(tz=UTC) - span if span else None

    async with database.read_session() as session:  # type: ignore[attr-defined]

        def scoped(query: Select[Any]) -> Select[Any]:
            """Apply the time window and the market filter, when there are any.

            The market filter goes here rather than at each call site so that every figure
            on the page is scoped the same way. A page where the genre distribution was
            per-market and the track count was not would invite exactly the comparison it
            cannot support.

            An absent symbol means every market. There is no "ALL" sentinel to mistype.
            """
            if since:
                query = query.where(Track.created_at >= since)
            if symbol:
                query = query.where(Track.symbol_at_generation == symbol.upper())
            return query

        generated = int(
            await session.scalar(scoped(select(func.count()).select_from(Track))) or 0
        )
        played = int(
            await session.scalar(
                scoped(select(func.count()).select_from(Track)).where(
                    Track.state == "played"
                )
            )
            or 0
        )
        energy = await session.scalar(scoped(select(func.avg(Track.energy_at_generation))))
        genres = await session.execute(
            scoped(
                select(Track.genre, func.count())
                .group_by(Track.genre)
                .order_by(func.count().desc())
            )
        )
        regimes = await session.execute(
            scoped(
                select(Track.regime_at_generation, func.count())
                .group_by(Track.regime_at_generation)
                .order_by(func.count().desc())
            )
        )
        bpms = await session.execute(
            scoped(select(Track.bpm, func.count()).group_by(Track.bpm).order_by(Track.bpm))
        )
        # Not scoped by market, and deliberately not: a generation job that failed may
        # never have produced a track to attach a symbol to, so filtering here would
        # silently drop the failures that matter most. It is also not scoped by window,
        # which predates this and is noted in the DTO.
        failures = int(
            await session.scalar(
                select(func.count())
                .select_from(GenerationJob)
                .where(GenerationJob.state == "failed")
            )
            or 0
        )

    station = view.station
    activations = 0
    radio_energy: float | None = None
    if station is not None:
        stats = station.emergency.stats  # type: ignore[attr-defined]
        activations = stats.tier2_activations + stats.tier3_activations
        entries = station.history().entries  # type: ignore[attr-defined]
        if entries:
            radio_energy = sum(e.energy_at_generation for e in entries) / len(entries)

    return AnalyticsV1(
        window=window,
        tracks_generated=generated,
        tracks_played=played,
        generation_failures=failures,
        emergency_activations=activations,
        average_market_energy=float(energy) if energy is not None else None,
        average_radio_energy=radio_energy,
        genre_distribution=[
            {"label": str(name), "count": int(count)} for name, count in genres.all() if name
        ],
        regime_distribution=[
            {"label": str(name), "count": int(count)} for name, count in regimes.all() if name
        ],
        bpm_distribution=[
            {"label": int(bpm), "count": int(count)} for bpm, count in bpms.all() if bpm
        ],
    )


# ----------------------------------------------------------------- not yet built


#: What the Originality page states, verbatim, above every number on it.
#:
#: §86 forbids claiming that fingerprinting guarantees copyright uniqueness, and §6 opens by
#: saying the stronger claim — that a song has never existed before — is not technically
#: defensible. So the scope is sent with the data rather than left to a UI copywriter: the
#: component that owns the numbers owns the sentence that bounds them.
ORIGINALITY_SCOPE_NOTE = (
    "These checks compare a track against this station's own library. They detect the "
    "station repeating itself. They do not and cannot establish that a track is original "
    "with respect to any other music, and they are not a copyright clearance."
)


@router.get("/originality/summary", response_model=OriginalitySummaryV1)
async def get_originality_summary(view: ViewDep) -> OriginalitySummaryV1:
    """Library-wide originality statistics (§6.15)."""
    _gate(view, Capability.ORIGINALITY)
    database = _require_database(view)
    async with database.read_session() as session:  # type: ignore[attr-defined]
        repository = OriginalityRepository(session)
        library_size = await repository.library_size()
        verdict_counts = await repository.verdict_counts()
        histogram = await repository.novelty_distribution()
        production_references = int(
            await session.scalar(
                select(func.count())
                .select_from(Track)
                .where(Track.provenance == TrackProvenance.PRODUCTION_RADIO.value)
            )
            or 0
        )
        resolution_counts = {
            str(row[0]): int(row[1])
            for row in (
                await session.execute(
                    select(SimilarityResult.final_disposition, func.count())
                    .where(SimilarityResult.final_disposition.is_not(None))
                    .group_by(SimilarityResult.final_disposition)
                )
            ).all()
        }
        evidence_counts = {
            str(row[0]): int(row[1])
            for row in (
                await session.execute(
                    select(SimilarityResult.evidence_class, func.count())
                    .where(SimilarityResult.evidence_class.is_not(None))
                    .group_by(SimilarityResult.evidence_class)
                )
            ).all()
        }
        resolver_version = await session.scalar(
            select(SimilarityResult.resolver_version)
            .where(SimilarityResult.resolver_version.is_not(None))
            .order_by(SimilarityResult.id.desc())
            .limit(1)
        )
    capability = fingerprint_capability()
    return OriginalitySummaryV1(
        library_size=library_size,
        evaluated_count=sum(verdict_counts.values()),
        verdict_counts=verdict_counts,
        novelty_histogram=tuple(histogram),
        fingerprint_provider=str(capability["active_provider"]),
        fingerprint_detail=str(capability["detail"]),
        scope_note=ORIGINALITY_SCOPE_NOTE,
        production_references=production_references,
        # Stated rather than inferred from a zero count, so the page can say "cold start"
        # instead of showing an approval rate that looks like quality and is an empty
        # library.
        cold_start=production_references == 0,
        resolution_counts=resolution_counts,
        evidence_counts=evidence_counts,
        resolver_version=None if resolver_version is None else str(resolver_version),
    )


@router.get("/originality/recent", response_model=list[OriginalityResultV1])
async def get_recent_originality(
    view: ViewDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[OriginalityResultV1]:
    """The most recent verdicts, newest first (§6.15)."""
    _gate(view, Capability.ORIGINALITY)
    database = _require_database(view)
    async with database.read_session() as session:  # type: ignore[attr-defined]
        rows = await OriginalityRepository(session).recent_evaluations(limit=limit)
    return [_originality_row_to_dto(row) for row in rows]


@router.get("/originality/tracks/{track_id}", response_model=TrackEvidenceV1)
async def get_track_evidence(track_id: str, view: ViewDep) -> TrackEvidenceV1:
    """Every number behind one track's approval or rejection (§6.15).

    404 when nothing was ever recorded, rather than an empty document: a track with no
    evidence has not been through post-production, and returning an empty shell would read
    as "everything passed with no findings".
    """
    _gate(view, Capability.ORIGINALITY)
    database = _require_database(view)
    async with database.read_session() as session:  # type: ignore[attr-defined]
        evidence = await OriginalityRepository(session).evidence_for(track_id)
    if not evidence.qc_results and evidence.features is None and evidence.similarity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No post-production record exists for track {track_id!r}.",
        )
    return _evidence_to_dto(evidence)


def _originality_row_to_dto(row: Any) -> OriginalityResultV1:
    return OriginalityResultV1(
        track_id=row.track_id,
        verdict=row.verdict,
        novelty_score=row.novelty_score,
        max_similarity=row.max_similarity,
        threshold=row.threshold,
        closest_track_id=row.closest_track_id,
        deciding_component=row.deciding_component,
        compared_against=row.compared_against,
        comparisons=tuple(_comparison_to_dto(entry) for entry in (row.components or ())),
        evaluated_at=row.evaluated_at,
    )


def _comparison_to_dto(entry: dict[str, Any]) -> SimilarityComponentV1:
    return SimilarityComponentV1(
        track_id=str(entry.get("track_id", "")),
        score=float(entry.get("score", 0.0)),
        components={
            str(name): float(value)
            for name, value in dict(entry.get("components") or {}).items()
        },
        is_exact_audio=bool(entry.get("is_exact_audio", False)),
        is_exact_lyrics=bool(entry.get("is_exact_lyrics", False)),
        blueprint_threshold=entry.get("blueprint_threshold"),
        blueprint_is_recent=entry.get("blueprint_is_recent"),
        detail=str(entry.get("detail", "")),
    )


def _evidence_to_dto(evidence: TrackEvidence) -> TrackEvidenceV1:
    similarity = evidence.similarity
    return TrackEvidenceV1(
        track_id=evidence.track_id,
        qc_results=tuple(
            QcResultV1(
                stage=result.stage,
                status=result.status,
                summary=result.summary,
                passed_count=result.passed_count,
                warned_count=result.warned_count,
                failed_count=result.failed_count,
                analysis_backend=result.analysis_backend,
                elapsed_seconds=result.elapsed_seconds,
                evaluated_at=result.evaluated_at,
                checks=tuple(
                    QcCheckV1(
                        name=check.name,
                        status=check.status,
                        value=check.value,
                        unit=check.unit,
                        threshold=check.threshold,
                        reason=check.reason,
                    )
                    for check in result.checks
                ),
            )
            for result in evidence.qc_results
        ),
        features=(None if evidence.features is None else AudioFeaturesV1(**evidence.features)),
        originality=(
            None
            if similarity is None
            else OriginalityResultV1(
                track_id=evidence.track_id,
                verdict=similarity["verdict"],
                novelty_score=similarity["novelty_score"],
                max_similarity=similarity["max_similarity"],
                threshold=similarity["threshold"],
                closest_track_id=similarity["closest_track_id"],
                deciding_component=similarity["deciding_component"],
                compared_against=similarity["compared_against"],
                comparisons=tuple(
                    _comparison_to_dto(entry) for entry in similarity["comparisons"]
                ),
                evaluated_at=similarity["evaluated_at"],
            )
        ),
        mastering=(None if evidence.mastering is None else MasteringV1(**evidence.mastering)),
        fingerprint=(
            None if evidence.fingerprint is None else FingerprintV1(**evidence.fingerprint)
        ),
        lyrics=(None if evidence.lyrics is None else LyricFingerprintV1(**evidence.lyrics)),
    )


@router.get("/obs/status")
async def get_obs_status(view: ViewDep) -> dict[str, object]:
    """Phase 8. Never reports a green connection it does not have."""
    _gate(view, Capability.OBS)
    raise HTTPException(  # pragma: no cover - unreachable until Phase 8 lands
        status_code=status.HTTP_409_CONFLICT,
        detail="OBS reported ready but has no implementation.",
    )
