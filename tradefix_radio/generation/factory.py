"""Choosing a generation provider (§7, ADR-02).

§7's opening instruction is that ACE-Step must be *"one implementation of the existing
generation-provider interface, not a special case spread through the codebase"*. That needs
exactly one place where `settings.generation.provider` is turned into an object, and before
Phase 7 there wasn't one — the dev runner and the soak command each constructed
`MockMusicProvider` directly.

Two call sites is not yet a problem; the third would have been. With ACE-Step added, every
one of them would otherwise have grown the same `if provider == "ace_step"` branch, the same
profile wiring and the same GPU probe — and they would have drifted. So the decision lives
here and the call sites ask for a provider.

The factory does not *start* anything. Loading the model is :meth:`AceStepProvider.load`, and
the caller decides when to pay a cold start — a soak wants it before the clock starts, the
dev runner wants it during warm-up.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

import structlog

from tradefix_radio.generation.mock import MockMusicProvider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradefix_radio.config.schema import AppSettings
    from tradefix_radio.core.clock import Clock
    from tradefix_radio.generation.ace_step.lifecycle import GenerationProfile
    from tradefix_radio.generation.provider import MusicGenerationProvider

_log = structlog.get_logger(__name__)

__all__ = ["build_provider", "provider_profiles"]


def build_provider(
    settings: AppSettings,
    *,
    clock: Clock,
    seed: int | None = None,
) -> MusicGenerationProvider:
    """The provider this configuration asks for.

    ``seed`` is the mock's determinism seed and is ignored by ACE-Step, which takes its seed
    per request from the blueprint — §23's registry assigns it, so a provider-level seed
    would be the wrong layer.
    """
    name = settings.generation.provider
    if name == "mock":
        return MockMusicProvider(
            settings.generation.mock,
            clock=clock,
            seed=seed if seed is not None else random.randrange(2**31),  # noqa: S311
        )
    if name == "ace_step":
        # Imported lazily so a mock-only process never pays for the ACE-Step module graph,
        # and — more usefully — so a syntax or import error in the ACE-Step package cannot
        # stop a station that is not using it from starting.
        from tradefix_radio.generation.ace_step import (  # noqa: PLC0415
            AceStepClient,
            AceStepProvider,
        )

        ace = settings.generation.ace_step
        api_key = ace.api_key.get_secret_value() if ace.api_key is not None else None
        _log.info(
            "generation.provider_selected",
            provider="ace_step",
            base_url=ace.base_url,
            dit_model=ace.dit_model,
            profile=ace.profile,
        )
        return AceStepProvider(
            ace,
            clock=clock,
            client=AceStepClient(ace.base_url, api_key=api_key),
            profiles=provider_profiles(settings),
        )
    # Unreachable while the config `Literal` holds, which is the point: adding a provider to
    # the enum without adding it here should fail loudly at startup rather than silently
    # returning the mock into a production broadcast.
    raise ValueError(f"unknown generation provider {name!r}")


def provider_profiles(settings: AppSettings) -> dict[str, GenerationProfile]:
    """Configured §7.8 profiles, as the lifecycle module's type.

    Converted here rather than having the provider read pydantic settings directly, so the
    profile type stays a plain dataclass the lifecycle logic can be tested against without
    constructing an `AppSettings`.
    """
    from tradefix_radio.generation.ace_step.lifecycle import (  # noqa: PLC0415
        GenerationProfile,
    )

    return {
        name: GenerationProfile(
            name=name,
            inference_steps=profile.inference_steps,
            guidance_scale=profile.guidance_scale,
            timeout_multiplier=profile.timeout_multiplier,
            max_duration_seconds=profile.max_duration_seconds,
            description=profile.description,
        )
        for name, profile in settings.generation.ace_step.profiles.items()
    }
