"""Normalized messages from devices, independent of the firmware's topic format."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StateReport:
    device_id: str
    state: dict[str, Any]
    ts: float | None = None
    seq: int | None = None


@dataclass(frozen=True)
class AckReport:
    device_id: str
    cmd_id: str
    status: str  # accepted | rejected | done
    error: str | None = None
    ts: float | None = None


@dataclass(frozen=True)
class AvailabilityReport:
    device_id: str
    online: bool


@dataclass(frozen=True)
class MetaReport:
    device_id: str
    meta: dict[str, Any] = field(default_factory=dict)


DeviceMessage = StateReport | AckReport | AvailabilityReport | MetaReport
