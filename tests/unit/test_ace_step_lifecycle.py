"""Model lifecycle, profiles and configuration (§7.6, §7.8, §7.20, §7.29, §7.30)."""

from __future__ import annotations

import pytest
from tests.conftest import make_settings
from tradefix_radio.core.errors import ConfigurationError
from tradefix_radio.generation.ace_step.lifecycle import (
    BUILTIN_PROFILES,
    GenerationProfile,
    IllegalModelTransition,
    ModelState,
    check_transition,
    profile_for_buffer,
)

# ----------------------------------------------------------------- states


def test_the_normal_path_is_legal() -> None:
    state = ModelState.UNAVAILABLE
    for target in (
        ModelState.LOADING,
        ModelState.READY,
        ModelState.GENERATING,
        ModelState.READY,
        ModelState.UNLOADING,
        ModelState.UNAVAILABLE,
    ):
        state = check_transition(state, target)
    assert state is ModelState.UNAVAILABLE


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ModelState.UNAVAILABLE, ModelState.READY),
        (ModelState.UNAVAILABLE, ModelState.GENERATING),
        (ModelState.LOADING, ModelState.GENERATING),
        (ModelState.UNLOADING, ModelState.GENERATING),
        (ModelState.GENERATING, ModelState.UNLOADING),
    ],
)
def test_skipping_a_state_fails_explicitly(
    current: ModelState, target: ModelState
) -> None:
    """§7.6 wants explicit states, which only means something if the gaps are enforced.

    ``UNAVAILABLE -> GENERATING`` is the one that matters: it would mean generating against
    a model that never loaded, and the error would surface far away as a timeout.
    """
    with pytest.raises(IllegalModelTransition):
        check_transition(current, target)


def test_a_failed_model_can_be_reloaded() -> None:
    """Recovery without a process restart."""
    assert check_transition(ModelState.FAILED, ModelState.LOADING) is ModelState.LOADING


def test_failing_twice_is_allowed() -> None:
    """A second failure while already failed is normal.

    Making it illegal would turn a provider outage — where every call fails — into a crash
    inside the error handler.
    """
    assert check_transition(ModelState.FAILED, ModelState.FAILED) is ModelState.FAILED


def test_only_ready_can_generate() -> None:
    assert ModelState.READY.can_generate
    for state in ModelState:
        if state is not ModelState.READY:
            assert not state.can_generate


# --------------------------------------------------------------- profiles


def test_the_builtin_profiles_are_ordered_by_cost() -> None:
    """fast < balanced < quality < vocal, or the §7.20 ladder is meaningless."""
    names = ("fast", "balanced", "quality", "vocal")
    steps = [BUILTIN_PROFILES[name].inference_steps for name in names]
    assert steps == sorted(steps)
    assert len(set(steps)) == len(names)


def test_the_vocal_profile_has_enough_steps_for_diction() -> None:
    """Measured, not chosen for roundness.

    At 8 steps the model rendered the arrangement and swallowed the words — twice, on real
    station output whose stored submission showed the full validated lyric had been sent. The
    same prompt at 28 steps produced intelligible rap. 16 is the nearest value not shown to
    work, so the floor is set above it.
    """
    assert BUILTIN_PROFILES["vocal"].inference_steps > 16
    assert BUILTIN_PROFILES["vocal"].guidance_scale > BUILTIN_PROFILES["quality"].guidance_scale


def test_a_profile_cannot_carry_a_qc_threshold() -> None:
    """§7.20, enforced structurally rather than by convention.

    *"Do not reduce audio safety thresholds during buffer pressure."* A profile has nowhere
    to put one — the fields are generation settings and nothing else — so the rule cannot be
    broken by someone adding a field in a hurry.
    """
    fields = set(GenerationProfile.__dataclass_fields__)
    assert fields == {
        "name",
        "inference_steps",
        "guidance_scale",
        "timeout_multiplier",
        "max_duration_seconds",
        "description",
    }


def test_an_invalid_profile_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="inference_steps"):
        GenerationProfile(name="broken", inference_steps=0, guidance_scale=1.0)


# ------------------------------------------------------- the §7.20 ladder


@pytest.mark.parametrize(
    ("level", "expected"),
    [("healthy", "quality"), ("low", "balanced"), ("critical", "fast"), ("empty", "fast")],
)
def test_buffer_pressure_steps_the_profile_down(level: str, expected: str) -> None:
    chosen = profile_for_buffer(level, configured="quality", available=BUILTIN_PROFILES)
    assert chosen.name == expected


def test_the_ladder_never_upgrades_beyond_what_was_configured() -> None:
    """A station configured for `fast` does not get `quality` for having a full buffer.

    The operator chose `fast`; spending three times the GPU time on their behalf is not a
    decision this function is entitled to make.
    """
    chosen = profile_for_buffer("healthy", configured="fast", available=BUILTIN_PROFILES)
    assert chosen.name == "fast"


def test_an_unknown_buffer_level_keeps_the_configured_profile() -> None:
    """An unrecognised state is not evidence of urgency."""
    chosen = profile_for_buffer("bananas", configured="balanced", available=BUILTIN_PROFILES)
    assert chosen.name == "balanced"


def test_a_missing_profile_falls_back_rather_than_raising() -> None:
    """A gap in optional configuration must not stop the station generating."""
    only_balanced = {"balanced": BUILTIN_PROFILES["balanced"]}
    chosen = profile_for_buffer("critical", configured="balanced", available=only_balanced)
    assert chosen.name == "balanced"


# ---------------------------------------------------------- config (§7.29)


def test_ace_step_settings_default_to_a_usable_configuration(tmp_path, clean_environ) -> None:
    settings = make_settings(tmp_path, clean_environ)
    ace = settings.generation.ace_step
    assert ace.base_url.startswith("http")
    assert ace.profile in ace.profiles
    assert set(ace.profiles) >= {"fast", "balanced", "quality"}
    # Loading gets its own, much longer budget than a generation: a cold start pulls a
    # checkpoint into VRAM and may download it.
    assert ace.load_timeout_seconds > ace.timeout_seconds


def test_an_unknown_default_profile_is_rejected(tmp_path, clean_environ) -> None:
    """Caught at load, not at the first generation under buffer pressure."""
    with pytest.raises(ConfigurationError, match="not among the configured profiles"):
        make_settings(
            tmp_path, clean_environ, generation={"ace_step": {"profile": "ludicrous"}}
        )


def test_launching_the_worker_requires_knowing_where_it_is(tmp_path, clean_environ) -> None:
    with pytest.raises(ConfigurationError, match="worker_directory"):
        make_settings(
            tmp_path, clean_environ, generation={"ace_step": {"worker_process": True}}
        )


def test_the_api_key_is_a_secret(tmp_path, clean_environ) -> None:
    """§50: secrets must never display unmasked after save."""
    settings = make_settings(
        tmp_path, clean_environ, generation={"ace_step": {"api_key": "super-secret"}}
    )
    assert "super-secret" not in str(settings.generation.ace_step)
    assert "super-secret" not in repr(settings.generation.ace_step)
    assert settings.generation.ace_step.api_key is not None
    assert settings.generation.ace_step.api_key.get_secret_value() == "super-secret"


def test_concurrent_jobs_are_refused_for_this_provider(tmp_path, clean_environ) -> None:
    """§7.22: prefer stable single-job GPU execution.

    Phase 1 already encoded this; asserted here because Phase 7 is where it starts to
    matter — two generations on one 12 GB card is how an OOM is manufactured.
    """
    with pytest.raises(ConfigurationError, match="contend for VRAM"):
        make_settings(
            tmp_path,
            clean_environ,
            generation={"provider": "ace_step", "max_concurrent_jobs": 2},
        )
