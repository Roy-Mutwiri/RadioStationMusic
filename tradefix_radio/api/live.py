"""The live-state WebSocket hub.

The brief's constraint is specific: *"Do not force the browser to hammer the backend with
one-second REST polling."* So state is **pushed**, and this module owns the one place that
decides what gets pushed and how often.

Two cadences, because the data has two very different rates:

``state``     the whole coherent frame — market, queue, buffer, health, capabilities. Sent on
              a slow tick and whenever something structural changes. It is a few kilobytes,
              and sending it whole is what keeps the dashboard self-consistent: a buffer
              reading and the generator health that explains it always arrive together.
``position``  audio progress alone, a handful of numbers, sent frequently. This is the one
              figure that changes continuously, and giving it its own small message is what
              lets the UI update a progress bar without re-rendering the station.

**The hub never blocks the station.** It reads the runtime through `RuntimeView` on its own
task; a slow or dead browser cannot apply backpressure to the broadcast. A client that cannot
keep up has its queue trimmed and, if it stays behind, is dropped — §86's "one component's
failure must not stop the radio" applies to a websocket as much as to a model.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from datetime import datetime
from typing import Any, Final

import structlog
from pydantic import BaseModel, ConfigDict

from tradefix_radio.api.snapshot import RuntimeView, build_live_state
from tradefix_radio.core.clock import UTC

_log = structlog.get_logger(__name__)

__all__ = ["POSITION_INTERVAL_SECONDS", "STATE_INTERVAL_SECONDS", "LiveEnvelope", "LiveHub"]

#: How often the whole frame is pushed. Slow on purpose — nothing structural changes faster.
STATE_INTERVAL_SECONDS: Final = 2.0

#: How often audio position is pushed. Fast enough for a smooth bar, small enough to be free.
POSITION_INTERVAL_SECONDS: Final = 0.5

#: Messages buffered per client before the slowest get dropped.
#:
#: Small deliberately. A browser that is 32 frames behind does not want the backlog, it wants
#: the *current* state — so the oldest are discarded rather than queued, and live data stays
#: live instead of replaying a minute of history after a stall.
CLIENT_QUEUE_SIZE: Final = 32


class LiveEnvelope(BaseModel):
    """Every websocket message has the same shape, so the client has one parser."""

    model_config = ConfigDict(frozen=True)

    #: ``state`` | ``position`` | ``hello`` | ``pong``
    type: str
    at: datetime
    payload: Any = None


class _Client:
    """One browser connection, with its own bounded mailbox."""

    __slots__ = ("dropped", "queue")

    def __init__(self) -> None:
        self.queue: asyncio.Queue[LiveEnvelope] = asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE)
        self.dropped = 0

    def offer(self, envelope: LiveEnvelope) -> None:
        """Hand over a message, discarding the oldest if this client is behind."""
        if not self.queue.full():
            self.queue.put_nowait(envelope)
            return
        with contextlib.suppress(asyncio.QueueEmpty):
            self.queue.get_nowait()
            self.dropped += 1
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(envelope)


class LiveHub:
    """Fans runtime state out to every connected client.

    One producer task per cadence, regardless of how many browsers are attached: building a
    snapshot is cheap but not free, and doing it per client would make the cost scale with the
    number of open tabs.
    """

    def __init__(
        self,
        view: RuntimeView,
        *,
        state_interval: float = STATE_INTERVAL_SECONDS,
        position_interval: float = POSITION_INTERVAL_SECONDS,
    ) -> None:
        self._view = view
        self._state_interval = state_interval
        self._position_interval = position_interval
        self._clients: set[_Client] = set()
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._state_loop(), name="live-hub-state"),
            asyncio.create_task(self._position_loop(), name="live-hub-position"),
        ]
        _log.info(
            "live_hub.started",
            state_interval=self._state_interval,
            position_interval=self._position_interval,
        )

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        self._clients.clear()
        _log.info("live_hub.stopped")

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # -- client registration ----------------------------------------------

    @contextlib.contextmanager
    def attach(self) -> Iterator[asyncio.Queue[LiveEnvelope]]:
        """Register a client for the life of its connection."""
        client = _Client()
        self._clients.add(client)
        try:
            yield client.queue
        finally:
            self._clients.discard(client)
            if client.dropped:
                _log.info("live_hub.client_lagged", dropped=client.dropped)

    # -- production --------------------------------------------------------

    def snapshot_envelope(self) -> LiveEnvelope:
        """The full frame. Also used for the initial message a client receives.

        A new client gets state immediately rather than waiting up to a tick: an operator
        opening the dashboard should see the station, not a loading state that resolves a
        second later.
        """
        # Builds the frame and nothing else. Market history used to be recorded here, which
        # meant the energy timeline only accumulated *while someone was watching* — open the
        # dashboard after an hour of broadcasting and the chart was empty. Recording belongs
        # with the thing that polls the market, not with the thing that serves browsers.
        state = build_live_state(self._view)
        return LiveEnvelope(type="state", at=state.at, payload=state)

    def position_envelope(self) -> LiveEnvelope | None:
        """Audio position only. ``None`` when nothing is playing."""
        station = self._view.station
        if station is None:
            return None
        playout = station.playout  # type: ignore[attr-defined]
        item = playout.current
        if item is None:
            return None
        duration = max(item.duration_seconds, 0.001)
        elapsed = min(max(playout.position_seconds, 0.0), duration)
        return LiveEnvelope(
            type="position",
            at=datetime.now(tz=UTC),
            payload={
                "track_id": item.track_id,
                "elapsed_seconds": elapsed,
                "remaining_seconds": max(0.0, duration - elapsed),
                "duration_seconds": duration,
                "progress": min(1.0, elapsed / duration),
                "output_peak": playout.stats.peak_sample or None,
            },
        )

    def broadcast(self, envelope: LiveEnvelope) -> None:
        for client in self._clients:
            client.offer(envelope)

    async def _state_loop(self) -> None:
        while self._running:
            try:
                if self._clients:
                    self.broadcast(self.snapshot_envelope())
            except Exception as error:  # noqa: BLE001 - a bad frame must not stop the hub
                _log.error(
                    "live_hub.state_failed",
                    error_type=type(error).__name__,
                    error=str(error),
                    exc_info=True,
                )
            await asyncio.sleep(self._state_interval)

    async def _position_loop(self) -> None:
        while self._running:
            try:
                if self._clients:
                    envelope = self.position_envelope()
                    if envelope is not None:
                        self.broadcast(envelope)
            except Exception as error:  # noqa: BLE001 - see above
                _log.error(
                    "live_hub.position_failed",
                    error_type=type(error).__name__,
                    error=str(error),
                    exc_info=True,
                )
            await asyncio.sleep(self._position_interval)


def _radio_energy(view: RuntimeView) -> float | None:
    """The station's own energy level, for the timeline's second series.

    Read from the queue's next planned track rather than from the director's internal planner:
    the planner's state is its own business, and what the operator wants to see is the energy
    the station has actually *committed* to.
    """
    station = view.station
    if station is None:
        return None
    snapshot = station.queue.snapshot()  # type: ignore[attr-defined]
    playing = snapshot.playing
    if playing is not None:
        return float(playing.blueprint.composition.energy)
    if snapshot.entries:
        return float(snapshot.entries[0].blueprint.composition.energy)
    return None
