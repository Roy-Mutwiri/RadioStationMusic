# ruff: noqa: E501 - the inline loading page keeps its CSS on long lines
r"""Trade Fix Radio desktop app - RTX 3060 Edition.

Optimized for RTX 3060 GPUs (12GB) running alongside other applications.
Uses lower VRAM thresholds and auto-start settings.

Same functionality as the main app.py but with RTX 3060-specific configuration.
"""
from __future__ import annotations

import argparse
import ctypes
import faulthandler
import json
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
from bootstrap_3060 import Installer3060 as Installer, InstallError, Layout, default_install_dir

TITLE = "Trade Fix Radio RTX3060"
APP_ID = "TradeFix.Radio.ControlCenter.RTX3060"
STARTUP_TIMEOUT_SECONDS = 180
ACE_STEP_STARTUP_TIMEOUT_SECONDS = 300
FIRST_MUSIC_TIMEOUT_SECONDS = 20 * 60
_log = logging.getLogger("tradefix.desktop")


def _bundled_dir() -> Path | None:
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def _checkout_root() -> Path | None:
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).resolve().parent
        for candidate in (here, *here.parents):
            if (candidate / "tradefix_radio").is_dir() and (candidate / ".venv").is_dir():
                return candidate
        return None
    return Path(__file__).resolve().parents[1]


CHECKOUT = _checkout_root()
LAYOUT = None if CHECKOUT is not None else Layout(default_install_dir())
ROOT = CHECKOUT if CHECKOUT is not None else LAYOUT.app
LOG_DIR = ROOT / "logs" if CHECKOUT is not None else LAYOUT.logs
APP_LOG = LOG_DIR / "desktop-app.log"


def _icon_path() -> Path:
    for candidate in (
        ROOT / "desktop" / "tradefix.ico",
        (_bundled_dir() or Path()) / "desktop" / "tradefix.ico",
    ):
        if candidate.is_file():
            return candidate
    return ROOT / "desktop" / "tradefix.ico"


ICON = _icon_path()

LOADING_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Trade Fix Radio RTX3060</title>
<style>
  html,body{height:100%;margin:0;background:#0c0e14;color:#e8e9ee;font-family:Segoe UI,system-ui,sans-serif}
  .wrap{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;padding:24px;box-sizing:border-box}
  .ring{width:72px;height:72px;border-radius:50%;border:6px solid #2a2f3d;border-top-color:#4ade80;animation:spin 1s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  h1{font-size:20px;letter-spacing:.18em;font-weight:600;margin:0}
  .badge{background:#4ade80;color:#0c0e14;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
  p{margin:0;color:#8b90a0;font-size:13px;text-align:center;max-width:720px}
  #bar{width:min(720px,90vw);height:6px;background:#1c2030;border-radius:3px;overflow:hidden;display:none}
  #fill{height:100%;width:0;background:#4ade80;transition:width .3s}
  #bytes{font-size:12px;color:#6c7184;font-family:Consolas,monospace;min-height:1em}
  #log{width:min(720px,90vw);max-height:38vh;overflow:auto;background:#10131c;border:1px solid #1c2030;border-radius:6px;padding:10px 12px;font:12px/1.5 Consolas,monospace;color:#9aa0b4;white-space:pre-wrap;display:none;box-sizing:border-box}
  #note{font-size:12px;color:#6c7184}
</style></head><body><div class="wrap">
  <div class="ring"></div><h1>TRADE FIX RADIO <span class="badge">RTX3060</span></h1><p id="msg">Starting…</p>
  <div id="bar"><div id="fill"></div></div><div id="bytes"></div>
  <div id="log"></div>
  <div id="note">Optimized for RTX 3060 with other apps running</div>
</div>
<script>
  function tfxSay(m, failed){document.getElementById('msg').textContent=m;document.querySelector('.ring').style.borderTopColor=failed?'#e05252':'#4ade80';if(failed){document.querySelector('.ring').style.animation='none'}}
  function tfxLog(line){var l=document.getElementById('log');l.style.display='block';l.textContent+=line+'\\n';l.scrollTop=l.scrollHeight}
  function tfxProgress(frac,label){var b=document.getElementById('bar'),f=document.getElementById('fill'),t=document.getElementById('bytes');b.style.display='block';f.style.width=Math.max(2,Math.min(100,100*frac))+'%';t.textContent=label||''}
  function tfxBytes(done,total){var b=document.getElementById('bar'),f=document.getElementById('fill'),t=document.getElementById('bytes');
    if(done===null){b.style.display='none';t.textContent='';return}
    b.style.display='block';var mb=function(x){return (x/1048576).toFixed(0)+' MB'};
    if(total){f.style.width=Math.min(100,100*done/total)+'%';t.textContent=mb(done)+' / '+mb(total)}else{f.style.width='100%';t.textContent=mb(done)}}
</script></body></html>"""


def _configure_logging() -> None:
    APP_LOG.parent.mkdir(parents=True, exist_ok=True)
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


def _child_env() -> dict[str, str]:
    if LAYOUT is not None:
        return LAYOUT.child_env()
    return {**os.environ, "PYTHONUNBUFFERED": "1", "UV_NO_SYNC": "1"}


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


class _Job:
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
        info.BasicLimitInformation.LimitFlags = 0x2000
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
        if not kernel32.AssignProcessToJobObject(self.handle, int(proc._handle)):
            _log.warning("AssignProcessToJobObject failed for pid %s (%s)",
                         proc.pid, ctypes.get_last_error())
        else:
            _log.info("pid %s tied to the app's job object", proc.pid)


JOB = _Job()


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    result = subprocess.run(
        [shutil.which("taskkill") or "taskkill", "/T", "/F", "/PID", str(proc.pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    _log.info("taskkill pid=%s rc=%s %s", proc.pid, result.returncode,
              (result.stdout or result.stderr).strip().replace("\n", " | "))


class AceStepServer:
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
        env = _child_env()
        uv = (str(LAYOUT.uv) if LAYOUT is not None and LAYOUT.uv.is_file() else None) or shutil.which("uv", path=env["PATH"])
        if uv is None:
            raise RuntimeError("`uv` was not found; it is how ACE-Step runs in its own environment")
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log = (LOG_DIR / "acestep.log").open("ab")
        self.proc = subprocess.Popen(
            [uv, "run", "--no-sync", "acestep-api", "--no-init"],
            cwd=self.directory,
            env=env,
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


def _station_python() -> Path:
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        raise RuntimeError(f"the station's Python environment is missing: {python}")
    return python


class Station:
    def __init__(self, host: str, port: int, scenario: str, seed: int) -> None:
        self.host, self.port, self.scenario, self.seed = host, port, scenario, seed
        self.proc: subprocess.Popen[bytes] | None = None
        self.attached = False

    def start(self) -> None:
        if _port_open(self.host, self.port):
            self.attached = True
            return
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log = (LOG_DIR / "desktop.log").open("ab")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        argv = [str(_station_python()), "-m", "tradefix_radio.cli.main"]
        high_vocal = ROOT / "tradefix_radio" / "config" / "high_vocal.yaml"
        if high_vocal.is_file() and os.environ.get("TFR_VOCALS", "high") != "default":
            argv += ["--config", str(high_vocal)]
        argv += [
            "dev",
            "--host", self.host,
            "--port", str(self.port),
            "--scenario", self.scenario,
            "--seed", str(self.seed),
        ]
        self.proc = subprocess.Popen(
            argv,
            cwd=ROOT,
            env=_child_env(),
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        JOB.assign(self.proc)

    def wait_ready(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                return False
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
            self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            self.proc.wait(timeout=15)
            _log.info("station exited rc=%s", self.proc.returncode)
        except (subprocess.TimeoutExpired, OSError) as error:
            _log.warning("graceful stop failed (%s); killing the tree", error)
            _kill_tree(self.proc)


def _status(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{url}api/status", timeout=3) as response:
            return json.load(response)
    except Exception:
        return None


def _real_music_on_air(status: dict) -> bool:
    now_playing = status.get("now_playing") or {}
    track_id = str(now_playing.get("track_id") or "")
    return (
        status.get("status", {}).get("playout_state") == "playing"
        and status.get("emergency", {}).get("tier") == "scheduled"
        and track_id.startswith("TF-")
    )


def _set_default_volume(url: str, volume: float = 0.1) -> bool:
    """Set the default startup volume to 10% for RTX 3060 edition."""
    try:
        data = json.dumps({"volume": volume}).encode("utf-8")
        request = urllib.request.Request(
            f"{url}api/radio/volume",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.load(response)
            _log.info("default volume set to %d%%", int(volume * 100))
            return result.get("applied", False)
    except Exception as error:
        _log.warning("failed to set default volume: %s", error)
        return False


def _describe_wait(status: dict) -> tuple[float, str]:
    buffer = status.get("buffer") or {}
    generation = status.get("generation") or {}
    market = status.get("market") or {}
    ready = float(buffer.get("ready_minutes") or 0.0)
    target = 8.0
    done = int(generation.get("completed") or 0)
    in_flight = int(generation.get("in_flight") or 0)
    parts = [f"{done} track{'s' if done != 1 else ''} generated"]
    if in_flight:
        parts.append(f"{in_flight} generating")
    parts.append(f"{ready:.1f} of {target:.0f} min ready")
    symbol, price = market.get("symbol"), market.get("price")
    if symbol and price is not None:
        parts.append(f"{symbol} {price:,.2f}")
    return min(1.0, ready / target), " · ".join(parts)


def _apply_window_icon() -> None:
    if sys.platform != "win32" or not ICON.is_file():
        return
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    pid = kernel32.GetCurrentProcessId()
    hwnds: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _enum(hwnd, _lparam):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            hwnds.append(hwnd)
        return True

    user32.EnumWindows(_enum, 0)
    image_icon, lr_loadfromfile, wm_seticon = 1, 0x10, 0x80
    user32.LoadImageW.restype = ctypes.c_void_p
    for hwnd in hwnds:
        for which, size in ((0, 16), (1, 48)):
            handle = user32.LoadImageW(None, str(ICON), image_icon, size, size, lr_loadfromfile)
            if handle:
                user32.SendMessageW(hwnd, wm_seticon, which, ctypes.c_void_p(handle))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trade Fix Radio RTX3060 Edition")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("TFR_APP_PORT", "8000")))
    parser.add_argument("--scenario", default=os.environ.get("TFR_APP_SCENARIO", "random_walk"))
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--debug", action="store_true", help="enable the WebView dev tools")
    parser.add_argument("--hide-console", action="store_true")
    args = parser.parse_args(argv)
    if args.hide_console and sys.platform == "win32":
        console = ctypes.windll.kernel32.GetConsoleWindow()
        if console:
            ctypes.windll.user32.ShowWindow(console, 0)
    _configure_logging()
    _log.info("RTX3060 launcher starting: %s", vars(args))
    _log.info("mode: %s root=%s", "checkout" if CHECKOUT is not None else "installed", ROOT)

    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)

    station = Station(args.host, args.port, args.scenario, args.seed)
    url = f"http://{args.host}:{args.port}/"

    window = webview.create_window(
        TITLE,
        html=LOADING_HTML,
        width=1440,
        height=920,
        min_size=(980, 640),
        background_color="#0c0e14",
        text_select=True,
    )

    def js(code: str) -> None:
        try:
            window.evaluate_js(code)
        except Exception:
            _log.debug("evaluate_js failed", exc_info=True)

    def say(message: str, *, failed: bool = False) -> None:
        _log.info("ui: %s", message)
        js(f"tfxSay({message!r}, {'true' if failed else 'false'})")

    def log_line(line: str) -> None:
        _log.info("install: %s", line)
        js(f"tfxLog({line!r})")

    def bytes_progress(done: int, total: int | None) -> None:
        js(f"tfxBytes({done}, {total if total is not None else 'null'})")

    def install_if_needed() -> bool:
        if LAYOUT is None:
            return True
        installer = Installer(
            LAYOUT, say=say, log=log_line, progress=bytes_progress, bundled=_bundled_dir()
        )
        if LAYOUT.installed:
            installer.run()
            return True
        say("First run: setting everything up for RTX 3060. This downloads several gigabytes once.")
        log_line(f"Installing into {LAYOUT.root}")
        try:
            installer.run()
        except InstallError as error:
            say(f"Setup stopped: {error}", failed=True)
            log_line("Close and reopen the app to resume from this step. "
                     f"Details: {LAYOUT.logs / 'install.log'}")
            return False
        except Exception as error:
            _log.exception("installer crashed")
            say(f"Setup failed: {error}", failed=True)
            return False
        bytes_progress(0, None)
        js("tfxBytes(null, null)")
        say("Setup complete. Starting the station…")
        return True

    ace_step: AceStepServer | None = None

    def boot() -> None:
        nonlocal ace_step
        if not install_if_needed():
            return
        dotenv = _read_dotenv()

        def setting(key: str, default: str = "") -> str:
            return os.environ.get(key, dotenv.get(key, default))

        if setting("TRADEFIX_GENERATION__PROVIDER", "mock") == "ace_step":
            ace_step = AceStepServer(
                Path(setting("TRADEFIX_GENERATION__ACE_STEP__WORKER_DIRECTORY",
                             str(LAYOUT.ace_step) if LAYOUT is not None else "D:/ace-step")),
                setting("TRADEFIX_GENERATION__ACE_STEP__BASE_URL", "http://127.0.0.1:8001"),
            )
        elif LAYOUT is not None:
            say("No suitable NVIDIA GPU was found, so the music is synthetic placeholder audio.")
        _log.info("boot: ace_step=%s", ace_step is not None and str(ace_step.directory))
        if ace_step is not None:
            if not ace_step.wanted:
                say(f"ACE-Step is not installed at {ace_step.directory}", failed=True)
                return
            say("Starting the ACE-Step music generator...")
            try:
                ace_step.start()
            except Exception as error:
                _log.exception("ace-step start failed")
                say(f"Could not start ACE-Step: {error}", failed=True)
                return
            if not ace_step.wait_ready(ACE_STEP_STARTUP_TIMEOUT_SECONDS):
                say(f"ACE-Step did not start. See {(LOG_DIR / 'acestep.log').as_posix()}", failed=True)
                return
        say("Starting the station...")
        try:
            station.start()
        except Exception as error:
            _log.exception("station start failed")
            say(f"Could not start the station: {error}", failed=True)
            return
        if not station.wait_ready(STARTUP_TIMEOUT_SECONDS):
            say(f"The station did not start. See {(LOG_DIR / 'desktop.log').as_posix()}", failed=True)
            return
        _log.info("station ready (attached=%s); waiting for real music", station.attached)
        # RTX 3060 Edition: Set default volume to 10%
        _set_default_volume(url, volume=0.1)
        say("Composing the first tracks from the live market…")
        js("tfxBytes(null, null)")
        deadline = time.monotonic() + FIRST_MUSIC_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            status = _status(url)
            if status is not None:
                if _real_music_on_air(status):
                    break
                fraction, caption = _describe_wait(status)
                js(f"tfxProgress({fraction:.3f}, {caption!r})")
            time.sleep(2.0)
        else:
            _log.warning("no generated track on air after %s s; showing the UI anyway",
                         FIRST_MUSIC_TIMEOUT_SECONDS)
        say("On air.")
        _log.info("loading %s", url)
        window.load_url(url)

    def shutdown() -> None:
        _log.info("shutdown requested")
        try:
            station.stop()
            if ace_step is not None:
                ace_step.stop()
        except Exception:
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
