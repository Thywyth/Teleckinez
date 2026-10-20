import asyncio
import os
import socket
import sys

import pytest

# Override to test against another broker, e.g. TKZ_MQTT_PORT=1884 with teleckinez.broker.
MQTT_HOST = os.environ.get("TKZ_MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("TKZ_MQTT_PORT", "1883"))


def pytest_asyncio_loop_factories(config, item):
    # aiomqtt (paho) needs add_reader/add_writer, which ProactorEventLoop lacks.
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return None


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture
def mqtt_broker() -> tuple[str, int]:
    """Address of a running broker; skip the test if none is listening."""
    if not _port_open(MQTT_HOST, MQTT_PORT):
        pytest.skip(f"no MQTT broker on {MQTT_HOST}:{MQTT_PORT} (docker compose up -d mosquitto)")
    return MQTT_HOST, MQTT_PORT


@pytest.fixture(scope="session")
def registry():
    from teleckinez.core.config import CONFIG_DIR, load_devices
    from teleckinez.devices.registry import DeviceRegistry

    return DeviceRegistry(load_devices(CONFIG_DIR / "devices.yaml"))


async def clear_retained(host: str, port: int, prefix: str) -> None:
    """Remove retained messages under `prefix/` so tests do not leak into each other."""
    import aiomqtt

    topics: set[str] = set()
    async with aiomqtt.Client(host, port) as client:
        await client.subscribe(f"{prefix}/#")
        try:
            async with asyncio.timeout(0.3):
                async for message in client.messages:
                    if message.retain and message.payload:
                        topics.add(message.topic.value)
        except TimeoutError:
            pass
        for topic in topics:
            await client.publish(topic, b"", qos=1, retain=True)


@pytest.fixture
async def mqtt_settings(mqtt_broker):
    """MqttSettings pointing at the test broker with a unique topic prefix."""
    import uuid

    from teleckinez.core.config import MqttSettings

    host, port = mqtt_broker
    settings = MqttSettings(host=host, port=port, prefix=f"tkz-test/{uuid.uuid4().hex[:8]}")
    yield settings
    await clear_retained(host, port, settings.prefix)
