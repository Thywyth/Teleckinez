"""Per-type physics: how a simulated device reaches the commanded state over time.

`apply()` is called after a command was accepted; it may change the state at
once (lights) or over time (curtains, gates, vacuum). `tick()` runs
periodically for continuous processes (heating, battery, sensor noise).
Both return True when something worth publishing changed.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any

State = dict[str, Any]
Publish = Callable[[], Awaitable[None]]

MOVE_STEP_S = 0.25  # how often a moving device reports intermediate positions


class Physics:
    def __init__(self, state: State, params: dict[str, Any], rng: random.Random) -> None:
        self.state = state
        self.params = params
        self.rng = rng

    async def apply(self, desired: State, publish: Publish) -> None:
        self.state.update(desired)
        await publish()

    def tick(self, dt: float) -> bool:
        return False


class Instant(Physics):
    pass


class Moving(Physics):
    """Curtains and gates: position changes at `speed` %/s with intermediate states."""

    def __init__(self, state: State, params: dict[str, Any], rng: random.Random, speed: float):
        super().__init__(state, params, rng)
        self.speed = float(params.get("speed", speed))

    async def apply(self, desired: State, publish: Publish) -> None:
        target = desired["position"]
        self.state["moving"] = self.state["position"] != target
        await publish()
        while self.state["position"] != target:
            await asyncio.sleep(MOVE_STEP_S)
            step = self.speed * MOVE_STEP_S
            pos = self.state["position"]
            pos = min(target, pos + step) if target > pos else max(target, pos - step)
            self.state["position"] = round(pos)
            if self.state["position"] == target:
                break
            await publish()
        self.state["moving"] = False
        await publish()


class Thermostat(Physics):
    """current_temp slowly follows target_temp; heating while below target."""

    rate = 0.05  # °C per second

    def tick(self, dt: float) -> bool:
        cur, target = self.state["current_temp"], self.state["target_temp"]
        if abs(cur - target) > 0.01:
            step = min(self.rate * dt, abs(target - cur))
            self.state["current_temp"] = round(cur + step if target > cur else cur - step, 2)
        heating = self.state["current_temp"] < target - 0.2
        changed = heating != self.state["heating"]
        self.state["heating"] = heating
        return changed


class WaterHeater(Physics):
    """Kettle and boiler: heat while on; the kettle switches itself off at 100 °C."""

    def __init__(
        self,
        state: State,
        params: dict[str, Any],
        rng: random.Random,
        *,
        heat_rate: float,
        max_temp: float,
        auto_off: bool,
    ) -> None:
        super().__init__(state, params, rng)
        self.heat_rate = float(params.get("heat_rate", heat_rate))
        self.max_temp = max_temp
        self.auto_off = auto_off
        self._last_reported = state["water_temp"]

    def tick(self, dt: float) -> bool:
        temp = self.state["water_temp"]
        if self.state["on"]:
            temp = min(self.max_temp, temp + self.heat_rate * dt)
            if self.auto_off and temp >= self.max_temp:
                self.state["on"] = False
                self.state["water_temp"] = round(temp, 1)
                self._last_reported = temp
                return True
        else:
            temp = max(20.0, temp - 0.02 * dt)
        self.state["water_temp"] = round(temp, 1)
        if abs(temp - self._last_reported) >= 5:  # report noticeable changes only
            self._last_reported = temp
            return True
        return False


class Socket(Physics):
    async def apply(self, desired: State, publish: Publish) -> None:
        self.state.update(desired)
        self.state["power_w"] = float(self.params.get("power_w", 60)) if self.state["on"] else 0.0
        await publish()


class Vacuum(Physics):
    return_s = 3.0

    def __init__(self, state: State, params: dict[str, Any], rng: random.Random) -> None:
        super().__init__(state, params, rng)
        self._battery = float(state["battery"])  # fractional, the state holds whole percent

    async def apply(self, desired: State, publish: Publish) -> None:
        if desired.get("status") == "docked" and self.state["status"] == "cleaning":
            self.state["status"] = "returning"
            await publish()
            await asyncio.sleep(float(self.params.get("return_s", self.return_s)))
        self.state.update(desired)
        await publish()

    def tick(self, dt: float) -> bool:
        if self.state["status"] == "cleaning":
            self._battery = max(0.0, self._battery - 0.1 * dt)
        elif self.state["status"] == "docked":
            self._battery = min(100.0, self._battery + 0.5 * dt)
        new = round(self._battery)
        changed = new != self.state["battery"]
        self.state["battery"] = new
        return changed and new % 5 == 0  # report every 5 %


class ClimateSensor(Physics):
    """Small random walk around the initial values; published with heartbeats."""

    def __init__(self, state: State, params: dict[str, Any], rng: random.Random) -> None:
        super().__init__(state, params, rng)
        self.base = dict(state)

    def tick(self, dt: float) -> bool:
        for key, spread in (("temperature", 0.5), ("humidity", 2.0)):
            value = self.state[key] + self.rng.uniform(-0.02, 0.02) * dt
            low, high = self.base[key] - spread, self.base[key] + spread
            self.state[key] = round(min(high, max(low, value)), 2)
        return False


class MotionSensor(Physics):
    """Random motion events: on average one per `period_s`, lasting `hold_s`."""

    def __init__(self, state: State, params: dict[str, Any], rng: random.Random) -> None:
        super().__init__(state, params, rng)
        self.period_s = float(params.get("period_s", 60))
        self.hold_s = float(params.get("hold_s", 5))
        self._left = 0.0

    def tick(self, dt: float) -> bool:
        if self.state["motion"]:
            self._left -= dt
            if self._left <= 0:
                self.state["motion"] = False
                return True
        elif self.rng.random() < dt / self.period_s:
            self.state["motion"] = True
            self._left = self.hold_s
            return True
        return False


def make_physics(
    type_name: str, state: State, params: dict[str, Any], rng: random.Random, speed: float | None
) -> Physics:
    if type_name in ("curtains", "garage_gate"):
        return Moving(state, params, rng, speed or 25.0)
    if type_name == "thermostat":
        return Thermostat(state, params, rng)
    if type_name == "kettle":
        return WaterHeater(state, params, rng, heat_rate=1.5, max_temp=100.0, auto_off=True)
    if type_name == "boiler":
        return WaterHeater(state, params, rng, heat_rate=0.2, max_temp=75.0, auto_off=False)
    if type_name == "socket":
        return Socket(state, params, rng)
    if type_name == "robot_vacuum":
        return Vacuum(state, params, rng)
    if type_name == "climate_sensor":
        return ClimateSensor(state, params, rng)
    if type_name == "motion_sensor":
        return MotionSensor(state, params, rng)
    return Instant(state, params, rng)
