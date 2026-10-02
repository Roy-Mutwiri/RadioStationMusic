"""Simulated feed (§7, ADR-03).

Wraps :class:`~tradefix_radio.market.simulation.MarketSimulationEngine` behind the
feed interface, so the entire station runs end to end with no internet, no broker
and no credentials — §7's central requirement.

The scenario is switchable at runtime, which is what makes §7's simulation buttons
drive the *real* engines rather than painting fake values into the UI.
"""

from __future__ import annotations

from tradefix_radio.contracts.market import MarketSnapshotV1
from tradefix_radio.core.clock import Clock, SystemClock
from tradefix_radio.market.feeds.base import FeedBase
from tradefix_radio.market.simulation import MarketSimulationEngine, Scenario


class SimulatedFeed(FeedBase):
    """Generates synthetic snapshots from a selectable §7 scenario."""

    def __init__(
        self,
        *,
        symbol: str = "XAUUSD",
        # Random walk by default: the least opinionated starting condition, so a
        # developer who has not chosen a scenario sees ordinary market behaviour
        # rather than a scenario's narrative.
        scenario: Scenario = Scenario.RANDOM_WALK,
        seed: int = 0,
        start_price: float = 4_000.0,
        ticks_per_bar: int = 60,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(symbol)
        self._clock: Clock = clock or SystemClock()
        self._engine = MarketSimulationEngine(
            symbol=symbol,
            start_price=start_price,
            seed=seed,
            ticks_per_bar=ticks_per_bar,
        )
        self._engine.set_scenario(scenario)

    @property
    def name(self) -> str:
        return f"simulated:{self._engine.scenario.value}"

    @property
    def is_simulated(self) -> bool:
        return True

    @property
    def engine(self) -> MarketSimulationEngine:
        """The underlying engine, for the §47 Market Lab's phase display."""
        return self._engine

    @property
    def scenario(self) -> Scenario:
        return self._engine.scenario

    def set_scenario(self, scenario: Scenario) -> None:
        """Switch scenario mid-run (§7 simulation buttons).

        Price continues from where it is rather than jumping, so the feature engine
        does not read an artificial gap as a flash crash.
        """
        self._engine.set_scenario(scenario)

    async def open(self) -> None:
        self._is_open = True

    async def close(self) -> None:
        self._is_open = False

    async def poll(self) -> MarketSnapshotV1 | None:
        """Produce one tick.

        Always returns a snapshot: a simulated feed has no reason to be silent, and
        the service's polling interval is what controls the rate.
        """
        if not self._is_open:
            return None
        return self._engine.next_tick(self._clock.now())


__all__ = ["SimulatedFeed"]
