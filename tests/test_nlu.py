import pytest

from teleckinez.nlu.normalizer import detect_lang, tokenize
from teleckinez.nlu.resolver import TargetResolver
from teleckinez.nlu.rule_parser import RuleParser


@pytest.fixture(scope="module")
def nlu(registry):
    parser, resolver = RuleParser(registry), TargetResolver(registry)

    def run(text: str):
        result = parser.parse(text)
        assert result.ok, f"{text!r}: {result.reason}"
        return result.intent, resolver.resolve(result.intent)

    run.parser = parser
    return run


def commands(resolution) -> list[tuple]:
    return [(c.device_id, c.action, c.params) for c in resolution.commands]


# ----------------------------------------------------------------- normalizer


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("Увімкни СВІТЛО!", ["увімкни", "світло"]),
        ("двадцять два градуси", ["22", "°"]),
        ("сорок відсотків", ["40", "%"]),
        ("40%", ["40", "%"]),
        ("22°", ["22", "°"]),
        ("twenty-two degrees", ["22", "°"]),
        ("на третю швидкість", ["на", "3", "швидкість"]),
        ("наполовину", ["50", "%"]),
        ("закип’яти", ["закип'яти"]),
        ("сто відсотків", ["100", "%"]),
        ("п'ятнадцять", ["15"]),
    ],
)
def test_tokenize(text: str, tokens: list[str]) -> None:
    assert tokenize(text) == tokens


def test_detect_lang() -> None:
    assert detect_lang("увімкни світло") == "uk"
    assert detect_lang("turn on the light") == "en"
    assert detect_lang("увімкни TV") == "uk"


# ------------------------------------------------- equivalent phrasings (spec)


@pytest.mark.parametrize(
    "text",
    [
        "штори вітальня",
        "відкрий штори у вітальні",
        "living room curtains",
        "open the curtains in the living room",
    ],
)
def test_spec_examples_target_the_same_device(nlu, text: str) -> None:
    _, res = nlu(text)
    assert [c.device_id for c in res.commands] == ["curtains.living_room"]


def test_phrase_without_verb_uses_type_default(nlu) -> None:
    _, res = nlu("штори вітальня")
    assert commands(res) == [("curtains.living_room", "toggle", {})]


# ------------------------------------------------------------------- commands


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("увімкни світло на кухні", [("light.kitchen_main", "turn_on", {})]),
        ("вимкни світло у спальні", [("light.bedroom_main", "turn_off", {})]),
        ("turn on the kitchen light", [("light.kitchen_main", "turn_on", {})]),
        ("kitchen light on", [("light.kitchen_main", "turn_on", {})]),
        ("включи люстру в залі", [("light.living_main", "turn_on", {})]),
        (
            "яскравість світла на кухні 40 відсотків",
            [("light.kitchen_main", "set", {"brightness": 40})],
        ),
        ("зроби світло у вітальні синім", [("light.living_floor", "set", {"color": "blue"})]),
        ("торшер червоний", [("light.living_floor", "set", {"color": "red"})]),
        (
            "встанови опалення на двадцять два градуси",
            [("thermostat.home", "set", {"target_temp": 22})],
        ),
        ("set heating to 23 degrees", [("thermostat.home", "set", {"target_temp": 23})]),
        ("гучніше телевізор", [("tv.living_room", "step", {"volume": 10})]),
        ("зроби телевізор тихіше", [("tv.living_room", "step", {"volume": -10})]),
        ("яскравіше світло на кухні на 30 %", [("light.kitchen_main", "step", {"brightness": 30})]),
        (
            "вентилятор у ванній на третю швидкість",
            [("fan.bathroom", "set", {"speed": 3})],
        ),
        ("відкрий гараж", [("gate.garage", "open", {})]),
        ("закрий штори в спальні наполовину", [("curtains.bedroom", "set", {"position": 50})]),
        (
            "відкрий штори у спальні на 30 відсотків",
            [("curtains.bedroom", "set", {"position": 30})],
        ),
        ("зачини двері", [("lock.front_door", "close", {})]),
        ("відчини вхідні двері", [("lock.front_door", "open", {})]),
        ("запусти пилосос", [("vacuum.home", "start", {})]),
        ("пилосос на базу", [("vacuum.home", "dock", {})]),
        ("постав чайник", [("kettle.kitchen", "turn_on", {})]),
        ("закип'яти чайник", [("kettle.kitchen", "turn_on", {})]),
        ("увімкни кондиціонер на охолодження", [("ac.living_room", "set", {"mode": "cool"})]),
        ("увімкни бойлер", [("boiler.bathroom", "turn_on", {})]),
        ("вимкни кавоварку", [("socket.kitchen_coffee", "turn_off", {})]),
    ],
)
def test_commands(nlu, text: str, expected: list[tuple]) -> None:
    _, res = nlu(text)
    assert res.ok, res
    assert commands(res) == expected


def test_group_fan_out(nlu, registry) -> None:
    for text in ("вимкни все світло", "turn off all lights"):
        intent, res = nlu(text)
        assert intent.group == "all_lights"
        assert len(res.commands) == len(registry.find(group="all_lights")) == 11
        assert {c.action for c in res.commands} == {"turn_off"}


def test_group_in_room(nlu) -> None:
    _, res = nlu("вимкни все світло у вітальні")
    assert {c.device_id for c in res.commands} == {"light.living_main", "light.living_floor"}


def test_all_devices_in_room(nlu) -> None:
    _, res = nlu("вимкни все у вітальні")
    assert "light.living_main" in {c.device_id for c in res.commands}
    assert "sensor.living_climate" not in {c.device_id for c in res.commands}


# --------------------------------------------------------- STT-style errors


@pytest.mark.parametrize(
    ("text", "device"),
    [
        ("увімкни свтло на кухні", "light.kitchen_main"),  # dropped letter
        ("вімкни світло у дитячій", "light.kids_room"),  # misheard verb
        ("увімкни світло в кухні", "light.kitchen_main"),  # case form not in YAML
        ("увімкни світло у спальнi", "light.bedroom_main"),  # Latin "i" from keyboard
        ("відкрий штори у вiтальні", "curtains.living_room"),
    ],
)
def test_tolerates_recognition_errors(nlu, text: str, device: str) -> None:
    intent, res = nlu(text)
    assert [c.device_id for c in res.commands] == [device]
    assert 0.6 <= intent.confidence <= 1.0


# ----------------------------------------------------------- queries & errors


@pytest.mark.parametrize(
    ("text", "devices"),
    [
        ("чи зачинені двері", ["lock.front_door"]),
        ("яка температура у спальні", ["sensor.bedroom_climate"]),
        ("яка температура надворі", ["sensor.yard_climate"]),
        ("what is the temperature in the bedroom", ["sensor.bedroom_climate"]),
        ("яка вологість у вітальні", ["sensor.living_climate"]),
        ("що з пилососом", ["vacuum.home"]),
    ],
)
def test_queries(nlu, text: str, devices: list[str]) -> None:
    intent, res = nlu(text)
    assert intent.kind == "query" and intent.action is None
    assert [d.id for d in res.devices] == devices


def test_ambiguous_without_room(nlu) -> None:
    _, res = nlu("увімкни світло")
    assert res.error == "ambiguous_target"
    assert "kitchen" in res.details["rooms"] and len(res.details["rooms"]) == 8


def test_ambiguous_curtains_lists_rooms(nlu) -> None:
    _, res = nlu("відкрий штори")
    assert res.error == "ambiguous_target"
    assert res.details["rooms"] == ["living_room", "bedroom"]


def test_device_not_in_room(nlu) -> None:
    _, res = nlu("увімкни чайник у спальні")
    assert res.error == "not_in_room" and res.details["rooms"] == ["kitchen"]


def test_unsupported_action(nlu) -> None:
    _, res = nlu("відкрий телевізор")
    assert res.error == "unsupported_action"


@pytest.mark.parametrize("text", ["розкажи анекдот", "зроби щось", "", "привіт"])
def test_not_understood(nlu, text: str) -> None:
    result = nlu.parser.parse(text)
    assert not result.ok and result.reason == "not_understood"


def test_intent_metadata(nlu) -> None:
    intent, _ = nlu("Увімкни світло на кухні")
    assert intent.lang == "uk" and intent.parser == "rule"
    assert intent.raw_text == "Увімкни світло на кухні"
    assert intent.confidence == 1.0
