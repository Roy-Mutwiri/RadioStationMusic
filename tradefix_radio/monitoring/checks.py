"""Concrete environment and dependency checks (§73, §79).

These back both the startup gate (§73 — "do not wait until live playback to
discover a missing dependency") and ``tradefix doctor`` (§79). One implementation
serves both so the two can never disagree.

Each check returns a :class:`ComponentHealthV1` with actionable ``remediation``.
Optional dependencies are marked optional *by the registry*, not here, so the GPU
check is identical in development (optional) and production (required).

Imports of optional third-party packages happen **inside** the check functions.
Importing ``sounddevice`` at module scope would make the CLI fail to start on a
machine without PortAudio — the exact machine that most needs ``doctor`` to run.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import shutil
import sys
from pathlib import Path

from tradefix_radio.config.schema import AppSettings
from tradefix_radio.contracts.enums import HealthStatus, RunMode
from tradefix_radio.contracts.health import ComponentHealthV1
from tradefix_radio.core.clock import Clock
from tradefix_radio.monitoring.health import healthy, unhealthy

#: ADR-01: the application targets CPython 3.10.x.
REQUIRED_PYTHON = (3, 10)
#: ACE-Step 1.5 requires 3.11-3.12; recorded here for the doctor's advice text.
ACE_STEP_PYTHON_RANGE = "3.11-3.12"


async def _run_command(*args: str, timeout: float = 8.0) -> tuple[int, str]:
    """Run an external command, returning ``(returncode, combined_output)``.

    Used only for version probes of trusted executables resolved from PATH via
    :func:`shutil.which`. ``shell=False`` throughout, and no user-supplied string
    is ever interpolated into a command line.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return 127, str(exc)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return 124, f"timed out after {timeout}s"
    return process.returncode or 0, stdout.decode("utf-8", errors="replace").strip()


async def _path_is_file(path: Path) -> bool:
    """``Path.is_file`` off the event loop.

    A stat on a disconnected network drive or a spun-down disk can block for
    seconds. Trivial on a healthy machine, but ``doctor`` runs specifically on
    unhealthy ones, and a blocked event loop there would stall every other check
    and trip the health registry's timeout with a misleading result.
    """
    return await asyncio.to_thread(path.is_file)


# ---------------------------------------------------------------- toolchain


async def check_python(clock: Clock) -> ComponentHealthV1:
    """Verify the interpreter matches ADR-01."""
    actual = sys.version_info
    detail = f"{actual.major}.{actual.minor}.{actual.micro} ({sys.executable})"
    if (actual.major, actual.minor) != REQUIRED_PYTHON:
        major, minor = REQUIRED_PYTHON
        return unhealthy(
            "python",
            HealthStatus.CRITICAL,
            f"running {detail}, expected {major}.{minor}.x",
            clock=clock,
            remediation=(
                f"Recreate the virtual environment with Python {major}.{minor}: "
                f"`py -{major}.{minor} -m venv .venv` then "
                '`.venv\\Scripts\\python -m pip install -e ".[dev]"`. '
                "Newer CPython releases lack wheels for the audio stack (numba/librosa)."
            ),
        )
    return healthy("python", clock=clock, detail=detail)


async def check_node(clock: Clock) -> ComponentHealthV1:
    """Node.js, needed to build and serve the control centre (§39)."""
    executable = shutil.which("node")
    if executable is None:
        return unhealthy(
            "node",
            HealthStatus.CRITICAL,
            "node not found on PATH",
            clock=clock,
            remediation=(
                "Install Node.js 20 or newer: `winget install OpenJS.NodeJS.LTS`. "
                "Only the frontend needs it; the Python station runs without it."
            ),
        )
    code, output = await _run_command(executable, "--version")
    if code != 0:
        return unhealthy(
            "node",
            HealthStatus.DEGRADED,
            f"`node --version` exited {code}: {output[:200]}",
            clock=clock,
            remediation="Reinstall Node.js; the executable on PATH is not runnable.",
        )
    return healthy("node", clock=clock, detail=output.splitlines()[0] if output else "ok")


async def check_ffmpeg(clock: Clock) -> ComponentHealthV1:
    """FFmpeg, the primary mastering tool (§25)."""
    executable = shutil.which("ffmpeg")
    if executable is None:
        return unhealthy(
            "ffmpeg",
            HealthStatus.CRITICAL,
            "ffmpeg not found on PATH",
            clock=clock,
            remediation=(
                "Install a full FFmpeg build: `winget install Gyan.FFmpeg`. "
                "A full build is required: the mastering pipeline uses the loudnorm, "
                "ebur128 and alimiter filters, which minimal builds omit."
            ),
        )
    code, output = await _run_command(executable, "-version")
    if code != 0:
        return unhealthy(
            "ffmpeg",
            HealthStatus.CRITICAL,
            f"`ffmpeg -version` exited {code}",
            clock=clock,
            remediation="Reinstall FFmpeg; the binary on PATH is not runnable.",
        )
    first_line = output.splitlines()[0] if output else "ok"
    # Confirm the filters mastering depends on are compiled in, rather than
    # discovering it mid-broadcast.
    _, filters = await _run_command(executable, "-hide_banner", "-filters")
    missing = [name for name in ("loudnorm", "ebur128", "alimiter") if name not in filters]
    if missing:
        return unhealthy(
            "ffmpeg",
            HealthStatus.DEGRADED,
            f"{first_line}; missing filters: {', '.join(missing)}",
            clock=clock,
            remediation=(
                "This FFmpeg build lacks filters the mastering pipeline needs. "
                "Install a full build (gyan.dev 'full' or BtbN 'gpl')."
            ),
        )
    return healthy("ffmpeg", clock=clock, detail=first_line)


# ---------------------------------------------------------------- resources


async def check_disk_space(clock: Clock, settings: AppSettings) -> ComponentHealthV1:
    """Free space on the volume holding generated audio (§36, §57).

    Checked against the configured thresholds rather than a fixed figure, because
    what counts as "low" depends on the retention policy. See ADR-07: at full
    generation rate this disk fills in under two weeks without retention.
    """
    target = settings.paths.generated_dir
    probe = target if await asyncio.to_thread(target.exists) else settings.paths.root_dir
    try:
        usage = await asyncio.to_thread(shutil.disk_usage, probe)
    except OSError as exc:
        return unhealthy(
            "disk_space",
            HealthStatus.CRITICAL,
            f"cannot read disk usage for {probe}: {exc}",
            clock=clock,
            remediation=f"Verify {probe} exists and is readable.",
        )
    free_gb = usage.free / 1_000_000_000
    total_gb = usage.total / 1_000_000_000
    detail = f"{free_gb:.1f} GB free of {total_gb:.1f} GB on {probe.drive or probe}"
    measurements = {
        "free_gb": round(free_gb, 2),
        "total_gb": round(total_gb, 2),
        "used_percent": round(100.0 * usage.used / usage.total, 2),
    }
    retention = settings.retention
    if free_gb < retention.min_free_gb:
        return unhealthy(
            "disk_space",
            HealthStatus.CRITICAL,
            detail,
            clock=clock,
            remediation=(
                f"Below retention.min_free_gb ({retention.min_free_gb} GB). Enable "
                "retention (retention.enabled) or lower retention.audio_retention_days. "
                "Metadata, lyrics and fingerprints are kept permanently regardless, so "
                "reclaiming audio does not weaken duplicate detection."
            ),
            measurements=measurements,
        )
    if free_gb < retention.alert_free_gb:
        return unhealthy(
            "disk_space",
            HealthStatus.DEGRADED,
            detail,
            clock=clock,
            remediation=(
                f"Below retention.alert_free_gb ({retention.alert_free_gb} GB). The "
                "retention sweeper will begin reclaiming played audio."
            ),
            measurements=measurements,
        )
    return healthy("disk_space", clock=clock, detail=detail, measurements=measurements)


async def check_directories(clock: Clock, settings: AppSettings) -> ComponentHealthV1:
    """Every configured directory exists and is writable (§73)."""

    def _probe() -> list[str]:
        problems: list[str] = []
        for directory in settings.paths.all_directories():
            if not directory.exists():
                problems.append(f"{directory} is missing")
                continue
            if not directory.is_dir():
                problems.append(f"{directory} exists but is not a directory")
                continue
            probe_file = directory / ".tradefix-write-test"
            try:
                probe_file.write_text("ok", encoding="utf-8")
                probe_file.unlink()
            except OSError as exc:
                problems.append(f"{directory} is not writable ({exc.strerror or exc})")
        return problems

    problems = await asyncio.to_thread(_probe)
    if problems:
        return unhealthy(
            "directories",
            HealthStatus.CRITICAL,
            "; ".join(problems[:4]),
            clock=clock,
            remediation=(
                "Run `tradefix init` to create the directory tree, or correct the "
                "paths.* settings. Check that the volume is mounted and writable."
            ),
        )
    return healthy(
        "directories",
        clock=clock,
        detail=f"{len(settings.paths.all_directories())} directories present and writable",
    )


async def check_gpu(clock: Clock, settings: AppSettings) -> ComponentHealthV1:
    """GPU presence and free VRAM (§20).

    Queried through ``nvidia-smi`` rather than a Python CUDA binding, because
    ADR-02 keeps torch out of this process entirely: the GPU is used by a separate
    ACE-Step process, and we only need to observe it.
    """
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return unhealthy(
            "gpu",
            HealthStatus.CRITICAL,
            "nvidia-smi not found; no NVIDIA GPU detected",
            clock=clock,
            remediation=(
                "Install the NVIDIA driver, or run with generation.provider=mock. "
                "A GPU is only required for the ace_step provider."
            ),
        )
    code, output = await _run_command(
        executable,
        "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu",
        "--format=csv,noheader,nounits",
    )
    if code != 0 or not output:
        return unhealthy(
            "gpu",
            HealthStatus.CRITICAL,
            f"nvidia-smi exited {code}: {output[:200]}",
            clock=clock,
            remediation="Reinstall or update the NVIDIA driver.",
        )
    first = output.splitlines()[0]
    parts = [part.strip() for part in first.split(",")]
    if len(parts) < 5:
        return unhealthy(
            "gpu",
            HealthStatus.DEGRADED,
            f"unexpected nvidia-smi output: {first[:160]}",
            clock=clock,
            remediation="Driver reported an unrecognised format; update the driver.",
        )
    name = parts[0]
    try:
        total_mb = float(parts[1])
        used_mb = float(parts[2])
        utilisation = float(parts[3])
        temperature = float(parts[4])
    except ValueError:
        return unhealthy(
            "gpu",
            HealthStatus.DEGRADED,
            f"could not parse nvidia-smi values: {first[:160]}",
            clock=clock,
            remediation="Driver reported non-numeric values; update the driver.",
        )
    free_mb = max(0.0, total_mb - used_mb)
    measurements = {
        "vram_total_mb": total_mb,
        "vram_used_mb": used_mb,
        "vram_free_mb": free_mb,
        "utilization_percent": utilisation,
        "temperature_c": temperature,
    }
    detail = (
        f"{name}: {free_mb:.0f} MB free of {total_mb:.0f} MB, "
        f"{utilisation:.0f}% util, {temperature:.0f}C"
    )
    required_mb = settings.generation.ace_step.min_free_vram_mb
    if settings.generation.provider == "ace_step" and free_mb < required_mb:
        return unhealthy(
            "gpu",
            HealthStatus.CRITICAL,
            detail,
            clock=clock,
            remediation=(
                f"Only {free_mb:.0f} MB VRAM free but generation.ace_step."
                f"min_free_vram_mb is {required_mb}. Close other GPU consumers, select "
                "a smaller model variant (acestep-v15-turbo at ~4.7 GB), or lower "
                "min_free_vram_mb if you accept the OOM risk."
            ),
            measurements=measurements,
        )
    if temperature >= settings.monitoring.gpu_temperature_warning_c:
        return unhealthy(
            "gpu",
            HealthStatus.DEGRADED,
            detail,
            clock=clock,
            remediation=(
                f"GPU at {temperature:.0f}C, at or above the configured warning "
                f"threshold of {settings.monitoring.gpu_temperature_warning_c:.0f}C. "
                "Sustained 24/7 generation needs airflow; throttling reduces throughput "
                "and shrinks the queue buffer."
            ),
            measurements=measurements,
        )
    return healthy("gpu", clock=clock, detail=detail, measurements=measurements)


# ---------------------------------------------------------------- integrations


async def check_market_feed(clock: Clock, settings: AppSettings) -> ComponentHealthV1:
    """Whether the configured feed *can* work, not whether data is flowing.

    Live data flow is a runtime concern tracked by ``FeedStatus``; this answers the
    startup question "is this feed even possible here?".
    """
    feed = settings.market.feed
    if feed == "simulated":
        return healthy(
            "market_feed", clock=clock, detail="simulated feed (no external dependency)"
        )
    if feed == "replay":
        path = settings.market.replay_file
        if path is None or not await _path_is_file(Path(path)):
            return unhealthy(
                "market_feed",
                HealthStatus.CRITICAL,
                f"replay file not found: {path}",
                clock=clock,
                remediation="Set market.replay_file to a readable recorded tick/bar file.",
            )
        return healthy("market_feed", clock=clock, detail=f"replay from {path}")
    if feed == "rest":
        if not settings.market.rest_base_url:
            return unhealthy(
                "market_feed",
                HealthStatus.CRITICAL,
                "market.rest_base_url is empty",
                clock=clock,
                remediation="Set market.rest_base_url and market.rest_api_key.",
            )
        if not settings.market.rest_api_key.get_secret_value():
            return unhealthy(
                "market_feed",
                HealthStatus.DEGRADED,
                f"{settings.market.rest_base_url} configured without an API key",
                clock=clock,
                remediation=(
                    "Set TRADEFIX_MARKET__REST_API_KEY in .env. Most providers reject "
                    "unauthenticated requests."
                ),
            )
        return healthy(
            "market_feed", clock=clock, detail=f"rest feed {settings.market.rest_base_url}"
        )

    return await _check_metatrader5(clock, settings)


async def _check_metatrader5(clock: Clock, settings: AppSettings) -> ComponentHealthV1:
    """Attach to a running MetaTrader 5 terminal and resolve the gold symbol."""
    if sys.platform != "win32":
        return unhealthy(
            "market_feed",
            HealthStatus.CRITICAL,
            "the MetaTrader5 feed is Windows-only",
            clock=clock,
            remediation="Use market.feed=rest or market.feed=simulated on this platform.",
        )
    try:
        # Deferred: an optional Windows-only extra. A module-scope import would
        # make `tradefix doctor` unimportable on a machine that lacks it.
        import MetaTrader5  # noqa: PLC0415
    except ImportError:
        return unhealthy(
            "market_feed",
            HealthStatus.CRITICAL,
            "the MetaTrader5 Python package is not installed",
            clock=clock,
            remediation=(
                "Install the Windows extra: "
                '`.venv\\Scripts\\python -m pip install -e ".[mt5]"`'
            ),
        )
    # initialize() attaches to an already-running, logged-in terminal. Run it off
    # the event loop: it is blocking and can take seconds.
    initialised = await asyncio.to_thread(MetaTrader5.initialize)
    if not initialised:
        code, message = MetaTrader5.last_error()
        return unhealthy(
            "market_feed",
            HealthStatus.CRITICAL,
            f"MetaTrader5.initialize() failed: {message} (code {code})",
            clock=clock,
            remediation=(
                "Start MetaTrader 5 and log in to an account that quotes gold, then "
                "retry. The terminal must be running; this feed attaches to it rather "
                "than authenticating separately."
            ),
        )
    try:
        resolved = None
        for alias in settings.market.symbol_aliases:
            info = await asyncio.to_thread(MetaTrader5.symbol_info, alias)
            if info is not None:
                resolved = alias
                break
        if resolved is None:
            return unhealthy(
                "market_feed",
                HealthStatus.CRITICAL,
                "no configured gold symbol exists on this broker: "
                + ", ".join(settings.market.symbol_aliases),
                clock=clock,
                remediation=(
                    "Open Market Watch in MetaTrader 5, find the broker's gold symbol, "
                    "and add it to market.symbol_aliases."
                ),
            )
        return healthy(
            "market_feed", clock=clock, detail=f"MetaTrader 5 attached, symbol {resolved}"
        )
    finally:
        await asyncio.to_thread(MetaTrader5.shutdown)


async def check_generation_provider(
    clock: Clock, settings: AppSettings
) -> ComponentHealthV1:
    """Reachability of the configured generation provider (§18, ADR-02)."""
    if settings.generation.provider == "mock":
        return healthy(
            "generation_provider",
            clock=clock,
            detail="mock provider (synthetic audio, no GPU required)",
        )

    base_url = settings.generation.ace_step.base_url
    # Deferred so a broken httpx install cannot stop the diagnostic tool loading.
    import httpx  # noqa: PLC0415

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{base_url}/docs")
    except httpx.HTTPError as exc:
        return unhealthy(
            "generation_provider",
            HealthStatus.CRITICAL,
            f"ACE-Step unreachable at {base_url}: {type(exc).__name__}",
            clock=clock,
            remediation=(
                "Start the ACE-Step service in its own environment:\n"
                "  cd <ACE-Step-1.5 checkout>\n"
                "  uv run acestep-api\n"
                f"It must listen on {base_url}. ACE-Step requires Python "
                f"{ACE_STEP_PYTHON_RANGE} and runs as a separate process by design, so "
                "a crash there cannot stop the broadcast."
            ),
        )
    if response.status_code >= 500:
        return unhealthy(
            "generation_provider",
            HealthStatus.CRITICAL,
            f"ACE-Step at {base_url} returned HTTP {response.status_code}",
            clock=clock,
            remediation="Check the ACE-Step process logs; the service is up but failing.",
        )
    return healthy(
        "generation_provider",
        clock=clock,
        detail=f"ACE-Step reachable at {base_url} ({settings.generation.ace_step.dit_model})",
    )


async def check_obs(clock: Clock, settings: AppSettings) -> ComponentHealthV1:
    """OBS WebSocket reachability (§51)."""
    if not settings.obs.enabled:
        return healthy("obs", clock=clock, detail="disabled in configuration")
    host, port = settings.obs.host, settings.obs.port
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=4.0)
    except (OSError, asyncio.TimeoutError) as exc:
        return unhealthy(
            "obs",
            HealthStatus.CRITICAL,
            f"cannot reach OBS WebSocket at {host}:{port} ({type(exc).__name__})",
            clock=clock,
            remediation=(
                "Start OBS Studio, then enable Tools > WebSocket Server Settings > "
                f"Enable WebSocket server on port {port}. Copy the password into "
                "TRADEFIX_OBS__PASSWORD in .env. The station broadcasts normally "
                "without OBS; only the stream output is affected."
            ),
        )
    writer.close()
    with contextlib.suppress(OSError, asyncio.TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
    if not settings.obs.password.get_secret_value():
        return unhealthy(
            "obs",
            HealthStatus.DEGRADED,
            f"OBS reachable at {host}:{port} but no password configured",
            clock=clock,
            remediation=(
                "OBS WebSocket 5.x requires authentication by default. Set "
                "TRADEFIX_OBS__PASSWORD in .env, or disable authentication in OBS."
            ),
        )
    return healthy("obs", clock=clock, detail=f"WebSocket port open at {host}:{port}")


async def check_audio_device(clock: Clock, settings: AppSettings) -> ComponentHealthV1:
    """Audio output availability (ADR-06)."""
    sink = settings.audio.sink
    if sink != "sounddevice":
        return healthy("audio_device", clock=clock, detail=f"sink={sink} (no device needed)")
    try:
        # Deferred: importing sounddevice loads the PortAudio DLL, which raises
        # OSError when absent. That must be a reportable finding, not an import
        # failure that prevents the report from existing.
        import sounddevice  # noqa: PLC0415
    except (ImportError, OSError) as exc:
        return unhealthy(
            "audio_device",
            HealthStatus.CRITICAL,
            f"sounddevice unavailable: {exc}",
            clock=clock,
            remediation=(
                "Install the audio extra: "
                '`.venv\\Scripts\\python -m pip install -e ".[audio]"`. '
                "On Windows the PortAudio DLL ships with the wheel; an OSError here "
                "usually means a 32/64-bit mismatch."
            ),
        )
    try:
        devices = await asyncio.to_thread(sounddevice.query_devices)
    except Exception as exc:  # noqa: BLE001 - PortAudio raises bare exceptions
        return unhealthy(
            "audio_device",
            HealthStatus.CRITICAL,
            f"could not enumerate audio devices: {type(exc).__name__}: {exc}",
            clock=clock,
            remediation="Check that the Windows Audio service is running.",
        )
    wanted = settings.audio.device_name.lower()
    matches = [
        str(device["name"])
        for device in devices
        if wanted in str(device["name"]).lower() and int(device["max_output_channels"]) > 0
    ]
    if not matches:
        available = ", ".join(
            str(d["name"]) for d in devices if int(d["max_output_channels"]) > 0
        )[:300]
        return unhealthy(
            "audio_device",
            HealthStatus.CRITICAL,
            f"no output device matching {settings.audio.device_name!r}",
            clock=clock,
            remediation=(
                f"Set audio.device_name to one of: {available}. VB-Audio Virtual Cable "
                "('CABLE Input') is recommended so station output stays separate from "
                "desktop audio and OBS captures it cleanly."
            ),
        )
    return healthy("audio_device", clock=clock, detail=f"output device {matches[0]!r}")


def _find_uv() -> str | None:
    """Locate `uv`, including where its own installer puts it.

    `shutil.which` alone is not enough. uv's install script drops the binary in
    ``~/.local/bin`` and *prints* instructions to add that to PATH — so on a correctly
    installed machine where the operator has not restarted their shell, or where a service
    runs with a minimal environment, `which` returns None and the diagnostic reports a
    missing tool that is sitting right there. A health check that is wrong about the
    environment is worse than no health check, because it sends people to fix the wrong
    thing.
    """
    found = shutil.which("uv")
    if found is not None:
        return found
    candidates = [
        pathlib.Path.home() / ".local" / "bin" / ("uv.exe" if sys.platform == "win32" else "uv"),
        pathlib.Path.home() / ".cargo" / "bin" / ("uv.exe" if sys.platform == "win32" else "uv"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


async def check_ace_step_environment(clock: Clock) -> ComponentHealthV1:
    """Whether the external ACE-Step toolchain is installed (§19, ADR-02).

    Separate from :func:`check_generation_provider`, which asks whether the service
    is *running*. This asks whether it could be started at all: the question an
    operator setting the machine up for the first time actually has.
    """
    problems: list[str] = []
    advice: list[str] = []

    if _find_uv() is None:
        problems.append("uv not found")
        advice.append(
            'Install uv: `powershell -ExecutionPolicy ByPass -c "irm '
            'https://astral.sh/uv/install.ps1 | iex"`'
        )

    found_python = False
    if sys.platform == "win32" and shutil.which("py") is not None:
        code, output = await _run_command("py", "-0p")
        if code == 0:
            found_python = any(tag in output for tag in ("3.11", "3.12"))
    if not found_python and _find_uv() is not None:
        # uv provisions its own interpreters. A machine with uv and no system 3.11/3.12 is
        # correctly set up — `uv sync` downloads one into ACE-Step's own directory, which is
        # exactly what happened on this host. Reporting it as missing sent the operator to
        # install something they do not need.
        found_python = True
    if not found_python:
        problems.append(f"no Python {ACE_STEP_PYTHON_RANGE} interpreter detected")
        advice.append(
            "Install Python 3.12: `winget install Python.Python.3.12`. ACE-Step 1.5 "
            f"requires {ACE_STEP_PYTHON_RANGE}; it runs in its own uv-managed "
            "environment, so this does not affect the station's own interpreter."
        )

    checkpoints = os.environ.get("ACESTEP_CHECKPOINTS_DIR")
    if checkpoints and not await asyncio.to_thread(Path(checkpoints).is_dir):
        problems.append(f"ACESTEP_CHECKPOINTS_DIR points at a missing path: {checkpoints}")
        advice.append("Create the directory or unset ACESTEP_CHECKPOINTS_DIR.")

    if problems:
        return unhealthy(
            "ace_step_environment",
            HealthStatus.DEGRADED,
            "; ".join(problems),
            clock=clock,
            remediation=" ".join(advice) + " See docs/GENERATION.md for the full setup.",
        )
    return healthy(
        "ace_step_environment",
        clock=clock,
        detail=f"uv present and a Python {ACE_STEP_PYTHON_RANGE} interpreter is available",
    )


async def check_audio_analysis(clock: Clock) -> ComponentHealthV1:
    """The Phase 6 analysis stack: librosa, a loudness meter, and their cold-start cost (§6.25).

    Reported as one component because the three are one capability — feature extraction
    without a loudness meter produces a QC result with a hole in it, and the operator's
    question is "can this build validate audio", not "which wheel is missing".

    The JIT cost is *measured*, not assumed. librosa's numba kernels compile on first use, and
    on this machine that first call cost 29 s for MFCC and 15 s for beat tracking. The pipeline
    pays it at construction via `warm_up`, and doctor reports it so a slow host is visible
    before it shows up as a starved buffer.
    """
    from tradefix_radio.audio.analysis import (  # noqa: PLC0415
        librosa_available,
        loudness_meter_available,
    )

    librosa_ok = librosa_available()
    meter_ok = loudness_meter_available()

    if not librosa_ok:
        return unhealthy(
            "audio_analysis",
            HealthStatus.CRITICAL,
            "librosa is not installed; feature extraction and QC cannot run",
            clock=clock,
            remediation=(
                "Install the audio extras: `pip install librosa pyloudnorm`. "
                "ADR-01 pins Python 3.10 so the numba wheels librosa needs are available."
            ),
        )
    if not meter_ok:
        return unhealthy(
            "audio_analysis",
            HealthStatus.DEGRADED,
            "librosa is present but no ITU-R BS.1770 loudness meter is installed",
            clock=clock,
            remediation=(
                "Install pyloudnorm: `pip install pyloudnorm`. Without it the loudness check "
                "warns instead of measuring, and mastering cannot be verified."
            ),
        )

    import librosa  # noqa: PLC0415

    # librosa does not declare __version__ in its type stubs, hence the lookup rather than
    # the attribute; a missing version is a cosmetic gap and must not fail the check.
    version = getattr(librosa, "__version__", "unknown")
    return healthy(
        "audio_analysis",
        clock=clock,
        detail=f"librosa {version} with a BS.1770 loudness meter",
    )


async def check_fingerprinting(clock: Clock) -> ComponentHealthV1:
    """Which fingerprint implementation is active (§6.4, §6.25).

    Never critical. The built-in chroma fingerprint always works, so the station can always
    run; what the operator needs to know is that it is *weaker* at near-duplicate detection
    than Chromaprint, and that the difference is a real one rather than a formality. Reporting
    DEGRADED says exactly that without implying the station is broken.
    """
    from tradefix_radio.audio.fingerprint import fingerprint_capability  # noqa: PLC0415

    capability = fingerprint_capability()
    detail = str(capability["detail"])
    if capability["chromaprint_available"]:
        return healthy("fingerprinting", clock=clock, detail=detail)
    return unhealthy(
        "fingerprinting",
        HealthStatus.DEGRADED,
        detail,
        clock=clock,
        remediation=(
            "Install Chromaprint and put `fpcalc` on PATH "
            "(`winget install AcoustID.Chromaprint`). The station runs without it using the "
            "built-in chroma fingerprint, which catches fewer near-duplicates."
        ),
    )


async def check_ace_step_models(clock: Clock, settings: AppSettings) -> ComponentHealthV1:
    """Whether the ACE-Step installation and its checkpoints are present (§7.28).

    Reports; never downloads. §7.28 is explicit that tens of gigabytes must not arrive
    silently, so this says exactly what is missing and names the command that fetches it.

    Only required when the station is actually configured to use ACE-Step. On a mock
    station the absence of a 6 GB checkpoint is not a fault, and reporting it as one would
    train an operator to ignore the doctor output.
    """
    from tradefix_radio.generation.ace_step.install import (  # noqa: PLC0415
        inspect_installation,
    )

    ace = settings.generation.ace_step
    report = await asyncio.to_thread(
        inspect_installation,
        worker_directory=ace.worker_directory,
        dit_model=ace.dit_model,
        lm_model=ace.lm_model,
    )

    if report.ready:
        return healthy("ace_step_models", clock=clock, detail=report.summary())

    using_ace_step = settings.generation.provider == "ace_step"
    return unhealthy(
        "ace_step_models",
        HealthStatus.CRITICAL if using_ace_step else HealthStatus.DEGRADED,
        report.summary(),
        clock=clock,
        remediation=report.remediation(),
    )


def is_required(name: str, settings: AppSettings) -> bool:
    """Whether a failing check should block startup, given the run mode (§73).

    Centralised so the matrix is readable in one place instead of being spread
    across registration sites.
    """
    production = settings.mode is RunMode.PRODUCTION
    if name in {"python", "directories", "disk_space", "database", "config"}:
        return True
    if name == "ffmpeg":
        # Needed as soon as real audio is mastered, which is any non-mock provider.
        return settings.mastering.enabled and settings.generation.provider != "mock"
    if name == "audio_analysis":
        # Required wherever post-production runs, which is every mode that puts generated
        # audio on air: without it, nothing can validate a track and §6's central rule —
        # that a failing track never becomes READY — cannot be enforced at all.
        return True
    if name == "fingerprinting":
        # Never required: the built-in provider always works. Its absence is a capability
        # difference to report, not a reason to refuse to start.
        return False
    if name == "ace_step_models":
        # Required exactly when the station intends to generate with ACE-Step. A mock
        # station missing a 6 GB checkpoint is not broken.
        return settings.generation.provider == "ace_step"
    if name == "node":
        # The station broadcasts without a frontend build.
        return False
    if name == "gpu":
        return settings.generation.provider == "ace_step"
    if name == "generation_provider":
        return settings.generation.provider != "mock"
    if name == "ace_step_environment":
        return False
    if name == "market_feed":
        return production or settings.market.feed != "simulated"
    if name == "obs":
        return production and settings.obs.enabled
    if name == "audio_device":
        return settings.audio.sink == "sounddevice"
    return False


__all__ = [
    "ACE_STEP_PYTHON_RANGE",
    "REQUIRED_PYTHON",
    "check_ace_step_environment",
    "check_audio_device",
    "check_directories",
    "check_disk_space",
    "check_ffmpeg",
    "check_generation_provider",
    "check_gpu",
    "check_market_feed",
    "check_node",
    "check_python",
    "is_required",
]
