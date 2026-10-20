from pathlib import Path

import pytest

from teleckinez.core.events import (
    DeviceAvailabilityChanged,
    DeviceStateChanged,
    EventBus,
    IntentFailed,
    IntentParsed,
    SpeakRequested,
    TranscriptReady,
)
from teleckinez.core.models import Availability, Intent
from teleckinez.core.models import CommandStatus as S
from teleckinez.dispatch.tracker import CommandTracker
from teleckinez.storage import EventRecorder, Storage, connect
from teleckinez.storage.db import SCHEMA_VERSION


class Clock:
    now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def env():
    bus = EventBus()
    storage = Storage(connect(":memory:"))
    EventRecorder(storage, bus)
    clock = Clock()
    yield bus, storage, CommandTracker(bus, clock=clock), clock
    storage.close()


def test_command_lifecycle_is_logged_with_stage_times(env) -> None:
    _, storage, tracker, clock = env
    rec = tracker.create(
        "light.k", "turn_on", {}, desired={"on": True}, session_id="s1", group_id="g1"
    )
    clock.now = 100.02
    tracker.transition(rec, S.PUBLISHED)
    clock.now = 100.07
    tracker.transition(rec, S.ACCEPTED)
    clock.now = 100.2
    tracker.transition(rec, S.COMPLETED)

    row = storage.commands.get(rec.cmd_id)
    assert row["status"] == "completed" and row["attempts"] == 1
    assert row["desired"] == {"on": True} and row["params"] == {}
    assert (row["t_created"], row["t_published"], row["t_ack"], row["t_finished"]) == (
        100.0,
        100.02,
        100.07,
        100.2,
    )
    assert (row["session_id"], row["group_id"]) == ("s1", "g1")


def test_retries_keep_first_publish_time_and_error(env) -> None:
    _, storage, tracker, clock = env
    rec = tracker.create("light.k", "turn_on", {}, desired={"on": True})
    tracker.transition(rec, S.PUBLISHED)
    clock.now = 101
    tracker.transition(rec, S.RETRYING)
    tracker.transition(rec, S.PUBLISHED)
    tracker.transition(rec, S.TIMEOUT_ACK, error="no_ack")
    row = storage.commands.get(rec.cmd_id)
    assert row["t_published"] == 100.0 and row["attempts"] == 2
    assert row["error"] == "no_ack" and row["t_ack"] is None


def test_recent_commands_newest_first(env) -> None:
    _, storage, tracker, clock = env
    for i in range(3):
        clock.now = 100 + i
        tracker.create(f"light.{i}", "turn_on", {}, desired={"on": True})
    assert [r["device_id"] for r in storage.commands.recent(2)] == ["light.2", "light.1"]
    assert [r["device_id"] for r in storage.commands.recent(device_id="light.0")] == ["light.0"]


def test_state_and_availability_history(env) -> None:
    bus, storage, _, _ = env
    bus.publish(DeviceStateChanged(device_id="l", reported={"on": True}, ts=1.0))
    bus.publish(DeviceStateChanged(device_id="l", reported={"on": False}, ts=2.0, source="drift"))
    bus.publish(DeviceAvailabilityChanged(device_id="l", availability=Availability.OFFLINE, ts=3.0))
    history = storage.states.for_device("l")
    assert [(h["state"], h["source"]) for h in history] == [
        ({"on": False}, "drift"),
        ({"on": True}, "report"),
    ]
    assert storage.availability.for_device("l")[0]["status"] == "offline"


def test_voice_session_timeline(env) -> None:
    bus, storage, _, _ = env
    bus.publish(TranscriptReady(session_id="s1", text="увімкни світло", engine="vosk", ts=10.0))
    intent = Intent(kind="command", action="turn_on", device_type="light", room="kitchen")
    bus.publish(IntentParsed(session_id="s1", intent=intent, ts=10.01))
    bus.publish(SpeakRequested(session_id="s1", text="Відкриваю…", final=False, ts=10.1))
    bus.publish(SpeakRequested(session_id="s1", text="Світло увімкнено.", ts=10.5))
    session = storage.sessions.get("s1")
    assert session["transcript"] == "увімкни світло" and session["stt_engine"] == "vosk"
    assert session["intent"]["action"] == "turn_on"
    assert session["response"] == "Світло увімкнено."  # progress messages are not final
    assert (session["t_transcript"], session["t_intent"], session["t_response"]) == (
        10.0,
        10.01,
        10.5,
    )


def test_failed_intent_is_logged(env) -> None:
    bus, storage, _, _ = env
    bus.publish(TranscriptReady(session_id="s2", text="бла бла"))
    bus.publish(IntentFailed(session_id="s2", text="бла бла", reason="not_understood"))
    assert storage.sessions.get("s2")["intent"] == {"failed": "not_understood"}


def test_session_rejects_unknown_columns(env) -> None:
    _, storage, _, _ = env
    with pytest.raises(ValueError):
        storage.sessions.upsert("s", 1.0, **{"id; DROP TABLE commands": 1})


def test_file_database_is_created_and_versioned(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "t.db"
    conn = connect(path)
    assert path.is_file()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()
    connect(path).close()  # reopening an existing database is fine
