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
    CapabilityV1,
    GenerationJobV1,
    SystemResourcesV1,
    TrackSummaryV1,
)
from tradefix_radio.api.snapshot import RuntimeView, capabilities_to_dtos, job_to_dto
from tradefix_radio.core.clock import UTC
from tradefix_radio.persistence.models import (
    GenerationJob,
    Track,
    TrackBlueprint,
    TrackFile,
)
from tradefix_radio.persistence.repositories import GenerationJobRepository

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
    return {
        "summary": summary,
        "blueprint": payload,
        # The count, deliberately **not** the path. §68: internal file paths are not exposed
        # unnecessarily, and the browser could not read one anyway.
        "has_audio": bool(audio_count),
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
            """Apply the time window, when there is one."""
            return query.where(Track.created_at >= since) if since else query

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


@router.get("/originality/summary")
async def get_originality(view: ViewDep) -> dict[str, object]:
    """Phase 6. Refuses with its reason rather than returning empty metrics."""
    _gate(view, Capability.ORIGINALITY)
    raise HTTPException(  # pragma: no cover - unreachable until Phase 6 lands
        status_code=status.HTTP_409_CONFLICT,
        detail="The originality engine reported ready but has no implementation.",
    )


@router.get("/obs/status")
async def get_obs_status(view: ViewDep) -> dict[str, object]:
    """Phase 8. Never reports a green connection it does not have."""
    _gate(view, Capability.OBS)
    raise HTTPException(  # pragma: no cover - unreachable until Phase 8 lands
        status_code=status.HTTP_409_CONFLICT,
        detail="OBS reported ready but has no implementation.",
    )
