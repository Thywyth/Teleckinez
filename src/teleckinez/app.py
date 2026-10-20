"""Composition root: builds all components from the configuration (ADR-001).

Every replaceable part (parser, adapters, storage) is chosen here, so the
modules themselves only know interfaces and the event bus.
"""

import asyncio
import contextlib
import json
import logging

from teleckinez.assistant import Assistant
from teleckinez.core.config import DevicesConfig, Settings
from teleckinez.core.events import EventBus, SimFaultsChanged
from teleckinez.devices.health import HealthMonitor
from teleckinez.devices.registry import DeviceRegistry
from teleckinez.devices.twin import TwinStore
from teleckinez.dispatch.dispatcher import Dispatcher
from teleckinez.dispatch.notifier import Notifier
from teleckinez.dispatch.response import ResponseComposer
from teleckinez.dispatch.tracker import CommandTracker
from teleckinez.nlu.resolver import TargetResolver
from teleckinez.nlu.rule_parser import RuleParser
from teleckinez.storage import EventRecorder, Storage, connect
from teleckinez.transport.adapters.native import NativeAdapter, Topics
from teleckinez.transport.gateway import DeviceGateway
from teleckinez.transport.mqtt_client import MqttClient

log = logging.getLogger(__name__)


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class App:
    def __init__(
        self, settings: Settings, devices: DevicesConfig, *, use_storage: bool = True
    ) -> None:
        self.settings = settings
        self.bus = EventBus()
        self.registry = DeviceRegistry(devices)
        self.twins = TwinStore(
            self.registry, self.bus, default_heartbeat_s=settings.health.heartbeat_s
        )
        self.health = HealthMonitor(
            self.twins,
            stale_factor=settings.health.stale_factor,
            check_interval_s=settings.health.check_interval_s,
        )
        self.tracker = CommandTracker(self.bus)
        topics = Topics(settings.mqtt.prefix)
        self.mqtt = MqttClient(settings.mqtt, availability_topic=topics.assistant_availability)
        self.gateway = DeviceGateway(
            self.mqtt, self.registry, {"native": NativeAdapter(settings.mqtt.prefix)}
        )
        self.dispatcher = Dispatcher(
            self.registry, self.twins, self.tracker, self.gateway, self.bus, settings.dispatcher
        )
        self.gateway.add_listener(self.twins.on_message)
        self.gateway.add_listener(self.dispatcher.on_device_message)
        if settings.nlu.mode != "rule":
            log.warning("NLU mode %r is not implemented yet, using 'rule'", settings.nlu.mode)
        self.parser = RuleParser(
            self.registry,
            fuzzy_threshold=settings.nlu.fuzzy_threshold,
            min_confidence=settings.nlu.min_confidence,
        )
        self.resolver = TargetResolver(self.registry)
        self.composer = ResponseComposer(self.registry, self.twins, fresh_s=settings.health.fresh_s)
        self.assistant = Assistant(
            self.bus,
            self.parser,
            self.resolver,
            self.dispatcher,
            self.tracker,
            self.composer,
            settings.dispatcher,
        )
        self.notifier = Notifier(
            self.registry,
            self.composer,
            self.bus,
            settings.notifications,
            language=lambda: self.settings.language,
        )
        self.storage: Storage | None = None
        if use_storage:
            self.storage = Storage(connect(settings.storage.path))
            EventRecorder(self.storage, self.bus)
        self.voice = None  # voice pipeline (stage 2); the web panel checks for it
        # Faults injected in the simulator, mirrored for the web panel (devices.md §4).
        self.topics = topics
        self.sim_faults: dict[str, dict] = {}
        self.mqtt.subscribe(topics.sim_faults_state, self._on_sim_faults)
        self._tasks: list[asyncio.Task] = []

    # Sections applied at once; voice ones are stored for the voice pipeline; the rest
    # (mqtt, web, storage, simulator) only take effect after a restart.
    HOT_SECTIONS = ("dispatcher", "nlu", "health", "notifications", "voice")
    RESTART_SECTIONS = ("mqtt", "web", "storage", "simulator")

    def apply_settings(self, patch: dict) -> dict[str, list[str]]:
        """Validate a partial settings update and apply what can be applied without restart.

        Raises pydantic.ValidationError on invalid values; nothing is changed then.
        """
        current = self.settings.model_dump(mode="json")
        merged = _deep_merge(current, patch)
        new = Settings.model_validate(merged)  # validates everything before touching anything
        applied, restart = [], []
        if new.language != self.settings.language:
            self.settings.language = new.language
            applied.append("language")
        for section in (*self.HOT_SECTIONS, *self.RESTART_SECTIONS):
            old_model, new_model = getattr(self.settings, section), getattr(new, section)
            for field in type(new_model).model_fields:
                if field == "password":
                    continue
                old_value, new_value = getattr(old_model, field), getattr(new_model, field)
                if old_value == new_value:
                    continue
                name = f"{section}.{field}"
                if section in self.RESTART_SECTIONS:
                    restart.append(name)
                    continue
                setattr(old_model, field, new_value)  # the same object the components hold
                applied.append(name)
        # components that copied values at construction time
        self.parser.fuzzy_threshold = self.settings.nlu.fuzzy_threshold
        self.parser.min_confidence = self.settings.nlu.min_confidence
        self.health.stale_factor = self.settings.health.stale_factor
        self.health.check_interval_s = self.settings.health.check_interval_s
        self.composer.fresh_s = self.settings.health.fresh_s
        return {"applied": applied, "restart_required": restart}

    def _on_sim_faults(self, topic: str, payload: bytes) -> None:
        try:
            faults = json.loads(payload) if payload else {}
        except ValueError:
            log.warning("bad fault state on %s", topic)
            return
        self.sim_faults = faults if isinstance(faults, dict) else {}
        self.bus.publish(SimFaultsChanged(faults=self.sim_faults))

    async def send_fault(self, device_id: str, fault: str, params: dict, enabled: bool) -> None:
        """Ask the simulator to inject / clear a fault (raises MqttNotConnected)."""
        message = {"device_id": device_id, "fault": fault, "params": params, "enabled": enabled}
        await self.mqtt.publish(self.topics.sim_faults, message, qos=1)

    async def serve_web(self) -> None:
        """Run the web panel (uvicorn) in this event loop until cancelled."""
        import uvicorn

        from teleckinez.web.app import create_app

        config = uvicorn.Config(
            create_app(self),
            host=self.settings.web.host,
            port=self.settings.web.port,
            log_level="warning",
            lifespan="off",
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # Ctrl+C is handled by our main
        await server.serve()

    async def start(self, settle_s: float = 0.5) -> None:
        """Connect to the broker and wait briefly for retained device states."""
        await self.mqtt.start()
        self._tasks.append(asyncio.create_task(self.health.run(), name="health"))
        await asyncio.sleep(settle_s)

    async def stop(self) -> None:
        await self.assistant.close()
        await self.dispatcher.close()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.mqtt.stop()
        await self.bus.close()
        if self.storage is not None:
            self.storage.close()

    async def __aenter__(self) -> "App":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()
