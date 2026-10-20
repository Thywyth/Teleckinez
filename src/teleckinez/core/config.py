"""Configuration models: config/settings.yaml and config/devices.yaml.

Both files are validated with pydantic at startup, so a typo in YAML fails
fast with a readable message instead of surfacing later as a strange
runtime error. Device types are validated by DeviceRegistry, because the
type catalogue lives in teleckinez.devices.
"""

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from teleckinez.core.text import normalize_name

ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = ROOT / "config"
Lang = Literal["uk", "en"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ settings.yaml


class MqttSettings(_Model):
    host: str = "127.0.0.1"
    port: int = 1883
    prefix: str = "tkz"
    keepalive_s: int = 10
    client_id: str = "teleckinez-assistant"
    username: str | None = None
    # Never stored in YAML: read from the TKZ_MQTT_PASSWORD environment variable (.env).
    password: str | None = Field(default=None, exclude=True)


class DispatcherSettings(_Model):
    ack_timeout_s: float = 2.0
    max_attempts: int = Field(3, ge=1)
    retry_backoff_s: list[float] = [0.5, 1.0]  # pause before attempt 2, 3, ...
    cmd_ttl_s: float = 5.0
    # Per device type; "default" applies to types not listed. Moving devices
    # (curtains, gates) compute distance / speed + state_margin_s instead.
    state_timeout_s: dict[str, float] = {"default": 3.0, "light": 2.0, "garage_gate": 12.0}
    state_margin_s: float = 2.0
    progress_messages: bool = True  # "Відкриваю..." after ACCEPTED for long actions
    progress_min_s: float = 3.0  # what counts as a long action

    def backoff(self, attempt: int) -> float:
        """Pause before `attempt` (2, 3, ...); the last value repeats."""
        if not self.retry_backoff_s:
            return 0.0
        return self.retry_backoff_s[min(attempt - 2, len(self.retry_backoff_s) - 1)]


class HealthSettings(_Model):
    heartbeat_s: float = 10.0  # used when neither meta nor devices.yaml give one
    stale_factor: float = 3.0  # stale after stale_factor * heartbeat_s without messages
    check_interval_s: float = 1.0


class NluSettings(_Model):
    mode: Literal["rule", "llm", "hybrid"] = "rule"
    fuzzy_threshold: int = Field(85, ge=50, le=100)
    min_confidence: float = Field(0.6, ge=0, le=1)


class VoiceSettings(_Model):
    trigger: Literal["vosk_kws", "openwakeword", "push_to_talk"] = "vosk_kws"
    stt: Literal["vosk", "whisper"] = "vosk"
    stt_lang: Literal["uk", "en", "auto"] = "uk"
    tts: Literal["piper", "sapi", "text"] = "piper"
    input_device: int | str | None = None  # sounddevice index/name; None = system default
    output_device: int | str | None = None
    vad_aggressiveness: int = Field(2, ge=0, le=3)
    vad_start_frames: int = Field(3, ge=1)  # voiced 30 ms frames in a row = speech started
    vad_end_silence_ms: int = Field(700, ge=90)
    vad_pre_roll_ms: int = Field(300, ge=0)
    max_utterance_s: float = Field(8.0, gt=0)
    no_speech_timeout_s: float = Field(5.0, gt=0)


class WebSettings(_Model):
    host: str = "127.0.0.1"
    port: int = 8000


class StorageSettings(_Model):
    path: Path = Path("data/teleckinez.db")


class SimulatorSettings(_Model):
    latency_ms: tuple[int, int] = (20, 80)


class Settings(_Model):
    language: Lang = "uk"
    mqtt: MqttSettings = MqttSettings()
    dispatcher: DispatcherSettings = DispatcherSettings()
    health: HealthSettings = HealthSettings()
    nlu: NluSettings = NluSettings()
    voice: VoiceSettings = VoiceSettings()
    web: WebSettings = WebSettings()
    storage: StorageSettings = StorageSettings()
    simulator: SimulatorSettings = SimulatorSettings()


# ------------------------------------------------------------------- devices.yaml


class Names(_Model):
    """Synonyms per language; normalized (lowercase, unified apostrophes)."""

    uk: list[str] = []
    en: list[str] = []

    @field_validator("uk", "en")
    @classmethod
    def _normalize(cls, values: list[str]) -> list[str]:
        out = [normalize_name(v) for v in values]
        if any(not v for v in out):
            raise ValueError("empty name")
        return list(dict.fromkeys(out))  # dedupe, keep order

    def all(self) -> list[str]:
        return [*self.uk, *self.en]

    def get(self, lang: str) -> list[str]:
        return self.uk if lang == "uk" else self.en


class LocalizedText(_Model):
    uk: str | None = None
    en: str | None = None

    def get(self, lang: str) -> str | None:
        return self.uk if lang == "uk" else self.en


class RoomConfig(Names):
    loc: LocalizedText = LocalizedText()  # "на кухні" / "in the kitchen"


class DeviceConfig(_Model):
    id: str = Field(pattern=r"^[a-z0-9_]+\.[a-z0-9_]+$")
    type: str
    room: str
    names: Names
    groups: list[str] = []
    protocol: Literal["native", "tasmota", "zigbee2mqtt"] = "native"
    critical: bool = False
    title: LocalizedText = LocalizedText()
    plural: bool = False
    heartbeat_s: float | None = None
    sim: dict[str, Any] = {}  # simulator-only overrides (initial state, speed, ...)

    @model_validator(mode="after")
    def _has_names(self) -> "DeviceConfig":
        if not self.names.all():
            raise ValueError(f"device {self.id}: at least one name is required")
        return self


class DevicesConfig(_Model):
    language_default: Lang = "uk"
    rooms: dict[str, RoomConfig]
    groups: dict[str, Names] = {}
    devices: list[DeviceConfig]

    @model_validator(mode="after")
    def _check_references(self) -> "DevicesConfig":
        errors: list[str] = []
        seen: set[str] = set()
        names_in_room: dict[tuple[str, str], str] = {}
        for dev in self.devices:
            if dev.id in seen:
                errors.append(f"duplicate device id {dev.id!r}")
            seen.add(dev.id)
            if dev.room not in self.rooms:
                errors.append(f"device {dev.id}: unknown room {dev.room!r}")
            for group in dev.groups:
                if group not in self.groups:
                    errors.append(f"device {dev.id}: unknown group {group!r}")
            for name in dev.names.all():
                other = names_in_room.setdefault((dev.room, name), dev.id)
                if other != dev.id:
                    errors.append(f"name {name!r} used by both {other} and {dev.id} in {dev.room}")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    def device(self, device_id: str) -> DeviceConfig:
        for dev in self.devices:
            if dev.id == device_id:
                return dev
        raise KeyError(device_id)


# ------------------------------------------------------------------------ loading


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def load_settings(path: str | Path | None = None) -> Settings:
    """Load settings.yaml; a missing file means defaults. Secrets come from the environment."""
    path = Path(path) if path else CONFIG_DIR / "settings.yaml"
    settings = Settings.model_validate(_read_yaml(path) if path.is_file() else {})
    if password := os.environ.get("TKZ_MQTT_PASSWORD"):
        settings.mqtt.password = password
    return settings


def load_devices(path: str | Path | None = None) -> DevicesConfig:
    path = Path(path) if path else CONFIG_DIR / "devices.yaml"
    return DevicesConfig.model_validate(_read_yaml(path))
