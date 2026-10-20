import importlib.util
from pathlib import Path

import numpy as np

_spec = importlib.util.spec_from_file_location(
    "audio_check", Path(__file__).resolve().parents[1] / "scripts" / "audio_check.py"
)
ac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ac)

RATE = ac.SAMPLE_RATE


def _tone(amplitude: float, seconds: float = 1.0) -> np.ndarray:
    t = np.arange(int(RATE * seconds)) / RATE
    return (amplitude * 32767 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)


def test_silence_detected() -> None:
    level = ac.measure(np.zeros(RATE, dtype=np.int16))
    assert level.verdict.startswith("SILENT")


def test_normal_speech_level_ok() -> None:
    # Half a second of silence, then a tone at about -15 dBFS.
    samples = np.concatenate([np.zeros(RATE // 2, dtype=np.int16), _tone(0.25)])
    level = ac.measure(samples)
    assert -20 < level.loudest_frame_dbfs < -10
    assert level.noise_floor_dbfs < -100
    assert level.verdict == "OK"


def test_quiet_and_clipping() -> None:
    assert ac.measure(_tone(0.005)).verdict.startswith("QUIET")
    clipped = np.clip(_tone(2.0).astype(np.int32) * 2, -32768, 32767).astype(np.int16)
    assert ac.measure(clipped).verdict.startswith("CLIPPING")


def test_level_bar_bounds() -> None:
    assert ac.level_bar(-120, 10) == "[..........]"
    assert ac.level_bar(0, 10) == "[##########]"


def test_save_wav_roundtrip(tmp_path: Path) -> None:
    import wave

    path = tmp_path / "x.wav"
    ac.save_wav(path, _tone(0.1, 0.1), RATE)
    with wave.open(str(path)) as wf:
        assert (wf.getframerate(), wf.getnchannels(), wf.getsampwidth()) == (RATE, 1, 2)
        assert wf.getnframes() == RATE // 10
