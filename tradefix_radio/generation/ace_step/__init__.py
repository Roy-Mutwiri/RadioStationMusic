"""ACE-Step 1.5 integration (§7).

Everything model-specific lives behind this package boundary. Outside it, the station knows
only :class:`~tradefix_radio.generation.provider.MusicGenerationProvider`, which is §7's
opening requirement: ACE-Step is *one implementation of the existing interface, not a special
case spread through the codebase*.

The service runs as a separate OS process under its own Python 3.12 — forced by ACE-Step's
``requires-python = ">=3.11,<3.13"`` against ADR-01's 3.10 pin, and independently what §7.5
wants so a model crash cannot reach the playout engine. See
``docs/status/PHASE_7_ENVIRONMENT.md`` for the full determination.
"""

from tradefix_radio.generation.ace_step.client import (
    AceStepClient,
    AceStepHttpError,
    TaskResult,
    TaskStatus,
)
from tradefix_radio.generation.ace_step.lifecycle import (
    BUILTIN_PROFILES,
    GenerationProfile,
    IllegalModelTransition,
    ModelState,
    profile_for_buffer,
)
from tradefix_radio.generation.ace_step.prompt import (
    AceStepPromptBuilder,
    GenerationSpec,
)
from tradefix_radio.generation.ace_step.provider import AceStepProvider, ProviderStatus

__all__ = [
    "BUILTIN_PROFILES",
    "AceStepClient",
    "AceStepHttpError",
    "AceStepPromptBuilder",
    "AceStepProvider",
    "GenerationProfile",
    "GenerationSpec",
    "IllegalModelTransition",
    "ModelState",
    "ProviderStatus",
    "TaskResult",
    "TaskStatus",
    "profile_for_buffer",
]
