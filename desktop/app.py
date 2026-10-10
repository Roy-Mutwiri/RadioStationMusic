# ruff: noqa: E501 - the inline loading page keeps its CSS on long lines
r"""Trade Fix Radio desktop app.

Starts the station (``tradefix dev``) and opens the Control Center in its own native
window with the Trade Fix Radio icon. No browser is involved: the page renders inside an
Edge WebView2 surface owned by this process, and closing the window stops the station.

Run from the project root:

    .venv\Scripts\pythonw desktop\app.py            # no console
    .venv\Scripts\python  desktop\app.py --scenario violent_breakout

If the station is already listening on the port, the window simply attaches to it.
"""
from __future__ import annotations

import argparse
import ctypes
import faulthandler
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import webview


def _project_root() -> Path:
    """The checkout this launcher belongs to.

    From source that is the parent of ``desktop/``. Frozen by PyInstaller into
    ``Trade Fix Radio.exe`` the file lives in a temp dir, so the exe's own directory is
    searched upward for the ``.venv`` and ``tradefix_radio`` the station needs.
    """
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).resolve().parent
        for candidate in (here, *here.parents):
            if (candidate / "tradefix_radio").is_dir() and (candidate / ".venv").is_dir():
                return candidate
        return here
    return Path(__file__).resolve().parents[1]


ROOT = _project_root()
ICON = ROOT / "desktop" / "tradefix.ico"
TITLE = "Trade Fix Radio"
APP_ID = "TradeFix.Radio.ControlCenter"
STARTUP_TIMEOUT_SECONDS = 120
APP_LOG = ROOT / "logs" / "desktop-app.log"
_log = logging.getLogger("tradefix.desktop")
ACE_STEP_STARTUP_TIMEOUT_SECONDS = 300

LOADING_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Trade Fix Radio</title>
<style>
  html,body{height:100%;margin:0;background:#0c0e14;color:#e8e9ee;font-family:Segoe UI,system-ui,sans-serif}
  .wrap{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px}
  .ring{width:72px;height:72px;border-radius:50%;border:6px solid #2a2f3d;border-top-color:#e8b440;animation:spin 1s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  h1{font-size:20px;letter-spacing:.18em;font-weight:600;margin:0}
  p{margin:0;color:#8b90a0;font-size:13px}
</style></head><body><div class="wrap">
  <div class="ring"></div><h1>TRADE FIX RADIO</h1><p id="msg">Starting the station…</p>
</div></body></html>"""


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def _configure_logging() -> None:
    """Everything the launcher itself says goes to ``logs/desktop-app.log``.

    Under ``pythonw`` there is no console: an uncaught exception would end the process with
    no trace at all, which is exactly how one launch disappeared while its station kept
    playing. So stdout/stderr are pointed at the log when they are missing, and faulthandler
    writes a native-crash traceback to the same file.
    """
    APP_LOG.parent.mkdir(exist_ok=True)
    stream = APP_LOG.open("a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    faulthandler.enable(file=stream, all_threads=True)
    logging.basicConfig(
        stream=stream,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sys.excepthook = lambda *exc: _log.critical("uncaught exception", exc_info=exc)
    threading.excepthook = lambda args: _log.critical(
        "uncaught exception in thread %s", args.thread.name if args.thread else "?",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def _read_dotenv() -> dict[str, str]:
    """Minimal reader for the project ``.env``: KEY=VALUE lines, quotes and comments stripped."""
    values: dict[str, str] = {}
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return values
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - loopback only
            return 200 <= response.status < 300
    except Exception:  # noqa: BLE001 - any failure means "not up yet"
        return False


class _Job:
    """A Windows job object with kill-on-close.

    Every child the launcher starts is assigned to it. When the launcher process ends for
    any reason — closed, crashed, or killed from outside — the kernel closes the job handle
    and terminates everything in it. That is the only guarantee that the station and
    ACE-Step cannot keep running headless after the window is gone.
    """

    def __init__(self) -> None:
        self.handle: int | None = None
        if sys.platform != "win32":
            return
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            _log.warning("CreateJobObject failed (%s); children will not be tied to the app",
                         ctypes.get_last_error())
            return

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimit),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        job_object_extended_limit_information = 9
        if not kernel32.SetInformationJobObject(
            handle, job_object_extended_limit_information, ctypes.byref(info), ctypes.sizeof(info)
        ):
            _log.warning("SetInformationJobObject failed (%s)", ctypes.get_last_error())
            kernel32.CloseHandle(handle)
            return
        self.handle = handle

    def assign(self, proc: subprocess.Popen[bytes]) -> None:
        if self.handle is None or sys.platform != "win32":
            return
        kernel32 = ctypes.windll.kernel32
        if not kernel32.AssignProcessToJobObject(self.handle, int(proc._handle)):  # type: ignore[attr-defined]
            _log.warning("AssignProcessToJobObject failed for pid %s (%s)",
                         proc.pid, ctypes.get_last_error())
        else:
            _log.info("pid %s tied to the app's job object", proc.pid)


JOB = _Job()


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Stop a process and everything it spawned (``uv run`` wraps a python child)."""
    if proc.poll() is not None:
        return
    result = subprocess.run(  # noqa: S603 - fixed argv
        [shutil.which("taskkill") or "taskkill", "/T", "/F", "/PID", str(proc.pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    _log.info("taskkill pid=%s rc=%s %s", proc.pid, result.returncode,
              (result.stdout or result.stderr).strip().replace("\n", " | "))


class AceStepServer:
    """Owns the ACE-Step API server when the station is configured to generate with it.

    The project's ``worker_process`` setting is declared but nothing in the station launches
    the server, so the desktop app does: ``uv run acestep-api --no-init`` in the ACE-Step
    checkout. ``--no-init`` leaves model loading to the station, which initialises the exact
    DiT and LM models it was configured with through the API.
    """

    def __init__(self, directory: Path, base_url: str) -> None:
        self.directory = directory
        self.base_url = base_url.rstrip("/")
        self.proc: subprocess.Popen[bytes] | None = None
        self.attached = False

    @property
    def wanted(self) -> bool:
        return self.directory.is_dir()

    def is_up(self) -> bool:
        return _http_ok(f"{self.base_url}/health")

    def start(self) -> None:
        if self.is_up():
            self.attached = True
            return
        log = (ROOT / "logs" / "acestep.log").open("ab")
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("`uv` is not on PATH; it is how ACE-Step runs in its own environment")
        self.proc = subprocess.Popen(  # noqa: S603 - fixed argv
            [uv, "run", "--no-sync", "acestep-api", "--no-init"],
            cwd=self.directory,
            # --no-sync: torch is installed from a local wheel (uv's streamed download of the
            # 3 GB CUDA build stalls here), and a sync would replace it from the index.
            env={**os.environ, "PYTHONUNBUFFERED": "1", "UV_NO_SYNC": "1"},
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        )
        JOB.assign(self.proc)

    def wait_ready(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                return False
            if self.is_up():
                return True
            time.sleep(1.0)
        return False

    def stop(self) -> None:
        if self.proc is not None:
            _log.info("ace-step stop: pid=%s", self.proc.pid)
            _kill_tree(self.proc)
        else:
            _log.info("ace-step stop: nothing to do (attached=%s)", self.attached)


def _tradefix_cli() -> list[str]:
    """Run the CLI module with the venv's python, not the ``tradefix.exe`` launcher.

    The launcher is a separate process whose relayed stderr never reached our log, and it
    stays locked while the app runs, which makes ``pip install`` into the venv fail.
    """
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.is_file() and getattr(sys, "frozen", False):
        raise RuntimeError(f"no .venv next to the app: expected {python}")
    return [str(python if python.is_file() else sys.executable), "-m", "tradefix_radio.cli.main"]


class Station:
    """Owns the ``tradefix dev`` child process."""

    def __init__(self, host: str, port: int, scenario: str, seed: int) -> None:
        self.host, self.port, self.scenario, self.seed = host, port, scenario, seed
        self.proc: subprocess.Popen[bytes] | None = None
        self.attached = False

    def start(self) -> None:
        if _port_open(self.host, self.port):
            self.attached = True  # something already serves here; don't start a second one
            return
        log_dir = ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        log = (log_dir / "desktop.log").open("ab")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        self.proc = subprocess.Popen(  # noqa: S603 - fixed argv from this file
            [
                *_tradefix_cli(),
                "dev",
                "--host", self.host,
                "--port", str(self.port),
                "--scenario", self.scenario,
                "--seed", str(self.seed),
            ],
            cwd=ROOT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},  # so the log fills as it happens
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        JOB.assign(self.proc)

    def wait_ready(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                return False  # died during startup; the log says why
            if _port_open(self.host, self.port):
                return True
            time.sleep(0.25)
        return False

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            _log.info("station stop: nothing to do (attached=%s)", self.attached)
            return
        _log.info("station stop: pid=%s", self.proc.pid)
        try:
            self.proc.send_signal(signal.CTRL_BREAK_EVENT)  # graceful: uvicorn shuts the station down
            self.proc.wait(timeout=15)
            _log.info("station exited rc=%s", self.proc.returncode)
        except (subprocess.TimeoutExpired, OSError) as error:
            _log.warning("graceful stop failed (%s); killing the tree", error)
            _kill_tree(self.proc)


def _apply_window_icon() -> None:
    """Set the title-bar and taskbar icon on the WinForms window pywebview created."""
    if sys.platform != "win32" or not ICON.is_file():
        return
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    pid = kernel32.GetCurrentProcessId()
    hwnds: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _enum(hwnd, _lparam):  # type: ignore[no-untyped-def]
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            hwnds.append(hwnd)
        return True

    user32.EnumWindows(_enum, 0)
    image_icon, lr_loadfromfile, wm_seticon = 1, 0x10, 0x80
    for hwnd in hwnds:
        for which, size in ((0, 16), (1, 48)):  # ICON_SMALL, ICON_BIG
            handle = user32.LoadImageW(None, str(ICON), image_icon, size, size, lr_loadfromfile)
            if handle:
                user32.SendMessageW(hwnd, wm_seticon, which, handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trade Fix Radio desktop window")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("TRADEFIX_APP_PORT", "8000")))
    parser.add_argument("--scenario", default=os.environ.get("TRADEFIX_APP_SCENARIO", "random_walk"))
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--debug", action="store_true", help="enable the WebView dev tools")
    parser.add_argument(
        "--hide-console",
        action="store_true",
        help="hide this process's console window (the shortcuts use it, so the app runs as "
        "python.exe rather than pythonw.exe, which other tooling on this machine kills)",
    )
    args = parser.parse_args(argv)
    if args.hide_console and sys.platform == "win32":
        console = ctypes.windll.kernel32.GetConsoleWindow()
        if console:
            ctypes.windll.user32.ShowWindow(console, 0)  # SW_HIDE
    _configure_logging()
    _log.info("launcher starting: %s", vars(args))

    if sys.platform == "win32":
        # Group taskbar buttons under our own identity rather than python.exe's.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)

    station = Station(args.host, args.port, args.scenario, args.seed)
    url = f"http://{args.host}:{args.port}/"

    dotenv = _read_dotenv()

    def setting(key: str, default: str = "") -> str:
        return os.environ.get(key, dotenv.get(key, default))

    ace_step: AceStepServer | None = None
    if setting("TRADEFIX_GENERATION__PROVIDER", "mock") == "ace_step":
        ace_step = AceStepServer(
            Path(setting("TRADEFIX_GENERATION__ACE_STEP__WORKER_DIRECTORY", "D:/ace-step")),
            setting("TRADEFIX_GENERATION__ACE_STEP__BASE_URL", "http://127.0.0.1:8001"),
        )

    window = webview.create_window(
        TITLE,
        html=LOADING_HTML,
        width=1440,
        height=920,
        min_size=(980, 640),
        background_color="#0c0e14",
        text_select=True,
    )

    def say(message: str, *, failed: bool = False) -> None:
        colour = "#e05252" if failed else "#e8b440"
        window.evaluate_js(
            f"document.querySelector('.ring').style.borderTopColor='{colour}';"
            f"document.getElementById('msg').textContent={message!r};"
        )

    def boot() -> None:
        _log.info("boot: ace_step=%s", ace_step is not None and str(ace_step.directory))
        if ace_step is not None:
            if not ace_step.wanted:
                say(f"ACE-Step is not installed at {ace_step.directory}", failed=True)
                return
            say("Starting the ACE-Step music generator...")
            ace_step.start()
            if not ace_step.wait_ready(ACE_STEP_STARTUP_TIMEOUT_SECONDS):
                log_path = ROOT / "logs" / "acestep.log"
                say(f"ACE-Step did not start. See {log_path.as_posix()}", failed=True)
                return
        say("Starting the station...")
        try:
            station.start()
        except Exception as error:  # noqa: BLE001 - shown on the page, logged with trace
            _log.exception("station start failed")
            say(f"Could not start the station: {error}", failed=True)
            return
        if station.wait_ready(STARTUP_TIMEOUT_SECONDS):
            _log.info("station ready (attached=%s); loading %s", station.attached, url)
            window.load_url(url)
            return
        log_path = ROOT / "logs" / "desktop.log"
        say(f"The station did not start. See {log_path.as_posix()}", failed=True)

    def shutdown() -> None:
        _log.info("shutdown requested")
        try:
            station.stop()
            if ace_step is not None:
                ace_step.stop()
        except Exception:  # noqa: BLE001 - log it; a failed shutdown must still be visible
            _log.exception("shutdown failed")

    window.events.shown += _apply_window_icon
    window.events.closed += shutdown
    threading.Thread(target=boot, name="station-boot", daemon=True).start()
    try:
        webview.start(debug=args.debug, icon=str(ICON), private_mode=False)
    finally:
        _log.info("window loop ended")
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
