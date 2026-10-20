import numpy as np
import pytest

from teleckinez.voice.audio_io import FRAME_BYTES, SAMPLE_RATE, WavSource, frames_of, tone
from teleckinez.voice.vad import EnergyVad, RecorderState, UtteranceRecorder, WebrtcVad

SILENCE = bytes(FRAME_BYTES)
LOUD = frames_of(tone(300, 0.03, volume=0.5))[0]


def recorder(**kw) -> UtteranceRecorder:
    defaults = {"start_frames": 3, "end_silence_ms": 300, "max_utterance_s": 1.0}
    return UtteranceRecorder(EnergyVad(), **{**defaults, **kw})


def feed(rec: UtteranceRecorder, frames: list[bytes]) -> list[RecorderState]:
    return [rec.feed(f) for f in frames]


def test_starts_after_three_voiced_frames_with_pre_roll() -> None:
    rec = recorder(pre_roll_ms=150)  # 5 frames
    states = feed(rec, [SILENCE] * 10 + [LOUD] * 2)
    assert states[-1] is RecorderState.WAITING
    assert rec.feed(LOUD) is RecorderState.SPEECH
    assert len(rec._frames) == 5  # pre-roll: 2 silent frames + 3 voiced ones


def test_short_noise_burst_does_not_start() -> None:
    rec = recorder()
    states = feed(rec, [LOUD, LOUD, SILENCE, LOUD, SILENCE] * 4)
    assert set(states) == {RecorderState.WAITING}


def test_ends_after_silence_and_trims_it() -> None:
    rec = recorder(pre_roll_ms=0)
    feed(rec, [LOUD] * 20 + [SILENCE] * 10)  # 300 ms of silence = 10 frames
    assert rec.state is RecorderState.DONE and rec.reason == "silence"
    utt = rec.utterance()
    # 20 voiced frames + 5 frames (150 ms) of the trailing silence
    assert utt.audio.size == 25 * FRAME_BYTES // 2


def test_pauses_shorter_than_end_silence_keep_recording() -> None:
    rec = recorder()
    feed(rec, [LOUD] * 5 + [SILENCE] * 5 + [LOUD] * 5)
    assert rec.state is RecorderState.SPEECH


def test_hard_length_limit() -> None:
    rec = recorder(max_utterance_s=0.3)  # 10 frames
    feed(rec, [LOUD] * 40)
    assert rec.state is RecorderState.DONE and rec.reason == "max_length"


def test_no_speech_timeout() -> None:
    rec = recorder(no_speech_timeout_s=0.3)
    feed(rec, [SILENCE] * 10)
    assert rec.state is RecorderState.DONE and rec.reason == "no_speech"
    assert rec.utterance() is None


async def test_record_from_source_calls_speech_start() -> None:
    audio = np.concatenate([np.zeros(SAMPLE_RATE // 2, np.int16), tone(300, 0.6, volume=0.5)])
    started: list[bool] = []
    utt = await recorder(max_utterance_s=3).record(
        WavSource(audio, trailing_silence_s=0.5).frames(), lambda: started.append(True)
    )
    assert started == [True]
    assert utt is not None and utt.reason == "silence" and 0.6 <= utt.duration_s <= 1.2


async def test_webrtcvad_on_synthesized_speech(speech) -> None:
    phrase = speech("увімкни світло на кухні")
    audio = np.concatenate([np.zeros(SAMPLE_RATE, np.int16), phrase])
    rec = UtteranceRecorder(WebrtcVad(2), end_silence_ms=700)
    utt = await rec.record(WavSource(audio, trailing_silence_s=1.5).frames())
    assert utt is not None and utt.reason == "silence"
    # the recorded part covers the phrase and not the leading second of silence
    assert phrase.size / SAMPLE_RATE - 0.3 < utt.duration_s < phrase.size / SAMPLE_RATE + 0.6


@pytest.mark.parametrize("aggressiveness", [0, 3])
def test_webrtcvad_rejects_silence(aggressiveness: int) -> None:
    assert WebrtcVad(aggressiveness).is_speech(SILENCE) is False
