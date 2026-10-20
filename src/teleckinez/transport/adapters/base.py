"""DeviceAdapter: maps internal commands/states to a firmware's MQTT format (protocol.md §4)."""

from typing import Any, Protocol

from teleckinez.transport.messages import DeviceMessage


class DeviceAdapter(Protocol):
    name: str
    heartbeat: bool  # devices re-send their state periodically (False: never mark them stale)

    def subscriptions(self) -> list[str]:
        """Topic filters that carry state/ack/availability/meta for this protocol."""
        ...

    def encode_command(
        self, device_id: str, cmd_id: str, desired: dict[str, Any], ttl_s: float, ts: float
    ) -> tuple[str, bytes]:
        """Topic and payload that make the device apply `desired`."""
        ...

    def decode(self, topic: str, payload: bytes) -> list[DeviceMessage]:
        """Normalize an incoming message (one may mean ack + state); [] if not ours."""
        ...
