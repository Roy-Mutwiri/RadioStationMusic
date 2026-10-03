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
    symbol: str | None = None,
) -> MarketFeed:
    """Construct the feed named by ``settings.market.feed``, for one symbol.

    The configuration schema has already rejected impossible combinations (a `rest`
    feed with no base URL, a `replay` feed with no file), so this is a dispatch rather
    than a validation.

    ``symbol`` defaults to ``market.symbol``, which is what a single-market deployment
    wants. Market routing passes it explicitly, once per configured symbol — the station
    runs one feed per market, and this is the one place that knows how to build one.
    """
    kind = settings.market.feed
    resolved = symbol or settings.market.symbol
    if kind == "simulated":
        return SimulatedFeed(
            symbol=resolved,
            scenario=scenario,
            seed=seed,
            clock=clock,
        )
    if kind == "replay":
        if settings.market.replay_file is None:
            raise ConfigurationError("market.replay_file is required for the replay feed")
        return ReplayFeed(Path(settings.market.replay_file), symbol=resolved)
    if kind == "metatrader5":
        # Deferred: the MetaTrader5 package is a Windows-only optional extra. A
        # module-scope import would make this package unimportable without it, and
        # with it the whole development stack.
        from tradefix_radio.market.feeds.metatrader5 import (  # noqa: PLC0415
            MetaTrader5Feed,
        )

        return MetaTrader5Feed(
            symbol=resolved,
            symbol_aliases=_aliases_for(settings, resolved),
        )
    if kind == "rest":
        # Deferred for symmetry and to keep httpx off the import path of a station
        # that does not use an HTTP feed.
        from tradefix_radio.market.feeds.rest import RestPollingFeed  # noqa: PLC0415

        # The REST feed reads its symbol from `market`, so a routed deployment needs the
        # section pointed at the symbol being built rather than at the configured default.
        return RestPollingFeed(
            settings.market.model_copy(update={"symbol": resolved}), clock=clock
        )
    raise ConfigurationError(f"unknown market.feed {kind!r}")


def _aliases_for(settings: AppSettings, symbol: str) -> tuple[str, ...]:
    """Broker spellings to try for ``symbol``.

    Three sources, in order of specificity. The last case is the one worth stating: a
    routed symbol with nothing configured gets *only its own name*, never the configured
    default's aliases — offering a broker "GOLD.spot" while asking for Bitcoin would at
    best fail and at worst attach the Bitcoin feed to the gold instrument.
    """
    configured = settings.markets.for_symbol(symbol).aliases
    if configured:
        return configured
    if symbol == settings.market.symbol:
        return settings.market.symbol_aliases
    return (symbol,)


__all__ = [
    "FeedBase",
    "MarketFeed",
    "ReplayFeed",
    "SimulatedFeed",
    "build_feed",
]
