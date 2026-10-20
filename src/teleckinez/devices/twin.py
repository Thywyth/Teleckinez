"""Digital twin: reported vs desired state of every device (dispatcher.md §2, ADR-006)."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from teleckinez.core.events import DeviceAvailabilityChanged, DeviceStateChanged, EventBus
from teleckinez.core.models import Availability
from teleckinez.devices.registry import Device, DeviceRegistry
from teleckinez.transport.messages import (
    AckReport,
    AvailabilityReport,
    DeviceMessage,
    MetaReport,
    StateReport,
)

log = logging.getLogger(__name__)
Clock = Callable[[], float]


@dataclass
class DigitalTwin:
    device: Device
    heartbeat_s: float | None  # None: the device never re-sends its state (Zigbee2MQTT)
    reported: dict[str, Any] = field(default_factory=dict)
    reported_ts: float | None = None  # device timestamp of the last state
    seq: int | None = None
    desired: dict[str, Any] | None = None  # set while a command is in progress
    availability: Availability = Availability.UNKNOWN
    last_seen: float | None = None  # our clock, any message from the device
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.device.id

    @property
    def converged(self) -> bool:
        return self.desired is None or self.device.type.converged(self.desired, self.reported)

    def age(self, now: float) -> float | None:
        """Seconds since the device was last heard from."""
        return None if self.last_seen is None else now - self.last_seen

    def snapshot(self, now: float) -> dict[str, Any]:
        return {
            "id": self.id,
            "reported": dict(self.reported),
            "desired": dict(self.desired) if self.desired else None,
            "availability": self.availability.value,
            "age_s": self.age(now),
        }


class TwinStore:
    """Keeps twins up to date from device messages and publishes changes on the bus."""

    def __init__(
        self,
        registry: DeviceRegistry,
        bus: EventBus,
        *,
        default_heartbeat_s: float = 10.0,
        clock: Clock = time.time,
        no_heartbeat: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        self.bus = bus
        self.clock = clock
        self._twins = {
            dev.id: DigitalTwin(
                dev,
                None if dev.id in no_heartbeat else dev.config.heartbeat_s or default_heartbeat_s,
            )
            for dev in registry
        }

    def __iter__(self):
        return iter(self._twins.values())

    def get(self, device_id: str) -> DigitalTwin:
        return self._twins[device_id]

    # ------------------------------------------------------------------ input

    def on_message(self, message: DeviceMessage) -> None:
        """Gateway listener."""
        twin = self._twins.get(message.device_id)
        if twin is None:
            return
        if isinstance(message, StateReport):
            self.apply_state(twin, message)
        elif isinstance(message, AvailabilityReport):
            self.set_availability(
                twin.id, Availability.ONLINE if message.online else Availability.OFFLINE
            )
        elif isinstance(message, MetaReport):
            twin.meta = message.meta
            if isinstance(hb := message.meta.get("heartbeat_s"), int | float) and hb > 0:
                twin.heartbeat_s = float(hb)
        elif isinstance(message, AckReport):
            self._seen(twin)

    def apply_state(self, twin: DigitalTwin, report: StateReport) -> None:
        self._seen(twin)  # first: going UNKNOWN/OFFLINE -> ONLINE resets the seq counter
        # seq <= 1 means the device restarted and counts from the beginning again.
        if report.seq is not None and twin.seq is not None and 1 < report.seq <= twin.seq:
            log.debug("%s: stale state seq %s <= %s dropped", twin.id, report.seq, twin.seq)
            return
        twin.seq = report.seq
        twin.reported_ts = report.ts
        changed = {k: v for k, v in report.state.items() if twin.reported.get(k, ...) != v}
        first = not twin.reported
        twin.reported = {**twin.reported, **report.state} if report.partial else dict(report.state)
        if not changed:
            return
        writable = {c.name for c in twin.device.type.writable}
        drift = not first and twin.desired is None and bool(writable & changed.keys())
        self.bus.publish(
            DeviceStateChanged(
                device_id=twin.id,
                reported=dict(twin.reported),
                changed=changed,
                source="drift" if drift else "report",
            )
        )

    def set_availability(self, device_id: str, availability: Availability) -> None:
        twin = self._twins[device_id]
        if availability is Availability.ONLINE:
            twin.last_seen = self.clock()
            if twin.availability in (Availability.OFFLINE, Availability.UNKNOWN):
                twin.seq = None  # (re)connected: the device may have restarted its counter
        previous = twin.availability
        if previous is availability:
            return
        twin.availability = availability
        log.info("%s: %s -> %s", device_id, previous.value, availability.value)
        self.bus.publish(
            DeviceAvailabilityChanged(
                device_id=device_id, availability=availability, previous=previous
            )
        )

    def _seen(self, twin: DigitalTwin) -> None:
        twin.last_seen = self.clock()
        if twin.availability in (Availability.STALE, Availability.UNKNOWN):
            # A message proves it is alive (UNKNOWN: availability topic not received yet).
            self.set_availability(twin.id, Availability.ONLINE)

    # ---------------------------------------------------------- dispatcher API

    def set_desired(self, device_id: str, desired: dict[str, Any] | None) -> None:
        self._twins[device_id].desired = dict(desired) if desired else None
