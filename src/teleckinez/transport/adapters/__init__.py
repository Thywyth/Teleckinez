"""Protocol adapters: native (tkz/dev/...), later tasmota and zigbee2mqtt."""

from teleckinez.transport.adapters.base import DeviceAdapter
from teleckinez.transport.adapters.native import NativeAdapter
from teleckinez.transport.adapters.tasmota import TasmotaAdapter
from teleckinez.transport.adapters.zigbee2mqtt import Zigbee2MqttAdapter

__all__ = ["DeviceAdapter", "NativeAdapter", "TasmotaAdapter", "Zigbee2MqttAdapter"]
