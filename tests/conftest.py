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
