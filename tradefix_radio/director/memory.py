"""Director creative memory across restarts (§96, milestone 3.11).

§96: "Restarting the application should NOT reset creative memory and cause immediate
repeats."

Most of the director's memory needs no help here. History, blueprint signatures and used
titles live in the ``tracks`` table, so a restart reads them back as a matter of course;
that is why the director takes them as parameters rather than holding them.

What a restart *does* lose is the state the director keeps between decisions and nowhere
else:

``station energy``        §98's planner anchors each track to the previous one. Lose it and
                          the first track after a restart jumps straight to market energy,
                          which is audibly a different station mid-sequence.
``consecutive peaks``     the counter behind "do not sit at an extreme". Lose it and a
                          restart during a violent session resets the peak budget, so the
                          station can run six peak tracks where §98 allows three.

Both are small, and that is the point: the §96 failure mode is not a dramatic one. It is a
station that sounds slightly wrong for ten minutes after every restart, which over a week
of supervised restarts is most of what a listener hears.

**Saved after every decision, not at shutdown.** A watchdog kill (§57) or a power cut never
reaches a shutdown hook, and those are precisely the restarts §96 is about. The write is one
upsert of two small values, which is cheap next to generating a track.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import structlog

from tradefix_radio.director.energy_curve import RadioEnergyPlanner
from tradefix_radio.persistence.repositories.memory import (
    MemoryKeys,
    RadioMemoryRepository,
)

_log = structlog.get_logger(__name__)

#: Key for the peak counter. The energy level itself uses the existing
#: :data:`MemoryKeys.RADIO_ENERGY`.
CONSECUTIVE_PEAKS_KEY: Final = "director.consecutive_peaks"


@dataclass(frozen=True)
class DirectorState:
    """The director's between-decision state, in a form that survives a restart."""

    station_energy: float | None
    consecutive_peaks: int

    def as_memory(self) -> dict[str, Any]:
        """Keys to write. A ``None`` station energy is **omitted**, not stored as null.

        ``radio_memory.value`` is a non-nullable JSON column, so writing ``None`` raises an
        integrity error — which it did, on every save before the first track existed. Omitting
        the key is semantically identical, because :func:`load_director_state` already treats a
        missing key as "no opinion yet", and it avoids a migration for a value that is only
        absent during the first few seconds of a cold start.
        """
        values: dict[str, Any] = {CONSECUTIVE_PEAKS_KEY: self.consecutive_peaks}
        if self.station_energy is not None:
            values[MemoryKeys.RADIO_ENERGY] = self.station_energy
        return values


def capture_state(planner: RadioEnergyPlanner) -> DirectorState:
    return DirectorState(
        station_energy=planner.current_energy,
        consecutive_peaks=planner.consecutive_peaks,
    )


async def save_director_state(
    memory: RadioMemoryRepository, planner: RadioEnergyPlanner, *, now: datetime
) -> DirectorState:
    """Persist the planner's state. Call after every decision."""
    state = capture_state(planner)
    await memory.set_many(state.as_memory(), now=now)
    return state


async def load_director_state(memory: RadioMemoryRepository) -> DirectorState:
    """Read the planner's state back, tolerating a missing or corrupt row.

    A missing row is the normal first-ever start. A *corrupt* row — wrong type, or an
    energy outside 0–100 because an older build wrote a different scale — is treated the
    same way: start fresh, and log it. Refusing to start because a creative hint could not
    be parsed would turn a cosmetic problem into an outage, which §86's "one model crash
    must not stop the radio" rules out for exactly the same reason.
    """
    stored = await memory.get_many([MemoryKeys.RADIO_ENERGY, CONSECUTIVE_PEAKS_KEY])

    energy = stored.get(MemoryKeys.RADIO_ENERGY)
    if isinstance(energy, bool) or not isinstance(energy, (int, float)):
        # bool is an int subclass, and True would silently become energy 1.0.
        if energy is not None:
            _log.warning(
                "director.memory_discarded",
                key=MemoryKeys.RADIO_ENERGY,
                value=repr(energy),
                detail="stored station energy is not a number; starting fresh",
            )
        energy = None
    elif not 0.0 <= float(energy) <= 100.0:
        _log.warning(
            "director.memory_discarded",
            key=MemoryKeys.RADIO_ENERGY,
            value=repr(energy),
            detail="stored station energy is outside 0-100; starting fresh",
        )
        energy = None

    peaks = stored.get(CONSECUTIVE_PEAKS_KEY)
    if isinstance(peaks, bool) or not isinstance(peaks, int) or peaks < 0:
        if peaks is not None:
            _log.warning(
                "director.memory_discarded",
                key=CONSECUTIVE_PEAKS_KEY,
                value=repr(peaks),
                detail="stored peak count is not a non-negative integer; starting fresh",
            )
        peaks = 0

    return DirectorState(
        station_energy=None if energy is None else float(energy),
        consecutive_peaks=peaks,
    )


async def restore_director_state(
    memory: RadioMemoryRepository, planner: RadioEnergyPlanner
) -> DirectorState:
    """Load and apply the planner's state in one step — what startup calls."""
    state = await load_director_state(memory)
    planner.restore(state.station_energy, state.consecutive_peaks)
    _log.info(
        "director.memory_restored",
        station_energy=state.station_energy,
        consecutive_peaks=state.consecutive_peaks,
    )
    return state


__all__ = [
    "CONSECUTIVE_PEAKS_KEY",
    "DirectorState",
    "capture_state",
    "load_director_state",
    "restore_director_state",
    "save_director_state",
]
