"""Structured logging setup (§55).

Two sinks with different jobs:

* **Console** — human-readable, for an operator watching a terminal.
* **Rotating file** — newline-delimited JSON, for querying a week of unattended
  operation. §55 requires rotation; an autonomous station that fills its own disk
  with logs has failed in a particularly silly way.

Every record carries the §55 keys where applicable. The mechanism is
``structlog.contextvars``: a subsystem binds ``track_id`` / ``job_id`` /
``market_regime`` once, and every log line emitted inside that scope inherits
them without the call site repeating itself. That is what makes it possible to
grep one track's entire journey out of a week of logs.

A note on why ``logging.handlers.RotatingFileHandler`` and not a custom writer:
on Windows, rotation renames an open file, which fails if another process holds
it. The three station processes (ADR-08) therefore log to *separate files*
distinguished by ``service``, rather than contending for one.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import structlog
from structlog.typing import EventDict, WrappedLogger

from tradefix_radio.config.schema import LoggingSettings

#: Guard so repeated setup calls (tests, reloads) do not stack handlers.
_configured_for: str | None = None


def _add_service(service: str) -> Any:
    """Processor factory that stamps every record with the owning service."""

    def processor(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        return event_dict

    return processor


def _rename_event_key(
    _logger: WrappedLogger, _name: str, event_dict: EventDict
) -> EventDict:
    """Expose structlog's ``event`` under the §55 name, which is also ``event``.

    structlog already calls it ``event``; this processor exists to guarantee the
    key survives if the renderer's default changes, and to normalise the common
    mistake of passing ``event=`` explicitly as a keyword.
    """
    if "event" not in event_dict and "message" in event_dict:
        event_dict["event"] = event_dict.pop("message")
    return event_dict


def _drop_internal_keys(
    _logger: WrappedLogger, _name: str, event_dict: EventDict
) -> EventDict:
    """Strip stdlib bookkeeping that adds noise without information."""
    for key in ("_record", "_from_structlog"):
        event_dict.pop(key, None)
    return event_dict


def configure_logging(
    settings: LoggingSettings,
    *,
    service: str,
    log_dir: Path | None = None,
    force: bool = False,
) -> None:
    """Install the console and rotating-file handlers.

    Called exactly once per process, early — before any subsystem logs.

    ``service`` names the process (``api``, ``worker``, ``playout``, ``cli``) and
    becomes both a field on every record and the log filename, which is what keeps
    Windows rotation from fighting across processes.
    """
    global _configured_for
    if _configured_for == service and not force:
        return

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _add_service(service),
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        # Render exc_info into a formatted `exception` string *before* the JSON
        # renderer sees it. Without this the file record contains only
        # `"exc_info": true` and the traceback is lost — the console happens to
        # show it, but the JSONL file is the durable record for a week-long
        # unattended run, and a logged failure with no traceback is barely a log
        # at all (§55, §86).
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        _rename_event_key,
        _drop_internal_keys,
    ]

    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    # Remove our own previously installed handlers so a reconfigure (tests, a
    # mode switch) does not duplicate every line.
    for handler in list(root.handlers):
        if getattr(handler, "_tradefix", False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(settings.level)

    console_renderer: Any
    if settings.console_format == "json":
        console_renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        console_renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=console_renderer,
            foreign_pre_chain=shared,
        )
    )
    console._tradefix = True  # type: ignore[attr-defined]
    root.addHandler(console)

    if settings.file_enabled and log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            filename=log_dir / f"{service}.jsonl",
            maxBytes=settings.max_bytes,
            backupCount=settings.backup_count,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.processors.JSONRenderer(sort_keys=True),
                foreign_pre_chain=shared,
            )
        )
        file_handler._tradefix = True  # type: ignore[attr-defined]
        root.addHandler(file_handler)

    # Third-party loggers: keep their output but stop the noisiest from drowning
    # a week-long run. Levels rather than filters, so nothing is silently lost.
    for name, level in (
        ("uvicorn.access", logging.WARNING),
        ("sqlalchemy.engine", logging.WARNING),
        ("asyncio", logging.WARNING),
        ("httpx", logging.WARNING),
        ("httpcore", logging.WARNING),
        ("multipart", logging.WARNING),
    ):
        logging.getLogger(name).setLevel(level)

    _configured_for = service


@contextmanager
def log_context(**bindings: Any) -> Iterator[None]:
    """Bind §55 fields for the duration of a block.

    Usage::

        with log_context(track_id=track.id, market_regime=state.regime.value):
            await generate(...)

    Everything logged inside — including by code three layers down that knows
    nothing about tracks — carries those fields. The previous context is restored
    on exit even if the block raises.
    """
    tokens = structlog.contextvars.bind_contextvars(**bindings)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Module-level logger. Prefer ``get_logger(__name__)``."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def reset_logging() -> None:
    """Tear down handlers. Test affordance only."""
    global _configured_for
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_tradefix", False):
            root.removeHandler(handler)
            handler.close()
    structlog.contextvars.clear_contextvars()
    _configured_for = None


__all__ = ["configure_logging", "get_logger", "log_context", "reset_logging"]
