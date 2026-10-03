"""GPU measurement around a generation (§7.7).

§7.7 asks for free/used VRAM, utilisation and temperature captured *before* a generation, and
peak VRAM and duration captured *after*.

Why this is not `ResourceMonitor`
---------------------------------
`monitoring/resources.py` already queries `nvidia-smi`, and reusing it was the first thing
tried. It does not fit, for two reasons that are both deliberate parts of its own design:

* **It caches for 5 seconds.** That is right for a dashboard polled every second and wrong
  for bracketing a 20-second generation — the "before" and "after" samples would frequently
  be the same cached reading, and the peak would be whatever happened to be cached.
* **It does not query `memory.free`.** It reports total and used, which is what the §49 page
  shows. §7.7's pre-flight needs *free*, and on a shared desktop free is not
  `total − used` as far as a new allocation is concerned.

So this is a second, narrower probe: uncached, different fields, and able to sample
repeatedly during a long operation to find a peak. The dashboard's monitor is untouched.

**No GPU is not an error.** A CPU-only host returns ``None`` everywhere and generation
proceeds; §7.7's measurements are evidence, not preconditions.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator

_log = structlog.get_logger(__name__)

__all__ = ["GpuProbe", "GpuSample", "GpuUsage"]

#: Fields requested from nvidia-smi, in order.
_QUERY: Final = "memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu"

#: How long to wait for nvidia-smi before giving up on the sample.
#:
#: 5 s. The call normally returns in tens of milliseconds; a probe that blocks longer than
#: this is a sick driver, and waiting on it would delay the generation it is measuring.
_TIMEOUT_SECONDS: Final = 5.0

#: Interval between peak samples during a generation.
#:
#: 2 s. Each sample is a subprocess spawn, so this is a compromise: frequent enough to catch
#: the allocation plateau of a 15–30 s generation, infrequent enough that the measurement
#: does not meaningfully compete with the work for CPU.
_PEAK_INTERVAL_SECONDS: Final = 2.0


@dataclass(frozen=True)
class GpuSample:
    """One instantaneous reading."""

    total_mb: float
    used_mb: float
    free_mb: float
    utilization_percent: float
    temperature_c: float

    @property
    def used_bytes(self) -> int:
        return int(self.used_mb * 1024 * 1024)


@dataclass(frozen=True)
class GpuUsage:
    """What one generation cost, as far as the GPU is concerned.

    Every field is optional because every field is a measurement that may not have been
    available. A missing value is ``None`` and renders as absent; §86's rule against fake
    metrics applies as much to a plausible-looking zero here as anywhere else.
    """

    before: GpuSample | None = None
    after: GpuSample | None = None
    peak_used_mb: float | None = None
    samples_taken: int = 0

    @property
    def peak_vram_bytes(self) -> int | None:
        """Peak used VRAM, for :attr:`GenerationResult.peak_vram_bytes`."""
        if self.peak_used_mb is None:
            return None
        return int(self.peak_used_mb * 1024 * 1024)

    @property
    def delta_mb(self) -> float | None:
        """How much more VRAM was in use at the peak than before the job started.

        The honest attribution of cost on a shared card: total used includes every browser
        tab, so the *difference* is the closest this can get to "what the model took" — and
        even that is an upper bound if something else allocated meanwhile.
        """
        if self.peak_used_mb is None or self.before is None:
            return None
        return max(0.0, self.peak_used_mb - self.before.used_mb)

    def as_metadata(self) -> dict[str, object]:
        """The persisted record (§7.4's "GPU metrics when available")."""
        payload: dict[str, object] = {"samples_taken": self.samples_taken}
        if self.before is not None:
            payload["free_mb_before"] = round(self.before.free_mb, 1)
            payload["used_mb_before"] = round(self.before.used_mb, 1)
            payload["total_mb"] = round(self.before.total_mb, 1)
            payload["utilization_before"] = round(self.before.utilization_percent, 1)
            payload["temperature_c_before"] = round(self.before.temperature_c, 1)
        if self.after is not None:
            payload["used_mb_after"] = round(self.after.used_mb, 1)
            payload["temperature_c_after"] = round(self.after.temperature_c, 1)
        if self.peak_used_mb is not None:
            payload["peak_used_mb"] = round(self.peak_used_mb, 1)
        if self.delta_mb is not None:
            payload["attributable_mb"] = round(self.delta_mb, 1)
        return payload


class GpuProbe:
    """Uncached `nvidia-smi` sampling, with peak tracking across a generation."""

    def __init__(self, *, enabled: bool = True) -> None:
        self._executable = shutil.which("nvidia-smi") if enabled else None

    @property
    def available(self) -> bool:
        return self._executable is not None

    async def sample(self) -> GpuSample | None:
        """One reading, or ``None`` when there is no GPU or the query failed."""
        if self._executable is None:
            return None
        try:
            process = await asyncio.create_subprocess_exec(
                self._executable,
                f"--query-gpu={_QUERY}",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=_TIMEOUT_SECONDS
            )
        except (OSError, asyncio.TimeoutError) as error:
            # Debug, not warning: on a CPU host or a busy driver this is routine, and a
            # warning per generation would bury the log in noise about a non-problem.
            _log.debug("gpu.sample_failed", error=str(error))
            return None

        lines = stdout.decode("utf-8", errors="replace").strip().splitlines()
        if not lines:
            return None
        parts = [part.strip() for part in lines[0].split(",")]
        if len(parts) < 5:
            return None
        try:
            return GpuSample(
                total_mb=float(parts[0]),
                used_mb=float(parts[1]),
                free_mb=float(parts[2]),
                utilization_percent=float(parts[3]),
                temperature_c=float(parts[4]),
            )
        except ValueError:
            return None

    async def free_mb(self) -> float | None:
        """Free VRAM right now, for the §7.7 pre-flight check."""
        sample = await self.sample()
        return None if sample is None else sample.free_mb

    @contextlib.asynccontextmanager
    async def measure(self) -> AsyncIterator[_UsageHolder]:
        """Bracket a generation, sampling for the peak while it runs.

        Yields a mutable holder whose ``usage`` is populated on exit. The sampler runs as a
        task and is always cancelled in ``finally`` — a probe that outlived a failed
        generation would keep spawning subprocesses for the life of the process.
        """
        before = await self.sample()
        peak = before.used_mb if before is not None else None
        samples = 1 if before is not None else 0
        holder = _UsageHolder()
        stop = asyncio.Event()

        async def poll() -> None:
            nonlocal peak, samples
            while not stop.is_set():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=_PEAK_INTERVAL_SECONDS)
                if stop.is_set():
                    return
                reading = await self.sample()
                if reading is not None:
                    samples += 1
                    peak = reading.used_mb if peak is None else max(peak, reading.used_mb)

        task = asyncio.create_task(poll(), name="gpu-peak-sampler") if self.available else None
        try:
            yield holder
        finally:
            stop.set()
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            after = await self.sample()
            if after is not None:
                samples += 1
                peak = after.used_mb if peak is None else max(peak, after.used_mb)
            holder.usage = GpuUsage(
                before=before, after=after, peak_used_mb=peak, samples_taken=samples
            )


class _UsageHolder:
    """Mutable cell so :meth:`GpuProbe.measure` can report after the body has run."""

    __slots__ = ("usage",)

    def __init__(self) -> None:
        self.usage = GpuUsage()
