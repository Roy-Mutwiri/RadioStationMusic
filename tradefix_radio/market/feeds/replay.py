"""Replay feed — scenario 15, "prerecorded historical feed playback" (§7).

Reads a recorded series from CSV and replays it through the normal pipeline. Its
value is reproducibility: an endurance finding or a regime misclassification can be
replayed against the exact data that produced it, which synthetic scenarios cannot
do because they are parameterised rather than literal.

Format: a header row, then one row per observation. Recognised columns (case
insensitive, order irrelevant):

```
timestamp,bid,ask,open,high,low,close,tick_volume
```

``timestamp`` accepts ISO-8601 or a Unix epoch. ``bid``/``ask`` may be omitted if
``close`` is present, in which case a symmetric spread is derived — and the derived
values are marked ``synthetic=True``, because a spread we invented is not an
observation.
"""

from __future__ import annotations

import asyncio
import csv
from datetime import datetime
from pathlib import Path

import structlog

from tradefix_radio.contracts.market import MarketSnapshotV1
from tradefix_radio.core.clock import UTC
from tradefix_radio.core.errors import MarketDataError
from tradefix_radio.market.feeds.base import FeedBase

_log = structlog.get_logger(__name__)

#: Spread applied when the recording has only a mid/close price.
DERIVED_SPREAD_FRACTION = 0.00005


def _parse_timestamp(raw: str) -> datetime:
    text = raw.strip()
    if not text:
        raise MarketDataError("replay row has an empty timestamp")
    try:
        # Epoch seconds, possibly fractional.
        return datetime.fromtimestamp(float(text), tz=UTC)
    except ValueError:
        pass
    normalised = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError as exc:
        raise MarketDataError(
            f"replay timestamp is neither epoch nor ISO-8601: {raw!r}"
        ) from exc
    # A recording without an offset is assumed UTC and said so in the log, rather
    # than silently adopting the machine's local zone — which would shift every
    # session boundary by the operator's offset.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_float(row: dict[str, str], key: str) -> float | None:
    raw = row.get(key)
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise MarketDataError(f"replay column {key!r} is not numeric: {raw!r}") from exc


class ReplayFeed(FeedBase):
    """Replays a recorded series, one observation per :meth:`poll`."""

    def __init__(self, path: Path, *, symbol: str = "XAUUSD", loop: bool = False) -> None:
        super().__init__(symbol)
        self._path = path
        self._loop = loop
        self._snapshots: list[MarketSnapshotV1] = []
        self._index = 0

    @property
    def name(self) -> str:
        return f"replay:{self._path.name}"

    @property
    def is_simulated(self) -> bool:
        """``True``: the data is real but the *timing* is not live.

        Marking a replay as live would let the §42 card and the AI DJ present a
        historical price as the current one, which §32 forbids.
        """
        return True

    @property
    def total_snapshots(self) -> int:
        return len(self._snapshots)

    @property
    def position(self) -> int:
        return self._index

    @property
    def exhausted(self) -> bool:
        return not self._loop and self._index >= len(self._snapshots)

    async def open(self) -> None:
        if self._is_open:
            return
        # Read off the event loop: a multi-megabyte recording would otherwise stall
        # the station for the duration of the parse.
        self._snapshots = await asyncio.to_thread(self._load)
        if not self._snapshots:
            raise MarketDataError(
                "replay file contains no usable rows", path=str(self._path)
            )
        self._index = 0
        self._is_open = True
        _log.info(
            "feed.replay_loaded",
            path=str(self._path),
            snapshots=len(self._snapshots),
            first=self._snapshots[0].timestamp.isoformat(),
            last=self._snapshots[-1].timestamp.isoformat(),
        )

    def _load(self) -> list[MarketSnapshotV1]:
        if not self._path.is_file():
            raise MarketDataError("replay file not found", path=str(self._path))
        snapshots: list[MarketSnapshotV1] = []
        with self._path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise MarketDataError("replay file has no header row", path=str(self._path))
            # Normalise header names once rather than per row.
            field_map = {name.strip().lower(): name for name in reader.fieldnames}
            if "timestamp" not in field_map:
                raise MarketDataError(
                    "replay file has no 'timestamp' column",
                    path=str(self._path),
                    columns=sorted(field_map),
                )
            for line_number, raw_row in enumerate(reader, start=2):
                row = {
                    key: (raw_row.get(original) or "")
                    for key, original in field_map.items()
                }
                try:
                    snapshots.append(self._row_to_snapshot(row))
                except MarketDataError as exc:
                    raise MarketDataError(
                        f"{self._path}:{line_number}: {exc.message}", path=str(self._path)
                    ) from exc
        return snapshots

    def _row_to_snapshot(self, row: dict[str, str]) -> MarketSnapshotV1:
        timestamp = _parse_timestamp(row["timestamp"])
        bid = _optional_float(row, "bid")
        ask = _optional_float(row, "ask")
        close = _optional_float(row, "close")

        if bid is None or ask is None:
            if close is None:
                raise MarketDataError("row has neither bid/ask nor close")
            half = close * DERIVED_SPREAD_FRACTION / 2.0
            bid, ask = close - half, close + half

        return MarketSnapshotV1(
            symbol=self._symbol,
            timestamp=timestamp,
            bid=bid,
            ask=ask,
            open=_optional_float(row, "open"),
            high=_optional_float(row, "high"),
            low=_optional_float(row, "low"),
            close=close,
            tick_volume=_optional_float(row, "tick_volume"),
            # Unconditionally synthetic. Even when every field came from a real
            # recording, the *timing* is not live, so presenting it as a current
            # price would be false (§32).
            synthetic=True,
        )

    async def close(self) -> None:
        self._is_open = False
        self._snapshots = []
        self._index = 0

    async def poll(self) -> MarketSnapshotV1 | None:
        """Return the next recorded snapshot, or ``None`` once exhausted.

        Exhaustion returns ``None`` rather than raising. The service then sees the
        data age grow and transitions to STALE and DISCONNECTED exactly as it would
        for a dead live feed — which makes the replay feed a usable way to test
        §63-E without unplugging anything.
        """
        if not self._is_open:
            return None
        if self._index >= len(self._snapshots):
            if not self._loop:
                return None
            self._index = 0
        snapshot = self._snapshots[self._index]
        self._index += 1
        return snapshot


__all__ = ["DERIVED_SPREAD_FRACTION", "ReplayFeed"]
