"""MQTT client for the assistant: reconnects, resubscribes, has its own Last Will."""

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from typing import Any

import aiomqtt

from teleckinez.core.config import MqttSettings

log = logging.getLogger(__name__)

MessageHandler = Callable[[str, bytes], None]


class MqttNotConnected(ConnectionError):
    pass


def encode(payload: Any) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode()
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


class MqttClient:
    """Long-lived connection. Use as `async with MqttClient(settings) as mqtt: ...`.

    The connection runs in a background task and is re-established after
    broker restarts; subscriptions are restored on every reconnect. The
    client announces itself as `online` on `availability_topic` (retained)
    and leaves `offline` there as its Last Will.
    """

    def __init__(
        self,
        settings: MqttSettings,
        *,
        client_id: str | None = None,
        availability_topic: str | None = None,
        reconnect_s: tuple[float, float] = (0.5, 10.0),
    ) -> None:
        self.settings = settings
        self.client_id = client_id or settings.client_id
        self.availability_topic = availability_topic
        self._reconnect_s = reconnect_s
        self._handlers: list[tuple[str, MessageHandler]] = []
        self._client: aiomqtt.Client | None = None
        self._task: asyncio.Task | None = None
        self.connected = asyncio.Event()

    # ------------------------------------------------------------------ public

    def subscribe(self, topic_filter: str, handler: MessageHandler) -> None:
        """Call `handler(topic, payload)` for messages matching the filter (sync, keep it fast)."""
        self._handlers.append((topic_filter, handler))
        if self._client is not None and self.connected.is_set():
            asyncio.get_running_loop().create_task(self._subscribe_one(topic_filter))

    async def publish(
        self, topic: str, payload: Any, *, qos: int = 1, retain: bool = False
    ) -> None:
        if self._client is None or not self.connected.is_set():
            raise MqttNotConnected(f"not connected to {self.settings.host}:{self.settings.port}")
        try:
            await self._client.publish(topic, encode(payload), qos=qos, retain=retain)
        except aiomqtt.MqttError as exc:
            raise MqttNotConnected(str(exc)) from exc

    async def start(self, wait_s: float | None = 5.0) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"mqtt:{self.client_id}")
        if wait_s:
            try:
                await asyncio.wait_for(self.connected.wait(), wait_s)
            except TimeoutError:
                raise MqttNotConnected(
                    f"cannot connect to MQTT broker {self.settings.host}:{self.settings.port} "
                    "(is it running? docker compose up -d mosquitto)"
                ) from None

    async def stop(self) -> None:
        if self._client is not None and self.connected.is_set() and self.availability_topic:
            with contextlib.suppress(Exception):
                await self.publish(self.availability_topic, "offline", retain=True)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def __aenter__(self) -> "MqttClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ---------------------------------------------------------------- internal

    def _make_client(self) -> aiomqtt.Client:
        will = None
        if self.availability_topic:
            will = aiomqtt.Will(self.availability_topic, b"offline", qos=1, retain=True)
        client = aiomqtt.Client(
            self.settings.host,
            self.settings.port,
            identifier=self.client_id,
            username=self.settings.username,
            password=self.settings.password,
            keepalive=self.settings.keepalive_s,
            will=will,
        )
        # A group command ("вимкни все світло") publishes dozens of QoS 1 messages at
        # once; aiomqtt warns above 10 in flight, which is normal here.
        client.pending_calls_threshold = 200
        return client

    async def _subscribe_one(self, topic_filter: str) -> None:
        assert self._client is not None
        await self._client.subscribe(topic_filter, qos=1)

    async def _run(self) -> None:
        delay, max_delay = self._reconnect_s
        while True:
            try:
                async with self._make_client() as client:
                    self._client = client
                    for topic_filter in dict.fromkeys(f for f, _ in self._handlers):
                        await client.subscribe(topic_filter, qos=1)
                    if self.availability_topic:
                        await client.publish(self.availability_topic, b"online", qos=1, retain=True)
                    self.connected.set()
                    delay = self._reconnect_s[0]
                    log.info("MQTT connected to %s:%s", self.settings.host, self.settings.port)
                    async for message in client.messages:
                        self._dispatch(message)
            except aiomqtt.MqttError as exc:
                log.warning("MQTT connection lost (%s), retrying in %.1f s", exc, delay)
            finally:
                self.connected.clear()
                self._client = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)

    def _dispatch(self, message: aiomqtt.Message) -> None:
        payload = message.payload if isinstance(message.payload, bytes) else b""
        for topic_filter, handler in self._handlers:
            if message.topic.matches(topic_filter):
                try:
                    handler(message.topic.value, payload)
                except Exception:
                    log.exception("MQTT handler failed for %s", message.topic.value)
