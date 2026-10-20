"""LlmParser: a local LLM through Ollama with structured output (nlu.md §3, ADR-005).

The answer schema is built from devices.yaml: rooms, device names, groups and
actions are JSON-schema enums, so the model cannot invent a device. The JSON
is validated with pydantic and mapped onto the same Intent the RuleParser
produces (generic `percent` / `degrees` / `step` params included), so the
resolver and dispatcher do not care which parser was used. Anything wrong -
Ollama not running, a timeout, invalid JSON - falls back to the RuleParser.

Modes (settings.nlu.mode): `rule`, `llm` (LLM first, rules as fallback) and
`hybrid` (rules first, LLM only when the rules are unsure or fail).
"""

import asyncio
import json
import logging
import time
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, create_model

from teleckinez.core.models import Intent
from teleckinez.devices.registry import DeviceRegistry
from teleckinez.devices.types import COLORS, DEVICE_TYPES
from teleckinez.nlu import lexicon
from teleckinez.nlu.normalizer import detect_lang
from teleckinez.nlu.rule_parser import ParseResult, RuleParser

log = logging.getLogger(__name__)

STEP_ATTRIBUTES = ("brightness", "volume", "target_temp", "speed", "level", "position")
QUERY_ATTRIBUTES = ("target_temp", "humidity", "motion", "battery", "power_w", "water_temp")

SYSTEM_PROMPT = """You turn smart-home requests (Ukrainian or English) into JSON.
Use only the ids and names listed below; use null when something is not said.

Rooms (id: names): {rooms}
Device names (name: type, rooms): {names}
Groups (id: names): {groups}

Fields:
- kind: "command" (change something), "query" (ask about a state)
  or "unknown" (not a smart-home request)
- action: one of {actions}; null if the phrase has no verb ("штори вітальня")
- device_name: a name from the list exactly as written, if the phrase names a device
- device_type: the device type, if no exact name fits
- room: room id; group: group id, or "all" for "все/всі/all" without a named group
- value + unit: a number from the phrase; unit "percent" for %, "degrees" for °C, else "none"
- color, mode: for colored lights / air conditioners
- step_direction + step_attribute: for "louder / brighter / warmer / тихіше / яскравіше"
- expect: for yes/no questions or "what is on?": on | off | open | closed
- attribute: what a query asks for: {query_attributes}
- confidence: 0..1, how sure you are"""

EXAMPLES: list[tuple[str, dict[str, Any]]] = [
    ("увімкни світло на кухні",
     {"kind": "command", "action": "turn_on", "device_name": "світло", "room": "kitchen"}),
    ("штори вітальня", {"kind": "command", "device_name": "штори", "room": "living_room"}),
    ("set the bedroom light to 40 percent",
     {"kind": "command", "action": "set", "device_name": "light", "room": "bedroom", "value": 40,
      "unit": "percent"}),
    ("зроби телевізор тихіше",
     {"kind": "command", "action": "step", "device_name": "телевізор", "step_direction": "down",
      "step_attribute": "volume"}),
    ("чи зачинені двері?", {"kind": "query", "device_name": "двері", "expect": "closed"}),
    ("яка температура у спальні", {"kind": "query", "room": "bedroom", "attribute": "target_temp"}),
    ("вимкни все світло", {"kind": "command", "action": "turn_off", "group": "all_lights"}),
    ("розкажи анекдот", {"kind": "unknown"}),
]  # fmt: skip


def _enum(values) -> Any:
    """Optional Literal of the given values (JSON schema enum + null)."""
    values = tuple(dict.fromkeys(values))
    return Literal[values] | None  # type: ignore[valid-type]


def build_answer_model(registry: DeviceRegistry) -> type[BaseModel]:
    """Pydantic model whose JSON schema constrains the LLM to this home."""
    names = sorted(registry.names())
    return create_model(
        "LlmAnswer",
        kind=(Literal["command", "query", "unknown"], ...),
        action=(_enum([*lexicon.ACTIONS, "step"]), None),
        device_name=(_enum(names), None),
        device_type=(_enum(DEVICE_TYPES), None),
        room=(_enum(registry.rooms), None),
        group=(_enum([*registry.config.groups, "all"]), None),
        value=(float | None, None),
        unit=(_enum(["percent", "degrees", "none"]), None),
        color=(_enum(COLORS), None),
        mode=(_enum(lexicon.AC_MODES), None),
        step_direction=(_enum(["up", "down"]), None),
        step_attribute=(_enum(STEP_ATTRIBUTES), None),
        expect=(_enum(["on", "off", "open", "closed"]), None),
        attribute=(_enum(QUERY_ATTRIBUTES), None),
        confidence=(float, Field(0.8, ge=0, le=1)),
    )


def build_prompt(registry: DeviceRegistry) -> str:
    rooms = "; ".join(f"{rid}: {', '.join(r.all()[:4])}" for rid, r in registry.rooms.items())
    names = "; ".join(
        f"{name}: {'/'.join(sorted({d.type.name for d in devs}))} in "
        f"{', '.join(sorted({d.room for d in devs}))}"
        for name, devs in sorted(registry.names().items())
    )
    groups = "; ".join(f"{g}: {', '.join(n.all()[:3])}" for g, n in registry.config.groups.items())
    return SYSTEM_PROMPT.format(
        rooms=rooms,
        names=names,
        groups=groups or "-",
        actions=", ".join([*lexicon.ACTIONS, "step"]),
        query_attributes=", ".join(QUERY_ATTRIBUTES),
    )


def answer_to_intent(answer: BaseModel, text: str, lang: str) -> Intent | None:
    """Map the validated LLM answer onto an Intent like the RuleParser builds."""
    a = answer.model_dump()
    if a["kind"] == "unknown":
        return None
    params: dict[str, Any] = {}
    if a["kind"] == "query":
        if a["attribute"]:
            params["attribute"] = a["attribute"]
        if a["expect"]:
            params["expect"] = a["expect"]
        action = None
    else:
        action = a["action"]
        value = a["value"]
        if value is not None:
            value = int(value) if float(value).is_integer() else value
        if a["step_direction"]:
            amount = value if a["unit"] in ("percent", "none") else None
            params["step"] = {
                "attribute": a["step_attribute"] or "",
                "direction": 1 if a["step_direction"] == "up" else -1,
                "amount": amount,
            }
            action = "step"
        else:
            if value is not None:
                key = {"percent": "percent", "degrees": "degrees"}.get(a["unit"] or "", "number")
                params[key] = value
            if a["color"]:
                params["color"] = a["color"]
            if a["mode"]:
                params["mode"] = a["mode"]
            if params and action in (None, "set", "turn_on"):
                action = "set"
    if not any((a["device_name"], a["device_type"], a["room"], a["group"], params)):
        return None
    return Intent(
        kind=a["kind"],
        action=action,
        device_type=a["device_type"],
        device_name=a["device_name"],
        room=a["room"],
        group=a["group"],
        params=params,
        lang=lang,
        confidence=round(float(a["confidence"]), 3),
        raw_text=text,
        parser="llm",
    )


class LlmParser:
    name = "llm"

    def __init__(
        self,
        registry: DeviceRegistry,
        *,
        model: str = "qwen2.5:3b",
        host: str = "http://127.0.0.1:11434",
        timeout_s: float = 10.0,
        keep_alive: str = "10m",
        client: Any = None,
    ) -> None:
        self.registry = registry
        self.model = model
        self.timeout_s = timeout_s
        self.keep_alive = keep_alive
        self.answer_model = build_answer_model(registry)
        self.schema = self.answer_model.model_json_schema()
        self.system = build_prompt(registry)
        if client is None:
            import ollama

            client = ollama.AsyncClient(host=host)
        self.client = client
        self.last_error: str | None = None
        self.last_latency_s: float | None = None

    def _messages(self, text: str) -> list[dict[str, str]]:
        messages = [{"role": "system", "content": self.system}]
        for example, answer in EXAMPLES:
            messages.append({"role": "user", "content": example})
            messages.append(
                {"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)}
            )
        messages.append({"role": "user", "content": text})
        return messages

    async def aparse(self, text: str) -> ParseResult:
        """LLM only (no fallback): ParseResult with reason llm_error / not_understood."""
        started = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                self.client.chat(
                    model=self.model,
                    messages=self._messages(text),
                    format=self.schema,
                    options={"temperature": 0},
                    keep_alive=self.keep_alive,
                ),
                self.timeout_s,
            )
            content = response["message"]["content"] if isinstance(response, dict) else (
                response.message.content
            )  # fmt: skip
            answer = self.answer_model.model_validate_json(content)
        except (ValidationError, ValueError) as exc:
            return self._error(f"invalid answer: {exc}".splitlines()[0])
        except TimeoutError:
            return self._error(f"timeout after {self.timeout_s} s")
        except Exception as exc:  # Ollama not running, model missing, network ...
            return self._error(f"{type(exc).__name__}: {exc}")
        finally:
            self.last_latency_s = time.perf_counter() - started
        self.last_error = None
        intent = answer_to_intent(answer, text, detect_lang(text))
        return ParseResult(intent, None if intent else "not_understood")

    async def warmup(self) -> None:
        """Load the model into memory now, so the first real request is not a cold start."""
        try:
            await self.client.chat(
                model=self.model,
                messages=[{"role": "user", "content": "ok"}],
                options={"num_predict": 1},
                keep_alive=self.keep_alive,
            )
            log.info("LLM %s is loaded", self.model)
        except Exception as exc:
            log.warning("LLM warm-up failed (%s); rules will be used until Ollama answers", exc)

    def _error(self, message: str) -> ParseResult:
        self.last_error = message
        log.warning("LLM parser: %s", message)
        return ParseResult(None, "llm_error")


class ModeParser:
    """Chooses rule / llm / hybrid at run time (settings.nlu.mode, hot-switchable)."""

    def __init__(
        self,
        rules: RuleParser,
        llm: LlmParser | None,
        mode: str = "rule",
        hybrid_threshold: float = 0.8,
    ) -> None:
        self.rules = rules
        self.llm = llm
        self.mode = mode
        self.hybrid_threshold = hybrid_threshold

    @property
    def name(self) -> str:
        return self.mode

    def parse(self, text: str) -> ParseResult:  # synchronous path: rules only
        return self.rules.parse(text)

    async def aparse(self, text: str) -> ParseResult:
        if self.mode == "rule" or self.llm is None:
            return self.rules.parse(text)
        if self.mode == "llm":
            result = await self.llm.aparse(text)
            if result.ok:
                return result
            if result.reason == "not_understood":
                return result  # the model is sure this is not a smart-home request
            return self.rules.parse(text)  # llm_error -> fallback (nlu.md §3)
        # hybrid: rules first; the LLM only for what the rules did not get confidently,
        # including phrases where the rules skipped words they did not know
        ruled = self.rules.parse(text)
        if ruled.ok and ruled.intent:
            sureness = min(ruled.intent.confidence, ruled.coverage)
            if sureness >= self.hybrid_threshold:
                return ruled
        result = await self.llm.aparse(text)
        return result if result.ok else ruled
