"""TargetResolver: Intent -> concrete devices and DeviceCommands (dispatcher.md §1.1).

Rules:
* room + type/name           -> devices of that type/name in the room (fan-out);
* type/name only, one device -> that device;
* type/name only, many rooms -> ambiguous_target with the list of rooms;
* group ("все світло")       -> all devices of the group (optionally in a room);
* room + verb only           -> the one device in the room that supports it
                                ("відкрий гараж" -> the gate);
* query                      -> devices to describe (several are fine).

Generic parameters from the parser ("percent", "degrees", "number", "step")
become the attribute the target device type actually has.
"""

from dataclasses import dataclass, field
from typing import Any

from teleckinez.core.models import DeviceCommand, Intent
from teleckinez.devices.registry import Device, DeviceRegistry
from teleckinez.devices.types import DeviceType

# Which attribute a generic value means, in order of preference.
PERCENT_ATTRS = ("brightness", "position", "volume")
NUMBER_ATTRS = ("speed", "level", "target_temp", "brightness", "position", "volume")
# Types that users call by the same words ("світло" may be an RGB lamp).
FAMILIES = ({"light", "rgb_light"},)
# A query for "temperature" may be answered by any of these.
QUERY_FAMILIES = {
    "target_temp": ("temperature", "current_temp", "target_temp", "water_temp"),
    "humidity": ("humidity",),
    "motion": ("motion",),
    "battery": ("battery",),
    "power_w": ("power_w",),
    "water_temp": ("water_temp",),
}


@dataclass
class Resolution:
    kind: str  # command | query
    commands: list[DeviceCommand] = field(default_factory=list)
    devices: list[Device] = field(default_factory=list)
    error: str | None = None  # unknown_device | ambiguous_target | not_in_room | unsupported_action
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None


class TargetResolver:
    def __init__(self, registry: DeviceRegistry) -> None:
        self.registry = registry

    def resolve(self, intent: Intent) -> Resolution:
        devices, error = self._candidates(intent)
        if error:
            return error
        if intent.kind == "query":
            return self._query(intent, devices)
        result = self._commands(intent, devices)
        if result.error == "unsupported_action" and intent.params:
            # "зроби світло у вітальні синім": the main light has no color, the floor lamp has.
            related = self._related(devices, intent.room)
            if related:
                retry = self._commands(intent, related)
                if retry.ok:
                    return retry
        return result

    def _related(self, devices: list[Device], room: str | None) -> list[Device]:
        types = {d.type.name for d in devices}
        family = set().union(*(f for f in FAMILIES if f & types)) - types
        rooms = {room} if room else {d.room for d in devices}
        return [d for d in self.registry if d.type.name in family and d.room in rooms]

    # ------------------------------------------------------------- candidates

    def _candidates(self, intent: Intent) -> tuple[list[Device], Resolution | None]:
        reg, room, kind = self.registry, intent.room, intent.kind
        fan_out = intent.group is not None  # "все світло", "всі штори": no ambiguity
        if intent.group and intent.group != "all":
            devices = reg.find(group=intent.group, room=room)
        elif intent.device_name:
            devices = reg.find(name=intent.device_name, room=room)
            if not devices and intent.device_type:
                devices = reg.find(type=intent.device_type, room=room)
            if not devices and room:
                elsewhere = reg.find(name=intent.device_name)
                if elsewhere:
                    return [], Resolution(
                        kind,
                        error="not_in_room",
                        details={
                            "room": room,
                            "rooms": _rooms(elsewhere),
                            "name": intent.device_name,
                        },
                    )
        elif intent.device_type:
            devices = reg.find(type=intent.device_type, room=room)
        elif room:
            devices = reg.find(room=room)
            fan_out = fan_out or kind == "query"
            if kind == "command":
                # "яскравість на кухні 40 %": only devices that have such an attribute
                devices = [d for d in devices if self._command_for(intent, d) is not None]
                if len(devices) > 1 and not fan_out:
                    return [], Resolution(
                        kind,
                        error="ambiguous_device",
                        details={"room": room, "devices": [d.id for d in devices]},
                    )
        elif kind == "query" and "attribute" in intent.params:
            family = QUERY_FAMILIES.get(intent.params["attribute"], (intent.params["attribute"],))
            devices = [d for d in reg if any(d.type.has(a) for a in family)]
            fan_out = True
        else:
            return [], Resolution(kind, error="unknown_device")

        if not devices:
            return [], Resolution(kind, error="unknown_device", details={"room": room})
        rooms = _rooms(devices)
        if len(rooms) > 1 and not fan_out and kind == "command":
            return [], Resolution(
                kind,
                error="ambiguous_target",
                details={"rooms": rooms, "devices": [d.id for d in devices]},
            )
        return devices, None

    # ---------------------------------------------------------------- queries

    def _query(self, intent: Intent, devices: list[Device]) -> Resolution:
        attribute = intent.params.get("attribute")
        if attribute:
            family = QUERY_FAMILIES.get(attribute, (attribute,))
            devices = [d for d in devices if any(d.type.has(a) for a in family)]
            if not devices:
                return Resolution(
                    "query", error="unsupported_action", details={"attribute": attribute}
                )
        return Resolution("query", devices=devices, details={"attribute": attribute})

    # --------------------------------------------------------------- commands

    def _commands(self, intent: Intent, devices: list[Device]) -> Resolution:
        commands, skipped = [], []
        for device in devices:
            command = self._command_for(intent, device)
            if command is None:
                skipped.append(device.id)
            else:
                commands.append(command)
        if not commands:
            return Resolution(
                "command",
                devices=devices,
                error="unsupported_action",
                details={"action": intent.action, "devices": skipped},
            )
        return Resolution(
            "command", commands=commands, devices=devices, details={"skipped": skipped}
        )

    def _command_for(self, intent: Intent, device: Device) -> DeviceCommand | None:
        dtype = device.type
        action = intent.action
        params = dict(intent.params)
        if action == "step":
            step = params.pop("step")
            attr = _pick(dtype, (step["attribute"], *dtype.steps)) if step["attribute"] else None
            attr = attr or next(iter(dtype.steps), None)
            if attr is None:
                return None
            amount = step["amount"] if step["amount"] is not None else dtype.steps.get(attr, 1)
            return DeviceCommand(device.id, "step", {attr: step["direction"] * amount})
        wanted = bool(params)
        params = _concrete(dtype, params)
        if wanted and not params:
            return None  # the device has none of the requested attributes
        if action in ("open", "close") and "position" in params:
            # "відкрий на 30 %" -> 30; "закрий наполовину" -> 50; "закрий на 30 %" -> 70
            pos = params["position"]
            return DeviceCommand(
                device.id, "set", {"position": pos if action == "open" else 100 - pos}
            )
        if action in (None, "set") and not params:
            action = dtype.default_action  # "штори вітальня", "постав чайник"
        elif action is None:
            action = "set"
        if action is None or not dtype.supports(action):
            return None
        return DeviceCommand(device.id, action, params if action == "set" else {})


def _rooms(devices: list[Device]) -> list[str]:
    return list(dict.fromkeys(d.room for d in devices))


def _pick(dtype: DeviceType, candidates) -> str | None:
    return next((a for a in candidates if a and dtype.has(a) and not dtype.cap(a).readonly), None)


def _concrete(dtype: DeviceType, params: dict[str, Any]) -> dict[str, Any]:
    """{"percent": 40} -> {"brightness": 40} for a light, {"position": 40} for curtains."""
    out: dict[str, Any] = {}
    for key, value in params.items():
        if key == "percent":
            attr = _pick(dtype, PERCENT_ATTRS)
        elif key == "degrees":
            attr = _pick(dtype, ("target_temp",))
        elif key == "number":
            attr = _pick(dtype, NUMBER_ATTRS)
        else:
            attr = key if dtype.has(key) else None
        if attr is not None:
            out[attr] = value
    return out
