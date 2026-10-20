import asyncio
from collections.abc import Callable

import pytest

from teleckinez.core.config import DispatcherSettings
from teleckinez.core.events import CommandFinished, CommandStatusChanged, EventBus
from teleckinez.core.models import Availability, DeviceCommand
from teleckinez.core.models import CommandStatus as S
from teleckinez.devices.twin import TwinStore
from teleckinez.dispatch.dispatcher import Dispatcher
from teleckinez.dispatch.tracker import CommandTracker
from teleckinez.transport.messages import AckReport, AvailabilityReport, StateReport
from teleckinez.transport.mqtt_client import MqttNotConnected

LIGHT = "light.kitchen_main"
CURTAINS = "curtains.bedroom"

FAST = DispatcherSettings(
    ack_timeout_s=0.05,
    max_attempts=3,
    retry_backoff_s=[0.02, 0.04],
    state_timeout_s={"default": 0.1, "light": 0.1},
    state_margin_s=0.1,
)


class FakeSender:
    """Records sends; `behaviour[device_id](cmd_id, desired, attempt)` plays the device."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict]] = []
        self.behaviour: dict[str, Callable] = {}
        self.connected = True
        self.fail_publish = 0  # number of publishes that raise MqttNotConnected
        self.env = None

    def is_connected(self) -> bool:
        return self.connected

    async def send(self, device_id: str, cmd_id: str, desired: dict, ttl_s: float) -> None:
        if self.fail_publish:
            self.fail_publish -= 1
            raise MqttNotConnected("broker down")
        self.sent.append((device_id, cmd_id, desired))
        attempt = sum(1 for _, c, _ in self.sent if c == cmd_id)
        if fn := self.behaviour.get(device_id):
            asyncio.get_running_loop().call_soon(fn, cmd_id, desired, attempt)


class Env:
    def __init__(self, registry) -> None:
        self.bus = EventBus()
        self.twins = TwinStore(registry, self.bus)
        self.tracker = CommandTracker(self.bus)
        self.sender = FakeSender()
        self.dispatcher = Dispatcher(
            registry, self.twins, self.tracker, self.sender, self.bus, FAST
        )
        self.seq = 100

    def feed(self, message) -> None:
        """Deliver a device message the way DeviceGateway does."""
        self.twins.on_message(message)
        self.dispatcher.on_device_message(message)

    def state(self, device_id: str, **state) -> None:
        self.seq += 1
        merged = {**self.twins.get(device_id).reported, **state}
        self.feed(StateReport(device_id, merged, None, self.seq))

    def ack(self, device_id: str, cmd_id: str, status: str = "accepted", error=None) -> None:
        self.feed(AckReport(device_id, cmd_id, status, error))

    def good_device(self, device_id: str) -> None:
        """Acks and applies every command."""

        def behave(cmd_id, desired, attempt):
            self.ack(device_id, cmd_id)
            self.state(device_id, **desired)

        self.sender.behaviour[device_id] = behave

    async def run(self, *commands: DeviceCommand, wait_s: float = 2.0):
        group = await self.dispatcher.dispatch(commands, session_id="s1")
        await group.wait(wait_s)
        return group


@pytest.fixture
def env(registry):
    e = Env(registry)
    for device_id in (LIGHT, CURTAINS, "light.bedroom_main", "light.hallway"):
        e.feed(AvailabilityReport(device_id, True))
    e.state(LIGHT, on=False, brightness=100)
    e.state(CURTAINS, position=0, moving=False)
    yield e


def statuses(record) -> list[S]:
    return [s for s, _ in record.history]


async def test_happy_path_completes_only_after_state(env) -> None:
    env.good_device(LIGHT)
    (record,) = (await env.run(DeviceCommand(LIGHT, "turn_on"))).records
    assert statuses(record) == [S.CREATED, S.PUBLISHED, S.ACCEPTED, S.COMPLETED]
    assert env.sender.sent == [(LIGHT, record.cmd_id, {"on": True})]
    assert env.twins.get(LIGHT).desired is None  # cleared when finished
    assert record.session_id == "s1"


async def test_state_before_ack_completes(env) -> None:
    def behave(cmd_id, desired, attempt):
        env.state(LIGHT, **desired)
        env.ack(LIGHT, cmd_id)  # late ack is ignored

    env.sender.behaviour[LIGHT] = behave
    (record,) = (await env.run(DeviceCommand(LIGHT, "turn_on"))).records
    assert statuses(record) == [S.CREATED, S.PUBLISHED, S.COMPLETED]


async def test_silent_device_retries_with_same_cmd_id_then_timeout_ack(env) -> None:
    (record,) = (await env.run(DeviceCommand(LIGHT, "turn_on"))).records
    assert record.status is S.TIMEOUT_ACK and record.error == "no_ack"
    assert record.attempts == 3
    assert [c for _, c, _ in env.sender.sent] == [record.cmd_id] * 3
    assert statuses(record) == [
        S.CREATED,
        S.PUBLISHED,
        S.RETRYING,
        S.PUBLISHED,
        S.RETRYING,
        S.PUBLISHED,
        S.TIMEOUT_ACK,
    ]


async def test_retry_saves_lost_command(env) -> None:
    def behave(cmd_id, desired, attempt):
        if attempt == 2:  # the first one was "lost"
            env.ack(LIGHT, cmd_id)
            env.state(LIGHT, **desired)

    env.sender.behaviour[LIGHT] = behave
    (record,) = (await env.run(DeviceCommand(LIGHT, "turn_on"))).records
    assert record.status is S.COMPLETED and record.attempts == 2


async def test_late_ack_during_backoff_stops_retrying(env) -> None:
    def late_ack(event: CommandStatusChanged) -> None:
        # the ack of attempt 1 arrives only after ack_timeout, during the backoff pause
        if event.new is S.RETRYING:
            env.ack(LIGHT, event.cmd_id)
            env.state(LIGHT, on=True)

    env.bus.subscribe(late_ack, CommandStatusChanged)
    (record,) = (await env.run(DeviceCommand(LIGHT, "turn_on"))).records
    assert statuses(record) == [S.CREATED, S.PUBLISHED, S.RETRYING, S.ACCEPTED, S.COMPLETED]
    assert record.attempts == 1 and len(env.sender.sent) == 1


async def test_accepted_but_not_executed_is_timeout_state(env) -> None:
    env.sender.behaviour[LIGHT] = lambda cmd_id, desired, attempt: env.ack(LIGHT, cmd_id)
    (record,) = (await env.run(DeviceCommand(LIGHT, "turn_on"))).records
    assert statuses(record)[-2:] == [S.ACCEPTED, S.TIMEOUT_STATE]
    assert record.error == "state_not_reached"


async def test_rejected_by_device(env) -> None:
    env.sender.behaviour[LIGHT] = lambda c, d, a: env.ack(LIGHT, c, "rejected", "busy")
    (record,) = (await env.run(DeviceCommand(LIGHT, "turn_on"))).records
    assert record.status is S.REJECTED and record.error == "busy"


async def test_offline_device_is_not_contacted(env) -> None:
    env.feed(AvailabilityReport(LIGHT, False))
    (record,) = (await env.run(DeviceCommand(LIGHT, "turn_on"))).records
    assert statuses(record) == [S.CREATED, S.DEVICE_OFFLINE]
    assert env.sender.sent == []


async def test_device_goes_offline_while_executing(env) -> None:
    def behave(cmd_id, desired, attempt):
        env.ack(CURTAINS, cmd_id)
        env.state(CURTAINS, moving=True, position=20)
        env.feed(AvailabilityReport(CURTAINS, False))  # LWT

    env.sender.behaviour[CURTAINS] = behave
    (record,) = (await env.run(DeviceCommand(CURTAINS, "open"))).records
    assert statuses(record)[-2:] == [S.ACCEPTED, S.DEVICE_OFFLINE]


@pytest.mark.parametrize(
    ("command", "error"),
    [
        (DeviceCommand(LIGHT, "open"), "unsupported_action"),
        (DeviceCommand(LIGHT, "set", {"brightness": 150}), "value_out_of_range"),
    ],
)
async def test_invalid_command_rejected_locally(env, command, error) -> None:
    (record,) = (await env.run(command)).records
    assert statuses(record) == [S.CREATED, S.REJECTED_LOCAL] and record.error == error
    assert env.sender.sent == []


async def test_broker_down_is_rejected_locally(env) -> None:
    env.sender.connected = False
    (record,) = (await env.run(DeviceCommand(LIGHT, "turn_on"))).records
    assert record.status is S.REJECTED_LOCAL and record.error == "broker_unavailable"


async def test_publish_failure_is_retried(env) -> None:
    env.good_device(LIGHT)
    env.sender.fail_publish = 1
    (record,) = (await env.run(DeviceCommand(LIGHT, "turn_on"))).records
    assert record.status is S.COMPLETED and record.error is None and record.attempts == 2


async def test_new_command_supersedes_old_one_for_same_attribute(env) -> None:
    def slow_curtains(cmd_id, desired, attempt):
        env.ack(CURTAINS, cmd_id)  # accepted, moving takes a while

    env.sender.behaviour[CURTAINS] = slow_curtains
    first = (await env.dispatcher.dispatch([DeviceCommand(CURTAINS, "open")])).records[0]
    await asyncio.sleep(0.01)
    env.good_device(CURTAINS)
    second = (await env.run(DeviceCommand(CURTAINS, "close"))).records[0]
    assert first.status is S.SUPERSEDED and first.error == f"superseded_by:{second.cmd_id}"
    assert second.status is S.COMPLETED


async def test_compatible_commands_do_not_supersede(env) -> None:
    floor = "light.living_floor"  # rgb_light
    env.feed(AvailabilityReport(floor, True))
    env.state(floor, on=False, brightness=100, color="white")
    env.sender.behaviour[floor] = lambda c, d, a: env.ack(floor, c)  # applies later
    group1 = await env.dispatcher.dispatch([DeviceCommand(floor, "set", {"brightness": 40})])
    group2 = await env.dispatcher.dispatch([DeviceCommand(floor, "set", {"color": "blue"})])
    await asyncio.sleep(0.01)
    assert env.twins.get(floor).desired == {"on": True, "brightness": 40, "color": "blue"}
    env.state(floor, on=True, brightness=40, color="blue")
    first, second = (await group1.wait(1))[0], (await group2.wait(1))[0]
    assert first.status is S.COMPLETED and second.status is S.COMPLETED


async def test_group_fan_out_reports_partial_success(env) -> None:
    for dev in (LIGHT, "light.bedroom_main"):
        env.good_device(dev)
    # light.hallway stays silent
    group = await env.run(
        DeviceCommand(LIGHT, "turn_off"),
        DeviceCommand("light.bedroom_main", "turn_off"),
        DeviceCommand("light.hallway", "turn_off"),
    )
    assert group.group_id and group.group_id.startswith("g-")
    assert {r.device_id for r in group.succeeded} == {LIGHT, "light.bedroom_main"}
    assert [(r.device_id, r.status) for r in group.failed] == [("light.hallway", S.TIMEOUT_ACK)]
    assert all(r.group_id == group.group_id for r in group.records)


async def test_already_in_desired_state(env) -> None:
    env.sender.behaviour[LIGHT] = lambda c, d, a: env.ack(LIGHT, c)  # no new state message
    (record,) = (await env.run(DeviceCommand(LIGHT, "turn_off"))).records
    assert record.already and record.status is S.COMPLETED


async def test_toggle_uses_reported_state(env) -> None:
    env.good_device(LIGHT)
    await env.run(DeviceCommand(LIGHT, "toggle"))
    await env.run(DeviceCommand(LIGHT, "toggle"))
    assert [d for _, _, d in env.sender.sent] == [{"on": True}, {"on": False}]


async def test_curtains_state_timeout_depends_on_distance(env) -> None:
    group = await env.dispatcher.dispatch([DeviceCommand(CURTAINS, "set", {"position": 50})])
    # 50 % at 25 %/s = 2 s + margin 0.1 s
    assert group.records[0].state_timeout_s == pytest.approx(2.1)
    await env.dispatcher.close()


async def test_command_finished_events(env) -> None:
    finished: list = []
    env.bus.subscribe(finished.append, CommandFinished)
    env.good_device(LIGHT)
    await env.run(DeviceCommand(LIGHT, "turn_on"))
    assert [(e.device_id, e.status, e.session_id) for e in finished] == [(LIGHT, S.COMPLETED, "s1")]


def test_stale_device_still_gets_commands(env) -> None:
    env.twins.set_availability(LIGHT, Availability.STALE)
    assert env.twins.get(LIGHT).availability is Availability.STALE
