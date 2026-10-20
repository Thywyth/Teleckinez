"""Repositories: typed access to the tables, used by the recorder, web panel and benchmarks."""

import json
import sqlite3
from typing import Any, ClassVar


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


def _row(row: sqlite3.Row | None, json_fields: tuple[str, ...] = ()) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in json_fields:
        if out.get(key) is not None:
            out[key.removesuffix("_json")] = json.loads(out.pop(key))
    return out


class CommandRepository:
    JSON: ClassVar[tuple[str, ...]] = ("params_json", "desired_json")

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert(
        self,
        *,
        cmd_id: str,
        device_id: str,
        action: str,
        params: dict,
        desired: dict,
        status: str,
        t_created: float,
        session_id: str | None = None,
        group_id: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO commands (cmd_id, session_id, group_id, device_id, action,"
            " params_json, desired_json, status, t_created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cmd_id, session_id, group_id, device_id, action, _dumps(params), _dumps(desired),
             status, t_created),
        )  # fmt: skip

    def update_status(
        self,
        cmd_id: str,
        status: str,
        ts: float,
        attempts: int,
        error: str | None,
        *,
        published: bool = False,
        acked: bool = False,
        finished: bool = False,
    ) -> None:
        self.conn.execute(
            "UPDATE commands SET status = ?, attempts = ?, error = ?,"
            " t_published = CASE WHEN ? AND t_published IS NULL THEN ? ELSE t_published END,"
            " t_ack = CASE WHEN ? AND t_ack IS NULL THEN ? ELSE t_ack END,"
            " t_finished = CASE WHEN ? THEN ? ELSE t_finished END"
            " WHERE cmd_id = ?",
            (status, attempts, error, published, ts, acked, ts, finished, ts, cmd_id),
        )

    def get(self, cmd_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM commands WHERE cmd_id = ?", (cmd_id,)).fetchone()
        return _row(row, self.JSON)

    def recent(self, limit: int = 50, device_id: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM commands", []
        if device_id:
            sql += " WHERE device_id = ?"
            args.append(device_id)
        sql += " ORDER BY t_created DESC LIMIT ?"
        rows = self.conn.execute(sql, (*args, limit)).fetchall()
        return [_row(r, self.JSON) for r in rows]


class StateHistoryRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert(self, device_id: str, ts: float, state: dict, source: str) -> None:
        self.conn.execute(
            "INSERT INTO state_history (device_id, ts, state_json, source) VALUES (?, ?, ?, ?)",
            (device_id, ts, _dumps(state), source),
        )

    def for_device(self, device_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT device_id, ts, state_json, source FROM state_history"
            " WHERE device_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
            (device_id, limit),
        ).fetchall()
        return [_row(r, ("state_json",)) for r in rows]


class AvailabilityRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert(self, device_id: str, ts: float, status: str) -> None:
        self.conn.execute(
            "INSERT INTO availability_log (device_id, ts, status) VALUES (?, ?, ?)",
            (device_id, ts, status),
        )

    def for_device(self, device_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT device_id, ts, status FROM availability_log"
            " WHERE device_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
            (device_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


class SessionRepository:
    COLUMNS: ClassVar[set[str]] = {
        "trigger", "stt_engine", "transcript", "lang", "confidence", "intent_json", "response",
        "t_speech_end", "t_transcript", "t_intent", "t_response",
    }  # fmt: skip

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, session_id: str, started_at: float, **fields: Any) -> None:
        """Create the session if new, then set the given fields (unknown names are rejected)."""
        unknown = set(fields) - self.COLUMNS
        if unknown:
            raise ValueError(f"unknown session fields: {unknown}")
        if "intent_json" in fields and not isinstance(fields["intent_json"], str):
            fields["intent_json"] = _dumps(fields["intent_json"])
        self.conn.execute(
            "INSERT OR IGNORE INTO voice_sessions (id, started_at) VALUES (?, ?)",
            (session_id, started_at),
        )
        if fields:
            assignments = ", ".join(f"{k} = ?" for k in fields)  # names checked above
            self.conn.execute(
                f"UPDATE voice_sessions SET {assignments} WHERE id = ?",
                (*fields.values(), session_id),
            )

    def get(self, session_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM voice_sessions WHERE id = ?", (session_id,))
        return _row(row.fetchone(), ("intent_json",))

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM voice_sessions ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row(r, ("intent_json",)) for r in rows]


class Storage:
    """All repositories over one connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.commands = CommandRepository(conn)
        self.states = StateHistoryRepository(conn)
        self.availability = AvailabilityRepository(conn)
        self.sessions = SessionRepository(conn)

    def close(self) -> None:
        self.conn.close()
