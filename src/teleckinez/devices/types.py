"""Device type catalogue (docs/specs/devices.md §2).

A DeviceType knows its capabilities, which user-level actions it supports,
how an action turns into a desired state, when the reported state counts as
"converged" with the desired one, and how long execution may take.

The assistant always sends absolute desired values (`set`), computed here
from the action and the last reported state, so retries stay idempotent and
convergence is a simple comparison.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from teleckinez.devices.capabilities import (
    Capability,
    UnsupportedAction,
    ValueOutOfRange,
    bool_cap,
    enum_cap,
    float_cap,
    int_cap,
)

State = Mapping[str, Any]
ActionFn = Callable[["DeviceType", dict[str, Any], State], dict[str, Any]]

COLORS = ("white", "warm", "red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink")


@dataclass(frozen=True)
class DeviceType:
    name: str
    capabilities: tuple[Capability, ...]
    actions: Mapping[str, ActionFn] = field(default_factory=dict)
    aliases: Mapping[str, str] = field(default_factory=dict)  # e.g. door_lock: open -> unlock
    default_action: str | None = None  # used when the phrase has no verb ("штори вітальня")
    steps: Mapping[str, float] = field(default_factory=dict)  # "гучніше" -> volume +10
    speed: float | None = None  # position units per second for moving devices
    state_timeout_s: float | None = None  # type default, settings.yaml may override

    # ----------------------------------------------------------------- queries

    def cap(self, name: str) -> Capability:
        for capability in self.capabilities:
            if capability.name == name:
                return capability
        raise KeyError(name)

    def has(self, name: str) -> bool:
        return any(c.name == name for c in self.capabilities)

    @property
    def writable(self) -> tuple[Capability, ...]:
        return tuple(c for c in self.capabilities if not c.readonly)

    @property
    def is_sensor(self) -> bool:
        return not self.actions

    def supports(self, action: str) -> bool:
        return self.aliases.get(action, action) in self.actions

    def initial_state(self) -> dict[str, Any]:
        return {c.name: c.initial() for c in self.capabilities}

    def meta(self) -> dict[str, Any]:
        return {c.name: c.describe() for c in self.capabilities}

    # ---------------------------------------------------------------- commands

    def desired_for(
        self, action: str, params: dict[str, Any] | None = None, reported: State | None = None
    ) -> dict[str, Any]:
        """Desired state for an action, validated; raises UnsupportedAction / ValueOutOfRange."""
        canonical = self.aliases.get(action, action)
        fn = self.actions.get(canonical)
        if fn is None:
            raise UnsupportedAction(f"{self.name} does not support {action!r}", action=action)
        desired = fn(self, dict(params or {}), reported or {})
        out: dict[str, Any] = {}
        for key, value in desired.items():
            if not self.has(key) or self.cap(key).readonly:
                raise UnsupportedAction(f"{self.name}.{key} is not writable", attribute=key)
            out[key] = self.cap(key).coerce(value)
        if not out:
            raise UnsupportedAction(f"{action!r} on {self.name} changes nothing", action=action)
        return out

    def converged(self, desired: State, reported: State) -> bool:
        """True when the reported state satisfies the desired one (ADR-006)."""
        for key, value in desired.items():
            if key not in reported or not self.cap(key).equal(reported[key], value):
                return False
        # Moving devices are done only when they stopped at the target.
        return not reported.get("moving", False)

    def state_timeout(
        self,
        desired: State,
        reported: State,
        overrides: Mapping[str, float],
        margin_s: float,
    ) -> float:
        """How long to wait for convergence after ACCEPTED (dispatcher.md §3)."""
        if self.speed and "position" in desired:
            start = reported.get("position")
            distance = abs(desired["position"] - start) if isinstance(start, int | float) else 100
            return distance / self.speed + margin_s
        if self.name in overrides:
            return overrides[self.name]
        if self.state_timeout_s is not None:
            return self.state_timeout_s
        return overrides.get("default", 3.0)


# ------------------------------------------------------------------- action helpers


def _turn_on(t: DeviceType, p: dict, r: State) -> dict:
    return {"on": True}


def _turn_off(t: DeviceType, p: dict, r: State) -> dict:
    return {"on": False}


def _toggle_on(t: DeviceType, p: dict, r: State) -> dict:
    return {"on": not r.get("on", False)}


def _with_power(t: DeviceType, desired: dict) -> dict:
    """Setting brightness/volume/speed of a device that is off also turns it on."""
    if t.has("on") and "on" not in desired:
        return {"on": True, **desired}
    return desired


def _set(t: DeviceType, p: dict, r: State) -> dict:
    if not p:
        raise ValueOutOfRange(f"set on {t.name} needs parameters")
    return _with_power(t, p)


def _step(t: DeviceType, p: dict, r: State) -> dict:
    """Relative change: {"volume": +10}; {"volume": None} uses the type's step size."""
    if not p:
        raise ValueOutOfRange(f"step on {t.name} needs an attribute")
    desired = {}
    for key, delta in p.items():
        if not t.has(key):
            raise UnsupportedAction(f"{t.name} has no {key}", attribute=key)
        cap = t.cap(key)
        if delta is None:
            delta = t.steps.get(key, 1)
        base = r.get(key, cap.initial())
        desired[key] = cap.clamp(base + delta)
    return _with_power(t, desired)


def _open(t: DeviceType, p: dict, r: State) -> dict:
    return {"position": 100}


def _close(t: DeviceType, p: dict, r: State) -> dict:
    return {"position": 0}


def _toggle_position(t: DeviceType, p: dict, r: State) -> dict:
    return {"position": 0 if r.get("position", 0) > 0 else 100}


def _lock(t: DeviceType, p: dict, r: State) -> dict:
    return {"locked": True}


def _unlock(t: DeviceType, p: dict, r: State) -> dict:
    return {"locked": False}


def _vacuum_start(t: DeviceType, p: dict, r: State) -> dict:
    return {"status": "cleaning"}


def _vacuum_dock(t: DeviceType, p: dict, r: State) -> dict:
    return {"status": "docked"}


ON_OFF: dict[str, ActionFn] = {"turn_on": _turn_on, "turn_off": _turn_off, "toggle": _toggle_on}
POSITION: dict[str, ActionFn] = {
    "open": _open,
    "close": _close,
    "toggle": _toggle_position,
    "set": _set,
    "step": _step,
}
ADJUSTABLE: dict[str, ActionFn] = {**ON_OFF, "set": _set, "step": _step}


# ------------------------------------------------------------------------ catalogue

DEVICE_TYPES: dict[str, DeviceType] = {
    t.name: t
    for t in (
        DeviceType(
            "light",
            (bool_cap("on"), int_cap("brightness", 1, 100, unit="%", default=100)),
            ADJUSTABLE,
            default_action="toggle",
            steps={"brightness": 20},
        ),
        DeviceType(
            "rgb_light",
            (
                bool_cap("on"),
                int_cap("brightness", 1, 100, unit="%", default=100),
                enum_cap("color", *COLORS),
            ),
            ADJUSTABLE,
            default_action="toggle",
            steps={"brightness": 20},
        ),
        DeviceType(
            "curtains",
            (int_cap("position", 0, 100, unit="%"), bool_cap("moving", readonly=True)),
            POSITION,
            default_action="toggle",
            steps={"position": 25},
            speed=25.0,
        ),
        DeviceType(
            "thermostat",
            (
                float_cap("target_temp", 16, 30, unit="°C", default=21.0),
                float_cap("current_temp", -40, 60, unit="°C", readonly=True, default=20.0),
                bool_cap("heating", readonly=True),
            ),
            {"set": _set, "step": _step},
            steps={"target_temp": 1},
        ),
        DeviceType(
            "air_conditioner",
            (
                bool_cap("on"),
                enum_cap("mode", "cool", "heat", "fan", "dry", "auto"),
                float_cap("target_temp", 16, 30, unit="°C", default=24.0),
            ),
            ADJUSTABLE,
            default_action="toggle",
            steps={"target_temp": 1},
        ),
        DeviceType(
            "socket",
            (bool_cap("on"), float_cap("power_w", 0, 3600, unit="W", readonly=True)),
            ON_OFF,
            default_action="toggle",
        ),
        DeviceType(
            "kettle",
            (
                bool_cap("on"),
                float_cap("water_temp", 0, 100, unit="°C", readonly=True, default=20.0),
            ),
            ON_OFF,
            default_action="turn_on",
        ),
        DeviceType(
            "boiler",
            (
                bool_cap("on"),
                float_cap("water_temp", 0, 90, unit="°C", readonly=True, default=35.0),
            ),
            ON_OFF,
            default_action="toggle",
        ),
        DeviceType(
            "fan",
            (bool_cap("on"), int_cap("speed", 1, 3, default=1)),
            ADJUSTABLE,
            default_action="toggle",
            steps={"speed": 1},
        ),
        DeviceType(
            "door_lock",
            (bool_cap("locked", default=True),),
            {"lock": _lock, "unlock": _unlock},
            aliases={"close": "lock", "open": "unlock", "turn_on": "lock", "turn_off": "unlock"},
            state_timeout_s=3.0,
        ),
        DeviceType(
            "garage_gate",
            (int_cap("position", 0, 100, unit="%"), bool_cap("moving", readonly=True)),
            POSITION,
            default_action="toggle",
            speed=12.5,
        ),
        DeviceType(
            "robot_vacuum",
            (
                enum_cap("status", "docked", "cleaning", "returning"),
                int_cap("battery", 0, 100, unit="%", readonly=True, default=100),
            ),
            {"start": _vacuum_start, "dock": _vacuum_dock},
            aliases={"turn_on": "start", "turn_off": "dock", "stop": "dock"},
            default_action="start",
            state_timeout_s=15.0,
        ),
        DeviceType(
            "tv",
            (bool_cap("on"), int_cap("volume", 0, 100, default=20)),
            ADJUSTABLE,
            default_action="toggle",
            steps={"volume": 10},
        ),
        DeviceType(
            "humidifier",
            (bool_cap("on"), int_cap("level", 1, 3, default=2)),
            ADJUSTABLE,
            default_action="toggle",
            steps={"level": 1},
        ),
        DeviceType(
            "climate_sensor",
            (
                float_cap("temperature", -40, 60, unit="°C", readonly=True, default=21.0),
                float_cap("humidity", 0, 100, unit="%", readonly=True, default=45.0),
            ),
        ),
        DeviceType("motion_sensor", (bool_cap("motion", readonly=True),)),
        DeviceType("door_sensor", (bool_cap("open", readonly=True),)),
    )
}
