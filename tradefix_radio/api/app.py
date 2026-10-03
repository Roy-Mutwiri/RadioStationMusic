"""The FastAPI application.

Assembled by a factory rather than created at import time, because the API has to be
constructable three ways: around a live station (the `dev` runner), around a station built by
a test fixture, and — for the frontend's `vite dev` loop — around nothing at all, serving
capability reports that say so. A module-level `app` would make the first case the only one.

**The API is a reader.** It holds a `RuntimeView` and three mutating endpoints, all of which
call methods Phase 4 already shipped. It does not own the station, does not start it, and
cannot restart it; the runner does that and hands the objects over. That separation is what
keeps the accepted runtime unchanged by Phase 5.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

import structlog
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from tradefix_radio import __version__
from tradefix_radio.api.deps import set_view
from tradefix_radio.api.live import LiveHub
from tradefix_radio.api.routes import engineering, station
from tradefix_radio.api.snapshot import RuntimeView

_log = structlog.get_logger(__name__)

__all__ = ["create_app"]

#: Where the built frontend lands. Served by the API so a deployment is one process.
_FRONTEND_DIST: Final = Path(__file__).resolve().parents[2] / "frontend" / "dist"

#: Origins allowed during frontend development.
#:
#: Narrow and explicit. Vite serves on 5173 and the API on 8000, so the browser treats them as
#: different origins and will not send the request without this. In production the frontend is
#: served *by* this app from the same origin, so CORS is not involved at all — which is why
#: this list is a development affordance and not a security boundary.
_DEV_ORIGINS: Final = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


def create_app(view: RuntimeView, *, serve_frontend: bool = True) -> FastAPI:
    """Build the application around an already-constructed runtime view."""
    hub = LiveHub(view)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await hub.start()
        try:
            yield
        finally:
            await hub.stop()

    app = FastAPI(
        title="TRADE FIX RADIO",
        description="Control Center API. The market composes the radio.",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    set_view(app.state, view)
    app.state.live_hub = hub

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(_DEV_ORIGINS),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    app.include_router(station.router, prefix="/api", tags=["station"])
    app.include_router(engineering.router, prefix="/api", tags=["engineering"])

    @app.get("/api/health", tags=["station"])
    async def health() -> JSONResponse:
        """A liveness probe that does not touch the station.

        Deliberately shallow: this answers "is the API process up", and a deep check here
        would make a slow database able to mark a healthy API as down. The station's own
        health is in `/api/status`, where it belongs.
        """
        return JSONResponse({"status": "ok", "version": __version__})

    @app.websocket("/ws")
    async def live(socket: WebSocket) -> None:
        """One socket per browser, pushing the two cadences the hub produces.

        The client is sent a full frame immediately on connect rather than waiting for the
        next tick, so opening the dashboard shows the station rather than a spinner. After
        that it only receives; the one thing it may send is ``ping``, which exists so a proxy
        that kills idle connections does not kill a working one.
        """
        await socket.accept()
        with hub.attach() as queue:
            try:
                await socket.send_text(hub.snapshot_envelope().model_dump_json())
                while True:
                    envelope = await queue.get()
                    await socket.send_text(envelope.model_dump_json())
            except WebSocketDisconnect:
                _log.debug("ws.disconnected")
            except Exception as error:  # noqa: BLE001 - one socket must not take the app down
                _log.warning(
                    "ws.failed",
                    error_type=type(error).__name__,
                    error=str(error),
                )

    if serve_frontend and _FRONTEND_DIST.is_dir():
        _mount_frontend(app)
    elif serve_frontend:
        _log.info(
            "api.frontend_not_built",
            path=str(_FRONTEND_DIST),
            detail="serving the API only; run `npm run build` in frontend/ to serve the UI",
        )

    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built single-page app, with history fallback.

    The fallback is what makes ``/radio`` and ``/overlay/live`` work on a hard refresh: the
    browser asks the server for a path only the client router knows about, and without this it
    gets a 404 instead of the app. API and websocket paths are excluded, so a mistyped
    endpoint still returns a real error rather than silently serving HTML — which is a
    genuinely confusing way to debug a request.
    """
    app.mount(
        "/assets",
        StaticFiles(directory=_FRONTEND_DIST / "assets"),
        name="assets",
    )
    index = _FRONTEND_DIST / "index.html"

    # ``response_model=None`` because the return is a union of two Response types, which
    # FastAPI otherwise tries to turn into a pydantic field and refuses at import time.
    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    async def spa(full_path: str) -> FileResponse | JSONResponse:
        if full_path.startswith(("api/", "ws")):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        candidate = _FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)

    _log.info("api.frontend_mounted", path=str(_FRONTEND_DIST))
