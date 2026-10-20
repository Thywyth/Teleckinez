import asyncio
import json
import random
import time

import aiomqtt
import pytest

from simulator.home import SimHome
from simulator.physics import ClimateSensor, MotionSensor, Thermostat, Vacuum, WaterHeater
from teleckinez.core.config import DevicesConfig, HealthSettings, Settings, SimulatorSettings
from teleckinez.devices.registry import DeviceRegistry
from teleckinez.devices.types import DEVICE_TYPES
from teleckinez.transport.adapters.native import Topics

HOME = DevicesConfig.model_validate(
    {
        "rooms": {"kitchen": {"uk": ["кухня"]}},
        "devices": [
            {"id": "light.k", "type": "light", "room": "kitchen", "names": {"uk": ["світло"]}},
            {
                "id": "curtains.k",
                "type": "curtains",
                "room": "kitchen",
                "names": {"uk": ["штори"]},
                "sim": {"speed": 200},
            },
            {"id": "lock.k", "type": "door_lock", "room": "kitchen", "names": {"uk": ["замок"]}},
        ],
    }
)


@pytest.fixture
async def home(mqtt_settings):
    settings = Settings(
        mqtt=mqtt_settings,
        health=HealthSettings(heartbeat_s=0.5),
        simulator=SimulatorSettings(latency_ms=(1, 2)),
    )
    async with SimHome(DeviceRegistry(HOME), settings, seed=1) as sim:
        yield sim


class Probe:
    """A raw MQTT client that plays the assistant in these tests."""

    def __init__(self, client: aiomqtt.Client, topics: Topics) -> None:
        self.client, self.topics = client, topics

    async def send(self, device_id: str, cmd_id: str, params: dict, **extra) -> None:
        cmd = {"cmd_id": cmd_id, "action": "set", "params": params, "ts": time.time(), "ttl_s": 5}
        cmd.update(extra)
        await self.client.publish(self.topics.device(device_id, "cmd"), json.dumps(cmd), qos=1)

    async def collect(self, wait_s: float, device: str | None = None) -> list:
        """(kind, payload) of messages received within wait_s, optionally for one device."""
        out = []
        try:
            async with asyncio.timeout(wait_s):
                async for m in self.client.messages:
                    device_id, kind = self.topics.parse(m.topic.value)
                    if device and device_id != device:
                        continue
                    raw = m.payload.decode()
                    out.append((kind, raw if kind == "availability" else json.loads(raw)))
        except TimeoutError:
            pass
        return out


@pytest.fixture
async def probe(mqtt_settings, home):
    topics = Topics(mqtt_settings.prefix)
    async with aiomqtt.Client(mqtt_settings.host, mqtt_settings.port) as client:
        await client.subscribe(f"{mqtt_settings.prefix}/dev/+/ack", qos=1)
        await client.subscribe(f"{mqtt_settings.prefix}/dev/+/state", qos=1)
        p = Probe(client, topics)
        await p.collect(0.2)  # drop retained states
        yield p


@pytest.mark.integration
async def test_announces_meta_availability_state(mqtt_settings, home) -> None:
    async with aiomqtt.Client(mqtt_settings.host, mqtt_settings.port) as client:
        await client.subscribe(f"{mqtt_settings.prefix}/dev/light.k/#", qos=1)
        got = await Probe(client, Topics(mqtt_settings.prefix)).collect(0.3)
    kinds = dict(got)
    assert kinds["availability"] == "online"
    assert kinds["meta"]["type"] == "light"
    assert kinds["meta"]["capabilities"]["brightness"]["max"] == 100
    assert kinds["state"]["on"] is False and kinds["state"]["seq"] >= 1


@pytest.mark.integration
async def test_command_ack_then_state(probe) -> None:
    await probe.send("light.k", "c1", {"on": True, "brightness": 40})
    got = await probe.collect(0.3, "light.k")
    assert got[0] == ("ack", {"cmd_id": "c1", "status": "accepted", "ts": got[0][1]["ts"]})
    state = next(p for k, p in got if k == "state")
    assert state["on"] is True and state["brightness"] == 40


@pytest.mark.integration
async def test_idempotent_by_cmd_id(probe) -> None:
    # toggle is not idempotent by itself, so a repeated cmd_id must not run it twice
    await probe.send("light.k", "c-t", {}, action="toggle")
    await probe.send("light.k", "c-t", {}, action="toggle")
    got = await probe.collect(0.3, "light.k")
    assert [p["cmd_id"] for k, p in got if k == "ack"] == ["c-t", "c-t"]
    states = [p for k, p in got if k == "state"]
    assert states and all(s["on"] is True for s in states)


@pytest.mark.integration
async def test_expired_command_ignored(probe) -> None:
    await probe.send("light.k", "old", {"on": True}, ts=time.time() - 60)
    assert [k for k, _ in await probe.collect(0.2, "light.k")] in ([], ["state"])  # heartbeat


@pytest.mark.integration
async def test_rejects_invalid_values(probe) -> None:
    await probe.send("light.k", "bad1", {"brightness": 400})
    await probe.send("light.k", "bad2", {"colour": "red"})
    acks = [p for k, p in await probe.collect(0.3, "light.k") if k == "ack"]
    assert [(a["status"], a["error"]) for a in acks] == [
        ("rejected", "value_out_of_range"),
        ("rejected", "unsupported_action"),
    ]


@pytest.mark.integration
async def test_curtains_report_intermediate_positions(probe) -> None:
    await probe.send("curtains.k", "open", {"position": 100})
    states = [p for k, p in await probe.collect(1.2, "curtains.k") if k == "state"]
    positions = [s["position"] for s in states]
    assert states[0]["moving"] is True
    assert any(0 < p < 100 for p in positions)  # at least one intermediate report
    assert states[-1] == {**states[-1], "position": 100, "moving": False}


@pytest.mark.integration
async def test_heartbeat_republishes_state(probe) -> None:
    states = [p for k, p in await probe.collect(1.2, "lock.k") if k == "state"]
    assert len(states) >= 2
    assert states[1]["seq"] > states[0]["seq"]


@pytest.mark.integration
async def test_graceful_stop_reports_offline(mqtt_settings) -> None:
    settings = Settings(mqtt=mqtt_settings, simulator=SimulatorSettings(latency_ms=(1, 2)))
    sim = SimHome(DeviceRegistry(HOME), settings, only={"lock.k"})
    await sim.start()
    async with aiomqtt.Client(mqtt_settings.host, mqtt_settings.port) as client:
        await client.subscribe(Topics(mqtt_settings.prefix).device("lock.k", "availability"))
        assert (await anext(aiter(client.messages))).payload == b"online"
        await sim.stop()
        async with asyncio.timeout(2):
            assert (await anext(aiter(client.messages))).payload == b"offline"


# ---------------------------------------------------------------- physics (unit)


def test_thermostat_follows_target() -> None:
    state = DEVICE_TYPES["thermostat"].initial_state() | {"target_temp": 22.0}
    t = Thermostat(state, {}, random.Random(0))
    assert t.tick(1.0) is True and state["heating"] is True
    for _ in range(100):
        t.tick(1.0)
    assert state["current_temp"] == 22.0 and state["heating"] is False


def test_kettle_switches_off_at_boiling() -> None:
    state = {"on": True, "water_temp": 95.0}
    k = WaterHeater(state, {}, random.Random(0), heat_rate=1.5, max_temp=100, auto_off=True)
    changed = [k.tick(1.0) for _ in range(5)]
    assert state == {"on": False, "water_temp": 100.0} and changed[-1] in (True, False)
    assert any(changed)


def test_vacuum_battery_drains_while_cleaning() -> None:
    state = {"status": "cleaning", "battery": 100}
    v = Vacuum(state, {}, random.Random(0))
    for _ in range(100):
        v.tick(1.0)
    assert state["battery"] == 90


def test_sensors_stay_in_range_and_motion_toggles() -> None:
    state = {"temperature": 21.0, "humidity": 45.0}
    s = ClimateSensor(state, {}, random.Random(0))
    for _ in range(1000):
        s.tick(1.0)
    assert 20.5 <= state["temperature"] <= 21.5 and 43 <= state["humidity"] <= 47
    motion = {"motion": False}
    m = MotionSensor(motion, {"period_s": 2, "hold_s": 1}, random.Random(0))
    assert any(m.tick(1.0) for _ in range(50))


def test_client_ids_are_unique_per_prefix(registry) -> None:
    from simulator.device import SimDevice

    dev = registry.get("light.kitchen_main")
    a = SimDevice(dev, host="h", port=1, topics=Topics("tkz"))
    b = SimDevice(dev, host="h", port=1, topics=Topics("tkz-test/abc"))
    assert a.client_id == "sim-tkz-light.kitchen_main"
    assert a.client_id != b.client_id


def test_assistant_client_ids_differ() -> None:
    from teleckinez.core.config import MqttSettings
    from teleckinez.transport.mqtt_client import MqttClient

    assert MqttClient(MqttSettings()).client_id != MqttClient(MqttSettings()).client_id
