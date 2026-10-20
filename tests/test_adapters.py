"""Tasmota and Zigbee2MQTT adapters (protocol.md §4) and the simulator speaking them."""

import json

import pytest

from teleckinez.core.config import DevicesConfig
from teleckinez.devices.colors import color_to_xy, nearest_color, xy_to_color
from teleckinez.devices.registry import DeviceRegistry
from teleckinez.transport.adapters.tasmota import TasmotaAdapter
from teleckinez.transport.adapters.zigbee2mqtt import Zigbee2MqttAdapter, from_z2m_brightness
from teleckinez.transport.messages import AckReport, AvailabilityReport, StateReport

HOME = DevicesConfig.model_validate(
    {
        "rooms": {"garage": {"uk": ["гараж"]}},
        "devices": [
            {"id": "light.t", "type": "light", "room": "garage", "names": {"uk": ["лампа"]},
             "protocol": "tasmota", "address": "garage_light"},
            {"id": "light.rgb", "type": "rgb_light", "room": "garage", "names": {"uk": ["нічник"]},
             "protocol": "tasmota"},
            {"id": "light.z", "type": "rgb_light", "room": "garage", "names": {"uk": ["бра"]},
             "protocol": "zigbee2mqtt", "address": "z_bulb"},
            {"id": "lock.z", "type": "door_lock", "room": "garage", "names": {"uk": ["замок"]},
             "protocol": "zigbee2mqtt", "address": "z_lock"},
            {"id": "curtains.z", "type": "curtains", "room": "garage", "names": {"uk": ["штори"]},
             "protocol": "zigbee2mqtt", "address": "z_cover", "sim": {"speed": 400}},
        ],
    }
)  # fmt: skip
REG = DeviceRegistry(HOME)


def by(protocol: str):
    return [d for d in REG if d.config.protocol == protocol]


# ------------------------------------------------------------------- colors


def test_color_roundtrips() -> None:
    for name in ("red", "green", "blue", "yellow", "purple", "white"):
        xy = color_to_xy(name)
        assert xy_to_color(xy["x"], xy["y"]) == name
    assert nearest_color((200, 10, 10)) == "red"  # a dimmed red is still red
    assert nearest_color((250, 250, 245)) == "white"


# ------------------------------------------------------------------ Tasmota


def test_tasmota_commands() -> None:
    a = TasmotaAdapter(by("tasmota"))
    assert a.encode_command("light.t", "c1", {"on": True}, 5, 0) == (
        "cmnd/garage_light/POWER",
        b"ON",
    )
    topic, payload = a.encode_command("light.t", "c2", {"on": True, "brightness": 40}, 5, 0)
    assert (topic, payload) == ("cmnd/garage_light/Backlog", b"Power ON; Dimmer 40")
    topic, payload = a.encode_command("light.rgb", "c3", {"on": True, "color": "red"}, 5, 0)
    assert (topic, payload) == ("cmnd/light_rgb/Backlog", b"Power ON; Color #FF0000")


def test_tasmota_result_acknowledges_and_merges_state() -> None:
    a = TasmotaAdapter(by("tasmota"))
    a.encode_command("light.t", "c9", {"brightness": 40}, 5, 0)
    out = a.decode("stat/garage_light/RESULT", b'{"POWER":"ON","Dimmer":40}')
    assert out == [
        AckReport("light.t", "c9", "accepted"),
        StateReport("light.t", {"on": True, "brightness": 40}, partial=True),
    ]
    # a later RESULT (someone pressed the button) is just state
    assert a.decode("stat/garage_light/RESULT", b'{"POWER":"OFF"}') == [
        StateReport("light.t", {"on": False}, partial=True)
    ]
    tele = a.decode(
        "tele/light_rgb/STATE",
        b'{"Time":"2026-10-20T10:00:00","POWER":"ON","Dimmer":80,"Color":"0000FF"}',
    )
    assert tele == [StateReport("light.rgb", {"on": True, "brightness": 80, "color": "blue"})]


@pytest.mark.parametrize(
    ("topic", "payload", "expected"),
    [
        ("tele/garage_light/LWT", b"Online", [AvailabilityReport("light.t", True)]),
        ("tele/garage_light/LWT", b"Offline", [AvailabilityReport("light.t", False)]),
        ("tele/garage_light/LWT", b"Maybe", []),
        ("tele/unknown/LWT", b"Online", []),
        ("stat/garage_light/RESULT", b"not json", []),
        ("tele/garage_light/SENSOR", b"{}", []),
    ],
)
def test_tasmota_decode_edge_cases(topic, payload, expected) -> None:
    assert TasmotaAdapter(by("tasmota")).decode(topic, payload) == expected


def test_tasmota_root_prefix_and_type_check() -> None:
    a = TasmotaAdapter(by("tasmota"), root="home")
    assert a.subscriptions()[0] == "home/stat/+/RESULT"
    assert a.decode("home/tele/garage_light/LWT", b"Online") == [
        AvailabilityReport("light.t", True)
    ]
    with pytest.raises(ValueError, match="does not support door_lock"):
        TasmotaAdapter(by("zigbee2mqtt"))


# -------------------------------------------------------------- Zigbee2MQTT


def test_z2m_commands() -> None:
    a = Zigbee2MqttAdapter(by("zigbee2mqtt"))
    topic, payload = a.encode_command("light.z", "c1", {"on": True, "brightness": 40}, 5, 0)
    assert topic == "zigbee2mqtt/z_bulb/set" and json.loads(payload) == {
        "state": "ON",
        "brightness": 102,
    }
    assert json.loads(a.encode_command("light.z", "c2", {"color": "red"}, 5, 0)[1])[
        "color"
    ] == color_to_xy("red")
    assert json.loads(a.encode_command("lock.z", "c3", {"locked": False}, 5, 0)[1]) == {
        "state": "UNLOCK"
    }
    assert json.loads(a.encode_command("curtains.z", "c4", {"position": 50}, 5, 0)[1]) == {
        "position": 50
    }


def test_z2m_state_acknowledges_pending_command() -> None:
    a = Zigbee2MqttAdapter(by("zigbee2mqtt"))
    a.encode_command("lock.z", "c5", {"locked": True}, 5, 0)
    out = a.decode("zigbee2mqtt/z_lock", b'{"state":"LOCK","lock_state":"locked","linkquality":90}')
    assert out == [AckReport("lock.z", "c5", "accepted"), StateReport("lock.z", {"locked": True})]
    bulb = a.decode(
        "zigbee2mqtt/z_bulb",
        json.dumps({"state": "ON", "brightness": 254, "color": color_to_xy("green")}).encode(),
    )
    assert bulb == [StateReport("light.z", {"on": True, "brightness": 100, "color": "green"})]


@pytest.mark.parametrize(
    ("topic", "payload", "expected"),
    [
        (
            "zigbee2mqtt/z_lock/availability",
            b'{"state":"offline"}',
            [AvailabilityReport("lock.z", False)],
        ),
        (
            "zigbee2mqtt/z_lock/availability",
            b"online",
            [AvailabilityReport("lock.z", True)],
        ),  # legacy
        ("zigbee2mqtt/bridge/state", b'{"state":"online"}', []),
        ("zigbee2mqtt/z_lock/set", b'{"state":"LOCK"}', []),  # our own command echo
        (
            "zigbee2mqtt/z_cover",
            b'{"position": 30}',
            [StateReport("curtains.z", {"position": 30, "moving": False})],
        ),
    ],
)
def test_z2m_decode_edge_cases(topic, payload, expected) -> None:
    assert Zigbee2MqttAdapter(by("zigbee2mqtt")).decode(topic, payload) == expected


def test_z2m_brightness_scale() -> None:
    assert from_z2m_brightness(254) == 100 and from_z2m_brightness(0) == 1


# ------------------------------------------- end to end with the simulator


@pytest.fixture
async def stack(mqtt_settings, tmp_path):
    from simulator.home import SimHome
    from teleckinez.app import App
    from teleckinez.core.config import HealthSettings, Settings, SimulatorSettings

    settings = Settings(
        mqtt=mqtt_settings,
        health=HealthSettings(heartbeat_s=0.3, check_interval_s=0.1),
        simulator=SimulatorSettings(latency_ms=(2, 5)),
    )
    settings.storage.path = tmp_path / "a.db"
    app = App(settings, HOME)
    sim = SimHome(app.registry, settings, seed=3)
    await sim.start()
    await app.start(settle_s=0.4)
    try:
        yield app, sim
    finally:
        await app.stop()
        await sim.stop()


@pytest.mark.integration
async def test_assistant_controls_tasmota_and_z2m_devices(stack) -> None:
    import asyncio

    from teleckinez.core.models import Availability

    app, sim = stack
    assert all(t.availability is Availability.ONLINE for t in app.twins)
    assert await app.assistant.ask("яскравість лампи 40 відсотків") == "Лампа: яскравість 40 %."
    assert sim.devices["light.t"].state == {"on": True, "brightness": 40}
    assert await app.assistant.ask("зроби бра синім") == "Бра: колір синій."
    assert sim.devices["light.z"].state["color"] == "blue"
    assert await app.assistant.ask("відчини замок") == "Замок відчинено."
    assert await app.assistant.ask("відкрий штори наполовину") == "Штори відкрито на 50 %."
    assert await app.assistant.ask("чи зачинений замок") == "Ні, замок відчинено."
    await asyncio.sleep(1.2)  # 4 x heartbeat: Tasmota re-sends tele/STATE, Z2M stays quiet
    assert app.twins.get("light.t").availability is Availability.ONLINE
    assert app.twins.get("lock.z").availability is Availability.ONLINE  # no heartbeat, not stale


@pytest.mark.integration
async def test_lwt_of_tasmota_and_z2m_devices(stack) -> None:
    import asyncio

    from simulator.faults import parse_fault
    from teleckinez.core.models import Availability

    app, sim = stack
    for device_id in ("light.t", "lock.z"):
        sim.apply_fault(parse_fault({"device_id": device_id, "fault": "offline"}))
    async with asyncio.timeout(5):
        while not all(  # noqa: ASYNC110 - waits for state owned by other components
            app.twins.get(i).availability is Availability.OFFLINE for i in ("light.t", "lock.z")
        ):
            await asyncio.sleep(0.02)
    assert await app.assistant.ask("увімкни лампу") == "Лампа зараз не в мережі."
