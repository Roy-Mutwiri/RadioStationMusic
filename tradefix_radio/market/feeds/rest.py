"""REST polling feed — the production alternative to MT5 (ADR-03).

For operators who do not run MetaTrader 5. Deliberately **provider-agnostic**:
rather than hard-coding Twelve Data or Polygon or Finnhub, the response shape is
described by a small field-path mapping in configuration. Hard-coding one provider
would make switching a code change, and these APIs change their response shapes more
often than our release cadence.

> **Verification status:** exercised in tests against a stubbed HTTP transport, not
> against a live provider (no API key is available in this environment — see
> `docs/INITIAL_AUDIT.md` ADR-03). The field mapping and error handling are tested;
> the specific JSON shape of any given provider is not, and must be confirmed when a
> key is supplied.

Secrets: the API key is read from :class:`~pydantic.SecretStr` and sent as a header.
It is never logged, never placed in a query string (where it would appear in proxy
and server access logs), and never included in an error message (§68).
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import httpx
import structlog

from tradefix_radio.config.schema import MarketSettings
from tradefix_radio.contracts.market import MarketSnapshotV1
from tradefix_radio.core.clock import UTC, Clock, SystemClock
from tradefix_radio.core.errors import MarketDataError
from tradefix_radio.market.feeds.base import FeedBase

_log = structlog.get_logger(__name__)

#: Dotted paths into the JSON response, per logical field. Overridable so a new
#: provider is a configuration change rather than a code change.
DEFAULT_FIELD_MAP: dict[str, str] = {
    "bid": "bid",
    "ask": "ask",
    "price": "price",
    "timestamp": "timestamp",
    "volume": "volume",
}

#: Consecutive failures after which the feed reports itself broken rather than
#: continuing to retry silently. The service then shows DISCONNECTED (§63-E).
FAILURE_THRESHOLD = 5


def _dig(payload: Any, path: str) -> Any:
    """Follow a dotted path into nested JSON, returning ``None`` if absent.

    Tolerant by design: a provider adding or renaming a field should degrade one
    value, not break the feed. The caller decides whether a missing value is fatal.
    """
    cursor = payload
    for part in path.split("."):
        if isinstance(cursor, dict):
            cursor = cursor.get(part)
        elif isinstance(cursor, list):
            try:
                cursor = cursor[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cursor is None:
            return None
    return cursor


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


class RestPollingFeed(FeedBase):
    """Polls an HTTP quote endpoint for the configured symbol."""

    def __init__(
        self,
        settings: MarketSettings,
        *,
        clock: Clock | None = None,
        client: httpx.AsyncClient | None = None,
        field_map: dict[str, str] | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        super().__init__(settings.symbol)
        if not settings.rest_base_url:
            raise MarketDataError("market.rest_base_url is required for the rest feed")
        self._settings = settings
        self._clock: Clock = clock or SystemClock()
        self._owns_client = client is None
        self._client = client
        self._field_map = {**DEFAULT_FIELD_MAP, **(field_map or {})}
        self._timeout = timeout_seconds
        self._consecutive_failures = 0
        self._last_timestamp: datetime | None = None

    @property
    def name(self) -> str:
        return f"rest:{self._settings.rest_base_url}"

    @property
    def is_simulated(self) -> bool:
        return False

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    async def open(self) -> None:
        if self._is_open:
            return
        if self._client is None:
            headers = {"Accept": "application/json"}
            key = self._settings.rest_api_key.get_secret_value()
            if key:
                # Header, not query string: query parameters are recorded verbatim in
                # proxy and server access logs (§68).
                headers["Authorization"] = f"Bearer {key}"
            self._client = httpx.AsyncClient(
                base_url=self._settings.rest_base_url,
                timeout=self._timeout,
                headers=headers,
            )
        self._is_open = True
        self._consecutive_failures = 0

    async def close(self) -> None:
        self._is_open = False
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def poll(self) -> MarketSnapshotV1 | None:
        """Fetch one quote.

        Transport and HTTP errors return ``None`` and increment a failure counter
        rather than raising, up to :data:`FAILURE_THRESHOLD`. A single timeout is
        normal internet behaviour and must not take the station off air; a sustained
        run of them is a real outage and is escalated.
        """
        if not self._is_open or self._client is None:
            return None
        try:
            response = await self._client.get(
                "", params={"symbol": self._settings.symbol}
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self._consecutive_failures += 1
            _log.warning(
                "feed.rest_poll_failed",
                # The URL is logged, never the Authorization header.
                url=self._settings.rest_base_url,
                error=type(exc).__name__,
                consecutive_failures=self._consecutive_failures,
            )
            if self._consecutive_failures >= FAILURE_THRESHOLD:
                raise MarketDataError(
                    f"REST feed failed {self._consecutive_failures} times consecutively",
                    url=self._settings.rest_base_url,
                ) from exc
            return None

        self._consecutive_failures = 0
        return self._to_snapshot(payload)

    def _to_snapshot(self, payload: Any) -> MarketSnapshotV1 | None:
        bid = _as_float(_dig(payload, self._field_map["bid"]))
        ask = _as_float(_dig(payload, self._field_map["ask"]))
        price = _as_float(_dig(payload, self._field_map["price"]))

        if bid is None or ask is None:
            if price is None or price <= 0:
                _log.warning("feed.rest_unusable_payload", keys=_describe(payload))
                return None
            # Many providers quote a single price. A symmetric nominal spread is
            # derived so the contract is satisfiable, and the snapshot is marked
            # synthetic because the spread is ours, not an observation (§86).
            half = price * 0.00005 / 2.0
            bid, ask = price - half, price + half
            synthetic = True
        else:
            synthetic = False

        if bid <= 0 or ask <= 0:
            return None

        timestamp = self._parse_timestamp(_dig(payload, self._field_map["timestamp"]))
        if self._last_timestamp is not None and timestamp <= self._last_timestamp:
            return None
        self._last_timestamp = timestamp

        return MarketSnapshotV1(
            symbol=self._settings.symbol,
            timestamp=timestamp,
            bid=bid,
            ask=max(ask, bid),
            tick_volume=_as_float(_dig(payload, self._field_map["volume"])),
            synthetic=synthetic,
        )

    def _parse_timestamp(self, raw: Any) -> datetime:
        """Interpret the provider's timestamp, falling back to arrival time.

        Falling back is correct rather than lazy: if a provider omits a timestamp the
        honest reading is "this arrived now", and the service's staleness tracking
        then behaves sensibly. Fabricating a past time would make fresh data look
        stale, or worse, stale data look fresh.
        """
        if raw is None:
            return self._clock.now()
        numeric = _as_float(raw)
        if numeric is not None and numeric > 0:
            # Heuristic: values beyond year-2286 seconds are milliseconds.
            seconds = numeric / 1000.0 if numeric > 1e11 else numeric
            try:
                return datetime.fromtimestamp(seconds, tz=UTC)
            except (OverflowError, OSError, ValueError):
                return self._clock.now()
        if isinstance(raw, str):
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return self._clock.now()
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        return self._clock.now()


def _describe(payload: Any) -> list[str]:
    """Top-level keys of a payload, for a diagnostic log that leaks no values."""
    if isinstance(payload, dict):
        return sorted(str(key) for key in payload)[:12]
    return [type(payload).__name__]


__all__ = ["DEFAULT_FIELD_MAP", "FAILURE_THRESHOLD", "RestPollingFeed"]
