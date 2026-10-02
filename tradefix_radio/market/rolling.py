"""Rolling windows and percentile normalisation (§6).

This module is the mechanism behind §6's instruction to "normalize using rolling
historical distributions" and to "not make assumptions about absolute XAUUSD price
values". Everything the regime engine and energy score consume is a **rank within
a recent window**, not a raw quantity — which is why gold at 1 800 and gold at
4 000 produce identical music for identical relative behaviour.

Deliberately **no third-party numeric dependency**. The feature engine runs once
per bar (once a minute in production), so NumPy would buy nothing measurable, and
keeping the market layer dependency-free means it imports instantly, can never be
broken by a wheel problem, and is trivially testable. Percentile lookups use a
sorted mirror of the window with ``bisect``, giving O(log n) queries and O(n)
insertion — negligible at these rates and exact, unlike a streaming approximation.
"""

from __future__ import annotations

import bisect
import math
from collections import deque
from collections.abc import Iterable, Iterator


class RollingWindow:
    """Fixed-capacity window of floats with exact summary statistics.

    Maintains both insertion order (for recency queries) and a sorted mirror (for
    percentiles and medians). The duplication costs memory proportional to the
    window, which for the default 720 bars is a few kilobytes — a trade worth
    making to keep percentile ranks exact rather than approximate.

    Non-finite values are rejected rather than stored. A NaN entering a rolling
    window silently poisons every subsequent statistic, and §4's warm-up flag
    exists precisely so the engine can be honest about not knowing yet instead of
    propagating garbage.
    """

    __slots__ = ("_capacity", "_sorted", "_sum", "_sum_squares", "_values")

    def __init__(self, capacity: int, values: Iterable[float] | None = None) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._capacity = capacity
        self._values: deque[float] = deque(maxlen=capacity)
        self._sorted: list[float] = []
        self._sum = 0.0
        self._sum_squares = 0.0
        for value in values or ():
            self.push(value)

    # -- mutation ----------------------------------------------------------

    def push(self, value: float) -> None:
        """Append a value, evicting the oldest once at capacity."""
        if not math.isfinite(value):
            raise ValueError(f"refusing to store a non-finite value: {value!r}")
        if len(self._values) == self._capacity:
            evicted = self._values[0]
            index = bisect.bisect_left(self._sorted, evicted)
            # The value is guaranteed present; this is an invariant, not a guess.
            del self._sorted[index]
            self._sum -= evicted
            self._sum_squares -= evicted * evicted
        self._values.append(value)
        bisect.insort(self._sorted, value)
        self._sum += value
        self._sum_squares += value * value

    def clear(self) -> None:
        self._values.clear()
        self._sorted.clear()
        self._sum = 0.0
        self._sum_squares = 0.0

    # -- introspection -----------------------------------------------------

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self) -> Iterator[float]:
        return iter(self._values)

    def __bool__(self) -> bool:
        return bool(self._values)

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def is_full(self) -> bool:
        return len(self._values) == self._capacity

    @property
    def values(self) -> tuple[float, ...]:
        """Insertion-ordered copy, oldest first."""
        return tuple(self._values)

    @property
    def latest(self) -> float | None:
        return self._values[-1] if self._values else None

    @property
    def oldest(self) -> float | None:
        return self._values[0] if self._values else None

    # -- statistics --------------------------------------------------------

    @property
    def mean(self) -> float:
        if not self._values:
            return 0.0
        return self._sum / len(self._values)

    @property
    def variance(self) -> float:
        """Population variance, clamped at zero.

        Computed from running sums rather than a second pass. The ``max(0.0, ...)``
        is not cosmetic: with running sums, catastrophic cancellation can produce a
        tiny negative result for a constant series, and ``sqrt`` of that raises.
        """
        count = len(self._values)
        if count < 2:
            return 0.0
        mean = self._sum / count
        return max(0.0, self._sum_squares / count - mean * mean)

    @property
    def stdev(self) -> float:
        return math.sqrt(self.variance)

    @property
    def minimum(self) -> float | None:
        return self._sorted[0] if self._sorted else None

    @property
    def maximum(self) -> float | None:
        return self._sorted[-1] if self._sorted else None

    def median(self) -> float | None:
        count = len(self._sorted)
        if count == 0:
            return None
        middle = count // 2
        if count % 2:
            return self._sorted[middle]
        return (self._sorted[middle - 1] + self._sorted[middle]) / 2.0

    def quantile(self, fraction: float) -> float | None:
        """Linear-interpolated quantile, ``fraction`` in [0, 1]."""
        if not 0.0 <= fraction <= 1.0:
            raise ValueError("fraction must be within [0, 1]")
        count = len(self._sorted)
        if count == 0:
            return None
        if count == 1:
            return self._sorted[0]
        position = fraction * (count - 1)
        lower = math.floor(position)
        upper = min(lower + 1, count - 1)
        weight = position - lower
        return self._sorted[lower] * (1.0 - weight) + self._sorted[upper] * weight

    def percentile_rank(self, value: float) -> float:
        """Where ``value`` sits in the window, as 0–100.

        Uses the **midpoint** convention: the fraction of stored values strictly
        below, plus half the fraction equal. This matters for the many series that
        plateau — a flat market produces long runs of identical ATR, and a
        strictly-less-than rank would report 0 for a value that is in fact typical,
        making a quiet market look like a record low and dragging energy to zero.

        An empty window returns 50.0: neutral, because "no opinion" must not read
        as "extreme".
        """
        count = len(self._sorted)
        if count == 0:
            return 50.0
        left = bisect.bisect_left(self._sorted, value)
        right = bisect.bisect_right(self._sorted, value)
        below = left
        equal = right - left
        return 100.0 * (below + equal / 2.0) / count

    def percentile_rank_stable(self, value: float, min_samples: int) -> float:
        """Percentile rank blended toward neutral while the window is sparse.

        A rank computed from a handful of observations is not a weak estimate of the
        true rank — it is a different quantity. With four samples in the window, the
        fourth-highest ATR the station has ever seen ranks at ~88, so a cold start
        reads as near-record volatility and the station opens *every run* playing
        energetic music for the first half hour before settling. That is audibly wrong
        and looks like a feature rather than a bug.

        The fix is to interpolate from 50 (no opinion) to the measured rank in
        proportion to how full the window is, converging smoothly as evidence
        accumulates. Blending weight depends only on the *count* of samples, never on
        their values, so §6 scale invariance is preserved.
        """
        rank = self.percentile_rank(value)
        if min_samples <= 1:
            return rank
        weight = min(1.0, len(self._values) / min_samples)
        return 50.0 * (1.0 - weight) + rank * weight

    def normalised_position(self, value: float) -> float:
        """Where ``value`` sits between the window's min and max, as 0–1.

        Unlike :meth:`percentile_rank` this is distribution-free and reacts to a
        single new extreme immediately, which is what §4's
        ``session_range_position`` needs. Returns 0.5 for a degenerate window.
        """
        low, high = self.minimum, self.maximum
        if low is None or high is None or high <= low:
            return 0.5
        return min(1.0, max(0.0, (value - low) / (high - low)))

    def slope_per_step(self) -> float:
        """Least-squares slope in units per step.

        Preferred over (last - first) / n because a single spike at either end
        would otherwise dominate the trend reading. Returns 0.0 below two points.
        """
        count = len(self._values)
        if count < 2:
            return 0.0
        # x is 0..count-1, so the x statistics are closed-form.
        mean_x = (count - 1) / 2.0
        mean_y = self._sum / count
        numerator = 0.0
        denominator = 0.0
        for index, value in enumerate(self._values):
            dx = index - mean_x
            numerator += dx * (value - mean_y)
            denominator += dx * dx
        if denominator == 0.0:
            return 0.0
        return numerator / denominator


class ExponentialSmoother:
    """Exponential moving average with an explicit, inspectable state.

    Used for §6's ``smoothed_energy`` and the regime engine's score smoothing. A
    class rather than a function because the state must survive a restart — §96
    requires creative and operational memory to persist — and a named object can
    be serialised deliberately.
    """

    __slots__ = ("_alpha", "_value")

    def __init__(self, alpha: float, initial: float | None = None) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be within (0, 1]")
        self._alpha = alpha
        self._value = initial

    @property
    def alpha(self) -> float:
        return self._alpha

    @property
    def value(self) -> float | None:
        return self._value

    def push(self, value: float) -> float:
        """Fold in a new observation and return the smoothed result.

        The first observation is adopted verbatim rather than blended toward zero.
        Seeding from zero would make the station open every run at minimum energy
        and climb for several minutes regardless of the market — audibly wrong, and
        the kind of bug that looks like a feature.
        """
        if not math.isfinite(value):
            raise ValueError(f"refusing to smooth a non-finite value: {value!r}")
        if self._value is None:
            self._value = value
        else:
            self._value = self._alpha * value + (1.0 - self._alpha) * self._value
        return self._value

    def reset(self, initial: float | None = None) -> None:
        self._value = initial


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    """Constrain ``value`` to ``[low, high]``, mapping non-finite input to ``low``.

    Non-finite handling is deliberate rather than defensive: contracts reject
    NaN/inf, so a value that is not finite by the time it reaches a clamp indicates
    a computation failure upstream. Mapping to the floor keeps the station running
    on conservative programming instead of crashing the playout process, and the
    feature engine's ``sufficient_history`` flag is what reports the real problem.
    """
    if not math.isfinite(value):
        return low
    return max(low, min(high, value))


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Division that yields ``default`` instead of raising or returning inf.

    Division by a near-zero denominator is routine in market maths — a perfectly
    flat series has zero range and zero ATR — so this is the normal path, not an
    error path.
    """
    if denominator == 0.0 or not math.isfinite(denominator):
        return default
    result = numerator / denominator
    return result if math.isfinite(result) else default


__all__ = [
    "ExponentialSmoother",
    "RollingWindow",
    "clamp",
    "safe_divide",
]
