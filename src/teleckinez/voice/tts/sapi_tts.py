"""Windows SAPI through pyttsx3 - the fallback voice (voice.md §5).

Windows usually has no Ukrainian SAPI voice; then Ukrainian answers are text
only. Synthesis goes to a WAV file, so playback uses the same half-duplex
output as Piper.

SAPI is a COM object: it is created, used and released in one dedicated
thread. COM objects freed by the garbage collector in another thread, or
after COM shut down at exit, crash the process with an access violation.
"""

import atexit
import gc
import logging
import queue
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from teleckinez.voice.audio_io import SAMPLE_RATE, read_wav
from teleckinez.voice.tts.base import Speech
from teleckinez.voice.tts.speakable import speakable

log = logging.getLogger(__name__)


class SapiTts:
    name = "sapi"

    def __init__(self) -> None:
        self._jobs: queue.Queue = queue.Queue()
        self._engine: Any = None
        self._thread = threading.Thread(target=self._worker, name="sapi", daemon=True)
        self._thread.start()
        atexit.register(self.close)
        try:
            self.voices: dict[str, str] = self._call(self._init)
        except Exception:
            self.close()
            raise
        if not self.voices:
            self.close()
            raise RuntimeError("no SAPI voices installed")
        self._warned: set[str] = set()

    # ------------------------------------------------------------- COM thread

    def _worker(self) -> None:
        import comtypes

        comtypes.CoInitialize()
        try:
            while (job := self._jobs.get()) is not None:
                fn, future = job
                try:
                    future.set_result(fn())
                except Exception as exc:
                    future.set_exception(exc)
        finally:
            self._engine = None
            gc.collect()  # release COM pointers here, before CoUninitialize
            comtypes.CoUninitialize()

    def _call(self, fn: Callable[[], Any]) -> Any:
        future: Future = Future()
        self._jobs.put((fn, future))
        return future.result()

    def close(self) -> None:
        if self._thread.is_alive():
            self._jobs.put(None)
            self._thread.join(timeout=5)

    def _init(self) -> dict[str, str]:
        import pyttsx3

        self._engine = pyttsx3.init("sapi5")
        voices: dict[str, str] = {}
        for v in self._engine.getProperty("voices"):
            for code in v.languages or []:
                voices.setdefault(str(code).lower()[:2], v.id)
            name = (v.name or "").lower()
            if "ukrain" in name:
                voices.setdefault("uk", v.id)
            if "english" in name:
                voices.setdefault("en", v.id)
        return voices

    # ------------------------------------------------------------------ public

    def synthesize(self, text: str, lang: str = "uk") -> Speech | None:
        if lang not in self.voices:
            if lang not in self._warned:
                self._warned.add(lang)
                log.warning("no SAPI voice for %r; answers in this language are text only", lang)
            return None
        spoken = speakable(text, lang)
        return self._call(lambda: self._synthesize(spoken, lang))

    def _synthesize(self, text: str, lang: str) -> Speech | None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "say.wav"
            self._engine.setProperty("voice", self.voices[lang])
            self._engine.save_to_file(text, str(path))
            self._engine.runAndWait()
            if not path.is_file():
                return None
            return Speech(read_wav(path), SAMPLE_RATE)  # read_wav converts to 16 kHz
