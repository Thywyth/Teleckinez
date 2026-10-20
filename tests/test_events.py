import asyncio

import pytest

from teleckinez.core.events import (
    CommandFinished,
    Event,
    EventBus,
    SpeakRequested,
    TranscriptReady,
)
from teleckinez.core.models import CommandStatus


async def test_sync_handler_runs_inline_and_filters_by_type() -> None:
    bus = EventBus()
    got: list[Event] = []
    bus.subscribe(got.append, TranscriptReady)
    bus.publish(TranscriptReady(text="hi"))
    bus.publish(SpeakRequested(text="ignored"))
    assert [e.text for e in got] == ["hi"]


async def test_base_class_subscription_receives_all() -> None:
    bus = EventBus()
    got: list[str] = []
    bus.subscribe(lambda e: got.append(e.type))
    bus.publish(TranscriptReady(text="a"))
    bus.publish(SpeakRequested(text="b"))
    assert got == ["TranscriptReady", "SpeakRequested"]


async def test_async_handler_keeps_order_and_does_not_block_publisher() -> None:
    bus = EventBus()
    got: list[str] = []

    async def slow(event: TranscriptReady) -> None:
        await asyncio.sleep(0.01)
        got.append(event.text)

    bus.subscribe(slow, TranscriptReady)
    for text in "abc":
        bus.publish(TranscriptReady(text=text))
    assert got == []  # publish returned immediately
    await bus.drain()
    assert got == ["a", "b", "c"]
    await bus.close()


async def test_failing_handler_is_isolated(caplog) -> None:
    bus = EventBus()
    got: list[str] = []

    def bad(event: Event) -> None:
        raise RuntimeError("boom")

    bus.subscribe(bad)
    bus.subscribe(lambda e: got.append(e.text), TranscriptReady)
    bus.publish(TranscriptReady(text="ok"))
    assert got == ["ok"]
    assert "boom" in caplog.text


async def test_wait_for_with_predicate_and_timeout() -> None:
    bus = EventBus()

    async def later() -> None:
        await asyncio.sleep(0)
        bus.publish(CommandFinished(cmd_id="1", device_id="x", status=CommandStatus.REJECTED))
        bus.publish(CommandFinished(cmd_id="2", device_id="x", status=CommandStatus.COMPLETED))

    task = asyncio.create_task(later())
    event = await bus.wait_for(CommandFinished, lambda e: e.status.is_success, wait_s=1)
    await task
    assert event.cmd_id == "2"
    with pytest.raises(TimeoutError):
        await bus.wait_for(SpeakRequested, wait_s=0.01)


async def test_stream_yields_published_events() -> None:
    bus = EventBus()
    stream = bus.stream(SpeakRequested)
    first = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    bus.publish(SpeakRequested(text="привіт", session_id="s1"))
    event = await asyncio.wait_for(first, 1)
    assert (event.text, event.session_id) == ("привіт", "s1")
    await stream.aclose()


def test_events_are_immutable_and_timestamped() -> None:
    event = TranscriptReady(text="x")
    assert event.ts > 0
    with pytest.raises(AttributeError):
        event.text = "y"  # type: ignore[misc]


def test_terminal_statuses() -> None:
    assert CommandStatus.COMPLETED.is_terminal
    assert CommandStatus.SUPERSEDED.is_terminal
    assert not CommandStatus.ACCEPTED.is_terminal
    assert not CommandStatus.RETRYING.is_terminal
