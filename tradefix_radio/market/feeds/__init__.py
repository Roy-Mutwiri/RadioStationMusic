"""Market data sources (ADR-03).

Four implementations behind one pull-based interface:

| Feed | Mode | Credentials |
| --- | --- | --- |
| :class:`SimulatedFeed` | development, simulation | none — fully offline |
| :class:`MetaTrader5Feed` | production default | a logged-in MT5 terminal |
| :class:`RestPollingFeed` | production alternative | provider API key |
| :class:`ReplayFeed` | simulation, debugging | none |

``MetaTrader5Feed`` and ``RestPollingFeed`` are imported lazily by
:func:`build_feed` so that a machine without the optional extras — or without
Windows — can still import this package and run the whole development stack.
"""

from __future__ import annotations

from pathlib import Path

from tradefix_radio.config.schema import AppSettings
from tradefix_radio.core.clock import Clock
from tradefix_radio.core.errors import ConfigurationError
from tradefix_radio.market.feeds.base import FeedBase, MarketFeed
from tradefix_radio.market.feeds.replay import ReplayFeed
from tradefix_radio.market.feeds.simulated import SimulatedFeed
from tradefix_radio.market.simulation import Scenario


def build_feed(
    settings: AppSettings,
    *,
    clock: Clock | None = None,
    scenario: Scenario = Scenario.RANDOM_WALK,
    seed: int = 0,
) -> MarketFeed:
    """Construct the feed named by ``settings.market.feed``.

    The configuration schema has already rejected impossible combinations (a `rest`
    feed with no base URL, a `replay` feed with no file), so this is a dispatch rather
    than a validation.
    """
    kind = settings.market.feed
    if kind == "simulated":
        return SimulatedFeed(
            symbol=settings.market.symbol,
            scenario=scenario,
            seed=seed,
            clock=clock,
        )
    if kind == "replay":
        if settings.market.replay_file is None:
            raise ConfigurationError("market.replay_file is required for the replay feed")
        return ReplayFeed(Path(settings.market.replay_file), symbol=settings.market.symbol)
    if kind == "metatrader5":
        # Deferred: the MetaTrader5 package is a Windows-only optional extra. A
        # module-scope import would make this package unimportable without it, and
        # with it the whole development stack.
        from tradefix_radio.market.feeds.metatrader5 import (  # noqa: PLC0415
            MetaTrader5Feed,
        )

        return MetaTrader5Feed(
            symbol=settings.market.symbol,
            symbol_aliases=settings.market.symbol_aliases,
        )
    if kind == "rest":
        # Deferred for symmetry and to keep httpx off the import path of a station
        # that does not use an HTTP feed.
        from tradefix_radio.market.feeds.rest import RestPollingFeed  # noqa: PLC0415

        return RestPollingFeed(settings.market, clock=clock)
    raise ConfigurationError(f"unknown market.feed {kind!r}")


__all__ = [
    "FeedBase",
    "MarketFeed",
    "ReplayFeed",
    "SimulatedFeed",
    "build_feed",
]
