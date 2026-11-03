"""Voice pipeline (voice.md §1): wake word / push-to-talk -> recording -> STT -> events; TTS.

Fake engines check the control flow fast and without models; the test
marked `models` runs real Vosk on Piper speech from a "WAV file" source.
"""

import asyncio

import numpy as np
import pytest

from teleckinez.core.config import VoiceSettings
from teleckinez.core.events import (
    Event,
    EventBus,
    ListeningStarted,
    ListeningStopped,
    SpeakFinished,
    SpeakRequested,
    SpeechEnded,
    TranscriptReady,
    WakeDetected,
)
from teleckinez.voice.audio_io import SAMPLE_RATE, NullOutput, WavSource, tone
from teleckinez.voice.pipeline import EngineCache, PipelineState, VoicePipeline
from teleckinez.voice.stt.base import Transcript
from teleckinez.voice.triggers import WakeHit
from teleckinez.voice.tts.base import Speech, TextOnly
from teleckinez.voice.vad import EnergyVad


def silence(s: float) -> np.ndarray:
    return np.zeros(int(s * SAMPLE_RATE), np.int16)


class FakeWake:
    """Fires when a loud sound (the "wake word") ends."""

    name = "fake"

    def __init__(self, tail: str = "") -> None:
        self.tail, self.loud, self.resets = tail, 0, 0

    def process(self, frame: bytes) -> WakeHit | None:
        if np.abs(np.frombuffer(frame, np.int16)).max() > 1000:
            self.loud += 1
            return None
        if self.loud >= 5:
            self.loud = 0
            return WakeHit("fake", 0.9, self.tail)
        self.loud = 0
        return None

    def reset(self) -> None:
        self.loud = 0
        self.resets += 1


class FakeStt:
    name = "vosk-grammar"

    def __init__(self, text: str = "увімкни світло на кухні") -> None:
        self.text, self.calls = text, []

    def transcribe(self, audio: np.ndarray, lang: str = "uk") -> Transcript:
        self.calls.append(audio.size / SAMPLE_RATE)
        return Transcript(self.text, lang, 0.95 if self.text else 0.0, self.name, 1.0, 0.01)


class FakeTts:
    name = "fake"

    def synthesize(self, text: str, lang: str = "uk") -> Speech | None:
        return Speech(tone(500, 0.2), SAMPLE_RATE)


class FakeEngines:
    def __init__(self, wake=None, stt=None, tts=None) -> None:
        self._wake, self._stt, self._tts = wake, stt or FakeStt(), tts or FakeTts()

    def wake(self, v):
        return self._wake if v.trigger != "push_to_talk" else None

    def stt(self, v):
        return self._stt

    def tts(self, v):
        return self._tts


class Recorder:
    def __init__(self, bus: EventBus) -> None:
        self.events: list[Event] = []
        self._new = asyncio.Event()
        bus.subscribe(self._on, Event)

    def _on(self, event: Event) -> None:
        self.events.append(event)
        self._new.set()

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    async def wait_for(self, kind: type, limit_s: float = 5.0) -> Event:
        async with asyncio.timeout(limit_s):
            while True:
                for e in self.events:
                    if isinstance(e, kind):
                        return e
                self._new.clear()
                await self._new.wait()


def make(audio, engines, *, realtime=False, **voice) -> tuple[VoicePipeline, Recorder, NullOutput]:
    bus = EventBus()
    output = NullOutput()
    settings = VoiceSettings(no_speech_timeout_s=1.0, vad_end_silence_ms=300, **voice)
    source = WavSource(audio, realtime=realtime, trailing_silence_s=2.0)
    pipeline = VoicePipeline(bus, settings, source, output, engines, vad=EnergyVad())
    return pipeline, Recorder(bus), output


@pytest.fixture
async def running():
    started: list[VoicePipeline] = []

    def start(pipeline: VoicePipeline) -> VoicePipeline:
        pipeline.start()
        started.append(pipeline)
        return pipeline

    yield start
    for p in started:
        await p.stop()


WAKE_THEN_COMMAND = np.concatenate(
    [silence(0.3), tone(1000, 0.4, volume=0.5), silence(0.3), tone(300, 0.8, volume=0.5)]
)


async def test_wake_word_then_command(running) -> None:
    pipeline, rec, output = make(WAKE_THEN_COMMAND, FakeEngines(wake=FakeWake()))
    running(pipeline)
    transcript = await rec.wait_for(TranscriptReady)
    assert transcript.text == "увімкни світло на кухні"
    assert transcript.trigger == "wake" and transcript.engine == "vosk"
    assert rec.types()[:4] == ["WakeDetected", "ListeningStarted", "SpeechEnded", "TranscriptReady"]
    assert len({e.session_id for e in rec.events[:4]}) == 1  # one session for the whole chain
    assert len(output.played) == 1  # the "listening" beep
    speech = next(e for e in rec.events if isinstance(e, SpeechEnded))
    assert 0.6 <= speech.duration_s <= 1.4


async def test_command_in_the_same_breath_skips_recording(running) -> None:
    stt = FakeStt()
    pipeline, rec, output = make(
        WAKE_THEN_COMMAND, FakeEngines(wake=FakeWake(tail="увімкни чайник"), stt=stt)
    )
    running(pipeline)
    transcript = await rec.wait_for(TranscriptReady)
    assert transcript.text == "увімкни чайник" and transcript.trigger == "wake"
    assert "ListeningStarted" not in rec.types() and stt.calls == [] and output.played == []


async def test_nothing_said_after_the_wake_word(running) -> None:
    audio = np.concatenate([silence(0.3), tone(1000, 0.4, volume=0.5), silence(2.0)])
    pipeline, rec, output = make(audio, FakeEngines(wake=FakeWake()))
    running(pipeline)
    stopped = await rec.wait_for(ListeningStopped)
    assert stopped.reason == "no_speech" and "TranscriptReady" not in rec.types()
    assert len(output.played) == 2  # "listening" and "did not hear" beeps
    assert pipeline.state is PipelineState.IDLE


async def test_empty_transcript_is_not_sent_to_the_nlu(running) -> None:
    pipeline, rec, _ = make(WAKE_THEN_COMMAND, FakeEngines(wake=FakeWake(), stt=FakeStt("")))
    running(pipeline)
    assert (await rec.wait_for(ListeningStopped)).reason == "not_recognized"
    assert "TranscriptReady" not in rec.types()


async def test_push_to_talk(running) -> None:
    audio = np.concatenate([silence(0.2), tone(300, 1.5, volume=0.5)])
    stt = FakeStt()
    pipeline, rec, output = make(
        audio, FakeEngines(wake=None, stt=stt), realtime=True, trigger="push_to_talk"
    )
    running(pipeline)
    await pipeline.push_to_talk(True)
    await asyncio.sleep(0.8)
    await pipeline.push_to_talk(False)
    transcript = await rec.wait_for(TranscriptReady)
    assert transcript.trigger == "ptt"
    assert rec.types()[0] == "ListeningStarted" and "WakeDetected" not in rec.types()
    assert 0.6 <= stt.calls[0] <= 1.1 and output.played == []  # no beep while the key is held


async def test_answers_are_spoken_with_the_mic_muted(running) -> None:
    pipeline, rec, output = make(silence(5), FakeEngines(wake=FakeWake()), realtime=True)
    muted_while_playing: list[bool] = []
    original = output.play

    async def play(samples, rate):
        muted_while_playing.append(pipeline.source.muted)
        await original(samples, rate)

    output.play = play
    running(pipeline)
    pipeline.bus.publish(SpeakRequested(session_id="s-1", text="Світло увімкнено."))
    finished = await rec.wait_for(SpeakFinished)
    assert finished.session_id == "s-1" and muted_while_playing == [True]
    assert not pipeline.source.muted


async def test_text_only_answers_still_finish(running) -> None:
    pipeline, rec, output = make(silence(3), FakeEngines(tts=TextOnly()))
    running(pipeline)
    pipeline.bus.publish(SpeakRequested(session_id="s-2", text="Світло увімкнено."))
    assert (await rec.wait_for(SpeakFinished)).session_id == "s-2" and output.played == []


async def test_wake_detector_is_reset_after_the_assistant_spoke(running) -> None:
    wake = FakeWake()
    pipeline, rec, _ = make(silence(5), FakeEngines(wake=wake), realtime=True)
    running(pipeline)
    await asyncio.sleep(0.1)
    before = wake.resets
    pipeline.bus.publish(SpeakRequested(text="Гаражні ворота не на зв'язку.", kind="notification"))
    await rec.wait_for(SpeakFinished)
    await asyncio.sleep(0.1)
    assert wake.resets > before


def test_engine_cache_rebuilds_only_what_changed(registry, monkeypatch) -> None:
    import teleckinez.voice.tts.base as tts_base

    built: list[str] = []
    monkeypatch.setattr(tts_base, "create_tts", lambda v: built.append(v.tts) or TextOnly())
    cache = EngineCache(registry)
    v = VoiceSettings(tts="text")
    assert cache.tts(v) is cache.tts(v) and built == ["text"]
    v.tts = "sapi"
    cache.tts(v)
    assert built == ["text", "sapi"]
    assert cache.wake(VoiceSettings(trigger="push_to_talk")) is None


# ------------------------------------------------------------------ real engines


@pytest.mark.models
async def test_real_wake_word_and_vosk_on_synthesized_speech(registry, speech, running) -> None:
    v = VoiceSettings()
    from teleckinez.voice.paths import model_path

    if not model_path(v.vosk_model_uk).is_dir():
        pytest.skip("Vosk uk model missing")
    audio = np.concatenate(
        [silence(0.5), speech("телекінез"), silence(1.0), speech("відкрий штори у вітальні")]
    )
    bus = EventBus()
    engines = EngineCache(registry)
    pipeline = VoicePipeline(
        bus, v, WavSource(audio, trailing_silence_s=2.0), NullOutput(), engines
    )
    rec = Recorder(bus)
    await pipeline.prepare()
    running(pipeline)
    wake = await rec.wait_for(WakeDetected, limit_s=20)
    transcript = await rec.wait_for(TranscriptReady, limit_s=20)
    assert wake.engine == "vosk_kws" and transcript.trigger == "wake"
    assert transcript.text == "відкрий штори у вітальні"
    assert isinstance(rec.events[1], ListeningStarted)
