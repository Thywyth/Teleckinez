"""In-process event bus and event types (docs/ARCHITECTURE.md §5).

Modules do not call each other through long chains; they publish events and
subscribe to the ones they care about. Delivery rules:

* synchronous handlers run inline inside `publish()` (cheap state updates);
* async handlers get a private queue and worker task, so a slow subscriber
  (TTS, storage) never blocks the publisher and still sees events in order;
* a handler exception is logged and never reaches the publisher.

Subscribing to a base class (e.g. `Event`) receives all its subclasses.
"""

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from teleckinez.core.models import Availability, CommandStatus, Intent

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- events


@dataclass(frozen=True, kw_only=True)
class Event:
    ts: float = field(default_factory=time.time)
    session_id: str | None = None  # links the chain wake word -> ... -> response

    @property
    def type(self) -> str:
        return type(self).__name__


@dataclass(frozen=True, kw_only=True)
class WakeDetected(Event):
    engine: str
    score: float = 1.0


@dataclass(frozen=True, kw_only=True)
class ListeningStarted(Event):
    trigger: str  # wake | ptt | text


@dataclass(frozen=True, kw_only=True)
class SpeechEnded(Event):
    duration_s: float


@dataclass(frozen=True, kw_only=True)
class TranscriptReady(Event):
    text: str
    lang: str = "uk"
    confidence: float = 1.0
    engine: str = "text"  # vosk | whisper | text
    trigger: str = "text"  # wake | ptt | text


@dataclass(frozen=True, kw_only=True)
class IntentParsed(Event):
    intent: Intent


@dataclass(frozen=True, kw_only=True)
class IntentFailed(Event):
    text: str
    reason: str  # not_understood | low_confidence | unknown_device | ambiguous_target ...
    lang: str = "uk"
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class CommandCreated(Event):
    cmd_id: str
    device_id: str
    action: str
    params: dict[str, Any]
    desired: dict[str, Any]
    group_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class CommandStatusChanged(Event):
    cmd_id: str
    device_id: str
    old: CommandStatus | None
    new: CommandStatus
    attempts: int = 0
    error: str | None = None


@dataclass(frozen=True, kw_only=True)
class CommandFinished(Event):
    cmd_id: str
    device_id: str
    status: CommandStatus
    error: str | None = None
    group_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class DeviceStateChanged(Event):
    device_id: str
    reported: dict[str, Any]
    changed: dict[str, Any] = field(default_factory=dict)  # only attributes that changed
    source: str = "report"  # report | drift (changed without our command)


@dataclass(frozen=True, kw_only=True)
class DeviceAvailabilityChanged(Event):
    device_id: str
    availability: Availability
    previous: Availability = Availability.UNKNOWN


@dataclass(frozen=True, kw_only=True)
class SpeakRequested(Event):
    text: str
    lang: str = "uk"
    final: bool = True  # False for progress messages ("Відкриваю...")


@dataclass(frozen=True, kw_only=True)
class SpeakFinished(Event):
    text: str


# ------------------------------------------------------------------------------ bus

Handler = Callable[[Any], None] | Callable[[Any], Awaitable[None]]


@dataclass(eq=False)
class Subscription:
    handler: Handler
    types: tuple[type[Event], ...]
    queue: asyncio.Queue | None = None
    worker: asyncio.Task | None = None

    def matches(self, event: Event) -> bool:
        return isinstance(event, self.types)


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Subscription] = []
        self._closed = False

    def subscribe(self, handler: Handler, *types: type[Event]) -> Subscription:
        """Call `handler(event)` for events of the given types (default: all)."""
        sub = Subscription(handler, types or (Event,))
        if inspect.iscoroutinefunction(handler):
            sub.queue = asyncio.Queue()
        self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)
        if sub.worker is not None:
            sub.worker.cancel()

    def publish(self, event: Event) -> None:
        if self._closed:
            return
        for sub in list(self._subs):
            if not sub.matches(event):
                continue
            if sub.queue is None:
                try:
                    sub.handler(event)
                except Exception:
                    log.exception("event handler %r failed on %s", sub.handler, event.type)
            else:
                sub.queue.put_nowait(event)
                if sub.worker is None or sub.worker.done():
                    sub.worker = asyncio.get_running_loop().create_task(self._work(sub))

    async def _work(self, sub: Subscription) -> None:
        assert sub.queue is not None
        while True:
            event = await sub.queue.get()
            try:
                await sub.handler(event)  # type: ignore[misc]
            except Exception:
                log.exception("event handler %r failed on %s", sub.handler, event.type)
            finally:
                sub.queue.task_done()

    async def drain(self) -> None:
        """Wait until async handlers have processed everything published so far."""
        for sub in list(self._subs):
            if sub.queue is not None:
                await sub.queue.join()

    async def stream(self, *types: type[Event]) -> AsyncIterator[Event]:
        """Iterate over events as they are published (WebSocket bridge, REPL, tests)."""
        queue: asyncio.Queue[Event] = asyncio.Queue()
        sub = self.subscribe(queue.put_nowait, *types)
        try:
            while True:
                yield await queue.get()
        finally:
            self.unsubscribe(sub)

    async def wait_for(
        self,
        event_type: type[Event],
        predicate: Callable[[Any], bool] = lambda e: True,
        wait_s: float | None = None,
    ) -> Any:
        """Return the first future event of `event_type` matching `predicate`."""
        future: asyncio.Future = asyncio.get_running_loop().create_future()

        def check(event: Event) -> None:
            if not future.done() and predicate(event):
                future.set_result(event)

        sub = self.subscribe(check, event_type)
        try:
            return await asyncio.wait_for(future, wait_s)
        finally:
            self.unsubscribe(sub)

    async def close(self) -> None:
        self._closed = True
        workers = [s.worker for s in self._subs if s.worker is not None]
        for worker in workers:
            worker.cancel()
        for worker in workers:
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        self._subs.clear()
