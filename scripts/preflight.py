"""Pre-flight check before a demo: is everything in place to run Teleckinez?

Usage:
    python scripts/preflight.py            # full check, about 5 s
    python scripts/preflight.py --no-audio # skip the microphone recording

Prints a checklist in Ukrainian: ✓ / ✗ (blocks the demo) / ! (optional part
missing) and what to do. Exit code 1 if something required is missing.
Nothing here goes to the internet: only localhost and local files.
"""

import argparse
import importlib.util
import json
import socket
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

OK, FAIL, WARN = "✓", "✗", "!"


@dataclass
class Check:
    mark: str
    title: str
    detail: str = ""
    fix: str = ""


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def check_python() -> Check:
    v = sys.version_info
    if v >= (3, 12):
        return Check(OK, "Python", f"{v.major}.{v.minor}.{v.micro}")
    return Check(FAIL, "Python", f"{v.major}.{v.minor}", "потрібен Python 3.12 або 3.13")


def check_packages() -> Check:
    required = ["aiomqtt", "fastapi", "uvicorn", "rapidfuzz", "yaml", "teleckinez", "simulator"]
    voice = ["sounddevice", "webrtcvad", "vosk", "piper", "faster_whisper", "openwakeword"]
    missing = [m for m in required if importlib.util.find_spec(m) is None]
    if missing:
        return Check(
            FAIL,
            "Пакети",
            "немає: " + ", ".join(missing),
            'активуйте .venv і виконайте: pip install -e ".[voice,broker]"',
        )
    missing = [m for m in voice if importlib.util.find_spec(m) is None]
    if missing:
        return Check(
            WARN,
            "Пакети голосу",
            "немає: " + ", ".join(missing),
            'pip install -e ".[voice]"  (без них працює лише --text / --no-voice)',
        )
    return Check(OK, "Пакети", "основні й голосові встановлено")


def check_models(settings) -> list[Check]:
    v = settings.voice
    need = {
        "Vosk uk (розпізнавання і «Телекінез»)": Path(v.vosk_model_uk) / "am" / "final.mdl",
        "Vosk en": Path(v.vosk_model_en) / "am" / "final.mdl",
        "Piper uk (голос відповідей)": Path(v.piper_voice_uk),
        "Piper en": Path(v.piper_voice_en),
    }
    optional = {
        "Whisper small (stt: whisper)": Path(v.whisper_model) / "model.bin",
        "openWakeWord (trigger: openwakeword)": Path(v.oww_model),
    }
    fix = "python scripts/download_models.py   (один раз, потрібен інтернет)"
    checks = []
    for title, path in need.items():
        present = (ROOT / path).exists()
        checks.append(
            Check(
                OK if present else FAIL, title, "" if present else str(path), "" if present else fix
            )
        )
    for title, path in optional.items():
        present = (ROOT / path).exists()
        checks.append(
            Check(
                OK if present else WARN, title, "" if present else str(path), "" if present else fix
            )
        )
    return checks


def check_broker(settings) -> Check:
    host, port = settings.mqtt.host, settings.mqtt.port
    if port_open(host, port):
        return Check(OK, "MQTT-брокер", f"{host}:{port} відповідає")
    return Check(
        FAIL,
        "MQTT-брокер",
        f"на {host}:{port} нікого немає",
        "запустіть один із: служба mosquitto (net start mosquitto, від адміністратора) | "
        "powershell -ExecutionPolicy Bypass -File scripts\\mosquitto.ps1 | "
        "python -m teleckinez.broker   — або все разом: scripts\\demo.ps1",
    )


def check_web_port(settings) -> Check:
    host, port = settings.web.host, settings.web.port
    if not port_open(host, port):
        return Check(OK, "Порт веб-панелі", f"{port} вільний")
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/devices", timeout=1) as r:
            if r.status == 200:
                return Check(OK, "Порт веб-панелі", f"{port}: асистент уже працює")
    except Exception:
        pass
    return Check(
        FAIL,
        "Порт веб-панелі",
        f"{port} зайнятий іншою програмою",
        "закрийте її або змініть web.port у config/settings.yaml",
    )


def check_audio(record: bool) -> list[Check]:
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError:
        return [
            Check(
                WARN,
                "Мікрофон / динамік",
                "sounddevice не встановлено",
                'pip install -e ".[voice]"',
            )
        ]
    checks = []
    try:
        mic = sd.query_devices(kind="input")
        detail = mic["name"]
        if record:
            data = sd.rec(int(0.5 * 16000), samplerate=16000, channels=1, dtype="int16")
            sd.wait()
            peak = int(np.abs(data).max())
            detail += f", пік {peak}"
            if peak == 0:
                checks.append(
                    Check(
                        FAIL,
                        "Мікрофон",
                        detail + " (сама тиша)",
                        "перевірте, що мікрофон не вимкнено; Windows → Конфіденційність → "
                        "Мікрофон; python scripts/audio_check.py",
                    )
                )
                detail = ""
        if detail:
            checks.append(Check(OK, "Мікрофон", detail))
    except Exception as exc:
        checks.append(
            Check(
                FAIL,
                "Мікрофон",
                str(exc),
                "підключіть мікрофон або запускайте: python -m teleckinez --text (план Б)",
            )
        )
    try:
        checks.append(Check(OK, "Динамік", sd.query_devices(kind="output")["name"]))
    except Exception as exc:
        checks.append(Check(FAIL, "Динамік", str(exc), "підключіть колонки / навушники"))
    return checks


def check_gpu() -> Check:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Check(OK, "GPU (для Whisper)", out.stdout.strip().splitlines()[0])
    except (OSError, subprocess.TimeoutExpired):
        pass
    return Check(
        WARN,
        "GPU (для Whisper)",
        "NVIDIA не знайдено",
        "Whisper працюватиме на CPU (~2 с); Vosk GPU не потрібен",
    )


def check_ollama(settings) -> Check:
    host, model = settings.nlu.ollama_host.rstrip("/"), settings.nlu.llm_model
    needed = settings.nlu.mode != "rule"
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=1) as r:
            names = [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        mark = FAIL if needed else WARN
        return Check(
            mark,
            "Ollama (локальна LLM)",
            "не запущена" + ("" if needed else ", але nlu.mode = rule — не потрібна"),
            "ollama serve  (лише для nlu.mode llm / hybrid)",
        )
    if any(n.startswith(model) for n in names):
        return Check(OK, "Ollama (локальна LLM)", f"модель {model} є")
    return Check(
        FAIL if needed else WARN,
        "Ollama (локальна LLM)",
        f"немає моделі {model}",
        f"ollama pull {model}",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Teleckinez pre-flight check")
    ap.add_argument("--no-audio", action="store_true", help="do not record from the microphone")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    from teleckinez.core.config import load_settings

    settings = load_settings()
    checks = [check_python(), check_packages(), *check_models(settings), check_broker(settings)]
    checks += [
        check_web_port(settings),
        *check_audio(not args.no_audio),
        check_gpu(),
        check_ollama(settings),
    ]

    print("Teleckinez — перевірка перед запуском\n")
    for c in checks:
        print(f" {c.mark} {c.title}" + (f": {c.detail}" if c.detail else ""))
        if c.fix:
            print(f"     → {c.fix}")
    failed = [c for c in checks if c.mark == FAIL]
    warned = [c for c in checks if c.mark == WARN]
    print()
    if failed:
        print(f"Не готово: {len(failed)} ✗. Виправте пункти зі стрілкою.")
        return 1
    print(
        "Готово до запуску."
        + (f" Необов'язкові попередження (!): {len(warned)}." if warned else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
