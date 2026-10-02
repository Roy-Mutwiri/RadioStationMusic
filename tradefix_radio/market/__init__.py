"""Market engine: feeds, features, energy, regimes, sessions (§4–§7).

The layer that turns price into an *interpretation* the music director can act on.

Two properties hold throughout and are enforced by tests rather than convention:

* **Scale invariance** (§6). Every quantity that reaches the music director is a
  percentile, a ratio, or a fractional return. Multiplying every price by ten
  produces identical energy, regime and programming.
* **Honest uncertainty.** During warm-up, during a gap, and when the feed dies, the
  engine says so rather than guessing. A non-live feed cannot carry a price at all.

This package has **no third-party numeric dependency**. The feature engine runs once
per bar, so NumPy would buy nothing measurable, and staying dependency-free means it
imports instantly and cannot be broken by a wheel problem.
"""

from tradefix_radio.market.energy import EnergyCalculator, EnergyContribution, energy_band
from tradefix_radio.market.features import BarAggregator, FeatureEngine
from tradefix_radio.market.feeds import MarketFeed, build_feed
from tradefix_radio.market.indicators import Bar
from tradefix_radio.market.regimes import (
    Direction,
    RegimeDecision,
    RegimeEngine,
    RegimeScorer,
    RegimeScores,
    RegimeStabiliser,
)
from tradefix_radio.market.rolling import ExponentialSmoother, RollingWindow
from tradefix_radio.market.service import MarketDataService
from tradefix_radio.market.sessions import (
    SessionTracker,
    classify_session,
    is_market_open,
)
from tradefix_radio.market.simulation import (
    UI_SCENARIOS,
    MarketSimulationEngine,
    Scenario,
)

__all__ = [
    "UI_SCENARIOS",
    "Bar",
    "BarAggregator",
    "Direction",
    "EnergyCalculator",
    "EnergyContribution",
    "ExponentialSmoother",
    "FeatureEngine",
    "MarketDataService",
    "MarketFeed",
    "MarketSimulationEngine",
    "RegimeDecision",
    "RegimeEngine",
    "RegimeScorer",
    "RegimeScores",
    "RegimeStabiliser",
    "RollingWindow",
    "Scenario",
    "SessionTracker",
    "build_feed",
    "classify_session",
    "energy_band",
    "is_market_open",
]
