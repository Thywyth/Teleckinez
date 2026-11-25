"""Assistant: the request pipeline transcript -> intent -> commands -> response.

Listens for TranscriptReady (from STT or text input - the same path) and
publishes IntentParsed / IntentFailed, dispatches commands, waits for their
outcome and publishes SpeakRequested. Each request runs in its own task, so
a slow gate does not block the next command.

It also handles the phrases that switch standby mode (no wake word,
ADR-014) and stays silent on phrases it did not understand in that mode.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable

from ulid import ULID

from teleckinez.core.config import DispatcherSettings
from teleckinez.core.events import (
    CommandStatusChanged,
    EventBus,
    IntentFailed,
    IntentParsed,
    SpeakRequested,
    StandbyChanged,
    TranscriptReady,
)
from teleckinez.core.models import CommandStatus as S
from teleckinez.dispatch.dispatcher import Dispatcher
from teleckinez.dispatch.response import ResponseComposer
from teleckinez.dispatch.tracker import CommandTracker
from teleckinez.nlu.normalizer import detect_lang
from teleckinez.nlu.resolver import TargetResolver
from teleckinez.nlu.rule_parser import IntentParser
from teleckinez.nlu.standby import standby_request

log = logging.getLogger(__name__)
COMMAND_WAIT_S = 120.0  # upper bound; commands finish on their own timeouts much earlier
# Parser failures that are not answered in standby mode: TV and talk in the room.
QUIET_IN_STANDBY = {"not_understood", "low_confidence"}


def new_session_id() -> str:
    return f"s-{ULID()}"


class Assistant:
    def __init__(
        self,
        bus: EventBus,
        parser: IntentParser,
        resolver: TargetResolver,
        dispatcher: Dispatcher,
        tracker: CommandTracker,
        composer: ResponseComposer,
        settings: DispatcherSettings | None = None,
        language: Callable[[], str] = lambda: "uk",
    ) -> None:
        self.bus = bus
        self.parser = parser
        self.resolver = resolver
        self.dispatcher = dispatcher
        self.tracker = tracker
        self.composer = composer
        self.settings = settings or DispatcherSettings()
        self._waiters: dict[str, asyncio.Future[str]] = {}
        self._progress_said: set[str] = set()
        self._session_lang: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()
        self.language = language  # for confirmations with no phrase behind them (panel, timeout)
        # VoicePipeline.set_standby once the voice pipeline runs (App.build_voice)
        self.standby_control: Callable[..., bool] | None = None
        bus.subscribe(self._on_transcript, TranscriptReady)
        bus.subscribe(self._on_status, CommandStatusChanged)
        bus.subscribe(self._on_standby, StandbyChanged)

    # ------------------------------------------------------------------ public

    async def ask(self, text: str, *, engine: str = "text", trigger: str = "text") -> str:
        """Submit text as a transcript and return the final response (REPL, tests)."""
        session_id = new_session_id()
        future = asyncio.get_running_loop().create_future()
        self._waiters[session_id] = future
        self.bus.publish(
            TranscriptReady(
                session_id=session_id,
                text=text,
                lang=detect_lang(text),
                engine=engine,
                trigger=trigger,
            )
        )
        try:
            return await future
        finally:
            self._waiters.pop(session_id, None)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ---------------------------------------------------------------- pipeline

    def _on_transcript(self, event: TranscriptReady) -> None:
        session_id = event.session_id or new_session_id()
        task = asyncio.create_task(
            self._handle(event.text, session_id, event.trigger), name=session_id
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, text: str, session_id: str, trigger: str = "text") -> None:
        try:
            await self._pipeline(text, session_id, trigger)
        except Exception:
            log.exception("request %s failed", session_id)
            lang = detect_lang(text)
            self._speak(session_id, self.composer.failure("not_understood", lang=lang), lang)
        finally:
            self._session_lang.pop(session_id, None)
            self._progress_said.discard(session_id)

    async def _pipeline(self, text: str, session_id: str, trigger: str = "text") -> None:
        lang = detect_lang(text)
        if (enable := standby_request(text)) is not None:
            self._switch_standby(enable, session_id, lang)
            return
        result = await self.parser.aparse(text)
        if not result.ok:
            reason = result.reason or "not_understood"
            self.bus.publish(
                IntentFailed(session_id=session_id, text=text, reason=reason, lang=lang)
            )
            if trigger == "standby" and reason in QUIET_IN_STANDBY:
                self._resolve(session_id, "")  # nobody asked for an answer
                return
            self._speak(session_id, self.composer.failure(reason, lang=lang), lang)
            return

        intent = result.intent
        assert intent is not None
        lang = intent.lang
        self._session_lang[session_id] = lang
        resolution = self.resolver.resolve(intent)
        if not resolution.ok:
            error = resolution.error or "unknown_device"
            self.bus.publish(
                IntentFailed(
                    session_id=session_id,
                    text=text,
                    reason=error,
                    lang=lang,
                    details=resolution.details,
                )
            )
            name = intent.device_name or intent.device_type
            self._speak(
                session_id, self.composer.failure(error, resolution.details, lang, name=name), lang
            )
            return

        self.bus.publish(IntentParsed(session_id=session_id, intent=intent))
        if resolution.kind == "query":
            details = resolution.details
            if details.get("expect"):
                text = self.composer.state_answer(
                    resolution.devices, details["expect"], lang, listing=details["listing"]
                )
            else:
                text = self.composer.query_result(
                    resolution.devices, lang, details.get("attribute")
                )
            self._speak(session_id, text, lang)
            return

        group = await self.dispatcher.dispatch(resolution.commands, session_id=session_id)
        records = await group.wait(COMMAND_WAIT_S)
        self._speak(session_id, self.composer.command_result(records, lang), lang)

    # ----------------------------------------------------------------- standby

    def _switch_standby(self, enable: bool, session_id: str, lang: str) -> None:
        """A voice/text phrase asked for standby mode; _on_standby confirms the change."""
        self._session_lang[session_id] = lang  # the confirmation answers in this language
        if self.standby_control is None:
            self._speak(session_id, self.composer.t(lang, "standby_unavailable"), lang)
        elif not self.standby_control(enable, "voice", session_id):
            key = "standby_already_on" if enable else "standby_already_off"
            self._speak(session_id, self.composer.t(lang, key), lang)

    def _on_standby(self, event: StandbyChanged) -> None:
        """Confirm every switch by voice, whoever made it (phrase, panel, timeout)."""
        lang = self._session_lang.get(event.session_id or "") or self.language()
        key = "standby_timeout" if event.reason == "timeout" else "standby_off"
        if event.enabled:
            key = "standby_on"
        session_id = event.session_id or new_session_id()
        self._speak(session_id, self.composer.t(lang, key), lang)

    # ----------------------------------------------------------------- helpers

    def _on_status(self, event: CommandStatusChanged) -> None:
        """Say "Відкриваю штори…" once per request when a long action is accepted."""
        sid = event.session_id
        if (
            not self.settings.progress_messages
            or event.new is not S.ACCEPTED
            or sid is None
            or sid in self._progress_said
        ):
            return
        record = self.tracker.get(event.cmd_id)
        if record is None or (record.state_timeout_s or 0) <= self.settings.progress_min_s:
            return
        self._progress_said.add(sid)
        lang = self._session_lang.get(sid, "uk")
        self.bus.publish(
            SpeakRequested(
                session_id=sid, text=self.composer.progress(record, lang), lang=lang, final=False
            )
        )

    def _speak(self, session_id: str, text: str, lang: str) -> None:
        self.bus.publish(SpeakRequested(session_id=session_id, text=text, lang=lang))
        self._resolve(session_id, text)

    def _resolve(self, session_id: str, text: str) -> None:
        future = self._waiters.get(session_id)
        if future is not None and not future.done():
            future.set_result(text)
