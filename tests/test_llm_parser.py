"""LlmParser (nlu.md §3): schema, mapping, fallbacks, hybrid routing; real Ollama if available."""

import asyncio
import json

import pytest

from teleckinez.nlu.llm_parser import LlmParser, ModeParser, build_answer_model
from teleckinez.nlu.resolver import TargetResolver
from teleckinez.nlu.rule_parser import RuleParser


class FakeOllama:
    """Returns canned JSON per phrase; records calls."""

    def __init__(self, answers: dict[str, object], delay_s: float = 0.0) -> None:
        self.answers, self.delay_s, self.calls = answers, delay_s, []

    async def chat(self, model, messages, format, options, keep_alive):
        text = messages[-1]["content"]
        self.calls.append(text)
        await asyncio.sleep(self.delay_s)
        answer = self.answers[text]
        if isinstance(answer, Exception):
            raise answer
        content = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        return {"message": {"content": content}}


def llm(registry, answers, **kw) -> LlmParser:
    return LlmParser(registry, client=FakeOllama(answers, kw.pop("delay_s", 0.0)), **kw)


def test_schema_constrains_answers_to_this_home(registry) -> None:
    schema = json.dumps(build_answer_model(registry).model_json_schema(), ensure_ascii=False)
    assert "kitchen" in schema and "світло" in schema and "all_lights" in schema
    model = build_answer_model(registry)
    with pytest.raises(ValueError):
        model.model_validate({"kind": "command", "room": "attic"})  # a room that does not exist


@pytest.mark.parametrize(
    ("text", "answer", "commands"),
    [
        (
            "запали люстру в кухні будь ласка",
            {"kind": "command", "action": "turn_on", "device_name": "люстра", "room": "kitchen"},
            [("light.kitchen_main", "turn_on", {})],
        ),
        (
            "приглуши трохи світло у спальні до сорока",
            {"kind": "command", "action": "set", "device_name": "світло", "room": "bedroom",
             "value": 40, "unit": "percent"},
            [("light.bedroom_main", "set", {"brightness": 40})],
        ),
        (
            "зроби, щоб у телевізора було тихіше",
            {"kind": "command", "action": "step", "device_name": "телевізор",
             "step_direction": "down", "step_attribute": "volume"},
            [("tv.living_room", "step", {"volume": -10})],
        ),
        (
            "погаси всюди світло",
            {"kind": "command", "action": "turn_off", "group": "all_lights"},
            None,
        ),
    ],
)  # fmt: skip
async def test_llm_answers_become_the_same_commands(registry, text, answer, commands) -> None:
    result = await llm(registry, {text: answer}).aparse(text)
    assert result.ok and result.intent.parser == "llm"
    res = TargetResolver(registry).resolve(result.intent)
    assert res.ok
    if commands is None:
        assert len(res.commands) == 11
    else:
        assert [(c.device_id, c.action, c.params) for c in res.commands] == commands


async def test_llm_query_with_expectation(registry) -> None:
    text = "скажи, двері в мене закриті?"
    result = await llm(
        registry, {text: {"kind": "query", "device_name": "двері", "expect": "closed"}}
    ).aparse(text)
    assert result.intent.kind == "query" and result.intent.params == {"expect": "closed"}


@pytest.mark.parametrize(
    "answer",
    [
        "not json at all",
        {"kind": "command", "device_name": "холодильник"},  # not in this home
        ConnectionError("Ollama is not running"),
    ],
)
async def test_llm_errors_are_reported(registry, answer) -> None:
    parser = llm(registry, {"x": answer})
    result = await parser.aparse("x")
    assert not result.ok and result.reason == "llm_error" and parser.last_error


async def test_llm_timeout(registry) -> None:
    parser = llm(registry, {"x": {"kind": "unknown"}}, delay_s=0.2, timeout_s=0.05)
    result = await parser.aparse("x")
    assert result.reason == "llm_error" and "timeout" in parser.last_error


async def test_unknown_kind_means_not_understood(registry) -> None:
    result = await llm(registry, {"розкажи анекдот": {"kind": "unknown"}}).aparse("розкажи анекдот")
    assert result.reason == "not_understood"


async def test_llm_mode_falls_back_to_rules_on_errors(registry) -> None:
    text = "увімкни світло на кухні"
    mode = ModeParser(RuleParser(registry), llm(registry, {text: ConnectionError("down")}), "llm")
    result = await mode.aparse(text)
    assert result.ok and result.intent.parser == "rule"


async def test_hybrid_uses_llm_only_when_rules_are_unsure(registry) -> None:
    sure = "увімкни світло на кухні"
    odd = "на кухні темнувато"  # no verb and no device: the rules give up
    fake = FakeOllama({odd: {"kind": "command", "action": "turn_on", "device_name": "світло",
                             "room": "kitchen", "confidence": 0.9}})  # fmt: skip
    rules = RuleParser(registry)
    mode = ModeParser(rules, LlmParser(registry, client=fake), "hybrid", hybrid_threshold=0.95)
    assert (await mode.aparse(sure)).intent.parser == "rule"
    assert fake.calls == []  # rules were sure enough, no LLM call
    assert not rules.parse(odd).ok
    result = await mode.aparse(odd)
    assert fake.calls == [odd] and result.intent.parser == "llm"


def test_rule_coverage(registry) -> None:
    rules = RuleParser(registry)
    assert rules.parse("увімкни світло на кухні будь ласка").coverage == 1.0
    assert rules.parse("приглуши трохи торшер").coverage == pytest.approx(0.333)


async def test_hybrid_asks_llm_when_rules_skipped_words(registry) -> None:
    odd = "приглуши трохи торшер"  # rules: only "торшер" -> would toggle it
    answer = {"kind": "command", "action": "step", "device_name": "торшер",
              "step_direction": "down", "step_attribute": "brightness"}  # fmt: skip
    fake = FakeOllama({odd: answer})
    mode = ModeParser(RuleParser(registry), LlmParser(registry, client=fake), "hybrid")
    result = await mode.aparse(odd)
    assert result.intent.parser == "llm" and result.intent.action == "step"


async def test_rule_mode_never_calls_the_llm(registry) -> None:
    fake = FakeOllama({})
    mode = ModeParser(RuleParser(registry), LlmParser(registry, client=fake), "rule")
    assert (await mode.aparse("увімкни світло на кухні")).ok and fake.calls == []


# --------------------------------------------------------------- real Ollama


def _ollama_model_ready(model: str) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1) as r:
            return any(m["name"] == model for m in json.load(r)["models"])
    except OSError:
        return False


@pytest.mark.llm
@pytest.mark.parametrize(
    ("text", "device"),
    [
        ("увімкни світло на кухні", "light.kitchen_main"),
        ("open the curtains in the bedroom", "curtains.bedroom"),
        ("запали люстру у вітальні", "light.living_main"),
    ],
)
async def test_real_ollama(registry, text: str, device: str) -> None:
    if not _ollama_model_ready("qwen2.5:3b"):
        pytest.skip("Ollama with qwen2.5:3b is not available (ollama pull qwen2.5:3b)")
    parser = LlmParser(registry, timeout_s=60)
    result = await parser.aparse(text)
    assert result.ok, parser.last_error
    res = TargetResolver(registry).resolve(result.intent)
    assert [c.device_id for c in res.commands] == [device]
