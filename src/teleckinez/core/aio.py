"""asyncio helpers."""

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any


def run(main: Coroutine[Any, Any, Any]) -> Any:
    """asyncio.run with a SelectorEventLoop on Windows.

    aiomqtt (paho) needs loop.add_reader/add_writer, which the default
    ProactorEventLoop on Windows does not implement.
    """
    if sys.platform == "win32":
        return asyncio.run(main, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(main)
