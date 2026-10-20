"""SimHome: all simulated devices of devices.yaml, each in its own task, plus fault control."""

import asyncio
import contextlib
import json
import logging

import aiomqtt

from simulator.device import SimDevice
from simulator.faults import FaultCommand, FaultError, parse_fault
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
        self.settings = settings
        self.topics = Topics(settings.mqtt.prefix)
        self.devices = {
            dev.id: SimDevice(
                dev,
                host=settings.mqtt.host,
                port=settings.mqtt.port,
                topics=self.topics,
                heartbeat_s=settings.health.heartbeat_s,
                latency_ms=settings.simulator.latency_ms,
                seed=None if seed is None else seed + i,
            )
            for i, dev in enumerate(registry)
            if only is None or dev.id in only
        }
        self._tasks: list[asyncio.Task] = []
        self._control: aiomqtt.Client | None = None
        self._control_ready = asyncio.Event()

    async def start(self, wait_s: float = 10.0) -> None:
        self._tasks = [
            asyncio.create_task(dev.run(), name=f"sim:{dev.id}") for dev in self.devices.values()
        ]
        self._tasks.append(asyncio.create_task(self._control_loop(), name="sim:faults"))
        waits = [d.connected.wait() for d in self.devices.values()]
        await asyncio.wait_for(asyncio.gather(*waits, self._control_ready.wait()), wait_s)
        log.info("simulator: %d devices online", len(self.devices))

    async def stop(self) -> None:
        await asyncio.gather(*(d.shutdown() for d in self.devices.values()))
        if self._control is not None:
            with contextlib.suppress(aiomqtt.MqttError):
                await self._control.publish(self.topics.sim_faults_state, b"", qos=1, retain=True)
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

    # ------------------------------------------------------------------- faults

    def apply_fault(self, command: FaultCommand) -> list[str]:
        """Apply a fault to one device or to all ("*"); returns the affected ids."""
        if command.device_id == "*":
            targets = list(self.devices.values())
        elif command.device_id in self.devices:
            targets = [self.devices[command.device_id]]
        else:
            log.warning("fault for unknown device %s ignored", command.device_id)
            return []
        for device in targets:
            device.apply_fault(command)
        log.info("fault %s on %s", command.fault, command.device_id)
        asyncio.get_running_loop().create_task(self._publish_fault_state())
        return [d.id for d in targets]

    def fault_state(self) -> dict[str, dict]:
        return {d.id: d.faults.describe() for d in self.devices.values() if d.faults.active}

    async def _publish_fault_state(self) -> None:
        if self._control is None:
            return
        payload = json.dumps(self.fault_state(), ensure_ascii=False).encode()
        with contextlib.suppress(aiomqtt.MqttError):
            await self._control.publish(self.topics.sim_faults_state, payload, qos=1, retain=True)

    async def _control_loop(self) -> None:
        """Listen to {prefix}/sim/faults (web panel, scripts) and apply the faults."""
        identifier = f"sim-{self.topics.prefix.replace('/', '_')}-control"
        mqtt = self.settings.mqtt
        while True:
            try:
                async with aiomqtt.Client(mqtt.host, mqtt.port, identifier=identifier) as client:
                    self._control = client
                    await client.subscribe(self.topics.sim_faults, qos=1)
                    await self._publish_fault_state()
                    self._control_ready.set()
                    async for message in client.messages:
                        payload = message.payload if isinstance(message.payload, bytes) else b""
                        try:
                            self.apply_fault(parse_fault(payload))
                        except FaultError as exc:
                            log.warning("bad fault message %r: %s", payload[:200], exc)
            except aiomqtt.MqttError as exc:
                log.warning("fault control: connection lost (%s)", exc)
            finally:
                self._control = None
            await asyncio.sleep(1.0)
