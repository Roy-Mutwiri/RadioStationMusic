"""Status, market, radio and simulation routes.

The operator controls here are deliberately few. The brief is blunt about it — *"Add only safe
controls currently supported by the runtime. Do not implement frontend buttons backed by fake
behavior."* — so this module exposes exactly three mutating endpoints, and each one maps to a
method Phase 4 already shipped and tested:

``skip``    → :meth:`PlayoutEngine.request_skip`, which ends the current track at the next
            block boundary. It does not reach into the audio path.
``lock``    → :meth:`RadioQueue.set_lock` with ``OPERATOR_PINNED``, which §28 already defines
            as outranking positional locks.
``unlock``  → the same, returning a slot to the positional lock the queue computes for it.

The simulator's market-closure control is a fourth, available only where §72 already allows
scenario switching. It overrides the *calendar*, never the feed: a symbol forced closed
reports CLOSED with a reason naming the override, so nobody reading the Market page can
mistake a test for a real closure, and the "closed market" and "broken feed" paths stay as
distinguishable under simulation as they are in production.

Everything else an operator might want — regenerate, remove, move, preview — is **not here**,
because the runtime has no safe path for it yet. A button that silently did nothing would be
worse than its absence, and the UI renders those actions as unavailable with a reason.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Final, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradefix_radio.api.capabilities import Capability
from tradefix_radio.api.deps import get_view
from tradefix_radio.api.dto import (
    ActiveMarketV1,
    BufferV1,
    EmergencyV1,
    LiveStateV1,
    MarketV1,
    NowPlayingV1,
    QueueItemV1,
    StationStatusV1,
)
from tradefix_radio.api.snapshot import (
    RuntimeView,
    buffer_to_dto,
    build_live_state,
    emergency_to_dto,
    market_history_dtos,
    market_to_dto,
    now_playing_to_dto,
    queue_to_dtos,
    routing_to_dto,
    status_to_dto,
)
from tradefix_radio.contracts.queue import QueueLockLevel
from tradefix_radio.core.clock import UTC

_log = structlog.get_logger(__name__)

router = APIRouter()

ViewDep = Annotated[RuntimeView, Depends(get_view)]

#: Windows the energy timeline offers, mapped to real durations.
_WINDOWS: dict[str, timedelta] = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "session": timedelta(hours=8),
}


def _require_database(view: RuntimeView) -> object:
    """The same guard `engineering.py` applies, stated here rather than imported.

    Routers do not import each other: a cross-module import between two route files is the
    start of a cycle, and the guard is four lines.
    """
    database = view.database
    if database is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No database is attached to this API process.",
        )
    return database


def _require_station(view: RuntimeView) -> object:
    station = view.station
    if station is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No station is attached to this API process.",
        )
    return station


# ----------------------------------------------------------------- status


@router.get("/status", response_model=LiveStateV1, summary="Everything, in one frame")
async def get_status(view: ViewDep) -> LiveStateV1:
    """The same payload the WebSocket pushes.

    Shared deliberately: one builder, one shape, so a page that loads over REST and then
    switches to the socket cannot show two different stations.
    """
    return build_live_state(view)


@router.get("/station", response_model=StationStatusV1)
async def get_station_status(view: ViewDep) -> StationStatusV1:
    return status_to_dto(view)


# ----------------------------------------------------------------- market


@router.get("/market/current", response_model=MarketV1 | None)
async def get_market_current(view: ViewDep) -> MarketV1 | None:
    service = view.market_service
    if service is None:
        return None
    state = service.current_state  # type: ignore[attr-defined]
    simulated = str(getattr(service.feed_status, "value", "")) == "simulated"  # type: ignore[attr-defined]
    return market_to_dto(
        state,
        price_change=None,
        simulated=simulated,
    )


class MarketHistoryV1(BaseModel):
    """The energy timeline's series, plus where the regime changed."""

    model_config = ConfigDict(frozen=True)

    window: str
    points: list[dict[str, object]]
    #: Index boundaries where the regime changed, so the chart can band them.
    regime_changes: list[dict[str, object]]


@router.get("/market/history", response_model=MarketHistoryV1)
async def get_market_history(
    view: ViewDep,
    window: Annotated[Literal["15m", "1h", "4h", "session"], Query()] = "1h",
) -> MarketHistoryV1:
    points = market_history_dtos(view, window=_WINDOWS[window])
    changes: list[dict[str, object]] = []
    previous: str | None = None
    for index, point in enumerate(points):
        regime = str(point["regime"])
        if regime != previous:
            changes.append({"index": index, "at": point["at"], "regime": regime})
            previous = regime
    return MarketHistoryV1(window=window, points=points, regime_changes=changes)


# ----------------------------------------------------------------- radio


@router.get("/radio/current", response_model=NowPlayingV1 | None)
async def get_radio_current(view: ViewDep) -> NowPlayingV1 | None:
    return now_playing_to_dto(_require_station(view))


@router.get("/radio/queue", response_model=list[QueueItemV1])
async def get_radio_queue(view: ViewDep) -> list[QueueItemV1]:
    return list(queue_to_dtos(_require_station(view)))


@router.get("/radio/buffer", response_model=BufferV1)
async def get_radio_buffer(view: ViewDep) -> BufferV1:
    station = _require_station(view)
    snapshot = station.queue.snapshot()  # type: ignore[attr-defined]
    return buffer_to_dto(
        station.assess_buffer(),  # type: ignore[attr-defined]
        ready_tracks=snapshot.ready_count,
        pending_tracks=max(0, snapshot.depth - snapshot.ready_count),
    )


@router.get("/radio/emergency", response_model=EmergencyV1)
async def get_radio_emergency(view: ViewDep) -> EmergencyV1:
    return emergency_to_dto(_require_station(view))


class HistoryEntryV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    track_id: str
    genre: str
    secondary_genre: str | None = None
    bpm: int
    musical_key: str
    duration_seconds: float
    is_instrumental: bool
    primary_topic: str | None = None
    persona_id: str | None = None
    energy_at_generation: float
    regime_at_generation: str
    played_at: object | None = None


@router.get("/radio/history", response_model=list[HistoryEntryV1])
async def get_radio_history(
    view: ViewDep,
    limit: Annotated[int, Query(ge=1, le=400)] = 50,
) -> list[HistoryEntryV1]:
    station = _require_station(view)
    entries = station.history().entries[:limit]  # type: ignore[attr-defined]
    return [
        HistoryEntryV1(
            track_id=entry.track_id,
            genre=entry.genre,
            secondary_genre=entry.secondary_genre,
            bpm=entry.bpm,
            musical_key=entry.musical_key,
            duration_seconds=entry.duration_seconds,
            is_instrumental=entry.is_instrumental,
            primary_topic=entry.primary_topic,
            persona_id=entry.persona_id,
            energy_at_generation=entry.energy_at_generation,
            regime_at_generation=entry.regime_at_generation,
            played_at=entry.played_at,
        )
        for entry in entries
    ]


# ----------------------------------------------------------------- controls


class ControlResultV1(BaseModel):
    """What a control actually did. Never just ``{"ok": true}``.

    The operator pressed a button that changes the broadcast; they are owed a sentence saying
    what changed, and the UI shows it. "Accepted" is not an outcome.
    """

    model_config = ConfigDict(frozen=True)

    applied: bool
    message: str
    track_id: str | None = None
    lock: str | None = None


@router.post(
    "/radio/skip",
    response_model=ControlResultV1,
    summary="End the current track at the next block boundary",
)
async def post_radio_skip(view: ViewDep) -> ControlResultV1:
    """Requests a skip. It does **not** touch the audio path.

    The engine notices the request on its next pump and finishes the track there, so the skip
    costs at most one block and cannot interrupt a write in progress. That is the whole reason
    this is a request rather than a stop.
    """
    station = _require_station(view)
    playout = station.playout  # type: ignore[attr-defined]
    item = playout.current
    if item is None:
        return ControlResultV1(applied=False, message="Nothing is playing.")
    playout.request_skip()
    _log.info("api.skip_requested", track_id=item.track_id)
    return ControlResultV1(
        applied=True,
        message=f"{item.track_id} will end at the next block boundary.",
        track_id=item.track_id,
    )


@router.post(
    "/radio/previous",
    response_model=ControlResultV1,
    summary="Replay the previous track, or restart the current one",
)
async def post_radio_previous(view: ViewDep) -> ControlResultV1:
    """Like skip, a request the engine honours at the next block boundary.

    The engine replays the last track from the audio it still holds; if nothing suitable
    came before, the current track restarts. Nothing in the queue is touched.
    """
    station = _require_station(view)
    playout = station.playout  # type: ignore[attr-defined]
    item = playout.current
    if item is None:
        return ControlResultV1(applied=False, message="Nothing is playing.")
    track_id = playout.request_previous()
    _log.info("api.previous_requested", track_id=track_id)
    if track_id == item.track_id:
        return ControlResultV1(
            applied=True, message=f"{track_id} will restart.", track_id=track_id
        )
    return ControlResultV1(
        applied=True, message=f"Going back to {track_id}.", track_id=track_id
    )


@router.post(
    "/radio/mute",
    response_model=ControlResultV1,
    summary="Toggle the operator mute",
)
async def post_radio_mute(view: ViewDep) -> ControlResultV1:
    """Silences the output without stopping playout.

    The engine keeps writing blocks, zeroed, so the clock, the play records and the
    transitions are unaffected; unmuting resumes mid-track. A toggle rather than a setter so
    one button with no state of its own cannot drift from the engine.
    """
    station = _require_station(view)
    playout = station.playout  # type: ignore[attr-defined]
    muted = not playout.muted
    playout.set_muted(muted)
    _log.info("api.mute_toggled", muted=muted)
    return ControlResultV1(
        applied=True, message="Output muted." if muted else "Output unmuted."
    )


class VolumeRequestV1(BaseModel):
    """Operator volume as linear gain."""

    model_config = ConfigDict(frozen=True)

    volume: float = Field(ge=0.0, le=1.0)


@router.post(
    "/radio/volume",
    response_model=ControlResultV1,
    summary="Set the operator volume",
)
async def post_radio_volume(request: VolumeRequestV1, view: ViewDep) -> ControlResultV1:
    """Linear gain applied to every block before the sink, heard within one block.

    Separate from mute so the two cannot fight: mute is a switch, volume is a level, and
    unmuting returns to the level that was set. The mastered file is untouched.
    """
    station = _require_station(view)
    playout = station.playout  # type: ignore[attr-defined]
    applied = playout.set_volume(request.volume)
    _log.info("api.volume_set", volume=applied)
    return ControlResultV1(applied=True, message=f"Volume {round(applied * 100)}%.")


@router.post("/radio/queue/{track_id}/lock", response_model=ControlResultV1)
async def post_queue_lock(track_id: str, view: ViewDep) -> ControlResultV1:
    """Pin a slot so a replan cannot touch it (§28's ``OPERATOR_PINNED``)."""
    station = _require_station(view)
    queue = station.queue  # type: ignore[attr-defined]
    if not queue.contains(track_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{track_id} is not in the queue.",
        )
    queue.set_lock(
        track_id, QueueLockLevel.OPERATOR_PINNED, reason="pinned by the operator"
    )
    _log.info("api.queue_locked", track_id=track_id)
    return ControlResultV1(
        applied=True,
        message=f"{track_id} is pinned and will survive a replan.",
        track_id=track_id,
        lock=QueueLockLevel.OPERATOR_PINNED.value,
    )


@router.post("/radio/queue/{track_id}/unlock", response_model=ControlResultV1)
async def post_queue_unlock(track_id: str, view: ViewDep) -> ControlResultV1:
    """Release an operator pin, returning the slot to its positional lock.

    The level is **not** set to ``REPLACEABLE``: §28 computes lock level from position and
    readiness, so forcing a value here would contradict the queue on its own rule. Clearing
    the pin and letting the queue recompute is the only correct unlock.
    """
    station = _require_station(view)
    queue = station.queue  # type: ignore[attr-defined]
    entry = queue.get(track_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{track_id} is not in the queue.",
        )
    if entry.lock_level is not QueueLockLevel.OPERATOR_PINNED:
        return ControlResultV1(
            applied=False,
            message=(
                f"{track_id} is not pinned; its {entry.lock_level.value} lock comes from its "
                "position and cannot be removed."
            ),
            track_id=track_id,
            lock=entry.lock_level.value,
        )
    queue.set_lock(track_id, QueueLockLevel.REPLACEABLE, reason="unpinned by the operator")
    recomputed = queue.get(track_id)
    level = recomputed.lock_level.value if recomputed else QueueLockLevel.REPLACEABLE.value
    _log.info("api.queue_unlocked", track_id=track_id, lock=level)
    return ControlResultV1(
        applied=True,
        message=f"{track_id} is unpinned; the queue recomputed its lock as {level}.",
        track_id=track_id,
        lock=level,
    )


# ----------------------------------------------------------------- simulation


class SimulationRequestV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    scenario: str = Field(min_length=1, max_length=48)


class SimulationResultV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    applied: bool
    scenario: str
    message: str
    available: list[str]


@router.get("/simulation/scenarios", response_model=SimulationResultV1)
async def get_scenarios(view: ViewDep) -> SimulationResultV1:
    from tradefix_radio.market.simulation import Scenario  # noqa: PLC0415

    available = [s.value for s in Scenario]
    report = view.capabilities.get(Capability.SIMULATION)
    return SimulationResultV1(
        applied=False,
        scenario="",
        message=report.detail if report else "Simulation state is unknown.",
        available=available if (report and report.is_ready) else [],
    )


@router.post("/simulation/regime", response_model=SimulationResultV1)
async def post_simulation_regime(
    request: SimulationRequestV1, view: ViewDep
) -> SimulationResultV1:
    """Switch the simulator's scenario.

    Refused outside development and simulation modes, and refused by the *capability* rather
    than by a config lookup here — §72 makes production's feed non-simulated by construction,
    so there is nothing to switch and pretending otherwise would be the start of a dashboard
    that can lie about which feed it is showing.
    """
    from tradefix_radio.market.simulation import Scenario  # noqa: PLC0415

    report = view.capabilities.get(Capability.SIMULATION)
    if report is None or not report.is_ready:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                report.detail
                if report
                else "Simulation controls are not available in this run mode."
            ),
        )
    try:
        scenario = Scenario(request.scenario)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown scenario {request.scenario!r}.",
        ) from error

    service = view.market_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No market feed is attached to this API process.",
        )
    feed = service.feed  # type: ignore[attr-defined]
    switch = getattr(feed, "set_scenario", None)
    if switch is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"The attached {type(feed).__name__} is not a simulator and has no scenarios."
            ),
        )
    switch(scenario)
    _log.info("api.simulation_scenario", scenario=scenario.value)
    return SimulationResultV1(
        applied=True,
        scenario=scenario.value,
        message=f"The simulator is now running the {scenario.value!r} scenario.",
        available=[s.value for s in Scenario],
    )


# ------------------------------------------------------------- market routing


class MarketClosureRequestV1(BaseModel):
    """Simulator control: override a symbol's calendar verdict, or release it.

    ``state`` is the expressive form. ``closed`` is kept because it was the original shape
    of this endpoint and is the common case; the two must not both be sent.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1, max_length=32)
    #: ``open`` | ``closed`` | ``auto``. ``auto`` clears the override.
    state: Literal["open", "closed", "auto"] | None = None
    closed: bool | None = None

    @model_validator(mode="after")
    def _one_form(self) -> MarketClosureRequestV1:
        if self.state is not None and self.closed is not None:
            raise ValueError("send either `state` or `closed`, not both")
        return self

    @property
    def resolved(self) -> str:
        if self.state is not None:
            return self.state
        return "auto" if self.closed is False else "closed"


def _require_routing(view: RuntimeView) -> object:
    routing = view.routing
    if routing is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Market routing is not attached to this API process.",
        )
    return routing


@router.get("/markets", response_model=ActiveMarketV1)
async def get_markets(view: ViewDep) -> ActiveMarketV1:
    """Which market is on air, and how every configured market is doing."""
    _require_routing(view)
    dto = routing_to_dto(view)
    if dto is None:  # pragma: no cover - _require_routing already raised
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Market routing is not attached to this API process.",
        )
    return dto


@router.post("/simulation/market-closure", response_model=ActiveMarketV1)
async def post_market_closure(
    request: MarketClosureRequestV1, view: ViewDep
) -> ActiveMarketV1:
    """Force a symbol closed, or reopen it, so the switch path can be exercised.

    Gated on the same capability as the scenario control, for the same reason: in
    production the market calendar is not ours to invent, and an endpoint that could tell
    the station gold was shut would be a way to make the dashboard lie about the market.

    The response is the routing state *after* the override is applied and re-evaluated —
    which will usually still show the old active symbol, because the confirmation window
    has not elapsed. That is the honest answer, and showing it is how the hysteresis
    becomes visible rather than looking like the control did nothing.
    """
    report = view.capabilities.get(Capability.SIMULATION)
    if report is None or not report.is_ready:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                report.detail
                if report
                else "Simulation controls are not available in this run mode."
            ),
        )
    routing = _require_routing(view)
    symbol = request.symbol.upper()
    known = {name.upper() for name in routing.services}  # type: ignore[attr-defined]
    if symbol not in known:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{symbol!r} is not a configured market. Configured: {sorted(known)}.",
        )

    wanted = request.resolved
    if wanted == "closed":
        routing.force_closed(symbol)  # type: ignore[attr-defined]
    elif wanted == "open":
        routing.force_open(symbol)  # type: ignore[attr-defined]
    else:
        # Clearing is expressed through either method; both drop the entry.
        routing.force_closed(symbol, closed=False)  # type: ignore[attr-defined]
    await routing.evaluate()  # type: ignore[attr-defined]
    _log.info("api.market_override", symbol=symbol, state=wanted)
    dto = routing_to_dto(view)
    assert dto is not None
    return dto


# ------------------------------------------------------------ human feedback


#: The shorthand reasons the UI offers. Free text is accepted in ``note`` instead.
#:
#: A fixed list because an operator rating a track every three minutes will not type, and
#: because counting "boring" across a session is only possible if everyone spells it the
#: same way. ``note`` carries anything the list does not.
FEEDBACK_REASONS: Final = (
    "great",
    "boring",
    "wrong_genre",
    "wrong_energy",
    "bad_vocals",
    "bad_lyrics",
    "too_repetitive",
    "distorted",
    "transition_bad",
    "transition_good",
    "branding_awkward",
    "excellent",
)


class FeedbackRequestV1(BaseModel):
    """One human verdict on one track."""

    model_config = ConfigDict(frozen=True)

    track_id: str = Field(min_length=1, max_length=64)
    verdict: Literal["good", "bad", "skip"]
    reason: str | None = Field(default=None, max_length=48)
    note: str | None = Field(default=None, max_length=500)
    #: Seconds into the track, if the UI knows. Never invented server-side.
    elapsed_seconds: float | None = Field(default=None, ge=0.0)


class FeedbackResultV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    recorded: bool
    track_id: str
    verdict: str
    message: str
    reasons: tuple[str, ...] = ()


@router.get("/radio/feedback/reasons", response_model=FeedbackResultV1)
async def get_feedback_reasons(view: ViewDep) -> FeedbackResultV1:
    """The shorthand vocabulary, so the UI does not hard-code it."""
    _require_database(view)
    return FeedbackResultV1(
        recorded=False,
        track_id="",
        verdict="",
        message="Offered reasons for operator feedback.",
        reasons=FEEDBACK_REASONS,
    )


@router.post("/radio/feedback", response_model=FeedbackResultV1)
async def post_feedback(request: FeedbackRequestV1, view: ViewDep) -> FeedbackResultV1:
    """Record a human verdict on a track that aired.

    Stored and nothing more. It does not change QC thresholds, does not influence the
    director, and does not cause a skip — a ``skip`` verdict *records* that the operator
    skipped, and the skip itself is the separate playout control. Preference feeding back
    into the technical gates automatically is exactly what must not happen: a run of
    "boring" would start moving thresholds that exist to catch clipping and silence.
    """
    database = _require_database(view)
    market = None
    service = view.market_service
    if service is not None:
        market = service.current_state  # type: ignore[attr-defined]

    reason = request.reason
    if reason is not None and reason not in FEEDBACK_REASONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Unknown reason {reason!r}. Offered: {', '.join(FEEDBACK_REASONS)}. "
                "Use `note` for anything else."
            ),
        )

    from tradefix_radio.persistence.models import OperatorFeedback  # noqa: PLC0415

    async with database.session() as session:  # type: ignore[attr-defined]
        session.add(
            OperatorFeedback(
                at=datetime.now(tz=UTC),
                track_id=request.track_id,
                verdict=request.verdict,
                reason=reason,
                note=request.note,
                symbol=None if market is None else market.symbol,
                market_regime=None if market is None else market.regime.value,
                elapsed_seconds=request.elapsed_seconds,
            )
        )

    _log.info(
        "api.operator_feedback",
        track_id=request.track_id,
        verdict=request.verdict,
        reason=reason,
        symbol=None if market is None else market.symbol,
    )
    return FeedbackResultV1(
        recorded=True,
        track_id=request.track_id,
        verdict=request.verdict,
        message=f"Recorded {request.verdict} for {request.track_id}.",
        reasons=FEEDBACK_REASONS,
    )


class FeedFaultRequestV1(BaseModel):
    """Simulator control: stop or restart one symbol's market feed."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1, max_length=32)
    #: False stops the feed (simulating a broker outage); True restarts it.
    enabled: bool


@router.post("/simulation/feed-fault", response_model=ActiveMarketV1)
async def post_feed_fault(request: FeedFaultRequestV1, view: ViewDep) -> ActiveMarketV1:
    """Break or restore a market feed, without touching the calendar.

    Distinct from the closure override on purpose, and the distinction is the point: this
    endpoint makes a market's *data* stop while the market remains open, which must leave
    the station exactly where it is. An operator who could only simulate closures could
    never check that.
    """
    report = view.capabilities.get(Capability.SIMULATION)
    if report is None or not report.is_ready:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                report.detail
                if report
                else "Simulation controls are not available in this run mode."
            ),
        )
    routing = _require_routing(view)
    symbol = request.symbol.upper()
    known = {name.upper() for name in routing.services}  # type: ignore[attr-defined]
    if symbol not in known:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{symbol!r} is not a configured market. Configured: {sorted(known)}.",
        )

    await routing.set_feed_enabled(symbol, enabled=request.enabled)  # type: ignore[attr-defined]
    await routing.evaluate()  # type: ignore[attr-defined]
    _log.warning("api.feed_fault", symbol=symbol, enabled=request.enabled)
    dto = routing_to_dto(view)
    assert dto is not None
    return dto
