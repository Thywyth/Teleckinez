"""Triggers that start listening (voice.md §2): wake word detectors and push-to-talk.

A wake word detector is fed the same 30 ms frames as the recorder, one at a
time, and reports a WakeHit. Push-to-talk works in every mode: a global
hotkey (pynput) or the button in the web panel hold the PushToTalk switch.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from teleckinez.core.config import VoiceSettings
from teleckinez.devices.registry import DeviceRegistry
from teleckinez.voice.audio_io import FRAME_MS, SAMPLE_RATE
from teleckinez.voice.vad import Utterance, Vad

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WakeHit:
    engine: str  # vosk_kws | openwakeword
    score: float
    # Words said in the same breath after the wake word ("Телекінез, увімкни світло");
    # the pipeline uses them as the command instead of recording a new phrase.
    tail: str = ""


class WakeWordDetector(Protocol):
    name: str

    def process(self, frame: bytes) -> WakeHit | None:
        """Feed one 30 ms frame of 16 kHz int16 audio."""
        ...

    def reset(self) -> None:
        """Forget buffered audio (after a hit, or while the assistant speaks)."""
        ...


# ------------------------------------------------------------------ Vosk keyword spotting


class VoskKws:
    """Wake word through Vosk with a restricted grammar.

    The grammar holds the wake word *and* all command words: with only
    ["телекінез", unk] similar words ("телевізор") were taken for the wake
    word, because the decoder had nothing better to choose. A hit needs the
    word's confidence >= threshold.
    """

    name = "vosk_kws"

    def __init__(
        self, model, grammar: list[str], wake_word: str = "телекінез", threshold: float = 0.8
    ) -> None:
        import vosk

        if wake_word not in grammar:
            raise ValueError(f"wake word {wake_word!r} is not in the grammar (model vocabulary?)")
        self.wake_word = wake_word
        self.threshold = threshold
        self._rec = vosk.KaldiRecognizer(
            model, SAMPLE_RATE, json.dumps(grammar, ensure_ascii=False)
        )
        self._rec.SetWords(True)

    def process(self, frame: bytes) -> WakeHit | None:
        if not self._rec.AcceptWaveform(frame):
            return None  # the phrase is still going on (Vosk endpointing)
        return self._hit(json.loads(self._rec.Result()))

    def _hit(self, result: dict) -> WakeHit | None:
        from teleckinez.voice.stt.vosk_stt import recognized_words

        words = recognized_words(result)
        for i, word in enumerate(words):
            if word["word"] == self.wake_word:
                score = float(word.get("conf", 0.0))
                if score >= self.threshold:
                    tail = " ".join(w["word"] for w in words[i + 1 :])
                    return WakeHit(self.name, round(score, 3), tail)
                log.debug("wake word with low confidence %.2f ignored", score)
        return None

    def reset(self) -> None:
        self._rec.Reset()


def create_vosk_kws(voice: VoiceSettings, registry: DeviceRegistry) -> VoskKws:
    from teleckinez.voice.paths import model_path
    from teleckinez.voice.stt.vosk_stt import grammar_for, load_model

    path = model_path(voice.vosk_model_uk)  # the wake word is Ukrainian
    return VoskKws(
        load_model(path), grammar_for(registry, "uk", path), threshold=voice.wake_threshold
    )


# ------------------------------------------------------------------------ push-to-talk


class PushToTalk:
    """Held / released switch; set from the hotkey thread (via the loop) or the web panel."""

    def __init__(self) -> None:
        self.held = False
        self._changed = asyncio.Event()

    def set(self, pressed: bool) -> None:
        if pressed == self.held:
            return  # key auto-repeat sends many presses
        self.held = pressed
        self._changed.set()

    async def wait_change(self) -> bool:
        await self._changed.wait()
        self._changed.clear()
        return self.held

    async def wait_pressed(self) -> None:
        while not self.held:
            await self.wait_change()


async def record_while_held(
    frames: AsyncIterator[bytes],
    ptt: PushToTalk,
    vad: Vad,
    *,
    max_utterance_s: float = 8.0,
    min_speech_frames: int = 3,
    clock: Callable[[], float] | None = None,
) -> Utterance | None:
    """Collect frames from now until the button is released (or the length limit).

    None if the VAD found no speech in what was recorded (a short accidental press).
    """
    import time

    clock = clock or time.time
    started = clock()
    max_frames = int(max_utterance_s * 1000 / FRAME_MS)
    chunks: list[bytes] = []
    voiced = 0
    reason = "released"
    async for frame in frames:
        chunks.append(frame)
        voiced += vad.is_speech(frame)
        if not ptt.held:
            break
        if len(chunks) >= max_frames:
            reason = "max_length"
            break
    if voiced < min_speech_frames:
        return None
    audio = np.frombuffer(b"".join(chunks), np.int16).copy()
    return Utterance(audio, reason, started, clock())


# pynput key names: https://pynput.readthedocs.io/en/latest/keyboard.html#pynput.keyboard.Key
class HotkeyListener:
    """Global push-to-talk key (works while another window has focus)."""

    def __init__(self, key: str, ptt: PushToTalk) -> None:
        from pynput import keyboard

        self._keyboard = keyboard
        self.key = self._parse(key)
        self.ptt = ptt
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listener = None

    def _parse(self, name: str):
        name = name.strip().lower()
        if hasattr(self._keyboard.Key, name):
            return getattr(self._keyboard.Key, name)
        if len(name) == 1:
            return self._keyboard.KeyCode.from_char(name)
        raise ValueError(f"unknown push-to-talk key {name!r} (examples: f9, ctrl_r, scroll_lock)")

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._listener = self._keyboard.Listener(
            on_press=lambda k: self._post(k, True), on_release=lambda k: self._post(k, False)
        )
        self._listener.daemon = True
        self._listener.start()

    def _post(self, key, pressed: bool) -> None:  # pynput thread
        if key == self.key and self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self.ptt.set, pressed)

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
