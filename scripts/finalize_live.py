"""After the live recordings: check the set, run E1 / E3 / E5 on it, update SUMMARY.md.

Usage (from the project root, .venv active):
    python scripts/finalize_live.py              # everything (~15-25 min)
    python scripts/finalize_live.py --check      # only check the recordings
    python scripts/finalize_live.py --skip e3    # without the E3 voice run
    python scripts/finalize_live.py --selftest   # dry run on the synthetic set,
                                                 # results and SUMMARY copy go to a temp folder

Inputs (docs/RECORDING.md):
    benchmarks/datasets/voice/manifest.csv       commands  (record_dataset.py --speaker X)
    benchmarks/datasets/voice/wake_manifest.csv  wake word (record_dataset.py --speaker X --wake)
    benchmarks/datasets/noise/*.wav              background audio, 10-15 min in total

E1 uses the commands, E5 the wake clips (positives) + the commands (speech
without the wake word) + the background audio, E3 the «Телекінез … команда»
clips played through the whole pipeline (starts a broker and the simulator
for that, like scripts/demo.py). The live numbers are written next to the
synthetic ones into the «Живі записи» section of benchmarks/results/SUMMARY.md;
at the end the script lists the [ЧЕКАЄ …] places in the report to rewrite.
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmarks"
DATASETS = BENCH / "datasets"
RESULTS = BENCH / "results"
SUMMARY = RESULTS / "SUMMARY.md"
LIVE_HEADING = "## Живі записи"
MARKER_FILES = [*sorted((ROOT / "docs" / "report").glob("*.md")), ROOT / "README.md", SUMMARY]

MIN_S, MAX_S = 0.4, 8.0  # shorter = an empty take, longer = Enter pressed late
SILENT_DBFS = -40.0  # peak below this: nothing was said or the wrong microphone
EDGE_MS = 40  # speech at the very edge of the file = the phrase was cut off


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
        if wf.getnchannels() > 1:
            data = data.reshape(-1, wf.getnchannels())[:, 0]
    return data, rate


def dbfs(x: np.ndarray) -> float:
    peak = float(np.max(np.abs(x.astype(np.int32)))) if x.size else 0.0
    return -120.0 if peak == 0 else 20 * np.log10(peak / 32768)


def clip_problems(path: Path) -> list[str]:
    """Problems of one take, in Ukrainian; empty list = fine."""
    if not path.is_file():
        return ["файлу немає"]
    audio, rate = read_wav(path)
    seconds = audio.size / rate
    problems = []
    if rate != 16_000:
        problems.append(f"частота {rate} Гц, потрібно 16000")
    if seconds < MIN_S:
        problems.append(f"надто короткий ({seconds:.1f} с)")
    if seconds > MAX_S:
        problems.append(f"надто довгий ({seconds:.1f} с)")
    peak = dbfs(audio)
    if peak < SILENT_DBFS:
        problems.append(f"майже тиша (пік {peak:.0f} dBFS)")
    elif peak > -0.1:
        problems.append("перевантаження (пік 0 dBFS)")
    edge = int(rate * EDGE_MS / 1000)
    if peak >= SILENT_DBFS and audio.size > 4 * edge:
        loud = 10 ** ((peak - 12) / 20) * 32768  # within 12 dB of the peak = speech
        if np.max(np.abs(audio[:edge].astype(np.int32))) > loud:
            problems.append("обрізаний початок")
        if np.max(np.abs(audio[-edge:].astype(np.int32))) > loud:
            problems.append("обрізаний кінець")
    return problems


def read_manifest(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def check_set(name: str, rows: list[dict[str, str]], report: Report, minimum: int) -> None:
    if not rows:
        report.errors.append(f"{name}: маніфест порожній або його немає")
        return
    speakers: dict[str, int] = {}
    seconds = 0.0
    bad = []
    for r in rows:
        speakers[r["speaker"]] = speakers.get(r["speaker"], 0) + 1
        path = DATASETS / r["file"]
        problems = clip_problems(path)
        if path.is_file():
            audio, rate = read_wav(path)
            seconds += audio.size / rate
        if problems:
            bad.append(f"    {r['file']}: {', '.join(problems)}")
    who = ", ".join(f"{s} — {n}" for s, n in sorted(speakers.items()))
    report.lines.append(
        f"{name}: {len(rows)} файлів, {seconds / 60:.1f} хв, дикторів {len(speakers)} ({who})"
    )
    if len(rows) < minimum:
        report.warnings.append(f"{name}: лише {len(rows)} файлів (бажано ≥ {minimum})")
    if len(speakers) < 2:
        report.warnings.append(f"{name}: один диктор — цифри не узагальнюються на інші голоси")
    if bad:
        report.warnings.append(f"{name}: проблемні записи ({len(bad)}):\n" + "\n".join(bad))


def check_noise(folder: Path, report: Report) -> float:
    files = sorted(folder.glob("*.wav")) if folder.is_dir() else []
    minutes = 0.0
    for path in files:
        try:
            audio, rate = read_wav(path)
        except (wave.Error, EOFError) as exc:
            report.warnings.append(f"фон: {path.name} не читається ({exc})")
            continue
        minutes += audio.size / rate / 60
        if rate != 16_000:
            report.warnings.append(f"фон: {path.name} — {rate} Гц, потрібно 16000 моно")
    report.lines.append(f"фонове аудіо: {len(files)} файлів, {minutes:.1f} хв")
    if minutes < 10:
        report.warnings.append(f"фонове аудіо: {minutes:.1f} хв (бажано 10–15 хв)")
    return minutes


def e5_manifest(wake: list[dict[str, str]], commands: list[dict[str, str]], out: Path) -> Path:
    """Live wake clips (positives) + live commands without the wake word (negatives)."""
    fields = ["file", "engine", "wake", "text", "command", "command_id", "intent", "speaker",
              "duration_s", "source"]  # fmt: skip
    rows = list(wake)
    rows += [
        {"file": r["file"], "engine": "", "wake": "0", "text": r["text"], "speaker": r["speaker"],
         "duration_s": r.get("duration_s", ""), "source": r.get("source", "real")}
        for r in commands
    ]  # fmt: skip
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return out


def bench(script: str, *args: str, env: dict[str, str]) -> None:
    cmd = [sys.executable, str(BENCH / script), *args]
    print("\n$ python benchmarks/" + script + " " + " ".join(args), flush=True)
    subprocess.run(cmd, cwd=ROOT, env=env, check=True)


def latest(results: Path, name: str) -> dict | None:
    files = sorted(results.glob(f"{name}_????-??-??.json"))
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.1f}".replace(".", ",")


def ms(x: float | None) -> str:
    return "—" if x is None else f"{x:.0f}"


def e1_table(real: dict, synth: dict | None) -> list[str]:
    names = {"vosk_grammar": "Vosk, граматика", "vosk_free": "Vosk, вільний",
             "whisper_gpu": "Whisper small, GPU", "whisper_cpu": "Whisper small, CPU"}  # fmt: skip
    out = [
        f"### E1 на живих записах ({real['clips']} кліпів, дикторів: {len(real['speakers'])})",
        "",
        "| Рушій | WER живі / синт., % | Команда живі / синт., % | uk / en команда (живі), % | p50 живі / синт., мс |",  # noqa: E501
        "|---|---|---|---|---|",
    ]
    for key, label in names.items():
        r = real["results"].get(key)
        if not r:
            continue
        s = (synth or {}).get("results", {}).get(key, {}).get("all", {})
        a = r["all"]
        uk = r.get("uk", {}).get("command_acc")
        en = r.get("en", {}).get("command_acc")
        out.append(
            f"| {label} | {pct(a['wer'])} / {pct(s.get('wer'))} | "
            f"{pct(a['command_acc'])} / {pct(s.get('command_acc'))} | {pct(uk)} / {pct(en)} | "
            f"{ms(a['elapsed_ms']['p50'])} / {ms(s.get('elapsed_ms', {}).get('p50'))} |"
        )
    return [*out, ""]


def e3_table(real: dict, synth: dict | None) -> list[str]:
    out = [
        "### E3 на живих записах (кінець фрази → звук відповіді)",
        "",
        "| Рушій | Виконано | STT p50, мс | Разом p50 / p95 живі, мс | Разом p50 синт., мс |",
        "|---|---|---|---|---|",
    ]
    for key, label in (("voice:vosk", "Vosk"), ("voice:whisper", "Whisper GPU")):
        r = real["results"].get(key)
        if not r:
            continue
        s = (synth or {}).get("results", {}).get(key, {}).get("speech_end_to_tts", {})
        total = r["speech_end_to_tts"]
        out.append(
            f"| {label} | {r['completed']}/{r['n']} | {ms(r['stt']['p50'])} | "
            f"{ms(total['p50'])} / {ms(total['p95'])} | {ms(s.get('p50'))} |"
        )
    return [*out, ""]


def e5_table(real: dict, synth: dict | None) -> list[str]:
    out = [
        "### E5 на живих записах",
        "",
        "| Детектор | FRR живі / синт., % | Хибні на мовленні, /год | Хибні на фоні | RTF |",
        "|---|---|---|---|---|",
    ]
    for key, label in (("vosk_kws", "Vosk KWS «Телекінез»"), ("openwakeword", "openWakeWord")):
        r = real["results"].get(key)
        if not r:
            continue
        s = (synth or {}).get("results", {}).get(key, {})
        noise = (
            f"{r['noise_false_accepts']} за {r['noise_hours'] * 60:.1f} хв "
            f"({pct(r['noise_fa_per_hour'])}/год)"
            if r.get("noise_false_accepts") is not None
            else "немає фонового аудіо"
        )
        frr = pct(r["frr_pct"]) if r.get("positives") else "—"
        out.append(
            f"| {label} | {frr} ({r.get('false_rejects', 0)}/{r.get('positives', 0)}) / "
            f"{pct(s.get('frr_pct'))} | {pct(r['speech_fa_per_hour'])} | {noise} | "
            f"{r['rtf']:.3f}".replace(".", ",")
            + " |"
        )
    return [*out, ""]


def update_summary(path: Path, sections: list[str], date: str) -> None:
    text = path.read_text(encoding="utf-8")
    head, found, _ = text.partition(LIVE_HEADING)
    if not found:
        head = text.rstrip() + "\n\n"
    body = [
        LIVE_HEADING,
        "",
        f"Прогін `scripts/finalize_live.py` {date}: живі голоси поряд із синтетичними",
        "(синтетичні — таблиці вище). Сирі дані — `*_real_*.csv/json/png`.",
        "",
        *sections,
    ]
    path.write_text(head + "\n".join(body).rstrip() + "\n", encoding="utf-8")


def list_markers(files: list[Path]) -> list[str]:
    out = []
    for path in files:
        if not path.is_file():
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "[ЧЕКАЄ" in line:
                out.append(f"  {path.relative_to(ROOT).as_posix()}:{i}: {line.strip()[:110]}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--check", action="store_true", help="only check the recordings")
    ap.add_argument("--skip", nargs="+", choices=("e1", "e3", "e5"), default=[])
    ap.add_argument(
        "--selftest", action="store_true", help="run on the synthetic set (temp output)"
    )
    ap.add_argument("--force", action="store_true", help="run even with too few recordings")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    env = dict(os.environ)
    results, summary, label = RESULTS, SUMMARY, "real"
    commands_path = DATASETS / "voice" / "manifest.csv"
    wake_path = DATASETS / "voice" / "wake_manifest.csv"
    noise_dir = DATASETS / "noise"
    limits: list[str] = []
    if args.selftest:
        tmp = Path(tempfile.mkdtemp(prefix="tkz_finalize_"))
        results, summary = tmp, tmp / "SUMMARY.md"
        shutil.copy(SUMMARY, summary)
        env["TKZ_RESULTS_DIR"] = str(tmp)
        commands_path = DATASETS / "voice_synth" / "manifest.csv"
        wake_path = DATASETS / "voice_synth" / "wake_manifest.csv"
        label = "selftest"
        limits = ["--limit", "40"]
        print(f"Самоперевірка на синтетичному наборі, вихід: {tmp}")

    commands = read_manifest(commands_path)
    wake = [r for r in read_manifest(wake_path) if r.get("wake") == "1"]
    if args.selftest:  # a small, mixed subset is enough to exercise every step
        commands = commands[::12]
        wake = wake[::40]

    report = Report()
    check_set("команди (E1)", commands, report, minimum=100)
    check_set("ключове слово (E5/E3)", wake, report, minimum=25)
    check_noise(noise_dir, report)
    print("\nПеревірка записів")
    for line in report.lines:
        print("  " + line)
    for w in report.warnings:
        print("  ! " + w)
    for e in report.errors:
        print("  ✗ " + e)
    if args.check:
        return 1 if report.errors else 0
    if report.errors and not args.force:
        print("\nЄ помилки (✗) — спершу запишіть набір (docs/RECORDING.md) або додайте --force.")
        return 1

    work = Path(tempfile.mkdtemp(prefix="tkz_live_"))
    sections: list[str] = []
    date = ""
    if "e1" not in args.skip and commands:
        manifest = work / "e1_manifest.csv"
        with manifest.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(commands[0]))
            writer.writeheader()
            writer.writerows(commands)
        name = f"e1_stt_{label}"
        bench("run_stt.py", "--manifest", str(manifest), "--label", label, "--name", name,
              "--cpu-limit", "60", *limits, env=env)  # fmt: skip
        real = latest(results, name)
        date = real["date"][:10]
        sections += e1_table(real, latest(RESULTS, "e1_stt_synth"))
    if "e5" not in args.skip and (wake or commands):
        manifest = e5_manifest(wake, commands, work / "e5_manifest.csv")
        name = f"e5_wakeword_{label}"
        bench("run_wakeword.py", "--manifest", str(manifest), "--label", label,
              "--noise", str(noise_dir), "--name", name, env=env)  # fmt: skip
        real = latest(results, name)
        date = date or real["date"][:10]
        sections += e5_table(real, latest(RESULTS, "e5_wakeword_synth"))
    if "e3" not in args.skip and any(r.get("command") for r in wake):
        sections += run_e3(wake_path, label, results, env, ["--voice-limit", "4"] if limits else [])
    if not sections:
        print("\nНічого не виміряно.")
        return 1

    update_summary(summary, sections, date or "—")
    print(f"\nОновлено: {summary.relative_to(ROOT) if summary.is_relative_to(ROOT) else summary}")
    print("\nМісця з [ЧЕКАЄ …], які тепер треба переписати живими цифрами:")
    print("\n".join(list_markers(MARKER_FILES)) or "  (немає)")
    print("\nДалі: python scripts/build_report.py — перезібрати записку.")
    return 0


def run_e3(
    wake_path: Path, label: str, results: Path, env: dict[str, str], extra: list[str]
) -> list[str]:
    """E3 needs a broker and the simulator: start what is missing, stop it afterwards."""
    sys.path.insert(0, str(ROOT / "scripts"))
    sys.path.insert(0, str(ROOT / "src"))
    import asyncio

    from demo import Broker, kill_tree, native_devices, spawn, wait_devices

    from teleckinez.core.config import load_devices, load_settings

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    settings = load_settings()
    broker = Broker(settings.mqtt.host, settings.mqtt.port)
    simulator = None
    try:
        broker.start()
        simulator = spawn([sys.executable, "-m", "simulator", "--seed", "42"], "simulator.log")
        expected = native_devices(load_devices())
        asyncio.run(wait_devices(settings.mqtt.host, settings.mqtt.port, settings.mqtt.prefix,
                                 expected, 20))  # fmt: skip
        name = f"e3_latency_{label}"
        bench("run_latency.py", "--modes", "voice", "--manifest", str(wake_path), "--label", label,
              "--broker", broker.kind or "mosquitto", "--name", name, *extra, env=env)  # fmt: skip
    finally:
        if simulator:
            kill_tree(simulator)
        broker.stop()
    real = latest(results, f"e3_latency_{label}")
    return e3_table(real, latest(RESULTS, "e3_latency")) if real else []


if __name__ == "__main__":
    raise SystemExit(main())
