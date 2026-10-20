"""Proactive notifications about device health (dispatcher.md §5).

The assistant speaks up on its own when a device drops off the network,
stops sending heartbeats, comes back, or when a critical device changes
state without a command (someone unlocked the front door by hand). The text
goes out as SpeakRequested(kind="notification"): the web panel shows it and
the TTS (stage 2) speaks it.
"""

import time
from collections.abc import Callable

from teleckinez.core.config import NotificationSettings
from teleckinez.core.events import (
    DeviceAvailabilityChanged,
    DeviceStateChanged,
    EventBus,
    SpeakRequested,
)
from teleckinez.core.models import Availability
from teleckinez.devices.registry import DeviceRegistry
from teleckinez.dispatch.response import ResponseComposer, lower_first

A = Availability


class Notifier:
    def __init__(
        self,
        registry: DeviceRegistry,
        composer: ResponseComposer,
        bus: EventBus,
        settings: NotificationSettings,
        language: Callable[[], str] = lambda: "uk",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.registry = registry
        self.composer = composer
        self.bus = bus
        self.settings = settings
        self.language = language
        self.clock = clock
        self.started = clock()
        self._last: dict[tuple[str, str], float] = {}  # (device, kind) -> time said
        self._announced_problem: set[str] = set()  # devices we said something bad about
        bus.subscribe(self._on_availability, DeviceAvailabilityChanged)
        bus.subscribe(self._on_state, DeviceStateChanged)

    def _on_availability(self, event: DeviceAvailabilityChanged) -> None:
        old, new = event.previous, event.availability
        if old is A.UNKNOWN:
            return  # first report after start, nothing changed for the user
        if new is A.OFFLINE and self.settings.offline:
            self._say(event.device_id, "offline")
        elif new is A.STALE and old is A.ONLINE and self.settings.stale:
            self._say(event.device_id, "stale")
        elif new is A.ONLINE and event.device_id in self._announced_problem:
            if self.settings.recovered:
                self._say(event.device_id, "back")
            self._announced_problem.discard(event.device_id)

    def _on_state(self, event: DeviceStateChanged) -> None:
        if event.source != "drift" or not self.settings.drift_critical:
            return
        device = self.registry.get(event.device_id)
        writable = {c.name for c in device.type.writable}
        changed = {k: v for k, v in event.changed.items() if k in writable}
        if device.critical and changed:
            self._say(event.device_id, "drift", changed)

    def _say(self, device_id: str, kind: str, changed: dict | None = None) -> None:
        if not self.settings.enabled:
            return
        now = self.clock()
        if now - self.started < self.settings.startup_grace_s:
            return
        last = self._last.get((device_id, kind))
        if last is not None and now - last < self.settings.cooldown_s:
            return  # a flapping device must not talk every second
        self._last[(device_id, kind)] = now
        if kind in ("offline", "stale"):
            self._announced_problem.add(device_id)
        lang = self.language()
        device = self.registry.get(device_id)
        title = device.title(lang)
        if kind == "drift":
            what = ", ".join(
                p
                for k, v in (changed or {}).items()
                if (p := self.composer.attr_phrase(k, v, lang))
            )
            text = self.composer.t(lang, "notify_drift", title=lower_first(title), what=what)
        else:
            be = "are" if device.is_plural(lang) else "is"
            text = self.composer.t(lang, f"notify_{kind}", title=title, be=be)
        self.bus.publish(SpeakRequested(text=text, lang=lang, kind="notification"))
