"""Technical indicators (§4).

Standard formulations, implemented as **incremental state machines** rather than
batch functions over a window. The reason is §64: a 7-day endurance run processes
~10 000 bars, and recomputing a 14-period ADX from scratch on every bar would make
the market engine the slowest thing in the simulation. Incremental updates are O(1)
per bar.

Scale invariance is called out per indicator. §6 forbids assumptions about absolute
price, and the energy score must be identical if every price is multiplied by ten.
Indicators that are *not* scale-free (ATR, true range) are only ever consumed as
percentile ranks or as ratios against another price-scaled quantity — never
directly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from tradefix_radio.market.rolling import RollingWindow, safe_divide


@dataclass(frozen=True)
class Bar:
    """One completed OHLCV bar."""

    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if self.low > self.high:
            raise ValueError(f"bar low {self.low} exceeds high {self.high}")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("bar prices must be positive")
        if self.volume < 0:
            raise ValueError("bar volume must not be negative")

    @property
    def range(self) -> float:
        """High minus low. **Not** scale-free; use :attr:`relative_range`."""
        return self.high - self.low

    @property
    def relative_range(self) -> float:
        """Range as a fraction of close. Scale-free."""
        return safe_divide(self.high - self.low, self.close)

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    @property
    def is_up(self) -> bool:
        return self.close >= self.open


def true_range(bar: Bar, previous_close: float | None) -> float:
    """Wilder's true range.

    Falls back to the bar's own range on the first bar, where there is no previous
    close. Returning 0 instead would make the first ATR reading meaningless and
    would take the full smoothing period to wash out.
    """
    if previous_close is None:
        return bar.high - bar.low
    return max(
        bar.high - bar.low,
        abs(bar.high - previous_close),
        abs(bar.low - previous_close),
    )


class WilderSmoother:
    """Wilder's smoothing (an EMA with alpha = 1/period).

    Seeded with a simple average of the first ``period`` values, as Wilder
    specified. The distinction matters: seeding from the first value alone makes
    early ATR and ADX readings track that one bar, and a station that starts during
    a spike would mis-read the market for its first quarter of an hour.
    """

    __slots__ = ("_count", "_period", "_seed_sum", "_value")

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("period must be at least 1")
        self._period = period
        self._value: float | None = None
        self._seed_sum = 0.0
        self._count = 0

    @property
    def period(self) -> int:
        return self._period

    @property
    def value(self) -> float | None:
        return self._value

    @property
    def is_ready(self) -> bool:
        return self._value is not None

    def push(self, value: float) -> float | None:
        if not math.isfinite(value):
            raise ValueError(f"refusing to smooth a non-finite value: {value!r}")
        if self._value is None:
            self._seed_sum += value
            self._count += 1
            if self._count >= self._period:
                self._value = self._seed_sum / self._period
            return self._value
        self._value = (self._value * (self._period - 1) + value) / self._period
        return self._value


class AtrCalculator:
    """Average true range.

    **Not scale-free** — ATR is in price units. Consumed only as a percentile rank
    or as a ratio (see :class:`CompressionCalculator`), never as an absolute.
    """

    __slots__ = ("_previous_close", "_smoother")

    def __init__(self, period: int = 14) -> None:
        self._smoother = WilderSmoother(period)
        self._previous_close: float | None = None

    @property
    def value(self) -> float | None:
        return self._smoother.value

    @property
    def is_ready(self) -> bool:
        return self._smoother.is_ready

    def push(self, bar: Bar) -> float | None:
        result = self._smoother.push(true_range(bar, self._previous_close))
        self._previous_close = bar.close
        return result


class RsiCalculator:
    """Relative strength index. Scale-free (built from gain/loss ratios)."""

    __slots__ = ("_gains", "_losses", "_previous_close")

    def __init__(self, period: int = 14) -> None:
        self._gains = WilderSmoother(period)
        self._losses = WilderSmoother(period)
        self._previous_close: float | None = None

    @property
    def is_ready(self) -> bool:
        return self._gains.is_ready and self._losses.is_ready

    @property
    def value(self) -> float:
        """0–100, defaulting to a neutral 50 before warm-up."""
        gain = self._gains.value
        loss = self._losses.value
        if gain is None or loss is None:
            return 50.0
        if loss == 0.0:
            # Unbroken advance. 100 is correct, and guards the division below.
            return 100.0 if gain > 0 else 50.0
        rs = gain / loss
        return 100.0 - (100.0 / (1.0 + rs))

    def push(self, bar: Bar) -> float:
        if self._previous_close is None:
            self._previous_close = bar.close
            return 50.0
        change = bar.close - self._previous_close
        self._gains.push(max(0.0, change))
        self._losses.push(max(0.0, -change))
        self._previous_close = bar.close
        return self.value


class AdxCalculator:
    """Average directional index with +DI / -DI. Scale-free.

    ADX measures trend *strength* without direction; the ``+DI > -DI`` comparison
    supplies direction. Keeping them separate is what lets the regime engine say
    "strong trend, direction unclear" — a real state that a single signed number
    cannot express, and one that must not be programmed as a breakout.
    """

    __slots__ = (
        "_adx",
        "_minus_dm",
        "_plus_dm",
        "_previous_bar",
        "_true_range",
    )

    def __init__(self, period: int = 14) -> None:
        self._plus_dm = WilderSmoother(period)
        self._minus_dm = WilderSmoother(period)
        self._true_range = WilderSmoother(period)
        self._adx = WilderSmoother(period)
        self._previous_bar: Bar | None = None

    @property
    def is_ready(self) -> bool:
        return self._adx.is_ready

    @property
    def plus_di(self) -> float:
        return self._directional_indicator(self._plus_dm.value)

    @property
    def minus_di(self) -> float:
        return self._directional_indicator(self._minus_dm.value)

    def _directional_indicator(self, smoothed_dm: float | None) -> float:
        atr = self._true_range.value
        if smoothed_dm is None or atr is None or atr == 0.0:
            return 0.0
        return 100.0 * smoothed_dm / atr

    @property
    def value(self) -> float:
        """ADX, 0–100. Defaults to 0 (no measurable trend) before warm-up."""
        return self._adx.value or 0.0

    @property
    def direction_sign(self) -> int:
        """``+1`` bullish, ``-1`` bearish, ``0`` when the DIs are level."""
        plus, minus = self.plus_di, self.minus_di
        if plus > minus:
            return 1
        if minus > plus:
            return -1
        return 0

    def push(self, bar: Bar) -> float:
        previous = self._previous_bar
        self._previous_bar = bar
        if previous is None:
            return 0.0

        up_move = bar.high - previous.high
        down_move = previous.low - bar.low
        # Only the larger move counts, and only if positive. Both being positive
        # means an outside bar, which Wilder treats as directionless.
        plus_dm = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm = down_move if down_move > up_move and down_move > 0 else 0.0

        self._plus_dm.push(plus_dm)
        self._minus_dm.push(minus_dm)
        self._true_range.push(true_range(bar, previous.close))

        plus_di, minus_di = self.plus_di, self.minus_di
        total = plus_di + minus_di
        dx = 0.0 if total == 0.0 else 100.0 * abs(plus_di - minus_di) / total
        self._adx.push(dx)
        return self.value


class CompressionCalculator:
    """Range compression and expansion, as scale-free ratios.

    The ratio of a short-window average true range to a long-window one. Below 1
    the market is coiling; above 1 it is expanding. Expressing it as a ratio rather
    than a difference is what makes it scale-free, and therefore usable as a
    musical input under §6.

    Both scores are reported because they are **not** complements. A market can be
    neither compressing nor expanding (steady), and collapsing that into one axis
    would make "normal" indistinguishable from "transitioning".
    """

    __slots__ = ("_long", "_short")

    def __init__(self, short_period: int = 10, long_period: int = 60) -> None:
        if short_period >= long_period:
            raise ValueError("short_period must be below long_period")
        self._short = RollingWindow(short_period)
        self._long = RollingWindow(long_period)

    @property
    def is_ready(self) -> bool:
        # The short window being full is enough to produce a reading; requiring the
        # long window too would leave the engine blind for an hour after startup.
        return self._short.is_full and len(self._long) >= self._short.capacity * 2

    @property
    def ratio(self) -> float:
        """Short-term range over long-term range. 1.0 means unchanged."""
        if not self._long:
            return 1.0
        return safe_divide(self._short.mean, self._long.mean, default=1.0)

    @property
    def compression_score(self) -> float:
        """0–100; 100 means maximally coiled.

        A ratio of 1.0 maps to 0, and the score reaches 100 as the ratio approaches
        0.25 — a short-term range a quarter of normal, which is a genuinely tight
        coil rather than ordinary quiet.
        """
        ratio = self.ratio
        if ratio >= 1.0:
            return 0.0
        return min(100.0, 100.0 * (1.0 - ratio) / 0.75)

    @property
    def expansion_score(self) -> float:
        """0–100; 100 means range is three times normal or more."""
        ratio = self.ratio
        if ratio <= 1.0:
            return 0.0
        return min(100.0, 100.0 * (ratio - 1.0) / 2.0)

    def push(self, bar: Bar, previous_close: float | None) -> None:
        # Relative true range keeps this scale-free.
        relative = safe_divide(true_range(bar, previous_close), bar.close)
        self._short.push(relative)
        self._long.push(relative)


class BreakoutCalculator:
    """How decisively price has left its recent range (§4 ``breakout_strength``).

    Measured in **ATR units** beyond the prior range boundary, so it is scale-free
    and comparable across volatility regimes. Two points on the design:

    * The lookback **excludes the current bar**, otherwise a bar is always inside
      its own range and a breakout can never register.
    * Volume confirmation is a *multiplier*, not an additive term. A move on no
      volume is suspect regardless of size, and adding the two would let a large
      move with no participation still score highly — exactly the fake breakout the
      §7 scenario exists to produce.
    """

    __slots__ = ("_closes", "_highs", "_lows", "_volumes")

    def __init__(self, lookback: int = 40) -> None:
        if lookback < 5:
            raise ValueError("lookback must be at least 5")
        self._highs = RollingWindow(lookback)
        self._lows = RollingWindow(lookback)
        self._closes = RollingWindow(lookback)
        self._volumes = RollingWindow(lookback)

    @property
    def is_ready(self) -> bool:
        return len(self._highs) >= 10

    @property
    def recent_high(self) -> float | None:
        """Highest high in the lookback window, excluding any unpushed bar."""
        return self._highs.maximum

    @property
    def recent_low(self) -> float | None:
        return self._lows.minimum

    @property
    def mean_volume(self) -> float:
        return self._volumes.mean

    def push(self, bar: Bar) -> None:
        self._highs.push(bar.high)
        self._lows.push(bar.low)
        self._closes.push(bar.close)
        self._volumes.push(bar.volume)

    def evaluate(self, bar: Bar, atr: float | None) -> tuple[float, int]:
        """Return ``(strength 0-100, direction sign)`` for ``bar``.

        ``bar`` must **not** already have been pushed, so the stored range is the
        prior one.
        """
        if not self.is_ready or atr is None or atr <= 0:
            return 0.0, 0
        prior_high = self._highs.maximum
        prior_low = self._lows.minimum
        if prior_high is None or prior_low is None:
            return 0.0, 0

        above = (bar.close - prior_high) / atr
        below = (prior_low - bar.close) / atr
        if above <= 0 and below <= 0:
            return 0.0, 0

        if above >= below:
            excursion, sign = above, 1
        else:
            excursion, sign = below, -1

        # Two ATR beyond the range is a decisive break; that maps to 100 before
        # volume confirmation.
        base = min(100.0, 100.0 * excursion / 2.0)

        volume_ratio = safe_divide(bar.volume, self._volumes.mean, default=1.0)
        # Clamped to [0.5, 1.5]: volume should confirm or temper, never dominate.
        confirmation = min(1.5, max(0.5, volume_ratio))
        return min(100.0, base * confirmation), sign


class ReturnCalculator:
    """Fractional returns over several horizons. Scale-free by construction."""

    __slots__ = ("_closes",)

    def __init__(self, max_lookback: int = 60) -> None:
        self._closes = RollingWindow(max_lookback + 1)

    def push(self, close: float) -> None:
        self._closes.push(close)

    def ready_for(self, bars: int) -> bool:
        return len(self._closes) > bars

    def over(self, bars: int) -> float:
        """Fractional return over ``bars`` bars. 0.0 when history is short.

        Returning 0 during warm-up rather than a partial figure is deliberate: a
        "15-minute return" computed from three bars is not a small error, it is a
        different quantity, and the engine's ``sufficient_history`` flag is the
        honest way to report not knowing.
        """
        if bars < 1:
            raise ValueError("bars must be at least 1")
        values = self._closes.values
        if len(values) <= bars:
            return 0.0
        past, current = values[-(bars + 1)], values[-1]
        return safe_divide(current - past, past)

    @property
    def closes(self) -> RollingWindow:
        return self._closes

    def realised_volatility(self, bars: int) -> float:
        """Standard deviation of per-bar returns over ``bars``. Scale-free."""
        values = self._closes.values
        if len(values) < 3:
            return 0.0
        window = values[-(bars + 1) :]
        returns = [
            safe_divide(window[i] - window[i - 1], window[i - 1])
            for i in range(1, len(window))
        ]
        if len(returns) < 2:
            return 0.0
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / len(returns)
        return math.sqrt(variance)


__all__ = [
    "AdxCalculator",
    "AtrCalculator",
    "Bar",
    "BreakoutCalculator",
    "CompressionCalculator",
    "ReturnCalculator",
    "RsiCalculator",
    "WilderSmoother",
    "true_range",
]
