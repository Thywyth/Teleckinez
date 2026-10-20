"""Teleckinez entry point: python -m teleckinez [--text]."""

import argparse
import contextlib
import logging
import sys

from teleckinez import __version__
from teleckinez.core import aio
from teleckinez.core.config import load_devices, load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m teleckinez", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--text", action="store_true", help="type commands instead of speaking")
    parser.add_argument("--settings", default=None, help="settings.yaml (default: config/)")
    parser.add_argument("--devices", default=None, help="devices.yaml (default: config/)")
    parser.add_argument("--no-db", action="store_true", help="do not write the SQLite log")
    parser.add_argument("--quiet", action="store_true", help="hide command lifecycle lines")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    # Ukrainian text must survive redirected/old consoles on Windows.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.text:
        print(
            "Голосовий конвеєр і веб-панель з'являться на етапах 2-3 (docs/ROADMAP.md).\n"
            "Зараз доступний текстовий режим:  python -m teleckinez --text"
        )
        return 2

    from teleckinez.app import App
    from teleckinez.repl import main_text
    from teleckinez.transport.mqtt_client import MqttNotConnected

    app = App(load_settings(args.settings), load_devices(args.devices), use_storage=not args.no_db)
    try:
        with contextlib.suppress(KeyboardInterrupt):
            aio.run(main_text(app, show_commands=not args.quiet))
    except MqttNotConnected as exc:
        print(f"Помилка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
