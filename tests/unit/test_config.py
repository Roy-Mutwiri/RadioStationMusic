"""Configuration loading and validation (§50, §71, milestone 1.2).

§71 requires configuration to be validated at startup and to "fail clearly if
invalid". These tests assert the *clarity*, not only the failure: an invalid
setting must produce a message naming the field path, because that is the whole
difference between a config error an operator can fix at 2 a.m. and one they
cannot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tradefix_radio.config.loader import (
    _coerce_scalar,
    _load_dotenv,
    _nest,
    ensure_directories,
    load_settings,
)
from tradefix_radio.config.schema import AppSettings, BpmBand, EnergyWeights
from tradefix_radio.contracts.enums import RunMode
from tradefix_radio.core.errors import ConfigurationError
from tests.conftest import make_settings


# ---------------------------------------------------------------- loading


def test_defaults_load_in_development_mode(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    assert settings.mode is RunMode.DEVELOPMENT
    assert settings.market.feed == "simulated"
    assert settings.generation.provider == "mock"
    assert settings.audio.sink == "null_sink"


def test_mode_overlay_is_applied(tmp_path: Path) -> None:
    """``simulation.yaml`` must override the development buffer sizes."""
    dev = make_settings(tmp_path)
    sim = make_settings(tmp_path, mode_override=RunMode.SIMULATION)
    assert dev.radio.target_buffer_minutes != sim.radio.target_buffer_minutes
    assert sim.radio.target_buffer_minutes == 45.0


def test_explicit_mode_argument_beats_environment(tmp_path: Path) -> None:
    settings = load_settings(
        env_file=None,
        environ={"TRADEFIX_MODE": "production"},
        mode=RunMode.SIMULATION,
        overrides={"paths": {"root_dir": str(tmp_path)}},
    )
    assert settings.mode is RunMode.SIMULATION


def test_environment_overrides_yaml(tmp_path: Path) -> None:
    # 15 rather than an arbitrary larger number: development.yaml caps the maximum
    # buffer at 20 minutes, and the cross-section check correctly rejects a target
    # above the maximum.
    settings = make_settings(
        tmp_path,
        environ={"TRADEFIX_RADIO__TARGET_BUFFER_MINUTES": "15"},
    )
    assert settings.radio.target_buffer_minutes == 15.0


def test_nested_environment_variables_two_levels_deep(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        environ={"TRADEFIX_GENERATION__ACE_STEP__INFERENCE_STEPS": "12"},
    )
    assert settings.generation.ace_step.inference_steps == 12


def test_operator_yaml_layers_above_mode_overlay(tmp_path: Path) -> None:
    config_file = tmp_path / "operator.yaml"
    config_file.write_text("radio:\n  target_buffer_minutes: 17\n", encoding="utf-8")
    settings = load_settings(
        config_file=config_file,
        env_file=None,
        environ={},
        overrides={
            "paths": {"root_dir": str(tmp_path)},
            # Raise the ceiling so the operator value is the thing under test
            # rather than the buffer-ordering rule.
            "radio": {"maximum_buffer_minutes": 30.0},
        },
    )
    assert settings.radio.target_buffer_minutes == 17.0


def test_missing_operator_config_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_settings(config_file=tmp_path / "nope.yaml", env_file=None, environ={})
    assert "not found" in str(caught.value)


def test_invalid_yaml_reports_the_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("radio:\n  - this is not a mapping\n   bad indent:\n", encoding="utf-8")
    with pytest.raises(ConfigurationError) as caught:
        load_settings(config_file=bad, env_file=None, environ={})
    assert str(bad) in str(caught.value)


def test_top_level_yaml_must_be_a_mapping(tmp_path: Path) -> None:
    bad = tmp_path / "list.yaml"
    bad.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="mapping"):
        load_settings(config_file=bad, env_file=None, environ={})


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    """``extra="forbid"`` turns a typo into an error instead of a silent no-op.

    Without this, ``taget_buffer_minutes`` would be ignored and the operator would
    spend an afternoon wondering why their change did nothing.
    """
    with pytest.raises(ConfigurationError) as caught:
        make_settings(tmp_path, radio={"taget_buffer_minutes": 50})
    message = str(caught.value)
    assert "radio.taget_buffer_minutes" in message
    assert "Extra inputs" in message or "extra" in message.lower()


# ---------------------------------------------------------------- field paths


@pytest.mark.parametrize(
    ("overrides", "expected_path"),
    [
        ({"radio": {"target_buffer_minutes": -1}}, "radio.target_buffer_minutes"),
        ({"market": {"poll_interval_seconds": 0}}, "market.poll_interval_seconds"),
        ({"mastering": {"target_lufs": 5.0}}, "mastering.target_lufs"),
        ({"api": {"port": 70_000}}, "api.port"),
        ({"diversity": {"bpm_tolerance": -3}}, "diversity.bpm_tolerance"),
        ({"generation": {"max_attempts": 0}}, "generation.max_attempts"),
        ({"audio": {"channels": 5}}, "audio.channels"),
    ],
)
def test_invalid_field_names_its_own_path(
    tmp_path: Path, overrides: dict[str, object], expected_path: str
) -> None:
    """Every validation failure must name the exact field (§71)."""
    with pytest.raises(ConfigurationError) as caught:
        make_settings(tmp_path, **overrides)
    assert expected_path in str(caught.value)


def test_all_errors_are_reported_not_just_the_first(tmp_path: Path) -> None:
    """An operator fixing config deserves the whole list in one pass."""
    with pytest.raises(ConfigurationError) as caught:
        make_settings(
            tmp_path,
            api={"port": 70_000},
            audio={"channels": 9},
            diversity={"bpm_tolerance": -1},
        )
    message = str(caught.value)
    assert "api.port" in message
    assert "audio.channels" in message
    assert "diversity.bpm_tolerance" in message


# ---------------------------------------------------------------- cross-section


def test_buffer_ordering_is_enforced(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="minimum < target <= maximum"):
        make_settings(
            tmp_path,
            radio={
                "minimum_buffer_minutes": 50.0,
                "target_buffer_minutes": 20.0,
                "maximum_buffer_minutes": 90.0,
            },
        )


def test_crossfade_longer_than_shortest_track_is_rejected(tmp_path: Path) -> None:
    """Two crossfades must fit inside the shortest permitted track."""
    with pytest.raises(ConfigurationError, match="crossfade"):
        make_settings(
            tmp_path,
            radio={"default_crossfade_seconds": 30.0},
            music={"min_duration_seconds": 40, "max_duration_seconds": 60},
        )


def test_qc_minimum_duration_above_music_minimum_is_rejected(tmp_path: Path) -> None:
    """Otherwise every short track the director may request is auto-rejected."""
    with pytest.raises(ConfigurationError, match="qc.min_duration_seconds"):
        make_settings(
            tmp_path,
            qc={"min_duration_seconds": 200.0},
            music={"min_duration_seconds": 150, "max_duration_seconds": 260},
        )


def test_mastering_target_outside_qc_window_is_rejected(tmp_path: Path) -> None:
    """A mastered track must be able to pass re-analysis."""
    with pytest.raises(ConfigurationError, match="outside the QC"):
        make_settings(
            tmp_path,
            mastering={"target_lufs": -28.0},
            qc={"min_loudness_lufs": -20.0, "max_loudness_lufs": -3.0},
        )


def test_locked_slots_must_fit_inside_the_target_buffer(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="locked_slots"):
        make_settings(
            tmp_path,
            radio={
                "locked_slots": 10,
                "semi_locked_slots": 10,
                "minimum_buffer_minutes": 4.0,
                "target_buffer_minutes": 10.0,
                "maximum_buffer_minutes": 20.0,
            },
        )


def test_emergency_reserve_cannot_exceed_maximum_buffer(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="emergency_reserve_minutes"):
        make_settings(tmp_path, radio={"emergency_reserve_minutes": 500.0})


def test_diversity_horizons_must_increase(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="strictly increasing"):
        make_settings(
            tmp_path,
            diversity={"horizon_short": 50, "horizon_medium": 20, "horizon_long": 100},
        )


def test_originality_review_threshold_below_reject(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="review_similarity"):
        make_settings(
            tmp_path, originality={"review_similarity": 0.9, "reject_similarity": 0.8}
        )


def test_retention_alert_must_be_above_minimum(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="alert_free_gb"):
        make_settings(tmp_path, retention={"min_free_gb": 50.0, "alert_free_gb": 10.0})


def test_market_staleness_ordering(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="disconnected_after_seconds"):
        make_settings(
            tmp_path,
            market={"stale_after_seconds": 100.0, "disconnected_after_seconds": 50.0},
        )


def test_configured_symbol_must_appear_in_aliases(tmp_path: Path) -> None:
    """Symbol discovery tries aliases in order; omitting the symbol is a mistake."""
    with pytest.raises(ConfigurationError, match="symbol_aliases"):
        make_settings(tmp_path, market={"symbol": "XAGUSD"})


# ---------------------------------------------------------------- production guards


def test_production_requires_a_control_token(tmp_path: Path) -> None:
    """§68: an unauthenticated control API could stop the broadcast."""
    with pytest.raises(ConfigurationError, match="api.control_token"):
        make_settings(
            tmp_path,
            mode_override=RunMode.PRODUCTION,
            market={"feed": "metatrader5"},
            generation={"provider": "ace_step"},
            obs={"enabled": False},
        )


def test_non_loopback_host_requires_a_control_token(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="api.control_token"):
        make_settings(tmp_path, api={"host": "0.0.0.0"})


def test_loopback_without_token_is_allowed_in_development(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, api={"host": "127.0.0.1"})
    assert settings.api.control_token.get_secret_value() == ""


def test_production_rejects_the_simulated_feed(tmp_path: Path) -> None:
    """§86: the station must not broadcast commentary driven by fake data."""
    with pytest.raises(ConfigurationError, match="simulated"):
        make_settings(
            tmp_path,
            mode_override=RunMode.PRODUCTION,
            market={"feed": "simulated"},
            generation={"provider": "ace_step"},
            api={"control_token": "x" * 48},
            obs={"enabled": False},
        )


def test_production_rejects_the_mock_provider(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="mock"):
        make_settings(
            tmp_path,
            mode_override=RunMode.PRODUCTION,
            market={"feed": "metatrader5"},
            generation={"provider": "mock"},
            api={"control_token": "x" * 48},
            obs={"enabled": False},
        )


def test_production_requires_an_obs_password_when_obs_is_enabled(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="obs.password"):
        make_settings(
            tmp_path,
            mode_override=RunMode.PRODUCTION,
            market={"feed": "metatrader5"},
            generation={"provider": "ace_step"},
            api={"control_token": "x" * 48},
            obs={"enabled": True, "password": ""},
        )


def test_valid_production_configuration_loads(tmp_path: Path) -> None:
    """The positive case, so the guards above are not merely blocking everything."""
    settings = make_settings(
        tmp_path,
        mode_override=RunMode.PRODUCTION,
        market={"feed": "metatrader5"},
        generation={"provider": "ace_step"},
        api={"control_token": "x" * 48},
        obs={"enabled": True, "password": "obs-secret-value"},
        audio={"sink": "null_sink"},
    )
    assert settings.is_production
    assert not settings.uses_simulated_market


def test_concurrent_ace_step_jobs_are_rejected(tmp_path: Path) -> None:
    """One GPU cannot host two concurrent ACE-Step jobs without VRAM contention."""
    with pytest.raises(ConfigurationError, match="max_concurrent_jobs"):
        make_settings(
            tmp_path, generation={"provider": "ace_step", "max_concurrent_jobs": 2}
        )


# ---------------------------------------------------------------- secrets


def test_masked_dump_never_reveals_a_secret(tmp_path: Path) -> None:
    """§50: secrets must never display unmasked after save."""
    secret = "super-secret-control-token-value"
    settings = make_settings(
        tmp_path,
        api={"host": "127.0.0.1", "control_token": secret},
        obs={"password": "obs-password-here"},
        market={"rest_api_key": "rest-key-here"},
    )
    payload = settings.masked_dump()
    rendered = str(payload)
    assert secret not in rendered
    assert "obs-password-here" not in rendered
    assert "rest-key-here" not in rendered
    # It must still tell the operator the value is set.
    assert "set," in payload["api"]["control_token"]
    assert payload["api"]["control_token"].startswith("•")


def test_masked_dump_distinguishes_empty_from_set(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    payload = settings.masked_dump()
    assert payload["api"]["control_token"] == ""


def test_repr_of_settings_does_not_leak_secrets(tmp_path: Path) -> None:
    """A stray log or print of the settings object must be safe (§68)."""
    settings = make_settings(
        tmp_path, api={"host": "127.0.0.1", "control_token": "leak-me-if-you-can"}
    )
    assert "leak-me-if-you-can" not in repr(settings)
    assert "leak-me-if-you-can" not in str(settings)


# ---------------------------------------------------------------- paths


def test_relative_paths_resolve_against_root_not_cwd(tmp_path: Path) -> None:
    """ADR-08: three processes may start from different working directories."""
    settings = make_settings(tmp_path)
    for directory in settings.paths.all_directories():
        assert directory.is_absolute()
        assert str(directory).startswith(str(tmp_path))


def test_absolute_paths_are_left_alone(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere" / "audio"
    settings = make_settings(tmp_path, paths={"generated_dir": str(elsewhere)})
    assert settings.paths.generated_dir == elsewhere


def test_sqlite_url_is_anchored_to_the_data_directory(tmp_path: Path) -> None:
    """A CWD-relative SQLite path would give each process its own database."""
    settings = make_settings(tmp_path)
    assert settings.database.url.startswith("sqlite+aiosqlite:///")
    assert settings.paths.data_dir.as_posix() in settings.database.url


def test_absolute_sqlite_url_is_preserved(tmp_path: Path) -> None:
    target = (tmp_path / "custom" / "station.db").as_posix()
    settings = make_settings(tmp_path, database={"url": f"sqlite+aiosqlite:///{target}"})
    assert settings.database.url == f"sqlite+aiosqlite:///{target}"


def test_in_memory_sqlite_url_is_preserved(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, database={"url": "sqlite+aiosqlite:///:memory:"})
    assert settings.database.url == "sqlite+aiosqlite:///:memory:"


def test_synchronous_database_driver_is_rejected(tmp_path: Path) -> None:
    """A sync driver would block the event loop on every query."""
    with pytest.raises(ConfigurationError, match="async drivers"):
        make_settings(tmp_path, database={"url": "sqlite:///data/x.db"})


def test_ensure_directories_creates_and_is_idempotent(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    created = ensure_directories(settings)
    assert len(created) == len(settings.paths.all_directories())
    assert all(d.is_dir() for d in settings.paths.all_directories())
    # Second call creates nothing.
    assert ensure_directories(settings) == []


def test_ensure_directories_rejects_a_file_where_a_directory_belongs(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.paths.generated_dir.parent.mkdir(parents=True, exist_ok=True)
    settings.paths.generated_dir.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="not a directory"):
        ensure_directories(settings)


# ---------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("FALSE", False),
        ("on", True),
        ("off", False),
        ("null", None),
        ("", None),
        ("42", 42),
        ("-7", -7),
        ("3.5", 3.5),
        ("XAUUSD", "XAUUSD"),
    ],
)
def test_scalar_coercion(raw: str, expected: object) -> None:
    assert _coerce_scalar(raw) == expected


def test_reserved_root_variable_is_not_treated_as_a_setting() -> None:
    """``TRADEFIX_ROOT`` locates the repository; it is not an AppSettings field.

    Nesting it would produce an unknown ``root`` key and reject the whole
    configuration — for anyone following the documented setup.
    """
    assert _nest({"TRADEFIX_ROOT": "D:/.Music"}) == {}


def test_root_env_var_still_relocates_the_tree(tmp_path: Path) -> None:
    settings = load_settings(
        env_file=None, environ={"TRADEFIX_ROOT": str(tmp_path)}, overrides={}
    )
    assert settings.paths.root_dir == tmp_path.resolve()
    assert settings.paths.generated_dir == tmp_path.resolve() / "generated"


def test_env_nesting_splits_on_double_underscore() -> None:
    """Single underscores inside a field name must survive."""
    nested = _nest(
        {
            "TRADEFIX_MARKET__POLL_INTERVAL_SECONDS": "2.5",
            "TRADEFIX_GENERATION__ACE_STEP__DIT_MODEL": "acestep-v15-base",
            "UNRELATED": "ignored",
        }
    )
    assert nested == {
        "market": {"poll_interval_seconds": 2.5},
        "generation": {"ace_step": {"dit_model": "acestep-v15-base"}},
    }


def test_comma_separated_env_value_becomes_a_list() -> None:
    nested = _nest({"TRADEFIX_MARKET__SYMBOL_ALIASES": "XAUUSD, GOLD , XAUUSD.m"})
    assert nested["market"]["symbol_aliases"] == ["XAUUSD", "GOLD", "XAUUSD.m"]


def test_dotenv_parsing(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "TRADEFIX_MODE=simulation",
                'TRADEFIX_OBS__PASSWORD="quoted value"',
                "export TRADEFIX_API__PORT=9000",
                "TRADEFIX_MARKET__SYMBOL='XAUUSD'",
            ]
        ),
        encoding="utf-8",
    )
    values = _load_dotenv(path)
    assert values["TRADEFIX_MODE"] == "simulation"
    assert values["TRADEFIX_OBS__PASSWORD"] == "quoted value"
    assert values["TRADEFIX_API__PORT"] == "9000"
    assert values["TRADEFIX_MARKET__SYMBOL"] == "XAUUSD"


def test_malformed_dotenv_line_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("THIS_IS_NOT_AN_ASSIGNMENT\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="KEY=VALUE"):
        _load_dotenv(path)


def test_absent_dotenv_is_not_an_error(tmp_path: Path) -> None:
    assert _load_dotenv(tmp_path / "missing.env") == {}


def test_loading_config_does_not_mutate_the_process_environment(tmp_path: Path) -> None:
    """``.env`` values are merged explicitly, never exported (§68)."""
    import os

    dotenv = tmp_path / ".env"
    dotenv.write_text("TRADEFIX_API__PORT=9123\n", encoding="utf-8")
    before = dict(os.environ)
    load_settings(
        env_file=dotenv,
        environ={},
        overrides={"paths": {"root_dir": str(tmp_path)}},
    )
    assert dict(os.environ) == before


# ---------------------------------------------------------------- derived helpers


def test_energy_weights_normalise_to_one() -> None:
    weights = EnergyWeights()
    assert sum(weights.normalised().values()) == pytest.approx(1.0)


def test_energy_weights_reject_all_zero() -> None:
    with pytest.raises(ValueError, match="must not all be zero"):
        EnergyWeights(
            atr_percentile=0.0,
            realized_volatility=0.0,
            price_velocity=0.0,
            trend_strength=0.0,
            volume_percentile=0.0,
            breakout_strength=0.0,
            range_expansion=0.0,
            momentum=0.0,
        )


def test_bpm_band_rejects_inverted_range() -> None:
    with pytest.raises(ValueError, match="exceeds high"):
        BpmBand(low=150, high=100)


@pytest.mark.parametrize(
    ("energy", "expected_low"),
    [(0.0, 72), (12.0, 72), (25.0, 86), (44.9, 86), (45.0, 98), (70.0, 118), (95.0, 134)],
)
def test_bpm_band_selection_is_a_step_function(
    tmp_path: Path, energy: float, expected_low: int
) -> None:
    """§1's energy-to-BPM mapping, resolved from config rather than if/else."""
    settings = make_settings(tmp_path)
    assert settings.music.bpm_band_for(energy).low == expected_low


@pytest.mark.parametrize("energy", [-50.0, 0.0, 50.0, 100.0, 150.0])
def test_bpm_band_selection_clamps_out_of_range_energy(tmp_path: Path, energy: float) -> None:
    """Energy is always 0-100 by contract, but the helper must not explode anyway."""
    settings = make_settings(tmp_path)
    band = settings.music.bpm_band_for(energy)
    assert 40 <= band.low <= band.high <= 220


def test_vocal_probability_rises_with_energy(tmp_path: Path) -> None:
    """§1: "stronger vocal probability" at breakout energy."""
    settings = make_settings(tmp_path)
    quiet = settings.music.vocal_probability_for(10.0)
    loud = settings.music.vocal_probability_for(90.0)
    assert quiet < loud


def test_app_settings_can_be_constructed_directly_with_defaults() -> None:
    """Direct construction must still produce a valid object.

    Tests and the Generation Lab build settings without the loader, so the
    defaults alone have to satisfy every cross-section invariant.
    """
    settings = AppSettings()
    assert settings.mode is RunMode.DEVELOPMENT
    assert settings.radio.minimum_buffer_minutes < settings.radio.target_buffer_minutes
