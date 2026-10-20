"""SimHome: all simulated devices of devices.yaml, each in its own task."""

import asyncio
import contextlib
import logging

from simulator.device import SimDevice
from teleckinez.core.config import Settings
from teleckinez.devices.registry import DeviceRegistry
from teleckinez.transport.adapters.native import Topics

log = logging.getLogger(__name__)


class SimHome:
    def __init__(
        self,
        registry: DeviceRegistry,
        settings: Settings,
        *,
        only: set[str] | None = None,
        seed: int | None = None,
    ) -> None:
        topics = Topics(settings.mqtt.prefix)
        self.devices = {
            dev.id: SimDevice(
                dev,
                host=settings.mqtt.host,
                port=settings.mqtt.port,
                topics=topics,
                heartbeat_s=settings.health.heartbeat_s,
                latency_ms=settings.simulator.latency_ms,
                seed=None if seed is None else seed + i,
            )
            for i, dev in enumerate(registry)
            if only is None or dev.id in only
        }
        self._tasks: list[asyncio.Task] = []

    async def start(self, wait_s: float = 10.0) -> None:
        self._tasks = [
            asyncio.create_task(dev.run(), name=f"sim:{dev.id}") for dev in self.devices.values()
        ]
        await asyncio.wait_for(
            asyncio.gather(*(d.connected.wait() for d in self.devices.values())), wait_s
        )
        log.info("simulator: %d devices online", len(self.devices))

    async def stop(self) -> None:
        await asyncio.gather(*(d.shutdown() for d in self.devices.values()))
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    async def __aenter__(self) -> "SimHome":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()
