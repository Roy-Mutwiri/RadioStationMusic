"""Finding and reporting the ACE-Step installation (§7.28).

§7.28 is a UX requirement with a hard edge: *"Do not auto-download tens of GB silently.
Provide explicit setup command/instructions."* The checkpoints for the 2B tier are around
6.5 GB, and a station that quietly consumed that on first run — on a metered connection, or
a nearly-full disk — would be doing something the operator did not ask for.

So nothing here downloads anything. It inspects, reports precisely what is present and what
is not, and tells the operator the command to run. `tradefix doctor` reads this, and
`tradefix models install ace-step` is the explicit action.

Inspection is a *file-system* check rather than an API call, deliberately: the question
"is the model on this disk" has to be answerable when the service is not running, which is
exactly the situation an operator is in when they need the answer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

__all__ = [
    "CHECKPOINT_ENV_VAR",
    "InstallationReport",
    "ModelPresence",
    "inspect_installation",
]

#: Environment variable ACE-Step reads for its checkpoint directory, if set.
CHECKPOINT_ENV_VAR: Final = "ACESTEP_CHECKPOINT_DIR"

#: Rough on-disk size of the 2B tier, for the "this will download N GB" warning.
#:
#: Measured during the Phase 7 install: a 4.79 GB DiT, a 1.19 GB LM and a 337 MB VAE/decoder.
#: Approximate on purpose — it exists to set an expectation, and quoting it to three decimal
#: places would imply a precision that varies with the model pair chosen.
APPROX_DOWNLOAD_GB: Final = 6.5


@dataclass(frozen=True)
class ModelPresence:
    """Whether one named model is on disk, and where it was found."""

    name: str
    present: bool
    path: Path | None = None
    size_bytes: int = 0

    @property
    def size_gb(self) -> float:
        return self.size_bytes / 1e9


@dataclass(frozen=True)
class InstallationReport:
    """What `tradefix doctor` and `tradefix models status` both render."""

    #: Where ACE-Step itself is installed, if it could be found.
    install_dir: Path | None = None
    #: Whether that directory looks like a real ACE-Step checkout.
    install_valid: bool = False
    #: The Python environment `uv sync` created, if present.
    venv_python: Path | None = None
    checkpoint_dirs: tuple[Path, ...] = ()
    models: tuple[ModelPresence, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ready(self) -> bool:
        """Installed, synced, and every requested model on disk."""
        return (
            self.install_valid
            and self.venv_python is not None
            and bool(self.models)
            and all(model.present for model in self.models)
        )

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(model.name for model in self.models if not model.present)

    def summary(self) -> str:
        """One line for the doctor table."""
        if self.install_dir is None:
            return "ACE-Step is not installed"
        if not self.install_valid:
            return f"{self.install_dir} does not look like an ACE-Step checkout"
        if self.venv_python is None:
            return f"installed at {self.install_dir}, but dependencies are not synced"
        if self.missing:
            return (
                f"installed at {self.install_dir}; "
                f"{len(self.missing)} model(s) not downloaded: {', '.join(self.missing)}"
            )
        total = sum(model.size_gb for model in self.models)
        return f"installed at {self.install_dir}; {len(self.models)} model(s), {total:.1f} GB"

    def remediation(self) -> str:
        """Exactly what to run. Never a download performed on the operator's behalf."""
        if self.install_dir is None or not self.install_valid:
            return (
                "Install ACE-Step outside this repository, then point "
                "generation.ace_step.worker_directory at it:\n"
                "    git clone https://github.com/ACE-Step/ACE-Step-1.5.git D:\\ace-step\n"
                "    cd D:\\ace-step && uv sync\n"
                "ACE-Step requires Python 3.11-3.12; `uv` provisions it. This project stays "
                "on 3.10 (ADR-01), which is why the two cannot share an environment."
            )
        if self.venv_python is None:
            return f"Run `uv sync` in {self.install_dir} to create the environment."
        return (
            f"Run `tradefix models install ace-step` to download "
            f"{', '.join(self.missing)} (~{APPROX_DOWNLOAD_GB:.0f} GB). "
            "Nothing is downloaded automatically."
        )


def _candidate_install_dirs(configured: Path | None) -> list[Path]:
    """Where to look, most-specific first.

    The configured directory wins; the conventional siblings are a convenience so `doctor`
    can find a normal installation before the operator has configured anything, which is
    precisely when they most need the report.
    """
    candidates: list[Path] = []
    if configured is not None:
        candidates.append(Path(configured))
    env = os.environ.get("ACESTEP_HOME")
    if env:
        candidates.append(Path(env))
    candidates.extend(
        [
            Path("D:/ace-step"),
            Path("C:/ace-step"),
            Path.home() / "ace-step",
            Path.cwd().parent / "ace-step",
        ]
    )
    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.expanduser()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def _looks_like_acestep(path: Path) -> bool:
    """A checkout, not just a directory with the right name."""
    return (path / "pyproject.toml").is_file() and (path / "acestep").is_dir()


def _hf_cache_dirs() -> list[Path]:
    """Hugging Face cache locations, where `auto-download on first run` puts things."""
    dirs: list[Path] = []
    for variable in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_HOME"):
        value = os.environ.get(variable)
        if value:
            root = Path(value)
            dirs.append(root / "hub" if variable == "HF_HOME" else root)
    dirs.append(Path.home() / ".cache" / "huggingface" / "hub")
    return [path for path in dirs if path.is_dir()]


def _directory_size(path: Path, *, limit: int = 20_000) -> int:
    """Total bytes under ``path``, bounded by file count.

    Bounded because this runs inside `doctor`, which an operator expects to answer in
    seconds; a model cache with a hundred thousand blobs should not turn a health check into
    a disk scan. The number is for display, and an approximation is honest when the cap is
    documented.
    """
    total = 0
    seen = 0
    for entry in path.rglob("*"):
        if seen >= limit:
            break
        try:
            if entry.is_file():
                total += entry.stat().st_size
                seen += 1
        except OSError:
            continue
    return total


def _find_model(name: str, roots: list[Path]) -> ModelPresence:
    """Look for one model by name across the candidate roots.

    Matches on a normalised substring because the same checkpoint appears under several
    spellings — ``acestep-v15-turbo`` as a directory, ``models--ACE-Step--Ace-Step1.5`` in
    the HF cache. Reporting "missing" for a model that is present under a different name
    would send an operator to re-download six gigabytes they already have.
    """
    needle = name.lower().replace("_", "-")
    short = needle.replace("acestep-", "").replace("ace-step-", "")
    for root in roots:
        if not root.is_dir():
            continue
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            candidate = entry.name.lower().replace("_", "-").replace("--", "-")
            if needle in candidate or (len(short) > 4 and short in candidate):
                return ModelPresence(
                    name=name,
                    present=True,
                    path=entry,
                    size_bytes=_directory_size(entry),
                )
    return ModelPresence(name=name, present=False)


def inspect_installation(
    *,
    worker_directory: Path | None = None,
    dit_model: str = "acestep-v15-turbo",
    lm_model: str | None = "acestep-5Hz-lm-0.6B",
) -> InstallationReport:
    """Inspect the filesystem. Downloads nothing, starts nothing, needs no running service."""
    notes: list[str] = []

    install_dir: Path | None = None
    for candidate in _candidate_install_dirs(worker_directory):
        if candidate.is_dir():
            install_dir = candidate
            break

    if install_dir is None:
        return InstallationReport(notes=("no ACE-Step directory found",))

    valid = _looks_like_acestep(install_dir)
    venv_python: Path | None = None
    for relative in ("Scripts/python.exe", "bin/python"):
        candidate = install_dir / ".venv" / relative
        if candidate.is_file():
            venv_python = candidate
            break
    if valid and venv_python is None:
        notes.append("dependencies are not synced; run `uv sync`")

    checkpoint_dirs = [
        path
        for path in (
            Path(os.environ[CHECKPOINT_ENV_VAR])
            if CHECKPOINT_ENV_VAR in os.environ
            else None,
            install_dir / "checkpoints",
            install_dir / "acestep" / "models",
        )
        if path is not None and path.is_dir()
    ]
    search_roots = [*checkpoint_dirs, *_hf_cache_dirs()]

    wanted = [dit_model] + ([lm_model] if lm_model else [])
    models = tuple(_find_model(name, search_roots) for name in wanted)

    return InstallationReport(
        install_dir=install_dir,
        install_valid=valid,
        venv_python=venv_python,
        checkpoint_dirs=tuple(checkpoint_dirs),
        models=models,
        notes=tuple(notes),
    )
