"""DeviceGateway: sends commands and delivers normalized device messages.

Picks the adapter by each device's `protocol` from devices.yaml, so the core
never sees firmware-specific topics or payloads.
"""

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from teleckinez.devices.registry import DeviceRegistry
from teleckinez.transport.adapters.base import DeviceAdapter
from teleckinez.transport.messages import DeviceMessage
from teleckinez.transport.mqtt_client import MqttClient

log = logging.getLogger(__name__)

Listener = Callable[[DeviceMessage], None]


class DeviceGateway:
    def __init__(
        self,
        mqtt: MqttClient,
        registry: DeviceRegistry,
        adapters: Mapping[str, DeviceAdapter],
    ) -> None:
        self.mqtt = mqtt
        self.registry = registry
        self.adapters = dict(adapters)
        self._listeners: list[Listener] = []
        missing = {d.config.protocol for d in registry} - set(self.adapters)
        if missing:
            raise ValueError(f"no adapter for protocol(s): {', '.join(sorted(missing))}")
        for adapter in self.adapters.values():
            for topic_filter in adapter.subscriptions():
                mqtt.subscribe(topic_filter, self._make_handler(adapter))

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def is_connected(self) -> bool:
        return self.mqtt.connected.is_set()

    async def send(
        self, device_id: str, cmd_id: str, desired: dict[str, Any], ttl_s: float
    ) -> None:
        """Publish a command (QoS 1). Raises MqttNotConnected if the broker is unreachable."""
        adapter = self.adapters[self.registry.get(device_id).config.protocol]
        topic, payload = adapter.encode_command(device_id, cmd_id, desired, ttl_s, time.time())
        await self.mqtt.publish(topic, payload, qos=1)

    def _make_handler(self, adapter: DeviceAdapter) -> Callable[[str, bytes], None]:
        def handle(topic: str, payload: bytes) -> None:
            for message in adapter.decode(topic, payload):
                if message.device_id not in self.registry:
                    log.debug("message from unknown device %s ignored", message.device_id)
                    continue
                if self.registry.get(message.device_id).config.protocol != adapter.name:
                    continue  # e.g. a native topic for a device configured as Tasmota
                for listener in self._listeners:
                    listener(message)

        return handle
