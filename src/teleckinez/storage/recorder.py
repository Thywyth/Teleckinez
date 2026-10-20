"""EventRecorder: writes bus events to SQLite (sessions, commands, states, availability)."""

import dataclasses
import logging

from teleckinez.core.events import (
    CommandCreated,
    CommandFinished,
    CommandStatusChanged,
    DeviceAvailabilityChanged,
    DeviceStateChanged,
    Event,
    EventBus,
    IntentFailed,
    IntentParsed,
    ListeningStarted,
    SpeakRequested,
    SpeechEnded,
    TranscriptReady,
)
from teleckinez.core.models import CommandStatus as S
from teleckinez.storage.repositories import Storage

log = logging.getLogger(__name__)


class EventRecorder:
    def __init__(self, storage: Storage, bus: EventBus) -> None:
        self.storage = storage
        bus.subscribe(self.handle)

    def handle(self, event: Event) -> None:
        s = self.storage
        match event:
            case CommandCreated():
                s.commands.insert(
                    cmd_id=event.cmd_id,
                    device_id=event.device_id,
                    action=event.action,
                    params=event.params,
                    desired=event.desired,
                    status=S.CREATED.value,
                    t_created=event.ts,
                    session_id=event.session_id,
                    group_id=event.group_id,
                )
            case CommandStatusChanged():
                s.commands.update_status(
                    event.cmd_id,
                    event.new.value,
                    event.ts,
                    event.attempts,
                    event.error,
                    published=event.new is S.PUBLISHED,
                    acked=event.new in (S.ACCEPTED, S.REJECTED),
                    finished=event.new.is_terminal,
                )
            case CommandFinished():
                pass  # t_finished is set with the terminal CommandStatusChanged
            case DeviceStateChanged():
                s.states.insert(event.device_id, event.ts, event.reported, event.source)
            case DeviceAvailabilityChanged():
                s.availability.insert(event.device_id, event.ts, event.availability.value)
            case _ if event.session_id:
                self._session(event)

    def _session(self, event: Event) -> None:
        sid, sessions = event.session_id, self.storage.sessions
        assert sid is not None
        match event:
            case ListeningStarted():
                sessions.upsert(sid, event.ts, trigger=event.trigger)
            case SpeechEnded():
                sessions.upsert(sid, event.ts, t_speech_end=event.ts)
            case TranscriptReady():
                sessions.upsert(
                    sid,
                    event.ts,
                    trigger=event.trigger,
                    stt_engine=event.engine,
                    transcript=event.text,
                    lang=event.lang,
                    confidence=event.confidence,
                    t_transcript=event.ts,
                )
            case IntentParsed():
                sessions.upsert(
                    sid, event.ts, intent_json=dataclasses.asdict(event.intent), t_intent=event.ts
                )
            case IntentFailed():
                sessions.upsert(
                    sid,
                    event.ts,
                    intent_json={"failed": event.reason, **event.details},
                    t_intent=event.ts,
                )
            case SpeakRequested() if event.final:
                sessions.upsert(sid, event.ts, response=event.text, t_response=event.ts)
