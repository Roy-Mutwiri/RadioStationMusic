"""Health registry and environment checks (§35, §73, §79, milestone 1.8).

The registry's value is entirely in its failure behaviour, so that is what is
tested: a check that hangs, a check that raises, and an optional component that is
genuinely broken must each produce a *useful* aggregate rather than taking the
health endpoint down with them. An unreliable health endpoint is worse than none,
because it is consulted precisely when things are going wrong.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tradefix_radio.contracts.enums import HealthStatus
from tradefix_radio.contracts.health import ComponentHealthV1
from tradefix_radio.core.clock import VirtualClock
from tradefix_radio.core.errors import DependencyMissingError
from tradefix_radio.monitoring import checks as env_checks
from tradefix_radio.monitoring.health import HealthRegistry, healthy, unhealthy
from tests.conftest import FIXED_NOW, make_settings


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock(start=FIXED_NOW)


# ---------------------------------------------------------------- aggregation


async def test_all_healthy_aggregates_to_healthy(clock: VirtualClock) -> None:
    registry = HealthRegistry(clock=clock)
    registry.register("a", lambda: _ok("a", clock))
    registry.register("b", lambda: _ok("b", clock))
    health = await registry.check_all()
    assert health.status is HealthStatus.HEALTHY
    assert health.can_start
    assert len(health.components) == 2


async def test_empty_registry_reports_unknown(clock: VirtualClock) -> None:
    """Nothing checked is not the same as everything fine."""
    health = await HealthRegistry(clock=clock).check_all()
    assert health.status is HealthStatus.UNKNOWN


async def test_worst_required_component_sets_overall_status(clock: VirtualClock) -> None:
    registry = HealthRegistry(clock=clock)
    registry.register("ok", lambda: _ok("ok", clock))
    registry.register("warn", lambda: _bad("warn", HealthStatus.DEGRADED, clock))
    registry.register("dead", lambda: _bad("dead", HealthStatus.CRITICAL, clock))
    health = await registry.check_all()
    assert health.status is HealthStatus.CRITICAL
    assert health.blocking_failures == ("dead",)
    assert not health.can_start


async def test_optional_component_degrades_but_does_not_block(clock: VirtualClock) -> None:
    """A missing GPU in development is a fact, not an emergency (§73)."""
    registry = HealthRegistry(clock=clock)
    registry.register("gpu", lambda: _bad("gpu", HealthStatus.CRITICAL, clock), required=False)
    health = await registry.check_all()
    assert health.status is HealthStatus.DEGRADED
    assert health.can_start
    assert health.blocking_failures == ()


async def test_registry_owns_the_required_flag(clock: VirtualClock) -> None:
    """A check returning the wrong flag must not be able to block startup."""

    async def lying_check() -> ComponentHealthV1:
        return unhealthy(
            "liar", HealthStatus.CRITICAL, "broken", clock=clock, required=True
        )

    registry = HealthRegistry(clock=clock)
    registry.register("liar", lying_check, required=False)
    health = await registry.check_all()
    component = health.component("liar")
    assert component is not None
    assert component.required is False
    assert health.can_start


async def test_uptime_is_reported(clock: VirtualClock) -> None:
    registry = HealthRegistry(clock=clock)
    registry.register("a", lambda: _ok("a", clock))
    await clock.advance(120)
    health = await registry.check_all()
    assert health.uptime_seconds == pytest.approx(120.0)


# ---------------------------------------------------------------- failure modes


async def test_a_raising_check_becomes_a_critical_result(clock: VirtualClock) -> None:
    """The probe must answer even when the thing it probes explodes."""

    async def explode() -> ComponentHealthV1:
        raise RuntimeError("database on fire")

    registry = HealthRegistry(clock=clock)
    registry.register("db", explode)
    health = await registry.check_all()
    component = health.component("db")
    assert component is not None
    assert component.status is HealthStatus.CRITICAL
    assert "RuntimeError" in component.detail
    assert "database on fire" in component.detail


async def test_a_raising_check_does_not_hide_the_others(clock: VirtualClock) -> None:
    async def explode() -> ComponentHealthV1:
        raise RuntimeError("boom")

    registry = HealthRegistry(clock=clock)
    registry.register("broken", explode)
    registry.register("fine", lambda: _ok("fine", clock))
    health = await registry.check_all()
    assert len(health.components) == 2
    fine = health.component("fine")
    assert fine is not None
    assert fine.status is HealthStatus.HEALTHY


async def test_dependency_missing_error_remediation_is_surfaced(clock: VirtualClock) -> None:
    """A raised DependencyMissingError must not lose its remediation text."""

    async def missing() -> ComponentHealthV1:
        raise DependencyMissingError("ffmpeg absent", remediation="winget install Gyan.FFmpeg")

    registry = HealthRegistry(clock=clock)
    registry.register("ffmpeg", missing)
    health = await registry.check_all()
    component = health.component("ffmpeg")
    assert component is not None
    assert "winget install Gyan.FFmpeg" in component.remediation


async def test_a_hanging_check_times_out_as_critical() -> None:
    """A wedged dependency must not stall the health endpoint (§35).

    Uses the real clock because the timeout is enforced with
    ``asyncio.wait_for``, which is driven by the event loop rather than the
    injected clock.
    """

    async def hang() -> ComponentHealthV1:
        await asyncio.sleep(10)
        raise AssertionError("should not be reached")

    registry = HealthRegistry(timeout_seconds=0.05)
    registry.register("wedged", hang)
    health = await registry.check_all()
    component = health.component("wedged")
    assert component is not None
    assert component.status is HealthStatus.CRITICAL
    assert "did not complete" in component.detail
    assert component.remediation


async def test_a_hanging_check_does_not_delay_the_others() -> None:
    """Checks run concurrently, so one slow probe does not serialise the rest."""

    async def hang() -> ComponentHealthV1:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    clock = VirtualClock(start=FIXED_NOW)
    registry = HealthRegistry(timeout_seconds=0.1)
    registry.register("wedged", hang)
    for name in ("a", "b", "c"):
        registry.register(name, lambda n=name: _ok(n, clock))  # type: ignore[misc]
    health = await asyncio.wait_for(registry.check_all(), timeout=2.0)
    assert len(health.components) == 4


async def test_latency_is_recorded_when_a_check_omits_it(clock: VirtualClock) -> None:
    registry = HealthRegistry(clock=clock)
    registry.register("a", lambda: _ok("a", clock))
    health = await registry.check_all()
    component = health.component("a")
    assert component is not None
    assert component.latency_ms is not None
    assert component.latency_ms >= 0.0


# ---------------------------------------------------------------- registration


async def test_duplicate_registration_is_rejected(clock: VirtualClock) -> None:
    registry = HealthRegistry(clock=clock)
    registry.register("a", lambda: _ok("a", clock))
    with pytest.raises(ValueError, match="already registered"):
        registry.register("a", lambda: _ok("a", clock))


def test_empty_name_is_rejected(clock: VirtualClock) -> None:
    registry = HealthRegistry(clock=clock)
    with pytest.raises(ValueError, match="must not be empty"):
        registry.register("", lambda: _ok("x", clock))


def test_non_positive_timeout_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        HealthRegistry(timeout_seconds=0)


async def test_check_one_on_an_unknown_name_raises(clock: VirtualClock) -> None:
    registry = HealthRegistry(clock=clock)
    with pytest.raises(KeyError):
        await registry.check_one("nope")


async def test_unregister_removes_the_check_and_its_cached_result(
    clock: VirtualClock,
) -> None:
    registry = HealthRegistry(clock=clock)
    registry.register("a", lambda: _ok("a", clock))
    await registry.check_all()
    registry.unregister("a")
    assert registry.names == ()
    assert registry.last_known().components == ()


async def test_last_known_avoids_re_running_checks(clock: VirtualClock) -> None:
    """A 1 s dashboard refresh must not re-probe the GPU every tick."""
    calls = 0

    async def counted() -> ComponentHealthV1:
        nonlocal calls
        calls += 1
        return healthy("counted", clock=clock)

    registry = HealthRegistry(clock=clock)
    registry.register("counted", counted)
    await registry.check_all()
    for _ in range(5):
        registry.last_known()
    assert calls == 1


async def test_last_known_before_any_check_is_unknown(clock: VirtualClock) -> None:
    registry = HealthRegistry(clock=clock)
    registry.register("a", lambda: _ok("a", clock))
    assert registry.last_known().status is HealthStatus.UNKNOWN


# ---------------------------------------------------------------- constructors


def test_healthy_rejects_being_used_for_a_failure(clock: VirtualClock) -> None:
    with pytest.raises(ValueError, match="use healthy"):
        unhealthy("a", HealthStatus.HEALTHY, "fine", clock=clock)


def test_measurements_are_an_explicit_dict(clock: VirtualClock) -> None:
    """Keyword-splatted measurements could collide with real parameter names."""
    component = healthy(
        "disk", clock=clock, detail="ok", measurements={"free_gb": 207.6, "required": 1.0}
    )
    assert component.measurements == {"free_gb": 207.6, "required": 1.0}
    assert component.required is True


def test_component_is_blocking_only_when_required_and_critical(
    clock: VirtualClock,
) -> None:
    assert unhealthy("a", HealthStatus.CRITICAL, "x", clock=clock, required=True).is_blocking
    assert not unhealthy(
        "a", HealthStatus.CRITICAL, "x", clock=clock, required=False
    ).is_blocking
    assert not unhealthy("a", HealthStatus.DEGRADED, "x", clock=clock).is_blocking


# ---------------------------------------------------------------- real checks


async def test_python_check_passes_on_the_supported_interpreter(
    clock: VirtualClock,
) -> None:
    """The suite runs on the ADR-01 interpreter, so this must pass here."""
    result = await env_checks.check_python(clock)
    assert result.status is HealthStatus.HEALTHY
    assert "3.10" in result.detail


async def test_directories_check_fails_before_init(
    clock: VirtualClock, tmp_path: Path
) -> None:
    settings = make_settings(tmp_path)
    result = await env_checks.check_directories(clock, settings)
    assert result.status is HealthStatus.CRITICAL
    assert "tradefix init" in result.remediation


async def test_directories_check_passes_after_creation(
    clock: VirtualClock, tmp_path: Path
) -> None:
    settings = make_settings(tmp_path)
    for directory in settings.paths.all_directories():
        directory.mkdir(parents=True, exist_ok=True)
    result = await env_checks.check_directories(clock, settings)
    assert result.status is HealthStatus.HEALTHY


async def test_disk_space_check_reports_measurements(
    clock: VirtualClock, tmp_path: Path
) -> None:
    settings = make_settings(tmp_path)
    result = await env_checks.check_disk_space(clock, settings)
    assert "free_gb" in result.measurements
    assert result.measurements["total_gb"] > 0


async def test_disk_space_check_goes_critical_against_an_absurd_floor(
    clock: VirtualClock, tmp_path: Path
) -> None:
    settings = make_settings(
        tmp_path, retention={"min_free_gb": 9_000.0, "alert_free_gb": 9_500.0}
    )
    result = await env_checks.check_disk_space(clock, settings)
    assert result.status is HealthStatus.CRITICAL
    assert "retention" in result.remediation


async def test_simulated_feed_check_needs_nothing_external(
    clock: VirtualClock, tmp_path: Path
) -> None:
    """§7: the whole application must be testable with no internet or credentials."""
    settings = make_settings(tmp_path)
    result = await env_checks.check_market_feed(clock, settings)
    assert result.status is HealthStatus.HEALTHY


async def test_replay_feed_check_fails_on_a_missing_file(
    clock: VirtualClock, tmp_path: Path
) -> None:
    settings = make_settings(
        tmp_path, market={"feed": "replay", "replay_file": str(tmp_path / "absent.csv")}
    )
    result = await env_checks.check_market_feed(clock, settings)
    assert result.status is HealthStatus.CRITICAL


async def test_rest_feed_without_a_key_is_degraded_not_fatal(
    clock: VirtualClock, tmp_path: Path
) -> None:
    settings = make_settings(
        tmp_path, market={"feed": "rest", "rest_base_url": "https://example.invalid"}
    )
    result = await env_checks.check_market_feed(clock, settings)
    assert result.status is HealthStatus.DEGRADED
    assert "REST_API_KEY" in result.remediation


async def test_mock_provider_check_needs_no_gpu(clock: VirtualClock, tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    result = await env_checks.check_generation_provider(clock, settings)
    assert result.status is HealthStatus.HEALTHY
    assert "mock" in result.detail


async def test_ace_step_check_fails_with_startup_instructions(
    clock: VirtualClock, tmp_path: Path
) -> None:
    """The unreachable case must tell the operator exactly how to start it (§79)."""
    settings = make_settings(
        tmp_path,
        generation={
            "provider": "ace_step",
            "ace_step": {"base_url": "http://127.0.0.1:1"},
        },
    )
    result = await env_checks.check_generation_provider(clock, settings)
    assert result.status is HealthStatus.CRITICAL
    assert "uv run acestep-api" in result.remediation
    assert env_checks.ACE_STEP_PYTHON_RANGE in result.remediation


async def test_disabled_obs_is_healthy(clock: VirtualClock, tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    result = await env_checks.check_obs(clock, settings)
    assert result.status is HealthStatus.HEALTHY
    assert "disabled" in result.detail


async def test_unreachable_obs_explains_the_setup(clock: VirtualClock, tmp_path: Path) -> None:
    settings = make_settings(tmp_path, obs={"enabled": True, "port": 1})
    result = await env_checks.check_obs(clock, settings)
    assert result.status is HealthStatus.CRITICAL
    assert "WebSocket Server Settings" in result.remediation
    # The station must keep broadcasting without OBS; the message should say so.
    assert "without OBS" in result.remediation


async def test_null_sink_needs_no_audio_device(clock: VirtualClock, tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    result = await env_checks.check_audio_device(clock, settings)
    assert result.status is HealthStatus.HEALTHY


# ---------------------------------------------------------------- required matrix


def test_core_checks_are_always_required(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    for name in ("python", "directories", "disk_space", "database", "config"):
        assert env_checks.is_required(name, settings)


def test_node_is_never_required(tmp_path: Path) -> None:
    """The station broadcasts without a frontend build."""
    assert not env_checks.is_required("node", make_settings(tmp_path))


def test_gpu_is_required_only_for_the_ace_step_provider(tmp_path: Path) -> None:
    assert not env_checks.is_required("gpu", make_settings(tmp_path))
    with_ace = make_settings(tmp_path, generation={"provider": "ace_step"})
    assert env_checks.is_required("gpu", with_ace)


def test_audio_device_is_required_only_for_the_sounddevice_sink(tmp_path: Path) -> None:
    assert not env_checks.is_required("audio_device", make_settings(tmp_path))
    with_device = make_settings(tmp_path, audio={"sink": "sounddevice"})
    assert env_checks.is_required("audio_device", with_device)


def test_ffmpeg_is_not_required_with_the_mock_provider(tmp_path: Path) -> None:
    """Development on a machine without FFmpeg must still work."""
    assert not env_checks.is_required("ffmpeg", make_settings(tmp_path))


def test_market_feed_is_required_when_it_is_not_simulated(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path, market={"feed": "replay", "replay_file": str(tmp_path / "x.csv")}
    )
    assert env_checks.is_required("market_feed", settings)


def test_ace_step_toolchain_is_advisory_only(tmp_path: Path) -> None:
    """Its absence must never block a mock-provider run."""
    assert not env_checks.is_required("ace_step_environment", make_settings(tmp_path))


# ---------------------------------------------------------------- helpers


async def _ok(name: str, clock: VirtualClock) -> ComponentHealthV1:
    return healthy(name, clock=clock, detail="fine")


async def _bad(
    name: str, status: HealthStatus, clock: VirtualClock
) -> ComponentHealthV1:
    return unhealthy(name, status, "not fine", clock=clock, remediation="do something")
