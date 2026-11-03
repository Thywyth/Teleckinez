"""Record the backup demo video of the web panel (no voice).

Output: presentation/demo.mp4 + docs/img/demo.gif.

    pip install playwright imageio-ffmpeg
    python -m playwright install ffmpeg      # Playwright's own encoder for the video
    python presentation/record_demo.py

Starts the demo stack itself (scripts/demo.py --no-voice: broker, simulator,
assistant), drives the web panel in Chrome with text commands - uk/en
commands, curtains, a group command, state queries, then an offline lock and
stuck curtains - and records the browser. The answers the assistant gave are
printed at the end (they go to docs/DEMO.md).
"""

import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "presentation"
URL = "http://127.0.0.1:8000"
SIZE = {"width": 1440, "height": 900}

# (pause after, text command) - pauses leave time to see the result on screen
SCRIPT = [
    (2.5, "увімкни світло на кухні"),
    (6.5, "відкрий штори у вітальні"),
    (2.5, "turn on the bathroom light"),
    (3.5, "вимкни все світло"),
    (3.5, "чи все світло вимкнено?"),
    (3.5, "what is the temperature in the bedroom"),
]
FAULTS = [("Замок вхідних дверей", "offline"), ("Штори у вітальні", "stuck")]
AFTER_FAULTS = [(3.5, "відчини вхідні двері"), (9.5, "закрий штори у вітальні")]
ANSWERS: list[tuple[str, str]] = []


def wait_http(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise SystemExit(f"{url} did not answer in {timeout} s")


def say(page, text: str, pause: float) -> None:
    box = page.locator("#text-input")
    box.click()
    box.type(text, delay=45)
    with page.expect_response("**/api/text-command", timeout=20_000) as answer:
        page.keyboard.press("Enter")
    ANSWERS.append((text, answer.value.json().get("response", "")))
    page.wait_for_timeout(int(pause * 1000))


def tab(page, view: str) -> None:
    page.locator(f'button.tab[data-view="{view}"]').click()
    page.wait_for_timeout(900)


def run(page) -> None:
    page.goto(URL)
    page.wait_for_selector(".card", timeout=15_000)
    page.wait_for_timeout(2000)
    for pause, text in SCRIPT:
        say(page, text, pause)
    tab(page, "faults")
    for title, fault in FAULTS:
        row = page.locator("tr", has_text=title).first
        row.locator("select").select_option(fault)
        page.wait_for_timeout(500)
        row.locator("button", has_text="Застосувати").click()
        page.wait_for_timeout(1200)
    page.wait_for_timeout(1000)
    tab(page, "dashboard")
    for pause, text in AFTER_FAULTS:
        say(page, text, pause)
    tab(page, "journal")
    page.wait_for_timeout(5000)
    tab(page, "faults")
    page.locator("button", has_text="Скинути всі").click()
    page.wait_for_timeout(2500)


def convert(webm: Path) -> tuple[Path, Path]:
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    mp4 = OUT_DIR / "demo.mp4"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(webm), "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-crf", "23", "-movflags", "+faststart", str(mp4)],
                   check=True)  # fmt: skip
    gif = ROOT / "docs" / "img" / "demo.gif"
    palette = webm.with_suffix(".png")
    vf = "fps=5,scale=800:-1:flags=lanczos"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(webm), "-vf",
                    f"{vf},palettegen=max_colors=64", str(palette)], check=True)  # fmt: skip
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(webm), "-i", str(palette),
                    "-lavfi", f"{vf}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5", str(gif)],
                   check=True)  # fmt: skip
    return mp4, gif


def main() -> int:
    from playwright.sync_api import sync_playwright

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    stack = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "demo.py"), "--no-voice",
                              "--no-browser"], cwd=ROOT)  # fmt: skip
    tmp = OUT_DIR / "_video"
    try:
        wait_http(f"{URL}/api/status", 90)
        time.sleep(3)  # retained states settle
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")
            context = browser.new_context(viewport=SIZE, record_video_dir=str(tmp),
                                          record_video_size=SIZE, color_scheme="light")  # fmt: skip
            page = context.new_page()
            started = time.monotonic()
            run(page)
            seconds = time.monotonic() - started
            video = page.video.path()
            context.close()
            browser.close()
        with urllib.request.urlopen(f"{URL}/api/commands?limit=30") as r:
            commands = json.load(r)
    finally:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(stack.pid)], capture_output=True)
    mp4, gif = convert(Path(video))
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"{seconds:.0f} s -> {mp4} ({mp4.stat().st_size / 1e6:.1f} MB), "
          f"{gif} ({gif.stat().st_size / 1e6:.1f} MB)")  # fmt: skip
    for text, answer in ANSWERS:
        print(f"{text!r:45} -> {answer}")
    for c in commands[:4]:
        print(c["device_id"], c["status"], c["attempts"], c["error"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
