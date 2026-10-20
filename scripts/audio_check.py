"""Microphone/speaker check: record a few seconds, report the signal level, play it back.

Usage:
    python scripts/audio_check.py              # 3 s at 16 kHz mono (the format STT uses)
    python scripts/audio_check.py --list       # list audio devices
    python scripts/audio_check.py --input 1 --output 5 --seconds 5
    python scripts/audio_check.py --no-play    # only measure, no playback
    python scripts/audio_check.py --save check.wav
"""

import argparse
import math
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16_000  # voice.md §1: 16 kHz, mono, int16
FRAME_MS = 30
SILENCE_DBFS = -50.0  # below this the microphone is probably muted or wrong device
QUIET_DBFS = -35.0  # speech this quiet will hurt recognition
CLIP_LEVEL = 32_000


@dataclass(frozen=True)
class Level:
    rms_dbfs: float  # whole recording
    peak_dbfs: float
    loudest_frame_dbfs: float  # loudest 30 ms frame, ~ level of speech
    noise_floor_dbfs: float  # 10th percentile of frames, ~ background noise
    clipped_ratio: float

    @property
    def verdict(self) -> str:
        if self.loudest_frame_dbfs < SILENCE_DBFS:
            return (
                "SILENT: no signal - check that the microphone is on and the right device is used"
            )
        if self.clipped_ratio > 0.001:
            return "CLIPPING: signal too loud - lower the input gain or speak further away"
        if self.loudest_frame_dbfs < QUIET_DBFS:
            return "QUIET: speech is weak - raise the input gain or speak closer"
        return "OK"


def _dbfs(value: float) -> float:
    return 20 * math.log10(value / 32768) if value > 0 else -120.0


def measure(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> Level:
    """Signal level of mono int16 samples."""
    x = samples.astype(np.float64).ravel()
    if x.size == 0:
        return Level(-120.0, -120.0, -120.0, -120.0, 0.0)
    frame = max(1, sample_rate * FRAME_MS // 1000)
    n_frames = max(1, x.size // frame)
    frames = x[: n_frames * frame].reshape(n_frames, -1) if x.size >= frame else x[None, :]
    frame_rms = np.sqrt(np.mean(frames**2, axis=1))
    return Level(
        rms_dbfs=_dbfs(float(np.sqrt(np.mean(x**2)))),
        peak_dbfs=_dbfs(float(np.max(np.abs(x)))),
        loudest_frame_dbfs=_dbfs(float(np.max(frame_rms))),
        noise_floor_dbfs=_dbfs(float(np.percentile(frame_rms, 10))),
        clipped_ratio=float(np.mean(np.abs(x) >= CLIP_LEVEL)),
    )


def level_bar(dbfs: float, width: int = 40) -> str:
    filled = round(width * min(1.0, max(0.0, (dbfs + 60) / 60)))  # -60..0 dBFS
    return "[" + "#" * filled + "." * (width - filled) + "]"


def save_wav(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.astype(np.int16).tobytes())


def _device_name(sd, index: int | None, kind: str) -> str:
    info = sd.query_devices(index, kind)
    return f"#{info['index']} {info['name']}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--rate", type=int, default=SAMPLE_RATE)
    parser.add_argument("--input", type=int, default=None, help="input device index (see --list)")
    parser.add_argument("--output", type=int, default=None, help="output device index")
    parser.add_argument("--list", action="store_true", help="list audio devices and exit")
    parser.add_argument("--no-play", action="store_true", help="do not play the recording back")
    parser.add_argument("--save", type=Path, help="save the recording as WAV")
    args = parser.parse_args(argv)

    import sounddevice as sd

    if args.list:
        print(sd.query_devices())
        return 0

    print(f"input:  {_device_name(sd, args.input, 'input')}")
    print(f"output: {_device_name(sd, args.output, 'output')}")
    for i in (3, 2, 1):
        print(f"\rrecording in {i}...", end="", flush=True)
        time.sleep(1)
    print(f"\rRECORDING {args.seconds:g} s - say something, e.g. 'Телекінез, увімкни світло'")
    samples = sd.rec(
        int(args.seconds * args.rate),
        samplerate=args.rate,
        channels=1,
        dtype="int16",
        device=args.input,
    )
    sd.wait()
    samples = samples[:, 0]

    level = measure(samples, args.rate)
    snr = level.loudest_frame_dbfs - level.noise_floor_dbfs
    rows = [
        ("speech (loudest frame)", level.loudest_frame_dbfs, True),
        ("noise floor", level.noise_floor_dbfs, True),
        ("peak", level.peak_dbfs, False),
    ]
    for label, value, bar in rows:
        print(f"  {label:<23}{value:6.1f} dBFS {level_bar(value) if bar else ''}")
    print(f"  {'signal-to-noise':<23}{snr:6.1f} dB")
    print(f"  {'clipped samples':<23}{level.clipped_ratio:6.2%}")
    print(f"result: {level.verdict}")

    if args.save:
        save_wav(args.save, samples, args.rate)
        print(f"saved to {args.save}")
    if not args.no_play:
        print("playing back...")
        sd.play(samples, samplerate=args.rate, device=args.output)
        sd.wait()
    return 0 if level.verdict == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
