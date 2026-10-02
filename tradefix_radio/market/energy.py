"""Market energy score (§6).

Produces a normalised 0–100 figure plus its smoothed value and velocity. This is
the single most consequential number in the station: it drives BPM, intensity,
vocal probability and the energy curve, so it has to be stable enough not to make
the music twitch and responsive enough to react to a real breakout.

Design decisions
----------------

**Every input is already normalised** before it is weighted. Mixing a percentile
(0–100) with a raw return (0.0003) would let whichever happened to be larger
dominate the sum regardless of its configured weight. Each contributor is therefore
mapped to 0–100 first, by a documented transform.

**Weights are configuration, normalised at use.** §6 says "use configurable
weights"; they need not sum to 1, because they are divided by their total. An
operator can set one weight to zero to remove a contributor entirely.

**``energy_velocity`` is the point of the exercise.** §6 notes music can *prepare*
for rising intensity. Velocity is the least-squares slope of recent smoothed energy
in points per minute, so the scheduler can raise the energy of the *next* track
before the market finishes moving.

**Scale invariance** is inherited: every contributor is a percentile, a ratio, or a
return, so multiplying all prices by ten leaves energy unchanged. That property is
asserted directly in the test suite.
"""

from __future__ import annotations

from dataclasses import dataclass

from tradefix_radio.config.schema import EnergySettings
from tradefix_radio.contracts.market import MarketEnergyV1, MarketFeaturesV1
from tradefix_radio.market.rolling import ExponentialSmoother, RollingWindow, clamp

#: Per-bar return magnitude, as a fraction, that maps to full price-velocity score.
#: 0.25 % in one bar is a decisive move for gold on a one-minute chart.
VELOCITY_FULL_SCALE = 0.0025

#: Per-bar realised volatility that maps to a full volatility score.
VOLATILITY_FULL_SCALE = 0.0015

#: Momentum magnitude (5-bar fractional return) mapping to a full momentum score.
MOMENTUM_FULL_SCALE = 0.006


@dataclass(frozen=True)
class EnergyContribution:
    """One weighted contributor, retained for the §47 Market Lab breakdown."""

    name: str
    normalised: float
    weight: float

    @property
    def points(self) -> float:
        return self.normalised * self.weight


def _scale(magnitude: float, full_scale: float) -> float:
    """Map an unsigned magnitude to 0–100, saturating at ``full_scale``.

    Linear with a hard ceiling rather than logarithmic: the ceiling is what stops a
    flash crash pinning energy at 100 for an hour afterwards, and linearity below it
    keeps the mapping legible to an operator tuning weights on the §47 page.
    """
    if full_scale <= 0:
        return 0.0
    return clamp(100.0 * abs(magnitude) / full_scale)


class EnergyCalculator:
    """Computes the §6 energy triple from a feature vector."""

    def __init__(self, settings: EnergySettings) -> None:
        self._settings = settings
        self._smoother = ExponentialSmoother(settings.smoothing_alpha)
        self._velocity_window = RollingWindow(settings.velocity_window_bars)
        self._last: MarketEnergyV1 | None = None

    @property
    def last(self) -> MarketEnergyV1 | None:
        return self._last

    def reset(self) -> None:
        self._smoother.reset()
        self._velocity_window.clear()
        self._last = None

    def contributions(self, features: MarketFeaturesV1) -> list[EnergyContribution]:
        """Normalise every §6 input to 0–100 and attach its configured weight.

        Exposed separately from :meth:`compute` so the §47 Market Lab can show *why*
        energy is what it is. Tuning weights blind is how a station ends up playing
        drum and bass through a dead Asian session.
        """
        weights = self._settings.weights.normalised()
        values = {
            # Already a percentile.
            "atr_percentile": features.atr_percentile,
            "realized_volatility": _scale(
                features.realized_volatility, VOLATILITY_FULL_SCALE
            ),
            # Short-horizon return magnitude: how fast price is moving right now.
            "price_velocity": _scale(features.returns_1m, VELOCITY_FULL_SCALE),
            "trend_strength": features.trend_strength,
            "volume_percentile": features.volume_percentile,
            "breakout_strength": features.breakout_strength,
            # Range expansion already arrives as a 0-100 score.
            "range_expansion": features.expansion_score,
            "momentum": _scale(features.momentum, MOMENTUM_FULL_SCALE),
        }
        return [
            EnergyContribution(name=name, normalised=clamp(value), weight=weights[name])
            for name, value in values.items()
        ]

    def compute(self, features: MarketFeaturesV1) -> MarketEnergyV1:
        """Produce raw, smoothed and velocity values.

        During warm-up the raw score is still computed, but it is **not** smoothed
        into the running state: seeding the smoother from unreliable features would
        bias the first several minutes of programming. Instead a neutral 50 is
        reported, which is also what the regime engine treats as "no opinion".
        """
        contributions = self.contributions(features)
        raw = clamp(sum(contribution.points for contribution in contributions))

        if not features.sufficient_history:
            neutral = MarketEnergyV1(
                raw_energy=raw,
                smoothed_energy=50.0,
                energy_velocity=0.0,
                components={c.name: round(c.points, 4) for c in contributions},
            )
            self._last = neutral
            return neutral

        smoothed = clamp(self._smoother.push(raw))
        self._velocity_window.push(smoothed)
        velocity = self._velocity()

        result = MarketEnergyV1(
            raw_energy=raw,
            smoothed_energy=smoothed,
            energy_velocity=velocity,
            components={c.name: round(c.points, 4) for c in contributions},
        )
        self._last = result
        return result

    def _velocity(self) -> float:
        """Signed rate of change of smoothed energy, in points per bar.

        A least-squares slope over the window rather than a simple difference,
        because a single bar's jitter in a smoothed series should not read as a
        trend in intensity. Returns 0 until the window has at least three points, so
        the station does not start pre-empting a move it has not seen.
        """
        if len(self._velocity_window) < 3:
            return 0.0
        return self._velocity_window.slope_per_step()


def energy_band(energy: float, bands: tuple[int, ...] = (25, 45, 65, 82)) -> int:
    """Index of the band ``energy`` falls into, 0-based.

    Shared by the music director and the §43 panel so a track's band and the band
    shown to the operator cannot disagree.
    """
    index = 0
    for bound in bands:
        if energy >= bound:
            index += 1
    return index


__all__ = [
    "MOMENTUM_FULL_SCALE",
    "VELOCITY_FULL_SCALE",
    "VOLATILITY_FULL_SCALE",
    "EnergyCalculator",
    "EnergyContribution",
    "energy_band",
]
