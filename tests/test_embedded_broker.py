"""The amqtt fallback broker supports the same MQTT features as Mosquitto."""

import asyncio
import socket

import aiomqtt
import pytest

pytest.importorskip("amqtt")

from teleckinez.broker import run_broker


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_embedded_broker_retained_roundtrip() -> None:
    port = _free_port()
    ready = asyncio.Event()
    task = asyncio.create_task(run_broker("127.0.0.1", port, ready))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        async with aiomqtt.Client("127.0.0.1", port) as pub:
            await pub.publish("tkz/dev/light.test/state", b'{"on": true}', qos=1, retain=True)
        async with aiomqtt.Client("127.0.0.1", port) as sub:
            await sub.subscribe("tkz/dev/+/state", qos=1)
            async with asyncio.timeout(3):
                message = await anext(aiter(sub.messages))
        assert message.payload == b'{"on": true}'
        assert message.retain
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
