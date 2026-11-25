"""Voice pipeline (voice.md §1): trigger -> recording -> STT -> TranscriptReady; TTS answers.

One task reads microphone frames and walks through the states

    IDLE -> LISTENING -> RECOGNIZING -> IDLE

In standby mode (ADR-014) the wake word is not needed: while IDLE the frames
go to a VAD recorder instead of the wake word detector, and the turn starts
when speech starts. Waiting for speech is not LISTENING on purpose, so
answers and notifications are still spoken between commands.

and publishes the same events the web panel and the database already know
(WakeDetected, ListeningStarted, SpeechEnded, TranscriptReady). The text
then goes through exactly the same path as typed text. A second task speaks
every SpeakRequested (answers, progress messages, notifications) through
HalfDuplex, so the microphone is muted while the assistant talks.

Engines are taken from settings.voice on every turn through EngineCache, so
changing the trigger / STT / TTS in the web panel works without a restart
(a newly selected engine is loaded in a worker thread on first use).
"""

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from enum import StrEnum
from typing import Any

from teleckinez.assistant import new_session_id
from teleckinez.core.config import VoiceSettings
from teleckinez.core.events import (
    EventBus,
    IntentParsed,
    ListeningStarted,
    ListeningStopped,
    SpeakFinished,
    SpeakRequested,
    SpeechEnded,
    StandbyChanged,
    TranscriptReady,
    WakeDetected,
)
from teleckinez.devices.registry import DeviceRegistry
from teleckinez.nlu.standby import strip_wake_word
from teleckinez.voice.audio_io import AudioOutput, AudioSource, HalfDuplex, tone
from teleckinez.voice.stt.base import SpeechRecognizer, transcribe_async
from teleckinez.voice.triggers import PushToTalk, WakeHit, WakeWordDetector, record_while_held
from teleckinez.voice.tts.base import SpeechSynthesizer
from teleckinez.voice.vad import RecorderState, UtteranceRecorder, Vad, WebrtcVad

log = logging.getLogger(__name__)

LISTEN_TONE = (880.0, 0.12)  # "слухаю" after the wake word
ERROR_TONE = (330.0, 0.25)  # nothing heard / nothing recognized
NEVER_S = 1e9  # standby waits for speech without a no-speech timeout


class PipelineState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    RECOGNIZING = "recognizing"
    SPEAKING = "speaking"


class EngineCache:
    """Builds engines for the current settings.voice and keeps them until settings change."""

    def __init__(self, registry: DeviceRegistry) -> None:
        self.registry = registry
        self._cache: dict[tuple, Any] = {}
        self._lock = threading.Lock()  # engines are built in worker threads

    def _get(self, key: tuple, build: Callable[[], Any]) -> Any:
        with self._lock:
            if key not in self._cache:
                engine = build()  # an error leaves the previous engine of this kind in place
                self._cache = {k: e for k, e in self._cache.items() if k[0] != key[0]}
                self._cache[key] = engine
            return self._cache[key]

    def stt(self, v: VoiceSettings) -> SpeechRecognizer:
        if v.stt == "whisper":
            from teleckinez.voice.stt.whisper_stt import create_whisper

            key = ("stt", "whisper", v.whisper_model, v.whisper_device, v.whisper_beam_size)
            return self._get(key, lambda: create_whisper(v, self.registry))
        from teleckinez.voice.stt.vosk_stt import create_vosk

        key = ("stt", "vosk", v.stt_lang, v.stt_grammar, v.vosk_model_uk, v.vosk_model_en)
        return self._get(key, lambda: create_vosk(v, self.registry))

    def wake(self, v: VoiceSettings) -> WakeWordDetector | None:
        if v.trigger == "vosk_kws":
            from teleckinez.voice.triggers import create_vosk_kws

            key = ("wake", "vosk_kws", v.vosk_model_uk, v.wake_threshold)
            return self._get(key, lambda: create_vosk_kws(v, self.registry))
        if v.trigger == "openwakeword":
            from teleckinez.voice.triggers import create_openwakeword

            key = ("wake", "openwakeword", v.oww_model, v.oww_threshold)
            return self._get(key, lambda: create_openwakeword(v))
        return None  # push_to_talk only

    def tts(self, v: VoiceSettings) -> SpeechSynthesizer:
        from teleckinez.voice.tts.base import create_tts

        key = ("tts", v.tts, v.piper_voice_uk, v.piper_voice_en)
        return self._get(key, lambda: create_tts(v))


class VoicePipeline:
    def __init__(
        self,
        bus: EventBus,
        voice: VoiceSettings,
        source: AudioSource,
        output: AudioOutput,
        engines: EngineCache | Any,
        *,
        vad: Vad | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bus = bus
        self.voice = voice  # the live settings object: the web panel changes it in place
        self.source = source
        self.engines = engines
        self.speaker = HalfDuplex(source, output)
        self.vad = vad or WebrtcVad(voice.vad_aggressiveness)
        self.ptt = PushToTalk()
        self.state = PipelineState.IDLE
        self._speech_queue: asyncio.Queue[SpeakRequested] = asyncio.Queue()
        self._not_listening = asyncio.Event()
        self._not_listening.set()
        self._wake_dirty = False  # the wake detector saw half a phrase before a mute
        self._tasks: list[asyncio.Task] = []
        self._hotkey = None
        self.clock = clock
        self.standby = False  # every phrase is a command, no wake word (ADR-014)
        self._standby_rec: UtteranceRecorder | None = None
        self._last_command = clock()
        bus.subscribe(self._on_speak, SpeakRequested)
        bus.subscribe(self._on_intent, IntentParsed)

    # ------------------------------------------------------------------ public

    async def prepare(self) -> None:
        """Load the configured engines now (Vosk lgraph takes ~10 s), not on the first word."""
        await asyncio.to_thread(self.engines.stt, self.voice)
        await asyncio.to_thread(self.engines.wake, self.voice)
        await asyncio.to_thread(self.engines.tts, self.voice)

    def enable_hotkey(self, key: str) -> None:
        """Global push-to-talk key (voice.ptt_key); call from the running event loop."""
        from teleckinez.voice.triggers import HotkeyListener

        self._hotkey = HotkeyListener(key, self.ptt)
        self._hotkey.start()

    def start(self) -> None:
        """Open the microphone (if the source is one) and start listening and speaking."""
        if (open_source := getattr(self.source, "start", None)) is not None:
            open_source()
        self._tasks = [
            asyncio.create_task(self._listen_loop(), name="voice-listen"),
            asyncio.create_task(self._speak_loop(), name="voice-speak"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        if self._hotkey is not None:
            self._hotkey.stop()
            self._hotkey = None
        if (close_source := getattr(self.source, "stop", None)) is not None:
            close_source()

    async def push_to_talk(self, pressed: bool) -> None:
        """Web panel button / hotkey: hold to talk."""
        self.ptt.set(pressed)

    def set_standby(
        self, enabled: bool, reason: str = "user", session_id: str | None = None
    ) -> bool:
        """Switch standby mode; False if it already was in that state (no event then)."""
        if enabled == self.standby:
            return False
        self.standby = enabled
        self._standby_rec = None
        self._wake_dirty = True  # neither detector may continue on old audio
        self._last_command = self.clock()
        log.info("standby mode %s (%s)", "on" if enabled else "off", reason)
        self.bus.publish(StandbyChanged(session_id=session_id, enabled=enabled, reason=reason))
        return True

    async def wait(self) -> None:
        """Until a pipeline task ends (an unexpected error); re-raises it."""
        done, _ = await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()

    # -------------------------------------------------------------- listening

    def _set_state(self, state: PipelineState) -> None:
        self.state = state
        if state in (PipelineState.LISTENING, PipelineState.RECOGNIZING):
            self._not_listening.clear()
        else:
            self._not_listening.set()

    def _wake_settings(self) -> tuple:
        v = self.voice
        return (v.trigger, v.vosk_model_uk, v.wake_threshold, v.oww_model, v.oww_threshold)

    async def _listen_loop(self) -> None:
        frames = aiter(self.source.frames())
        wake_key = self._wake_settings()
        wake = await asyncio.to_thread(self.engines.wake, self.voice)
        while True:
            try:
                frame = await anext(frames)
            except StopAsyncIteration:  # an audio file (--audio-file) has ended
                log.info("audio source ended")
                return
            if self.ptt.held:
                await self._turn(frames, trigger="ptt", first=frame)
                self._wake_dirty = True
                continue
            if self._wake_settings() != wake_key:  # changed in the web panel
                wake_key = self._wake_settings()
                try:
                    wake = await asyncio.to_thread(self.engines.wake, self.voice)
                except Exception as exc:
                    log.error("wake word engine unavailable (%s); push-to-talk only", exc)
                    wake = None
                self._wake_dirty = True
            if self.standby:
                await self._standby_frame(frames, frame)
                continue
            if wake is None:
                continue
            if self._wake_dirty:  # audio before a mute (our own speech) is not a phrase
                wake.reset()
                self._wake_dirty = False
            if (hit := wake.process(frame)) is not None:
                await self._turn(frames, trigger="wake", hit=hit)
                self._wake_dirty = True

    async def _standby_frame(self, frames: AsyncIterator[bytes], frame: bytes) -> None:
        """Standby mode: wait for speech with the VAD (still IDLE), then run a turn."""
        timeout_min = self.voice.standby_timeout_min
        if timeout_min and self.clock() - self._last_command > timeout_min * 60:
            self.set_standby(False, "timeout")
            return
        if self._standby_rec is None or self._wake_dirty:  # audio before a mute is not a phrase
            v = self.voice
            self._standby_rec = UtteranceRecorder(
                self.vad,
                start_frames=v.vad_start_frames,
                end_silence_ms=v.vad_end_silence_ms,
                max_utterance_s=v.max_utterance_s,
                no_speech_timeout_s=NEVER_S,
                pre_roll_ms=v.vad_pre_roll_ms,
            )
            self._wake_dirty = False
        if self._standby_rec.feed(frame) is RecorderState.SPEECH:
            recorder, self._standby_rec = self._standby_rec, None
            await self._turn(frames, trigger="standby", recorder=recorder)
            self._wake_dirty = True

    def _on_intent(self, _event: IntentParsed) -> None:
        self._last_command = self.clock()  # a real command keeps standby mode alive

    async def _turn(
        self,
        frames: AsyncIterator[bytes],
        *,
        trigger: str,
        hit: WakeHit | None = None,
        first: bytes | None = None,
        recorder: UtteranceRecorder | None = None,
    ) -> None:
        session_id = new_session_id()
        try:
            await self._listen(frames, session_id, trigger, hit, first, recorder)
        except Exception:
            log.exception("voice turn %s failed", session_id)
            self.bus.publish(ListeningStopped(session_id=session_id, reason="error"))
        finally:
            self._set_state(PipelineState.IDLE)

    async def _listen(
        self,
        frames: AsyncIterator[bytes],
        session_id: str,
        trigger: str,
        hit: WakeHit | None,
        first: bytes | None,
        recorder: UtteranceRecorder | None = None,
    ) -> None:
        v = self.voice
        if hit is not None:
            self.bus.publish(
                WakeDetected(session_id=session_id, engine=hit.engine, score=hit.score)
            )
            if hit.tail:  # "Телекінез, увімкни світло": the command is already recognized
                self.bus.publish(
                    TranscriptReady(
                        session_id=session_id,
                        text=hit.tail,
                        lang="uk",
                        confidence=hit.score,
                        engine="vosk",
                        trigger="wake",
                    )
                )
                return
        self._set_state(PipelineState.LISTENING)
        stt = await asyncio.to_thread(self.engines.stt, v)
        warm = asyncio.create_task(_keep_warm(stt)) if hasattr(stt, "warm_up") else None
        try:
            await self._record_and_recognize(
                frames, session_id, trigger, first, stt, warm, recorder
            )
        finally:
            if warm:
                warm.cancel()

    async def _record_and_recognize(
        self,
        frames: AsyncIterator[bytes],
        session_id: str,
        trigger: str,
        first: bytes | None,
        stt: SpeechRecognizer,
        warm: asyncio.Task | None,
        recorder: UtteranceRecorder | None = None,
    ) -> None:
        v = self.voice
        quiet = trigger == "standby"  # no "did not hear" tone for every cough in standby
        if trigger == "wake" and v.beeps:
            await self._beep(*LISTEN_TONE)  # muted: the beep is not recorded
        self.bus.publish(ListeningStarted(session_id=session_id, trigger=trigger))

        if recorder is not None:  # standby: speech has already started
            utterance = await recorder.record(_relay(frames), resume=True)
        elif trigger == "ptt":
            utterance = await record_while_held(
                _prepend(first, frames), self.ptt, self.vad, max_utterance_s=v.max_utterance_s
            )
        else:
            recorder = UtteranceRecorder(
                self.vad,
                start_frames=v.vad_start_frames,
                end_silence_ms=v.vad_end_silence_ms,
                max_utterance_s=v.max_utterance_s,
                no_speech_timeout_s=v.no_speech_timeout_s,
                pre_roll_ms=v.vad_pre_roll_ms,
            )
            utterance = await recorder.record(_relay(frames))
        if utterance is None:
            await self._give_up(session_id, "no_speech", beep=not quiet)
            return
        self.bus.publish(SpeechEnded(session_id=session_id, duration_s=utterance.duration_s))

        self._set_state(PipelineState.RECOGNIZING)
        if warm:
            warm.cancel()
        transcript = await transcribe_async(stt, utterance.audio, v.stt_lang)
        log.info(
            "heard %r (%s, conf %.2f, %.2f s for %.1f s of audio)",
            transcript.text,
            transcript.engine,
            transcript.confidence,
            transcript.elapsed_s,
            transcript.audio_s,
        )
        text = strip_wake_word(transcript.text) if trigger == "standby" else transcript.text
        if not text:
            await self._give_up(session_id, "not_recognized", beep=not quiet)
            return
        self.bus.publish(
            TranscriptReady(
                session_id=session_id,
                text=text,
                lang=transcript.lang,
                confidence=transcript.confidence,
                engine="vosk" if transcript.engine.startswith("vosk") else transcript.engine,
                trigger=trigger,
            )
        )

    async def _give_up(self, session_id: str, reason: str, *, beep: bool = True) -> None:
        self._set_state(PipelineState.IDLE)
        self.bus.publish(ListeningStopped(session_id=session_id, reason=reason))
        if beep and self.voice.beeps:
            await self._beep(*ERROR_TONE)

    async def _beep(self, freq: float, duration_s: float) -> None:
        await self.speaker.play(tone(freq, duration_s), 16_000)
        self._wake_dirty = True

    # --------------------------------------------------------------- speaking

    def _on_speak(self, event: SpeakRequested) -> None:
        self._speech_queue.put_nowait(event)

    async def _speak_loop(self) -> None:
        while True:
            event = await self._speech_queue.get()
            # a notification must not cut into a phrase being recorded
            await self._not_listening.wait()
            try:
                await self._say(event)
            except Exception:
                log.exception("could not speak %r", event.text)
            finally:
                if self.state is PipelineState.SPEAKING:
                    self._set_state(PipelineState.IDLE)
                self.bus.publish(SpeakFinished(session_id=event.session_id, text=event.text))

    async def _say(self, event: SpeakRequested) -> None:
        tts = await asyncio.to_thread(self.engines.tts, self.voice)
        speech = await asyncio.to_thread(tts.synthesize, event.text, event.lang)
        if speech is None:
            return  # text-only answer (or no voice for this language)
        self._set_state(PipelineState.SPEAKING)
        await self.speaker.play(speech.samples, speech.rate)
        self._wake_dirty = True


async def _keep_warm(stt: Any) -> None:
    """Call stt.warm_up() in a loop until cancelled (see WhisperRecognizer.warm_up)."""
    from teleckinez.voice.stt.whisper_stt import WARM_UP_EVERY_S

    while True:
        await asyncio.to_thread(stt.warm_up)
        await asyncio.sleep(WARM_UP_EVERY_S)


async def _relay(frames: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """A wrapper the recorder may abandon without closing the shared frame iterator."""
    while True:
        try:
            frame = await anext(frames)
        except StopAsyncIteration:
            return
        yield frame


async def _prepend(first: bytes | None, frames: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    if first is not None:
        yield first
    async for frame in _relay(frames):
        yield frame
