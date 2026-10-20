"""Broker sanity checks: the MQTT features protocol.md relies on."""

import asyncio
import uuid

import aiomqtt
import paho.mqtt.client as paho
import pytest

pytestmark = pytest.mark.integration


async def _first_message(client: aiomqtt.Client, wait_s: float = 3.0) -> aiomqtt.Message:
    async with asyncio.timeout(wait_s):
        async for message in client.messages:
            return message
    raise AssertionError("no message")


async def test_publish_subscribe_qos1(mqtt_broker) -> None:
    host, port = mqtt_broker
    topic = f"tkz/test/{uuid.uuid4().hex}/cmd"
    async with aiomqtt.Client(host, port) as sub, aiomqtt.Client(host, port) as pub:
        await sub.subscribe(topic, qos=1)
        await pub.publish(topic, b'{"action": "set"}', qos=1)
        message = await _first_message(sub)
    assert message.payload == b'{"action": "set"}'
    assert message.qos == 1


async def test_retained_state_delivered_to_late_subscriber(mqtt_broker) -> None:
    host, port = mqtt_broker
    topic = f"tkz/test/{uuid.uuid4().hex}/state"
    async with aiomqtt.Client(host, port) as pub:
        await pub.publish(topic, b'{"on": true}', qos=1, retain=True)
    async with aiomqtt.Client(host, port) as sub:
        await sub.subscribe(topic, qos=1)
        message = await _first_message(sub)
        assert message.payload == b'{"on": true}'
        assert message.retain
        await sub.publish(topic, b"", qos=1, retain=True)  # clear retained message


async def test_last_will_on_unclean_disconnect(mqtt_broker) -> None:
    host, port = mqtt_broker
    topic = f"tkz/test/{uuid.uuid4().hex}/availability"
    async with aiomqtt.Client(host, port) as watcher:
        await watcher.subscribe(topic, qos=1)
        device = paho.Client(paho.CallbackAPIVersion.VERSION2)
        device.will_set(topic, b"offline", qos=1)
        device.connect(host, port)
        device.loop(timeout=1.0)  # send CONNECT and read CONNACK
        assert device.is_connected()
        # Simulate a crash: drop the TCP connection without sending DISCONNECT.
        device.socket().close()
        message = await _first_message(watcher, wait_s=5.0)
    assert message.payload == b"offline"
