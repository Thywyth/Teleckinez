import asyncio
import os
import socket
import sys
from pathlib import Path

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
        await asyncio.gather(*(client.publish(t, b"", qos=1, retain=True) for t in topics))


@pytest.fixture
async def mqtt_settings(mqtt_broker):
    """MqttSettings pointing at the test broker with a unique topic prefix."""
    import uuid

    from teleckinez.core.config import MqttSettings

    host, port = mqtt_broker
    settings = MqttSettings(host=host, port=port, prefix=f"tkz-test/{uuid.uuid4().hex[:8]}")
    yield settings
    await clear_retained(host, port, settings.prefix)


MODELS = Path(__file__).resolve().parents[1] / "models"
PIPER_VOICES = {
    "uk": MODELS / "piper/uk_UA-ukrainian_tts-medium/uk_UA-ukrainian_tts-medium.onnx",
    "en": MODELS / "piper/en_US-lessac-medium/en_US-lessac-medium.onnx",
}


@pytest.fixture(scope="session")
def speech():
    """speech(text, lang) -> 16 kHz int16 numpy audio synthesized by Piper (cached)."""
    if not all(p.is_file() for p in PIPER_VOICES.values()):
        pytest.skip("Piper voices missing (python scripts/download_models.py)")
    import numpy as np
    from piper import PiperVoice
    from piper.voice import ESPEAK_DATA_DIR

    from teleckinez.voice.audio_io import SAMPLE_RATE, resample
    from teleckinez.voice.paths import ascii_safe_path

    voices = {
        lang: PiperVoice.load(path, espeak_data_dir=ascii_safe_path(ESPEAK_DATA_DIR))
        for lang, path in PIPER_VOICES.items()
    }
    cache: dict = {}

    def synth(text: str, lang: str = "uk"):
        if (text, lang) not in cache:
            voice = voices[lang]
            chunks = [
                c.audio_int16_array
                for c in voice.synthesize(text.lower() if lang == "uk" else text)
            ]
            audio = np.concatenate(chunks)
            cache[(text, lang)] = resample(audio, voice.config.sample_rate, SAMPLE_RATE)
        return cache[(text, lang)]

    return synth
