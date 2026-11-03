"""Record the voice command set for E1/E5 (docs/specs/metrics.md §1).

Shows a phrase, records it as WAV 16 kHz mono into
benchmarks/datasets/voice/<speaker>/ and appends a row to
benchmarks/datasets/voice/manifest.csv (file, text, language, speaker,
expected intent). Phrases are the ones marked `voice: true` in
benchmarks/datasets/nlu_commands.yaml (≈95 uk + ≈45 en).

Already recorded phrases of the speaker are skipped, so the script can be
stopped (q) and started again later. A bad take can be redone right away (r)
or later with --redo.

Usage:
    python scripts/record_dataset.py --speaker roman              # all phrases
    python scripts/record_dataset.py --speaker roman --lang en    # English only
    python scripts/record_dataset.py --speaker roman --redo uk_017 uk_020
    python scripts/record_dataset.py --speaker roman --list       # progress only
    python scripts/record_dataset.py --speaker anna --input 1     # another microphone

Keys while recording: Enter - start, Enter - stop; then
Enter = save and next, r = record again, p = play back, s = skip, q = quit.
"""

import argparse
import csv
import datetime as dt
import json
import re
import sys
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATASETS = ROOT / "benchmarks" / "datasets"
PHRASES = DATASETS / "nlu_commands.yaml"
VOICE_DIR = DATASETS / "voice"
SAMPLE_RATE = 16_000
FIELDS = [
    "file", "phrase_id", "text", "lang", "speaker", "cat", "intent", "source", "duration_s",
    "recorded_at",
]  # fmt: skip


@dataclass(frozen=True)
class Phrase:
    phrase_id: str  # uk_017: stable while nlu_commands.yaml keeps its order
    text: str
    lang: str
    cat: str
    expect: dict


def load_phrases(path: Path = PHRASES, lang: str = "all") -> list[Phrase]:
    items = yaml.safe_load(path.read_text(encoding="utf-8"))
    counters: dict[str, int] = {}
    phrases = []
    for item in items:
        if not item.get("voice"):
            continue
        n = counters[item["lang"]] = counters.get(item["lang"], 0) + 1
        phrases.append(
            Phrase(
                f"{item['lang']}_{n:03d}", item["text"], item["lang"], item["cat"], item["expect"]
            )
        )
    return [p for p in phrases if lang in ("all", p.lang)]


def safe_name(speaker: str) -> str:
    name = re.sub(r"[^\w-]+", "_", speaker.strip().lower(), flags=re.UNICODE).strip("_")
    if not name:
        raise ValueError("speaker name is empty")
    return name


def read_manifest(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_manifest(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: (r["speaker"], r["phrase_id"]))
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)  # never a half-written manifest, even on Ctrl+C


def upsert(rows: list[dict[str, str]], row: dict[str, str]) -> list[dict[str, str]]:
    """Replace the row of the same speaker + phrase (a redone take) or append it."""
    key = (row["speaker"], row["phrase_id"])
    return [r for r in rows if (r["speaker"], r["phrase_id"]) != key] + [row]


def done_ids(rows: list[dict[str, str]], speaker: str, voice_dir: Path) -> set[str]:
    """Phrases of this speaker whose WAV file really exists."""
    return {
        r["phrase_id"]
        for r in rows
        if r["speaker"] == speaker and (voice_dir.parent / r["file"]).exists()
    }


def save_wav(path: Path, pcm: bytes, rate: int = SAMPLE_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)


def manifest_row(phrase: Phrase, speaker: str, wav: Path, pcm: bytes, source: str) -> dict:
    return {
        "file": wav.relative_to(DATASETS).as_posix() if wav.is_relative_to(DATASETS) else str(wav),
        "phrase_id": phrase.phrase_id,
        "text": phrase.text,
        "lang": phrase.lang,
        "speaker": speaker,
        "cat": phrase.cat,
        "intent": json.dumps(phrase.expect, ensure_ascii=False, sort_keys=True),
        "source": source,
        "duration_s": f"{len(pcm) / 2 / SAMPLE_RATE:.2f}",
        "recorded_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------- recording


class MicRecorder:
    """Enter starts, Enter stops: no fixed length, no clipped endings."""

    def __init__(self, device: int | None = None) -> None:
        import sounddevice as sd

        self.sd = sd
        self.device = device

    def record(self) -> bytes:
        chunks: list[bytes] = []

        def callback(indata, frames, time_info, status) -> None:
            chunks.append(bytes(indata))

        input("   [Enter] - почати запис ")
        with self.sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            device=self.device,
            callback=callback,
        ):
            input("   ЗАПИС... говоріть, потім [Enter] ")
        return b"".join(chunks)

    def play(self, pcm: bytes) -> None:
        import numpy as np

        self.sd.play(np.frombuffer(pcm, dtype=np.int16), SAMPLE_RATE)
        self.sd.wait()


def level_dbfs(pcm: bytes) -> float:
    import numpy as np

    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    if x.size == 0:
        return -120.0
    peak = float(np.max(np.abs(x)))
    return -120.0 if peak == 0 else 20 * np.log10(peak / 32768)


def session(
    phrases: list[Phrase],
    speaker: str,
    manifest: Path,
    record: Callable[[], bytes],
    play: Callable[[bytes], None],
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> int:
    """Record the phrases one by one; returns how many takes were saved."""
    voice_dir = manifest.parent
    saved = 0
    for i, phrase in enumerate(phrases, 1):
        out(f"\n[{i}/{len(phrases)}] {phrase.phrase_id} ({phrase.lang})")
        out(f"   >>> {phrase.text} <<<")
        while True:
            pcm = record()
            seconds = len(pcm) / 2 / SAMPLE_RATE
            peak = level_dbfs(pcm)
            warn = "  (тихо!)" if peak < -35 else "  (перевантаження!)" if peak > -0.5 else ""
            out(f"   {seconds:.1f} с, пік {peak:.0f} dBFS{warn}")
            choice = ask("   [Enter] зберегти · r ще раз · p прослухати · s пропустити · q вийти ")
            choice = choice.strip().lower()
            if choice == "p":
                play(pcm)
                choice = ask("   [Enter] зберегти · r ще раз · s пропустити · q вийти ")
                choice = choice.strip().lower()
            if choice == "r":
                continue
            if choice in ("s", "q"):
                break
            wav = voice_dir / speaker / f"{phrase.phrase_id}.wav"
            save_wav(wav, pcm)
            rows = upsert(read_manifest(manifest), manifest_row(phrase, speaker, wav, pcm, "real"))
            write_manifest(manifest, rows)
            saved += 1
            break
        if choice == "q":
            break
    return saved


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--speaker", required=True, help="who is speaking (folder name), e.g. roman")
    ap.add_argument("--lang", choices=("uk", "en", "all"), default="all")
    ap.add_argument("--input", type=int, default=None, help="microphone index (audio_check --list)")
    ap.add_argument("--redo", nargs="+", metavar="ID", help="record these phrase ids again")
    ap.add_argument("--list", action="store_true", help="show progress and exit")
    ap.add_argument("--manifest", type=Path, default=VOICE_DIR / "manifest.csv")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    speaker = safe_name(args.speaker)
    phrases = load_phrases(lang=args.lang)
    done = done_ids(read_manifest(args.manifest), speaker, args.manifest.parent)
    if args.redo:
        todo = [p for p in phrases if p.phrase_id in set(args.redo)]
    else:
        todo = [p for p in phrases if p.phrase_id not in done]
    print(f"Диктор: {speaker} · записано {len(done & {p.phrase_id for p in phrases})}"
          f"/{len(phrases)} · залишилось {len(todo)}")  # fmt: skip
    if args.list or not todo:
        return 0
    mic = MicRecorder(args.input)
    saved = session(todo, speaker, args.manifest, mic.record, mic.play)
    print(f"\nЗбережено {saved}. Маніфест: {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
