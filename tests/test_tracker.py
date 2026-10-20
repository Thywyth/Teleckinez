import itertools

import pytest

from teleckinez.core.events import CommandCreated, CommandFinished, CommandStatusChanged, EventBus
from teleckinez.core.models import CommandStatus as S
from teleckinez.dispatch.tracker import (
    TERMINAL,
    TRANSITIONS,
    CommandTracker,
    InvalidTransition,
    new_cmd_id,
)

# The diagram from dispatcher.md §3 plus the documented stage-1 refinements.
EXPECTED = {
    (S.CREATED, S.PUBLISHED),
    (S.CREATED, S.REJECTED_LOCAL),
    (S.CREATED, S.DEVICE_OFFLINE),
    (S.PUBLISHED, S.ACCEPTED),
    (S.PUBLISHED, S.REJECTED),
    (S.PUBLISHED, S.RETRYING),
    (S.PUBLISHED, S.TIMEOUT_ACK),
    (S.PUBLISHED, S.COMPLETED),
    (S.PUBLISHED, S.DEVICE_OFFLINE),
    (S.PUBLISHED, S.SUPERSEDED),
    (S.RETRYING, S.PUBLISHED),
    (S.RETRYING, S.ACCEPTED),
    (S.RETRYING, S.REJECTED),
    (S.RETRYING, S.COMPLETED),
    (S.RETRYING, S.TIMEOUT_ACK),
    (S.RETRYING, S.DEVICE_OFFLINE),
    (S.RETRYING, S.SUPERSEDED),
    (S.ACCEPTED, S.COMPLETED),
    (S.ACCEPTED, S.TIMEOUT_STATE),
    (S.ACCEPTED, S.DEVICE_OFFLINE),
    (S.ACCEPTED, S.SUPERSEDED),
}


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def tracker():
    bus = EventBus()
    events: list = []
    bus.subscribe(events.append)
    t = CommandTracker(bus, clock=Clock())
    t.events = events
    return t


def test_table_matches_specification() -> None:
    table = {(a, b) for a, targets in TRANSITIONS.items() for b in targets}
    assert table == EXPECTED


def test_terminal_states_match_model() -> None:
    assert {s for s in S if s.is_terminal} == TERMINAL
    assert {
        S.COMPLETED,
        S.REJECTED,
        S.REJECTED_LOCAL,
        S.TIMEOUT_ACK,
        S.TIMEOUT_STATE,
        S.DEVICE_OFFLINE,
        S.SUPERSEDED,
    } == TERMINAL


@pytest.mark.parametrize(("old", "new"), list(itertools.product(S, S)))
def test_every_transition_pair(tracker, old: S, new: S) -> None:
    record = tracker.create("light.k", "turn_on")
    record.status = old
    if (old, new) in EXPECTED:
        tracker.transition(record, new)
        assert record.status is new
    else:
        with pytest.raises(InvalidTransition):
            tracker.transition(record, new)
        assert record.status is old


def test_happy_path_events_and_timestamps(tracker) -> None:
    clock = tracker.clock
    record = tracker.create("light.k", "turn_on", desired={"on": True}, session_id="s1")
    clock.now += 0.01
    tracker.transition(record, S.PUBLISHED)
    clock.now += 0.05
    tracker.transition(record, S.ACCEPTED)
    clock.now += 0.10
    tracker.transition(record, S.COMPLETED)

    assert record.attempts == 1
    assert record.durations_ms() == {"publish": 10.0, "ack": 50.0, "total": 160.0}
    assert [s for s, _ in record.history] == [S.CREATED, S.PUBLISHED, S.ACCEPTED, S.COMPLETED]
    types = [type(e) for e in tracker.events]
    assert types == [
        CommandCreated,
        CommandStatusChanged,
        CommandStatusChanged,
        CommandStatusChanged,
        CommandFinished,
    ]
    assert all(e.session_id == "s1" for e in tracker.events)
    assert tracker.events[-1].status is S.COMPLETED
    assert tracker.active("light.k") == []


def test_retry_counts_attempts_and_keeps_first_publish_time(tracker) -> None:
    clock = tracker.clock
    record = tracker.create("light.k", "turn_on")
    tracker.transition(record, S.PUBLISHED)
    first = record.t_published
    for _ in range(2):
        clock.now += 1
        tracker.transition(record, S.RETRYING)
        tracker.transition(record, S.PUBLISHED)
    tracker.transition(record, S.TIMEOUT_ACK, error="no_ack")
    assert record.attempts == 3
    assert record.t_published == first
    assert record.error == "no_ack"
    assert record.is_terminal


def test_active_commands_per_device(tracker) -> None:
    a = tracker.create("light.k", "turn_on")
    b = tracker.create("light.k", "turn_off")
    tracker.create("tv.k", "turn_on")
    assert tracker.active("light.k") == [a, b]
    tracker.transition(a, S.PUBLISHED)
    tracker.transition(a, S.SUPERSEDED)
    assert tracker.active("light.k") == [b]
    assert tracker.get(a.cmd_id) is a


def test_cmd_ids_are_unique_ulids() -> None:
    ids = {new_cmd_id() for _ in range(1000)}
    assert len(ids) == 1000
    assert all(i.startswith("c-") and len(i) == 28 for i in ids)
