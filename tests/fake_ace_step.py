"""A fake ACE-Step API server (§7.30).

Implements the documented protocol — `/health`, `/v1/init`, `/v1/models`, `/v1/stats`,
`/release_task`, `/query_result`, `/v1/audio` — over a real socket, so the client is
exercised through actual HTTP rather than against a mocked method.

Why a real socket and not a stubbed client
------------------------------------------
The things most likely to be wrong in an HTTP integration are the things a stub cannot
reach: JSON envelope handling, how a 500 surfaces, what a timeout does, whether a bare
payload is tolerated, whether the audio download path works. Patching `AceStepClient._post`
would test that the provider calls a method, which was never in doubt.

It renders **real audio** with the Phase 6 fixtures, so a test can take the whole path
through to QC and mastering without a GPU. §7.30: *"Normal CI should not require downloading
the model."*
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from tests.audio_fixtures import musical, silence
from tradefix_radio.audio.io import write_audio

__all__ = ["FakeAceStep", "FakeBehaviour"]


@dataclass
class FakeBehaviour:
    """Knobs for the failure cases §7.19 requires.

    Everything defaults to "works", so a test states only the deviation it cares about.
    """

    #: Fail every task with this message, as the server would report it.
    fail_with: str | None = None
    #: Never finish: `/query_result` stays pending forever. Exercises the timeout path.
    hang: bool = False
    #: Polls to report pending before succeeding. Models a real generation taking time.
    pending_polls: int = 0
    #: Produce a zero-byte file instead of audio.
    zero_byte: bool = False
    #: Produce a file that is not decodable audio.
    corrupt: bool = False
    #: Produce digitally silent audio — valid file, QC must reject it.
    silent_audio: bool = False
    #: Produce audio of this length regardless of what was asked (§7.12 drift).
    force_duration: float | None = None
    #: Return HTTP 500 from `/release_task`.
    reject_submission: bool = False
    #: Make `/health` fail, as a dead service would.
    unhealthy: bool = False
    #: Echo this seed back rather than the one submitted (§7.13).
    override_seed: int | None = None
    #: Return the audio as an absolute local path rather than a download reference.
    serve_local_path: bool = True


@dataclass
class _Task:
    task_id: str
    payload: dict[str, Any]
    polls: int = 0
    path: Path | None = None
    seed: int = 0


@dataclass
class FakeAceStep:
    """A fake service, started on an ephemeral port.

    Use as a context manager::

        with FakeAceStep(tmp_path) as server:
            provider = AceStepProvider(settings_with(server.base_url), clock=clock)
    """

    audio_dir: Path
    behaviour: FakeBehaviour = field(default_factory=FakeBehaviour)
    sample_rate: int = 44_100

    _server: ThreadingHTTPServer | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    #: Every `/release_task` body received, in order. Tests assert on what was *sent*.
    submissions: list[dict[str, Any]] = field(default_factory=list, init=False)
    init_calls: list[dict[str, Any]] = field(default_factory=list, init=False)
    _tasks: dict[str, _Task] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _counter: int = field(default=0, init=False)

    # ------------------------------------------------------------ lifecycle

    def __enter__(self) -> FakeAceStep:
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-ace-step", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    @property
    def port(self) -> int:
        assert self._server is not None, "server not started"
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # -------------------------------------------------------------- the work

    def _create_task(self, payload: dict[str, Any]) -> str:
        with self._lock:
            self._counter += 1
            task_id = f"task-{self._counter}-{uuid.uuid4().hex[:8]}"
            self.submissions.append(dict(payload))
            seed = self.behaviour.override_seed
            if seed is None:
                raw = payload.get("seed", 0)
                seed = int(raw) if isinstance(raw, (int, float, str)) else 0
            self._tasks[task_id] = _Task(task_id=task_id, payload=dict(payload), seed=seed)
        return task_id

    def _render(self, task: _Task) -> Path:
        """Produce the audio file this task's parameters describe."""
        path = self.audio_dir / f"{task.task_id}.wav"
        if self.behaviour.zero_byte:
            path.write_bytes(b"")
            return path
        if self.behaviour.corrupt:
            path.write_bytes(b"not audio at all, just bytes")
            return path

        requested = self.behaviour.force_duration
        if requested is None:
            raw = task.payload.get("audio_duration", 45.0)
            requested = float(raw) if isinstance(raw, (int, float, str)) else 45.0
        duration = max(1.0, float(requested))

        if self.behaviour.silent_audio:
            buffer = silence(duration, rate=self.sample_rate)
        else:
            # Seed-derived so §7.13's determinism test has something real to measure: the
            # same seed produces the same audio, a different seed does not.
            bpm_raw = task.payload.get("bpm", 120)
            bpm = float(bpm_raw) if isinstance(bpm_raw, (int, float, str)) else 120.0
            buffer = musical(
                duration, seed=task.seed % 10_000, rate=self.sample_rate, bpm=bpm
            )
        write_audio(path, buffer)
        return path

    def _poll(self, task_id: str) -> dict[str, Any]:
        """One envelope, in the shape the real server actually sends.

        Verified against a live ACE-Step 1.5 at commit ``ca1e85f``: the envelope carries
        ``result`` as a **JSON-encoded string** holding a list of per-item records, and the
        per-item ``status`` is the authoritative one. The documentation describes a flatter
        shape; a fake that implemented the documentation would have passed every test while
        the real client hung forever, which is exactly what happened before this was fixed.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return {
                    "task_id": task_id,
                    "status": 2,
                    "result": _encode([{"status": 2, "error": "unknown task"}]),
                }
            if self.behaviour.hang:
                return _pending(task_id, "Phase 1: Generating CoT metadata...")
            if self.behaviour.fail_with is not None:
                return {
                    "task_id": task_id,
                    "status": 2,
                    "result": _encode(
                        [{"status": 2, "error": self.behaviour.fail_with, "file": ""}]
                    ),
                }
            task.polls += 1
            if task.polls <= self.behaviour.pending_polls:
                return _pending(task_id, "Phase 2: Diffusion...")
            if task.path is None:
                task.path = self._render(task)
            reference = (
                str(task.path) if self.behaviour.serve_local_path else task.path.name
            )
            item = {
                "status": 1,
                "file": reference,
                "wave": "",
                "progress": 1.0,
                "stage": "succeeded",
                # Comma-separated, as the real server sends: requested, then actual.
                "seed_value": f"{task.payload.get('seed', 0)},{task.seed}",
                "prompt": task.payload.get("prompt"),
                "lyrics": task.payload.get("lyrics"),
                "metas": {
                    "bpm": task.payload.get("bpm"),
                    "duration": task.payload.get("audio_duration"),
                    "keyscale": task.payload.get("key_scale"),
                },
                "generation_info": "**Total generation time (1 song): 12.3s**",
                "dit_model": task.payload.get("model"),
            }
            return {"task_id": task_id, "status": 1, "result": _encode([item])}


def _encode(items: list[dict[str, Any]]) -> str:
    """`result` is a JSON string on the wire, not a nested object."""
    return json.dumps(items)


def _pending(task_id: str, stage: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "status": 0,
        "result": _encode([{"status": 0, "file": "", "progress": 0.1, "stage": stage}]),
        "progress_text": stage,
    }


def _make_handler(fake: FakeAceStep) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: object) -> None:
            """Silence. The stdlib handler writes every request to stderr otherwise."""

        # -- helpers

        def _send(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _envelope(self, data: Any, status: int = 200) -> None:
            self._send({"data": data, "code": 200, "error": None}, status=status)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}

        # -- routes

        def do_GET(self) -> None:
            route = urlparse(self.path)
            if route.path == "/health":
                if fake.behaviour.unhealthy:
                    self._send({"code": 503, "error": "model not loaded"}, status=503)
                    return
                self._envelope({"status": "ok", "service": "fake ACE-Step", "version": "1.5.0"})
                return
            if route.path == "/v1/stats":
                self._envelope({"tasks": len(fake.submissions), "queue": 0})
                return
            if route.path == "/v1/models":
                self._envelope(["acestep-v15-turbo", "acestep-v15-base"])
                return
            if route.path == "/v1/audio":
                query = parse_qs(route.query)
                name = (query.get("path") or query.get("file") or [""])[0]
                candidate = Path(name)
                path = candidate if candidate.is_absolute() else fake.audio_dir / name
                if not path.is_file():
                    self._send({"code": 404, "error": "no such file"}, status=404)
                    return
                payload = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self._send({"code": 404, "error": "not found"}, status=404)

        def do_POST(self) -> None:
            route = urlparse(self.path)
            payload = self._body()

            if route.path == "/v1/init":
                fake.init_calls.append(payload)
                self._envelope({"loaded": payload.get("model")})
                return
            if route.path == "/release_task":
                if fake.behaviour.reject_submission:
                    self._send({"code": 500, "error": "service busy"}, status=500)
                    return
                self._envelope({"task_id": fake._create_task(payload)})
                return
            if route.path == "/query_result":
                # `task_id_list` is what the real handler reads. Accepting only this (and
                # not the documented `task_ids`) is deliberate: a fake that accepted both
                # would let a client using the wrong name keep passing its tests.
                ids = payload.get("task_id_list") or []
                if isinstance(ids, str):
                    try:
                        ids = json.loads(ids)
                    except json.JSONDecodeError:
                        ids = []
                results = [fake._poll(str(task_id)) for task_id in ids]
                self._envelope(results)
                return
            self._send({"code": 404, "error": "not found"}, status=404)

    return Handler
