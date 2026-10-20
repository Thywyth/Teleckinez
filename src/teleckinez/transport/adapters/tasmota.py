"""Tasmota firmware adapter (protocol.md §4; tasmota.github.io/docs/MQTT, /Commands).

Topics (default FullTopic `%prefix%/%topic%/`): commands go to
`cmnd/{topic}/{Command}`, replies come on `stat/{topic}/RESULT`, telemetry
on `tele/{topic}/STATE` every TelePeriod and the LWT on `tele/{topic}/LWT`
(`Online` / `Offline`). Tasmota has no command ids, so the first RESULT after
a command is taken as its acknowledgement.
"""

import json
import logging
from collections.abc import Iterable
from typing import Any

from teleckinez.devices.colors import COLOR_HEX, hex_to_rgb, nearest_color
from teleckinez.devices.registry import Device
from teleckinez.transport.messages import AckReport, AvailabilityReport, DeviceMessage, StateReport

log = logging.getLogger(__name__)

SUPPORTED_TYPES = frozenset({"light", "rgb_light", "socket"})


def device_address(device: Device) -> str:
    return device.config.address or device.id.replace(".", "_")


class TasmotaAdapter:
    name = "tasmota"
    heartbeat = True  # tele/STATE every TelePeriod

    def __init__(self, devices: Iterable[Device], root: str = "") -> None:
        self.root = root.strip("/")
        self._by_address: dict[str, Device] = {}
        for device in devices:
            if device.type.name not in SUPPORTED_TYPES:
                raise ValueError(
                    f"{device.id}: Tasmota adapter does not support {device.type.name}"
                )
            self._by_address[device_address(device)] = device
        self._address = {d.id: a for a, d in self._by_address.items()}
        self._pending: dict[str, str] = {}  # device id -> cmd_id waiting for its RESULT

    def _topic(self, prefix: str, address: str, command: str) -> str:
        base = f"{prefix}/{address}/{command}"
        return f"{self.root}/{base}" if self.root else base

    def subscriptions(self) -> list[str]:
        return [self._topic("stat", "+", "RESULT"), self._topic("tele", "+", "STATE"),
                self._topic("tele", "+", "LWT")]  # fmt: skip

    def encode_command(
        self, device_id: str, cmd_id: str, desired: dict[str, Any], ttl_s: float, ts: float
    ) -> tuple[str, bytes]:
        parts = []
        if "on" in desired:
            parts.append(f"Power {'ON' if desired['on'] else 'OFF'}")
        if "brightness" in desired:
            parts.append(f"Dimmer {int(desired['brightness'])}")
        if "color" in desired:
            parts.append(f"Color #{COLOR_HEX[desired['color']]}")
        if not parts:
            raise ValueError(f"{device_id}: nothing Tasmota can set in {desired}")
        self._pending[device_id] = cmd_id
        address = self._address[device_id]
        if len(parts) == 1 and parts[0].startswith("Power"):
            return self._topic("cmnd", address, "POWER"), parts[0].split()[1].encode()
        return self._topic("cmnd", address, "Backlog"), "; ".join(parts).encode()

    def decode(self, topic: str, payload: bytes) -> list[DeviceMessage]:
        parts = topic.split("/")
        if self.root:
            if topic.split("/")[: len(self.root.split("/"))] != self.root.split("/"):
                return []
            parts = parts[len(self.root.split("/")) :]
        if len(parts) != 3 or not payload:
            return []
        prefix, address, kind = parts
        device = self._by_address.get(address)
        if device is None:
            return []
        if prefix == "tele" and kind == "LWT":
            text = payload.decode(errors="replace").strip()
            if text not in ("Online", "Offline"):
                return []
            return [AvailabilityReport(device.id, text == "Online")]
        try:
            data = json.loads(payload)
        except ValueError:
            log.warning("malformed JSON on %s", topic)
            return []
        if not isinstance(data, dict):
            return []
        state = self.normalize(device, data)
        messages: list[DeviceMessage] = []
        if prefix == "stat" and kind == "RESULT" and device.id in self._pending:
            messages.append(AckReport(device.id, self._pending.pop(device.id), "accepted"))
        if state:
            # RESULT carries only what changed; tele/STATE is complete for our attributes
            messages.append(StateReport(device.id, state, partial=kind == "RESULT"))
        return messages

    @staticmethod
    def normalize(device: Device, data: dict[str, Any]) -> dict[str, Any]:
        state: dict[str, Any] = {}
        power = data.get("POWER", data.get("POWER1"))
        if isinstance(power, str):
            state["on"] = power.upper() == "ON"
        if device.type.has("brightness") and isinstance(data.get("Dimmer"), int | float):
            state["brightness"] = max(1, min(100, int(data["Dimmer"])))
        color = data.get("Color")
        if device.type.has("color") and isinstance(color, str) and color:
            try:
                if "," in color:
                    rgb = tuple(int(c) for c in color.split(",")[:3])
                else:
                    rgb = hex_to_rgb(color)
                state["color"] = nearest_color(rgb)  # type: ignore[arg-type]
            except ValueError:
                pass
        return state
