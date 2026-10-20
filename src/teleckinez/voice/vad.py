"""Voice activity detection and utterance recording (docs/specs/voice.md §3).

UtteranceRecorder is a small state machine fed with 30 ms frames:
WAITING (no speech yet) -> SPEECH (recording) -> DONE. Speech starts after
`start_frames` voiced frames in a row and ends after `end_silence_ms` of
silence or at `max_utterance_s`. A short pre-roll keeps the first syllable.
"""

import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import numpy as np

from teleckinez.voice.audio_io import FRAME_MS, SAMPLE_RATE


class Vad(Protocol):
    def is_speech(self, frame: bytes) -> bool: ...


class WebrtcVad:
    def __init__(self, aggressiveness: int = 2) -> None:
        import webrtcvad

        self._vad = webrtcvad.Vad(aggressiveness)

    def is_speech(self, frame: bytes) -> bool:
        return self._vad.is_speech(frame, SAMPLE_RATE)


class EnergyVad:
    """Loudness threshold; a dependency-free stand-in for tests."""

    def __init__(self, threshold_dbfs: float = -40.0) -> None:
        self.threshold = 32768 * 10 ** (threshold_dbfs / 20)

    def is_speech(self, frame: bytes) -> bool:
        samples = np.frombuffer(frame, np.int16).astype(np.float32)
        return bool(samples.size) and float(np.sqrt(np.mean(samples**2))) > self.threshold


class RecorderState(StrEnum):
    WAITING = "waiting"
    SPEECH = "speech"
    DONE = "done"


@dataclass(frozen=True)
class Utterance:
    audio: np.ndarray  # int16, 16 kHz mono
    reason: str  # silence | max_length
    started_at: float  # wall clock of speech start
    ended_at: float  # wall clock when the end was detected (VAD decision)

    @property
    def duration_s(self) -> float:
        return self.audio.size / SAMPLE_RATE


class UtteranceRecorder:
    def __init__(
        self,
        vad: Vad,
        *,
        start_frames: int = 3,
        end_silence_ms: int = 700,
        max_utterance_s: float = 8.0,
        no_speech_timeout_s: float = 5.0,
        pre_roll_ms: int = 300,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.vad = vad
        self.start_frames = start_frames
        self.end_frames = max(1, end_silence_ms // FRAME_MS)
        self.max_frames = int(max_utterance_s * 1000 / FRAME_MS)
        self.no_speech_frames = int(no_speech_timeout_s * 1000 / FRAME_MS)
        self.clock = clock
        self._pre = deque(maxlen=max(start_frames, pre_roll_ms // FRAME_MS))
        self.reset()

    def reset(self) -> None:
        self.state = RecorderState.WAITING
        self.reason = ""
        self._pre.clear()
        self._frames: list[bytes] = []
        self._voiced_run = 0
        self._silent_run = 0
        self._waited = 0
        self.started_at = 0.0
        self.ended_at = 0.0

    def feed(self, frame: bytes) -> RecorderState:
        """Process one 30 ms frame and return the new state."""
        if self.state is RecorderState.DONE:
            return self.state
        speech = self.vad.is_speech(frame)
        if self.state is RecorderState.WAITING:
            self._pre.append(frame)
            self._voiced_run = self._voiced_run + 1 if speech else 0
            self._waited += 1
            if self._voiced_run >= self.start_frames:
                self.state = RecorderState.SPEECH
                self.started_at = self.clock()
                self._frames = list(self._pre)
            elif self._waited >= self.no_speech_frames:
                self._finish("no_speech")
            return self.state
        self._frames.append(frame)
        self._silent_run = 0 if speech else self._silent_run + 1
        if self._silent_run >= self.end_frames:
            self._finish("silence")
        elif len(self._frames) >= self.max_frames:
            self._finish("max_length")
        return self.state

    def _finish(self, reason: str) -> None:
        self.state = RecorderState.DONE
        self.reason = reason
        self.ended_at = self.clock()

    def utterance(self) -> Utterance | None:
        if self.state is not RecorderState.DONE or self.reason == "no_speech":
            return None
        frames = self._frames
        if self.reason == "silence":  # drop the trailing silence, keep ~150 ms of it
            frames = frames[: len(frames) - self.end_frames + 5]
        audio = np.frombuffer(b"".join(frames), np.int16).copy()
        return Utterance(audio, self.reason, self.started_at, self.ended_at)

    async def record(
        self,
        frames: AsyncIterator[bytes],
        on_speech_start: Callable[[], None] | None = None,
    ) -> Utterance | None:
        """Consume frames until the utterance ends; None if nobody spoke."""
        self.reset()
        async for frame in frames:
            before = self.state
            state = self.feed(frame)
            if (
                before is RecorderState.WAITING
                and state is RecorderState.SPEECH
                and on_speech_start
            ):
                on_speech_start()
            if state is RecorderState.DONE:
                break
        if self.state is RecorderState.SPEECH:  # the source ended mid-utterance
            self._finish("end_of_stream")
        return self.utterance()
