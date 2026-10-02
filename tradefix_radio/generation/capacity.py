"""Generation capacity and latency (§93, milestone 4.3).

§93: ``generation_capacity_ratio = audio_duration_generated / generation_wall_time``.

A four-minute track produced in thirty seconds is a ratio of 8 — the station can generate
eight times faster than it broadcasts, so the buffer fills. The same track produced in six
minutes is a ratio of 0.67, and the buffer drains however full it currently looks. That single
number is the difference between "45 minutes queued and healthy" and "45 minutes queued and
dying", which is the distinction §93 exists to make.

Three design points:

**Ratios are per job, then aggregated — not totals over totals.** Dividing total audio by total
wall time silently credits concurrency: two workers each at ratio 1.0 would report 2.0, so a
station that is exactly breaking even would look comfortable. The per-job ratio measures what
the *provider* can do, and concurrency is accounted for separately where the scheduler decides
how many jobs to run.

**Latency percentiles, not just a mean.** §93 asks for p50 and p95 because the shape matters:
a mean of 40 seconds could be every job taking 40 seconds, or nine taking 10 and one taking
310. The first is healthy, the second is a provider about to time out, and the mean cannot tell
them apart.

**Failures count toward latency but not capacity.** A job that burned four minutes and produced
nothing is real wall time the station spent, so excluding it would overstate throughput — but
it produced zero audio, so including it in the ratio would report a capacity of zero and make
one failure look like total collapse. They are tracked as separate series.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

#: Capacity ratio below which generation is losing to playback.
#:
#: Exactly 1.0 is break-even: the station generates one second of audio per second of wall
#: time, so the buffer holds steady and never recovers from a dip.
BREAK_EVEN_RATIO = 1.0

#: Ratio reported before anything has been measured.
#:
#: 1.0 — break-even — rather than 0 or infinity. Zero would make a cold-started station
#: immediately believe it was starving and suppress all experimentation (§94) for the first
#: few tracks; infinity would make it reckless. Break-even is the honest "no opinion yet",
#: and :attr:`CapacitySnapshot.samples` lets a caller tell it from a measurement.
COLD_START_RATIO = 1.0


@dataclass(frozen=True)
class CapacitySnapshot:
    """What the scheduler reads. A value object, so it cannot change mid-decision."""

    #: §93's headline figure: audio seconds produced per wall-clock second.
    capacity_ratio: float
    latency_p50_seconds: float
    latency_p95_seconds: float
    #: Successful jobs in the window. ``0`` means every figure here is a default.
    samples: int
    failures: int
    timeouts: int
    retries: int
    #: Mean audio duration of a generated track, for projecting how much a job will add.
    mean_audio_seconds: float

    @property
    def is_losing(self) -> bool:
        """Whether generation is falling behind playback."""
        return self.capacity_ratio < BREAK_EVEN_RATIO

    @property
    def has_measurements(self) -> bool:
        return self.samples > 0

    @property
    def failure_rate(self) -> float:
        total = self.samples + self.failures
        return self.failures / total if total else 0.0

    def seconds_to_generate(self, audio_seconds: float) -> float:
        """How long a track of ``audio_seconds`` is expected to take.

        Uses p95 rather than the ratio, because this answers a *planning* question — "will
        this arrive before the buffer runs out?" — and planning against the median means being
        wrong one time in two.
        """
        if not self.has_measurements or self.mean_audio_seconds <= 0:
            return self.latency_p95_seconds
        return self.latency_p95_seconds * (audio_seconds / self.mean_audio_seconds)


class CapacityTracker:
    """Rolling window of generation outcomes (§93).

    Window measured in **jobs**, not in time. A station that generated nothing for an hour
    should still report the capacity it had when it last worked, rather than decaying to zero
    and conflating "slow" with "stopped" — the latter is the buffer trajectory's job to notice
    (see :mod:`tradefix_radio.radio.buffer`), and a tracker that reported zero would make the
    two indistinguishable.
    """

    __slots__ = (
        "_audio_seconds",
        "_failures",
        "_latencies",
        "_ratios",
        "_retries",
        "_timeouts",
        "_window",
    )

    def __init__(self, window_jobs: int = 20) -> None:
        if window_jobs < 1:
            raise ValueError("window_jobs must be at least 1")
        self._window = window_jobs
        self._ratios: deque[float] = deque(maxlen=window_jobs)
        self._latencies: deque[float] = deque(maxlen=window_jobs)
        self._audio_seconds: deque[float] = deque(maxlen=window_jobs)
        # Failure counters are *lifetime*, not windowed: the §101 report wants totals, and the
        # scheduler reads the rate from the ratio rather than from these.
        self._failures = 0
        self._timeouts = 0
        self._retries = 0

    # -- recording ---------------------------------------------------------

    def record_success(self, *, audio_seconds: float, wall_seconds: float) -> None:
        """One completed generation."""
        if audio_seconds <= 0:
            raise ValueError("audio_seconds must be positive")
        if wall_seconds <= 0:
            # A zero or negative measurement means the clock went backwards or the provider
            # returned instantly. Clamp rather than raise: a monitoring input must not be able
            # to kill the generation worker.
            wall_seconds = 1e-6
        self._ratios.append(audio_seconds / wall_seconds)
        self._latencies.append(wall_seconds)
        self._audio_seconds.append(audio_seconds)

    def record_failure(self, *, wall_seconds: float, timeout: bool = False) -> None:
        """One failed generation.

        The wall time is recorded in the latency series — the station really did spend it —
        but nothing is added to the ratio series, because a zero-audio ratio would make a
        single failure read as total capacity collapse.
        """
        self._failures += 1
        if timeout:
            self._timeouts += 1
        if wall_seconds > 0:
            self._latencies.append(wall_seconds)

    def record_retry(self) -> None:
        self._retries += 1

    def reset(self) -> None:
        self._ratios.clear()
        self._latencies.clear()
        self._audio_seconds.clear()

    # -- reading -----------------------------------------------------------

    @property
    def window_jobs(self) -> int:
        return self._window

    def snapshot(self) -> CapacitySnapshot:
        return CapacitySnapshot(
            capacity_ratio=self._mean(self._ratios, COLD_START_RATIO),
            latency_p50_seconds=percentile(self._latencies, 50.0),
            latency_p95_seconds=percentile(self._latencies, 95.0),
            samples=len(self._ratios),
            failures=self._failures,
            timeouts=self._timeouts,
            retries=self._retries,
            mean_audio_seconds=self._mean(self._audio_seconds, 0.0),
        )

    @staticmethod
    def _mean(values: deque[float], default: float) -> float:
        return sum(values) / len(values) if values else default


def percentile(values: deque[float] | list[float], rank: float) -> float:
    """Linear-interpolated percentile. ``0.0`` for an empty series.

    Written out rather than pulled from NumPy because this is called from the scheduler's hot
    path on at most a few dozen values, where converting to an array costs more than the
    arithmetic. The interpolation matters at these sizes: on twenty samples, a nearest-rank p95
    is whichever single value happens to sit at index 18, which jumps around as the window
    slides.
    """
    if not 0.0 <= rank <= 100.0:
        raise ValueError("rank must be within 0-100")
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (rank / 100.0)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


__all__ = [
    "BREAK_EVEN_RATIO",
    "COLD_START_RATIO",
    "CapacitySnapshot",
    "CapacityTracker",
    "percentile",
]
