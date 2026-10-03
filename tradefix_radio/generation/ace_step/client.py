"""HTTP client for the ACE-Step API server (§7.3 option C).

A thin, typed wrapper over the protocol documented at
``docs/en/API.md`` in `ace-step/ACE-Step-1.5` (commit ``ca1e85fe9430``):

====================  ======  ====================================================
``/health``           GET     liveness
``/v1/stats``         GET     runtime statistics
``/v1/models``        GET     available DiT models
``/v1/init``          POST    load or switch model
``/release_task``     POST    submit a generation, returns ``task_id``
``/query_result``     POST    poll by ``task_id_list``; per-item status 1 = success
``/v1/audio``         GET     download the rendered file
====================  ======  ====================================================

Why a separate process at all is settled in `docs/status/PHASE_7_ENVIRONMENT.md`: ACE-Step
requires Python ≥3.11 and the station is pinned to 3.10 by ADR-01, so in-process is not
available at any price. It is also what §7.5 wants independently — a model that deadlocks
cannot take the playout engine with it if it is not in the same process.

The cancellation gap, stated plainly
------------------------------------
**The API has no cancellation endpoint.** `cancel` therefore stops *this side* waiting and
frees the job slot; the GPU keeps working until the task finishes on its own. The client
says so in its return value rather than reporting success, because a provider that claimed
to have cancelled would let the manager start a second generation into a GPU that is still
busy with the first — turning a cancellation into an OOM.
"""

from __future__ import annotations

import asyncio
import enum
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Final

import structlog

_log = structlog.get_logger(__name__)

__all__ = [
    "AceStepClient",
    "AceStepHttpError",
    "TaskResult",
    "TaskStatus",
]

#: Task status codes, from the documented protocol.
_STATUS_PENDING: Final = 0
_STATUS_SUCCESS: Final = 1
_STATUS_FAILURE: Final = 2

#: How often to poll `/query_result`.
#:
#: 1 s. Generation on this class of GPU is measured in tens of seconds, so a faster poll adds
#: request load for no latency benefit, and a slower one adds dead time to every track.
POLL_INTERVAL_SECONDS: Final = 1.0

#: Timeout for the small control requests (health, stats, models).
#:
#: Short on purpose: these are used to decide whether the service is alive, and a liveness
#: check that can block for a minute is not a liveness check.
CONTROL_TIMEOUT_SECONDS: Final = 10.0


class AceStepHttpError(RuntimeError):
    """The service was reachable but did not answer usefully."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class TaskStatus(str, enum.Enum):
    """Where a submitted task has got to."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"


@dataclass(frozen=True)
class TaskResult:
    """One polled task state."""

    status: TaskStatus
    #: Server-side path or URL of the rendered audio, when finished.
    file: str | None = None
    #: The seed the server actually used — which, with ``use_random_seed``, is not the one
    #: that was sent. §7.13 depends on recording what happened rather than what was asked.
    seed: int | None = None
    error: str | None = None
    metas: dict[str, Any] = field(default_factory=dict)
    #: 0.0-1.0 from the model itself. Real, but coarse — it sits at 0.1 for the whole LM
    #: phase — so it is reported as a number *and* a stage label rather than rendered as a
    #: smooth bar (§7.25).
    progress: float | None = None
    #: Human-readable phase, e.g. "Phase 1: Generating CoT metadata".
    stage: str | None = None
    #: What the model reported about its own run: timings, phase breakdown.
    generation_info: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class AceStepClient:
    """Async client. Every blocking call runs in a thread.

    `urllib` rather than `httpx` or `aiohttp` deliberately: the station's dependency set is
    small and this protocol is six endpoints of JSON over localhost. Adding an HTTP stack to
    the broadcast process to talk to a loopback port is not a trade worth making, and
    `asyncio.to_thread` makes the blocking calls safe for the event loop — the same pattern
    Phase 6 uses for FFmpeg.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        control_timeout: float = CONTROL_TIMEOUT_SECONDS,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._control_timeout = control_timeout

    # ------------------------------------------------------------ plumbing

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None, timeout: float
    ) -> dict[str, Any]:
        """One blocking HTTP round trip. Always called via ``to_thread``."""
        url = f"{self._base}{path}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310 - scheme validated by config
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                body = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:400]
            raise AceStepHttpError(
                f"{method} {path} returned {error.code}: {detail}", status=error.code
            ) from error
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise AceStepHttpError(f"{method} {path} failed: {error}") from error

        if not body:
            return {}
        try:
            decoded = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as error:
            raise AceStepHttpError(f"{method} {path} returned non-JSON") from error
        if not isinstance(decoded, dict):
            raise AceStepHttpError(f"{method} {path} returned {type(decoded).__name__}")
        return decoded

    async def _get(self, path: str, *, timeout: float | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._request, "GET", path, None, timeout or self._control_timeout
        )

    async def _post(
        self, path: str, payload: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._request, "POST", path, payload, timeout or self._control_timeout
        )

    @staticmethod
    def _unwrap(response: dict[str, Any]) -> Any:
        """Pull `data` out of the ``{data, code, error, timestamp}`` envelope.

        Tolerant of a bare payload: the documented envelope is what the server sends, but a
        client that *requires* it would break on a proxy that unwrapped it, and the failure
        would look like a model problem.
        """
        if "code" in response and response.get("code") not in (200, None):
            raise AceStepHttpError(
                f"service returned code {response['code']}: {response.get('error')}"
            )
        data = response.get("data")
        return response if data is None else data

    # ------------------------------------------------------------- control

    async def health(self) -> dict[str, Any]:
        """``/health``. Raises :class:`AceStepHttpError` when unreachable."""
        return dict(self._unwrap(await self._get("/health")) or {})

    async def stats(self) -> dict[str, Any]:
        """``/v1/stats``. Best-effort: an older server may not have it."""
        try:
            return dict(self._unwrap(await self._get("/v1/stats")) or {})
        except AceStepHttpError as error:
            _log.debug("ace_step.stats_unavailable", error=str(error))
            return {}

    async def models(self) -> list[str]:
        """``/v1/models``, normalised to a list of names."""
        data = self._unwrap(await self._get("/v1/models"))
        if isinstance(data, dict):
            data = data.get("models", data.get("dit_models", []))
        if not isinstance(data, list):
            return []
        names: list[str] = []
        for item in data:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("model") or item.get("id")
                if isinstance(name, str):
                    names.append(name)
        return names

    async def init_model(
        self, *, dit_model: str, lm_model: str | None, timeout: float
    ) -> dict[str, Any]:
        """``/v1/init``. Slow — this is where the checkpoint is loaded.

        Field names verified against the running server's OpenAPI schema rather than the
        documentation: ``InitModelRequest`` is ``{model, slot, init_llm, lm_model_path}``.
        An earlier version sent ``lm_model``, which pydantic ignores by default — so the LM
        would simply never have loaded, silently, and the only symptom would have been
        worse captions.
        """
        payload: dict[str, Any] = {"model": dit_model}
        if lm_model:
            payload["init_llm"] = True
            payload["lm_model_path"] = lm_model
        return dict(self._unwrap(await self._post("/v1/init", payload, timeout=timeout)) or {})

    # ---------------------------------------------------------- generation

    async def release_task(self, payload: dict[str, Any], *, timeout: float) -> str:
        """``/release_task``. Returns the task id.

        Submission is expected to return promptly with an id; the work happens afterwards.
        A server that blocks here until the audio is ready still works — the timeout passed
        in is the generation timeout, not the control one — but polling is the documented
        shape and the one this client is built around.
        """
        data = self._unwrap(await self._post("/release_task", payload, timeout=timeout))
        task_id = None
        if isinstance(data, dict):
            task_id = data.get("task_id") or data.get("taskId") or data.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise AceStepHttpError(f"/release_task returned no task id: {data!r:.200}")
        return task_id

    async def query_result(self, task_id: str, *, timeout: float | None = None) -> TaskResult:
        """``/query_result`` for one task.

        Shaped to the server as it actually behaves, which differs from ``docs/en/API.md``
        in two ways that both fail silently:

        * the request field is **``task_id_list``**, not ``task_ids``. The handler reads
          ``body.get("task_id_list", "[]")``, so a request using the documented name parses
          as an empty list and returns ``data: []`` with HTTP 200 — indistinguishable from
          "still running". A generation that had already finished polled until it timed out.
        * ``result`` is a **JSON-encoded string** containing a *list* of per-item records,
          one per ``batch_size``. The per-item ``status`` is the real one.

        Both were found by submitting a task and reading the wire, not from the docs. The
        parsing below is deliberately tolerant of the documented shape as well, so this
        keeps working if a later build aligns with its own documentation.
        """
        data = self._unwrap(
            await self._post(
                "/query_result", {"task_id_list": [task_id]}, timeout=timeout
            )
        )
        entry = _first_task_entry(data, task_id)
        if entry is None:
            return TaskResult(status=TaskStatus.PENDING, raw={})

        item = _first_result_item(entry)
        # Prefer the per-item status; fall back to the envelope's for a build that only
        # sets one of them.
        raw_status = item.get("status", entry.get("status"))
        if raw_status == _STATUS_SUCCESS:
            status = TaskStatus.SUCCESS
        elif raw_status == _STATUS_FAILURE:
            status = TaskStatus.FAILURE
        else:
            status = TaskStatus.PENDING

        return TaskResult(
            status=status,
            file=_as_str(item.get("file") or item.get("audio") or item.get("path")),
            seed=_parse_seed(item.get("seed_value", item.get("seed"))),
            error=_as_str(
                item.get("error") or entry.get("error") or item.get("message")
            ),
            metas=dict(item.get("metas") or {}),
            progress=_as_float(item.get("progress")),
            stage=_as_str(item.get("stage")) or _as_str(entry.get("progress_text")),
            generation_info=_as_str(item.get("generation_info")),
            raw={"entry": entry, "item": item},
        )

    async def download(self, file_ref: str, *, timeout: float) -> bytes:
        """Fetch rendered audio.

        ``file_ref`` may be an absolute URL, a server path, or a bare filename depending on
        the server build, so all three are handled. The caller checks whether the reference
        is a local path it can read directly before reaching for this.
        """
        if file_ref.startswith(("http://", "https://")):
            url = file_ref
        elif file_ref.startswith("/"):
            # What this server actually returns: a ready-made relative URL, already
            # percent-encoded — "/v1/audio?path=D%3A%5Cace-step%5C...". Re-encoding it
            # would double-escape the drive colon and 404.
            url = f"{self._base}{file_ref}"
        else:
            url = f"{self._base}/v1/audio?{urllib.parse.urlencode({'path': file_ref})}"

        def _fetch() -> bytes:
            request = urllib.request.Request(url, headers=self._auth_headers(), method="GET")  # noqa: S310
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                    payload: bytes = response.read()
                    return payload
            except urllib.error.HTTPError as error:
                raise AceStepHttpError(
                    f"audio download returned {error.code}", status=error.code
                ) from error
            except (urllib.error.URLError, OSError, TimeoutError) as error:
                raise AceStepHttpError(f"audio download failed: {error}") from error

        return await asyncio.to_thread(_fetch)

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}


def _first_task_entry(data: Any, task_id: str) -> dict[str, Any] | None:
    """Find this task's record in whichever shape the server used.

    The documented response is a batch query, so the result may be a list, a dict keyed by
    task id, or — for a single task — the record itself. Accepting all three is cheaper than
    being wrong about which one this build sends.
    """
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("task_id") in (task_id, None):
                return item
        return None
    if isinstance(data, dict):
        if task_id in data and isinstance(data[task_id], dict):
            return dict(data[task_id])
        for key in ("results", "tasks", "data"):
            nested = data.get(key)
            if nested is not None:
                found = _first_task_entry(nested, task_id)
                if found is not None:
                    return found
        if "status" in data:
            return data
    return None


def _first_result_item(entry: dict[str, Any]) -> dict[str, Any]:
    """The first per-item record inside the envelope's ``result``.

    ``result`` arrives as a JSON-encoded string holding a list — one entry per
    ``batch_size``. The station always submits ``batch_size: 1``, so the first item is the
    track; taking it explicitly rather than assuming keeps this correct if a caller ever
    raises the batch size, at which point the extra items are simply unused rather than
    silently shadowing the first.
    """
    raw = entry.get("result")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                return item
        return {}
    if isinstance(raw, dict):
        return raw
    # No `result` at all: a build that puts the fields on the envelope directly.
    return entry


def _parse_seed(value: Any) -> int | None:
    """The seed the model actually used.

    ``seed_value`` arrives as ``"101,1437145256"`` — the requested seed, then the one
    derived for the item. The **second** is what produced this audio, so it is the one
    recorded; §7.13 measures what happened, not what was asked for.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",") if part.strip()]
        for candidate in reversed(parts):
            try:
                return int(candidate)
            except ValueError:
                continue
    return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
