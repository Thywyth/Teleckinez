"""faster-whisper (CTranslate2) recognizer: CUDA float16, CPU int8 as fallback (voice.md §4)."""

import logging
import math
import threading
import time
from pathlib import Path

import numpy as np

from teleckinez.core.config import VoiceSettings
from teleckinez.devices.registry import DeviceRegistry
from teleckinez.voice.audio_io import SAMPLE_RATE
from teleckinez.voice.cuda import register_cuda_dlls
from teleckinez.voice.paths import model_path
from teleckinez.voice.stt.base import Transcript

log = logging.getLogger(__name__)

# Whisper "hears" phrases in silence or noise ("Дякую за перегляд!"); such segments
# have a high no-speech probability and a low log-probability at the same time.
NO_SPEECH_PROB = 0.6
LOW_LOGPROB = -1.0
# With an initial_prompt, silence or faint noise comes back as a piece of the prompt
# ("Датчик клімату у спальні."), so near-silent audio is not decoded at all.
SILENCE_RMS_DBFS = -50.0
WARM_UP_EVERY_S = 0.5


def build_prompt(registry: DeviceRegistry, lang: str) -> str:
    """initial_prompt with this home's names: Whisper then spells them as in devices.yaml."""
    names: list[str] = []
    for dev in registry:
        names += dev.config.names.get(lang)[:2]
    for room in registry.rooms.values():
        names += room.get(lang)[:1]
    names = list(dict.fromkeys(n.lower() for n in names))
    if lang == "uk":
        return "Телекінез. Увімкни світло на кухні. " + ", ".join(names) + "."
    return "Turn on the kitchen light. " + ", ".join(names) + "."


class WhisperRecognizer:
    name = "whisper"

    def __init__(
        self,
        model_dir: str | Path,
        *,
        device: str = "auto",  # auto | cuda | cpu
        prompts: dict[str, str] | None = None,
        beam_size: int = 5,
    ) -> None:
        from faster_whisper import WhisperModel

        model_dir = Path(model_dir)
        if not (model_dir / "model.bin").is_file():
            raise FileNotFoundError(
                f"Whisper model not found: {model_dir} (python scripts/download_models.py "
                "--only whisper)"
            )
        self.prompts = prompts or {}
        self.beam_size = beam_size
        self._lock = threading.Lock()
        self.model = None
        if device in ("auto", "cuda"):
            register_cuda_dlls()
            try:
                self.model = WhisperModel(str(model_dir), device="cuda", compute_type="float16")
                self._run(np.zeros(SAMPLE_RATE // 2, np.float32), "uk")  # CUDA errors are lazy
                self.device = "cuda float16"
            except Exception as exc:  # no NVIDIA GPU, no cuBLAS/cuDNN, out of memory ...
                if device == "cuda":
                    raise
                log.warning("Whisper on CUDA failed (%s); using CPU int8", exc)
                self.model = None
        if self.model is None:
            self.model = WhisperModel(str(model_dir), device="cpu", compute_type="int8")
            self.device = "cpu int8"
        log.info("Whisper %s runs on %s", model_dir.name, self.device)

    def _run(self, audio: np.ndarray, lang: str) -> tuple[str, float, str]:
        segments, info = self.model.transcribe(
            audio,
            language=None if lang == "auto" else lang,
            beam_size=self.beam_size,
            initial_prompt=self.prompts.get("uk" if lang == "auto" else lang),
            vad_filter=False,  # the utterance is already cut by our VAD
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        kept = [
            s
            for s in segments  # lazy: decoding happens while iterating
            if not (s.no_speech_prob > NO_SPEECH_PROB and s.avg_logprob < LOW_LOGPROB)
        ]
        text = " ".join(s.text.strip() for s in kept).strip()
        logprob = float(np.mean([s.avg_logprob for s in kept])) if kept else -math.inf
        return text, math.exp(logprob) if kept else 0.0, info.language

    def warm_up(self) -> None:
        """Keep a laptop GPU at working clocks while the user is speaking.

        After 1-3 s without work the GPU lowers its clocks, and the next phrase
        took ~400 ms instead of ~150 ms (benchmarks/diag_whisper_idle.py). The
        pipeline calls this every WARM_UP_EVERY_S while listening. Skipped on CPU
        (a call costs as much as a real phrase there) and while a phrase is decoded.
        """
        if not self.device.startswith("cuda") or not self._lock.acquire(blocking=False):
            return
        try:
            self._run(np.zeros(SAMPLE_RATE // 2, np.float32), "en")
        finally:
            self._lock.release()

    def transcribe(self, audio: np.ndarray, lang: str = "uk") -> Transcript:
        started = time.perf_counter()
        samples = audio.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
        if rms <= 10 ** (SILENCE_RMS_DBFS / 20):
            text, confidence, detected = "", 0.0, lang
        else:
            with self._lock:
                text, confidence, detected = self._run(samples, lang)
        return Transcript(
            text=text,
            lang=detected if detected in ("uk", "en") else (lang if lang != "auto" else "uk"),
            confidence=round(min(confidence, 1.0), 3),
            engine=self.name,
            audio_s=audio.size / SAMPLE_RATE,
            elapsed_s=time.perf_counter() - started,
        )


def create_whisper(voice: VoiceSettings, registry: DeviceRegistry) -> WhisperRecognizer:
    return WhisperRecognizer(
        model_path(voice.whisper_model),
        device=voice.whisper_device,
        prompts={lang: build_prompt(registry, lang) for lang in ("uk", "en")},
        beam_size=voice.whisper_beam_size,
    )
