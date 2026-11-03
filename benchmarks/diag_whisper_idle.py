"""Why is Whisper STT slower in E3 (inside the voice pipeline) than in E1?

E1 transcribes clips back to back; in E3 the GPU idles for seconds between
phrases (real-time playback, wake word, the device's answer). This script runs
the same clips through the same WhisperRecognizer with different pauses before
each call and, optionally, with a warm-up call right before (what the pipeline
can do when the wake word fires, while the user is still speaking).

    python benchmarks/diag_whisper_idle.py --limit 20
"""

import argparse
import csv
import statistics
import time

from common import DATASETS

from teleckinez.core.config import load_devices, load_settings
from teleckinez.devices.registry import DeviceRegistry
from teleckinez.voice.audio_io import read_wav
from teleckinez.voice.stt.whisper_stt import create_whisper


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20, help="uk clips per condition")
    ap.add_argument("--pauses", nargs="+", type=float, default=[0.0, 1.0, 3.0, 6.0])
    args = ap.parse_args()

    registry = DeviceRegistry(load_devices())
    v = load_settings().voice.model_copy()
    v.stt_lang, v.whisper_device = "uk", "cuda"
    stt = create_whisper(v, registry)
    print("device:", stt.device)

    with open(DATASETS / "voice_synth" / "manifest.csv", encoding="utf-8") as f:
        clips = [r for r in csv.DictReader(f) if r["lang"] == "uk"][: args.limit]
    audio = [read_wav(DATASETS / c["file"]) for c in clips]
    stt.transcribe(audio[0], "uk")  # load / first-call cost is not measured

    for warm in (False, True):
        for pause in args.pauses:
            times = []
            for a in audio:
                time.sleep(pause)
                if warm:
                    stt.transcribe(audio[0][:8000], "uk")  # 0.5 s warm-up call
                times.append(stt.transcribe(a, "uk").elapsed_s * 1000)
            label = f"pause {pause:.0f} s" + (" + warm-up" if warm else "")
            print(
                f"{label:22} p50 {statistics.median(times):6.0f} ms   "
                f"p95 {sorted(times)[int(0.95 * (len(times) - 1))]:6.0f} ms"
            )


if __name__ == "__main__":
    main()
