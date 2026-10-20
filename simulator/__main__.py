"""Run the device simulator: python -m simulator [--config config/devices.yaml]."""

import argparse
import asyncio
import contextlib
import logging

from simulator.home import SimHome
from teleckinez.core import aio
from teleckinez.core.config import load_devices, load_settings
from teleckinez.devices.registry import DeviceRegistry


async def _main(args: argparse.Namespace) -> None:
    settings = load_settings(args.settings)
    if args.host:
        settings.mqtt.host = args.host
    if args.port:
        settings.mqtt.port = args.port
    registry = DeviceRegistry(load_devices(args.config))
    only = set(args.only.split(",")) if args.only else None
    home = SimHome(registry, settings, only=only, seed=args.seed)
    print(
        f"simulator: starting {len(home.devices)} devices on "
        f"{settings.mqtt.host}:{settings.mqtt.port} (prefix {settings.mqtt.prefix!r})"
    )
    await home.start()
    print("simulator: all devices online. Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    finally:
        await home.stop()
        print("simulator: stopped, devices reported offline")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m simulator", description=__doc__)
    parser.add_argument(
        "--config", default=None, help="devices.yaml (default: config/devices.yaml)"
    )
    parser.add_argument("--settings", default=None, help="settings.yaml")
    parser.add_argument("--host", default=None, help="MQTT host (overrides settings)")
    parser.add_argument("--port", type=int, default=None, help="MQTT port (overrides settings)")
    parser.add_argument("--only", default=None, help="comma-separated device ids to simulate")
    parser.add_argument("--seed", type=int, default=None, help="random seed (reproducible runs)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    with contextlib.suppress(KeyboardInterrupt):
        aio.run(_main(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
