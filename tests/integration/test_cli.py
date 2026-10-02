"""CLI integration (§78, §79, milestone 1.8).

``tradefix doctor`` is the component most likely to be run on a broken machine, so
its contract is tested directly: it must produce the §79 table, exit non-zero only
for *blocking* failures, and never leak a secret. An exit code that fails on an
optional warning would break any CI pipeline using doctor as a smoke test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tradefix_radio.cli.main import build_parser, main
from tests.conftest import make_settings


@pytest.fixture
def isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a throwaway tree so it cannot touch real station data."""
    monkeypatch.setenv("TRADEFIX_ROOT", str(tmp_path))
    monkeypatch.setenv("TRADEFIX_DATABASE__URL", f"sqlite+aiosqlite:///{(tmp_path / 'data' / 'cli.db').as_posix()}")
    monkeypatch.delenv("TRADEFIX_MODE", raising=False)
    return tmp_path


# ---------------------------------------------------------------- parser


def test_parser_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_parser_accepts_every_phase_one_command() -> None:
    parser = build_parser()
    for command in ("doctor", "init", "migrate", "config", "version"):
        args = parser.parse_args([command])
        assert args.command == command
        assert callable(args.handler)


def test_parser_rejects_an_invalid_mode() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--mode", "banana", "doctor"])


@pytest.mark.parametrize("mode", ["development", "simulation", "production"])
def test_parser_accepts_every_run_mode(mode: str) -> None:
    assert build_parser().parse_args(["--mode", mode, "doctor"]).mode == mode


# ---------------------------------------------------------------- version


def test_version_command_reports_package_and_interpreter(
    capsys: pytest.CaptureFixture[str], isolated_root: Path
) -> None:
    assert main(["version"]) == 0
    output = capsys.readouterr().out
    assert "tradefix-radio" in output
    assert "3.10" in output


# ---------------------------------------------------------------- init


def test_init_creates_directories_and_applies_migrations(
    capsys: pytest.CaptureFixture[str], isolated_root: Path
) -> None:
    assert main(["init"]) == 0
    output = capsys.readouterr().out
    assert "Directory tree" in output
    assert "schema is up to date" in output

    settings = make_settings(isolated_root)
    for directory in settings.paths.all_directories():
        assert directory.is_dir(), f"{directory} was not created"
    assert (isolated_root / "data" / "cli.db").is_file()


def test_init_is_idempotent(
    capsys: pytest.CaptureFixture[str], isolated_root: Path
) -> None:
    assert main(["init"]) == 0
    capsys.readouterr()
    assert main(["init"]) == 0
    output = capsys.readouterr().out
    assert "created" not in output.split("Applying migrations")[0]


# ---------------------------------------------------------------- doctor


def test_doctor_fails_before_init_and_names_the_blocker(
    capsys: pytest.CaptureFixture[str], isolated_root: Path
) -> None:
    """§73: a missing dependency must be reported at startup, not on air."""
    assert main(["doctor"]) == 1
    output = capsys.readouterr().out
    assert "Blocking failures" in output
    assert "directories" in output or "database" in output
    assert "tradefix init" in output


def test_doctor_passes_after_init(
    capsys: pytest.CaptureFixture[str], isolated_root: Path
) -> None:
    assert main(["init"]) == 0
    capsys.readouterr()
    assert main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "Overall:" in output
    assert "Blocking failures" not in output


def test_doctor_renders_the_brief_s_table_labels(
    capsys: pytest.CaptureFixture[str], isolated_root: Path
) -> None:
    """§79 names these rows explicitly."""
    main(["init"])
    capsys.readouterr()
    main(["doctor"])
    output = capsys.readouterr().out
    for label in (
        "Python",
        "Node",
        "FFmpeg",
        "Database",
        "GPU",
        "Market Feed",
        "OBS WebSocket",
        "Audio Device",
        "Disk Space",
    ):
        assert label in output, f"doctor output is missing the {label!r} row"


def test_doctor_json_output_is_machine_readable(
    capsys: pytest.CaptureFixture[str], isolated_root: Path
) -> None:
    main(["init"])
    capsys.readouterr()
    assert main(["doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "status" in payload
    assert "components" in payload
    names = {component["name"] for component in payload["components"]}
    assert {"python", "database", "disk_space"} <= names


def test_doctor_exit_code_tolerates_optional_warnings(
    capsys: pytest.CaptureFixture[str], isolated_root: Path
) -> None:
    """A warning on an optional dependency must not break a CI smoke test.

    This machine has no ACE-Step toolchain (expected, per the Phase 0 audit), so
    doctor legitimately warns while still exiting zero.
    """
    main(["init"])
    capsys.readouterr()
    code = main(["doctor"])
    output = capsys.readouterr().out
    assert code == 0
    assert "optional" in output


def test_doctor_provides_remediation_for_every_problem(
    capsys: pytest.CaptureFixture[str], isolated_root: Path
) -> None:
    """§79: "With useful remediation when something fails"."""
    assert main(["doctor"]) == 1
    output = capsys.readouterr().out
    assert "Remediation" in output
    remediation_block = output.split("Remediation", 1)[1]
    assert len(remediation_block.strip()) > 40


# ---------------------------------------------------------------- config


def test_config_command_emits_valid_json(
    capsys: pytest.CaptureFixture[str], isolated_root: Path
) -> None:
    assert main(["config"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "development"
    assert payload["generation"]["provider"] == "mock"


def test_config_command_masks_secrets(
    capsys: pytest.CaptureFixture[str],
    isolated_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§50/§68: a secret must never be printed, even by an explicit dump."""
    monkeypatch.setenv("TRADEFIX_OBS__PASSWORD", "do-not-print-this-value")
    assert main(["config"]) == 0
    output = capsys.readouterr().out
    assert "do-not-print-this-value" not in output
    payload = json.loads(output)
    assert "set," in payload["obs"]["password"]


def test_config_section_filter(
    capsys: pytest.CaptureFixture[str], isolated_root: Path
) -> None:
    assert main(["config", "--section", "radio"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"radio"}


def test_config_unknown_section_lists_the_valid_ones(
    capsys: pytest.CaptureFixture[str], isolated_root: Path
) -> None:
    assert main(["config", "--section", "nope"]) == 1
    error = capsys.readouterr().err
    assert "unknown section" in error
    assert "radio" in error


def test_config_reports_a_broken_configuration_clearly(
    capsys: pytest.CaptureFixture[str],
    isolated_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfigured station must fail with a readable message, not a traceback."""
    monkeypatch.setenv("TRADEFIX_API__PORT", "999999")
    assert main(["config"]) == 2
    error = capsys.readouterr().err
    assert "CONFIGURATION ERROR" in error
    assert "api.port" in error


# ---------------------------------------------------------------- migrate


def test_migrate_current_reports_the_applied_revision(
    capsys: pytest.CaptureFixture[str], isolated_root: Path
) -> None:
    assert main(["init"]) == 0
    assert main(["migrate", "current"]) == 0


def test_migrate_down_requires_a_revision(
    capsys: pytest.CaptureFixture[str], isolated_root: Path
) -> None:
    assert main(["init"]) == 0
    assert main(["migrate", "down"]) == 1
    assert "--revision" in capsys.readouterr().err


def test_migrate_history_runs(isolated_root: Path) -> None:
    assert main(["init"]) == 0
    assert main(["migrate", "history"]) == 0
