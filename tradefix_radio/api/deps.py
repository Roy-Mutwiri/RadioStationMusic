"""Dependency wiring for the API.

One `RuntimeView` per process, attached to `app.state` at startup and handed to routes by
FastAPI's dependency system. No module-level singleton: Phase 1 made that choice for the
database and the reasoning carries over — a global would be the first thing to break when a
test wants a throwaway station, and the API test suite builds one per test.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from tradefix_radio.api.snapshot import RuntimeView

__all__ = ["get_view", "set_view"]

_STATE_KEY = "tradefix_view"


def set_view(app_state: object, view: RuntimeView) -> None:
    setattr(app_state, _STATE_KEY, view)


def get_view(request: Request) -> RuntimeView:
    view = getattr(request.app.state, _STATE_KEY, None)
    if view is None:  # pragma: no cover - a misconfigured app, not a runtime condition
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The API has no runtime attached.",
        )
    assert isinstance(view, RuntimeView)
    return view
