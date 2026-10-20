"""Native Teleckinez protocol: tkz/dev/{id}/{meta|availability|state|cmd|ack} (protocol.md §2-3)."""

import json
import logging
from typing import Any

from teleckinez.transport.messages import (
    AckReport,
    AvailabilityReport,
    DeviceMessage,
    MetaReport,
    StateReport,
)

log = logging.getLogger(__name__)

ACK_STATUSES = {"accepted", "rejected", "done"}


class Topics:
    """Topic layout for one prefix; shared by the adapter and the simulator."""

    def __init__(self, prefix: str = "tkz") -> None:
        self.prefix = prefix.rstrip("/")

    def device(self, device_id: str, kind: str) -> str:
        return f"{self.prefix}/dev/{device_id}/{kind}"

    def all(self, kind: str) -> str:
        return f"{self.prefix}/dev/+/{kind}"

    @property
    def assistant_availability(self) -> str:
        return f"{self.prefix}/assistant/availability"

    @property
    def sim_faults(self) -> str:
        return f"{self.prefix}/sim/faults"

    def parse(self, topic: str) -> tuple[str, str] | None:
        """'tkz/dev/light.k/state' -> ('light.k', 'state')."""
        parts = topic.split("/")
        head = self.prefix.split("/")
        if len(parts) != len(head) + 3 or parts[: len(head)] != head or parts[len(head)] != "dev":
            return None
        return parts[-2], parts[-1]


class NativeAdapter:
    name = "native"

    def __init__(self, prefix: str = "tkz") -> None:
        self.topics = Topics(prefix)

    def subscriptions(self) -> list[str]:
        return [self.topics.all(kind) for kind in ("meta", "availability", "state", "ack")]

    def encode_command(
        self, device_id: str, cmd_id: str, desired: dict[str, Any], ttl_s: float, ts: float
    ) -> tuple[str, bytes]:
        payload = {"cmd_id": cmd_id, "action": "set", "params": desired, "ts": ts, "ttl_s": ttl_s}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        return self.topics.device(device_id, "cmd"), body

    def decode(self, topic: str, payload: bytes) -> DeviceMessage | None:
        parsed = self.topics.parse(topic)
        if parsed is None or not payload:  # empty payload = cleared retained message
            return None
        device_id, kind = parsed
        if kind == "availability":
            text = payload.decode(errors="replace").strip().lower()
            if text not in ("online", "offline"):
                log.warning("bad availability %r from %s", text, device_id)
                return None
            return AvailabilityReport(device_id, text == "online")
        try:
            data = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            log.warning("malformed JSON on %s", topic)
            return None
        if not isinstance(data, dict):
            return None
        if kind == "state":
            state = {k: v for k, v in data.items() if k not in ("ts", "seq")}
            seq = data.get("seq")
            return StateReport(
                device_id, state, data.get("ts"), seq if isinstance(seq, int) else None
            )
        if kind == "ack":
            status = data.get("status")
            if not data.get("cmd_id") or status not in ACK_STATUSES:
                log.warning("bad ack on %s: %r", topic, data)
                return None
            return AckReport(
                device_id, str(data["cmd_id"]), status, data.get("error"), data.get("ts")
            )
        if kind == "meta":
            return MetaReport(device_id, data)
        return None
