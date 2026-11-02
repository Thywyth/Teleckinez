"""Piper: local neural TTS (ONNX), Ukrainian and English voices (voice.md §5)."""

import logging
from collections.abc import Mapping
from pathlib import Path

import numpy as np

from teleckinez.core.config import VoiceSettings
from teleckinez.voice.paths import ascii_safe_path, model_path
from teleckinez.voice.tts.base import Speech
from teleckinez.voice.tts.speakable import speakable

log = logging.getLogger(__name__)


class PiperTts:
    name = "piper"

    def __init__(self, voices: Mapping[str, str | Path]) -> None:
        from piper import PiperVoice
        from piper.voice import ESPEAK_DATA_DIR

        # espeak-ng cannot open its data folder from a non-ASCII path and exits the process.
        espeak = ascii_safe_path(ESPEAK_DATA_DIR)
        self.voices = {}
        for lang, path in voices.items():
            if not Path(path).is_file():
                raise FileNotFoundError(
                    f"Piper voice not found: {path} (python scripts/download_models.py)"
                )
            self.voices[lang] = PiperVoice.load(path, espeak_data_dir=espeak)

    def synthesize(self, text: str, lang: str = "uk") -> Speech | None:
        voice = self.voices.get(lang)
        if voice is None:
            return None
        text = speakable(text, lang)
        if lang == "uk":  # character-based voice: its alphabet has lowercase letters only
            text = text.lower()
        chunks = [c.audio_int16_array for c in voice.synthesize(text)]
        if not chunks:
            return None
        return Speech(np.concatenate(chunks), voice.config.sample_rate)


def create_piper(voice: VoiceSettings) -> PiperTts:
    return PiperTts(
        {"uk": model_path(voice.piper_voice_uk), "en": model_path(voice.piper_voice_en)}
    )
