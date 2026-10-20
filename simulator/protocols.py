"""How a simulated device talks: native Teleckinez, Tasmota or Zigbee2MQTT (protocol.md §4).

A SimProtocol only maps topics and payloads; physics, faults, latency and
idempotence stay in SimDevice. The firmware-like personalities let the
assistant's adapters be tested end to end without real hardware.
"""

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from teleckinez.devices.colors import COLOR_HEX, color_to_xy, hex_to_rgb, nearest_color, xy_to_color
from teleckinez.devices.registry import Device
from teleckinez.transport.adapters.native import Topics

Publication = tuple[str, bytes, bool]  # topic, payload, retain


def _dumps(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()


@dataclass(frozen=True)
class ParsedCommand:
    cmd_id: str
    action: str
    params: dict[str, Any]
    ts: float | None = None
    ttl_s: float | None = None


class SimProtocol:
    name = "native"
    heartbeat = True

    def __init__(self, device: Device, topics: Topics) -> None:
        self.device = device
        self.topics = topics

    def topic(self, kind: str) -> str:
        return self.topics.device(self.device.id, kind)

    def command_topics(self) -> list[str]:
        return [self.topic("cmd")]

    def will(self) -> Publication:
        return self.topic("availability"), b"offline", True

    def announce(self, meta: dict[str, Any]) -> list[Publication]:
        return [
            (self.topic("meta"), _dumps(meta), True),
            (self.topic("availability"), b"online", True),
        ]

    def offline(self) -> Publication:
        return self.topic("availability"), b"offline", True

    def state(self, state: dict[str, Any], seq: int, reason: str) -> list[Publication]:
        payload = {**state, "ts": round(time.time(), 3), "seq": seq}
        return [(self.topic("state"), _dumps(payload), True)]

    def ack(self, ack: dict[str, Any]) -> list[Publication]:
        return [(self.topic("ack"), _dumps(ack), False)]

    def parse(self, topic: str, payload: bytes, state: dict[str, Any]) -> ParsedCommand | None:
        try:
            cmd = json.loads(payload)
            return ParsedCommand(
                str(cmd["cmd_id"]),
                str(cmd.get("action", "set")),
                dict(cmd.get("params") or {}),
                cmd.get("ts"),
                cmd.get("ttl_s"),
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            return None


def _synthetic_id(prefix: str) -> str:
    """These firmwares carry no command id; every message is a new command."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class TasmotaProtocol(SimProtocol):
    """cmnd/{topic}/POWER|Dimmer|Color|Backlog -> stat/{topic}/RESULT, tele/{topic}/STATE, LWT."""

    name = "tasmota"

    def __init__(self, device: Device, topics: Topics, root: str = "") -> None:
        super().__init__(device, topics)
        self.address = device.config.address or device.id.replace(".", "_")
        self.root = root.strip("/")

    def t(self, prefix: str, command: str) -> str:
        base = f"{prefix}/{self.address}/{command}"
        return f"{self.root}/{base}" if self.root else base

    def command_topics(self) -> list[str]:
        return [self.t("cmnd", "+")]

    def will(self) -> Publication:
        return self.t("tele", "LWT"), b"Offline", True

    def announce(self, meta: dict[str, Any]) -> list[Publication]:
        return [(self.t("tele", "LWT"), b"Online", True)]

    def offline(self) -> Publication:
        return self.t("tele", "LWT"), b"Offline", True

    def _json_state(self, state: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"POWER": "ON" if state.get("on") else "OFF"}
        if "brightness" in state:
            out["Dimmer"] = state["brightness"]
        if "color" in state:
            out["Color"] = COLOR_HEX.get(state["color"], "FFFFFF")
        return out

    def state(self, state: dict[str, Any], seq: int, reason: str) -> list[Publication]:
        body = _dumps(self._json_state(state))
        if reason == "heartbeat":  # TelePeriod telemetry
            return [(self.t("tele", "STATE"), body, False)]
        return [(self.t("stat", "RESULT"), body, False),
                (self.t("stat", "POWER"), b"ON" if state.get("on") else b"OFF", False)]  # fmt: skip

    def ack(self, ack: dict[str, Any]) -> list[Publication]:
        return []  # the RESULT after execution is the only reply

    def parse(self, topic: str, payload: bytes, state: dict[str, Any]) -> ParsedCommand | None:
        command = topic.rsplit("/", 1)[-1]
        text = payload.decode(errors="replace").strip()
        steps = (
            [s.strip() for s in text.split(";")]
            if command.lower() == "backlog"
            else [f"{command} {text}"]
        )
        params: dict[str, Any] = {}
        for step in steps:
            name, _, value = step.partition(" ")
            name, value = name.lower(), value.strip()
            if name in ("power", "power1"):
                upper = value.upper()
                params["on"] = (not state.get("on")) if upper == "TOGGLE" else upper in ("ON", "1")
            elif name == "dimmer" and value.isdigit():
                params["brightness"] = int(value)
            elif name in ("color", "color1") and value:
                try:
                    rgb = (
                        tuple(int(c) for c in value.split(","))
                        if "," in value
                        else hex_to_rgb(value)
                    )
                    params["color"] = nearest_color(rgb)  # type: ignore[arg-type]
                except ValueError:
                    return None
        if not params:
            return None
        return ParsedCommand(_synthetic_id("tasmota"), "set", params)


class Zigbee2MqttProtocol(SimProtocol):
    """{base}/{name}/set JSON -> {base}/{name} JSON state; availability JSON; no heartbeat."""

    name = "zigbee2mqtt"
    heartbeat = False

    def __init__(self, device: Device, topics: Topics, base: str = "zigbee2mqtt") -> None:
        super().__init__(device, topics)
        self.name_ = device.config.address or device.id.replace(".", "_")
        self.base = base.strip("/")

    def command_topics(self) -> list[str]:
        return [f"{self.base}/{self.name_}/set"]

    def will(self) -> Publication:
        return f"{self.base}/{self.name_}/availability", b'{"state":"offline"}', True

    def announce(self, meta: dict[str, Any]) -> list[Publication]:
        return [(f"{self.base}/{self.name_}/availability", b'{"state":"online"}', True)]

    def offline(self) -> Publication:
        return self.will()

    def state(self, state: dict[str, Any], seq: int, reason: str) -> list[Publication]:
        kind = self.device.type.name
        out: dict[str, Any] = {"linkquality": 120}
        if kind == "door_lock":
            locked = state.get("locked", True)
            out |= {
                "state": "LOCK" if locked else "UNLOCK",
                "lock_state": "locked" if locked else "unlocked",
            }
        elif kind == "curtains":
            out |= {
                "position": state.get("position", 0),
                "state": "OPEN" if state.get("position") else "CLOSE",
            }
        else:
            out["state"] = "ON" if state.get("on") else "OFF"
            if "brightness" in state:
                out["brightness"] = round(state["brightness"] * 254 / 100)
            if "color" in state:
                out |= {"color": color_to_xy(state["color"]), "color_mode": "xy"}
        return [(f"{self.base}/{self.name_}", _dumps(out), True)]

    def ack(self, ack: dict[str, Any]) -> list[Publication]:
        return []

    def parse(self, topic: str, payload: bytes, state: dict[str, Any]) -> ParsedCommand | None:
        try:
            data = json.loads(payload)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        kind, params = self.device.type.name, {}
        value = str(data.get("state", "")).upper()
        if kind == "door_lock" and value in ("LOCK", "UNLOCK"):
            params["locked"] = value == "LOCK"
        elif kind == "curtains":
            if value in ("OPEN", "CLOSE"):
                params["position"] = 100 if value == "OPEN" else 0
            if isinstance(data.get("position"), int | float):
                params["position"] = int(data["position"])
        else:
            if value in ("ON", "OFF", "TOGGLE"):
                params["on"] = (not state.get("on")) if value == "TOGGLE" else value == "ON"
            if isinstance(data.get("brightness"), int | float):
                params["brightness"] = max(1, round(data["brightness"] * 100 / 254))
            color = data.get("color")
            if isinstance(color, dict) and "x" in color:
                params["color"] = xy_to_color(float(color["x"]), float(color["y"]))
            elif isinstance(color, dict) and "hex" in color:
                params["color"] = nearest_color(hex_to_rgb(color["hex"]))
        if not params:
            return None
        return ParsedCommand(_synthetic_id("z2m"), "set", params)


def make_protocol(
    device: Device, topics: Topics, *, tasmota_root: str = "", z2m_base: str = "zigbee2mqtt"
) -> SimProtocol:
    if device.config.protocol == "tasmota":
        return TasmotaProtocol(device, topics, tasmota_root)
    if device.config.protocol == "zigbee2mqtt":
        return Zigbee2MqttProtocol(device, topics, z2m_base)
    return SimProtocol(device, topics)
