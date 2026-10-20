import asyncio

import pytest

from teleckinez.assistant import Assistant
from teleckinez.core.config import DispatcherSettings
from teleckinez.core.events import EventBus, IntentFailed, IntentParsed, SpeakRequested
from teleckinez.devices.twin import TwinStore
from teleckinez.dispatch.dispatcher import Dispatcher
from teleckinez.dispatch.response import ResponseComposer
from teleckinez.dispatch.tracker import CommandTracker
from teleckinez.nlu.resolver import TargetResolver
from teleckinez.nlu.rule_parser import RuleParser
from teleckinez.transport.messages import AckReport, AvailabilityReport, StateReport

SETTINGS = DispatcherSettings(
    ack_timeout_s=0.05,
    retry_backoff_s=[0.01],
    max_attempts=2,
    state_timeout_s={"default": 0.2},
    progress_min_s=0.5,
    state_margin_s=0.1,
)


class InstantHome:
    """Every device acks and applies commands at once (curtains take two steps)."""

    def __init__(self, registry, twins: TwinStore, dispatcher_ref: list) -> None:
        self.registry, self.twins, self.ref = registry, twins, dispatcher_ref
        self.seq = 0
        self.silent: set[str] = set()

    def is_connected(self) -> bool:
        return True

    async def send(self, device_id: str, cmd_id: str, desired: dict, ttl_s: float) -> None:
        if device_id in self.silent:
            return
        asyncio.get_running_loop().call_soon(self._apply, device_id, cmd_id, desired)

    def feed(self, message) -> None:
        self.twins.on_message(message)
        self.ref[0].on_device_message(message)

    def state(self, device_id: str, **values) -> None:
        self.seq += 1
        merged = {**self.twins.get(device_id).reported, **values}
        self.feed(StateReport(device_id, merged, None, self.seq))

    def _apply(self, device_id: str, cmd_id: str, desired: dict) -> None:
        self.feed(AckReport(device_id, cmd_id, "accepted"))
        self.state(device_id, **desired)


@pytest.fixture
def env(registry):
    bus = EventBus()
    twins = TwinStore(registry, bus)
    tracker = CommandTracker(bus)
    ref: list = []
    home = InstantHome(registry, twins, ref)
    dispatcher = Dispatcher(registry, twins, tracker, home, bus, SETTINGS)
    ref.append(dispatcher)
    for dev in registry:
        home.feed(AvailabilityReport(dev.id, True))
        home.state(dev.id, **dev.type.initial_state())
    assistant = Assistant(
        bus,
        RuleParser(registry),
        TargetResolver(registry),
        dispatcher,
        tracker,
        ResponseComposer(registry, twins),
        SETTINGS,
    )
    events: list = []
    bus.subscribe(events.append, IntentParsed, IntentFailed, SpeakRequested)
    return assistant, home, events


async def test_command_end_to_end(env) -> None:
    assistant, home, events = env
    assert await assistant.ask("увімкни світло на кухні") == "Світло на кухні увімкнено."
    assert home.twins.get("light.kitchen_main").reported["on"] is True
    parsed = next(e for e in events if isinstance(e, IntentParsed))
    spoken = [e for e in events if isinstance(e, SpeakRequested)]
    assert parsed.session_id == spoken[-1].session_id and spoken[-1].final


async def test_english_command_gets_english_answer(env) -> None:
    assistant, _, _ = env
    assert await assistant.ask("turn on the kitchen light") == "Kitchen light is on."


async def test_query_answer(env) -> None:
    assistant, home, _ = env
    home.state("sensor.bedroom_climate", temperature=19.5)
    answer = await assistant.ask("яка температура у спальні?")
    assert answer == "Датчик у спальні: температура 19,5 °C."


async def test_failures_publish_intent_failed(env) -> None:
    assistant, _, events = env
    assert await assistant.ask("розкажи анекдот") == "Вибачте, я не зрозумів команду."
    answer = await assistant.ask("відкрий штори")
    assert answer == "Знайшов штори у вітальні та у спальні — назвіть кімнату."
    reasons = [e.reason for e in events if isinstance(e, IntentFailed)]
    assert reasons == ["not_understood", "ambiguous_target"]


async def test_device_not_responding(env) -> None:
    assistant, home, _ = env
    home.silent.add("light.kitchen_main")
    assert await assistant.ask("вимкни світло на кухні") == "Світло на кухні не відповідає."


async def test_progress_message_for_long_actions_only(env) -> None:
    assistant, _, events = env
    await assistant.ask("відкрий штори у вітальні")  # 100 % / 25 %/s = 4 s expected
    await assistant.ask("увімкни світло у спальні")
    progress = [e.text for e in events if isinstance(e, SpeakRequested) and not e.final]
    assert progress == ["Відкриваю штори у вітальні…"]


async def test_attribute_selects_device_in_room(env) -> None:
    assistant, _, _ = env
    # the kitchen has a light and a hood; only the light has brightness
    answer = await assistant.ask("яскравість на кухні 150 відсотків")
    assert answer == "Світло на кухні: таке значення не підтримується."
    assert await assistant.ask("яскравість на кухні 40 відсотків") == (
        "Світло на кухні: яскравість 40 %."
    )
