"""Text mode (--text): type commands, read the answers and watch execution control.

Typed text goes through exactly the same path as recognized speech
(TranscriptReady -> NLU -> dispatcher -> response), so the whole logic can
be checked without a microphone.
"""

import asyncio
import contextlib
import threading

from teleckinez.app import App
from teleckinez.core.events import CommandFinished, SpeakRequested
from teleckinez.core.models import Availability

HELP = """\
Команди / commands:
  будь-яка фраза            напр. «увімкни світло на кухні», «яка температура у спальні?»
  /devices  (/d)            стан усіх пристроїв
  /help     (/h)            ця довідка
  /exit, вихід, exit        вийти (також Ctrl+C)
"""
EXIT = {"/exit", "/quit", "exit", "quit", "вихід", "/q"}
MARK = {
    Availability.ONLINE: "online ",
    Availability.STALE: "STALE  ",
    Availability.OFFLINE: "OFFLINE",
    Availability.UNKNOWN: "unknown",
}


async def ainput(prompt: str) -> str:
    """input() in a daemon thread: Ctrl+C / exit never waits for a pending Enter."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def read() -> None:
        try:
            line = input(prompt)
        except BaseException as exc:  # EOFError, KeyboardInterrupt
            loop.call_soon_threadsafe(_set, future, exc)
        else:
            loop.call_soon_threadsafe(_set, future, line)

    threading.Thread(target=read, daemon=True, name="repl-input").start()
    return await future


def _set(future: asyncio.Future, value: object) -> None:
    if future.done():
        return
    if isinstance(value, BaseException):
        future.set_exception(EOFError() if isinstance(value, KeyboardInterrupt) else value)
    else:
        future.set_result(value)


def _format_finished(app: App, event: CommandFinished) -> str:
    record = app.tracker.get(event.cmd_id)
    path = " -> ".join(s.value for s, _ in record.history) if record else event.status.value
    d = record.durations_ms() if record else {}
    times = f"ack {d['ack']:.0f} ms, " if d.get("ack") is not None else ""
    total = f"{times}total {d['total']:.0f} ms" if d.get("total") is not None else ""
    extra = f", attempts {record.attempts}" if record and record.attempts > 1 else ""
    error = f", error {event.error}" if event.error else ""
    return f"   · {event.device_id}: {path} ({total}{extra}{error})"


def device_table(app: App) -> str:
    now = app.twins.clock()
    lines = []
    for room in app.registry.rooms:
        lines.append(f"[{app.registry.room_loc(room)}]")
        for dev in app.registry.find(room=room):
            twin = app.twins.get(dev.id)
            age = twin.age(now)
            age_s = f"{age:5.0f}s" if age is not None else "    - "
            state = ", ".join(f"{k}={v}" for k, v in twin.reported.items()) or "-"
            lines.append(f"  {MARK[twin.availability]} {age_s}  {dev.id:<26} {state}")
    return "\n".join(lines)


async def run_repl(app: App, *, show_commands: bool = True) -> None:
    online = sum(t.availability is Availability.ONLINE for t in app.twins)
    print(f"Teleckinez: текстовий режим. Пристроїв online: {online}/{len(app.registry)}.")
    if online == 0:
        print("  (симулятор не запущено? В іншому терміналі: python -m simulator)")
    print("  /help — довідка, /exit — вихід.\n")

    def on_speak(event: SpeakRequested) -> None:
        if event.kind == "notification":
            print(f"\n[сповіщення] {event.text}")
        elif not event.final:
            print(f"асистент> {event.text}")

    def on_finished(event: CommandFinished) -> None:
        if show_commands:
            print(_format_finished(app, event))

    app.bus.subscribe(on_speak, SpeakRequested)
    app.bus.subscribe(on_finished, CommandFinished)

    while True:
        try:
            text = (await ainput("ви> ")).strip()
        except EOFError:
            break
        if not text:
            continue
        if text.lower() in EXIT:
            break
        if text in ("/help", "/h"):
            print(HELP)
            continue
        if text in ("/devices", "/d"):
            print(device_table(app))
            continue
        answer = await app.assistant.ask(text)
        print(f"асистент> {answer}\n")


def _web_url(app: App) -> str:
    host = app.settings.web.host
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    return f"http://{shown}:{app.settings.web.port}"


async def main_text(app: App, show_commands: bool = True, web: bool = True) -> None:
    async with app:
        web_task = asyncio.create_task(app.serve_web()) if web else None
        if web_task:
            print(f"Веб-панель: {_web_url(app)}")
        try:
            with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
                await run_repl(app, show_commands=show_commands)
        finally:
            if web_task:
                web_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await web_task


async def main_web(app: App) -> None:
    """Assistant + web panel (the voice pipeline joins here in stage 2)."""
    async with app:
        online = sum(t.availability is Availability.ONLINE for t in app.twins)
        print(f"Teleckinez: пристроїв online {online}/{len(app.registry)}.")
        print(f"Веб-панель: {_web_url(app)}   (Ctrl+C — зупинити)")
        with contextlib.suppress(asyncio.CancelledError):
            await app.serve_web()
