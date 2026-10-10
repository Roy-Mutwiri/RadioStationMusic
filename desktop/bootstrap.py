# ruff: noqa: E501, S310 - long download URLs and command lines; urlopen is only given the https
# constants above, and the loopback health check.
"""First-run installer behind ``Trade Fix Radio.exe``.

A listener with no tooling double-clicks the exe and ends up with real, market-driven music
playing. Everything the station needs is fetched into one folder under ``%LOCALAPPDATA%``:

    TradeFixRadio/
      app/            the station source, its Python 3.10 ``.venv`` and the built UI
      ace-step/       the ACE-Step 1.5 checkout, its Python 3.12 ``.venv`` and checkpoints
      tools/          uv (package manager) and FFmpeg
      downloads/      resumable downloads, kept so a retry never starts over
      state/          one marker file per finished step
      logs/

Every step is idempotent and marked when done, so a crash, a lost connection or a closed
window resumes where it stopped. Nothing is installed system-wide and nothing touches the
registry: uninstalling is deleting the folder.

The two multi-gigabyte downloads (torch and the ACE-Step checkpoints) are the fragile part.
Torch is fetched with this module's own resumable downloader and checked against the sha256
from ACE-Step's lockfile, then installed from disk, because uv's streamed download of that
wheel stalled repeatedly during development. The checkpoints come through ACE-Step's own
downloader, which resumes partial files, wrapped in a retry loop that only stops when the
files are actually there.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger("tradefix.desktop.install")

SOURCE_ZIP = "https://github.com/Roy-Mutwiri/RadioStationMusic/archive/refs/heads/main.zip"
ACE_STEP_ZIP = "https://github.com/ACE-Step/ACE-Step-1.5/archive/refs/heads/main.zip"
UV_ZIP = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
FFMPEG_ZIP = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/"
    "ffmpeg-master-latest-win64-gpl.zip"
)
# From ACE-Step's uv.lock: the Windows CUDA 12.8 build it pins. Verified by hash after
# download, so a truncated or tampered file is never installed.
TORCH_WHEEL = "https://download.pytorch.org/whl/cu128/torch-2.7.1%2Bcu128-cp312-cp312-win_amd64.whl"
TORCH_SHA256 = "2bb8c05d48ba815b316879a18195d53a6472a03e297d971e916753f8e1053d30"

STATION_PYTHON = "3.10"  # ADR-01
ACE_STEP_PYTHON = "3.12"  # ADR-02
MIN_GPU_MB = 6_000  # README: ACE-Step needs >= 6 GB free VRAM
LM_MODEL = "acestep-5Hz-lm-1.7B"  # what ACE-Step 1.5 actually ships
DIT_MODEL = "acestep-v15-turbo"

Progress = Callable[[str], None]
Bytes = Callable[[int, int | None], None]


def default_install_dir() -> Path:
    # Not ``TRADEFIX_``-prefixed on purpose: the station's settings loader reads every
    # ``TRADEFIX_*`` variable and rejects names it does not know.
    base = os.environ.get("TFR_INSTALL_DIR")
    if base:
        return Path(base)
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "TradeFixRadio"


@dataclass
class Layout:
    root: Path

    @property
    def app(self) -> Path:
        return self.root / "app"

    @property
    def app_python(self) -> Path:
        return self.app / ".venv" / "Scripts" / "python.exe"

    @property
    def ace_step(self) -> Path:
        return self.root / "ace-step"

    @property
    def ace_python(self) -> Path:
        return self.ace_step / ".venv" / "Scripts" / "python.exe"

    @property
    def uv(self) -> Path:
        return self.root / "tools" / "uv" / "uv.exe"

    @property
    def ffmpeg_bin(self) -> Path:
        return self.root / "tools" / "ffmpeg" / "bin"

    @property
    def downloads(self) -> Path:
        return self.root / "downloads"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def child_env(self) -> dict[str, str]:
        """Environment for every process the app starts: our tools first on PATH."""
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join(
            [str(self.uv.parent), str(self.ffmpeg_bin), env.get("PATH", "")]
        )
        env["PYTHONUNBUFFERED"] = "1"
        env["UV_CACHE_DIR"] = str(self.root / "cache" / "uv")
        env["UV_PYTHON_INSTALL_DIR"] = str(self.root / "cache" / "python")
        env["UV_NO_SYNC"] = "1"
        env["HF_HUB_DOWNLOAD_TIMEOUT"] = "30"
        env.pop("VIRTUAL_ENV", None)
        env.pop("PYTHONPATH", None)
        return env

    @property
    def installed(self) -> bool:
        return (self.state / "config.done").is_file() and self.app_python.is_file()


class InstallError(RuntimeError):
    """A step failed in a way the installer could not retry its way out of."""


class Installer:
    """Runs the install steps in order, skipping the ones already marked done."""

    def __init__(
        self,
        layout: Layout,
        *,
        say: Progress,
        log: Progress,
        progress: Bytes,
        bundled: Path | None,
    ) -> None:
        self.l = layout
        self.say = say
        self.log = log
        self.progress = progress
        self.bundled = bundled  # PyInstaller's extraction dir: frontend/dist lives here
        self.gpu_mb: int | None = None

    # ------------------------------------------------------------------ driver

    def run(self) -> None:
        for d in (self.l.root, self.l.downloads, self.l.state, self.l.logs):
            d.mkdir(parents=True, exist_ok=True)
        self.gpu_mb = self._detect_gpu()
        steps: list[tuple[str, str, Callable[[], None]]] = [
            ("uv", "Fetching the package manager", self._step_uv),
            ("source", "Fetching Trade Fix Radio", self._step_source),
            ("frontend", "Installing the Control Center UI", self._step_frontend),
            ("python-env", "Installing Python 3.10 and the station", self._step_python_env),
            ("ffmpeg", "Fetching FFmpeg", self._step_ffmpeg),
        ]
        if self.gpu_mb is not None and self.gpu_mb >= MIN_GPU_MB:
            steps += [
                ("ace-source", "Fetching the ACE-Step music generator", self._step_ace_source),
                ("ace-env", "Installing Python 3.12 for ACE-Step", self._step_ace_env),
                ("ace-torch", "Fetching PyTorch with CUDA (3 GB)", self._step_ace_torch),
                ("ace-models", "Fetching the music models (about 8.5 GB)", self._step_ace_models),
            ]
        steps += [
            ("config", "Configuring sound and generator", self._step_config),
            ("database", "Preparing the database", self._step_database),
        ]
        total = len(steps)
        for index, (key, title, fn) in enumerate(steps, 1):
            marker = self.l.state / f"{key}.done"
            if marker.is_file():
                self.log(f"[{index}/{total}] {title}: already done")
                continue
            self.say(f"Step {index} of {total}: {title}…")
            self.log(f"[{index}/{total}] {title}")
            started = time.monotonic()
            fn()
            marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
            self.log(f"    done in {time.monotonic() - started:.0f} s")

    # ------------------------------------------------------------------ steps

    def _step_uv(self) -> None:
        zip_path = self._download(UV_ZIP, "uv.zip")
        target = self.l.uv.parent
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as z:
            for member in z.namelist():
                if member.endswith("uv.exe") or member.endswith("uvx.exe"):
                    data = z.read(member)
                    (target / Path(member).name).write_bytes(data)
        if not self.l.uv.is_file():
            raise InstallError("uv.exe was not in the downloaded archive")
        self._run([str(self.l.uv), "--version"])

    def _step_source(self) -> None:
        zip_path = self._download(SOURCE_ZIP, "RadioStationMusic-main.zip")
        self._extract_github_zip(zip_path, self.l.app, keep=(".venv", ".env", "data", "logs"))
        if not (self.l.app / "tradefix_radio").is_dir():
            raise InstallError("the source archive did not contain tradefix_radio/")

    def _step_frontend(self) -> None:
        dist = self.l.app / "frontend" / "dist"
        if self.bundled is not None and (self.bundled / "frontend" / "dist" / "index.html").is_file():
            if dist.exists():
                shutil.rmtree(dist)
            shutil.copytree(self.bundled / "frontend" / "dist", dist)
            self.log("    UI copied from the installer")
        elif not (dist / "index.html").is_file():
            raise InstallError(
                "this build of the installer carries no UI; rebuild it with desktop/build_exe.py"
            )

    def _step_python_env(self) -> None:
        self._run(
            [str(self.l.uv), "venv", "--python", STATION_PYTHON, "--allow-existing",
             str(self.l.app / ".venv")],
            cwd=self.l.app,
        )
        self.log("    installing packages (a few minutes)")
        self._run(
            [str(self.l.uv), "pip", "install", "--python", str(self.l.app_python),
             "-e", f"{self.l.app}[audio,analysis]"],
            cwd=self.l.app,
        )
        self._run([str(self.l.app_python), "-c", "import tradefix_radio, sounddevice, librosa"])

    def _step_ffmpeg(self) -> None:
        zip_path = self._download(FFMPEG_ZIP, "ffmpeg-win64.zip")
        self.l.ffmpeg_bin.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as z:
            for member in z.namelist():
                name = Path(member).name
                if name in ("ffmpeg.exe", "ffprobe.exe") and "/bin/" in member:
                    (self.l.ffmpeg_bin / name).write_bytes(z.read(member))
        if not (self.l.ffmpeg_bin / "ffmpeg.exe").is_file():
            raise InstallError("ffmpeg.exe was not in the downloaded archive")
        self._run([str(self.l.ffmpeg_bin / "ffmpeg.exe"), "-version"])

    def _step_ace_source(self) -> None:
        zip_path = self._download(ACE_STEP_ZIP, "ACE-Step-1.5-main.zip")
        self._extract_github_zip(zip_path, self.l.ace_step, keep=(".venv", "checkpoints"))
        if not (self.l.ace_step / "pyproject.toml").is_file():
            raise InstallError("the ACE-Step archive did not contain pyproject.toml")

    def _step_ace_env(self) -> None:
        # Everything but torch: torch comes from a verified local wheel in the next step,
        # and a sync that included it would try to stream the 3 GB wheel itself.
        self.log("    installing ACE-Step packages (several minutes)")
        self._run(
            [str(self.l.uv), "sync", "--python", ACE_STEP_PYTHON, "--no-install-package", "torch"],
            cwd=self.l.ace_step,
            env_overrides={"UV_NO_SYNC": ""},
        )

    def _step_ace_torch(self) -> None:
        wheel = self._download(TORCH_WHEEL, "torch-2.7.1+cu128-cp312-cp312-win_amd64.whl",
                               sha256=TORCH_SHA256)
        self._run(
            [str(self.l.uv), "pip", "install", "--python", str(self.l.ace_python), str(wheel)],
            cwd=self.l.ace_step,
        )
        self._run(
            [str(self.l.ace_python), "-c",
             "import torch; assert torch.cuda.is_available(), 'CUDA is not available'; "
             "print('torch', torch.__version__, torch.cuda.get_device_name(0))"],
        )

    def _step_ace_models(self) -> None:
        checkpoints = self.l.ace_step / "checkpoints"
        for attempt in range(1, 9):
            if self._models_present(checkpoints):
                return
            self.log(f"    downloading checkpoints (attempt {attempt}; resumes if interrupted)")
            try:
                self._run(
                    [str(self.l.uv), "run", "--no-sync", "acestep-download"],
                    cwd=self.l.ace_step,
                    timeout=4 * 3600,
                )
            except InstallError as error:
                self.log(f"    downloader stopped: {error}")
            time.sleep(5)
        if not self._models_present(checkpoints):
            raise InstallError("the music models did not finish downloading; run the app again")

    @staticmethod
    def _models_present(checkpoints: Path) -> bool:
        def has_weights(folder: Path) -> bool:
            return folder.is_dir() and any(folder.glob("*.safetensors"))

        return (
            has_weights(checkpoints / DIT_MODEL)
            and has_weights(checkpoints / LM_MODEL)
            and has_weights(checkpoints / "vae")
            and has_weights(checkpoints / "Qwen3-Embedding-0.6B")
            and not any((checkpoints / ".cache").rglob("*.incomplete"))
        )

    def _step_config(self) -> None:
        device = self._default_output_device()
        real_music = (self.l.state / "ace-models.done").is_file()
        lines = [
            "# Written by the Trade Fix Radio installer. Edit freely; delete state/config.done",
            "# in the install folder to have it regenerated.",
            "TRADEFIX_MODE=development",
            "",
            "# Real market prices with no account or key: spot gold (gold-api.com) with Yahoo",
            "# history, Bitcoin from Binance. Gold is closed at weekends; the station then",
            "# plays against Bitcoin. Alternatives: metatrader5 (logged-in terminal), simulated.",
            "TRADEFIX_MARKET__FEED=public",
            "",
            "# Sound: the Windows default output device at install time.",
            "TRADEFIX_AUDIO__SINK=sounddevice",
        ]
        if device is not None:
            name, host_api = device
            # `default` follows whatever Windows has as its output device, and switches
            # with it. The detected device is recorded as a comment, for the operator.
            lines += [f"# Detected at install time: {name} ({host_api})",
                      "TRADEFIX_AUDIO__DEVICE_NAME=default",
                      f"TRADEFIX_AUDIO__DEVICE_HOST_API={host_api}"]
            self.log(f"    sound: following the Windows default output (now {name})")
        else:
            lines += ["TRADEFIX_AUDIO__SINK=null_sink"]
            self.log("    no output device found; the station will run silently")
        lines += [""]
        if real_music:
            lines += [
                "# Real music: ACE-Step 1.5 in its own environment, started by the app.",
                "TRADEFIX_GENERATION__PROVIDER=ace_step",
                "TRADEFIX_GENERATION__ACE_STEP__BASE_URL=http://127.0.0.1:8001",
                "TRADEFIX_GENERATION__ACE_STEP__WORKER_PROCESS=true",
                f"TRADEFIX_GENERATION__ACE_STEP__WORKER_DIRECTORY={self.l.ace_step.as_posix()}",
                f"TRADEFIX_GENERATION__ACE_STEP__LM_MODEL={LM_MODEL}",
            ]
        else:
            lines += [
                "# No NVIDIA GPU with enough memory was found, so the station uses its mock",
                "# generator (synthetic placeholder audio). Install ACE-Step later to change this.",
                "TRADEFIX_GENERATION__PROVIDER=mock",
            ]
        (self.l.app / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _step_database(self) -> None:
        self._run([str(self.l.app_python), "-m", "tradefix_radio.cli.main", "init"], cwd=self.l.app)

    # ------------------------------------------------------------------ helpers

    def _detect_gpu(self) -> int | None:
        smi = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
        try:
            out = subprocess.run(  # noqa: S603 - fixed argv
                [smi, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=20, check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            self.log("No NVIDIA GPU detected: music will be synthetic placeholder audio")
            return None
        values = [int(v) for v in out.stdout.split() if v.strip().isdigit()]
        if not values:
            self.log("No NVIDIA GPU detected: music will be synthetic placeholder audio")
            return None
        best = max(values)
        if best < MIN_GPU_MB:
            self.log(f"GPU has {best} MB; ACE-Step needs {MIN_GPU_MB} MB. Using synthetic audio")
        else:
            self.log(f"NVIDIA GPU with {best} MB found: real music with ACE-Step")
        return best

    def _default_output_device(self) -> tuple[str, str] | None:
        code = (
            "import json, sounddevice as sd\n"
            "d = sd.query_devices(kind='output')\n"
            "print(json.dumps({'name': d['name'], 'host_api': sd.query_hostapis(d['hostapi'])['name']}))\n"
        )
        try:
            out = self._run([str(self.l.app_python), "-c", code], capture=True)
            info = json.loads(out.strip().splitlines()[-1])
        except (InstallError, ValueError, IndexError) as error:
            self.log(f"    could not query the default output device: {error}")
            return None
        host = str(info["host_api"])
        # Host-API names as PortAudio reports them: "MME", "Windows DirectSound", "Windows WASAPI".
        short = "MME" if "MME" in host else "DirectSound" if "DirectSound" in host else "WASAPI"
        return str(info["name"]), short

    def _download(self, url: str, filename: str, *, sha256: str | None = None) -> Path:
        """Resumable download with retries; returns the finished file."""
        target = self.l.downloads / filename
        part = target.with_suffix(target.suffix + ".part")
        if target.is_file() and (sha256 is None or self._sha256(target) == sha256):
            self.log(f"    {filename}: already downloaded")
            return target
        self.log(f"    downloading {filename}")
        for attempt in range(1, 13):
            try:
                self._fetch(url, part)
                break
            except (OSError, ValueError) as error:
                self.log(f"    attempt {attempt} interrupted ({error}); resuming in 5 s")
                time.sleep(5)
        else:
            raise InstallError(f"could not download {filename}")
        if sha256 is not None and self._sha256(part) != sha256:
            part.unlink(missing_ok=True)
            raise InstallError(f"{filename} failed its checksum; it will be re-downloaded")
        part.replace(target)
        return target

    def _fetch(self, url: str, part: Path) -> None:
        have = part.stat().st_size if part.is_file() else 0
        request = urllib.request.Request(url, headers={"User-Agent": "TradeFixRadio-installer"})
        if have:
            request.add_header("Range", f"bytes={have}-")
        with urllib.request.urlopen(request, timeout=60) as response:
            status = getattr(response, "status", 200)
            if have and status != 206:
                have = 0  # server ignored the range; start over
            total_header = response.headers.get("Content-Length")
            total = (int(total_header) + have) if total_header else None
            mode = "ab" if have else "wb"
            done = have
            last = time.monotonic()
            with part.open(mode) as fh:
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    now = time.monotonic()
                    if now - last > 0.5:
                        self.progress(done, total)
                        last = now
            self.progress(done, total)
            if total is not None and done < total:
                raise ValueError(f"connection closed at {done} of {total} bytes")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _extract_github_zip(self, zip_path: Path, target: Path, *, keep: tuple[str, ...]) -> None:
        """Unpack a GitHub source archive (single top-level folder) into ``target``.

        Entries in ``keep`` already present in ``target`` survive a re-extract, so a
        re-install keeps the environment, config and data it already built.
        """
        staging = target.with_name(target.name + ".new")
        if staging.exists():
            shutil.rmtree(staging)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(staging)
        inner = next(p for p in staging.iterdir() if p.is_dir())
        target.mkdir(parents=True, exist_ok=True)
        for item in inner.iterdir():
            dest = target / item.name
            if item.name in keep and dest.exists():
                continue
            if dest.is_dir():
                shutil.rmtree(dest)
            elif dest.exists():
                dest.unlink()
            shutil.move(str(item), str(dest))
        shutil.rmtree(staging)

    def _run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        timeout: float = 3600,
        capture: bool = False,
        env_overrides: dict[str, str] | None = None,
    ) -> str:
        env = self.l.child_env()
        for key, value in (env_overrides or {}).items():
            if value == "":
                env.pop(key, None)
            else:
                env[key] = value
        self.log(f"    $ {' '.join(Path(a).name if i == 0 else a for i, a in enumerate(argv))}")
        log_file = self.l.logs / "install.log"
        with log_file.open("ab") as out:
            out.write(f"\n$ {' '.join(argv)}\n".encode())
            out.flush()
            try:
                result = subprocess.run(  # noqa: S603 - argv built from this file's constants
                    argv,
                    cwd=cwd,
                    env=env,
                    stdout=subprocess.PIPE if capture else out,
                    stderr=subprocess.STDOUT if not capture else subprocess.PIPE,
                    text=capture,
                    timeout=timeout,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except subprocess.TimeoutExpired as error:
                raise InstallError(f"{Path(argv[0]).name} timed out after {timeout:.0f} s") from error
        if result.returncode != 0:
            detail = (result.stderr or "").strip()[-400:] if capture else f"see {log_file}"
            raise InstallError(f"{Path(argv[0]).name} exited {result.returncode}: {detail}")
        return result.stdout if capture else ""
