"""E1: speech recognition on a manifest of WAV files (metrics.md §2).

Engines: Vosk free vocabulary, Vosk with the command grammar, faster-whisper
small on the GPU (float16) and on the CPU (int8). For every clip: WER, CER,
whether the command is still understood after the NLU (same scoring as E2),
real-time factor and processing time.

    python benchmarks/run_stt.py                                   # real recordings
    python benchmarks/run_stt.py --manifest benchmarks/datasets/voice_synth/manifest.csv \
        --label synthetic
    python benchmarks/run_stt.py --engines vosk_grammar whisper_gpu --limit 20

The language hint is taken from the manifest (the user would set stt_lang).
"""

import argparse
import csv
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from common import (
    DATASETS,
    bar_style,
    conditions,
    fmt,
    fmt_uk,
    out_path,
    setup_plot,
    summary,
    write_csv,
    write_json,
)
from run_nlu import outcome_of, score

from teleckinez.core.config import load_devices, load_settings
from teleckinez.devices.registry import DeviceRegistry
from teleckinez.nlu.normalizer import tokenize
from teleckinez.nlu.resolver import TargetResolver
from teleckinez.nlu.rule_parser import RuleParser
from teleckinez.voice.audio_io import read_wav

ENGINES = ("vosk_free", "vosk_grammar", "whisper_gpu", "whisper_cpu")
ENGINE_UK = {
    "vosk_free": "Vosk (вільний)",
    "vosk_grammar": "Vosk (граматика)",
    "whisper_gpu": "Whisper small GPU",
    "whisper_cpu": "Whisper small CPU",
}


def edit_distance(a: list, b: list) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def normalize(text: str) -> list[str]:
    """Same normalization as the NLU: case, punctuation, apostrophes, numbers -> digits."""
    return tokenize(text)


def build_engine(name: str, registry: DeviceRegistry, langs: set[str]):
    v = load_settings().voice.model_copy()
    v.stt_lang = "auto" if len(langs) > 1 else next(iter(langs))
    if name.startswith("vosk"):
        from teleckinez.voice.stt.vosk_stt import create_vosk

        v.stt_lang = "auto"  # load both models; the hint picks one per clip
        v.stt_grammar = name == "vosk_grammar"
        return create_vosk(v, registry)
    from teleckinez.voice.stt.whisper_stt import create_whisper

    v.whisper_device = "cuda" if name == "whisper_gpu" else "cpu"
    return create_whisper(v, registry)


def load_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def run_engine(name: str, clips, registry, parser, resolver, label: str) -> list[dict[str, Any]]:
    started = time.perf_counter()
    engine = build_engine(name, registry, {c["lang"] for c in clips})
    load_s = time.perf_counter() - started
    device = getattr(engine, "device", "cpu")
    engine.transcribe(read_wav(DATASETS / clips[0]["file"]), clips[0]["lang"])  # warm-up
    rows = []
    for n, clip in enumerate(clips, 1):
        audio = read_wav(DATASETS / clip["file"])
        t = engine.transcribe(audio, clip["lang"])
        ref, hyp = normalize(clip["text"]), normalize(t.text)
        ref_c, hyp_c = " ".join(ref), " ".join(hyp)
        expect = json.loads(clip["intent"])
        got = outcome_of(parser.parse(t.text), resolver, registry) if t.text else None
        marks = score(expect, got) if got else {"full_ok": "error" in expect}
        rows.append(
            {
                "engine": name,
                "device": device,
                "label": label,
                "file": clip["file"],
                "speaker": clip["speaker"],
                "lang": clip["lang"],
                "cat": clip["cat"],
                "reference": clip["text"],
                "hypothesis": t.text,
                "words": len(ref),
                "word_errors": edit_distance(ref, hyp),
                "chars": len(ref_c),
                "char_errors": edit_distance(list(ref_c), list(hyp_c)),
                "exact": ref == hyp,
                "command_ok": bool(marks["full_ok"]),
                "audio_s": round(t.audio_s, 3),
                "elapsed_ms": round(t.elapsed_s * 1000, 1),
                "rtf": round(t.rtf, 4),
                "load_s": round(load_s, 1),
            }
        )
        if n % 100 == 0:
            print(f"  {name}: {n}/{len(clips)}", flush=True)
    return rows


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    by = defaultdict(list)
    for r in rows:
        by[(r["engine"], r["lang"])].append(r)
        by[(r["engine"], "all")].append(r)
    for (engine, lang), mine in by.items():
        words = sum(r["words"] for r in mine)
        chars = sum(r["chars"] for r in mine)
        out.setdefault(engine, {"device": mine[0]["device"], "load_s": mine[0]["load_s"]})
        out[engine][lang] = {
            "n": len(mine),
            "wer": 100 * sum(r["word_errors"] for r in mine) / words if words else None,
            "cer": 100 * sum(r["char_errors"] for r in mine) / chars if chars else None,
            "sentence_acc": 100 * sum(r["exact"] for r in mine) / len(mine),
            "command_acc": 100 * sum(r["command_ok"] for r in mine) / len(mine),
            "rtf": summary([r["rtf"] for r in mine]),
            "elapsed_ms": summary([r["elapsed_ms"] for r in mine]),
        }
    return out


def plot(stats: dict[str, Any], name: str, label: str) -> list[Path]:
    plt = setup_plot()
    engines = list(stats)
    langs = [lang for lang in ("uk", "en") if any(lang in stats[e] for e in engines)]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10.5, 4.2))
    width = 0.8 / len(engines)
    for i, e in enumerate(engines):
        xs = [k + (i - (len(engines) - 1) / 2) * width for k in range(len(langs))]
        a1.bar(xs, [stats[e].get(lg, {}).get("wer") or 0 for lg in langs], width,
               label=ENGINE_UK[e], **bar_style(i))  # fmt: skip
        a2.bar(xs, [stats[e].get(lg, {}).get("command_acc") or 0 for lg in langs], width,
               **bar_style(i))  # fmt: skip
    for ax, title in ((a1, "WER, % (менше — краще)"), (a2, "Команду зрозуміло, %")):
        ax.set_xticks(range(len(langs)))
        ax.set_xticklabels({"uk": "українська", "en": "англійська"}[lg] for lg in langs)
        ax.set_ylabel(title)
    a2.set_ylim(0, 105)
    a1.legend(fontsize=8.5)
    fig.suptitle(f"Набір: {label}", fontsize=11)
    acc = out_path(f"{name}_accuracy", ".png")
    fig.savefig(acc)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7, 3.8))
    p50 = [stats[e]["all"]["elapsed_ms"]["p50"] for e in engines]
    p95 = [stats[e]["all"]["elapsed_ms"]["p95"] for e in engines]
    xs = range(len(engines))
    ax.bar([x - 0.18 for x in xs], p50, 0.36, label="медіана", **bar_style(0))
    ax.bar([x + 0.18 for x in xs], p95, 0.36, label="p95", **bar_style(2))
    for x, a in zip(xs, p50, strict=True):
        ax.text(x - 0.18, a, fmt_uk(a, 0), ha="center", va="bottom", fontsize=9)
    ax.set_xticks(list(xs))
    ax.set_xticklabels([ENGINE_UK[e].replace(" ", "\n", 1) for e in engines], fontsize=9)
    ax.set_ylabel("Час розпізнавання фрази, мс")
    ax.legend()
    lat = out_path(f"{name}_latency", ".png")
    fig.savefig(lat)
    plt.close(fig)
    return [acc, lat]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", default=str(DATASETS / "voice" / "manifest.csv"))
    ap.add_argument("--label", default="real", help="real | synthetic (goes into every row)")
    ap.add_argument("--engines", nargs="+", choices=ENGINES, default=list(ENGINES))
    ap.add_argument("--limit", type=int, default=0, help="first N clips")
    ap.add_argument("--cpu-limit", type=int, default=0, help="first N clips for whisper_cpu")
    ap.add_argument("--name", default="")
    args = ap.parse_args()
    manifest = Path(args.manifest).resolve()
    if not manifest.exists():
        raise SystemExit(f"{manifest} not found: record (scripts/record_dataset.py) first")
    clips = load_manifest(manifest)[: args.limit or None]
    name = args.name or f"e1_stt_{args.label}"
    registry = DeviceRegistry(load_devices())
    parser, resolver = RuleParser(registry), TargetResolver(registry)
    rows = []
    for engine in args.engines:
        mine = clips[: args.cpu_limit] if engine == "whisper_cpu" and args.cpu_limit else clips
        print(f"E1 {engine}: {len(mine)} clips", flush=True)
        rows += run_engine(engine, mine, registry, parser, resolver, args.label)
    stats = aggregate(rows)
    csv_path = write_csv(name, rows)
    pngs = plot(stats, name, args.label)
    write_json(
        name,
        conditions(
            experiment="E1 STT",
            label=args.label,
            manifest=manifest.relative_to(DATASETS).as_posix(),
            clips=len(clips),
            speakers=sorted({c["speaker"] for c in clips}),
            voice_settings=load_settings().voice.model_dump(),
            results=stats,
        ),
    )
    for e, s in stats.items():
        a = s["all"]
        print(
            f"{e:13s} {s['device']:13s} WER {fmt(a['wer'])} %  CER {fmt(a['cer'])} %  "
            f"cmd {fmt(a['command_acc'])} %  p50 {fmt(a['elapsed_ms']['p50'], 0)} ms  "
            f"RTF {fmt(a['rtf']['p50'], 3)}  (uk WER {fmt(s.get('uk', {}).get('wer'))}, "
            f"en WER {fmt(s.get('en', {}).get('wer'))})"
        )
    print(f"-> {csv_path}\n-> {', '.join(map(str, pngs))}")


if __name__ == "__main__":
    main()
