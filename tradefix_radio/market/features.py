"""Feature engine (§4) and tick-to-bar aggregation.

Produces the full §4 feature vector from a stream of snapshots. Two properties are
load-bearing and tested as such:

**Affine invariance.** Multiplying every price by a constant must not change
``energy``, the regime, or any ``*_percentile`` / score field. That is §6's "do not
make assumptions about absolute XAUUSD price values" expressed as a testable
property. Achieving it requires discipline at every step: returns are fractional,
ADX and RSI are ratio-based, the MA slope is reported *per bar as a fraction of
price*, and ``distance_from_high`` / ``distance_from_low`` are in **ATR units**
rather than dollars.

**Honest warm-up.** During the first ``warmup_bars`` the engine has real but
unreliable numbers. It publishes them with ``sufficient_history=False`` rather than
emitting zeros that look like data, and the regime engine refuses to classify until
the flag flips. This is §24's "never pass unusable output downstream", applied to
market data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from tradefix_radio.config.schema import MarketSettings
from tradefix_radio.contracts.market import MarketFeaturesV1, MarketSnapshotV1
from tradefix_radio.core.clock import UTC
from tradefix_radio.market.indicators import (
    AdxCalculator,
    AtrCalculator,
    Bar,
    BreakoutCalculator,
    CompressionCalculator,
    ReturnCalculator,
    RsiCalculator,
)
from tradefix_radio.market.rolling import RollingWindow, clamp, safe_divide
from tradefix_radio.market.sessions import SessionTracker

#: Horizons for the §4 return features, in bars. With the default 60 s bar these
#: are literally 1, 5 and 15 minutes; with another bar size they remain "1, 5 and
#: 15 bars", which is what the names mean operationally.
RETURN_HORIZONS = (1, 5, 15)


@dataclass
class PartialBar:
    """A bar under construction."""

    opened_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    ticks: int = 1

    def update(self, price: float, volume_delta: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += max(0.0, volume_delta)
        self.ticks += 1

    def finish(self) -> Bar:
        return Bar(
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


class BarAggregator:
    """Groups snapshots into fixed-duration bars.

    Bars are aligned to wall-clock boundaries (``timestamp // bar_seconds``) rather
    than to the first tick seen. Alignment matters because the station restarts: a
    first-tick-relative grid would shift on every restart, and percentile windows
    built before and after would be comparing differently-sized buckets.

    A gap longer than one bar does **not** produce synthetic filler bars. Inventing
    data to paper over a weekend or an outage is exactly what §86 forbids; the gap
    is reported so the caller can decide.
    """

    __slots__ = ("_bar_seconds", "_current", "_last_volume")

    def __init__(self, bar_seconds: int) -> None:
        if bar_seconds < 1:
            raise ValueError("bar_seconds must be at least 1")
        self._bar_seconds = bar_seconds
        self._current: PartialBar | None = None
        self._last_volume: float | None = None

    @property
    def bar_seconds(self) -> int:
        return self._bar_seconds

    @property
    def current(self) -> PartialBar | None:
        return self._current

    def _bucket(self, at: datetime) -> datetime:
        epoch = int(at.timestamp())
        aligned = epoch - (epoch % self._bar_seconds)
        return datetime.fromtimestamp(aligned, tz=UTC)

    def push(self, snapshot: MarketSnapshotV1) -> tuple[Bar, datetime] | None:
        """Fold a snapshot in. Returns a completed bar when one closes.

        Volume handling: feeds report ``tick_volume`` as a *running total within the
        provider's own bar*, so the per-tick contribution is the increase. A
        decrease signals the provider rolled its bar, in which case the new value is
        the contribution. Treating the raw figure as a delta would inflate volume by
        the tick count and make every volume percentile meaningless.
        """
        bucket = self._bucket(snapshot.timestamp)
        price = snapshot.mid
        raw_volume = snapshot.tick_volume

        if raw_volume is None:
            volume_delta = 1.0  # No volume data: count ticks instead.
        elif self._last_volume is None or raw_volume < self._last_volume:
            volume_delta = raw_volume
        else:
            volume_delta = raw_volume - self._last_volume
        self._last_volume = raw_volume

        completed: tuple[Bar, datetime] | None = None
        if self._current is None:
            self._current = PartialBar(
                opened_at=bucket, open=price, high=price, low=price, close=price,
                volume=max(0.0, volume_delta),
            )
            return None

        if bucket > self._current.opened_at:
            completed = (self._current.finish(), self._current.opened_at)
            self._current = PartialBar(
                opened_at=bucket, open=price, high=price, low=price, close=price,
                volume=max(0.0, volume_delta),
            )
            return completed

        # Same bucket, or a late/out-of-order tick. Out-of-order ticks are folded
        # into the current bar rather than dropped: they are real observations, and
        # discarding them would understate range during fast markets.
        self._current.update(price, volume_delta)
        return None

    def reset(self) -> None:
        self._current = None
        self._last_volume = None


class FeatureEngine:
    """Computes the §4 feature vector incrementally.

    One instance per symbol. Not thread-safe, and not intended to be: it is driven
    by a single market service task.
    """

    def __init__(self, settings: MarketSettings) -> None:
        self._settings = settings
        # Declared here for the type checker and readability; all initialisation
        # happens in reset(), so construction and re-warm-up cannot diverge.
        self._previous_close: float | None = None
        self._last_features: MarketFeaturesV1 | None = None
        self._last_bar_at: datetime | None = None
        self.reset()

    # -- introspection -----------------------------------------------------

    @property
    def bars_observed(self) -> int:
        return self._bars_observed

    @property
    def sufficient_history(self) -> bool:
        """Whether features may be trusted (§4).

        Requires both the configured warm-up and the indicators that take longest to
        seed. Reporting readiness while ADX is still blind would mean the regime
        engine classifies a trend from a default of zero.
        """
        return (
            self._bars_observed >= self._settings.warmup_bars
            and self._atr.is_ready
            and self._adx.is_ready
            and self._compression.is_ready
        )

    @property
    def last_features(self) -> MarketFeaturesV1 | None:
        return self._last_features

    @property
    def session_tracker(self) -> SessionTracker:
        return self._sessions

    @property
    def gap_count(self) -> int:
        """Bars skipped because of a data gap. Surfaced on the §47 Market Lab."""
        return self._gap_count

    # -- ingestion ---------------------------------------------------------

    def push_snapshot(self, snapshot: MarketSnapshotV1) -> MarketFeaturesV1 | None:
        """Fold in a snapshot. Returns features when a bar closes, else ``None``.

        Returning ``None`` between bars is deliberate: recomputing a 20-feature
        vector on every tick would make the feature engine the busiest thing in the
        process and would publish noise the regime engine must then smooth back out.
        """
        result = self._aggregator.push(snapshot)
        if result is None:
            return None
        bar, bar_at = result
        return self.push_bar(bar, bar_at)

    def push_bar(self, bar: Bar, bar_at: datetime) -> MarketFeaturesV1:
        """Fold in a completed bar and produce the feature vector."""
        if bar_at.tzinfo is None:
            raise ValueError("bar_at must be timezone-aware")

        if self._last_bar_at is not None:
            expected = self._last_bar_at + timedelta(seconds=self._settings.bar_seconds)
            if bar_at > expected:
                # A gap. Counted, never filled with invented bars (§86).
                missing = int(
                    (bar_at - expected).total_seconds() // self._settings.bar_seconds
                )
                self._gap_count += max(0, missing)
        self._last_bar_at = bar_at

        # Breakout is evaluated against the PRIOR range, so it must be read before
        # this bar is pushed into its window.
        atr_before = self._atr.value
        breakout_strength, breakout_sign = self._breakout.evaluate(bar, atr_before)
        self._breakout.push(bar)

        self._atr.push(bar)
        self._rsi.push(bar)
        self._adx.push(bar)
        self._compression.push(bar, self._previous_close)
        self._returns.push(bar.close)
        self._ma_window.push(bar.close)
        self._sessions.observe(bar_at, bar.high, bar.low)

        atr = self._atr.value
        if atr is not None:
            self._atr_window.push(atr)
        self._range_window.push(bar.relative_range)
        self._volume_window.push(bar.volume)

        self._bars_observed += 1
        self._previous_close = bar.close

        features = self._build(bar, bar_at, atr, breakout_strength, breakout_sign)
        self._last_features = features
        return features

    # -- assembly ----------------------------------------------------------

    def _build(
        self,
        bar: Bar,
        bar_at: datetime,
        atr: float | None,
        breakout_strength: float,
        breakout_sign: int,
    ) -> MarketFeaturesV1:
        atr_value = atr or 0.0
        # Blended ranks: see RollingWindow.percentile_rank_stable. Without this, a cold
        # start ranks its first few observations as extremes and the station opens every
        # run at the wrong energy.
        stable_samples = self._percentile_stabilisation_samples
        atr_percentile = (
            self._atr_window.percentile_rank_stable(atr, stable_samples)
            if atr is not None
            else 50.0
        )
        range_percentile = self._range_window.percentile_rank_stable(
            bar.relative_range, stable_samples
        )
        volume_percentile = self._volume_window.percentile_rank_stable(
            bar.volume, stable_samples
        )

        realized_volatility = self._returns.realised_volatility(30)
        adx = self._adx.value
        rsi = self._rsi.value

        # Slope expressed as a fraction of price per bar, which is what makes it
        # scale-free. A dollars-per-bar slope would be ten times larger if gold were
        # ten times more expensive, and the music would change for no reason.
        ma_slope = safe_divide(self._ma_window.slope_per_step(), bar.close)

        trend_strength = self._trend_strength(adx, ma_slope, realized_volatility)
        momentum = self._returns.over(5)

        # Distances in ATR units: scale-free, and directly meaningful (a two-ATR
        # distance from the high means the same thing at any price level).
        recent_high = self._breakout.recent_high
        recent_low = self._breakout.recent_low
        unit = atr_value if atr_value > 0 else max(1e-9, bar.close * 1e-4)
        distance_from_high = (
            max(0.0, (recent_high - bar.close) / unit) if recent_high is not None else 0.0
        )
        distance_from_low = (
            max(0.0, (bar.close - recent_low) / unit) if recent_low is not None else 0.0
        )

        # A breakout against the prevailing direction is still a breakout; sign is
        # carried separately by the regime engine, so strength stays unsigned here.
        signed_breakout = breakout_strength if breakout_sign != 0 else 0.0

        return MarketFeaturesV1(
            symbol=self._settings.symbol,
            timestamp=bar_at,
            sufficient_history=self.sufficient_history,
            samples_observed=self._bars_observed,
            returns_1m=self._returns.over(RETURN_HORIZONS[0]),
            returns_5m=self._returns.over(RETURN_HORIZONS[1]),
            returns_15m=self._returns.over(RETURN_HORIZONS[2]),
            atr=atr_value,
            atr_percentile=clamp(atr_percentile),
            realized_volatility=max(0.0, realized_volatility),
            range_percentile=clamp(range_percentile),
            adx=clamp(adx),
            rsi=clamp(rsi),
            moving_average_slope=ma_slope,
            trend_strength=clamp(trend_strength),
            momentum=momentum,
            volume_percentile=clamp(volume_percentile),
            session_range_position=clamp(self._sessions.range_position(bar.close), 0.0, 1.0),
            distance_from_high=max(0.0, distance_from_high),
            distance_from_low=max(0.0, distance_from_low),
            breakout_strength=clamp(signed_breakout),
            compression_score=clamp(self._compression.compression_score),
            expansion_score=clamp(self._compression.expansion_score),
        )

    def _trend_strength(
        self, adx: float, ma_slope: float, realized_volatility: float
    ) -> float:
        """Combine ADX with slope consistency into a 0–100 trend score.

        ADX alone is not enough. It rises during a volatile chop that has no net
        direction, which would make the station play trending programming through a
        directionless mess. Requiring the moving-average slope to be *material
        relative to current volatility* filters that: a slope smaller than the noise
        is not a trend, however high ADX reads.
        """
        if realized_volatility <= 0:
            slope_significance = 0.0
        else:
            # Slope per bar compared against per-bar volatility. A slope equal to
            # one standard deviation of bar returns is an unambiguous trend.
            slope_significance = min(1.0, abs(ma_slope) / realized_volatility)
        # ADX contributes two thirds, slope significance one third. ADX is the
        # established measure; slope significance is the veto.
        return clamp(adx * (0.55 + 0.45 * slope_significance))

    def reset(self) -> None:
        """Discard all accumulated state and start warming up again.

        Rebuilds the calculators rather than calling ``__init__`` on a live
        instance: re-running a constructor on ``self`` works by accident, not by
        contract, and breaks silently the moment a subclass or a cached attribute
        appears.
        """
        settings = self._settings
        window = settings.percentile_window_bars
        # How many samples a rolling distribution needs before its percentile ranks are
        # trusted outright. Capped at the window itself, and never below a floor,
        # because the production window is 720 bars (12 hours) — waiting for a full one
        # would mean half a day of cold start.
        self._percentile_stabilisation_samples = min(
            window, max(60, settings.warmup_bars * 2)
        )
        self._aggregator = BarAggregator(settings.bar_seconds)
        self._atr = AtrCalculator(period=14)
        self._rsi = RsiCalculator(period=14)
        self._adx = AdxCalculator(period=14)
        self._compression = CompressionCalculator(short_period=10, long_period=60)
        self._breakout = BreakoutCalculator(lookback=40)
        self._returns = ReturnCalculator(max_lookback=max(RETURN_HORIZONS) * 4)
        self._sessions = SessionTracker()
        self._atr_window = RollingWindow(window)
        self._range_window = RollingWindow(window)
        self._volume_window = RollingWindow(window)
        self._ma_window = RollingWindow(20)
        self._bars_observed = 0
        self._previous_close = None
        self._last_features = None
        self._last_bar_at = None
        self._gap_count = 0


__all__ = ["RETURN_HORIZONS", "BarAggregator", "FeatureEngine", "PartialBar"]
