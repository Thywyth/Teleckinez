"""E3: where the time goes from the end of a phrase to the start of the answer (metrics.md §2).

Text mode: the phrase enters as TranscriptReady (like `--text`), the stages are
taken from the event bus: NLU -> publish -> ack -> state converged -> response
text -> Piper synthesis (the moment audio could start playing).

Voice mode: WAV clips «Телекінез … <pause> … команда» are played in real time
through the real VoicePipeline (wake word -> VAD -> STT -> the same chain).
The clips are Piper speech (make_synth_dataset.py), so the voice numbers are
labelled `synthetic`. The VAD needs `vad_end_silence_ms` of silence to decide
the phrase ended - that wait is a setting, reported separately.

Needs a broker and the simulator:
    python benchmarks/run_latency.py                       # text, N=50 + voice, vosk
    python benchmarks/run_latency.py --modes voice --stt whisper --voice-limit 10
"""

import argparse
import asyncio
import csv
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from common import (
    DATASETS,
    conditions,
    fmt,
    out_path,
    setup_plot,
    summary,
    write_csv,
    write_json,
)

from teleckinez.app import App
from teleckinez.core.aio import run
from teleckinez.core.config import load_devices, load_settings
from teleckinez.core.events import (
    CommandStatusChanged,
    IntentParsed,
    SpeakFinished,
    SpeakRequested,
    SpeechEnded,
    TranscriptReady,
    WakeDetected,
)
from teleckinez.core.models import CommandStatus as S
from teleckinez.voice.pipeline import EngineCache, VoicePipeline

# Devices that react at once: the chain without curtain / gate motion (ARCHITECTURE §8).
TEXT_PHRASES = [
    "увімкни світло на кухні", "вимкни світло на кухні", "увімкни телевізор",
    "вимкни телевізор", "увімкни кавоварку", "вимкни кавоварку", "увімкни світло в коридорі",
    "вимкни світло в коридорі", "turn on the bathroom light", "turn off the bathroom light",
]  # fmt: skip
INSTANT = {"light", "rgb_light", "socket", "tv", "fan", "boiler", "humidifier", "air_conditioner"}
STAGES = ("wake_to_listen", "stt", "nlu", "publish", "ack", "converge", "response", "tts")
STAGE_UK = {
    "stt": "STT", "nlu": "NLU", "publish": "публікація", "ack": "ack",
    "converge": "стан збігся", "response": "відповідь", "tts": "синтез TTS",
}  # fmt: skip


class Timeline:
    """First timestamp of each milestone per session."""

    def __init__(self, app: App) -> None:
        self.marks: dict[str, dict[str, float]] = defaultdict(dict)
        self.done: dict[str, asyncio.Event] = defaultdict(asyncio.Event)
        bus = app.bus
        for kind in (WakeDetected, SpeechEnded, TranscriptReady, IntentParsed, SpeakRequested,
                     SpeakFinished, CommandStatusChanged):  # fmt: skip
            bus.subscribe(self._on, kind)

    def mark(self, sid: str, name: str, ts: float) -> None:
        self.marks[sid].setdefault(name, ts)

    def _on(self, e) -> None:
        sid = e.session_id
        if sid is None:
            return
        if isinstance(e, CommandStatusChanged):
            self.mark(sid, str(e.new), e.ts)
        elif isinstance(e, SpeakRequested):
            if e.final:
                self.mark(sid, "SpeakRequested", e.ts)
        else:
            self.mark(sid, e.type, e.ts)
        if isinstance(e, SpeakFinished):
            self.done[sid].set()


def stages(m: dict[str, float]) -> dict[str, float | None]:
    def ms(a: str, b: str) -> float | None:
        return round((m[b] - m[a]) * 1000, 2) if a in m and b in m else None

    return {
        "stt": ms("SpeechEnded", "TranscriptReady"),
        "nlu": ms("TranscriptReady", "IntentParsed"),
        "publish": ms("IntentParsed", str(S.PUBLISHED)),
        "ack": ms(str(S.PUBLISHED), str(S.ACCEPTED)),
        "converge": ms(str(S.ACCEPTED), str(S.COMPLETED)),
        "response": ms(str(S.COMPLETED), "SpeakRequested"),
        "tts": ms("SpeakRequested", "tts_start"),
        "to_response": ms("TranscriptReady", "SpeakRequested"),
        "speech_end_to_tts": ms("SpeechEnded", "tts_start"),
        "transcript_to_tts": ms("TranscriptReady", "tts_start"),
    }


class TimedOutput:
    """AudioOutput that only notes when playback would start (no real sound)."""

    def __init__(self, timeline: Timeline) -> None:
        self.timeline = timeline
        self.session: str | None = None

    async def play(self, samples, rate: int) -> None:
        if self.session is not None and samples.size > rate * 0.3:  # not the 0.12 s beep
            self.timeline.mark(self.session, "tts_start", time.time())


async def text_mode(app: App, timeline: Timeline, n: int, tts) -> list[dict[str, Any]]:
    rows = []
    for k in range(n):
        text = TEXT_PHRASES[k % len(TEXT_PHRASES)]
        answer = await app.assistant.ask(text)
        sid = next(reversed(timeline.marks))  # the session just finished
        if tts is not None:  # what the voice pipeline does right after SpeakRequested
            lang = "en" if text.isascii() else "uk"
            await asyncio.to_thread(tts.synthesize, answer, lang)
            timeline.mark(sid, "tts_start", time.time())
        m = timeline.marks[sid]
        rows.append({"mode": "text", "engine": "", "text": text, "answer": answer,
                     "status": "completed" if str(S.COMPLETED) in m else "other",
                     **stages(m)})  # fmt: skip
        await asyncio.sleep(0.2)
    return rows


async def voice_mode(app: App, timeline: Timeline, clips, stt: str) -> list[dict[str, Any]]:
    from teleckinez.voice.audio_io import WavSource

    v = app.settings.voice
    v.trigger, v.stt, v.stt_lang, v.beeps = "vosk_kws", stt, "uk", True
    engines = EngineCache(app.registry)
    await asyncio.to_thread(engines.stt, v)
    await asyncio.to_thread(engines.wake, v)
    await asyncio.to_thread(engines.tts, v)
    rows = []
    for clip in clips:
        output = TimedOutput(timeline)
        source = WavSource(DATASETS / clip["file"], realtime=True, trailing_silence_s=2.0)
        pipeline = VoicePipeline(app.bus, v, source, output, engines)
        before = set(timeline.marks)
        pipeline.start()
        sid = None
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            new = [s for s in timeline.marks if s not in before]
            if new:
                sid = new[0]
                output.session = sid
                if timeline.done[sid].is_set():
                    break
            await asyncio.sleep(0.02)
        await pipeline.stop()
        m = timeline.marks.get(sid, {}) if sid else {}
        rows.append({"mode": "voice", "engine": stt, "text": clip["command"],
                     "status": "completed" if str(S.COMPLETED) in m else "other",
                     "heard": "", **stages(m)})  # fmt: skip
        await asyncio.sleep(0.3)
    return rows


def voice_clips(limit: int) -> list[dict[str, str]]:
    path = DATASETS / "voice_synth" / "wake_manifest.csv"
    with path.open(encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["engine"] == "vosk_kws" and r["command"]]
    devices = {d.id: d.type for d in load_devices().devices}
    out = []
    for r in rows:
        expect = json.loads(r["intent"])
        ids = expect.get("devices", [])
        if expect.get("kind") == "command" and ids and all(devices[i] in INSTANT for i in ids):
            out.append(r)
    return out[:limit] if limit else out


def plot(rows: list[dict[str, Any]], name: str) -> Path:
    plt = setup_plot()
    groups = {}
    for key in ("text", "voice:vosk", "voice:whisper"):
        mode, _, stt = key.partition(":")
        mine = [r for r in rows if r["mode"] == mode and (not stt or r["engine"] == stt)
                and r["status"] == "completed"]  # fmt: skip
        if mine:
            groups[key] = mine
    labels = {"text": "Текст", "voice:vosk": "Голос, Vosk", "voice:whisper": "Голос, Whisper"}
    stages_shown = [s for s in STAGES if s in STAGE_UK]
    fig, ax = plt.subplots(figsize=(9, 0.9 * len(groups) + 1.6))
    patterns = ["", "//", "..", "xx", "\\\\", "++", "oo"]
    greys = ["#222222", "#ffffff", "#bbbbbb", "#777777", "#dddddd", "#555555", "#999999"]
    for y, mine in enumerate(groups.values()):
        left = 0.0
        for i, st in enumerate(stages_shown):
            vals = [r[st] for r in mine if r.get(st) is not None]
            med = summary(vals)["p50"] if vals else 0
            ax.barh(y, med, left=left, color=greys[i], hatch=patterns[i], edgecolor="black",
                    label=STAGE_UK[st] if y == 0 else None)  # fmt: skip
            left += med or 0
        ax.text(left + 10, y, f"{left:.0f} мс", va="center", fontsize=9)
    ax.set_yticks(range(len(groups)))
    ax.set_yticklabels([labels[k] for k in groups])
    ax.set_xlabel("Медіана етапу, мс (від кінця фрази / тексту до початку звуку відповіді)")
    ax.legend(ncol=4, fontsize=8.5, loc="lower center", bbox_to_anchor=(0.5, 1.0))
    path = out_path(name, ".png")
    fig.savefig(path)
    plt.close(fig)
    return path


async def main_async(args) -> None:
    settings = load_settings()
    settings.notifications.enabled = False
    settings.dispatcher.progress_messages = False
    app = App(settings, load_devices(), use_storage=False)
    timeline = Timeline(app)
    rows: list[dict[str, Any]] = []
    await app.start(settle_s=2.0)
    try:
        if "text" in args.modes:
            from teleckinez.voice.tts.base import create_tts

            tts = await asyncio.to_thread(create_tts, settings.voice)
            print(f"E3 text: {args.n} requests", flush=True)
            rows += await text_mode(app, timeline, args.n, tts)
        if "voice" in args.modes:
            clips = voice_clips(args.voice_limit)
            for stt in args.stt:
                print(f"E3 voice ({stt}): {len(clips)} synthetic clips", flush=True)
                rows += await voice_mode(app, timeline, clips, stt)
    finally:
        await app.stop()
    stats: dict[str, Any] = {}
    for key in ("text", "voice:vosk", "voice:whisper"):
        mode, _, stt = key.partition(":")
        mine = [r for r in rows if r["mode"] == mode and (not stt or r["engine"] == stt)]
        if not mine:
            continue
        ok = [r for r in mine if r["status"] == "completed"]
        stats[key] = {"n": len(mine), "completed": len(ok)} | {
            col: summary([r[col] for r in ok if r.get(col) is not None])
            for col in (*STAGES[1:], "to_response", "transcript_to_tts", "speech_end_to_tts")
        }
    csv_path = write_csv(args.name, rows)
    png = plot(rows, args.name)
    write_json(args.name, conditions(
        experiment="E3 latency", voice_label="synthetic", broker=args.broker,
        vad_end_silence_ms=settings.voice.vad_end_silence_ms,
        simulator_latency_ms=settings.simulator.latency_ms, results=stats))  # fmt: skip
    for key, s in stats.items():
        print(f"{key:14s} {s['completed']}/{s['n']}  " + "  ".join(
            f"{st} {fmt(s[st]['p50'], 1)}" for st in (*STAGES[1:], "speech_end_to_tts")
            if s.get(st) and s[st]["n"]))  # fmt: skip
    print(f"-> {csv_path}\n-> {png}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--modes", nargs="+", choices=("text", "voice"), default=["text", "voice"])
    ap.add_argument("--n", type=int, default=50, help="text requests")
    ap.add_argument("--stt", nargs="+", choices=("vosk", "whisper"), default=["vosk", "whisper"])
    ap.add_argument("--voice-limit", type=int, default=30, help="voice clips per STT engine")
    ap.add_argument("--broker", default="mosquitto")
    ap.add_argument("--name", default="e3_latency")
    run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
