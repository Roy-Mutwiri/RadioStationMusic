"""Health and diagnostic contracts (§35, §49, §57, §73, §79).

These are cross-boundary: the same objects are rendered by ``tradefix doctor`` in
a terminal, returned by ``GET /health``, and pushed over the §38 WebSocket to the
§49 System page. Keeping one shape means the CLI and the UI can never disagree
about whether the station is healthy.

``remediation`` is a first-class field, not an afterthought. §79 asks for "useful
remediation when something fails", and a check that reports a problem without
saying what to do about it forces the operator to read source code at exactly the
moment they are least able to.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, computed_field, field_validator

from tradefix_radio.contracts.base import Contract
from tradefix_radio.contracts.enums import HealthStatus


class ComponentHealthV1(Contract):
    """Health of one named component or dependency."""

    name: str = Field(min_length=1, max_length=64)
    status: HealthStatus
    #: One-line summary. Shown verbatim in the §79 table's right column.
    detail: str = Field(default="", max_length=500)
    #: What to do about it. Empty when healthy.
    remediation: str = Field(default="", max_length=800)
    #: ``False`` for optional components (OBS, GPU in development). An unhealthy
    #: optional component degrades but never blocks startup (§73).
    required: bool = True
    latency_ms: float | None = Field(default=None, ge=0.0)
    checked_at: datetime
    #: Free-form measurements for the §49 System page (vram_free_bytes, disk_free_gb…).
    measurements: dict[str, float] = Field(default_factory=dict)

    @field_validator("checked_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("checked_at must be timezone-aware (UTC)")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_blocking(self) -> bool:
        """Whether this component's state should prevent the station starting."""
        return self.required and self.status is HealthStatus.CRITICAL


class SystemHealthV1(Contract):
    """Aggregate health across every registered component."""

    status: HealthStatus
    components: tuple[ComponentHealthV1, ...]
    checked_at: datetime
    #: Seconds since the process started. §49 displays uptime.
    uptime_seconds: float = Field(ge=0.0)

    @field_validator("checked_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("checked_at must be timezone-aware (UTC)")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def blocking_failures(self) -> tuple[str, ...]:
        """Names of required components in a CRITICAL state."""
        return tuple(c.name for c in self.components if c.is_blocking)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def can_start(self) -> bool:
        """§73: do not wait until live playback to discover a missing dependency."""
        return not self.blocking_failures

    def component(self, name: str) -> ComponentHealthV1 | None:
        for candidate in self.components:
            if candidate.name == name:
                return candidate
        return None


class ResourceSnapshotV1(Contract):
    """Host resource measurements for the §49 System page."""

    at: datetime

    cpu_percent: float = Field(ge=0.0, le=100.0)
    ram_used_bytes: int = Field(ge=0)
    ram_total_bytes: int = Field(gt=0)

    disk_free_bytes: int = Field(ge=0)
    disk_total_bytes: int = Field(gt=0)

    #: GPU fields are optional: the station runs without a GPU in development,
    #: and reporting zeros would misrepresent "absent" as "idle".
    gpu_name: str | None = Field(default=None, max_length=128)
    gpu_utilization_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    vram_used_bytes: int | None = Field(default=None, ge=0)
    vram_total_bytes: int | None = Field(default=None, gt=0)
    gpu_temperature_c: float | None = Field(default=None, ge=0.0, le=150.0)

    process_rss_bytes: int = Field(ge=0)
    open_file_handles: int | None = Field(default=None, ge=0)
    thread_count: int = Field(ge=1)

    @field_validator("at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("at must be timezone-aware (UTC)")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ram_percent(self) -> float:
        return 100.0 * self.ram_used_bytes / self.ram_total_bytes

    @computed_field  # type: ignore[prop-decorator]
    @property
    def disk_free_gb(self) -> float:
        return self.disk_free_bytes / 1_000_000_000

    @computed_field  # type: ignore[prop-decorator]
    @property
    def vram_free_bytes(self) -> int | None:
        if self.vram_total_bytes is None or self.vram_used_bytes is None:
            return None
        return max(0, self.vram_total_bytes - self.vram_used_bytes)


class AlertV1(Contract):
    """An active §57 alert condition."""

    key: str = Field(min_length=1, max_length=64)
    severity: HealthStatus
    message: str = Field(min_length=1, max_length=500)
    raised_at: datetime
    #: Where the operator should look. Mirrors ``remediation`` on health checks.
    remediation: str = Field(default="", max_length=800)

    @field_validator("raised_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("raised_at must be timezone-aware (UTC)")
        return value


__all__ = [
    "AlertV1",
    "ComponentHealthV1",
    "ResourceSnapshotV1",
    "SystemHealthV1",
]
