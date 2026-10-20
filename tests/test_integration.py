"""End-to-end: real MQTT broker + simulator process logic + the whole assistant (ROADMAP 1.14)."""

import asyncio

import pytest

from simulator.home import SimHome
from teleckinez.app import App
from teleckinez.core.config import (
    CONFIG_DIR,
    DispatcherSettings,
    HealthSettings,
    Settings,
    SimulatorSettings,
    StorageSettings,
    load_devices,
)
from teleckinez.core.events import CommandFinished, SpeakRequested
from teleckinez.core.models import Availability
from teleckinez.core.models import CommandStatus as S

pytestmark = pytest.mark.integration


@pytest.fixture
async def house(mqtt_settings, tmp_path):
    """The real devices.yaml home: 31 simulated devices + the assistant, isolated by prefix."""
    settings = Settings(
        mqtt=mqtt_settings,
        dispatcher=DispatcherSettings(ack_timeout_s=1.0, retry_backoff_s=[0.2, 0.4]),
        health=HealthSettings(heartbeat_s=2.0),
        simulator=SimulatorSettings(latency_ms=(5, 20)),
        storage=StorageSettings(path=tmp_path / "test.db"),
    )
    devices = load_devices(CONFIG_DIR / "devices.yaml")
    app = App(settings, devices)
    sim = SimHome(app.registry, settings, seed=42)
    await sim.start()
    await app.start(settle_s=0.5)
    finished: list[CommandFinished] = []
    spoken: list[SpeakRequested] = []
    app.bus.subscribe(finished.append, CommandFinished)
    app.bus.subscribe(spoken.append, SpeakRequested)
    try:
        yield app, sim, finished, spoken
    finally:
        await app.stop()
        await sim.stop()


async def test_turn_on_kitchen_light_completes(house) -> None:
    app, sim, finished, _ = house
    assert all(t.availability is Availability.ONLINE for t in app.twins)

    answer = await app.assistant.ask("увімкни світло на кухні")

    assert answer == "Світло на кухні увімкнено."
    (event,) = finished
    record = app.tracker.get(event.cmd_id)
    assert record.device_id == "light.kitchen_main"
    assert [s for s, _ in record.history] == [S.CREATED, S.PUBLISHED, S.ACCEPTED, S.COMPLETED]
    # the decisive check: the simulated device really is on, and the twin agrees
    assert sim.devices["light.kitchen_main"].state["on"] is True
    assert app.twins.get("light.kitchen_main").reported["on"] is True
    # and the command log in SQLite has every stage
    row = app.storage.commands.get(record.cmd_id)
    assert row["status"] == "completed"
    assert row["t_created"] <= row["t_published"] <= row["t_ack"] <= row["t_finished"]
    assert row["desired"] == {"on": True}
    session = app.storage.sessions.get(record.session_id)
    assert session["transcript"] == "увімкни світло на кухні"
    assert session["response"] == answer


async def test_curtains_move_with_progress_message(house) -> None:
    app, sim, _, spoken = house
    sim.devices["curtains.bedroom"].physics.speed = 100.0  # keep the test short (1 s)
    app.dispatcher.settings.progress_min_s = 0.5

    answer = await app.assistant.ask("відкрий штори у спальні")

    assert answer == "Штори у спальні відкрито."
    assert [e.text for e in spoken if not e.final] == ["Відкриваю штори у спальні…"]
    assert sim.devices["curtains.bedroom"].state == {"position": 100, "moving": False}


async def test_group_command_turns_off_all_lights(house) -> None:
    app, sim, _, _ = house
    await app.assistant.ask("увімкни світло у спальні")
    answer = await app.assistant.ask("вимкни все світло")
    assert answer == "Вимкнено 11 пристроїв."
    lights = app.registry.find(group="all_lights")
    assert all(sim.devices[d.id].state["on"] is False for d in lights)


async def test_offline_device_detected_and_not_contacted(house) -> None:
    app, sim, finished, _ = house
    await sim.devices["lock.front_door"].shutdown()  # the device reports offline
    for _ in range(50):
        if app.twins.get("lock.front_door").availability is Availability.OFFLINE:
            break
        await asyncio.sleep(0.02)

    answer = await app.assistant.ask("зачини двері")

    assert answer == "Замок вхідних дверей зараз не в мережі."
    assert finished[-1].status is S.DEVICE_OFFLINE
    assert app.tracker.get(finished[-1].cmd_id).attempts == 0  # nothing was sent
