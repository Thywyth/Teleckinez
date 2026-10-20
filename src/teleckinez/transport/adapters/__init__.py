"""Protocol adapters: native (tkz/dev/...), later tasmota and zigbee2mqtt."""

from teleckinez.transport.adapters.base import DeviceAdapter
from teleckinez.transport.adapters.native import NativeAdapter

__all__ = ["DeviceAdapter", "NativeAdapter"]
