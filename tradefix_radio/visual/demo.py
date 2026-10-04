"""VISUAL DEMO MODE — a state source for watching the engine behave.

This module exists so a human can open Chrome and *see* the behaviour system work. It is
a **state source only**: it produces :class:`VisualStateV1` frames and nothing else. The
movement a viewer sees is chosen by the real `BehaviorDirector`, framed by the real
`CameraDirector`, and drawn by the real renderer from the real frozen geometry. There are
no scripted movements here, because a scripted movement would prove nothing — the whole
point is to watch the engine decide.

**It cannot reach production.** The only thing demo mode writes to is its own dataclass.
It holds no repository, no event bus, no station socket, no generation handle; it reads
the clock and returns a value object. `test_demo_mode_cannot_reach_production` asserts
that by inspecting this module's imports, because "it doesn't touch anything" is a claim
worth enforcing rather than promising.

The timeline walks the market from quiet through extreme volatility and switches symbol
part-way, so the viewer sees band-driven pacing, music-driven rhythm, reaction salience
and a symbol change in one sitting. After the first pass it varies deterministically from
a seed: same seed, same show, but not the same six minutes on a loop.

One rule the symbol switch has to obey: **the character does not reset.** Demo mode cannot
violate it by construction — it returns state and holds no reference to the director — and
`test_the_character_survives_a_symbol_change` pins the behaviour end to end anyway.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Final

from tradefix_radio.core.clock import Clock, SystemClock
from tradefix_radio.visual.contracts import (
    FeedTrust,
    IntensityBand,
    MarketDirection,
    StationMode,
    VisualStateV1,
)
from tradefix_radio.visual.simulate import Scenario

# ============================================================ the timeline


@dataclass(frozen=True, slots=True)
class DemoPhase:
    """One segment of the demo show."""

    label: str
    seconds: float
    scenario: Scenario

    @property
    def headline(self) -> str:
        return f"{self.scenario.symbol} · {self.label}"


def _phase(
    label: str,
    seconds: float,
    *,
    band: IntensityBand,
    energy: float,
    velocity: float,
    bpm: int,
    music_energy: float,
    genre: str,
    symbol: str,
    direction: MarketDirection,
    confidence: float,
    salience: float,
) -> DemoPhase:
    return DemoPhase(
        label=label,
        seconds=seconds,
        scenario=Scenario(
            name=f"demo_{label.lower()}",
            band=band,
            market_energy=energy,
            energy_velocity=velocity,
            direction=direction,
            confidence=confidence,
            bpm=bpm,
            music_energy=music_energy,
            genre=genre,
            symbol=symbol,
            feed_trust=FeedTrust.LIVE,
            station_mode=StationMode.NORMAL,
            broadcasting=True,
            base_salience=salience,
        ),
    )


#: The show. Six minutes, then it varies.
#:
#: Salience rises with the band because a market reaction should be something the viewer
#: sees *happen* rather than something they are told about — the reaction path is the most
#: interesting thing the director does and the demo has to exercise it.
DEMO_TIMELINE: Final[tuple[DemoPhase, ...]] = (
    _phase(
        "QUIET", 60.0,
        band=IntensityBand.B1_QUIET, energy=20.0, velocity=0.4,
        bpm=80, music_energy=0.22, genre="ambient",
        symbol="XAUUSD", direction=MarketDirection.NEUTRAL, confidence=0.35,
        salience=0.05,
    ),
    _phase(
        "TREND", 60.0,
        band=IntensityBand.B3_FOCUSED, energy=50.0, velocity=2.1,
        bpm=110, music_energy=0.52, genre="downtempo",
        symbol="XAUUSD", direction=MarketDirection.BULLISH, confidence=0.64,
        salience=0.14,
    ),
    _phase(
        "BREAKOUT", 60.0,
        band=IntensityBand.B4_ALERT, energy=78.0, velocity=4.8,
        bpm=145, music_energy=0.78, genre="dnb",
        symbol="XAUUSD", direction=MarketDirection.BULLISH, confidence=0.81,
        salience=0.30,
    ),
    _phase(
        "EXTREME_VOLATILITY", 60.0,
        band=IntensityBand.B5_PEAK, energy=95.0, velocity=7.9,
        bpm=170, music_energy=0.94, genre="dnb",
        symbol="XAUUSD", direction=MarketDirection.BEARISH, confidence=0.58,
        salience=0.42,
    ),
    _phase(
        "TREND", 60.0,
        band=IntensityBand.B3_FOCUSED, energy=60.0, velocity=2.6,
        bpm=125, music_energy=0.58, genre="techno",
        symbol="BTCUSD", direction=MarketDirection.BULLISH, confidence=0.70,
        salience=0.16,
    ),
    _phase(
        "BREAKOUT", 60.0,
        band=IntensityBand.B4_ALERT, energy=85.0, velocity=5.4,
        bpm=155, music_energy=0.84, genre="techno",
        symbol="BTCUSD", direction=MarketDirection.BULLISH, confidence=0.77,
        salience=0.33,
    ),
)

CYCLE_SECONDS: Final = sum(phase.seconds for phase in DEMO_TIMELINE)

#: Bands reachable from the control panel, by the label the buttons use.
PANEL_BANDS: Final[dict[str, IntensityBand]] = {
    "quiet": IntensityBand.B1_QUIET,
    "trend": IntensityBand.B3_FOCUSED,
    "breakout": IntensityBand.B4_ALERT,
    "extreme": IntensityBand.B5_PEAK,
}

#: What the panel's band buttons imply beyond the band itself. A button that changed the
#: band and left energy and salience alone would barely alter the behaviour, and the
#: viewer would reasonably conclude the market input does nothing.
PANEL_BAND_PROFILE: Final[dict[str, tuple[float, float, int, float, float]]] = {
    # label -> (market_energy, energy_velocity, bpm, music_energy, salience)
    "quiet": (20.0, 0.4, 80, 0.22, 0.05),
    "trend": (52.0, 2.2, 112, 0.54, 0.15),
    "breakout": (80.0, 5.0, 146, 0.80, 0.31),
    "extreme": (95.0, 8.1, 170, 0.94, 0.44),
}

PANEL_SYMBOLS: Final[tuple[str, ...]] = ("XAUUSD", "BTCUSD")

#: The panel's two music buttons. Deliberately at the ends of the usable range, because
#: the rhythm policy's effect is a *subtle* nod and a mid-range change is not watchable.
PANEL_BPM: Final[dict[str, tuple[int, float]]] = {
    "low": (76, 0.20),
    "high": (172, 0.95),
}


# ============================================================ the source


@dataclass
class DemoStateSource:
    """Walks :data:`DEMO_TIMELINE`, with operator overrides from the control panel.

    Holds no handle to anything that can be mutated. `state()` is a pure function of the
    clock and this object's own fields.
    """

    clock: Clock = field(default_factory=SystemClock)
    seed: int = 7
    #: Sticky overrides from the control panel, cleared by :meth:`resume`.
    override_band: str | None = None
    override_symbol: str | None = None
    override_music: str | None = None

    _started: float = field(default=0.0, init=False)
    _rng: random.Random = field(init=False)
    _last_phase: str | None = field(default=None, init=False)
    _phase_changes: int = field(default=0, init=False)
    _symbol_changes: int = field(default=0, init=False)
    _last_symbol: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)  # noqa: S311 - a show, not a secret
        #: Stamped at construction rather than on first use. Deferring it would make the
        #: timeline's zero point "whenever something first asked for state", which is
        #: unpredictable and makes the clock impossible to reason about in a test.
        self._started = self.clock.monotonic()

    # ------------------------------------------------------------ timing

    def elapsed(self) -> float:
        return max(0.0, self.clock.monotonic() - self._started)

    def _position(self) -> tuple[int, DemoPhase, float, float]:
        """`(cycle, phase, seconds into the phase, seconds remaining)`."""
        elapsed = self.elapsed()
        cycle = int(elapsed // CYCLE_SECONDS)
        into_cycle = elapsed - cycle * CYCLE_SECONDS

        order = self._order(cycle)
        for index in order:
            phase = DEMO_TIMELINE[index]
            if into_cycle < phase.seconds:
                varied = self._vary(phase, cycle, index)
                return cycle, varied, into_cycle, phase.seconds - into_cycle
            into_cycle -= phase.seconds
        # Only reachable on a float edge at the very end of a cycle.
        last = DEMO_TIMELINE[order[-1]]
        return cycle, self._vary(last, cycle, order[-1]), last.seconds, 0.0

    def _order(self, cycle: int) -> list[int]:
        """Phase order for a cycle. The first pass is the brief's order, verbatim.

        Later passes are shuffled from a seeded generator, so the show stops being a
        six-minute loop without becoming unreproducible. The first and last phases are
        pinned: starting a cycle on EXTREME after ending the last one on BREAKOUT is a
        step change with no build, and the build is part of what there is to watch.
        """
        indices = list(range(len(DEMO_TIMELINE)))
        if cycle == 0:
            return indices
        middle = indices[1:-1]
        random.Random(self.seed * 7919 + cycle).shuffle(middle)  # noqa: S311
        return [indices[0], *middle, indices[-1]]

    def _vary(self, phase: DemoPhase, cycle: int, index: int) -> DemoPhase:
        """Jitter a phase deterministically. Same seed and cycle, same numbers."""
        if cycle == 0:
            return phase
        rng = random.Random(self.seed * 104_729 + cycle * 61 + index)  # noqa: S311
        scenario = phase.scenario
        # Every timeline phase sets these, but the contract allows them to be absent for
        # the feed-down case, so vary from a floor rather than asserting.
        energy = scenario.market_energy or 20.0
        velocity = scenario.energy_velocity or 0.5
        bpm = scenario.bpm or 100
        music = scenario.music_energy or 0.4
        return replace(
            phase,
            scenario=replace(
                scenario,
                market_energy=round(
                    min(99.0, max(5.0, energy * rng.uniform(0.86, 1.14))), 1
                ),
                energy_velocity=round(max(0.1, velocity * rng.uniform(0.7, 1.35)), 2),
                bpm=int(min(176, max(72, bpm * rng.uniform(0.92, 1.08)))),
                music_energy=round(
                    min(0.98, max(0.15, music * rng.uniform(0.88, 1.12))), 3
                ),
                base_salience=round(
                    min(0.5, max(0.02, scenario.base_salience * rng.uniform(0.75, 1.3))),
                    3,
                ),
                direction=rng.choice(
                    (MarketDirection.BULLISH, MarketDirection.BEARISH)
                    if scenario.band
                    in (IntensityBand.B4_ALERT, IntensityBand.B5_PEAK)
                    else (scenario.direction,)
                ),
            ),
        )

    # ------------------------------------------------------------ overrides

    def set_band(self, label: str) -> None:
        if label not in PANEL_BANDS:
            raise KeyError(f"unknown market condition {label!r}")
        self.override_band = label

    def set_symbol(self, symbol: str) -> None:
        if symbol not in PANEL_SYMBOLS:
            raise KeyError(f"unknown symbol {symbol!r}")
        self.override_symbol = symbol

    def set_music(self, label: str) -> None:
        if label not in PANEL_BPM:
            raise KeyError(f"unknown music setting {label!r}")
        self.override_music = label

    def resume(self) -> None:
        """Hand the show back to the timeline."""
        self.override_band = None
        self.override_symbol = None
        self.override_music = None

    @property
    def overridden(self) -> bool:
        return any(
            (self.override_band, self.override_symbol, self.override_music)
        )

    # ------------------------------------------------------------ the state

    def _effective(self) -> tuple[DemoPhase, float, float, int]:
        cycle, phase, into, remaining = self._position()
        scenario = phase.scenario
        label = phase.label

        if self.override_band is not None:
            energy, velocity, bpm, music, salience = PANEL_BAND_PROFILE[
                self.override_band
            ]
            scenario = replace(
                scenario,
                band=PANEL_BANDS[self.override_band],
                market_energy=energy,
                energy_velocity=velocity,
                bpm=bpm,
                music_energy=music,
                base_salience=salience,
            )
            label = self.override_band.upper()
        if self.override_symbol is not None:
            scenario = replace(scenario, symbol=self.override_symbol)
        if self.override_music is not None:
            bpm, music = PANEL_BPM[self.override_music]
            scenario = replace(scenario, bpm=bpm, music_energy=music)

        return replace(phase, label=label, scenario=scenario), into, remaining, cycle

    def state(self, at: datetime | None = None) -> VisualStateV1:
        """The frame the director should see. Built by the production `Scenario.state`."""
        phase, _, _, _ = self._effective()
        moment = at if at is not None else self.clock.now()

        if phase.label != self._last_phase:
            self._last_phase = phase.label
            self._phase_changes += 1
        if phase.scenario.symbol != self._last_symbol:
            if self._last_symbol is not None:
                self._symbol_changes += 1
            self._last_symbol = phase.scenario.symbol

        return phase.scenario.state(moment)

    def snapshot(self) -> dict[str, Any]:
        """What the demo HUD panel reads."""
        phase, into, remaining, cycle = self._effective()
        scenario = phase.scenario
        return {
            "cycle": cycle,
            "phase": phase.label,
            "headline": phase.headline,
            "seconds_into_phase": round(into, 1),
            "seconds_remaining": round(remaining, 1),
            "elapsed": round(self.elapsed(), 1),
            "cycle_seconds": CYCLE_SECONDS,
            "phase_changes": self._phase_changes,
            "symbol_changes": self._symbol_changes,
            "overridden": self.overridden,
            "overrides": {
                "band": self.override_band,
                "symbol": self.override_symbol,
                "music": self.override_music,
            },
            "market": {
                "symbol": scenario.symbol,
                "band": scenario.band.value,
                "energy": scenario.market_energy,
                "energy_velocity": scenario.energy_velocity,
                "direction": scenario.direction.value,
                "confidence": scenario.confidence,
                "salience": scenario.base_salience,
            },
            "music": {
                "bpm": scenario.bpm,
                "energy": scenario.music_energy,
                "genre": scenario.genre,
            },
            "timeline": [
                {
                    "label": entry.label,
                    "symbol": entry.scenario.symbol,
                    "seconds": entry.seconds,
                    "band": entry.scenario.band.value,
                    "bpm": entry.scenario.bpm,
                }
                for entry in DEMO_TIMELINE
            ],
        }


__all__ = [
    "CYCLE_SECONDS",
    "DEMO_TIMELINE",
    "PANEL_BANDS",
    "PANEL_BAND_PROFILE",
    "PANEL_BPM",
    "PANEL_SYMBOLS",
    "DemoPhase",
    "DemoStateSource",
]
