"""The visual performance layer: behaviour, state bridge, geometry.

This package decides **what the character does**. It does not draw anything, and it
does not know how anything is drawn — the WebGL2 renderer inside an OBS browser source
receives commands and performs them (ADR-10, ADR-11).

Three rules hold across every module here, and they are the reason the package exists
as its own process rather than as part of the station:

1. **Read-only toward the radio.** Nothing in `tradefix_radio.visual` opens a database
   session, publishes an event, or mutates station state. The bridge is a consumer of a
   WebSocket the station already serves. `tests/unit/test_visual_isolation.py` asserts
   this structurally rather than trusting it.

2. **No market vocabulary below the bridge.** `MarketRegime` is read in exactly one
   place — :mod:`tradefix_radio.visual.bridge` — and collapsed into an
   :class:`~tradefix_radio.visual.contracts.IntensityBand`. No action in the catalogue
   names a regime, so a new regime is a one-line change in the bridge and touches no
   behaviour data.

3. **Deterministic under an injected clock and RNG.** Every interval is sampled, never
   constant, and every sample comes from an injected `random.Random`. That is what makes
   a 24-hour behaviour soak reproducible and fast (`VirtualClock.advance_sync`), which is
   the whole argument for the director living in Python.
"""

from __future__ import annotations

from tradefix_radio.visual.contracts import (
    ActionCategory,
    ActionSpecV1,
    BandBiasV1,
    CharacterActionV1,
    CharacterState,
    GazeShiftV1,
    GazeTarget,
    IntensityBand,
    InteractionLock,
    Interruptibility,
    StationMode,
    TransitionState,
    VisualStateV1,
)

__all__ = [
    "ActionCategory",
    "ActionSpecV1",
    "BandBiasV1",
    "CharacterActionV1",
    "CharacterState",
    "GazeShiftV1",
    "GazeTarget",
    "IntensityBand",
    "InteractionLock",
    "Interruptibility",
    "StationMode",
    "TransitionState",
    "VisualStateV1",
]
