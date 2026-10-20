import asyncio
from pathlib import Path

import numpy as np
import pytest

from teleckinez.voice.audio_io import (
    FRAME_BYTES,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    HalfDuplex,
    MicrophoneSource,
    NullOutput,
    WavSource,
    frames_of,
    read_wav,
    resample,
    tone,
    write_wav,
)


def test_frames_are_30_ms_and_padded() -> None:
    frames = frames_of(np.ones(FRAME_SAMPLES * 2 + 10, np.int16))
    assert len(frames) == 3 and all(len(f) == FRAME_BYTES for f in frames)
    assert np.frombuffer(frames[-1], np.int16)[10:].sum() == 0


def test_resample_changes_length_and_keeps_shape() -> None:
    src = tone(440, 1.0, rate=48_000)
    out = resample(src, 48_000, SAMPLE_RATE)
    assert out.size == SAMPLE_RATE and out.dtype == np.int16
    assert abs(int(np.abs(out).max()) - int(np.abs(src).max())) < 200


def test_wav_roundtrip_converts_rate_and_channels(tmp_path: Path) -> None:
    import wave

    stereo = np.stack([tone(300, 0.5, 44_100)] * 2, axis=1)
    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(44_100)
        wf.writeframes(stereo.astype(np.int16).tobytes())
    assert read_wav(path).size == SAMPLE_RATE // 2
    mono = tmp_path / "mono.wav"
    write_wav(mono, tone(300, 0.1))
    assert read_wav(mono).size == SAMPLE_RATE // 10


async def test_wav_source_yields_audio_then_silence() -> None:
    source = WavSource(tone(440, 0.3), trailing_silence_s=0.3)
    frames = [f async for f in source.frames()]
    assert len(frames) == 20  # 0.6 s / 30 ms
    assert np.frombuffer(frames[0], np.int16).any()
    assert not np.frombuffer(frames[-1], np.int16).any()


async def test_queue_drops_oldest_and_mute_discards() -> None:
    source = MicrophoneSource()
    for i in range(205):
        source.push(bytes([i % 256]) * FRAME_BYTES)
    assert source.dropped == 5
    first = await anext(aiter(source.frames()))
    assert first[0] == 5
    source.mute()
    source.push(b"x" * FRAME_BYTES)
    source.unmute()  # also flushes what was queued before muting
    assert source._queue.empty()


async def test_microphone_callback_resamples_device_rate() -> None:
    source = MicrophoneSource()
    source._loop = asyncio.get_running_loop()
    source._rate = 48_000
    block = tone(440, 0.03, rate=48_000)  # one 30 ms block at 48 kHz
    source._callback(block.tobytes(), block.size, None, None)
    await asyncio.sleep(0)
    frame = await asyncio.wait_for(anext(aiter(source.frames())), 1)
    assert len(frame) == FRAME_BYTES


async def test_half_duplex_mutes_during_playback() -> None:
    source = MicrophoneSource()
    states: list[bool] = []

    class SpyOutput(NullOutput):
        async def play(self, samples, rate):
            states.append(source.muted)
            await super().play(samples, rate)

    duplex = HalfDuplex(source, SpyOutput())
    await duplex.play(tone(880, 0.05), SAMPLE_RATE)
    assert states == [True] and source.muted is False


@pytest.mark.parametrize("freq", [440, 880])
def test_tone(freq: int) -> None:
    beep = tone(freq, 0.1)
    assert beep.size == SAMPLE_RATE // 10 and beep[0] == 0 and beep.max() > 9000
