import pytest

from teleckinez.core.events import EventBus
from teleckinez.core.models import Availability
from teleckinez.core.models import CommandStatus as S
from teleckinez.devices.twin import TwinStore
from teleckinez.dispatch.response import (
    ResponseComposer,
    age_text,
    devices_word,
    num,
    plural_uk,
)
from teleckinez.dispatch.tracker import CommandRecord
from teleckinez.transport.messages import StateReport


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def rc(registry):
    clock = Clock()
    twins = TwinStore(registry, EventBus(), clock=clock)
    composer = ResponseComposer(registry, twins, clock=clock)
    composer.twins_ = twins
    return composer


def rec(device, action, desired, status, error=None, already=False) -> CommandRecord:
    return CommandRecord(
        "c", device, action, {}, desired, status=status, error=error, already=already
    )


@pytest.mark.parametrize(
    ("record", "uk", "en"),
    [
        (
            rec("light.kitchen_main", "turn_on", {"on": True}, S.COMPLETED),
            "Світло на кухні увімкнено.",
            "Kitchen light is on.",
        ),
        (
            rec("light.kitchen_main", "turn_off", {"on": False}, S.COMPLETED, already=True),
            "Світло на кухні вже вимкнено.",
            "Kitchen light is already off.",
        ),
        (
            rec("curtains.living_room", "open", {"position": 100}, S.COMPLETED),
            "Штори у вітальні відкрито.",
            "Living room curtains are open.",
        ),
        (
            rec("curtains.bedroom", "set", {"position": 50}, S.COMPLETED),
            "Штори у спальні відкрито на 50 %.",
            "Bedroom curtains are 50 % open.",
        ),
        (
            rec("light.kitchen_main", "set", {"on": True, "brightness": 40}, S.COMPLETED),
            "Світло на кухні: яскравість 40 %.",
            "Kitchen light: brightness 40 %.",
        ),
        (
            rec("thermostat.home", "set", {"target_temp": 22.5}, S.COMPLETED),
            "Опалення: встановлено 22,5 °C.",
            "Heating: set to 22.5 °C.",
        ),
        (
            rec("lock.front_door", "lock", {"locked": True}, S.COMPLETED),
            "Замок вхідних дверей зачинено.",
            "Front door lock is locked.",
        ),
        (
            rec("light.kitchen_main", "turn_on", {"on": True}, S.TIMEOUT_ACK, "no_ack"),
            "Світло на кухні не відповідає.",
            "Kitchen light is not responding.",
        ),
        (
            rec("curtains.bedroom", "close", {"position": 0}, S.TIMEOUT_ACK, "no_ack"),
            "Штори у спальні не відповідають.",
            "Bedroom curtains are not responding.",
        ),
        (
            rec("curtains.bedroom", "open", {"position": 100}, S.TIMEOUT_STATE),
            "Штори у спальні: команду прийнято, але стан не змінився.",
            "Bedroom curtains: the command was accepted, but the state did not change.",
        ),
        (
            rec("lock.front_door", "lock", {"locked": True}, S.DEVICE_OFFLINE),
            "Замок вхідних дверей зараз не в мережі.",
            "Front door lock is offline.",
        ),
        (
            rec("light.kitchen_main", "set", {}, S.REJECTED_LOCAL, "value_out_of_range"),
            "Світло на кухні: таке значення не підтримується.",
            "Kitchen light: the value is out of range.",
        ),
        (
            rec("light.kitchen_main", "turn_on", {}, S.REJECTED_LOCAL, "broker_unavailable"),
            "Немає зв'язку з MQTT-брокером.",
            "The MQTT broker is unreachable.",
        ),
    ],
)
def test_single_command_results(rc, record, uk: str, en: str) -> None:
    assert rc.command_result([record], "uk") == uk
    assert rc.command_result([record], "en") == en


def test_group_results(rc) -> None:
    lights = ["kitchen_main", "living_main", "bedroom_main", "hallway", "garage"]
    ok = [rec(f"light.{x}", "turn_off", {"on": False}, S.COMPLETED) for x in lights]
    failed = rec("light.kids_nightlight", "turn_off", {"on": False}, S.TIMEOUT_ACK)
    assert rc.command_result(ok, "uk") == "Вимкнено 5 пристроїв."
    assert rc.command_result(ok[:2], "uk") == "Вимкнено 2 пристрої."
    assert rc.command_result([*ok, failed], "uk") == (
        "Вимкнено 5 з 6 пристроїв. Нічник у дитячій не відповідає."
    )
    assert rc.command_result([*ok, failed], "en") == (
        "Turned off 5 of 6 devices. Kids room night light is not responding."
    )


def test_group_lists_at_most_three_failures(rc) -> None:
    ids = ["kitchen_main", "living_main", "bedroom_main", "hallway", "garage"]
    failed = [rec(f"light.{x}", "turn_on", {"on": True}, S.TIMEOUT_ACK) for x in ids]
    text = rc.command_result(failed, "uk")
    assert text.count("не відповідає") == 3
    assert text.endswith("і ще 2 пристрої з проблемами.")


def test_progress_messages(rc) -> None:
    curtains = rec("curtains.living_room", "open", {"position": 100}, S.ACCEPTED)
    gate = rec("gate.garage", "set", {"position": 0}, S.ACCEPTED)
    assert rc.progress(curtains, "uk") == "Відкриваю штори у вітальні…"
    assert rc.progress(gate, "uk") == "Закриваю гаражні ворота…"
    assert rc.progress(curtains, "en") == "Opening the living room curtains…"


def test_failures(rc, registry) -> None:
    two = {"rooms": ["living_room", "bedroom"]}
    assert rc.failure("ambiguous_target", two, "uk", name="штори") == (
        "Знайшов штори у вітальні та у спальні — назвіть кімнату."
    )
    assert rc.failure("ambiguous_target", {"rooms": list(registry.rooms)}, "uk", "світло") == (
        "Знайшов світло у 8 кімнатах — назвіть кімнату."
    )
    assert rc.failure(
        "not_in_room", {"room": "bedroom", "rooms": ["kitchen"], "name": "чайник"}, "uk"
    ) == ("У спальні немає такого пристрою, а «чайник» є на кухні.")
    assert rc.failure("not_understood") == "Вибачте, я не зрозумів команду."
    assert rc.failure("unsupported_action", {"devices": ["tv.living_room"]}) == (
        "Телевізор у вітальні не підтримує цю дію."
    )


def test_queries_with_freshness(rc) -> None:
    twins, clock = rc.twins_, rc.clock
    twins.on_message(
        StateReport("sensor.bedroom_climate", {"temperature": 21.37, "humidity": 44.8}, None, 1)
    )
    sensor = rc.registry.get("sensor.bedroom_climate")
    assert (
        rc.query_result([sensor], "uk", "target_temp") == "Датчик у спальні: температура 21,4 °C."
    )
    assert rc.query_result([sensor], "en") == (
        "Bedroom sensor: temperature 21.4 °C, humidity 44.8 %."
    )
    clock.now += 125
    assert rc.query_result([sensor], "uk", "target_temp") == (
        "Датчик у спальні: температура 21,4 °C (дані 2 хв тому)."
    )
    twins.set_availability("sensor.bedroom_climate", Availability.STALE)
    assert "дані можуть бути застарілими" in rc.query_result([sensor], "uk")
    twins.set_availability("sensor.bedroom_climate", Availability.OFFLINE)
    assert rc.query_result([sensor], "uk", "target_temp").startswith(
        "Датчик у спальні не в мережі, дані 2 хв тому."
    )


def test_query_hides_details_of_devices_that_are_off(rc) -> None:
    rc.twins_.on_message(StateReport("tv.living_room", {"on": False, "volume": 20}, None, 1))
    assert rc.query_result([rc.registry.get("tv.living_room")]) == "Телевізор у вітальні: вимкнено."
    assert rc.query_result([rc.registry.get("kettle.kitchen")]) == "Чайник: немає даних."


@pytest.mark.parametrize(
    ("n", "word"),
    [(1, "пристрій"), (2, "пристрої"), (4, "пристрої"), (5, "пристроїв"), (11, "пристроїв"),
     (12, "пристроїв"), (21, "пристрій"), (22, "пристрої"), (25, "пристроїв")],
)  # fmt: skip
def test_ukrainian_plural(n: int, word: str) -> None:
    assert plural_uk(n, "пристрій", "пристрої", "пристроїв") == word
    assert devices_word(n, "uk") == word


def test_number_and_age_formatting() -> None:
    assert num(21.5, "uk") == "21,5" and num(21.5, "en") == "21.5" and num(22.0, "uk") == "22"
    assert age_text(5, "uk") == "5 с тому"
    assert age_text(125, "en") == "2 min ago"
    assert age_text(7300, "uk") == "2 год тому"
