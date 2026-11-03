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


async def test_embedded_broker_survives_concurrent_connects() -> None:
    """Load check: 60 device-like clients connect, retain and subscribe at once."""
    port = _free_port()
    ready = asyncio.Event()
    task = asyncio.create_task(run_broker("127.0.0.1", port, ready))
    n = 60

    async def client(i: int) -> bytes:
        # like a simulated device: retained state + own subscriptions, all at once
        async with aiomqtt.Client("127.0.0.1", port, identifier=f"c{i}") as c:
            await c.publish(f"tkz/race/{i}/state", str(i).encode(), qos=1, retain=True)
            for k in range(3):
                await c.subscribe(f"tkz/race/{i}/cmd{k}", qos=1)
            await c.subscribe(f"tkz/race/{i}/state", qos=1)
            async with asyncio.timeout(10):
                message = await anext(aiter(c.messages))
            return message.payload

    try:
        await asyncio.wait_for(ready.wait(), 5)
        payloads = await asyncio.gather(*(client(i) for i in range(n)))
        assert sorted(int(p) for p in payloads) == list(range(n))
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_embedded_broker_replays_only_own_retained_filters() -> None:
    """amqtt 0.12.1 replays retained messages of other clients' filters on connect."""
    port = _free_port()
    ready = asyncio.Event()
    task = asyncio.create_task(run_broker("127.0.0.1", port, ready))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        async with aiomqtt.Client("127.0.0.1", port, identifier="a") as a:
            await a.publish("tkz/a/state", b"a", qos=1, retain=True)
            await a.subscribe("tkz/a/#", qos=1)
            async with aiomqtt.Client("127.0.0.1", port, identifier="b") as b:
                await b.subscribe("tkz/b/#", qos=1)
                await b.publish("tkz/b/x", b"b", qos=1)
                async with asyncio.timeout(3):
                    message = await anext(aiter(b.messages))
        assert message.payload == b"b"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
