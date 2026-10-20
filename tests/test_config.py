from pathlib import Path

import pytest
from pydantic import ValidationError

from teleckinez.core.config import (
    CONFIG_DIR,
    DevicesConfig,
    DispatcherSettings,
    load_devices,
    load_settings,
)
from teleckinez.core.text import normalize_name

DEVICES = {
    "rooms": {
        "kitchen": {"uk": ["Кухня", "кухні"], "en": ["kitchen"], "loc": {"uk": "на кухні"}},
        "hallway": {"uk": ["коридор"], "en": ["hallway"]},
    },
    "groups": {"all_lights": {"uk": ["все світло"], "en": ["all lights"]}},
    "devices": [
        {
            "id": "light.kitchen_main",
            "type": "light",
            "room": "kitchen",
            "names": {"uk": ["світло", "люстра"], "en": ["light"]},
            "groups": ["all_lights"],
        },
        {
            "id": "lock.front_door",
            "type": "door_lock",
            "room": "hallway",
            "names": {"uk": ["замок", "вхідні двері"]},
            "critical": True,
        },
    ],
}


def _with(**changes) -> dict:
    data = {**DEVICES, "devices": [dict(d) for d in DEVICES["devices"]]}
    data.update(changes)
    return data


def test_valid_devices_config_and_normalized_names() -> None:
    cfg = DevicesConfig.model_validate(DEVICES)
    assert cfg.rooms["kitchen"].uk == ["кухня", "кухні"]
    assert cfg.rooms["kitchen"].loc.get("uk") == "на кухні"
    assert cfg.device("lock.front_door").critical
    assert cfg.device("light.kitchen_main").protocol == "native"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["devices"].append(dict(d["devices"][0])), "duplicate device id"),
        (lambda d: d["devices"][0].update(room="attic"), "unknown room"),
        (lambda d: d["devices"][0].update(groups=["nope"]), "unknown group"),
        (lambda d: d["devices"][0].update(id="Light kitchen"), "pattern"),
        (lambda d: d["devices"][0].update(names={}), "at least one name"),
        (lambda d: d["devices"][0].update(colour="red"), "Extra inputs"),
    ],
)
def test_invalid_devices_config(mutate, message) -> None:
    data = _with()
    mutate(data)
    with pytest.raises(ValidationError, match=message):
        DevicesConfig.model_validate(data)


def test_conflicting_synonyms_in_same_room_rejected() -> None:
    data = _with()
    data["devices"].append(
        {
            "id": "light.kitchen_spot",
            "type": "light",
            "room": "kitchen",
            "names": {"uk": ["Люстра"]},
        }
    )
    with pytest.raises(ValidationError, match="'люстра' used by both"):
        DevicesConfig.model_validate(data)


def test_same_synonym_in_different_rooms_allowed() -> None:
    data = _with()
    data["devices"].append(
        {"id": "light.hallway", "type": "light", "room": "hallway", "names": {"uk": ["світло"]}}
    )
    DevicesConfig.model_validate(data)


def test_settings_defaults_when_file_missing(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "missing.yaml")
    assert settings.mqtt.port == 1883
    assert settings.dispatcher.max_attempts == 3


def test_settings_file_in_repo_is_valid_and_password_from_env(monkeypatch) -> None:
    monkeypatch.setenv("TKZ_MQTT_PASSWORD", "secret")
    settings = load_settings(CONFIG_DIR / "settings.yaml")
    assert settings.mqtt.prefix == "tkz"
    assert settings.mqtt.password == "secret"
    assert "password" not in settings.model_dump()["mqtt"]  # never serialized


def test_settings_reject_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "s.yaml"
    path.write_text("mqtt:\n  hots: localhost\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(path)


def test_backoff_sequence() -> None:
    d = DispatcherSettings(retry_backoff_s=[0.5, 1.0])
    assert [d.backoff(a) for a in (2, 3, 4)] == [0.5, 1.0, 1.0]


def test_load_devices_from_file(tmp_path: Path) -> None:
    import yaml

    path = tmp_path / "devices.yaml"
    path.write_text(yaml.safe_dump(DEVICES, allow_unicode=True), encoding="utf-8")
    assert len(load_devices(path).devices) == 2


def test_normalize_name() -> None:
    assert normalize_name("  П’ять   Хвилин ") == "п'ять хвилин"
    assert normalize_name("Дев`ять") == "дев'ять"
