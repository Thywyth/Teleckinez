"""Vosk (Kaldi) speech recognizer: free vocabulary or restricted grammar (voice.md §4, ADR-004)."""

import functools
import json
import logging
import threading
import time
from collections.abc import Mapping
from pathlib import Path

import numpy as np

from teleckinez.core.config import VoiceSettings
from teleckinez.devices.registry import DeviceRegistry
from teleckinez.nlu.grammar import UNK_SYMBOLS, build_grammar, load_vocabulary
from teleckinez.voice.audio_io import SAMPLE_RATE
from teleckinez.voice.paths import ascii_safe_path, model_path
from teleckinez.voice.stt.base import Transcript

log = logging.getLogger(__name__)


@functools.cache
def load_model(path: Path):
    """Load a Vosk model once per process (the uk lgraph model takes ~10 s and ~1 GB);
    the recognizer and the wake word detector share it."""
    import vosk

    vosk.SetLogLevel(-1)
    if not path.is_dir():
        raise FileNotFoundError(
            f"Vosk model not found: {path} (python scripts/download_models.py --only vosk)"
        )
    started = time.perf_counter()
    model = vosk.Model(ascii_safe_path(path))
    log.info("Vosk model %s loaded in %.1f s", path.name, time.perf_counter() - started)
    return model


@functools.cache
def vocabulary_of(path: Path) -> frozenset[str] | None:
    words = load_vocabulary(path)
    return frozenset(words) if words is not None else None


def recognized_words(result: dict) -> list[dict]:
    """Words of a Vosk result without the out-of-vocabulary markers."""
    return [w for w in result.get("result", []) if w.get("word") not in UNK_SYMBOLS]


class VoskRecognizer:
    """One Kaldi model per language; `grammars[lang]` switches that language to grammar mode."""

    def __init__(
        self,
        model_dirs: Mapping[str, str | Path],
        grammars: Mapping[str, list[str]] | None = None,
    ) -> None:
        import vosk

        self._vosk = vosk
        self.grammars = dict(grammars or {})
        self.models = {lang: load_model(Path(path)) for lang, path in model_dirs.items()}
        self._recognizers: dict[str, object] = {}
        self._lock = threading.Lock()  # transcribe() runs in worker threads

    @property
    def name(self) -> str:
        return "vosk-grammar" if self.grammars else "vosk"

    def _recognizer(self, lang: str):
        rec = self._recognizers.get(lang)
        if rec is None:
            grammar = self.grammars.get(lang)
            if grammar:
                rec = self._vosk.KaldiRecognizer(
                    self.models[lang], SAMPLE_RATE, json.dumps(grammar, ensure_ascii=False)
                )
            else:
                rec = self._vosk.KaldiRecognizer(self.models[lang], SAMPLE_RATE)
            rec.SetWords(True)  # per-word confidences
            self._recognizers[lang] = rec
        return rec

    def _run(self, audio: np.ndarray, lang: str) -> tuple[str, float]:
        rec = self._recognizer(lang)
        rec.AcceptWaveform(audio.astype(np.int16).tobytes())
        result = json.loads(rec.FinalResult())
        rec.Reset()
        words = recognized_words(result)
        text = " ".join(w["word"] for w in words)
        confidence = float(np.mean([w.get("conf", 0.0) for w in words])) if words else 0.0
        return text, confidence

    def transcribe(self, audio: np.ndarray, lang: str = "uk") -> Transcript:
        started = time.perf_counter()
        langs = list(self.models) if lang == "auto" else [lang]
        with self._lock:
            results = [(*self._run(audio, lg), lg) for lg in langs if lg in self.models]
        if not results:
            raise ValueError(f"no Vosk model for language {lang!r}")
        text, confidence, chosen = max(results, key=lambda r: r[1])  # auto: best confidence
        return Transcript(
            text=text,
            lang=chosen,
            confidence=round(confidence, 3),
            engine=self.name,
            audio_s=audio.size / SAMPLE_RATE,
            elapsed_s=time.perf_counter() - started,
        )


def vosk_model_dirs(voice: VoiceSettings) -> dict[str, Path]:
    langs = ["uk", "en"] if voice.stt_lang == "auto" else [voice.stt_lang]
    paths = {"uk": voice.vosk_model_uk, "en": voice.vosk_model_en}
    return {lang: model_path(paths[lang]) for lang in langs}


def grammar_for(registry: DeviceRegistry, lang: str, model_dir: Path) -> list[str]:
    grammar, missing = build_grammar(
        registry, lang, set(vocab) if (vocab := vocabulary_of(model_dir)) else None
    )
    if missing:
        log.info("words unknown to the %s model (not recognizable): %s", lang, ", ".join(missing))
    return grammar


def create_vosk(voice: VoiceSettings, registry: DeviceRegistry) -> VoskRecognizer:
    """VoskRecognizer for settings.voice (languages, model paths, grammar on/off)."""
    dirs = vosk_model_dirs(voice)
    grammars = (
        {lang: grammar_for(registry, lang, path) for lang, path in dirs.items()}
        if voice.stt_grammar
        else None
    )
    return VoskRecognizer(dirs, grammars)
