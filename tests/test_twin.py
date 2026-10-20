import pytest

from teleckinez.core.events import DeviceAvailabilityChanged, DeviceStateChanged, EventBus
from teleckinez.core.models import Availability
from teleckinez.devices.health import HealthMonitor
from teleckinez.devices.twin import TwinStore
from teleckinez.transport.messages import (
    AckReport,
    AvailabilityReport,
    MetaReport,
    StateReport,
)

LIGHT = "light.kitchen_main"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def env(registry):
    bus, clock = EventBus(), Clock()
    events: list = []
    bus.subscribe(events.append, DeviceStateChanged, DeviceAvailabilityChanged)
    twins = TwinStore(registry, bus, default_heartbeat_s=10, clock=clock)
    return twins, clock, events


def test_state_updates_twin_and_publishes_changes(env) -> None:
    twins, clock, events = env
    twins.on_message(StateReport(LIGHT, {"on": False, "brightness": 100}, 1.0, 1))
    twins.on_message(StateReport(LIGHT, {"on": False, "brightness": 100}, 2.0, 2))  # heartbeat
    twins.on_message(StateReport(LIGHT, {"on": True, "brightness": 100}, 3.0, 3))
    state_events = [e for e in events if isinstance(e, DeviceStateChanged)]
    assert [e.changed for e in state_events] == [{"on": False, "brightness": 100}, {"on": True}]
    twin = twins.get(LIGHT)
    assert twin.reported == {"on": True, "brightness": 100} and twin.seq == 3
    assert twin.last_seen == clock.now


def test_out_of_order_state_dropped_but_restart_accepted(env) -> None:
    twins, _, _ = env
    twins.on_message(StateReport(LIGHT, {"on": True}, None, 10))
    twins.on_message(StateReport(LIGHT, {"on": False}, None, 9))  # late duplicate
    assert twins.get(LIGHT).reported == {"on": True}
    twins.on_message(StateReport(LIGHT, {"on": False}, None, 1))  # device restarted
    assert twins.get(LIGHT).reported == {"on": False}


def test_drift_is_change_without_active_command(env) -> None:
    twins, _, events = env
    twins.on_message(StateReport(LIGHT, {"on": False}, None, 1))
    twins.set_desired(LIGHT, {"on": True})
    twins.on_message(StateReport(LIGHT, {"on": True}, None, 2))
    assert twins.get(LIGHT).converged
    twins.set_desired(LIGHT, None)  # command finished
    twins.on_message(StateReport(LIGHT, {"on": False}, None, 3))  # someone switched it off
    sources = [e.source for e in events if isinstance(e, DeviceStateChanged)]
    assert sources == ["report", "report", "drift"]


def test_readonly_changes_are_not_drift(env) -> None:
    twins, _, events = env
    twins.on_message(
        StateReport("thermostat.home", {"target_temp": 21, "current_temp": 20}, None, 1)
    )
    twins.on_message(
        StateReport("thermostat.home", {"target_temp": 21, "current_temp": 20.5}, None, 2)
    )
    assert events[-1].source == "report"


def test_availability_from_lwt(env) -> None:
    twins, _, events = env
    twins.on_message(AvailabilityReport(LIGHT, True))
    twins.on_message(AvailabilityReport(LIGHT, True))  # retained repeat: no event
    twins.on_message(AvailabilityReport(LIGHT, False))
    changes = [(e.previous, e.availability) for e in events]
    assert changes == [
        (Availability.UNKNOWN, Availability.ONLINE),
        (Availability.ONLINE, Availability.OFFLINE),
    ]


def test_meta_overrides_heartbeat(env) -> None:
    twins, _, _ = env
    twins.on_message(MetaReport(LIGHT, {"heartbeat_s": 2}))
    assert twins.get(LIGHT).heartbeat_s == 2.0


def test_health_marks_silent_device_stale_and_recovers(env) -> None:
    twins, clock, events = env
    health = HealthMonitor(twins, stale_factor=3)
    twins.on_message(AvailabilityReport(LIGHT, True))
    twins.on_message(StateReport(LIGHT, {"on": False}, None, 1))
    clock.now += 29
    assert health.check() == []
    clock.now += 2  # 31 s > 3 x 10 s
    assert health.check() == [LIGHT]
    assert twins.get(LIGHT).availability is Availability.STALE
    assert health.check() == []  # reported once
    twins.on_message(AckReport(LIGHT, "c", "accepted"))  # any message revives it
    assert twins.get(LIGHT).availability is Availability.ONLINE
    assert [e.availability for e in events if isinstance(e, DeviceAvailabilityChanged)] == [
        Availability.ONLINE,
        Availability.STALE,
        Availability.ONLINE,
    ]


def test_offline_devices_are_not_marked_stale(env) -> None:
    twins, clock, _ = env
    health = HealthMonitor(twins)
    twins.on_message(AvailabilityReport(LIGHT, True))
    twins.on_message(AvailabilityReport(LIGHT, False))
    clock.now += 1000
    assert health.check() == []
    assert twins.get(LIGHT).availability is Availability.OFFLINE


def test_snapshot(env) -> None:
    twins, clock, _ = env
    twins.on_message(StateReport(LIGHT, {"on": True}, None, 1))
    clock.now += 5
    snap = twins.get(LIGHT).snapshot(clock.now)
    assert snap == {
        "id": LIGHT,
        "reported": {"on": True},
        "desired": None,
        "availability": "online",
        "age_s": 5.0,
    }
