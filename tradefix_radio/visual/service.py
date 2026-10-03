"""The visual process: serves the runtime, pushes commands, exposes controls.

Its own OS process on its own port (ADR-11). Four responsibilities and no others:

* serve `visual/runtime/` — the page an OBS browser source points at
* serve the scene derived from the frozen blockout
* push `VisualCommandV1` frames over a WebSocket at up to 20 Hz, and accept telemetry back
* expose the V9 control surface

**It never writes to the station.** The bridge is a read-only client of an endpoint the
station already serves, and the visual layer's whole relationship with the radio is that
one socket. If this process dies, music keeps playing; if the station dies, the character
degrades to neutral idle (`STATE_BRIDGE.md` §9).

Why a separate app rather than routes on the station's API
----------------------------------------------------------
A reverse proxy would put the visual layer's availability inside the station's request
path, and a 24/7 radio should not be able to serve a slow page because a renderer is
wedged. The console reaches this by configured base URL and CORS, following the narrow
explicit `_DEV_ORIGINS` precedent in `api/app.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import structlog
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from tradefix_radio.core.clock import Clock, SystemClock
from tradefix_radio.visual.bridge import VisualStateBridge, default_station_url
from tradefix_radio.visual.camera import CAMERA_METADATA
from tradefix_radio.visual.catalog import CATALOG, SCHEDULABLE
from tradefix_radio.visual.director import TICK_SECONDS, BehaviorDirector
from tradefix_radio.visual.modulation import rhythm_policy
from tradefix_radio.visual.renderer import (
    PROTOCOL_VERSION,
    TARGET_FPS,
    TARGET_HEIGHT,
    TARGET_WIDTH,
    QualityController,
    action_command,
    camera_command,
    decode_telemetry,
    encode,
    gaze_command,
    resync_commands,
    rhythm_command,
    screen_command,
)
from tradefix_radio.visual.scene import build_scene
from tradefix_radio.visual.simulate import SCENARIOS

_log = structlog.get_logger(__name__)

#: Where the placeholder runtime lives.
RUNTIME_DIR: Final = Path(__file__).resolve().parents[2] / "visual" / "runtime"

#: Default port. Deliberately not the station's 8080.
DEFAULT_PORT: Final = 8090

#: Console origins permitted to reach the control API. Narrow and explicit, following
#: the station's own precedent — a development affordance, not a security boundary.
CONSOLE_ORIGINS: Final = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
)

#: How often the screen surfaces are refreshed, seconds. Matches the station's own
#: state cadence; charts do not need to move faster than the data behind them.
SCREEN_INTERVAL: Final = 2.0

#: Telemetry frames retained for `benchmark()`. At one frame per second this is roughly
#: twenty minutes of history, which is longer than any benchmark run needs.
TELEMETRY_HISTORY: Final = 1_200


@dataclass
class VisualRuntime:
    """Owns the director, the bridge and the connected renderers.

    One director regardless of how many renderers attach: behaviour is the performance,
    and two browsers watching must see the same man.
    """

    clock: Clock = field(default_factory=SystemClock)
    seed: int | None = None
    station_url: str = field(default_factory=default_station_url)
    #: When set, the station socket is not used and this scenario drives the director.
    #: Every simulated frame is badged, so a test mode cannot be streamed by accident.
    scenario: str | None = None

    director: BehaviorDirector = field(init=False)
    bridge: VisualStateBridge = field(init=False)
    quality: QualityController = field(init=False)

    _clients: set[Any] = field(default_factory=set, init=False)
    _sequence: int = field(default=0, init=False)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list, init=False)
    _running: bool = field(default=False, init=False)
    _last_screen: float = field(default=0.0, init=False)
    _last_policy: str | None = field(default=None, init=False)
    #: Optional read-only station socket, started and stopped with the runtime.
    link: Any = field(default=None, init=False)
    #: Retained telemetry frames, for `tradefix visual benchmark`. Bounded, because this
    #: process runs for weeks and an unbounded list is a slow leak with a nice name.
    _telemetry: deque[Any] = field(
        default_factory=lambda: deque(maxlen=TELEMETRY_HISTORY), init=False
    )

    def __post_init__(self) -> None:
        rng = random.Random(self.seed) if self.seed is not None else None  # noqa: S311
        self.director = BehaviorDirector(clock=self.clock, rng=rng)
        self.bridge = VisualStateBridge(clock=self.clock)
        self.quality = QualityController()

    # ------------------------------------------------------------ state

    def current_state(self) -> Any:
        """The state the director should see this tick."""
        if self.scenario is not None:
            return SCENARIOS[self.scenario].state(self.clock.now())
        return self.bridge.state_or_degraded()

    def snapshot(self) -> dict[str, Any]:
        """What the V9 control page reads."""
        director = self.director
        shot = director.snapshot()
        decision = director.last_camera_decision
        return {
            "protocol": PROTOCOL_VERSION,
            "mode": "simulated" if self.scenario else "live",
            "scenario": self.scenario,
            "renderers": len(self._clients),
            "sequence": self._sequence,
            "character": {
                "state": shot.character_state.value,
                "action": shot.current_action,
                "chain": shot.current_chain,
                "gaze": shot.current_gaze.value,
                "locks": list(shot.held_locks),
                "next_action_in": shot.next_action_in_seconds,
                "actions_performed": shot.actions_performed,
                "recent": list(shot.recent_actions),
            },
            "rhythm": director.rhythm.snapshot(),
            "drive": {
                "behavior_energy": shot.behavior_energy,
                "intensity_band": shot.intensity_band.value,
                "activity": shot.activity,
                "reactions_this_hour": shot.reactions_this_hour,
            },
            "camera": {
                "live": director.camera.camera_id,
                "auto": director.camera.auto,
                "hold_target": round(director.camera_director.hold_seconds, 1),
                "held_for": round(
                    director.camera.hold_seconds(self.clock.monotonic()), 1
                ),
                "recent": list(director.camera.recent[-6:]),
                "last_reason": decision.reason if decision else None,
                "last_motivation": (
                    decision.motivation.value
                    if decision and decision.motivation
                    else None
                ),
            },
            "bridge": {
                "connected": self.bridge.stats.connected,
                "frames": self.bridge.stats.frames_received,
                "rejected": self.bridge.stats.frames_rejected,
                "reconnects": self.bridge.stats.reconnects,
                "last_error": self.bridge.stats.last_error,
            },
            "quality": {
                "profile": self.quality.profile,
                "renderer_alive": self.quality.renderer_alive(self.clock.monotonic()),
            },
            "target": {
                "width": TARGET_WIDTH,
                "height": TARGET_HEIGHT,
                "fps": TARGET_FPS,
            },
        }

    # ------------------------------------------------------------ clients

    def attach(self, socket: Any) -> None:
        self._clients.add(socket)

    def detach(self, socket: Any) -> None:
        self._clients.discard(socket)

    async def _broadcast(self, commands: list[dict[str, Any]]) -> None:
        """Push to every renderer, dropping rather than queueing for a slow one.

        Same reasoning the station's `LiveHub` applies to browsers: a client that is
        behind does not want the backlog, it wants the current state. A renderer stall
        must never apply back-pressure to the director.
        """
        if not commands or not self._clients:
            return
        payload = encode(commands)
        dead: list[Any] = []
        for socket in self._clients:
            try:
                await socket.send_text(payload)
            except Exception:  # noqa: BLE001 - one dead socket must not stop the rest
                dead.append(socket)
        for socket in dead:
            self.detach(socket)

    def ingest_telemetry(self, payload: str) -> None:
        try:
            telemetry = decode_telemetry(payload)
        except Exception as error:  # noqa: BLE001 - a bad frame is reported, not fatal
            _log.warning(
                "visual.telemetry_rejected",
                error_type=type(error).__name__,
                error=str(error),
            )
            return
        self._telemetry.append(telemetry)
        shed = self.quality.observe(telemetry, self.clock.monotonic())
        if shed is not None:
            _log.info("visual.quality_shed", profile=shed)

    def benchmark(self) -> dict[str, Any]:
        """Summarise retained telemetry into the table `V8_REPORT.md` §D wants.

        Reported from the renderer's own frame timings rather than from a wall-clock
        guess outside the browser. The GPU-side figures this cannot see — GPU
        utilisation and total VRAM — have to come from Task Manager or OBS's stats
        panel alongside it; `gl_memory_mb` is only what the renderer itself allocated.
        """
        frames = list(self._telemetry)
        if not frames:
            return {"samples": 0, "note": "no renderer has reported telemetry yet"}

        def spread(values: list[float]) -> dict[str, float]:
            ordered = sorted(values)
            return {
                "min": round(ordered[0], 3),
                "mean": round(sum(ordered) / len(ordered), 3),
                "p95": round(ordered[int(len(ordered) * 0.95) - 1], 3),
                "max": round(ordered[-1], 3),
            }

        return {
            "samples": len(frames),
            "profile": frames[-1].quality_profile,
            "frames_rendered": frames[-1].frames_rendered,
            "fps_mean": spread([frame.fps_mean for frame in frames]),
            "fps_p05": spread([frame.fps_p05 for frame in frames]),
            "frame_time_p95_ms": spread([frame.frame_time_p95_ms for frame in frames]),
            "gl_memory_mb": spread([frame.gl_memory_mb or 0.0 for frame in frames]),
            "dropped_frames": frames[-1].dropped_frames,
            "target": {"width": TARGET_WIDTH, "height": TARGET_HEIGHT, "fps": TARGET_FPS},
        }

    # ------------------------------------------------------------ the loop

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [asyncio.create_task(self._loop(), name="visual-director")]
        if self.link is not None:
            await self.link.start()
        _log.info(
            "visual.runtime_started",
            mode="simulated" if self.scenario else "live",
            station=self.station_url if not self.scenario else None,
        )

    async def stop(self) -> None:
        self._running = False
        if self.link is not None:
            await self.link.stop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        _log.info("visual.runtime_stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - a bad tick must not stop the loop
                _log.error(
                    "visual.tick_failed",
                    error_type=type(error).__name__,
                    error=str(error),
                    exc_info=True,
                )
            await self.clock.sleep(TICK_SECONDS)

    async def _tick(self) -> None:
        state = self.current_state()
        output = self.director.tick(state)
        commands: list[dict[str, Any]] = []

        for action in output.actions:
            commands.append(action_command(action))
        for shift in output.gaze_shifts:
            commands.append(gaze_command(shift))
        if output.camera_cut is not None:
            self._sequence += 1
            commands.append(
                camera_command(
                    output.camera_cut.camera_id,
                    sequence=self._sequence,
                    transition=output.camera_cut.transition,
                    parallax=CAMERA_METADATA[
                        output.camera_cut.camera_id
                    ].parallax_amplitude,
                )
            )

        now = self.clock.monotonic()
        if now - self._last_screen >= SCREEN_INTERVAL:
            self._last_screen = now
            self._sequence += 1
            commands.append(screen_command(state, sequence=self._sequence))
            policy = rhythm_policy(state)
            signature = f"{policy.bpm}:{policy.beat_subdivision}"
            if signature != self._last_policy:
                self._last_policy = signature
                self._sequence += 1
                commands.append(rhythm_command(policy, sequence=self._sequence))

        await self._broadcast(commands)

    def resync(self) -> list[dict[str, Any]]:
        """Everything a freshly-attached renderer needs."""
        self._sequence += 1
        return resync_commands(
            sequence=self._sequence,
            character_state=self.director.character_state,
            gaze=self.director.gaze_state.target,
            camera_id=self.director.camera.camera_id,
            running=self.director.running_actions,
            fatigue_phase=self.director.rhythm.fatigue_level,
            policy=rhythm_policy(self.current_state()),
        )


def create_app(runtime: VisualRuntime) -> FastAPI:
    """Build the visual process's own FastAPI app.

    FastAPI types are imported at **module** scope, not here, and that is load-bearing.
    This module uses `from __future__ import annotations`, so every annotation is a
    string that FastAPI resolves with `get_type_hints` against the module globals. With
    `WebSocket` imported as a function local, `socket: WebSocket` on the socket handler
    could not be resolved, FastAPI treated it as a request parameter, and the route
    closed the connection instead of accepting it — a 403 with no traceback anywhere,
    because the handler body never ran.
    """

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(
        title="TRADE FIX VISUAL",
        description="Behaviour director, state bridge and placeholder renderer.",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(CONSOLE_ORIGINS),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/api/visual/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "protocol": PROTOCOL_VERSION})

    @app.get("/api/visual/scene")
    async def scene() -> JSONResponse:
        """The blockout-derived placeholder scene. Cached by the browser per session."""
        return JSONResponse(build_scene().to_json())

    @app.get("/api/visual/state")
    async def state() -> JSONResponse:
        return JSONResponse(runtime.snapshot())

    @app.get("/api/visual/benchmark")
    async def benchmark() -> JSONResponse:
        """Renderer-reported frame timings, summarised. See `V8_REPORT.md` §D."""
        return JSONResponse(runtime.benchmark())

    @app.get("/api/visual/catalog")
    async def catalog() -> JSONResponse:
        return JSONResponse(
            {
                "actions": sorted(CATALOG),
                "schedulable": sorted(SCHEDULABLE),
                "cameras": sorted(CAMERA_METADATA),
                "scenarios": sorted(SCENARIOS),
            }
        )

    @app.post("/api/visual/camera/{camera_id}")
    async def select_camera(camera_id: str) -> JSONResponse:
        """Request a camera. **Queues behind the safety vetoes** — see CAMERA_DIRECTOR §10."""
        try:
            runtime.director.camera_director.request(camera_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return JSONResponse({"requested": camera_id, "queued_behind_vetoes": True})

    @app.post("/api/visual/camera/auto/{enabled}")
    async def camera_auto(enabled: bool) -> JSONResponse:
        runtime.director.camera.auto = enabled
        return JSONResponse({"auto": enabled})

    @app.post("/api/visual/force_idle/{enabled}")
    async def force_idle(enabled: bool) -> JSONResponse:
        runtime.director.force_idle = enabled
        return JSONResponse({"force_idle": enabled})

    @app.post("/api/visual/trigger/{action_id}")
    async def trigger(action_id: str) -> JSONResponse:
        """Fire one action now. Cooldowns bypassed; **locks still respected**."""
        if action_id not in CATALOG:
            raise HTTPException(status_code=404, detail=f"unknown action {action_id!r}")
        emitted = runtime.director.trigger(action_id)
        if emitted is None:
            return JSONResponse(
                {"triggered": False, "reason": "a lock or an object in hand blocked it"},
                status_code=409,
            )
        return JSONResponse({"triggered": True, "action": emitted.action_id})

    @app.websocket("/ws")
    async def renderer_socket(socket: WebSocket) -> None:
        """One socket per renderer: commands out, telemetry in."""
        await socket.accept()
        runtime.attach(socket)
        try:
            await socket.send_text(encode(runtime.resync()))
            while True:
                message = await socket.receive_text()
                runtime.ingest_telemetry(message)
        except WebSocketDisconnect:
            _log.debug("visual.renderer_disconnected")
        except Exception as error:  # noqa: BLE001 - one socket must not take the app down
            _log.warning(
                "visual.renderer_socket_failed",
                error_type=type(error).__name__,
                error=str(error),
            )
        finally:
            runtime.detach(socket)

    if RUNTIME_DIR.is_dir():
        # Mounted LAST so the API and socket routes match first.
        app.mount("/", StaticFiles(directory=RUNTIME_DIR, html=True), name="runtime")
    else:  # pragma: no cover - only if the runtime directory is missing

        @app.get("/")
        async def missing() -> JSONResponse:
            return JSONResponse(
                {"detail": f"runtime not found at {RUNTIME_DIR}"}, status_code=404
            )

    return app


def runtime_url(host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> str:
    """The URL an OBS browser source points at."""
    return f"http://{host}:{port}/"


__all__ = [
    "CONSOLE_ORIGINS",
    "DEFAULT_PORT",
    "RUNTIME_DIR",
    "SCREEN_INTERVAL",
    "VisualRuntime",
    "create_app",
    "runtime_url",
]
