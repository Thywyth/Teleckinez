"""JSON views of events, devices and commands for the web panel (web.md §2)."""

import dataclasses
from enum import Enum
from typing import Any

from teleckinez.core.events import Event
from teleckinez.devices.registry import Device, DeviceRegistry
from teleckinez.devices.twin import DigitalTwin
from teleckinez.dispatch.tracker import CommandRecord


def jsonable(value: Any) -> Any:
    """Dataclasses, enums and containers -> plain JSON types."""
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [jsonable(v) for v in value]
    return value


def event_to_dict(event: Event) -> dict[str, Any]:
    """{type, ts, data} as sent over /ws."""
    data = jsonable(event)
    data.pop("ts", None)
    return {"type": event.type, "ts": event.ts, "data": data}


def room_name(registry: DeviceRegistry, room: str, lang: str = "uk") -> str:
    names = registry.rooms[room].get(lang) or registry.rooms[room].all()
    return names[0].capitalize() if names else room


def device_to_dict(device: Device, twin: DigitalTwin, now: float, lang: str = "uk") -> dict:
    return {
        "id": device.id,
        "type": device.type.name,
        "room": device.room,
        "title": device.title(lang),
        "critical": device.critical,
        "capabilities": device.type.meta(),
        "actions": sorted({*device.type.actions, *device.type.aliases}),
        **twin.snapshot(now),
    }


def record_to_dict(record: CommandRecord) -> dict[str, Any]:
    return {
        "cmd_id": record.cmd_id,
        "session_id": record.session_id,
        "group_id": record.group_id,
        "device_id": record.device_id,
        "action": record.action,
        "params": record.params,
        "desired": record.desired,
        "status": record.status.value,
        "attempts": record.attempts,
        "error": record.error,
        "t_created": record.t_created,
        "t_published": record.t_published,
        "t_ack": record.t_ack,
        "t_finished": record.t_finished,
        "durations_ms": record.durations_ms(),
    }
