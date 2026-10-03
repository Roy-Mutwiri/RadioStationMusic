"""``tradefix market-sim`` — run a §7 scenario and print the market engine's reading.

Phase 2's independent verification path. The §47 Market Lab (Phase 5) will show the
same information graphically, but the market engine must be inspectable *before* an
API and a frontend exist — otherwise the only way to understand a misclassification
would be to read the code.

Driven by a :class:`~tradefix_radio.core.clock.VirtualClock`, so a simulated day runs
in a couple of seconds and the output is byte-identical for a given seed.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from tradefix_radio.config.schema import AppSettings
from tradefix_radio.contracts.enums import MarketRegime
from tradefix_radio.core.clock import UTC, VirtualClock
from tradefix_radio.market.energy import EnergyCalculator
from tradefix_radio.market.features import FeatureEngine
from tradefix_radio.market.regimes import RegimeEngine
from tradefix_radio.market.simulation import MarketSimulationEngine, Scenario

#: Simulation start. Fixed and in the London/New York overlap, so session-dependent
#: behaviour is exercised and output is reproducible.
SIMULATION_START = datetime(2026, 10, 2, 13, 0, 0, tzinfo=UTC)


@dataclass
class BarReading:
    """One bar's worth of the engine's interpretation."""

    bar: int
    at: datetime
    price: float
    regime: MarketRegime
    confidence: float
    energy: float
    energy_velocity: float
    volatility: float
    trend: float
    compression: float
    expansion: float
    breakout: float
    direction: str
    changed: bool
    suppression: str


#: Plausible starting levels, by symbol. See the note where this is used.
START_PRICES: dict[str, float] = {"XAUUSD": 4_000.0, "BTCUSD": 103_000.0}


def run_simulation(
    settings: AppSettings,
    scenario: Scenario,
    *,
    bars: int,
    seed: int,
    symbol: str | None = None,
    switch_to: Scenario | None = None,
    switch_at: int | None = None,
) -> list[BarReading]:
    """Drive the real engines with a simulated scenario.

    Uses the production :class:`FeatureEngine`, :class:`EnergyCalculator` and
    :class:`RegimeEngine` — not a simplified copy. §7 is explicit that changing the
    simulation must drive the real music-director system, and a reporting tool that
    reimplemented the maths would be able to disagree with the station.
    """
    clock = VirtualClock(start=SIMULATION_START)
    ticks_per_bar = 20
    resolved = (symbol or settings.markets.primary).upper()
    simulator = MarketSimulationEngine(
        symbol=resolved,
        # Only the Market page ever shows the level, and §6 forbids musical decisions
        # depending on it — but a four-thousand-dollar Bitcoin in this table reads as a
        # broken tool rather than as a simulation.
        start_price=START_PRICES.get(resolved, 4_000.0),
        seed=seed,
        ticks_per_bar=ticks_per_bar,
    )
    simulator.set_scenario(scenario)

    features_engine = FeatureEngine(settings.market)
    energy_engine = EnergyCalculator(settings.energy)
    regime_engine = RegimeEngine(settings.regime)

    tick_interval = timedelta(seconds=settings.market.bar_seconds / ticks_per_bar)
    readings: list[BarReading] = []
    bar_index = 0

    for _ in range(bars * ticks_per_bar):
        moment = clock.now()
        snapshot = simulator.next_tick(moment)
        vector = features_engine.push_snapshot(snapshot)
        if vector is not None:
            bar_index += 1
            energy = energy_engine.compute(vector)
            decision = regime_engine.classify(vector, now=moment)
            readings.append(
                BarReading(
                    bar=bar_index,
                    at=moment,
                    price=snapshot.mid,
                    regime=decision.regime,
                    confidence=decision.confidence,
                    energy=energy.smoothed_energy,
                    energy_velocity=energy.energy_velocity,
                    volatility=vector.atr_percentile,
                    trend=vector.trend_strength,
                    compression=vector.compression_score,
                    expansion=vector.expansion_score,
                    breakout=vector.breakout_strength,
                    direction=decision.direction.value,
                    changed=decision.changed,
                    suppression=decision.suppression_reason,
                )
            )
            if switch_to is not None and switch_at is not None and bar_index == switch_at:
                simulator.set_scenario(switch_to)
        # Nothing awaits this clock — the whole simulation is synchronous — so the
        # synchronous advance is correct and will raise if that assumption breaks.
        clock.advance_sync(tick_interval.total_seconds())

    return readings


def render_table(readings: list[BarReading], *, every: int) -> str:
    """A fixed-width timeline, plus every regime change regardless of sampling.

    Sampling keeps a 1 440-bar day readable, but a regime change must never be sampled
    away — it is the single most interesting event in the output.
    """
    lines = [
        f"{'bar':>5} {'time':>8} {'price':>10} {'regime':<26} {'conf':>5} "
        f"{'enr':>5} {'dE':>6} {'vol':>5} {'trd':>5} {'cmp':>5} {'exp':>5} {'brk':>5} dir",
        "-" * 118,
    ]
    for reading in readings:
        if not reading.changed and reading.bar % every != 0:
            continue
        marker = "*" if reading.changed else " "
        lines.append(
            f"{reading.bar:>5}{marker}{reading.at:%H:%M:%S}"
            f" {reading.price:>10.2f} {reading.regime.value:<26}"
            f" {reading.confidence:>5.2f} {reading.energy:>5.1f}"
            f" {reading.energy_velocity:>+6.2f} {reading.volatility:>5.1f}"
            f" {reading.trend:>5.1f} {reading.compression:>5.1f}"
            f" {reading.expansion:>5.1f} {reading.breakout:>5.1f} {reading.direction}"
        )
    return "\n".join(lines)


def render_summary(readings: list[BarReading]) -> str:
    """Aggregate statistics — what an operator tuning the engine actually reads."""
    if not readings:
        return "  no bars produced"
    classified = [r for r in readings if r.regime is not MarketRegime.UNKNOWN]
    transitions = sum(1 for r in readings if r.changed)
    occupancy: dict[str, int] = {}
    for reading in readings:
        occupancy[reading.regime.value] = occupancy.get(reading.regime.value, 0) + 1

    energies = [r.energy for r in classified] or [0.0]
    lines = [
        f"  bars                {len(readings)}",
        f"  classified          {len(classified)}"
        f" ({100.0 * len(classified) / len(readings):.0f}%)",
        f"  regime transitions  {transitions}",
        f"  bars per transition {len(readings) / transitions:.1f}"
        if transitions
        else "  bars per transition n/a (no transitions)",
        f"  energy min/mean/max {min(energies):.1f} / "
        f"{sum(energies) / len(energies):.1f} / {max(energies):.1f}",
        "",
        "  regime occupancy",
    ]
    for regime, count in sorted(occupancy.items(), key=lambda item: -item[1]):
        share = 100.0 * count / len(readings)
        bar = "#" * int(share / 2)
        lines.append(f"    {regime:<28} {count:>5} {share:>5.1f}%  {bar}")
    return "\n".join(lines)


async def command(args: argparse.Namespace, settings: AppSettings) -> int:
    """Entry point wired into the CLI parser."""
    try:
        scenario = Scenario(args.scenario)
    except ValueError:
        available = ", ".join(s.value for s in Scenario)
        print(f"  unknown scenario {args.scenario!r}; available: {available}")
        return 1

    switch_to: Scenario | None = None
    if args.switch_to:
        try:
            switch_to = Scenario(args.switch_to)
        except ValueError:
            available = ", ".join(s.value for s in Scenario)
            print(f"  unknown scenario {args.switch_to!r}; available: {available}")
            return 1

    readings = run_simulation(
        settings,
        scenario,
        bars=args.bars,
        seed=args.seed,
        symbol=args.symbol,
        switch_to=switch_to,
        switch_at=args.switch_at,
    )

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "bar": r.bar,
                        "at": r.at.isoformat(),
                        "price": round(r.price, 4),
                        "regime": r.regime.value,
                        "confidence": round(r.confidence, 4),
                        "energy": round(r.energy, 3),
                        "energy_velocity": round(r.energy_velocity, 4),
                        "volatility": round(r.volatility, 2),
                        "trend_strength": round(r.trend, 2),
                        "compression": round(r.compression, 2),
                        "expansion": round(r.expansion, 2),
                        "breakout": round(r.breakout, 2),
                        "direction": r.direction,
                        "changed": r.changed,
                    }
                    for r in readings
                ],
                indent=2,
            )
        )
        return 0

    print()
    header = f"  MARKET SIMULATION - {scenario.value}"
    if switch_to is not None:
        header += f" -> {switch_to.value} at bar {args.switch_at}"
    print(header)
    symbol = (args.symbol or settings.markets.primary).upper()
    print(f"  seed={args.seed} bars={args.bars} symbol={symbol}")
    print()
    print(render_table(readings, every=args.every))
    print()
    print(render_summary(readings))
    print()
    print("  * marks an adopted regime change")
    print()
    return 0


def register(subparsers: Any) -> None:
    """Add the ``market-sim`` subcommand.

    ``subparsers`` is argparse's private ``_SubParsersAction``; typed as ``Any`` rather
    than naming a private generic, which mypy and argparse disagree about across
    versions.
    """
    parser = subparsers.add_parser(
        "market-sim", help="run a market scenario and print the engine's reading"
    )
    parser.add_argument(
        "--scenario",
        default=Scenario.BREAKOUT_UP.value,
        choices=[item.value for item in Scenario],
        help="scenario to run",
    )
    parser.add_argument(
        "--symbol",
        default=None,
        help=(
            "symbol to simulate; defaults to markets.primary. Use the fallback (BTCUSD) to "
            "check how the engines read the market the station runs on at weekends."
        ),
    )
    parser.add_argument("--bars", type=int, default=240, help="bars to simulate")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed (reproducible)")
    parser.add_argument(
        "--every", type=int, default=10, help="print every Nth bar (changes always print)"
    )
    parser.add_argument(
        "--switch-to",
        default=None,
        choices=[item.value for item in Scenario],
        help="switch to this scenario mid-run",
    )
    parser.add_argument(
        "--switch-at", type=int, default=None, help="bar at which to switch"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")


__all__ = ["SIMULATION_START", "BarReading", "command", "register", "run_simulation"]
