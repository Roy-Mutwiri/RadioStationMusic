"""Model lifecycle and generation profiles (§7.6, §7.8).

Two small, explicit pieces of state that are easy to leave implicit and expensive to get
wrong.

**The lifecycle** exists because loading the model is slow and unloading it is deliberate.
§7.6: *"Do not reload the entire model for every track."* Without a named state the provider
would have only "did the last call work", which cannot distinguish a model that is loading
from one that failed to load — and the station would retry into a cold start it should have
waited out.

**The profiles** exist because §7.20 lets the scheduler trade quality for speed when the
buffer is under pressure. They are configuration, not constants, so the trade can be tuned
against a real GPU without a code change — and crucially they control *generation* settings
only. §7.20: *"Do not reduce audio safety thresholds during buffer pressure. Operational
urgency may change generation parameters, not QC integrity."* Nothing here can reach QC.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Final

__all__ = [
    "BUILTIN_PROFILES",
    "GenerationProfile",
    "IllegalModelTransition",
    "ModelState",
    "profile_for_buffer",
]


class ModelState(str, enum.Enum):
    """§7.6's explicit states."""

    UNAVAILABLE = "unavailable"
    LOADING = "loading"
    READY = "ready"
    GENERATING = "generating"
    UNLOADING = "unloading"
    FAILED = "failed"

    @property
    def can_generate(self) -> bool:
        return self is ModelState.READY

    @property
    def is_busy(self) -> bool:
        """Transitional states. A caller should wait rather than act."""
        return self in (ModelState.LOADING, ModelState.GENERATING, ModelState.UNLOADING)


#: Legal transitions. Anything absent raises.
#:
#: FAILED is reachable from every working state because any of them can hit a dead service,
#: and it leads back to LOADING so a recovered service can be picked up without restarting
#: the station. FAILED → FAILED is permitted: a second failure while already failed is
#: normal, and making it illegal would turn a provider outage into a crash.
_TRANSITIONS: Final[dict[ModelState, frozenset[ModelState]]] = {
    ModelState.UNAVAILABLE: frozenset({ModelState.LOADING, ModelState.FAILED}),
    ModelState.LOADING: frozenset(
        {ModelState.READY, ModelState.FAILED, ModelState.UNAVAILABLE}
    ),
    ModelState.READY: frozenset(
        {ModelState.GENERATING, ModelState.UNLOADING, ModelState.FAILED}
    ),
    ModelState.GENERATING: frozenset({ModelState.READY, ModelState.FAILED}),
    ModelState.UNLOADING: frozenset({ModelState.UNAVAILABLE, ModelState.FAILED}),
    ModelState.FAILED: frozenset(
        {ModelState.LOADING, ModelState.UNAVAILABLE, ModelState.FAILED}
    ),
}


class IllegalModelTransition(RuntimeError):
    """A lifecycle transition that is not in the table.

    Raised rather than tolerated for the same reason Phase 6's pipeline raises on its own
    illegal transitions: silently allowing READY → GENERATING from a model that never loaded
    would produce a generation attempt against nothing, and the error would surface somewhere
    far away as a timeout.
    """

    def __init__(self, current: ModelState, target: ModelState) -> None:
        super().__init__(
            f"illegal model transition {current.value} -> {target.value}; "
            f"legal from {current.value}: "
            f"{', '.join(sorted(s.value for s in _TRANSITIONS.get(current, frozenset())))}"
        )
        self.current = current
        self.target = target


def check_transition(current: ModelState, target: ModelState) -> ModelState:
    """Return ``target`` if the move is legal, else raise."""
    if target not in _TRANSITIONS.get(current, frozenset()):
        raise IllegalModelTransition(current, target)
    return target


@dataclass(frozen=True)
class GenerationProfile:
    """One §7.8 preset: what to ask the model for, and nothing else.

    Deliberately contains no QC, originality or mastering settings. The type is the
    enforcement of §7.20's rule — a profile *cannot* relax a safety threshold because it has
    nowhere to put one.
    """

    name: str
    inference_steps: int
    guidance_scale: float
    #: Multiplied into the request timeout. A quality profile is allowed to take longer
    #: before it is called hung.
    timeout_multiplier: float = 1.0
    #: Longest track this profile should be asked for, when it should be capped at all.
    max_duration_seconds: float | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if self.inference_steps < 1:
            raise ValueError(f"profile {self.name!r}: inference_steps must be >= 1")
        if self.guidance_scale < 0:
            raise ValueError(f"profile {self.name!r}: guidance_scale must be >= 0")
        if self.timeout_multiplier <= 0:
            raise ValueError(f"profile {self.name!r}: timeout_multiplier must be > 0")


#: Starting points, to be revisited against measured hardware (§7.8, §7.21).
#:
#: The step counts follow ACE-Step's documented turbo range (1–20). They are *defaults*: the
#: whole point of making profiles configuration is that the numbers that matter are the ones
#: measured on the actual GPU, and §7.21's benchmark is what sets them.
BUILTIN_PROFILES: Final[dict[str, GenerationProfile]] = {
    "fast": GenerationProfile(
        name="fast",
        inference_steps=4,
        guidance_scale=2.0,
        timeout_multiplier=0.6,
        description="Fewest steps. For buffer pressure, where a track now beats a better "
        "track later.",
    ),
    "balanced": GenerationProfile(
        name="balanced",
        inference_steps=8,
        guidance_scale=3.0,
        timeout_multiplier=1.0,
        description="The documented turbo default. Normal operation.",
    ),
    "quality": GenerationProfile(
        name="quality",
        inference_steps=16,
        guidance_scale=4.5,
        timeout_multiplier=1.8,
        description="More steps and stronger guidance. Only when the buffer is healthy.",
    ),
    "vocal": GenerationProfile(
        name="vocal",
        inference_steps=28,
        guidance_scale=7.5,
        timeout_multiplier=2.6,
        description="For tracks with words. Measured: below ~16 steps the arrangement "
        "renders and the diction does not.",
    ),
}


def profile_for_buffer(
    level: str, *, configured: str, available: dict[str, GenerationProfile]
) -> GenerationProfile:
    """Pick a profile for the current buffer level (§7.20).

    The ladder only ever moves *downward* in cost from the configured profile. A station
    configured for ``fast`` does not get upgraded to ``quality`` because its buffer happens
    to be full — the operator chose ``fast``, and silently spending three times the GPU time
    on their behalf is not a decision this function is entitled to make.

    Falls back to the configured profile when the level is unrecognised, rather than
    guessing: an unknown buffer state is not evidence of urgency.
    """
    # Ordered by cost, which is why `vocal` sits at the top: 28 steps against quality's 16.
    # It is listed here so that a station configured for vocals is not quietly demoted to
    # `quality` the moment buffer-aware stepping is switched on — at 16 steps the words stop
    # resolving, and losing them to a *healthy* buffer would be the wrong way round.
    order = ["fast", "balanced", "quality", "vocal"]
    ceiling = {
        "healthy": "vocal",
        "low": "balanced",
        "critical": "fast",
        "empty": "fast",
    }.get(level.lower())
    if ceiling is None:
        return available.get(configured, BUILTIN_PROFILES["balanced"])

    configured_rank = order.index(configured) if configured in order else len(order) - 1
    ceiling_rank = order.index(ceiling)
    chosen = order[min(configured_rank, ceiling_rank)]
    # A profile named in the ladder but absent from configuration falls back rather than
    # raising: a missing optional profile must not stop the station generating.
    return available.get(chosen) or available.get(configured) or BUILTIN_PROFILES["balanced"]
