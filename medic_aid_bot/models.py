"""Data-access functions over the SQLite database."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from . import config
from .db import get_conn, transaction

EVENT_TYPES = {
    "dose_started",
    "stage_dispensed",
    "dose_completed",
    "fault",
    "manual_dispense",
    "schedule_updated",
    "patient_confirmed",
    "missed_dose",
}


# ---------- users ----------

def get_user_by_chat(chat_id: int) -> sqlite3.Row | None:
    cur = get_conn().execute(
        "SELECT * FROM users WHERE telegram_chat_id = ?;", (chat_id,)
    )
    return cur.fetchone()


def register_user(
    chat_id: int,
    role: str,
    display_name: str | None = None,
    linked_patient_id: int | None = None,
) -> int:
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO users (telegram_chat_id, role, display_name, linked_patient_id) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(telegram_chat_id) DO UPDATE SET "
            "  role = excluded.role, "
            "  display_name = COALESCE(excluded.display_name, users.display_name), "
            "  linked_patient_id = COALESCE(excluded.linked_patient_id, users.linked_patient_id) "
            "RETURNING id;",
            (chat_id, role, display_name, linked_patient_id),
        )
        return cur.fetchone()["id"]


def link_caregiver_to_patient(caregiver_chat_id: int, patient_id: int) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE users SET linked_patient_id = ? WHERE telegram_chat_id = ?;",
            (patient_id, caregiver_chat_id),
        )


def find_patient_by_identifier(identifier: str) -> sqlite3.Row | None:
    """Look up a patient row by chat id, username, or display name."""
    identifier = identifier.strip().lstrip("@")
    if identifier.lstrip("-").isdigit():
        cur = get_conn().execute(
            "SELECT * FROM users WHERE role='patient' AND telegram_chat_id = ?;",
            (int(identifier),),
        )
    else:
        cur = get_conn().execute(
            "SELECT * FROM users WHERE role='patient' AND display_name = ?;",
            (identifier,),
        )
    return cur.fetchone()


def list_patients() -> list[sqlite3.Row]:
    cur = get_conn().execute("SELECT * FROM users WHERE role='patient';")
    return cur.fetchall()


def list_caregivers_for_patient(patient_id: int) -> list[sqlite3.Row]:
    cur = get_conn().execute(
        "SELECT * FROM users WHERE role='caregiver' AND linked_patient_id = ?;",
        (patient_id,),
    )
    return cur.fetchall()


def all_caregivers() -> list[sqlite3.Row]:
    cur = get_conn().execute("SELECT * FROM users WHERE role='caregiver';")
    return cur.fetchall()


# ---------- schedules ----------

def get_schedules() -> list[sqlite3.Row]:
    cur = get_conn().execute(
        "SELECT * FROM schedules ORDER BY hour, minute;"
    )
    return cur.fetchall()


def get_schedule(slot: str) -> sqlite3.Row | None:
    cur = get_conn().execute("SELECT * FROM schedules WHERE slot = ?;", (slot,))
    return cur.fetchone()


def update_schedule(
    slot: str, hour: int, minute: int, p1: int, p2: int, p3: int
) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE schedules SET hour=?, minute=?, pills_stage1=?, pills_stage2=?, "
            "pills_stage3=?, updated_at=CURRENT_TIMESTAMP WHERE slot=?;",
            (hour, minute, p1, p2, p3, slot),
        )


def next_schedule(now: datetime | None = None) -> sqlite3.Row | None:
    """Return the next upcoming enabled schedule entry today, or earliest tomorrow."""
    now = now or datetime.now(config.TZ)
    rows = [r for r in get_schedules() if r["enabled"]]
    if not rows:
        return None
    today_minutes = now.hour * 60 + now.minute
    upcoming = [r for r in rows if r["hour"] * 60 + r["minute"] >= today_minutes]
    return upcoming[0] if upcoming else rows[0]


# ---------- events ----------

def log_event(
    event_type: str,
    *,
    stage: int | None = None,
    pills_target: int | None = None,
    pills_actual: int | None = None,
    fault_reason: str | None = None,
    payload: dict[str, Any] | None = None,
    timestamp: datetime | str | None = None,
) -> int:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown event_type: {event_type}")
    payload_json = json.dumps(payload) if payload is not None else None
    with transaction() as conn:
        if timestamp is None:
            cur = conn.execute(
                "INSERT INTO events (event_type, stage, pills_target, pills_actual, "
                "fault_reason, payload_json) VALUES (?, ?, ?, ?, ?, ?) RETURNING id;",
                (event_type, stage, pills_target, pills_actual, fault_reason, payload_json),
            )
        else:
            cur = conn.execute(
                "INSERT INTO events (event_type, stage, pills_target, pills_actual, "
                "fault_reason, payload_json, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id;",
                (event_type, stage, pills_target, pills_actual, fault_reason, payload_json, timestamp),
            )
        return cur.fetchone()["id"]


def recent_events(days: int = 7) -> list[sqlite3.Row]:
    since = datetime.utcnow() - timedelta(days=days)
    cur = get_conn().execute(
        "SELECT * FROM events WHERE timestamp >= ? ORDER BY timestamp ASC;",
        (since,),
    )
    return cur.fetchall()


def last_event(event_type: str | None = None) -> sqlite3.Row | None:
    if event_type:
        cur = get_conn().execute(
            "SELECT * FROM events WHERE event_type = ? ORDER BY timestamp DESC LIMIT 1;",
            (event_type,),
        )
    else:
        cur = get_conn().execute(
            "SELECT * FROM events ORDER BY timestamp DESC LIMIT 1;"
        )
    return cur.fetchone()


def events_in_range(start: datetime, end: datetime) -> list[sqlite3.Row]:
    cur = get_conn().execute(
        "SELECT * FROM events WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp ASC;",
        (start, end),
    )
    return cur.fetchall()


# ---------- command queue ----------

def queue_command(
    command: str, params: dict[str, Any] | None = None, issued_by_chat_id: int | None = None
) -> int:
    params_json = json.dumps(params or {})
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO command_queue (command, params_json, issued_by_chat_id) "
            "VALUES (?, ?, ?) RETURNING id;",
            (command, params_json, issued_by_chat_id),
        )
        return cur.fetchone()["id"]


def next_pending_command() -> sqlite3.Row | None:
    with transaction() as conn:
        cur = conn.execute(
            "SELECT * FROM command_queue WHERE status='pending' ORDER BY id ASC LIMIT 1;"
        )
        row = cur.fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE command_queue SET status='consumed', consumed_at=CURRENT_TIMESTAMP WHERE id=?;",
            (row["id"],),
        )
        return row


def update_command_status(command_id: int, status: str, detail: str | None = None) -> None:
    with transaction() as conn:
        existing = conn.execute(
            "SELECT params_json FROM command_queue WHERE id=?;", (command_id,)
        ).fetchone()
        if existing is None:
            return
        try:
            params = json.loads(existing["params_json"] or "{}")
        except json.JSONDecodeError:
            params = {}
        if detail is not None:
            params["_result_detail"] = detail
        conn.execute(
            "UPDATE command_queue SET status=?, params_json=? WHERE id=?;",
            (status, json.dumps(params), command_id),
        )


def pending_commands() -> list[sqlite3.Row]:
    cur = get_conn().execute(
        "SELECT * FROM command_queue WHERE status='pending' ORDER BY id ASC;"
    )
    return cur.fetchall()
