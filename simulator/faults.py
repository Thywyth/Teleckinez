"""Fault injection for simulated devices (docs/specs/devices.md §4).

Faults are switched on/off with a JSON message on `{prefix}/sim/faults`
(web panel, CLI, scenario file):

    {"device_id": "lock.front_door", "fault": "offline"}
    {"device_id": "light.kitchen_main", "fault": "drop_rate", "params": {"p": 0.5}}
    {"device_id": "light.kitchen_main", "fault": "slow", "params": {"ms": 3000}}
    {"device_id": "light.kitchen_main", "fault": "slow", "enabled": false}
    {"device_id": "*", "fault": "clear"}

| fault     | behaviour                                                         |
|-----------|-------------------------------------------------------------------|
| offline   | TCP connection dropped without DISCONNECT -> broker sends the LWT |
| freeze    | connected, but silent: no ack, no state, no heartbeat              |
| drop_rate | a share `p` of commands is lost (no ack, not executed)             |
| stuck     | ack `accepted`, but the state does not change                      |
| slow      | extra delay `ms` before ack and execution                         |
| reject    | ack `rejected` with `error` (default `device_error`)              |
| drift     | one-shot: the state changes by itself (someone pressed a switch)  |
"""

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

PERSISTENT = ("offline", "freeze", "drop_rate", "stuck", "slow", "reject")
ONE_SHOT = ("drift",)
FAULTS = (*PERSISTENT, *ONE_SHOT, "clear")
DEFAULTS: dict[str, dict[str, Any]] = {
    "drop_rate": {"p": 0.5},
    "slow": {"ms": 3000},
    "reject": {"error": "device_error"},
}


class FaultError(ValueError):
    pass


@dataclass(frozen=True)
class FaultCommand:
    device_id: str  # "*" = every device
    fault: str
    params: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "fault": self.fault,
            "params": self.params,
            "enabled": self.enabled,
        }


def parse_fault(data: dict[str, Any] | bytes | str) -> FaultCommand:
    """Validate a fault message (raises FaultError with a readable reason)."""
    if isinstance(data, bytes | str):
        try:
            data = json.loads(data)
        except ValueError as exc:
            raise FaultError(f"not JSON: {exc}") from None
    if not isinstance(data, dict):
        raise FaultError("fault message must be a JSON object")
    device_id, fault = data.get("device_id"), data.get("fault")
    if not isinstance(device_id, str) or not device_id:
        raise FaultError("device_id is required")
    if fault not in FAULTS:
        raise FaultError(f"unknown fault {fault!r}; expected one of {', '.join(FAULTS)}")
    params = {**DEFAULTS.get(fault, {}), **(data.get("params") or {})}
    if fault == "drop_rate" and not 0 <= float(params["p"]) <= 1:
        raise FaultError("drop_rate.p must be within 0..1")
    if fault == "slow" and float(params["ms"]) < 0:
        raise FaultError("slow.ms must be >= 0")
    return FaultCommand(device_id, fault, params, bool(data.get("enabled", True)))


@dataclass
class FaultSet:
    """Active faults of one device (several may combine, e.g. slow + drop_rate)."""

    active: dict[str, dict[str, Any]] = field(default_factory=dict)

    def apply(self, command: FaultCommand) -> None:
        if command.fault == "clear":
            self.active.clear()
        elif command.fault in PERSISTENT:
            if command.enabled:
                self.active[command.fault] = dict(command.params)
            else:
                self.active.pop(command.fault, None)

    def has(self, fault: str) -> bool:
        return fault in self.active

    @property
    def silent(self) -> bool:
        """Nothing at all is sent (offline or frozen)."""
        return self.has("offline") or self.has("freeze")

    def delay_s(self) -> float:
        return float(self.active["slow"]["ms"]) / 1000 if self.has("slow") else 0.0

    def drops(self, rng: random.Random) -> bool:
        return self.has("drop_rate") and rng.random() < float(self.active["drop_rate"]["p"])

    def describe(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self.active.items()}


def drift_change(state: dict[str, Any], writable: dict[str, Any], rng: random.Random) -> dict:
    """A plausible manual change: flip `on`/`locked`, or move a numeric attribute."""
    if "on" in writable:
        return {"on": not state.get("on", False)}
    if "locked" in writable:
        return {"locked": not state.get("locked", True)}
    if "position" in writable:
        return {"position": 0 if state.get("position", 0) > 50 else 100}
    for name, cap in writable.items():
        if cap.kind in ("int", "float") and cap.min is not None and cap.max is not None:
            value = rng.uniform(cap.min, cap.max)
            return {name: round(value) if cap.kind == "int" else round(value, 1)}
    return {}


# --------------------------------------------------------------------- scenarios


@dataclass(frozen=True)
class ScenarioStep:
    at_s: float
    command: FaultCommand


def load_scenario(path: str | Path) -> list[ScenarioStep]:
    """YAML list of {at_s, device_id, fault, params?, enabled?} (fault_scenarios.yaml)."""
    with Path(path).open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    steps = data.get("steps", []) if isinstance(data, dict) else data
    out = []
    for i, raw in enumerate(steps):
        if "at_s" not in raw:
            raise FaultError(f"step {i}: at_s is required")
        out.append(ScenarioStep(float(raw["at_s"]), parse_fault(raw)))
    return sorted(out, key=lambda s: s.at_s)


async def run_scenario(steps: list[ScenarioStep], apply) -> None:
    """Call apply(command) for every step at its time offset."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    for step in steps:
        await asyncio.sleep(max(0.0, start + step.at_s - loop.time()))
        log.info("scenario t=%.1fs: %s", step.at_s, step.command)
        apply(step.command)
