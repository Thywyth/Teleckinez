"""Zigbee2MQTT adapter (protocol.md §4; zigbee2mqtt.io guide: MQTT topics, availability, exposes).

State is published as JSON on `{base}/{friendly_name}` after every change,
commands go to `{base}/{friendly_name}/set`, availability is the retained
`{base}/{friendly_name}/availability` = {"state": "online" | "offline"}.
There is no acknowledgement and no heartbeat: the first state message after
a command acknowledges it, and these devices are never marked `stale`.
"""

import json
import logging
from collections.abc import Iterable
from typing import Any

from teleckinez.devices.colors import color_to_xy, hex_to_rgb, nearest_color, xy_to_color
from teleckinez.devices.registry import Device
from teleckinez.transport.messages import AckReport, AvailabilityReport, DeviceMessage, StateReport

log = logging.getLogger(__name__)

SUPPORTED_TYPES = frozenset({"light", "rgb_light", "curtains", "door_lock"})
Z2M_MAX_BRIGHTNESS = 254


def device_address(device: Device) -> str:
    return device.config.address or device.id.replace(".", "_")


def to_z2m_brightness(percent: float) -> int:
    return round(percent * Z2M_MAX_BRIGHTNESS / 100)


def from_z2m_brightness(value: float) -> int:
    return max(1, min(100, round(value * 100 / Z2M_MAX_BRIGHTNESS)))


class Zigbee2MqttAdapter:
    name = "zigbee2mqtt"
    heartbeat = False  # devices report on change only; Z2M itself tracks availability

    def __init__(self, devices: Iterable[Device], base_topic: str = "zigbee2mqtt") -> None:
        self.base = base_topic.strip("/")
        self._by_name: dict[str, Device] = {}
        for device in devices:
            if device.type.name not in SUPPORTED_TYPES:
                raise ValueError(
                    f"{device.id}: Zigbee2MQTT adapter does not support {device.type.name}"
                )
            self._by_name[device_address(device)] = device
        self._name = {d.id: n for n, d in self._by_name.items()}
        self._pending: dict[str, str] = {}

    def subscriptions(self) -> list[str]:
        return [f"{self.base}/+", f"{self.base}/+/availability"]

    def encode_command(
        self, device_id: str, cmd_id: str, desired: dict[str, Any], ttl_s: float, ts: float
    ) -> tuple[str, bytes]:
        device = self._by_name[self._name[device_id]]
        payload: dict[str, Any] = {}
        if device.type.name == "door_lock":
            payload["state"] = "LOCK" if desired["locked"] else "UNLOCK"
        else:
            if "on" in desired:
                payload["state"] = "ON" if desired["on"] else "OFF"
            if "brightness" in desired:
                payload["brightness"] = to_z2m_brightness(desired["brightness"])
            if "color" in desired:
                payload["color"] = color_to_xy(desired["color"])
            if "position" in desired:
                payload["position"] = int(desired["position"])
        if not payload:
            raise ValueError(f"{device_id}: nothing Zigbee2MQTT can set in {desired}")
        self._pending[device_id] = cmd_id
        body = json.dumps(payload, separators=(",", ":")).encode()
        return f"{self.base}/{self._name[device_id]}/set", body

    def decode(self, topic: str, payload: bytes) -> list[DeviceMessage]:
        head = f"{self.base}/"
        if not topic.startswith(head) or not payload:
            return []
        rest = topic[len(head) :].split("/")
        device = self._by_name.get(rest[0])
        if device is None or len(rest) > 2 or (len(rest) == 2 and rest[1] != "availability"):
            return []  # bridge topics, /set echoes, unknown devices
        text = payload.decode(errors="replace").strip()
        if len(rest) == 2:
            try:
                value = json.loads(text)
                value = value.get("state") if isinstance(value, dict) else value
            except ValueError:
                value = text  # legacy plain payload "online" / "offline"
            if value not in ("online", "offline"):
                return []
            return [AvailabilityReport(device.id, value == "online")]
        try:
            data = json.loads(text)
        except ValueError:
            log.warning("malformed JSON on %s", topic)
            return []
        if not isinstance(data, dict):
            return []
        state = self.normalize(device, data)
        messages: list[DeviceMessage] = []
        if device.id in self._pending:
            messages.append(AckReport(device.id, self._pending.pop(device.id), "accepted"))
        if state:
            messages.append(StateReport(device.id, state))
        return messages

    @staticmethod
    def normalize(device: Device, data: dict[str, Any]) -> dict[str, Any]:
        state: dict[str, Any] = {}
        kind = device.type.name
        if kind == "door_lock":
            lock_state = data.get("lock_state")
            if lock_state in ("locked", "unlocked"):
                state["locked"] = lock_state == "locked"
            elif data.get("state") in ("LOCK", "UNLOCK"):
                state["locked"] = data["state"] == "LOCK"
            return state
        if kind == "curtains":
            if isinstance(data.get("position"), int | float):
                state["position"] = int(data["position"])
            state["moving"] = bool(data.get("moving", False))
            return state
        if data.get("state") in ("ON", "OFF"):
            state["on"] = data["state"] == "ON"
        if isinstance(data.get("brightness"), int | float):
            state["brightness"] = from_z2m_brightness(data["brightness"])
        color = data.get("color")
        if device.type.has("color") and isinstance(color, dict):
            if "x" in color and "y" in color:
                state["color"] = xy_to_color(float(color["x"]), float(color["y"]))
            elif isinstance(color.get("hex"), str):
                state["color"] = nearest_color(hex_to_rgb(color["hex"]))
        return state
