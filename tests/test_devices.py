import pytest

from teleckinez.core.config import CONFIG_DIR, DevicesConfig, load_devices
from teleckinez.devices.capabilities import UnsupportedAction, ValueOutOfRange
from teleckinez.devices.registry import DeviceRegistry, UnknownDeviceType
from teleckinez.devices.types import DEVICE_TYPES

T = DEVICE_TYPES
TIMEOUTS = {"default": 3.0, "light": 2.0, "garage_gate": 12.0}


@pytest.fixture(scope="module")
def registry() -> DeviceRegistry:
    return DeviceRegistry(load_devices(CONFIG_DIR / "devices.yaml"))


# ---------------------------------------------------------------- desired state


def test_light_on_off_toggle_and_brightness() -> None:
    light = T["light"]
    assert light.desired_for("turn_on") == {"on": True}
    assert light.desired_for("toggle", reported={"on": True}) == {"on": False}
    assert light.desired_for("set", {"brightness": 40}) == {"on": True, "brightness": 40}
    assert light.desired_for("step", {"brightness": None}, {"brightness": 90}) == {
        "on": True,
        "brightness": 100,  # clamped
    }
    assert light.desired_for("step", {"brightness": -30}, {"brightness": 20}) == {
        "on": True,
        "brightness": 1,
    }


def test_validation_before_sending() -> None:
    with pytest.raises(ValueOutOfRange):
        T["light"].desired_for("set", {"brightness": 140})
    with pytest.raises(ValueOutOfRange):
        T["thermostat"].desired_for("set", {"target_temp": 35})
    with pytest.raises(ValueOutOfRange):
        T["rgb_light"].desired_for("set", {"color": "ultraviolet"})
    with pytest.raises(UnsupportedAction):
        T["curtains"].desired_for("turn_on")
    with pytest.raises(UnsupportedAction):
        T["thermostat"].desired_for("set", {"current_temp": 25})  # readonly
    with pytest.raises(UnsupportedAction):
        T["climate_sensor"].desired_for("set", {"temperature": 25})


def test_aliases_for_lock_and_vacuum() -> None:
    assert T["door_lock"].desired_for("open") == {"locked": False}
    assert T["door_lock"].desired_for("close") == {"locked": True}
    assert T["robot_vacuum"].desired_for("stop") == {"status": "docked"}
    assert T["door_lock"].supports("open") and not T["door_lock"].supports("toggle")


def test_curtains_open_close_toggle() -> None:
    c = T["curtains"]
    assert c.desired_for("open") == {"position": 100}
    assert c.desired_for("toggle", reported={"position": 30}) == {"position": 0}
    assert c.desired_for("toggle", reported={"position": 0}) == {"position": 100}
    assert c.desired_for("set", {"position": 50}) == {"position": 50}


# ------------------------------------------------------------------ convergence


def test_converged_compares_only_desired_keys() -> None:
    light = T["light"]
    assert light.converged({"on": True}, {"on": True, "brightness": 30})
    assert not light.converged({"on": True}, {"on": False})
    assert not light.converged({"on": True}, {})


def test_thermostat_checks_target_not_current_temperature() -> None:
    t = T["thermostat"]
    desired = t.desired_for("set", {"target_temp": 22})
    assert t.converged(desired, {"target_temp": 22.0, "current_temp": 18.4})


def test_moving_device_converges_only_when_stopped() -> None:
    c = T["curtains"]
    assert not c.converged({"position": 100}, {"position": 100, "moving": True})
    assert c.converged({"position": 100}, {"position": 100, "moving": False})


def test_float_tolerance() -> None:
    assert T["air_conditioner"].converged({"target_temp": 22.0}, {"target_temp": 22.04})


# --------------------------------------------------------------- state timeouts


def test_state_timeout_by_type_and_distance() -> None:
    assert T["light"].state_timeout({"on": True}, {}, TIMEOUTS, 2.0) == 2.0
    assert T["socket"].state_timeout({"on": True}, {}, TIMEOUTS, 2.0) == 3.0
    assert T["robot_vacuum"].state_timeout({"status": "docked"}, {}, TIMEOUTS, 2.0) == 15.0
    # curtains 25 %/s: 0 -> 100 takes 4 s + margin
    assert T["curtains"].state_timeout({"position": 100}, {"position": 0}, TIMEOUTS, 2.0) == 6.0
    assert T["curtains"].state_timeout({"position": 50}, {"position": 0}, TIMEOUTS, 2.0) == 4.0
    # unknown start position -> worst case (full travel)
    assert T["garage_gate"].state_timeout({"position": 100}, {}, TIMEOUTS, 2.0) == 10.0


def test_initial_state_and_meta() -> None:
    assert T["door_lock"].initial_state() == {"locked": True}
    assert T["light"].meta()["brightness"] == {"type": "int", "min": 1, "max": 100, "unit": "%"}
    assert T["curtains"].meta()["moving"]["readonly"] is True


# --------------------------------------------------------------------- registry


def test_registry_loads_repo_config(registry: DeviceRegistry) -> None:
    assert len(registry) == 31
    assert "light.kitchen_main" in registry
    assert registry.get("lock.front_door").critical
    assert registry.get("curtains.bedroom").plural


def test_registry_search(registry: DeviceRegistry) -> None:
    ids = lambda devs: {d.id for d in devs}  # noqa: E731
    assert ids(registry.find(room="kitchen", type="light")) == {"light.kitchen_main"}
    assert ids(registry.find(type="curtains")) == {"curtains.living_room", "curtains.bedroom"}
    assert ids(registry.find(name="Штори")) == {"curtains.living_room", "curtains.bedroom"}
    assert len(registry.find(group="all_lights")) == 11
    assert ids(registry.find(room="garage", action="open")) == {"gate.garage"}
    assert ids(registry.find(capability="temperature", room="yard")) == {"sensor.yard_climate"}


def test_titles_and_room_loc(registry: DeviceRegistry) -> None:
    assert registry.get("light.kitchen_main").title("uk") == "Світло на кухні"
    assert registry.get("light.kitchen_main").title("en") == "Kitchen light"
    assert registry.room_loc("kitchen") == "на кухні"
    assert registry.room_loc("bedroom", "en") == "in the bedroom"


def test_default_title_from_name_and_room() -> None:
    cfg = DevicesConfig.model_validate(
        {
            "rooms": {"kitchen": {"uk": ["кухня"], "loc": {"uk": "на кухні"}}},
            "devices": [
                {"id": "fan.k", "type": "fan", "room": "kitchen", "names": {"uk": ["витяжка"]}}
            ],
        }
    )
    assert DeviceRegistry(cfg).get("fan.k").title("uk") == "Витяжка на кухні"


def test_unknown_type_rejected() -> None:
    cfg = DevicesConfig.model_validate(
        {
            "rooms": {"kitchen": {"uk": ["кухня"]}},
            "devices": [
                {"id": "x.k", "type": "toaster", "room": "kitchen", "names": {"uk": ["тостер"]}}
            ],
        }
    )
    with pytest.raises(UnknownDeviceType, match="toaster"):
        DeviceRegistry(cfg)


def test_every_device_type_in_catalogue_is_used(registry: DeviceRegistry) -> None:
    used = {d.type.name for d in registry}
    assert used >= set(DEVICE_TYPES) - {"door_sensor"}
