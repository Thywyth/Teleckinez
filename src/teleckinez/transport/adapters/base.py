"""DeviceAdapter: maps internal commands/states to a firmware's MQTT format (protocol.md §4)."""

from typing import Any, Protocol

from teleckinez.transport.messages import DeviceMessage


class DeviceAdapter(Protocol):
    name: str

    def subscriptions(self) -> list[str]:
        """Topic filters that carry state/ack/availability/meta for this protocol."""
        ...

    def encode_command(
        self, device_id: str, cmd_id: str, desired: dict[str, Any], ttl_s: float, ts: float
    ) -> tuple[str, bytes]:
        """Topic and payload that make the device apply `desired`."""
        ...

    def decode(self, topic: str, payload: bytes) -> DeviceMessage | None:
        """Normalize an incoming message; None if it is not ours or malformed."""
        ...
