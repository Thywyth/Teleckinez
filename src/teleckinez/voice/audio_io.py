"""Audio input/output (docs/specs/voice.md §1).

Input: 16 kHz, mono, int16 in 30 ms frames. The sounddevice callback runs in
an audio thread and hands frames to an asyncio.Queue. Half-duplex: while the
assistant speaks the input is muted, so it does not recognize itself.

Sources and outputs are small interfaces, so the pipeline can run from a
WAV file and into a null output in tests (no microphone needed).
"""

import asyncio
import contextlib
import logging
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 480
FRAME_BYTES = FRAME_SAMPLES * 2
QUEUE_FRAMES = 200  # ~6 s of audio buffered at most; older frames are dropped
UNMUTE_TAIL_S = 0.15  # keep the mic muted a bit after playback (room echo)


def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear-interpolation resampling of int16 mono audio (good enough for speech)."""
    if src_rate == dst_rate or samples.size == 0:
        return samples.astype(np.int16)
    n_out = round(samples.size * dst_rate / src_rate)
    x_old = np.linspace(0.0, 1.0, samples.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
    return np.interp(x_new, x_old, samples.astype(np.float32)).astype(np.int16)


def frames_of(samples: np.ndarray) -> list[bytes]:
    """Split int16 audio into whole 30 ms frames (a partial tail is padded with silence)."""
    samples = samples.astype(np.int16)
    pad = (-samples.size) % FRAME_SAMPLES
    if pad:
        samples = np.concatenate([samples, np.zeros(pad, np.int16)])
    raw = samples.tobytes()
    return [raw[i : i + FRAME_BYTES] for i in range(0, len(raw), FRAME_BYTES)]


def read_wav(path: str | Path) -> np.ndarray:
    """Read a WAV file as 16 kHz mono int16 (converting channels / rate if needed)."""
    with wave.open(str(path), "rb") as wf:
        if wf.getsampwidth() != 2:
            raise ValueError(f"{path}: only 16-bit PCM is supported")
        data = np.frombuffer(wf.readframes(wf.getnframes()), np.int16)
        if wf.getnchannels() > 1:
            data = data.reshape(-1, wf.getnchannels()).mean(axis=1).astype(np.int16)
        return resample(data, wf.getframerate(), SAMPLE_RATE)


def write_wav(path: str | Path, samples: np.ndarray, rate: int = SAMPLE_RATE) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.astype(np.int16).tobytes())


# --------------------------------------------------------------------- sources


class AudioSource(Protocol):
    def frames(self) -> AsyncIterator[bytes]:
        """30 ms frames of 16 kHz mono int16 PCM."""
        ...

    def mute(self) -> None: ...

    def unmute(self) -> None: ...


class _QueuedSource:
    """Common part: a bounded queue that drops the oldest frame on overflow, plus mute."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(QUEUE_FRAMES)
        self.muted = False
        self.dropped = 0

    def push(self, frame: bytes) -> None:
        """Called on the event loop thread."""
        if self.muted:
            return
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self.dropped += 1
        self._queue.put_nowait(frame)

    def mute(self) -> None:
        self.muted = True

    def unmute(self) -> None:
        # Frames captured just before muting may contain the start of our own speech.
        while not self._queue.empty():
            self._queue.get_nowait()
        self.muted = False

    async def frames(self) -> AsyncIterator[bytes]:
        while True:
            yield await self._queue.get()


class MicrophoneSource(_QueuedSource):
    """sounddevice input stream; opens at 16 kHz, or at the device rate and resamples."""

    def __init__(self, device: int | str | None = None) -> None:
        super().__init__()
        self.device = device
        self._stream = None
        self._rate = SAMPLE_RATE
        self._pending = np.zeros(0, np.int16)
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        import sounddevice as sd

        self._loop = asyncio.get_running_loop()
        try:
            self._open(sd, SAMPLE_RATE)
        except sd.PortAudioError:
            rate = int(sd.query_devices(self.device, "input")["default_samplerate"])
            log.info("input device does not support 16 kHz, capturing at %d Hz", rate)
            self._open(sd, rate)

    def _open(self, sd, rate: int) -> None:
        self._rate = rate
        blocksize = rate * FRAME_MS // 1000
        self._stream = sd.RawInputStream(
            samplerate=rate,
            blocksize=blocksize,
            channels=1,
            dtype="int16",
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status) -> None:  # audio thread
        if status:
            log.debug("input status: %s", status)
        data = bytes(indata)
        if self._rate != SAMPLE_RATE:
            converted = resample(np.frombuffer(data, np.int16), self._rate, SAMPLE_RATE)
            self._pending = np.concatenate([self._pending, converted])
            while self._pending.size >= FRAME_SAMPLES:
                chunk, self._pending = self._pending[:FRAME_SAMPLES], self._pending[FRAME_SAMPLES:]
                self._post(chunk.tobytes())
        else:
            self._post(data)

    def _post(self, frame: bytes) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self.push, frame)

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class WavSource(_QueuedSource):
    """Frames from audio in memory or a WAV file; for tests and offline benchmarks.

    `realtime=True` paces frames at 30 ms like a microphone would. After the
    audio, `trailing_silence_s` of silence lets the VAD see the end of speech.
    """

    def __init__(
        self,
        audio: np.ndarray | str | Path,
        *,
        realtime: bool = False,
        trailing_silence_s: float = 1.0,
        repeat: bool = False,
    ) -> None:
        super().__init__()
        samples = read_wav(audio) if isinstance(audio, str | Path) else audio
        silence = np.zeros(int(trailing_silence_s * SAMPLE_RATE), np.int16)
        self._frames = frames_of(np.concatenate([samples.astype(np.int16), silence]))
        self.realtime = realtime
        self.repeat = repeat

    async def frames(self) -> AsyncIterator[bytes]:
        while True:
            for frame in self._frames:
                if self.realtime:
                    await asyncio.sleep(FRAME_MS / 1000)
                else:
                    await asyncio.sleep(0)
                if not self.muted:
                    yield frame
            if not self.repeat:
                return


# --------------------------------------------------------------------- outputs


class AudioOutput(Protocol):
    async def play(self, samples: np.ndarray, rate: int) -> None: ...


class SpeakerOutput:
    """Plays int16 audio on the default (or given) output device without blocking the loop."""

    def __init__(self, device: int | str | None = None) -> None:
        self.device = device

    async def play(self, samples: np.ndarray, rate: int) -> None:
        if samples.size == 0:
            return
        await asyncio.to_thread(self._play_blocking, samples.astype(np.int16), rate)

    def _play_blocking(self, samples: np.ndarray, rate: int) -> None:
        import sounddevice as sd

        sd.play(samples, samplerate=rate, device=self.device)
        sd.wait()


class NullOutput:
    """Records what would have been played (tests, text-only mode)."""

    def __init__(self) -> None:
        self.played: list[tuple[np.ndarray, int]] = []

    async def play(self, samples: np.ndarray, rate: int) -> None:
        self.played.append((samples, rate))


class HalfDuplex:
    """Mutes the input source while something is played (voice.md §1)."""

    def __init__(self, source: AudioSource | None, output: AudioOutput) -> None:
        self.source = source
        self.output = output
        self._lock = asyncio.Lock()

    async def play(self, samples: np.ndarray, rate: int) -> None:
        async with self._lock:
            if self.source is not None:
                self.source.mute()
            try:
                await self.output.play(samples, rate)
                await asyncio.sleep(UNMUTE_TAIL_S)
            finally:
                if self.source is not None:
                    self.source.unmute()


def tone(
    freq: float, duration_s: float, rate: int = SAMPLE_RATE, volume: float = 0.3
) -> np.ndarray:
    """A short sine beep with soft edges ("слухаю" / "не зрозумів" signals)."""
    t = np.arange(int(duration_s * rate)) / rate
    wave_ = np.sin(2 * np.pi * freq * t)
    fade = min(len(t) // 4, int(0.01 * rate))
    if fade:
        envelope = np.ones_like(wave_)
        envelope[:fade] = np.linspace(0, 1, fade)
        envelope[-fade:] = np.linspace(1, 0, fade)
        wave_ *= envelope
    return (wave_ * volume * 32767).astype(np.int16)
