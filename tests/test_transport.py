import asyncio
import json

import aiomqtt
import pytest

from teleckinez.transport.adapters.native import NativeAdapter, Topics
from teleckinez.transport.gateway import DeviceGateway
from teleckinez.transport.messages import (
    AckReport,
    AvailabilityReport,
    MetaReport,
    StateReport,
)
from teleckinez.transport.mqtt_client import MqttClient, MqttNotConnected

# ------------------------------------------------------------------ native adapter


def test_topics_parse() -> None:
    topics = Topics("tkz-test/abc")
    assert topics.device("light.k", "state") == "tkz-test/abc/dev/light.k/state"
    assert topics.parse("tkz-test/abc/dev/light.k/state") == ("light.k", "state")
    assert topics.parse("tkz/dev/light.k/state") is None
    assert topics.parse("tkz-test/abc/sim/faults") is None


def test_encode_command() -> None:
    topic, payload = NativeAdapter("tkz").encode_command(
        "curtains.bedroom", "c-1", {"position": 100}, 5, 1761000000.05
    )
    assert topic == "tkz/dev/curtains.bedroom/cmd"
    assert json.loads(payload) == {
        "cmd_id": "c-1",
        "action": "set",
        "params": {"position": 100},
        "ts": 1761000000.05,
        "ttl_s": 5,
    }


def test_decode_messages() -> None:
    a = NativeAdapter("tkz")
    assert a.decode("tkz/dev/l.k/state", b'{"on": true, "ts": 1.5, "seq": 7}') == [
        StateReport("l.k", {"on": True}, 1.5, 7)
    ]
    assert a.decode("tkz/dev/l.k/ack", b'{"cmd_id": "c", "status": "rejected", "error": "x"}') == [
        AckReport("l.k", "c", "rejected", "x", None)
    ]
    assert a.decode("tkz/dev/l.k/availability", b"offline") == [AvailabilityReport("l.k", False)]
    assert a.decode("tkz/dev/l.k/meta", b'{"type": "light"}') == [
        MetaReport("l.k", {"type": "light"})
    ]


@pytest.mark.parametrize(
    ("topic", "payload"),
    [
        ("tkz/dev/l.k/state", b""),  # cleared retained message
        ("tkz/dev/l.k/state", b"{not json"),
        ("tkz/dev/l.k/state", b"[1, 2]"),
        ("tkz/dev/l.k/ack", b'{"cmd_id": "c", "status": "maybe"}'),
        ("tkz/dev/l.k/availability", b"sleeping"),
        ("other/topic", b"{}"),
    ],
)
def test_decode_ignores_garbage(topic: str, payload: bytes) -> None:
    assert NativeAdapter("tkz").decode(topic, payload) == []


# --------------------------------------------------------------- with a broker


async def _next(client: aiomqtt.Client, wait_s: float = 3.0) -> aiomqtt.Message:
    async with asyncio.timeout(wait_s):
        return await anext(aiter(client.messages))


@pytest.mark.integration
async def test_gateway_roundtrip_with_fake_device(mqtt_settings, registry) -> None:
    topics = Topics(mqtt_settings.prefix)
    received: list = []
    mqtt = MqttClient(mqtt_settings, availability_topic=topics.assistant_availability)
    from teleckinez.app import build_adapters
    from teleckinez.core.config import Settings

    gateway = DeviceGateway(mqtt, registry, build_adapters(registry, Settings(mqtt=mqtt_settings)))
    two = asyncio.Event()

    def listen(message) -> None:
        received.append(message)
        if len(received) >= 2:
            two.set()

    gateway.add_listener(listen)

    async with (
        aiomqtt.Client(mqtt_settings.host, mqtt_settings.port) as device,
        mqtt,
    ):
        await device.subscribe(topics.device("light.kitchen_main", "cmd"), qos=1)
        await gateway.send("light.kitchen_main", "c-42", {"on": True}, ttl_s=5)
        cmd = json.loads((await _next(device)).payload)
        assert cmd["cmd_id"] == "c-42" and cmd["params"] == {"on": True}

        await device.publish(
            topics.device("light.kitchen_main", "ack"), b'{"cmd_id": "c-42", "status": "accepted"}'
        )
        await device.publish(
            topics.device("light.kitchen_main", "state"),
            b'{"on": true, "brightness": 100, "seq": 1}',
        )
        await device.publish(topics.device("ghost.device", "state"), b'{"on": true}')
        await asyncio.wait_for(two.wait(), 3)
        await asyncio.sleep(0.1)

    assert received == [
        AckReport("light.kitchen_main", "c-42", "accepted"),
        StateReport("light.kitchen_main", {"on": True, "brightness": 100}, None, 1),
    ]  # message from an unknown device was dropped


@pytest.mark.integration
async def test_assistant_availability_online_then_offline(mqtt_settings) -> None:
    topic = Topics(mqtt_settings.prefix).assistant_availability
    async with aiomqtt.Client(mqtt_settings.host, mqtt_settings.port) as watcher:
        await watcher.subscribe(topic, qos=1)
        mqtt = MqttClient(mqtt_settings, availability_topic=topic)
        await mqtt.start()
        assert (await _next(watcher)).payload == b"online"
        await mqtt.stop()
        assert (await _next(watcher)).payload == b"offline"


async def test_publish_without_connection_raises() -> None:
    from teleckinez.core.config import MqttSettings

    mqtt = MqttClient(MqttSettings(port=1))
    with pytest.raises(MqttNotConnected):
        await mqtt.publish("x", {"a": 1})
    with pytest.raises(MqttNotConnected, match="docker compose"):
        await mqtt.start(wait_s=0.3)
    await mqtt.stop()
