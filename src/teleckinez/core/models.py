"""Shared domain models used across modules (and carried by events)."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


@dataclass(frozen=True)
class Intent:
    """Parsed user request (docs/specs/nlu.md)."""

    kind: Literal["command", "query"]
    action: str | None = None  # turn_on, turn_off, toggle, open, close, set, step, lock, ...
    device_type: str | None = None  # light, curtains, ...
    device_name: str | None = None  # a concrete name from devices.yaml ("люстра")
    room: str | None = None
    group: str | None = None  # all_lights, ...
    params: dict[str, Any] = field(default_factory=dict)
    lang: str = "uk"
    confidence: float = 1.0
    raw_text: str = ""
    parser: str = "rule"  # "rule" | "llm"


@dataclass(frozen=True)
class DeviceCommand:
    """A user-level action addressed to one device, before it becomes a CommandRecord."""

    device_id: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)


class CommandStatus(StrEnum):
    """Command lifecycle states (docs/specs/dispatcher.md §3)."""

    CREATED = "created"
    PUBLISHED = "published"
    RETRYING = "retrying"
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    REJECTED = "rejected"
    REJECTED_LOCAL = "rejected_local"
    TIMEOUT_ACK = "timeout_ack"
    TIMEOUT_STATE = "timeout_state"
    DEVICE_OFFLINE = "device_offline"
    SUPERSEDED = "superseded"

    @property
    def is_terminal(self) -> bool:
        return self not in _ACTIVE

    @property
    def is_success(self) -> bool:
        return self is CommandStatus.COMPLETED


_ACTIVE = frozenset(
    {
        CommandStatus.CREATED,
        CommandStatus.PUBLISHED,
        CommandStatus.RETRYING,
        CommandStatus.ACCEPTED,
    }
)


class Availability(StrEnum):
    UNKNOWN = "unknown"
    ONLINE = "online"
    STALE = "stale"  # connected, but no heartbeat for > 3 x heartbeat_s
    OFFLINE = "offline"
