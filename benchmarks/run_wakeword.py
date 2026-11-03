"""E5: wake word detectors - false rejects on clips with the word, false accepts without it.

    python benchmarks/run_wakeword.py      # synthetic set (make_synth_dataset.py)
    python benchmarks/run_wakeword.py --manifest benchmarks/datasets/voice/wake_manifest.csv \
        --label real
    python benchmarks/run_wakeword.py --noise benchmarks/datasets/noise   # FA per hour

Every clip is fed frame by frame (30 ms, as from the microphone) into a fresh
detector state. Positive clips are those whose `engine` column names the
detector (vosk_kws: «Телекінез», openwakeword: "hey jarvis"); clips with
wake=0 are speech without any wake word. Long background audio (TV, music,
talk) from --noise gives false accepts per hour - the number that matters in
a living room; without it that part stays [ЧЕКАЄ: фонове аудіо].
"""

import argparse
import csv
import time
from pathlib import Path
from typing import Any

from common import (
    DATASETS,
    bar_style,
    conditions,
    fmt,
    out_path,
    setup_plot,
    write_csv,
    write_json,
)

from teleckinez.core.config import load_devices, load_settings
from teleckinez.devices.registry import DeviceRegistry
from teleckinez.voice.audio_io import frames_of, read_wav

DETECTORS = ("vosk_kws", "openwakeword")
DETECTOR_UK = {"vosk_kws": "Vosk KWS «Телекінез»", "openwakeword": "openWakeWord «hey jarvis»"}


def build(name: str):
    v = load_settings().voice
    if name == "vosk_kws":
        from teleckinez.voice.triggers import create_vosk_kws

        return create_vosk_kws(v, DeviceRegistry(load_devices()))
    from teleckinez.voice.triggers import create_openwakeword

    return create_openwakeword(v)


def detect(detector, audio) -> tuple[int, float]:
    """Number of hits in the clip and the processing time per second of audio."""
    detector.reset()
    hits = 0
    started = time.perf_counter()
    for frame in frames_of(audio):
        if detector.process(frame) is not None:
            hits += 1
            detector.reset()
    return hits, time.perf_counter() - started


def noise_files(folder: Path | None) -> list[Path]:
    if folder is None or not folder.is_dir():
        return []
    return sorted(p for p in folder.rglob("*.wav"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", default=str(DATASETS / "voice_synth" / "wake_manifest.csv"))
    ap.add_argument("--label", default="synthetic")
    ap.add_argument("--noise", default=str(DATASETS / "noise"), help="folder of background WAVs")
    ap.add_argument("--detectors", nargs="+", choices=DETECTORS, default=list(DETECTORS))
    ap.add_argument("--name", default="")
    args = ap.parse_args()
    with Path(args.manifest).open(encoding="utf-8", newline="") as f:
        clips = list(csv.DictReader(f))
    noise = noise_files(Path(args.noise))
    name = args.name or f"e5_wakeword_{args.label}"
    rows: list[dict[str, Any]] = []
    stats: dict[str, Any] = {}
    for det_name in args.detectors:
        detector = build(det_name)
        positives = negatives = misses = fa = 0
        neg_seconds = proc = audio_s = 0.0
        print(f"E5 {det_name}: {len(clips)} clips, {len(noise)} noise files", flush=True)
        for clip in clips:
            audio = read_wav(DATASETS / clip["file"])
            hits, elapsed = detect(detector, audio)
            seconds = audio.size / 16_000
            proc += elapsed
            audio_s += seconds
            own = clip["engine"] == det_name and clip["wake"] == "1"
            if clip["wake"] == "1" and not own:
                continue  # the other detector's word: neither a positive nor clean speech
            if own:
                positives += 1
                misses += hits == 0
            else:
                negatives += 1
                neg_seconds += seconds
                fa += hits
            rows.append({"detector": det_name, "label": args.label, "file": clip["file"],
                         "positive": own, "hits": hits, "speaker": clip["speaker"],
                         "text": clip["text"], "audio_s": round(seconds, 2)})  # fmt: skip
        noise_fa, noise_s = 0, 0.0
        for path in noise:
            audio = read_wav(path)
            hits, _ = detect(detector, audio)
            noise_fa += hits
            noise_s += audio.size / 16_000
            rows.append({"detector": det_name, "label": "noise", "file": str(path),
                         "positive": False, "hits": hits,
                         "audio_s": round(audio.size / 16_000, 1)})  # fmt: skip
        stats[det_name] = {
            "positives": positives,
            "false_rejects": misses,
            "frr_pct": 100 * misses / positives if positives else None,
            "speech_negatives": negatives,
            "speech_negative_hours": neg_seconds / 3600,
            "speech_false_accepts": fa,
            "speech_fa_per_hour": fa / (neg_seconds / 3600) if neg_seconds else None,
            "noise_hours": noise_s / 3600,
            "noise_false_accepts": noise_fa if noise else None,
            "noise_fa_per_hour": noise_fa / (noise_s / 3600) if noise_s else None,
            "rtf": proc / audio_s if audio_s else None,
        }
        s = stats[det_name]
        print(
            f"  FRR {fmt(s['frr_pct'])} % ({misses}/{positives}); FA on speech {fa} in "
            f"{neg_seconds / 60:.1f} min ({fmt(s['speech_fa_per_hour'])}/h); noise: "
            + (f"{noise_fa} in {noise_s / 3600:.2f} h" if noise else "[ЧЕКАЄ: фонове аудіо]")
            + f"; RTF {fmt(s['rtf'], 3)}"
        )
    csv_path = write_csv(name, rows)
    plt = setup_plot()
    fig, ax = plt.subplots(figsize=(6.5, 3.6))
    dets = list(stats)
    ax.bar(range(len(dets)), [stats[d]["frr_pct"] or 0 for d in dets], 0.5,
           **bar_style(1))  # fmt: skip
    for i, d in enumerate(dets):
        s = stats[d]
        ax.text(i, (s["frr_pct"] or 0) + 1, f"FRR {s['false_rejects']}/{s['positives']}\n"
                f"FA {s['speech_false_accepts']} на {s['speech_negatives']} фразах",
                ha="center", fontsize=9)  # fmt: skip
    ax.set_xticks(range(len(dets)))
    ax.set_xticklabels([DETECTOR_UK[d] for d in dets])
    ax.set_ylabel("Пропущені активації (FRR), %")
    ax.set_ylim(0, max([stats[d]["frr_pct"] or 0 for d in dets] + [10]) * 1.5)
    ax.set_title(f"Набір: {args.label}", fontsize=11)
    png = out_path(name, ".png")
    fig.savefig(png)
    plt.close(fig)
    write_json(name, conditions(experiment="E5 wake word", label=args.label,
                                manifest=args.manifest, noise_files=len(noise),
                                voice_settings=load_settings().voice.model_dump(),
                                results=stats))  # fmt: skip
    print(f"-> {csv_path}\n-> {png}")


if __name__ == "__main__":
    main()
