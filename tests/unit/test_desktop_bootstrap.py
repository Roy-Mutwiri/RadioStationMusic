"""The first-run installer behind the desktop exe: the parts that run without a network."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "desktop"))

from bootstrap import Installer, Layout


def _installer(root: Path) -> Installer:
    return Installer(
        Layout(root), say=lambda _m: None, log=lambda _m: None,
        progress=lambda _d, _t: None, bundled=None,
    )


def _github_zip(path: Path, top: str, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, content in files.items():
            z.writestr(f"{top}/{name}", content)
    return path


def test_a_github_archive_is_unpacked_without_its_top_folder(tmp_path: Path) -> None:
    archive = _github_zip(tmp_path / "src.zip", "RadioStationMusic-main",
                          {"tradefix_radio/__init__.py": "", "README.md": "hi"})
    target = tmp_path / "app"
    _installer(tmp_path)._extract_github_zip(archive, target, keep=())
    assert (target / "tradefix_radio" / "__init__.py").is_file()
    assert (target / "README.md").read_text() == "hi"
    assert not (target / "RadioStationMusic-main").exists()


def test_b_reinstall_keeps_the_environment_and_config_but_refreshes_source(tmp_path: Path) -> None:
    target = tmp_path / "app"
    (target / ".venv").mkdir(parents=True)
    (target / ".venv" / "marker").write_text("keep me")
    (target / ".env").write_text("TRADEFIX_MODE=development")
    (target / "README.md").write_text("old")
    archive = _github_zip(tmp_path / "src.zip", "RadioStationMusic-main",
                          {"README.md": "new", ".env": "overwritten?", ".venv/x": "no"})
    _installer(tmp_path)._extract_github_zip(archive, target, keep=(".venv", ".env"))
    assert (target / "README.md").read_text() == "new"
    assert (target / ".env").read_text() == "TRADEFIX_MODE=development"
    assert (target / ".venv" / "marker").read_text() == "keep me"


@pytest.mark.parametrize("missing", [None, "acestep-v15-turbo", "acestep-5Hz-lm-1.7B", "vae"])
def test_c_models_count_as_present_only_when_every_folder_has_weights(
    tmp_path: Path, missing: str | None
) -> None:
    checkpoints = tmp_path / "checkpoints"
    for folder in ("acestep-v15-turbo", "acestep-5Hz-lm-1.7B", "vae", "Qwen3-Embedding-0.6B"):
        (checkpoints / folder).mkdir(parents=True)
        if folder != missing:
            (checkpoints / folder / "model.safetensors").write_bytes(b"\0")
    assert Installer._models_present(checkpoints) is (missing is None)


def test_d_a_partial_download_left_behind_means_not_present(tmp_path: Path) -> None:
    checkpoints = tmp_path / "checkpoints"
    for folder in ("acestep-v15-turbo", "acestep-5Hz-lm-1.7B", "vae", "Qwen3-Embedding-0.6B"):
        (checkpoints / folder).mkdir(parents=True)
        (checkpoints / folder / "model.safetensors").write_bytes(b"\0")
    assert Installer._models_present(checkpoints) is True
    partial = checkpoints / ".cache" / "huggingface" / "download" / "x.incomplete"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"\0")
    assert Installer._models_present(checkpoints) is False


def test_e_steps_already_marked_done_are_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installer = _installer(tmp_path)
    ran: list[str] = []
    monkeypatch.setattr(installer, "_detect_gpu", lambda: None)  # no ACE-Step steps
    for name in ("_step_uv", "_step_source", "_step_frontend", "_step_python_env",
                 "_step_ffmpeg", "_step_config", "_step_database"):
        monkeypatch.setattr(installer, name, lambda n=name: ran.append(n))
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "uv.done").write_text("x")
    (tmp_path / "state" / "ffmpeg.done").write_text("x")
    installer.run()
    assert "_step_uv" not in ran and "_step_ffmpeg" not in ran
    assert ran == ["_step_source", "_step_frontend", "_step_python_env", "_step_config", "_step_database"]
    assert (tmp_path / "state" / "config.done").is_file()
