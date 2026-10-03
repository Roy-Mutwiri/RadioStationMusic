"""What this build can actually do (§51, §86).

The Control Center is being built while four of its pages describe subsystems that do not exist
yet: originality is Phase 6, ACE-Step is Phase 7, OBS is Phase 8, the watchdog is Phase 9. The
brief is explicit about the only acceptable way to handle that — *capability detection*, not
mocked data:

    "Do not fabricate metrics. When unavailable, show: Awaiting Phase 6 backend."
    "Do not mock a green connection."

So the API reports what it has, the UI renders the absence, and nothing in between invents a
number. §86's "no static fake metrics in production UI" is enforced here rather than left to
frontend discipline: a page cannot display a metric the API declines to send.

A capability is **not** a feature flag. It answers "is this subsystem present and reporting?",
which is a fact about the running process — so it is computed from what is actually wired up,
never read from configuration. A missing capability with a stated reason is honest; a
configurable one would let a deployment claim a subsystem it does not have.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Final

__all__ = [
    "Capability",
    "CapabilityReport",
    "CapabilityState",
    "detect_capabilities",
]


class Capability(str, enum.Enum):
    """Subsystems the UI asks about before rendering a page."""

    PLAYOUT = "playout"
    MARKET_FEED = "market_feed"
    GENERATION = "generation"
    SIMULATION = "simulation"
    LIBRARY = "library"
    ORIGINALITY = "originality"
    MASTERING = "mastering"
    GPU = "gpu"
    OBS = "obs"
    WATCHDOG = "watchdog"


class CapabilityState(str, enum.Enum):
    """Why a capability is or is not usable.

    ``PLANNED`` is distinct from ``UNAVAILABLE`` on purpose. "This arrives in Phase 6" and
    "this should be here and is not" are different messages to an operator, and a UI that
    cannot tell them apart will either cry wolf or hide a real fault.
    """

    READY = "ready"
    #: Implemented, present, but not currently connected or healthy.
    DEGRADED = "degraded"
    #: Not built yet. The UI says which phase delivers it.
    PLANNED = "planned"
    #: Built, but unavailable in this environment — no GPU, no OBS process, production-only.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CapabilityReport:
    """One capability's state, and a sentence a human can act on."""

    capability: Capability
    state: CapabilityState
    detail: str
    #: Phase that delivers it, for anything ``PLANNED``. ``None`` once it exists.
    arrives_in_phase: int | None = None

    @property
    def is_ready(self) -> bool:
        return self.state is CapabilityState.READY


#: Phase that delivers each subsystem not yet built, straight from the implementation plan.
_PLANNED_PHASES: Final[dict[Capability, int]] = {
    Capability.ORIGINALITY: 6,
    Capability.MASTERING: 6,
    Capability.OBS: 8,
    Capability.WATCHDOG: 9,
}


def detect_capabilities(
    *,
    has_station: bool,
    has_market_feed: bool,
    has_generation: bool,
    simulation_allowed: bool,
    gpu_present: bool,
    provider_name: str,
) -> dict[Capability, CapabilityReport]:
    """Work out what this process can do, from what is actually wired into it.

    Takes plain facts rather than the objects themselves so it stays testable and so the
    caller — which owns the runtime — decides what counts as "wired up".
    """
    reports: dict[Capability, CapabilityReport] = {}

    def add(
        capability: Capability,
        state: CapabilityState,
        detail: str,
    ) -> None:
        reports[capability] = CapabilityReport(
            capability=capability,
            state=state,
            detail=detail,
            arrives_in_phase=_PLANNED_PHASES.get(capability),
        )

    add(
        Capability.PLAYOUT,
        CapabilityState.READY if has_station else CapabilityState.UNAVAILABLE,
        "The playout engine is running."
        if has_station
        else "No station is attached to this API process.",
    )
    add(
        Capability.MARKET_FEED,
        CapabilityState.READY if has_market_feed else CapabilityState.UNAVAILABLE,
        "Market data is being polled."
        if has_market_feed
        else "No market feed is attached to this API process.",
    )
    add(
        Capability.GENERATION,
        CapabilityState.READY if has_generation else CapabilityState.UNAVAILABLE,
        f"Generating with the {provider_name!r} provider."
        if has_generation
        else "No generation manager is attached to this API process.",
    )
    add(
        Capability.LIBRARY,
        CapabilityState.READY if has_station else CapabilityState.UNAVAILABLE,
        "Track records are queryable."
        if has_station
        else "No database-backed library is attached.",
    )
    add(
        Capability.SIMULATION,
        CapabilityState.READY if simulation_allowed else CapabilityState.UNAVAILABLE,
        "Scenario controls are enabled for this run mode."
        if simulation_allowed
        else "Scenario controls are disabled outside development and simulation modes.",
    )
    add(
        Capability.GPU,
        CapabilityState.READY if gpu_present else CapabilityState.UNAVAILABLE,
        "An NVIDIA GPU is visible to this process."
        if gpu_present
        else "No NVIDIA GPU is visible. The mock provider does not need one.",
    )

    # Not built yet. Each says which phase delivers it, so the UI can be specific rather than
    # showing an empty card.
    add(
        Capability.ORIGINALITY,
        CapabilityState.PLANNED,
        "Fingerprinting, embeddings and lyric similarity arrive with Phase 6.",
    )
    add(
        Capability.MASTERING,
        CapabilityState.PLANNED,
        "Loudness normalisation and the mastering chain arrive with Phase 6.",
    )
    add(
        Capability.OBS,
        CapabilityState.PLANNED,
        "OBS websocket control arrives with Phase 8. Nothing is connected.",
    )
    add(
        Capability.WATCHDOG,
        CapabilityState.PLANNED,
        "Process supervision and chaos recovery arrive with Phase 9.",
    )
    return reports


def gpu_is_present() -> bool:
    """Whether an NVIDIA GPU is visible, without making it a hard dependency.

    Imported lazily and failures swallowed *by design*: the absence of a GPU is a normal,
    expected answer on a machine running the mock provider, not an error to report. This is
    the one place in the codebase where a bare import failure means "no" rather than "broken".
    """
    try:  # pragma: no cover - depends on the host
        import pynvml  # type: ignore[import-not-found]  # noqa: PLC0415
    except ImportError:
        return False
    try:  # pragma: no cover - depends on the host
        pynvml.nvmlInit()
    except Exception:  # noqa: BLE001 - a driver that will not initialise means "no GPU"
        return False
    else:
        try:
            return int(pynvml.nvmlDeviceGetCount()) > 0
        finally:
            with_shutdown = getattr(pynvml, "nvmlShutdown", None)
            if with_shutdown is not None:
                with_shutdown()
