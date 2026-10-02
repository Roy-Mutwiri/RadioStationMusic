"""Configuration loading and layering (§71).

Precedence, lowest to highest::

    schema defaults
      -> config/defaults.yaml            (shipped baseline)
      -> config/<mode>.yaml              (mode overlay, optional)
      -> <path given to load_settings>   (operator file, optional)
      -> .env                            (local secrets)
      -> process environment             (TRADEFIX_*)

Why layer rather than require one complete file: an operator should be able to
change ``radio.target_buffer_minutes`` without copying 300 lines of defaults and
then silently diverging from them on the next upgrade. Mode overlays exist so
``development`` can default to mock-everything while ``production`` cannot
(enforced in :class:`AppSettings`).

Environment variables use ``TRADEFIX_`` with ``__`` for nesting, e.g.
``TRADEFIX_RADIO__TARGET_BUFFER_MINUTES=60``. Single underscores inside a field
name are unambiguous because the delimiter is doubled.

Failure mode is deliberately loud: :class:`ConfigurationError` carries the
pydantic field path for every problem at once, rather than stopping at the first.
An operator fixing config at 2 a.m. should get the whole list.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from tradefix_radio.config.schema import AppSettings
from tradefix_radio.contracts.enums import RunMode
from tradefix_radio.core.errors import ConfigurationError

ENV_PREFIX = "TRADEFIX_"
ENV_NESTED_DELIMITER = "__"

#: Environment variables that share the prefix but are NOT settings fields.
#:
#: ``TRADEFIX_ROOT`` is read directly by
#: :func:`~tradefix_radio.config.schema._default_root` to locate the repository
#: before any settings exist. Without this exclusion it would be nested into a
#: ``root`` key and rejected as an unknown field — breaking configuration loading
#: entirely for anyone who sets the variable the docs tell them to set.
RESERVED_ENV_VARS = frozenset({"TRADEFIX_ROOT"})

#: Directory holding the shipped YAML baselines.
CONFIG_DIR = Path(__file__).resolve().parent


def _read_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML mapping, tolerating an absent or empty file."""
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            f"{path} is not valid YAML", path=str(path), detail=str(exc)
        ) from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigurationError(
            f"{path} must contain a mapping at the top level, got {type(raw).__name__}",
            path=str(path),
        )
    return raw


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` into ``base``, returning a new dict.

    Mappings merge; everything else (including lists) replaces. Replacing lists
    is the right call for this config: a partial merge of ``symbol_aliases`` or
    ``cors_origins`` would produce a surprising union that no operator intended.
    """
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _load_dotenv(path: Path) -> dict[str, str]:
    """Minimal ``.env`` reader.

    Hand-rolled rather than pulled from a library because the needed behaviour is
    tiny and the semantics must be predictable: ``KEY=VALUE``, ``#`` comments,
    optional surrounding quotes, no interpolation. Values are *not* exported to
    ``os.environ`` — they are merged explicitly, so loading config never mutates
    process state behind the caller's back.
    """
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            raise ConfigurationError(
                f"{path}:{lineno} is not a KEY=VALUE assignment", line=raw_line.strip()
            )
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _coerce_scalar(value: str) -> Any:
    """Interpret an environment string as the most plausible scalar.

    Needed because environment variables are always strings, while the schema has
    bools, ints, floats and tuples. Comma handling only applies when the field is
    a sequence — pydantic decides that — so we hand through a list and let
    validation reject it if the field is scalar. That keeps a value like
    ``"a,b"`` for a genuinely comma-containing string field from being mangled
    only when the field is a sequence.
    """
    lowered = value.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none", ""}:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _nest(flat: dict[str, str]) -> dict[str, Any]:
    """Turn ``TRADEFIX_A__B=1`` into ``{"a": {"b": 1}}``."""
    nested: dict[str, Any] = {}
    for raw_key, raw_value in flat.items():
        if not raw_key.startswith(ENV_PREFIX) or raw_key in RESERVED_ENV_VARS:
            continue
        path = raw_key[len(ENV_PREFIX) :].lower().split(ENV_NESTED_DELIMITER)
        if not path or not path[0]:
            continue
        cursor = nested
        for part in path[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        leaf = path[-1]
        value: Any = _coerce_scalar(raw_value)
        # Comma-separated strings become lists so tuple fields accept them.
        if isinstance(value, str) and "," in value:
            value = [part.strip() for part in value.split(",") if part.strip()]
        cursor[leaf] = value
    return nested


def _resolve_mode(layers: dict[str, Any], explicit: RunMode | None) -> RunMode:
    """Decide the run mode before loading its overlay.

    Mode has to be known early (to pick ``<mode>.yaml``) but is itself a setting,
    so it is resolved from the layers available at that point: an explicit
    argument wins, then the environment, then whatever the YAML said.
    """
    if explicit is not None:
        return explicit
    candidate = layers.get("mode")
    if candidate is None:
        return RunMode.DEVELOPMENT
    try:
        return RunMode(str(candidate))
    except ValueError as exc:
        valid = ", ".join(m.value for m in RunMode)
        raise ConfigurationError(
            f"mode {candidate!r} is not a valid run mode; expected one of: {valid}"
        ) from exc


def _format_validation_error(exc: ValidationError) -> str:
    """Render pydantic errors as a path-per-line operator-readable list."""
    lines: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


def load_settings(
    config_file: str | Path | None = None,
    *,
    mode: RunMode | None = None,
    env_file: str | Path | None = ".env",
    environ: dict[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> AppSettings:
    """Build and validate :class:`AppSettings` from all layers.

    Parameters
    ----------
    config_file:
        Optional operator YAML, layered above the shipped defaults and the mode
        overlay.
    mode:
        Forces the run mode, overriding YAML and environment. Used by the CLI's
        ``--mode`` flag and by tests.
    env_file:
        ``.env`` path, resolved relative to the repository root. Pass ``None`` to
        skip — tests do, so a developer's local ``.env`` cannot change results.
    environ:
        Environment mapping to read. Defaults to ``os.environ``. Injectable so
        tests need not mutate global state.
    overrides:
        Highest-precedence programmatic overlay, for tests and the Generation Lab.

    Raises
    ------
    ConfigurationError
        With every problem listed, not just the first.
    """
    env = dict(os.environ if environ is None else environ)

    layers: dict[str, Any] = {}
    layers = _deep_merge(layers, _read_yaml(CONFIG_DIR / "defaults.yaml"))

    # Pre-resolve mode from env/explicit so the right overlay is chosen.
    env_nested = _nest(env)

    # TRADEFIX_ROOT is reserved (see RESERVED_ENV_VARS) and read directly by
    # schema._default_root from os.environ. That bypasses the `environ` argument,
    # which exists precisely so callers and tests need not touch process state.
    # Translate it into an ordinary paths.root_dir value at the environment layer
    # so the injected mapping is honoured consistently.
    root_override = env.get("TRADEFIX_ROOT")
    if root_override:
        paths_layer = dict(env_nested.get("paths") or {})
        paths_layer.setdefault("root_dir", root_override)
        env_nested["paths"] = paths_layer

    provisional = _deep_merge(layers, env_nested)
    resolved_mode = _resolve_mode(provisional, mode)

    layers = _deep_merge(layers, _read_yaml(CONFIG_DIR / f"{resolved_mode.value}.yaml"))

    if config_file is not None:
        path = Path(config_file).expanduser()
        if not path.is_file():
            raise ConfigurationError(
                f"config file not found: {path}", path=str(path.resolve())
            )
        layers = _deep_merge(layers, _read_yaml(path))

    if env_file is not None:
        root = Path(env.get("TRADEFIX_ROOT") or Path(__file__).resolve().parent.parent.parent)
        dotenv_path = Path(env_file)
        if not dotenv_path.is_absolute():
            dotenv_path = root / dotenv_path
        layers = _deep_merge(layers, _nest(_load_dotenv(dotenv_path)))

    layers = _deep_merge(layers, env_nested)

    if overrides:
        layers = _deep_merge(layers, overrides)

    # The explicit argument must win over every layer.
    layers["mode"] = resolved_mode.value

    try:
        return AppSettings.model_validate(layers)
    except ValidationError as exc:
        raise ConfigurationError(
            "configuration failed validation:\n" + _format_validation_error(exc),
            error_count=exc.error_count(),
        ) from exc


def ensure_directories(settings: AppSettings) -> list[Path]:
    """Create every configured directory, returning those actually created.

    Part of the §73 startup check. Creation failure is reported as a
    :class:`ConfigurationError` naming the path, because "permission denied on
    D:/.Music/generated" is actionable while a bare ``OSError`` three layers
    down is not.
    """
    created: list[Path] = []
    for directory in settings.paths.all_directories():
        if directory.exists():
            if not directory.is_dir():
                raise ConfigurationError(
                    f"configured path exists but is not a directory: {directory}",
                    path=str(directory),
                )
            continue
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigurationError(
                f"cannot create directory {directory}: {exc.strerror or exc}",
                path=str(directory),
            ) from exc
        created.append(directory)
    return created


__all__ = [
    "CONFIG_DIR",
    "ENV_NESTED_DELIMITER",
    "ENV_PREFIX",
    "RESERVED_ENV_VARS",
    "ensure_directories",
    "load_settings",
]
