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


class _SnapshotDict(dict):
    """Dict whose iteration walks a copy of the keys/items.

    amqtt 0.12.1 loops over ``Broker._subscriptions`` with ``await`` inside the
    loop body (retained-message replay on connect, broadcast). When another
    client subscribes meanwhile, the loop dies with "dictionary changed size
    during iteration" and that client's session is dropped. Iterating over a
    snapshot makes those loops safe without patching the library (ADR-013).
    """

    def __iter__(self):  # type: ignore[override]
        return iter(list(super().keys()))

    def keys(self):  # type: ignore[override]
        return list(super().keys())

    def values(self):  # type: ignore[override]
        return list(super().values())

    def items(self):  # type: ignore[override]
        return list(super().items())


def broker_config(host: str, port: int) -> dict[str, Any]:
    return {
        "listeners": {"default": {"type": "tcp", "bind": f"{host}:{port}"}},
        "plugins": {
            "amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True},
        },
    }


def _make_broker(config: dict[str, Any]) -> Any:
    from amqtt.broker import Broker

    class SafeBroker(Broker):
        """amqtt Broker with two fixes for 0.12.1 (see ADR-013)."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._subscriptions = _SnapshotDict(self._subscriptions)

        async def _publish_retained_messages_for_subscription(self, subscription, session) -> None:  # type: ignore[no-untyped-def]
            # On connect amqtt replays retained messages for *every* filter any
            # client has subscribed to; only the session's own filters are valid.
            subscribers = self._subscriptions.get(subscription[0], [])
            if any(s.client_id == session.client_id for s, _ in subscribers):
                await super()._publish_retained_messages_for_subscription(subscription, session)

    return SafeBroker(config)


async def run_broker(host: str, port: int, ready: asyncio.Event | None = None) -> None:
    """Run the broker until cancelled."""
    broker = _make_broker(broker_config(host, port))
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
