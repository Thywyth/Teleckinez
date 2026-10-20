"""Dispatcher: sends commands and controls their execution (dispatcher.md §1, §3, §4).

Two kinds of inputs drive a command through the state machine:

* device events (ack, state, availability) are handled synchronously as they
  arrive and cause immediate transitions;
* time is handled by one supervisor task per command: ack timeout with
  retries (same cmd_id, exponential backoff) and the state timeout after
  ACCEPTED.

A command is COMPLETED only when the reported state converged with the
desired one; an ack alone means "accepted", not "done" (ADR-006).
"""

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from teleckinez.core.config import DispatcherSettings
from teleckinez.core.events import DeviceAvailabilityChanged, DeviceStateChanged, EventBus
from teleckinez.core.models import Availability, DeviceCommand
from teleckinez.core.models import CommandStatus as S
from teleckinez.devices.capabilities import CapabilityError
from teleckinez.devices.registry import DeviceRegistry
from teleckinez.devices.twin import TwinStore
from teleckinez.dispatch.tracker import CommandRecord, CommandTracker, new_cmd_id
from teleckinez.transport.messages import AckReport, DeviceMessage
from teleckinez.transport.mqtt_client import MqttNotConnected

log = logging.getLogger(__name__)

WAITING_FOR_ACK = (S.PUBLISHED,)
RUNNING = (S.PUBLISHED, S.RETRYING, S.ACCEPTED)


class CommandSender(Protocol):
    """What the dispatcher needs from the transport (DeviceGateway implements it)."""

    async def send(self, device_id: str, cmd_id: str, desired: dict, ttl_s: float) -> None: ...

    def is_connected(self) -> bool: ...


@dataclass
class CommandGroup:
    """All commands created from one user request ("вимкни все світло" -> N commands)."""

    group_id: str | None
    records: list[CommandRecord]
    _done: list[asyncio.Event] = field(default_factory=list, repr=False)

    @property
    def done(self) -> bool:
        return all(r.is_terminal for r in self.records)

    async def wait(self, wait_s: float | None = None) -> list[CommandRecord]:
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in self._done)), wait_s)
        return self.records

    @property
    def succeeded(self) -> list[CommandRecord]:
        return [r for r in self.records if r.status is S.COMPLETED]

    @property
    def failed(self) -> list[CommandRecord]:
        return [r for r in self.records if r.is_terminal and r.status is not S.COMPLETED]


class Dispatcher:
    def __init__(
        self,
        registry: DeviceRegistry,
        twins: TwinStore,
        tracker: CommandTracker,
        sender: CommandSender,
        bus: EventBus,
        settings: DispatcherSettings | None = None,
    ) -> None:
        self.registry = registry
        self.twins = twins
        self.tracker = tracker
        self.sender = sender
        self.bus = bus
        self.settings = settings or DispatcherSettings()
        self._changed: dict[str, asyncio.Event] = {}  # cmd_id -> "status changed"
        self._finished: dict[str, asyncio.Event] = {}  # cmd_id -> "terminal"
        self._tasks: set[asyncio.Task] = set()
        bus.subscribe(self._on_state, DeviceStateChanged)
        bus.subscribe(self._on_availability, DeviceAvailabilityChanged)

    # ------------------------------------------------------------------ public

    async def dispatch(
        self, commands: Iterable[DeviceCommand], session_id: str | None = None
    ) -> CommandGroup:
        """Create, validate and send commands; returns at once, use group.wait()."""
        commands = list(commands)
        group_id = new_cmd_id().replace("c-", "g-") if len(commands) > 1 else None
        group = CommandGroup(group_id, [])
        for command in commands:
            record, done = self._start(command, session_id, group_id)
            group.records.append(record)
            group._done.append(done)
        return group

    def on_device_message(self, message: DeviceMessage) -> None:
        """Gateway listener: acks drive ACCEPTED / REJECTED."""
        if isinstance(message, AckReport):
            self._on_ack(message)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ------------------------------------------------------------- creation

    def _start(
        self, command: DeviceCommand, session_id: str | None, group_id: str | None
    ) -> tuple[CommandRecord, asyncio.Event]:
        device = self.registry.get(command.device_id)
        twin = self.twins.get(command.device_id)
        desired, error = {}, None
        try:
            desired = device.type.desired_for(command.action, command.params, twin.reported)
        except CapabilityError as exc:
            error = exc.code

        record = self.tracker.create(
            command.device_id,
            command.action,
            command.params,
            desired=desired,
            session_id=session_id,
            group_id=group_id,
        )
        done = asyncio.Event()
        self._changed[record.cmd_id] = asyncio.Event()
        self._finished[record.cmd_id] = done

        if error is not None:
            self._move(record, S.REJECTED_LOCAL, error)
            return record, done
        if twin.availability is Availability.OFFLINE:
            self._move(record, S.DEVICE_OFFLINE, "device_offline")
            return record, done
        if not self.sender.is_connected():
            self._move(record, S.REJECTED_LOCAL, "broker_unavailable")
            return record, done

        self._supersede(record)
        record.already = device.type.converged(desired, twin.reported)
        record.state_timeout_s = device.type.state_timeout(
            desired, twin.reported, self.settings.state_timeout_s, self.settings.state_margin_s
        )
        self._update_desired(record.device_id)
        task = asyncio.create_task(self._supervise(record), name=f"cmd:{record.cmd_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return record, done

    def _supersede(self, new: CommandRecord) -> None:
        """The newest command for an attribute wins (dispatcher.md §1.4).

        An older running command is superseded when the new one wants a
        different value of a shared attribute - then the old desired state can
        never be reached. Same values (e.g. the implied on=True of "brightness
        40 %" and "blue") do not conflict, both commands stay verifiable.
        """
        dtype = self.registry.get(new.device_id).type
        for old in self.tracker.active(new.device_id):
            if old is new or old.status not in RUNNING:
                continue
            shared = set(old.desired) & set(new.desired)
            if any(not dtype.cap(k).equal(old.desired[k], new.desired[k]) for k in shared):
                self._move(old, S.SUPERSEDED, f"superseded_by:{new.cmd_id}")

    # ------------------------------------------------------------- supervision

    async def _supervise(self, record: CommandRecord) -> None:
        cfg = self.settings
        if record.status is not S.CREATED:
            return
        for attempt in range(1, cfg.max_attempts + 1):
            if attempt > 1:
                self._move(record, S.RETRYING)
                await self._wait_while(record, (S.RETRYING,), cfg.backoff(attempt))
                if record.status is not S.RETRYING:
                    break  # a late ack/state arrived during the pause, or offline/superseded
            self._move(record, S.PUBLISHED)
            try:
                await self.sender.send(
                    record.device_id, record.cmd_id, record.desired, cfg.cmd_ttl_s
                )
            except MqttNotConnected as exc:
                log.warning("%s: publish failed: %s", record.cmd_id, exc)
                record.error = "broker_unavailable"
            else:
                record.error = None
                await self._wait_while(record, WAITING_FOR_ACK, cfg.ack_timeout_s)
            if record.status is not S.PUBLISHED:
                break
        else:
            if record.status is S.PUBLISHED:
                self._move(record, S.TIMEOUT_ACK, record.error or "no_ack")
                return

        if record.status is S.ACCEPTED:
            await self._wait_while(record, (S.ACCEPTED,), record.state_timeout_s or 3.0)
            if record.status is S.ACCEPTED:
                self._move(record, S.TIMEOUT_STATE, "state_not_reached")

    async def _wait_while(
        self, record: CommandRecord, statuses: tuple[S, ...], wait_s: float
    ) -> None:
        """Sleep up to wait_s, waking early when the record leaves `statuses`."""
        changed = self._changed.get(record.cmd_id)
        if changed is None:  # already terminal
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while record.status in statuses:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(changed.wait(), remaining)

    # ----------------------------------------------------------- event inputs

    def _on_ack(self, ack: AckReport) -> None:
        record = self.tracker.get(ack.cmd_id)
        if record is None or record.is_terminal or record.device_id != ack.device_id:
            return  # unknown, late (after a final state) or forged ack
        if ack.status == "rejected":
            if record.status in (S.PUBLISHED, S.RETRYING):
                self._move(record, S.REJECTED, ack.error or "rejected")
            return
        if record.status in (S.PUBLISHED, S.RETRYING):
            self._move(record, S.ACCEPTED)
        self._check_converged(record)

    def _on_state(self, event: DeviceStateChanged) -> None:
        for record in self.tracker.active(event.device_id):
            self._check_converged(record)

    def _on_availability(self, event: DeviceAvailabilityChanged) -> None:
        if event.availability is not Availability.OFFLINE:
            return
        for record in self.tracker.active(event.device_id):
            if record.status in RUNNING:
                self._move(record, S.DEVICE_OFFLINE, "device_offline")

    def _check_converged(self, record: CommandRecord) -> None:
        if record.status not in RUNNING or not record.desired:
            return
        device = self.registry.get(record.device_id)
        if device.type.converged(record.desired, self.twins.get(record.device_id).reported):
            self._move(record, S.COMPLETED)

    # ----------------------------------------------------------------- helpers

    def _move(self, record: CommandRecord, new: S, error: str | None = None) -> None:
        self.tracker.transition(record, new, error)
        self._changed[record.cmd_id].set()
        if record.is_terminal:
            self._finished.pop(record.cmd_id).set()  # CommandGroup keeps its own reference
            self._changed.pop(record.cmd_id, None)
            self._update_desired(record.device_id)

    def _update_desired(self, device_id: str) -> None:
        """Twin desired = merge of all still running commands (None when idle)."""
        merged: dict = {}
        for record in self.tracker.active(device_id):
            if record.status in (S.CREATED, *RUNNING):
                merged.update(record.desired)
        self.twins.set_desired(device_id, merged or None)
