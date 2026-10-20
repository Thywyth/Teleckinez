"""Command lifecycle as an explicit finite-state machine (dispatcher.md §3, ADR-006).

The transition table below is the single source of truth: any transition not
listed raises InvalidTransition. Every transition is timestamped and
published as CommandStatusChanged; reaching a terminal state also publishes
CommandFinished.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ulid import ULID

from teleckinez.core.events import (
    CommandCreated,
    CommandFinished,
    CommandStatusChanged,
    EventBus,
)
from teleckinez.core.models import CommandStatus as S

TRANSITIONS: dict[S, frozenset[S]] = {
    S.CREATED: frozenset({S.PUBLISHED, S.REJECTED_LOCAL, S.DEVICE_OFFLINE}),
    S.PUBLISHED: frozenset(
        {
            S.ACCEPTED,
            S.REJECTED,
            S.RETRYING,
            S.TIMEOUT_ACK,
            S.COMPLETED,  # state converged before the ack arrived
            S.DEVICE_OFFLINE,
            S.SUPERSEDED,
        }
    ),
    S.RETRYING: frozenset(
        {
            S.PUBLISHED,  # next attempt, same cmd_id
            S.ACCEPTED,  # late ack of the previous attempt
            S.REJECTED,
            S.COMPLETED,
            S.TIMEOUT_ACK,  # the last attempt could not even be published
            S.DEVICE_OFFLINE,
            S.SUPERSEDED,
        }
    ),
    S.ACCEPTED: frozenset({S.COMPLETED, S.TIMEOUT_STATE, S.DEVICE_OFFLINE, S.SUPERSEDED}),
}
TERMINAL = frozenset(s for s in S if s not in TRANSITIONS)


class InvalidTransition(RuntimeError):
    pass


def new_cmd_id() -> str:
    """ULID: unique and sortable by creation time (protocol.md §3)."""
    return f"c-{ULID()}"


@dataclass
class CommandRecord:
    cmd_id: str
    device_id: str
    action: str
    params: dict[str, Any]
    desired: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    group_id: str | None = None
    status: S = S.CREATED
    attempts: int = 0
    error: str | None = None
    already: bool = False  # the device was already in the desired state
    state_timeout_s: float | None = None  # expected execution time after ACCEPTED
    t_created: float = 0.0
    t_published: float | None = None  # first publish
    t_ack: float | None = None
    t_finished: float | None = None
    history: list[tuple[S, float]] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    def durations_ms(self) -> dict[str, float | None]:
        """Stage latencies for the command log and benchmarks."""

        def ms(a: float | None, b: float | None) -> float | None:
            return None if a is None or b is None else round((b - a) * 1000, 1)

        return {
            "publish": ms(self.t_created, self.t_published),
            "ack": ms(self.t_published, self.t_ack),
            "total": ms(self.t_created, self.t_finished),
        }


class CommandTracker:
    def __init__(self, bus: EventBus, clock: Callable[[], float] = time.time) -> None:
        self.bus = bus
        self.clock = clock
        self._records: dict[str, CommandRecord] = {}
        self._active: dict[str, list[CommandRecord]] = {}  # device_id -> records

    def create(
        self,
        device_id: str,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        desired: dict[str, Any] | None = None,
        session_id: str | None = None,
        group_id: str | None = None,
        cmd_id: str | None = None,
    ) -> CommandRecord:
        now = self.clock()
        record = CommandRecord(
            cmd_id=cmd_id or new_cmd_id(),
            device_id=device_id,
            action=action,
            params=dict(params or {}),
            desired=dict(desired or {}),
            session_id=session_id,
            group_id=group_id,
            t_created=now,
            history=[(S.CREATED, now)],
        )
        self._records[record.cmd_id] = record
        self._active.setdefault(device_id, []).append(record)
        self.bus.publish(
            CommandCreated(
                ts=now,
                session_id=session_id,
                cmd_id=record.cmd_id,
                device_id=device_id,
                action=action,
                params=record.params,
                desired=record.desired,
                group_id=group_id,
            )
        )
        return record

    @staticmethod
    def can(record: CommandRecord, new: S) -> bool:
        return new in TRANSITIONS.get(record.status, frozenset())

    def transition(self, record: CommandRecord, new: S, error: str | None = None) -> None:
        if not self.can(record, new):
            raise InvalidTransition(f"{record.cmd_id}: {record.status.value} -> {new.value}")
        now = self.clock()
        old = record.status
        record.status = new
        record.history.append((new, now))
        if error is not None:
            record.error = error
        if new is S.PUBLISHED:
            record.attempts += 1
            record.t_published = record.t_published or now
        elif new in (S.ACCEPTED, S.REJECTED) and record.t_ack is None:
            record.t_ack = now
        self.bus.publish(
            CommandStatusChanged(
                ts=now,
                session_id=record.session_id,
                cmd_id=record.cmd_id,
                device_id=record.device_id,
                old=old,
                new=new,
                attempts=record.attempts,
                error=record.error,
            )
        )
        if new in TERMINAL:
            record.t_finished = now
            active = self._active.get(record.device_id, [])
            if record in active:
                active.remove(record)
            self.bus.publish(
                CommandFinished(
                    ts=now,
                    session_id=record.session_id,
                    cmd_id=record.cmd_id,
                    device_id=record.device_id,
                    status=new,
                    error=record.error,
                    group_id=record.group_id,
                )
            )

    def get(self, cmd_id: str) -> CommandRecord | None:
        return self._records.get(cmd_id)

    def active(self, device_id: str) -> list[CommandRecord]:
        return list(self._active.get(device_id, []))

    def recent(self, limit: int = 50) -> list[CommandRecord]:
        return sorted(self._records.values(), key=lambda r: r.t_created, reverse=True)[:limit]
