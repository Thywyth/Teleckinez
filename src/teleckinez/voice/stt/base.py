"""Speech recognizer interface (voice.md §4): audio -> Transcript."""

import asyncio
from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class Transcript:
    text: str
    lang: str
    confidence: float  # 0..1
    engine: str  # vosk | vosk-grammar | whisper
    audio_s: float = 0.0  # length of the recognized audio
    elapsed_s: float = 0.0  # processing time; RTF = elapsed_s / audio_s

    @property
    def rtf(self) -> float:
        return self.elapsed_s / self.audio_s if self.audio_s else 0.0


class SpeechRecognizer(Protocol):
    name: str

    def transcribe(self, audio: np.ndarray, lang: str = "uk") -> Transcript:
        """Recognize 16 kHz mono int16 audio; lang is uk | en | auto. CPU/GPU-heavy, blocking."""
        ...


async def transcribe_async(
    recognizer: SpeechRecognizer, audio: np.ndarray, lang: str = "uk"
) -> Transcript:
    """Run the blocking recognizer in a worker thread so the event loop stays responsive."""
    return await asyncio.to_thread(recognizer.transcribe, audio, lang)
