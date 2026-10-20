"""Fault injection (devices.md §4): every fault and what the assistant must detect."""

import asyncio
import json
import random
from pathlib import Path

import aiomqtt
import pytest

from simulator.faults import (
    FaultCommand,
    FaultError,
    FaultSet,
    drift_change,
    load_scenario,
    parse_fault,
)
from simulator.home import SimHome
from teleckinez.app import App
from teleckinez.core.config import (
    CONFIG_DIR,
    DispatcherSettings,
    HealthSettings,
    Settings,
    SimulatorSettings,
    load_devices,
)
from teleckinez.core.events import DeviceStateChanged
from teleckinez.core.models import Availability, DeviceCommand
from teleckinez.core.models import CommandStatus as S
from teleckinez.devices.types import DEVICE_TYPES

LIGHT, LOCK = "light.kitchen_main", "lock.front_door"

# ------------------------------------------------------------------------- unit


def test_parse_fault_with_defaults_and_validation() -> None:
    cmd = parse_fault({"device_id": LIGHT, "fault": "drop_rate"})
    assert cmd == FaultCommand(LIGHT, "drop_rate", {"p": 0.5}, True)
    assert parse_fault(b'{"device_id": "*", "fault": "slow", "params": {"ms": 100}}').params == {
        "ms": 100
    }
    for bad in (
        {"fault": "offline"},
        {"device_id": LIGHT, "fault": "explode"},
        {"device_id": LIGHT, "fault": "drop_rate", "params": {"p": 2}},
        b"not json",
    ):
        with pytest.raises(FaultError):
            parse_fault(bad)


def test_fault_set_combines_and_clears() -> None:
    faults = FaultSet()
    faults.apply(parse_fault({"device_id": LIGHT, "fault": "slow", "params": {"ms": 1500}}))
    faults.apply(parse_fault({"device_id": LIGHT, "fault": "drop_rate", "params": {"p": 1}}))
    assert faults.delay_s() == 1.5 and faults.drops(random.Random(0)) and not faults.silent
    faults.apply(parse_fault({"device_id": LIGHT, "fault": "slow", "enabled": False}))
    assert faults.describe() == {"drop_rate": {"p": 1}}
    faults.apply(parse_fault({"device_id": LIGHT, "fault": "freeze"}))
    assert faults.silent
    faults.apply(parse_fault({"device_id": LIGHT, "fault": "clear"}))
    assert faults.active == {}


def test_drift_change_is_plausible() -> None:
    light = {c.name: c for c in DEVICE_TYPES["light"].writable}
    assert drift_change({"on": True}, light, random.Random(0)) == {"on": False}
    lock = {c.name: c for c in DEVICE_TYPES["door_lock"].writable}
    assert drift_change({"locked": True}, lock, random.Random(0)) == {"locked": False}
    thermostat = {c.name: c for c in DEVICE_TYPES["thermostat"].writable}
    value = drift_change({}, thermostat, random.Random(0))["target_temp"]
    assert 16 <= value <= 30


def test_scenario_file(tmp_path: Path) -> None:
    path = tmp_path / "s.yaml"
    path.write_text(
        "steps:\n"
        "  - {at_s: 5, device_id: lock.front_door, fault: clear}\n"
        "  - {at_s: 1, device_id: lock.front_door, fault: offline}\n",
        encoding="utf-8",
    )
    steps = load_scenario(path)
    assert [(s.at_s, s.command.fault) for s in steps] == [(1.0, "offline"), (5.0, "clear")]


# -------------------------------------------------------------- with a broker


@pytest.fixture
async def house(mqtt_settings, tmp_path):
    settings = Settings(
        mqtt=mqtt_settings,
        dispatcher=DispatcherSettings(
            ack_timeout_s=0.5, retry_backoff_s=[0.1, 0.1], state_timeout_s={"default": 1.0}
        ),
        health=HealthSettings(heartbeat_s=0.4, check_interval_s=0.1),
        simulator=SimulatorSettings(latency_ms=(2, 5)),
    )
    settings.storage.path = tmp_path / "f.db"
    app = App(settings, load_devices(CONFIG_DIR / "devices.yaml"))
    sim = SimHome(app.registry, settings, only={LIGHT, LOCK}, seed=7)
    await sim.start()
    await app.start(settle_s=0.4)
    try:
        yield app, sim
    finally:
        await app.stop()
        await sim.stop()


async def run(app: App, device_id: str, action: str, params: dict | None = None):
    group = await app.dispatcher.dispatch([DeviceCommand(device_id, action, params or {})])
    (record,) = await group.wait(15)
    return record


async def until(predicate, wait_s: float = 5.0) -> None:
    async with asyncio.timeout(wait_s):
        while not predicate():  # noqa: ASYNC110 - polls state owned by other components
            await asyncio.sleep(0.02)


@pytest.mark.integration
async def test_offline_sends_lwt_and_recovers(house) -> None:
    app, sim = house
    sim.apply_fault(parse_fault({"device_id": LOCK, "fault": "offline"}))
    await until(lambda: app.twins.get(LOCK).availability is Availability.OFFLINE)
    record = await run(app, LOCK, "lock")
    assert record.status is S.DEVICE_OFFLINE and record.attempts == 0
    sim.apply_fault(parse_fault({"device_id": LOCK, "fault": "clear"}))
    await until(lambda: app.twins.get(LOCK).availability is Availability.ONLINE)
    assert (await run(app, LOCK, "unlock")).status is S.COMPLETED


@pytest.mark.integration
async def test_freeze_gives_timeout_ack_then_stale(house) -> None:
    app, sim = house
    sim.apply_fault(parse_fault({"device_id": LIGHT, "fault": "freeze"}))
    record = await run(app, LIGHT, "turn_on")
    assert record.status is S.TIMEOUT_ACK and record.attempts == 3
    await until(lambda: app.twins.get(LIGHT).availability is Availability.STALE)


@pytest.mark.integration
async def test_drop_rate_is_recovered_by_retries(house) -> None:
    app, sim = house
    sim.devices[LIGHT].rng = random.Random(1)  # first draw < 0.5 (dropped), second >= 0.5
    sim.apply_fault(parse_fault({"device_id": LIGHT, "fault": "drop_rate", "params": {"p": 0.5}}))
    record = await run(app, LIGHT, "turn_on")
    assert record.status is S.COMPLETED and record.attempts >= 2
    sim.apply_fault(parse_fault({"device_id": LIGHT, "fault": "drop_rate", "params": {"p": 1}}))
    assert (await run(app, LIGHT, "turn_off")).status is S.TIMEOUT_ACK


@pytest.mark.integration
async def test_stuck_gives_timeout_state(house) -> None:
    app, sim = house
    sim.apply_fault(parse_fault({"device_id": LIGHT, "fault": "stuck"}))
    record = await run(app, LIGHT, "turn_on")
    assert [s for s, _ in record.history][-2:] == [S.ACCEPTED, S.TIMEOUT_STATE]
    assert sim.devices[LIGHT].state["on"] is False


@pytest.mark.integration
async def test_slow_depends_on_ack_timeout(house) -> None:
    app, sim = house
    sim.apply_fault(parse_fault({"device_id": LIGHT, "fault": "slow", "params": {"ms": 200}}))
    assert (await run(app, LIGHT, "turn_on")).status is S.COMPLETED  # 200 ms < ack timeout 500
    sim.apply_fault(parse_fault({"device_id": LIGHT, "fault": "slow", "params": {"ms": 3000}}))
    record = await run(app, LIGHT, "turn_off")
    assert record.status is S.TIMEOUT_ACK  # 3 attempts x (0.5 s + backoff) < 3 s delay


@pytest.mark.integration
async def test_reject_is_explained(house) -> None:
    app, sim = house
    sim.apply_fault(parse_fault({"device_id": LIGHT, "fault": "reject"}))
    record = await run(app, LIGHT, "turn_on")
    assert record.status is S.REJECTED and record.error == "device_error"
    assert app.composer.command_result([record]) == (
        "Світло на кухні: пристрій відхилив команду (внутрішня помилка пристрою)."
    )


@pytest.mark.integration
async def test_drift_is_reported_as_drift(house) -> None:
    app, sim = house
    drifts: list = []
    app.bus.subscribe(drifts.append, DeviceStateChanged)
    sim.apply_fault(parse_fault({"device_id": LIGHT, "fault": "drift"}))
    await until(lambda: any(e.source == "drift" for e in drifts))
    assert app.twins.get(LIGHT).reported["on"] is True  # it was off, "someone" switched it on


@pytest.mark.integration
async def test_faults_via_mqtt_topic_and_state_topic(house, mqtt_settings) -> None:
    _, sim = house
    topics = sim.topics
    async with aiomqtt.Client(mqtt_settings.host, mqtt_settings.port) as panel:
        await panel.subscribe(topics.sim_faults_state, qos=1)
        await panel.publish(
            topics.sim_faults, json.dumps({"device_id": LIGHT, "fault": "stuck"}), qos=1
        )
        await panel.publish(topics.sim_faults, b'{"device_id": "x", "fault": "nonsense"}', qos=1)
        async with asyncio.timeout(3):
            async for message in panel.messages:
                state = json.loads(message.payload or b"{}")
                if state:
                    break
    assert state == {LIGHT: {"stuck": {}}}
    assert sim.devices[LIGHT].faults.has("stuck")
