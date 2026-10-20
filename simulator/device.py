"""A simulated IoT device speaking the native protocol (protocol.md, devices.md §4)."""

import asyncio
import contextlib
import json
import logging
import random
import socket
import time
from collections import OrderedDict
from typing import Any

import aiomqtt

from simulator.faults import FaultCommand, FaultSet, drift_change
from simulator.physics import Physics, make_physics
from teleckinez.devices.capabilities import CapabilityError, UnsupportedAction
from teleckinez.devices.registry import Device
from teleckinez.transport.adapters.native import Topics

log = logging.getLogger(__name__)

TICK_S = 1.0
SEEN_COMMANDS = 256  # remembered cmd_ids for idempotence
FW = "tkz-sim/1.0"


def _dumps(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()


class SimDevice:
    """One device = one MQTT connection with its own Last Will (`offline`)."""

    def __init__(
        self,
        device: Device,
        *,
        host: str,
        port: int,
        topics: Topics,
        heartbeat_s: float = 10.0,
        latency_ms: tuple[int, int] = (20, 80),
        seed: int | None = None,
    ) -> None:
        self.device = device
        self.host, self.port = host, port
        self.topics = topics
        self.heartbeat_s = device.config.heartbeat_s or heartbeat_s
        self.latency_ms = latency_ms
        self.rng = random.Random(seed)
        params = dict(device.config.sim)
        self.state: dict[str, Any] = {**device.type.initial_state(), **params.pop("state", {})}
        self.physics: Physics = make_physics(
            device.type.name, self.state, params, self.rng, device.type.speed
        )
        self.seq = 0
        self._seen: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._client: aiomqtt.Client | None = None
        self._motion: asyncio.Task | None = None
        self.connected = asyncio.Event()
        self.faults = FaultSet()
        self._powered = asyncio.Event()  # cleared while the `offline` fault is active
        self._powered.set()

    @property
    def id(self) -> str:
        return self.device.id

    @property
    def client_id(self) -> str:
        """Unique per topic prefix: a test home never kicks the real simulator off the broker."""
        return f"sim-{self.topics.prefix.replace('/', '_')}-{self.id}"

    def topic(self, kind: str) -> str:
        return self.topics.device(self.id, kind)

    # ------------------------------------------------------------------ running

    async def run(self) -> None:
        """Connect, announce, serve commands; reconnect if the broker goes away."""
        while True:
            await self._powered.wait()  # `offline` fault: stay disconnected until cleared
            will = aiomqtt.Will(self.topic("availability"), b"offline", qos=1, retain=True)
            try:
                async with aiomqtt.Client(
                    self.host, self.port, identifier=self.client_id, keepalive=10, will=will
                ) as client:
                    self._client = client
                    await self._announce()
                    await client.subscribe(self.topic("cmd"), qos=1)
                    self.connected.set()
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._heartbeat())
                        tg.create_task(self._ticker())
                        tg.create_task(self._serve(client))
            except* aiomqtt.MqttError as group:
                if self._powered.is_set():
                    log.warning(
                        "%s: connection lost (%s), reconnecting", self.id, group.exceptions[0]
                    )
                else:
                    log.info("%s: offline (injected fault)", self.id)
            finally:
                self.connected.clear()
                self._client = None
            await asyncio.sleep(1.0)

    async def shutdown(self) -> None:
        """Graceful power-off: say `offline` explicitly (no LWT needed)."""
        if self._client is not None:
            with contextlib.suppress(aiomqtt.MqttError):
                await self._client.publish(self.topic("availability"), b"offline", 1, True)

    async def _announce(self) -> None:
        assert self._client is not None
        meta = {
            "id": self.id,
            "type": self.device.type.name,
            "fw": FW,
            "capabilities": self.device.type.meta(),
            "heartbeat_s": self.heartbeat_s,
        }
        await self._client.publish(self.topic("meta"), _dumps(meta), qos=1, retain=True)
        await self._client.publish(self.topic("availability"), b"online", qos=1, retain=True)
        await self.publish_state()

    async def publish_state(self) -> None:
        if self._client is None or self.faults.silent:
            return
        self.seq += 1
        payload = {**self.state, "ts": round(time.time(), 3), "seq": self.seq}
        await self._client.publish(self.topic("state"), _dumps(payload), qos=1, retain=True)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            await self.publish_state()

    async def _ticker(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(TICK_S)
            now = time.monotonic()
            if self.physics.tick(now - last):
                await self.publish_state()
            last = now

    async def _serve(self, client: aiomqtt.Client) -> None:
        async for message in client.messages:
            payload = message.payload if isinstance(message.payload, bytes) else b""
            await self.handle_command(payload)

    # ----------------------------------------------------------------- commands

    async def _ack(self, ack: dict[str, Any]) -> None:
        if self._client is not None:
            await self._client.publish(self.topic("ack"), _dumps(ack), qos=1)

    def desired_for(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """Validate a command against the device capabilities."""
        dtype = self.device.type
        if action != "set":
            return dtype.desired_for(action, params, self.state)
        if not params:
            raise UnsupportedAction("empty set")
        desired = {}
        for key, value in params.items():
            if not dtype.has(key) or dtype.cap(key).readonly:
                raise UnsupportedAction(f"{key} is not writable", attribute=key)
            desired[key] = dtype.cap(key).coerce(value)
        return desired

    async def handle_command(self, payload: bytes) -> None:
        try:
            cmd = json.loads(payload)
            cmd_id = str(cmd["cmd_id"])
        except (ValueError, KeyError, TypeError):
            log.warning("%s: malformed command %r", self.id, payload[:200])
            return

        if self.faults.silent or self.faults.drops(self.rng):
            log.info("%s: command %s lost (fault)", self.id, cmd_id)
            return
        lo, hi = self.latency_ms
        await asyncio.sleep(self.rng.uniform(lo, hi) / 1000 + self.faults.delay_s())
        if self.faults.silent:  # went offline / froze while "processing"
            return

        if cmd_id in self._seen:  # retry of a command we already executed: ack again only
            await self._ack(self._seen[cmd_id])
            return
        ts, ttl = cmd.get("ts"), cmd.get("ttl_s")
        if isinstance(ts, int | float) and isinstance(ttl, int | float) and time.time() > ts + ttl:
            log.info("%s: command %s expired, ignored", self.id, cmd_id)
            return

        desired: dict[str, Any] = {}
        try:
            desired = self.desired_for(str(cmd.get("action", "set")), dict(cmd.get("params") or {}))
        except CapabilityError as exc:
            ack = {"cmd_id": cmd_id, "status": "rejected", "error": exc.code}
        else:
            ack = {"cmd_id": cmd_id, "status": "accepted"}
        if self.faults.has("reject"):
            ack = {
                "cmd_id": cmd_id,
                "status": "rejected",
                "error": self.faults.active["reject"]["error"],
            }
        ack["ts"] = round(time.time(), 3)
        self._remember(cmd_id, ack)
        await self._ack(ack)
        if ack["status"] == "accepted" and not self.faults.has("stuck"):
            if self._motion is not None and not self._motion.done():
                self._motion.cancel()  # a new command interrupts movement in progress
            self._motion = asyncio.create_task(self.physics.apply(desired, self.publish_state))

    # ------------------------------------------------------------------- faults

    def apply_fault(self, command: FaultCommand) -> None:
        was_offline = self.faults.has("offline")
        self.faults.apply(command)
        if command.fault == "drift":
            asyncio.get_running_loop().create_task(self._drift(command.params))
        if self.faults.has("offline") and not was_offline:
            self._go_offline()
        elif was_offline and not self.faults.has("offline"):
            self._powered.set()

    def _go_offline(self) -> None:
        """Drop the TCP connection without DISCONNECT, so the broker publishes our LWT."""
        self._powered.clear()
        client = self._client
        if client is None:
            return
        with contextlib.suppress(Exception):
            client._client.socket().shutdown(socket.SHUT_RDWR)

    async def _drift(self, params: dict[str, Any]) -> None:
        writable = {c.name: c for c in self.device.type.writable}
        change = params.get("state") or drift_change(self.state, writable, self.rng)
        if change:
            self.state.update(change)
            log.info("%s: drift %s", self.id, change)
            await self.publish_state()

    def _remember(self, cmd_id: str, ack: dict[str, Any]) -> None:
        self._seen[cmd_id] = ack
        while len(self._seen) > SEEN_COMMANDS:
            self._seen.popitem(last=False)
