"""SQLite database: schema and connection (dispatcher.md §7, ADR-009)."""

import sqlite3
from pathlib import Path

from teleckinez.core.config import ROOT

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_sessions (
    id            TEXT PRIMARY KEY,
    started_at    REAL NOT NULL,
    trigger       TEXT,             -- wake | ptt | text
    stt_engine    TEXT,
    transcript    TEXT,
    lang          TEXT,
    confidence    REAL,
    intent_json   TEXT,
    response      TEXT,
    t_speech_end  REAL,
    t_transcript  REAL,
    t_intent      REAL,
    t_response    REAL
);

CREATE TABLE IF NOT EXISTS commands (
    cmd_id        TEXT PRIMARY KEY,
    session_id    TEXT,
    group_id      TEXT,
    device_id     TEXT NOT NULL,
    action        TEXT NOT NULL,
    params_json   TEXT NOT NULL,
    desired_json  TEXT NOT NULL,
    status        TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    t_created     REAL NOT NULL,
    t_published   REAL,
    t_ack         REAL,
    t_finished    REAL,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS commands_created ON commands (t_created);
CREATE INDEX IF NOT EXISTS commands_device ON commands (device_id, t_created);

CREATE TABLE IF NOT EXISTS state_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id     TEXT NOT NULL,
    ts            REAL NOT NULL,
    state_json    TEXT NOT NULL,
    source        TEXT NOT NULL     -- report | drift
);
CREATE INDEX IF NOT EXISTS state_device ON state_history (device_id, ts);

CREATE TABLE IF NOT EXISTS availability_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id     TEXT NOT NULL,
    ts            REAL NOT NULL,
    status        TEXT NOT NULL     -- online | stale | offline
);
CREATE INDEX IF NOT EXISTS availability_device ON availability_log (device_id, ts);
"""


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open (and create if needed) the database; relative paths are from the project root."""
    if str(path) != ":memory:":
        path = Path(path)
        if not path.is_absolute():
            path = ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit; explicit tx if needed
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{version} is newer than this code (v{SCHEMA_VERSION})"
        )
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
