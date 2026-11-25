"""Standby mode without the wake word (voice.md "Режим очікування", ADR-014)."""

import asyncio

import numpy as np
import pytest
from test_assistant import env  # noqa: F401  (fixture)
from test_pipeline import FakeEngines, FakeStt, FakeWake, make, running, silence  # noqa: F401
from test_web import client, core  # noqa: F401  (fixtures)

from teleckinez.core.events import (
    IntentFailed,
    IntentParsed,
    SpeakFinished,
    SpeakRequested,
    StandbyChanged,
    TranscriptReady,
)
from teleckinez.core.models import Intent
from teleckinez.nlu import grammar
from teleckinez.nlu.standby import standby_request, strip_wake_word
from teleckinez.voice.audio_io import tone
from teleckinez.voice.pipeline import PipelineState

# --------------------------------------------------------------------- phrases


@pytest.mark.parametrize(
    "text",
    [
        "режим очікування",
        "Увімкни режим очікування, будь ласка",
        "телекінез перейди в режим очікування",
        "standby mode",
        "start listening",
    ],
)
def test_phrases_that_turn_standby_on(text) -> None:
    assert standby_request(text) is True


@pytest.mark.parametrize(
    "text", ["вимкни режим очікування", "Телекінез, вийди з режиму очікування", "stop listening"]
)
def test_phrases_that_turn_standby_off(text) -> None:
    assert standby_request(text) is False


@pytest.mark.parametrize("text", ["увімкни світло на кухні", "який режим кондиціонера", ""])
def test_commands_are_not_standby_phrases(text) -> None:
    assert standby_request(text) is None


def test_wake_word_is_stripped_from_a_standby_phrase() -> None:
    assert strip_wake_word("Телекінез увімкни чайник") == "увімкни чайник"
    assert strip_wake_word("увімкни чайник") == "увімкни чайник"


def test_grammar_knows_the_standby_words(registry) -> None:
    words = set(grammar.command_words(registry, "uk"))
    assert {"режим", "очікування", "режиму", "вийди"} <= words
    assert {"standby", "listening"} <= set(grammar.command_words(registry, "en"))


# -------------------------------------------------------------------- pipeline

COMMAND = np.concatenate([silence(0.5), tone(300, 0.8, volume=0.5), silence(1.5)])


async def test_standby_turns_speech_into_a_command_without_wake_word(running) -> None:  # noqa: F811
    wake = FakeWake()
    audio = np.concatenate([silence(0.3), COMMAND])
    pipeline, rec, output = make(
        audio, FakeEngines(wake=wake, stt=FakeStt("телекінез увімкни чайник")), realtime=True
    )
    assert pipeline.set_standby(True) is True
    assert pipeline.set_standby(True) is False  # already on: no second event
    running(pipeline)
    transcript = await rec.wait_for(TranscriptReady)
    assert transcript.trigger == "standby" and transcript.text == "увімкни чайник"
    assert "WakeDetected" not in rec.types()
    assert output.played == []  # no "listening" beep in standby
    changed = [e for e in rec.events if isinstance(e, StandbyChanged)]
    assert len(changed) == 1 and changed[0].enabled and changed[0].reason == "user"


async def test_standby_waits_for_speech_while_idle(running) -> None:  # noqa: F811
    pipeline, rec, _ = make(silence(3), FakeEngines(wake=FakeWake()), realtime=True)
    pipeline.set_standby(True)
    running(pipeline)
    await asyncio.sleep(0.5)
    assert pipeline.state is PipelineState.IDLE  # answers are not blocked while waiting
    pipeline.bus.publish(SpeakRequested(session_id="s-1", text="Світло увімкнено."))
    await rec.wait_for(SpeakFinished)
    assert "ListeningStarted" not in rec.types()


async def test_standby_turns_off_after_the_timeout(running) -> None:  # noqa: F811
    now = [0.0]
    pipeline, rec, _ = make(
        silence(3), FakeEngines(wake=FakeWake()), realtime=True, standby_timeout_min=1
    )
    pipeline.clock = lambda: now[0]
    pipeline.set_standby(True)
    running(pipeline)
    await asyncio.sleep(0.2)
    assert pipeline.standby
    pipeline.bus.publish(
        IntentParsed(session_id="s-1", intent=Intent(kind="command", action="turn_on"))
    )
    now[0] = 59.0  # a command was given just now
    await asyncio.sleep(0.2)
    assert pipeline.standby
    now[0] = 200.0
    await asyncio.sleep(0.3)
    assert not pipeline.standby
    off = [e for e in rec.events if isinstance(e, StandbyChanged) and not e.enabled]
    assert len(off) == 1 and off[0].reason == "timeout"


async def test_standby_timeout_zero_never_turns_off(running) -> None:  # noqa: F811
    now = [0.0]
    pipeline, _, _ = make(
        silence(2), FakeEngines(wake=FakeWake()), realtime=True, standby_timeout_min=0
    )
    pipeline.clock = lambda: now[0]
    pipeline.set_standby(True)
    running(pipeline)
    now[0] = 1e6
    await asyncio.sleep(0.3)
    assert pipeline.standby


# ------------------------------------------------------------------- assistant


def _spoken(events) -> list[str]:
    return [e.text for e in events if isinstance(e, SpeakRequested)]


async def test_voice_phrase_switches_standby_and_is_confirmed(env) -> None:  # noqa: F811
    assistant, _, events = env
    calls = []

    def control(enabled, reason, session_id=None):
        calls.append((enabled, reason))
        assistant.bus.publish(StandbyChanged(session_id=session_id, enabled=enabled, reason=reason))
        return True

    assistant.standby_control = control
    answer = await assistant.ask("увімкни режим очікування")
    assert calls == [(True, "voice")]
    assert answer.startswith("Режим очікування увімкнено")
    assert not any(isinstance(e, IntentParsed | IntentFailed) for e in events)


async def test_standby_already_on(env) -> None:  # noqa: F811
    assistant, _, _ = env
    assistant.standby_control = lambda *a, **k: False
    assert await assistant.ask("режим очікування") == "Режим очікування вже увімкнено."
    assert await assistant.ask("stop listening") == "Standby mode is already off."


async def test_standby_without_voice(env) -> None:  # noqa: F811
    assistant, _, _ = env
    assert "лише з голосом" in await assistant.ask("режим очікування")


async def test_panel_switch_and_timeout_are_spoken(env) -> None:  # noqa: F811
    assistant, _, events = env
    assistant.bus.publish(StandbyChanged(enabled=False, reason="timeout"))
    assistant.bus.publish(StandbyChanged(enabled=True, reason="user"))
    assert _spoken(events) == [
        "Режим очікування вимкнено: давно не було команд.",
        "Режим очікування увімкнено. Кажіть команди без слова Телекінез.",
    ]


async def test_standby_stays_silent_on_talk_it_did_not_understand(env) -> None:  # noqa: F811
    assistant, _, events = env
    answer = await assistant.ask("ну і погода сьогодні", trigger="standby")
    assert answer == "" and _spoken(events) == []
    assert any(isinstance(e, IntentFailed) for e in events)  # still visible in the panel
    answer = await assistant.ask("увімкни світло на кухні", trigger="standby")
    assert "увімкнено" in answer.lower()


async def test_after_wake_word_not_understood_is_still_answered(env) -> None:  # noqa: F811
    assistant, _, events = env
    assert await assistant.ask("ну і погода сьогодні", trigger="wake") != ""
    assert _spoken(events)


# ------------------------------------------------------------------------- web


async def test_standby_api_without_voice(client) -> None:  # noqa: F811
    r = await client.post("/api/voice/standby", json={"enabled": True})
    assert r.status_code == 503
    assert (await client.get("/api/status")).json()["standby"] is False


class FakeVoice:
    standby = False

    def set_standby(self, enabled, reason="user", session_id=None) -> bool:
        changed, self.standby = enabled != self.standby, enabled
        self.reason = reason
        return changed


async def test_standby_api_switches_the_pipeline(client, core) -> None:  # noqa: F811
    core.voice = FakeVoice()
    r = await client.post("/api/voice/standby", json={"enabled": True})
    assert r.status_code == 200 and r.json() == {"standby": True}
    assert core.voice.reason == "user"
    assert (await client.get("/api/status")).json()["standby"] is True
    r = await client.post("/api/voice/standby", json={"enabled": False})
    assert r.json() == {"standby": False}
