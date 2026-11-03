"""Start the whole demo with one command: broker, simulator, assistant, web panel.

Usage (from the project root, .venv active; or use scripts\\demo.ps1):
    python scripts/demo.py               # voice + web panel
    python scripts/demo.py --text        # no microphone: commands from the keyboard
    python scripts/demo.py --no-voice    # web panel only (text box in the panel)
    python scripts/demo.py --no-browser  # do not open the browser

Broker: an already running one on 1883 is used as is; otherwise Docker
(Mosquitto) -> native Mosquitto -> embedded amqtt, whichever works first.
Ctrl+C stops everything this script started (a broker that was already
running, e.g. the mosquitto Windows service, is left alone).
Logs of the broker and the simulator: data/logs/.
"""

import argparse
import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "data" / "logs"
MOSQUITTO = (
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "mosquitto" / "mosquitto.exe"
)


def say(text: str) -> None:
    print(f"[demo] {text}", flush=True)


def port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def wait_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_open(host, port):
            return True
        time.sleep(0.3)
    return False


def spawn(args: list[str], log_name: str | None) -> subprocess.Popen:
    out = None
    if log_name:
        LOGS.mkdir(parents=True, exist_ok=True)
        out = open(LOGS / log_name, "w", encoding="utf-8")  # noqa: SIM115 - lives as long as the process
    return subprocess.Popen(args, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT if out else None)


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":  # a venv python.exe is a launcher: its child is the real process
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()


class Broker:
    """Find or start an MQTT broker; remember what to stop."""

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self.proc: subprocess.Popen | None = None
        self.docker = False
        self.kind = ""

    def start(self) -> None:
        if port_open(self.host, self.port):
            self.kind = "вже запущений (напр. служба mosquitto)"
            return
        if self._docker():
            return
        if MOSQUITTO.is_file():
            (ROOT / "data" / "mosquitto").mkdir(parents=True, exist_ok=True)
            conf = ROOT / "config" / "mosquitto" / "mosquitto-windows.conf"
            self.proc = spawn([str(MOSQUITTO), "-c", str(conf)], "broker.log")
            if wait_port(self.host, self.port, 10):
                self.kind = "нативний Mosquitto"
                return
            kill_tree(self.proc)
        self.proc = spawn(
            [sys.executable, "-m", "teleckinez.broker", "--port", str(self.port)], "broker.log"
        )
        if not wait_port(self.host, self.port, 15):
            raise SystemExit("[demo] брокер не запустився, див. data/logs/broker.log")
        self.kind = "вбудований amqtt"

    def _docker(self) -> bool:
        if not shutil.which("docker"):
            return False
        try:
            info = subprocess.run(["docker", "info"], capture_output=True, timeout=8)
            if info.returncode != 0:
                return False
            up = subprocess.run(
                ["docker", "compose", "up", "-d", "mosquitto"],
                cwd=ROOT,
                capture_output=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return False
        if up.returncode == 0 and wait_port(self.host, self.port, 15):
            self.docker, self.kind = True, "Mosquitto у Docker"
            return True
        return False

    def stop(self) -> None:
        if self.proc:
            kill_tree(self.proc)
        if self.docker:
            subprocess.run(
                ["docker", "compose", "stop", "mosquitto"], cwd=ROOT, capture_output=True
            )


def native_devices(devices) -> int:
    """Devices that report availability on the native topic (Tasmota/Z2M use their own)."""
    return sum(d.protocol == "native" for d in devices.devices)


async def wait_devices(host: str, port: int, prefix: str, expected: int, limit_s: float) -> int:
    """Count native devices that announced availability=online (retained)."""
    import aiomqtt

    online: set[str] = set()
    try:
        async with asyncio.timeout(limit_s), aiomqtt.Client(host, port) as client:
            await client.subscribe(f"{prefix}/dev/+/availability")
            async for message in client.messages:
                device = str(message.topic).split("/")[-2]
                if message.payload == b"online":
                    online.add(device)
                else:
                    online.discard(device)
                if len(online) >= expected:
                    break
    except TimeoutError:
        pass
    return len(online)


def wait_http(url: str, proc: subprocess.Popen, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Teleckinez: one-command demo")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--text", action="store_true", help="commands from the keyboard, no microphone"
    )
    mode.add_argument("--no-voice", action="store_true", help="web panel only")
    ap.add_argument("--no-browser", action="store_true", help="do not open the web panel")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.platform == "win32":
        import ctypes

        # a parent (IDE, tool runner) may have started us with Ctrl+C ignored; it is inherited
        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    sys.path.insert(0, str(ROOT / "src"))
    from teleckinez.core.config import load_devices, load_settings

    settings = load_settings()
    devices = load_devices()
    expected = native_devices(devices)
    host, port = settings.mqtt.host, settings.mqtt.port
    url = f"http://{settings.web.host}:{settings.web.port}"
    if port_open(settings.web.host, settings.web.port):
        say(
            f"порт {settings.web.port} зайнятий: асистент уже запущений? Закрийте його й повторіть."
        )
        return 1

    broker = Broker(host, port)
    procs: list[subprocess.Popen] = []
    try:
        broker.start()
        say(f"брокер: {broker.kind}, {host}:{port}")

        procs.append(spawn([sys.executable, "-m", "simulator"], "simulator.log"))
        n = asyncio.run(wait_devices(host, port, settings.mqtt.prefix, expected, 20))
        others = len(devices.devices) - expected
        say(f"симулятор: на зв'язку {n} з {expected} пристроїв (+{others} Tasmota/Zigbee2MQTT)")

        extra = ["--text"] if args.text else ["--no-voice"] if args.no_voice else []
        say("запускаю асистента" + (" " + extra[0] if extra else " (голос + веб-панель)") + "…")
        assistant = spawn([sys.executable, "-m", "teleckinez", *extra], None)
        procs.append(assistant)
        if not wait_http(f"{url}/api/status", assistant, 90):
            say("асистент не відповів за 90 с — дивіться повідомлення вище")
            return 1
        say(f"веб-панель: {url}   (Ctrl+C — зупинити все)")
        if not args.no_browser:
            webbrowser.open(url)
        while assistant.poll() is None:  # Popen.wait() ignores Ctrl+C on Windows, sleep does not
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        say("зупиняю…")
        for proc in reversed(procs):
            kill_tree(proc)
        broker.stop()
        say("усе зупинено")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
