"""Smoke tests covering the main user flows.

These exercise the data-access layer, the Flask device API, and the notification
fan-out via a stubbed Telegram application. No network access required.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from medic_aid_bot import config, models
from medic_aid_bot.api.routes import create_app
from medic_aid_bot.jobs.scheduled import check_missed_doses


# ----------------------------- helpers -----------------------------

def make_stub_app(send_log: list):
    """Return a stubbed Telegram Application whose bot.send_message records calls."""
    app = MagicMock()
    bot = MagicMock()

    async def _send(chat_id, text, **kwargs):
        send_log.append({"chat_id": chat_id, "text": text, **kwargs})

    bot.send_message = AsyncMock(side_effect=_send)
    app.bot = bot
    return app


def client_with_auth(telegram_app=None, loop=None):
    """Build a Flask test client with the device token preset."""
    app = create_app(telegram_app=telegram_app, loop=loop)
    return app.test_client()


def auth_headers():
    return {"X-Device-Token": config.DEVICE_TOKEN}


# ----------------------------- tests -------------------------------

def test_default_schedule_is_seeded():
    rows = models.get_schedules()
    slots = {r["slot"] for r in rows}
    assert slots == {"morning", "afternoon", "night"}
    morning = models.get_schedule("morning")
    assert (morning["hour"], morning["minute"]) == (8, 0)
    assert (morning["pills_stage1"], morning["pills_stage2"], morning["pills_stage3"]) == (1, 2, 1)


def test_register_patient_and_caregiver_linking():
    patient_id = models.register_user(chat_id=1001, role="patient", display_name="alice")
    cg_id = models.register_user(chat_id=2002, role="caregiver", display_name="bob")
    models.link_caregiver_to_patient(caregiver_chat_id=2002, patient_id=patient_id)

    found = models.find_patient_by_identifier("alice")
    assert found is not None and found["id"] == patient_id
    found_by_id = models.find_patient_by_identifier("1001")
    assert found_by_id["id"] == patient_id

    caregivers = models.list_caregivers_for_patient(patient_id)
    assert len(caregivers) == 1 and caregivers[0]["id"] == cg_id


def test_command_queue_lifecycle():
    cmd_id = models.queue_command(
        "dispense_stage", params={"stage": 1, "pills": 2}, issued_by_chat_id=99
    )
    pending = models.pending_commands()
    assert any(p["id"] == cmd_id for p in pending)

    fetched = models.next_pending_command()
    assert fetched is not None
    assert fetched["id"] == cmd_id
    assert json.loads(fetched["params_json"]) == {"stage": 1, "pills": 2}

    # consumed: next poll returns nothing
    assert models.next_pending_command() is None

    models.update_command_status(cmd_id, "executed", "all good")
    row = models.get_conn().execute(
        "SELECT status, params_json FROM command_queue WHERE id=?;", (cmd_id,)
    ).fetchone()
    assert row["status"] == "executed"
    assert json.loads(row["params_json"]).get("_result_detail") == "all good"


def test_poll_endpoint_requires_token():
    client = client_with_auth()
    resp = client.get("/api/poll")
    assert resp.status_code == 401
    resp = client.get("/api/poll", headers={"X-Device-Token": "wrong"})
    assert resp.status_code == 401


def test_poll_returns_queued_command_then_none():
    client = client_with_auth()
    cmd_id = models.queue_command("dispense_stage", params={"stage": 1, "pills": 2})
    resp = client.get("/api/poll", headers=auth_headers())
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["command"] == "dispense_stage"
    assert payload["params"] == {"stage": 1, "pills": 2}
    assert payload["command_id"] == cmd_id

    resp2 = client.get("/api/poll", headers=auth_headers())
    assert resp2.get_json() == {"command": "none"}


def test_schedule_endpoint_returns_seeded_schedule():
    client = client_with_auth()
    resp = client.get("/api/schedule", headers=auth_headers())
    assert resp.status_code == 200
    data = resp.get_json()
    slots = {s["slot"] for s in data["schedules"]}
    assert slots == {"morning", "afternoon", "night"}


def test_dose_completed_event_notifies_both(event_loop):
    """A dose_completed event triggers messages to patient and caregiver."""
    send_log: list = []
    stub = make_stub_app(send_log)

    patient_id = models.register_user(1001, "patient", display_name="alice")
    models.register_user(2002, "caregiver", display_name="bob", linked_patient_id=patient_id)
    models.link_caregiver_to_patient(2002, patient_id)

    client = client_with_auth(telegram_app=stub, loop=event_loop)
    resp = client.post(
        "/api/event",
        headers=auth_headers(),
        json={
            "event_type": "dose_completed",
            "pills_target": 4,
            "pills_actual": 4,
            "timestamp": "2026-05-12T08:00:15Z",
        },
    )
    assert resp.status_code == 200

    # drain any scheduled coroutines
    async def _wait():
        await asyncio.sleep(0.05)
    event_loop.run_until_complete(_wait())

    chat_ids = {entry["chat_id"] for entry in send_log}
    assert 1001 in chat_ids
    assert 2002 in chat_ids

    patient_msg = next(e["text"] for e in send_log if e["chat_id"] == 1001)
    caregiver_msg = next(e["text"] for e in send_log if e["chat_id"] == 2002)
    assert "/confirm" in patient_msg
    assert "completed" in caregiver_msg.lower()
    assert "4" in caregiver_msg


def test_fault_event_notifies_caregiver_with_reason(event_loop):
    send_log: list = []
    stub = make_stub_app(send_log)

    patient_id = models.register_user(1001, "patient", display_name="alice")
    models.register_user(2002, "caregiver", display_name="bob", linked_patient_id=patient_id)

    client = client_with_auth(telegram_app=stub, loop=event_loop)
    resp = client.post(
        "/api/event",
        headers=auth_headers(),
        json={
            "event_type": "fault",
            "stage": 2,
            "fault_reason": "no_drop_detected",
            "timestamp": "2026-05-12T08:00:15Z",
        },
    )
    assert resp.status_code == 200

    async def _wait():
        await asyncio.sleep(0.05)
    event_loop.run_until_complete(_wait())

    caregiver_msgs = [e for e in send_log if e["chat_id"] == 2002]
    assert caregiver_msgs, "caregiver did not receive fault alert"
    text = caregiver_msgs[0]["text"]
    assert "FAULT" in text or "fault" in text.lower()
    assert "no_drop_detected" in text
    # patient receives the generic "call your caregiver" message too
    patient_msgs = [e for e in send_log if e["chat_id"] == 1001]
    assert patient_msgs
    assert "caregiver" in patient_msgs[0]["text"].lower()


def test_missed_dose_detection_and_notification(event_loop):
    send_log: list = []
    stub = make_stub_app(send_log)
    patient_id = models.register_user(1001, "patient", display_name="alice")
    models.register_user(2002, "caregiver", display_name="bob", linked_patient_id=patient_id)

    sixteen_min_ago = datetime.utcnow() - timedelta(minutes=16)
    models.log_event(
        "dose_completed",
        pills_target=4,
        pills_actual=4,
        timestamp=sixteen_min_ago,
    )

    missed = event_loop.run_until_complete(check_missed_doses(stub))
    assert len(missed) == 1

    rows = models.get_conn().execute(
        "SELECT COUNT(*) AS c FROM events WHERE event_type='missed_dose';"
    ).fetchone()
    assert rows["c"] == 1

    # second run is idempotent: no duplicate missed_dose entries
    missed_again = event_loop.run_until_complete(check_missed_doses(stub))
    assert missed_again == []

    chat_ids = {e["chat_id"] for e in send_log}
    assert 1001 in chat_ids and 2002 in chat_ids


def test_missed_dose_not_flagged_when_patient_confirms(event_loop):
    send_log: list = []
    stub = make_stub_app(send_log)
    models.register_user(1001, "patient", display_name="alice")
    models.register_user(2002, "caregiver", display_name="bob")

    twenty_min_ago = datetime.utcnow() - timedelta(minutes=20)
    models.log_event(
        "dose_completed", pills_target=4, pills_actual=4, timestamp=twenty_min_ago,
    )
    nineteen_min_ago = datetime.utcnow() - timedelta(minutes=19)
    models.log_event("patient_confirmed", timestamp=nineteen_min_ago)

    missed = event_loop.run_until_complete(check_missed_doses(stub))
    assert missed == []


def test_set_schedule_updates_table_and_queues_command():
    # Simulate what caregiver.set_schedule does (DB write + command enqueue).
    slot, hour, minute, p1, p2, p3 = "morning", 9, 0, 1, 1, 1
    models.update_schedule(slot, hour, minute, p1, p2, p3)
    cmd_id = models.queue_command(
        "update_schedule",
        params={
            "slot": slot, "hour": hour, "minute": minute,
            "pills_stage1": p1, "pills_stage2": p2, "pills_stage3": p3,
        },
        issued_by_chat_id=2002,
    )

    row = models.get_schedule(slot)
    assert row["hour"] == 9 and row["minute"] == 0
    assert (row["pills_stage1"], row["pills_stage2"], row["pills_stage3"]) == (1, 1, 1)

    pending = models.pending_commands()
    assert any(p["id"] == cmd_id and p["command"] == "update_schedule" for p in pending)


def test_event_endpoint_rejects_unknown_type(event_loop):
    client = client_with_auth(telegram_app=None, loop=event_loop)
    resp = client.post(
        "/api/event",
        headers=auth_headers(),
        json={"event_type": "totally_made_up"},
    )
    assert resp.status_code == 400


def test_command_result_updates_status():
    cmd_id = models.queue_command("dispense_all", params={})
    # consume it
    models.next_pending_command()
    client = client_with_auth()
    resp = client.post(
        "/api/command_result",
        headers=auth_headers(),
        json={"command_id": cmd_id, "status": "executed", "detail": "ok"},
    )
    assert resp.status_code == 200
    row = models.get_conn().execute(
        "SELECT status FROM command_queue WHERE id=?;", (cmd_id,)
    ).fetchone()
    assert row["status"] == "executed"
