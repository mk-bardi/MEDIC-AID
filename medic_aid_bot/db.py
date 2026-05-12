"""SQLite connection helper and schema initialisation."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from . import config

_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_chat_id INTEGER UNIQUE NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('patient', 'caregiver')),
    display_name TEXT,
    linked_patient_id INTEGER,
    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (linked_patient_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot TEXT NOT NULL UNIQUE CHECK (slot IN ('morning', 'afternoon', 'night')),
    hour INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
    minute INTEGER NOT NULL CHECK (minute BETWEEN 0 AND 59),
    pills_stage1 INTEGER NOT NULL DEFAULT 0,
    pills_stage2 INTEGER NOT NULL DEFAULT 0,
    pills_stage3 INTEGER NOT NULL DEFAULT 0,
    enabled BOOLEAN DEFAULT 1,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    stage INTEGER,
    pills_target INTEGER,
    pills_actual INTEGER,
    fault_reason TEXT,
    payload_json TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

CREATE TABLE IF NOT EXISTS command_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,
    params_json TEXT,
    issued_by_chat_id INTEGER,
    issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    consumed_at TIMESTAMP,
    status TEXT DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_command_queue_status ON command_queue(status);
"""

SEED_SCHEDULES = [
    ("morning", 8, 0, 1, 2, 1),
    ("afternoon", 14, 0, 1, 0, 1),
    ("night", 20, 0, 2, 1, 0),
]


def connect(path: str | None = None) -> sqlite3.Connection:
    """Return a new SQLite connection with row factory configured."""
    conn = sqlite3.connect(
        path or config.DATABASE_PATH,
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


_CONN: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    """Return the process-wide connection, opening it lazily."""
    global _CONN
    if _CONN is None:
        with _LOCK:
            if _CONN is None:
                _CONN = connect()
                init_schema(_CONN)
    return _CONN


def set_conn(conn: sqlite3.Connection) -> None:
    """Override the process-wide connection (used by tests)."""
    global _CONN
    _CONN = conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables and seed default schedules if empty."""
    conn.executescript(SCHEMA)
    cur = conn.execute("SELECT COUNT(*) AS c FROM schedules;")
    if cur.fetchone()["c"] == 0:
        conn.executemany(
            "INSERT INTO schedules (slot, hour, minute, pills_stage1, pills_stage2, pills_stage3) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            SEED_SCHEDULES,
        )


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Serialise writes with the module-level lock."""
    conn = get_conn()
    with _LOCK:
        try:
            conn.execute("BEGIN;")
            yield conn
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
