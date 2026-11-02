"""Speech synthesizer interface (voice.md §5): text -> audio, played by the pipeline.

Engines only synthesize; playback goes through HalfDuplex in the voice
pipeline, so the microphone is muted while any engine speaks.
"""

import logging
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from teleckinez.core.config import VoiceSettings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Speech:
    samples: np.ndarray  # int16 mono
    rate: int

    @property
    def duration_s(self) -> float:
        return self.samples.size / self.rate


class SpeechSynthesizer(Protocol):
    name: str

    def synthesize(self, text: str, lang: str = "uk") -> Speech | None:
        """Audio for `text`, or None if this engine cannot speak `lang`. Blocking."""
        ...


class TextOnly:
    """Answers are only shown as text (web panel / console)."""

    name = "text"

    def synthesize(self, text: str, lang: str = "uk") -> Speech | None:
        return None


FALLBACKS = {"piper": ["piper", "sapi", "text"], "sapi": ["sapi", "text"], "text": ["text"]}


def create_tts(voice: VoiceSettings) -> SpeechSynthesizer:
    """The engine from settings; if it cannot start: piper -> sapi -> text."""
    for name in FALLBACKS[voice.tts]:
        try:
            if name == "piper":
                from teleckinez.voice.tts.piper_tts import create_piper

                return create_piper(voice)
            if name == "sapi":
                from teleckinez.voice.tts.sapi_tts import SapiTts

                return SapiTts()
        except Exception as exc:  # missing package / model / no SAPI
            log.warning("TTS %s unavailable (%s), trying the next one", name, exc)
    return TextOnly()
