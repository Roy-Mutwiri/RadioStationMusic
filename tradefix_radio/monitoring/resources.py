"""Host resource sampling for the §49 System page and §64 leak detection.

CPU, RAM, disk and process statistics come from ``psutil``. GPU statistics come
from ``nvidia-smi``, because ADR-02 keeps torch out of this process entirely — we
observe the GPU that a separate ACE-Step process uses, and shelling out to the
driver's own tool is both lighter and more honest than linking a CUDA runtime we
do not otherwise need.

Optional fields stay ``None`` rather than zero when a measurement is unavailable.
Reporting 0 °C for an absent GPU would make the §49 page lie, and §64's leak
detection would read "no growth" where it should read "no data".
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
from pathlib import Path

import psutil
import structlog

from tradefix_radio.contracts.health import ResourceSnapshotV1
from tradefix_radio.core.clock import Clock, SystemClock

_log = structlog.get_logger(__name__)

#: GPU values change slowly relative to a 1 s UI refresh, and each query spawns a
#: process. Cached for this long so a dashboard cannot fork nvidia-smi every tick.
GPU_CACHE_SECONDS = 5.0


class ResourceMonitor:
    """Samples host and process resources."""

    def __init__(self, *, disk_path: Path, clock: Clock | None = None) -> None:
        self._disk_path = disk_path
        self._clock: Clock = clock or SystemClock()
        self._process = psutil.Process(os.getpid())
        self._gpu_cache: tuple[float, dict[str, float | str] | None] | None = None
        # Prime the CPU sampler. psutil's first cpu_percent() call always returns
        # 0.0 because it has no interval to compare against; priming here means the
        # first real sample is meaningful rather than a misleading zero.
        with contextlib.suppress(Exception):
            self._process.cpu_percent(interval=None)
            psutil.cpu_percent(interval=None)

    async def sample(self) -> ResourceSnapshotV1:
        """Take one snapshot. Safe to call frequently."""
        gpu = await self._gpu_stats()

        memory = psutil.virtual_memory()
        disk_target = self._disk_path if self._disk_path.exists() else Path.cwd()
        try:
            disk = shutil.disk_usage(disk_target)
            disk_free, disk_total = disk.free, disk.total
        except OSError as exc:
            _log.warning("resources.disk_usage_failed", path=str(disk_target), error=str(exc))
            disk_free, disk_total = 0, 1

        with self._process.oneshot():
            rss = int(self._process.memory_info().rss)
            threads = int(self._process.num_threads())
            handles = self._open_handles()

        return ResourceSnapshotV1(
            at=self._clock.now(),
            cpu_percent=float(psutil.cpu_percent(interval=None)),
            ram_used_bytes=int(memory.total - memory.available),
            ram_total_bytes=int(memory.total),
            disk_free_bytes=int(disk_free),
            disk_total_bytes=int(disk_total),
            gpu_name=str(gpu["name"]) if gpu and "name" in gpu else None,
            gpu_utilization_percent=_as_float(gpu, "utilization"),
            vram_used_bytes=_as_bytes_from_mb(gpu, "vram_used_mb"),
            vram_total_bytes=_as_bytes_from_mb(gpu, "vram_total_mb"),
            gpu_temperature_c=_as_float(gpu, "temperature"),
            process_rss_bytes=rss,
            open_file_handles=handles,
            thread_count=threads,
        )

    def _open_handles(self) -> int | None:
        """Open handle count, or ``None`` where unavailable.

        §65 watches this for leaks. Windows exposes handles, POSIX file
        descriptors; both are reported through the same field because the leak
        signal — a number that only goes up — is identical.
        """
        try:
            if sys.platform == "win32":
                return int(self._process.num_handles())
            return int(self._process.num_fds())
        except (psutil.AccessDenied, psutil.NoSuchProcess, AttributeError):
            return None

    async def _gpu_stats(self) -> dict[str, float | str] | None:
        """Cached nvidia-smi query, or ``None`` when no GPU is present."""
        now = self._clock.monotonic()
        if self._gpu_cache is not None and now - self._gpu_cache[0] < GPU_CACHE_SECONDS:
            return self._gpu_cache[1]

        executable = shutil.which("nvidia-smi")
        if executable is None:
            self._gpu_cache = (now, None)
            return None

        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5.0)
        except (OSError, asyncio.TimeoutError) as exc:
            _log.debug("resources.gpu_query_failed", error=str(exc))
            self._gpu_cache = (now, None)
            return None

        line = stdout.decode("utf-8", errors="replace").strip().splitlines()
        if not line:
            self._gpu_cache = (now, None)
            return None
        parts = [part.strip() for part in line[0].split(",")]
        if len(parts) < 5:
            self._gpu_cache = (now, None)
            return None
        try:
            stats: dict[str, float | str] = {
                "name": parts[0],
                "vram_total_mb": float(parts[1]),
                "vram_used_mb": float(parts[2]),
                "utilization": float(parts[3]),
                "temperature": float(parts[4]),
            }
        except ValueError:
            self._gpu_cache = (now, None)
            return None
        self._gpu_cache = (now, stats)
        return stats


def _as_float(stats: dict[str, float | str] | None, key: str) -> float | None:
    if not stats or key not in stats:
        return None
    value = stats[key]
    return float(value) if isinstance(value, (int, float)) else None


def _as_bytes_from_mb(stats: dict[str, float | str] | None, key: str) -> int | None:
    megabytes = _as_float(stats, key)
    if megabytes is None:
        return None
    return int(megabytes * 1024 * 1024)


__all__ = ["GPU_CACHE_SECONDS", "ResourceMonitor"]
