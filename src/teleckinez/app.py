"""Composition root: builds all components from the configuration (ADR-001).

Every replaceable part (parser, adapters, storage) is chosen here, so the
modules themselves only know interfaces and the event bus.
"""

import asyncio
import contextlib
import logging

from teleckinez.assistant import Assistant
from teleckinez.core.config import DevicesConfig, Settings
from teleckinez.core.events import EventBus
from teleckinez.devices.health import HealthMonitor
from teleckinez.devices.registry import DeviceRegistry
from teleckinez.devices.twin import TwinStore
from teleckinez.dispatch.dispatcher import Dispatcher
from teleckinez.dispatch.response import ResponseComposer
from teleckinez.dispatch.tracker import CommandTracker
from teleckinez.nlu.resolver import TargetResolver
from teleckinez.nlu.rule_parser import RuleParser
from teleckinez.storage import EventRecorder, Storage, connect
from teleckinez.transport.adapters.native import NativeAdapter, Topics
from teleckinez.transport.gateway import DeviceGateway
from teleckinez.transport.mqtt_client import MqttClient

log = logging.getLogger(__name__)


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
        self.composer = ResponseComposer(self.registry, self.twins)
        self.assistant = Assistant(
            self.bus,
            self.parser,
            self.resolver,
            self.dispatcher,
            self.tracker,
            self.composer,
            settings.dispatcher,
        )
        self.storage: Storage | None = None
        if use_storage:
            self.storage = Storage(connect(settings.storage.path))
            EventRecorder(self.storage, self.bus)
        self.voice = None  # voice pipeline (stage 2); the web panel checks for it
        self._tasks: list[asyncio.Task] = []

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
