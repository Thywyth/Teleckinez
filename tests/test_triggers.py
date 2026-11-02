"""Triggers (voice.md §2): push-to-talk recording and the Vosk wake word on synthesized speech."""

import asyncio

import numpy as np
import pytest

from teleckinez.core.config import VoiceSettings
from teleckinez.voice.audio_io import SAMPLE_RATE, WavSource, frames_of, tone
from teleckinez.voice.paths import model_path
from teleckinez.voice.triggers import PushToTalk, WakeHit, record_while_held
from teleckinez.voice.vad import EnergyVad


async def test_ptt_ignores_auto_repeat() -> None:
    ptt = PushToTalk()
    ptt.set(True)
    ptt.set(True)
    assert await ptt.wait_change() is True
    ptt.set(False)
    assert await ptt.wait_change() is False


async def test_record_while_held_stops_on_release() -> None:
    ptt = PushToTalk()
    ptt.set(True)
    source = WavSource(tone(300, 3.0, volume=0.5), realtime=True)
    loop = asyncio.get_running_loop()
    loop.call_later(0.6, ptt.set, False)
    utt = await record_while_held(source.frames(), ptt, EnergyVad())
    assert utt is not None and utt.reason == "released"
    assert 0.4 <= utt.duration_s <= 1.0


async def test_record_while_held_short_press_without_speech() -> None:
    ptt = PushToTalk()
    ptt.set(True)
    source = WavSource(np.zeros(SAMPLE_RATE, np.int16), realtime=True)
    asyncio.get_running_loop().call_later(0.2, ptt.set, False)
    assert await record_while_held(source.frames(), ptt, EnergyVad()) is None


async def test_record_while_held_max_length() -> None:
    ptt = PushToTalk()
    ptt.set(True)
    source = WavSource(tone(300, 2.0, volume=0.5))
    utt = await record_while_held(source.frames(), ptt, EnergyVad(), max_utterance_s=0.6)
    assert utt.reason == "max_length" and utt.duration_s == pytest.approx(0.6, abs=0.03)


def test_hotkey_names() -> None:
    pytest.importorskip("pynput")
    from teleckinez.voice.triggers import HotkeyListener

    keys = [HotkeyListener(name, PushToTalk()).key for name in ("ctrl_r", "F9", "q")]
    assert [str(k) for k in keys] == ["Key.ctrl_r", "Key.f9", "'q'"]
    with pytest.raises(ValueError):
        HotkeyListener("hyper_button", PushToTalk())


# --------------------------------------------------------------- Vosk keyword spotting


@pytest.fixture(scope="module")
def kws_factory(registry):
    if not model_path(VoiceSettings().vosk_model_uk).is_dir():
        pytest.skip("Vosk uk lgraph model missing (python scripts/download_models.py)")
    from teleckinez.voice.triggers import create_vosk_kws

    return lambda: create_vosk_kws(VoiceSettings(), registry)


def listen(detector, audio: np.ndarray) -> list[WakeHit]:
    silence = np.zeros(SAMPLE_RATE, np.int16)
    hits = []
    for frame in frames_of(np.concatenate([silence, audio, silence])):
        if hit := detector.process(frame):
            hits.append(hit)
    return hits


@pytest.mark.models
def test_wake_word_detected(kws_factory, speech) -> None:
    hits = listen(kws_factory(), speech("телекінез"))
    assert len(hits) == 1 and hits[0].engine == "vosk_kws" and hits[0].tail == ""
    assert hits[0].score >= 0.8


@pytest.mark.models
def test_wake_word_with_command_in_one_breath(kws_factory, speech) -> None:
    hits = listen(kws_factory(), speech("телекінез увімкни світло на кухні"))
    assert [h.tail for h in hits] == ["увімкни світло на кухні"]


@pytest.mark.models
@pytest.mark.parametrize(
    "phrase", ["телевізор", "увімкни телевізор", "яка температура на кухні", "текст"]
)
def test_similar_words_are_not_the_wake_word(kws_factory, speech, phrase) -> None:
    assert listen(kws_factory(), speech(phrase)) == []


@pytest.mark.models
def test_wake_word_ignores_noise(kws_factory) -> None:
    noise = np.random.default_rng(0).normal(0, 800, 3 * SAMPLE_RATE).astype(np.int16)
    assert listen(kws_factory(), noise) == []
