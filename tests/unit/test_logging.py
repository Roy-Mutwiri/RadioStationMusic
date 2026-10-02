"""Structured logging (§55, milestone 1.4).

§55 lists the fields a log event should carry. The mechanism that makes those
fields appear without every call site repeating them is bound context, and the
property that matters for a week-long unattended run is that **rotation actually
happens** — an autonomous station that fills its own disk with logs has failed in
a particularly silly way.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import structlog

from tradefix_radio.config.schema import LoggingSettings
from tradefix_radio.monitoring.logging import (
    configure_logging,
    get_logger,
    log_context,
    reset_logging,
)


def read_records(path: Path) -> list[dict[str, object]]:
    """Parse the JSONL log file into records."""
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "logs"
    directory.mkdir()
    return directory


def setup(log_dir: Path, **overrides: object) -> Path:
    settings = LoggingSettings.model_validate({"level": "DEBUG", **overrides})
    configure_logging(settings, service="test", log_dir=log_dir, force=True)
    return log_dir / "test.jsonl"


# ---------------------------------------------------------------- shape


def test_record_contains_the_required_fields(log_dir: Path) -> None:
    """§55: timestamp, service, event, severity at minimum."""
    path = setup(log_dir)
    get_logger("x").info("track.generated")
    logging.shutdown()

    records = read_records(path)
    assert records, "no record was written"
    record = records[-1]
    assert record["event"] == "track.generated"
    assert record["service"] == "test"
    assert record["level"] == "info"
    assert "timestamp" in record
    # ISO-8601 UTC, so a week of logs sorts lexicographically.
    assert str(record["timestamp"]).endswith("Z") or "+00:00" in str(record["timestamp"])


def test_explicit_keyword_fields_are_preserved(log_dir: Path) -> None:
    path = setup(log_dir)
    get_logger("x").warning(
        "generator.failed",
        track_id="TF-20261002-00007",
        generation_job_id="job-42",
        market_regime="bullish_breakout",
        duration=12.5,
        error="timeout",
    )
    logging.shutdown()

    record = read_records(path)[-1]
    assert record["track_id"] == "TF-20261002-00007"
    assert record["generation_job_id"] == "job-42"
    assert record["market_regime"] == "bullish_breakout"
    assert record["duration"] == 12.5
    assert record["error"] == "timeout"


def test_bound_context_propagates_to_nested_calls(log_dir: Path) -> None:
    """The point of §55's fields: grep one track's whole journey out of a week."""
    path = setup(log_dir)
    logger = get_logger("x")

    with log_context(track_id="TF-1", market_regime="quiet"):
        logger.info("track.planned")
        _deep_helper()

    logging.shutdown()
    records = read_records(path)
    assert len(records) >= 2
    for record in records:
        assert record["track_id"] == "TF-1"
        assert record["market_regime"] == "quiet"


def test_context_is_restored_after_the_block(log_dir: Path) -> None:
    path = setup(log_dir)
    logger = get_logger("x")
    with log_context(track_id="TF-1"):
        logger.info("inside")
    logger.info("outside")
    logging.shutdown()

    records = read_records(path)
    inside = next(r for r in records if r["event"] == "inside")
    outside = next(r for r in records if r["event"] == "outside")
    assert inside["track_id"] == "TF-1"
    assert "track_id" not in outside


def test_context_is_restored_even_when_the_block_raises(log_dir: Path) -> None:
    """A leaked context would mislabel every later record in the process."""
    path = setup(log_dir)
    logger = get_logger("x")
    with pytest.raises(RuntimeError), log_context(track_id="TF-LEAK"):
        raise RuntimeError("boom")
    logger.info("after")
    logging.shutdown()

    record = next(r for r in read_records(path) if r["event"] == "after")
    assert "track_id" not in record


def test_nested_contexts_merge_and_unwind(log_dir: Path) -> None:
    path = setup(log_dir)
    logger = get_logger("x")
    with log_context(track_id="TF-1"):
        with log_context(generation_job_id="job-1"):
            logger.info("both")
        logger.info("outer_only")
    logging.shutdown()

    records = {r["event"]: r for r in read_records(path)}
    assert records["both"]["track_id"] == "TF-1"
    assert records["both"]["generation_job_id"] == "job-1"
    assert records["outer_only"]["track_id"] == "TF-1"
    assert "generation_job_id" not in records["outer_only"]


def test_exceptions_are_logged_with_a_traceback(log_dir: Path) -> None:
    """§86 forbids suppressing errors; the traceback must reach the log."""
    path = setup(log_dir)
    logger = get_logger("x")
    try:
        raise ValueError("deliberate")
    except ValueError:
        logger.exception("provider.crashed")
    logging.shutdown()

    record = read_records(path)[-1]
    assert record["event"] == "provider.crashed"
    assert "ValueError" in str(record.get("exception", ""))
    assert "deliberate" in str(record.get("exception", ""))


# ---------------------------------------------------------------- levels


def test_level_filtering_is_applied(log_dir: Path) -> None:
    path = setup(log_dir, level="WARNING")
    logger = get_logger("x")
    logger.debug("debug.event")
    logger.info("info.event")
    logger.warning("warning.event")
    logger.error("error.event")
    logging.shutdown()

    events = {r["event"] for r in read_records(path)}
    assert "debug.event" not in events
    assert "info.event" not in events
    assert "warning.event" in events
    assert "error.event" in events


def test_file_output_is_always_json_even_in_console_mode(log_dir: Path) -> None:
    """The file must stay machine-queryable regardless of console preference."""
    path = setup(log_dir, console_format="console")
    get_logger("x").info("some.event", extra_field=7)
    logging.shutdown()

    record = read_records(path)[-1]
    assert record["extra_field"] == 7


# ---------------------------------------------------------------- rotation


def test_rotation_caps_total_log_size(log_dir: Path) -> None:
    """§55: logs must rotate and not fill the disk."""
    setup(log_dir, max_bytes=4_096, backup_count=2)
    logger = get_logger("x")
    for index in range(600):
        logger.info("noisy.event", index=index, payload="y" * 200)
    logging.shutdown()

    files = sorted(log_dir.glob("test.jsonl*"))
    assert len(files) > 1, "rotation did not occur"
    # Current file plus at most backup_count archives.
    assert len(files) <= 3
    total = sum(f.stat().st_size for f in files)
    # Generous ceiling, but far below the ~130 KB written without rotation.
    assert total < 60_000, f"log files grew to {total} bytes despite rotation"


def test_file_logging_can_be_disabled(log_dir: Path) -> None:
    path = setup(log_dir, file_enabled=False)
    get_logger("x").info("nothing.to.disk")
    logging.shutdown()
    assert not path.exists()


# ---------------------------------------------------------------- lifecycle


def test_repeated_configuration_does_not_duplicate_records(log_dir: Path) -> None:
    """Stacked handlers would double every line and double the disk cost."""
    # Configured directly rather than through `setup`, because the point is to call
    # configure_logging repeatedly with force=True.
    settings = LoggingSettings.model_validate({"level": "INFO"})
    for _ in range(4):
        configure_logging(settings, service="test", log_dir=log_dir, force=True)
    get_logger("x").info("once.only")
    logging.shutdown()

    records = [r for r in read_records(log_dir / "test.jsonl") if r["event"] == "once.only"]
    assert len(records) == 1


def test_configuration_is_skipped_when_already_set_up_for_a_service(log_dir: Path) -> None:
    settings = LoggingSettings.model_validate({"level": "INFO"})
    configure_logging(settings, service="svc", log_dir=log_dir)
    handler_count = len(logging.getLogger().handlers)
    configure_logging(settings, service="svc", log_dir=log_dir)
    assert len(logging.getLogger().handlers) == handler_count


def test_each_service_writes_its_own_file(log_dir: Path) -> None:
    """ADR-08: three processes must not contend for one file on Windows."""
    settings = LoggingSettings.model_validate({"level": "INFO"})
    configure_logging(settings, service="worker", log_dir=log_dir, force=True)
    get_logger("x").info("worker.event")
    logging.shutdown()
    reset_logging()
    configure_logging(settings, service="playout", log_dir=log_dir, force=True)
    get_logger("x").info("playout.event")
    logging.shutdown()

    assert (log_dir / "worker.jsonl").exists()
    assert (log_dir / "playout.jsonl").exists()
    worker_events = {r["event"] for r in read_records(log_dir / "worker.jsonl")}
    assert worker_events == {"worker.event"}


def test_reset_clears_handlers_and_context(log_dir: Path) -> None:
    setup(log_dir)
    structlog.contextvars.bind_contextvars(track_id="TF-X")
    reset_logging()
    assert not [h for h in logging.getLogger().handlers if getattr(h, "_tradefix", False)]
    assert structlog.contextvars.get_contextvars() == {}


def test_noisy_third_party_loggers_are_turned_down(log_dir: Path) -> None:
    """A week of uvicorn access logs would drown the station's own events."""
    setup(log_dir)
    assert logging.getLogger("sqlalchemy.engine").level >= logging.WARNING
    assert logging.getLogger("uvicorn.access").level >= logging.WARNING


def _deep_helper() -> None:
    """Code three layers down that knows nothing about tracks."""
    get_logger("deep").debug("deep.operation")
