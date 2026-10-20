import pytest

from teleckinez.core.config import NotificationSettings
from teleckinez.core.events import (
    DeviceAvailabilityChanged,
    DeviceStateChanged,
    EventBus,
    SpeakRequested,
)
from teleckinez.core.models import Availability as A
from teleckinez.devices.twin import TwinStore
from teleckinez.dispatch.notifier import Notifier
from teleckinez.dispatch.response import ResponseComposer

LOCK, LIGHT, GATE = "lock.front_door", "light.kitchen_main", "gate.garage"


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def env(registry):
    bus, clock = EventBus(), Clock()
    said: list[SpeakRequested] = []
    bus.subscribe(said.append, SpeakRequested)
    settings = NotificationSettings(cooldown_s=60, startup_grace_s=10)
    composer = ResponseComposer(registry, TwinStore(registry, bus))
    notifier = Notifier(registry, composer, bus, settings, clock=clock)
    clock.now += 11  # past the startup grace period
    return bus, clock, said, notifier


def avail(bus, device, old, new) -> None:
    bus.publish(DeviceAvailabilityChanged(device_id=device, previous=old, availability=new))


def test_offline_and_recovery(env) -> None:
    bus, _, said, _ = env
    avail(bus, LOCK, A.ONLINE, A.OFFLINE)
    avail(bus, LOCK, A.OFFLINE, A.ONLINE)
    assert [s.text for s in said] == [
        "Замок вхідних дверей не на зв'язку",
        "Замок вхідних дверей знову на зв'язку",
    ]
    assert all(s.kind == "notification" and s.session_id is None for s in said)


def test_stale_and_plural_in_english(env) -> None:
    bus, _, said, notifier = env
    notifier.language = lambda: "en"
    avail(bus, GATE, A.ONLINE, A.STALE)
    avail(bus, GATE, A.STALE, A.OFFLINE)
    assert [s.text for s in said] == [
        "Garage gate: no signal, the data may be stale",
        "Garage gate is offline",
    ]


def test_startup_reports_and_plain_reconnects_are_silent(env) -> None:
    bus, _, said, _ = env
    avail(bus, LOCK, A.UNKNOWN, A.ONLINE)  # first retained message after start
    avail(bus, LOCK, A.UNKNOWN, A.OFFLINE)
    avail(bus, LIGHT, A.STALE, A.ONLINE)  # recovery without an announced problem
    assert said == []


def test_grace_period_and_cooldown(env, registry) -> None:
    bus, clock, said, notifier = env
    notifier.started = clock.now  # just started
    avail(bus, LOCK, A.ONLINE, A.OFFLINE)
    assert said == []
    clock.now += 11
    avail(bus, LOCK, A.ONLINE, A.OFFLINE)
    avail(bus, LOCK, A.ONLINE, A.OFFLINE)  # flapping within the cooldown
    assert len(said) == 1
    clock.now += 61
    avail(bus, LOCK, A.ONLINE, A.OFFLINE)
    assert len(said) == 2


def test_drift_only_for_critical_devices(env) -> None:
    bus, _, said, _ = env
    bus.publish(
        DeviceStateChanged(
            device_id=LIGHT, reported={"on": True}, changed={"on": True}, source="drift"
        )
    )
    bus.publish(
        DeviceStateChanged(device_id=LOCK, reported={"locked": False}, changed={"locked": False})
    )  # source=report: a command of ours
    bus.publish(
        DeviceStateChanged(
            device_id=LOCK, reported={"locked": False}, changed={"locked": False}, source="drift"
        )
    )
    assert [s.text for s in said] == ["Увага: замок вхідних дверей відчинено без команди"]


def test_settings_switches(env) -> None:
    bus, _, said, notifier = env
    notifier.settings.offline = False
    avail(bus, LOCK, A.ONLINE, A.OFFLINE)
    notifier.settings.enabled = False
    avail(bus, GATE, A.ONLINE, A.STALE)
    assert said == []


@pytest.mark.integration
async def test_real_offline_fault_is_announced(mqtt_settings, tmp_path) -> None:
    import asyncio

    from simulator.faults import parse_fault
    from simulator.home import SimHome
    from teleckinez.app import App
    from teleckinez.core.config import CONFIG_DIR, Settings, load_devices

    settings = Settings(mqtt=mqtt_settings)
    settings.notifications.startup_grace_s = 0
    settings.storage.path = tmp_path / "n.db"
    app = App(settings, load_devices(CONFIG_DIR / "devices.yaml"))
    sim = SimHome(app.registry, settings, only={LOCK})
    await sim.start()
    await app.start(settle_s=0.3)
    try:
        notification = asyncio.ensure_future(
            app.bus.wait_for(SpeakRequested, lambda e: e.kind == "notification", wait_s=5)
        )
        await asyncio.sleep(0)
        sim.apply_fault(parse_fault({"device_id": LOCK, "fault": "offline"}))
        assert (await notification).text == "Замок вхідних дверей не на зв'язку"
    finally:
        await app.stop()
        await sim.stop()
