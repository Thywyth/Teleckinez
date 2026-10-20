"""Embedded MQTT broker (amqtt) for running without Docker/Mosquitto.

Usage: python -m teleckinez.broker [--host 127.0.0.1] [--port 1883]
Requires the `broker` extra: pip install -e ".[broker]"
Mosquitto (docker compose up -d mosquitto) remains the primary broker.
"""

import argparse
import asyncio
import contextlib
import logging
import sys
from typing import Any


def broker_config(host: str, port: int) -> dict[str, Any]:
    return {
        "listeners": {"default": {"type": "tcp", "bind": f"{host}:{port}"}},
        "plugins": {
            "amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True},
        },
    }


async def run_broker(host: str, port: int, ready: asyncio.Event | None = None) -> None:
    """Run the broker until cancelled."""
    from amqtt.broker import Broker

    broker = Broker(broker_config(host, port))
    await broker.start()
    if ready is not None:
        ready.set()
    try:
        await asyncio.Event().wait()
    finally:
        await broker.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m teleckinez.broker", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("amqtt").setLevel(logging.WARNING)  # per-packet INFO logs are too noisy
    print(f"amqtt broker listening on {args.host}:{args.port} (Ctrl+C to stop)")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_broker(args.host, args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
