"""Flask routes for the ESP32 dispenser device."""
from __future__ import annotations

import asyncio
import json
import logging
from functools import wraps

from flask import Blueprint, Flask, jsonify, request
from flask_cors import CORS

from .. import config, models
from ..telegram_handlers import notifications

logger = logging.getLogger(__name__)

bp = Blueprint("device_api", __name__, url_prefix="/api")

# Set by the application bootstrap so notifications can be dispatched into the asyncio loop.
_TELEGRAM_APP = None
_EVENT_LOOP: asyncio.AbstractEventLoop | None = None


def configure(telegram_app, loop: asyncio.AbstractEventLoop | None) -> None:
    global _TELEGRAM_APP, _EVENT_LOOP
    _TELEGRAM_APP = telegram_app
    _EVENT_LOOP = loop


def _dispatch(coro) -> None:
    """Schedule a coroutine on the Telegram app's event loop from a Flask thread."""
    if _EVENT_LOOP is None or _TELEGRAM_APP is None:
        logger.debug("Telegram app not configured; skipping notification dispatch.")
        return
    try:
        asyncio.run_coroutine_threadsafe(coro, _EVENT_LOOP)
    except RuntimeError:
        logger.exception("Failed to schedule notification on event loop")


def require_device_token(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = request.headers.get("X-Device-Token", "")
        if not token or token != config.DEVICE_TOKEN:
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


@bp.get("/poll")
@require_device_token
def poll():
    row = models.next_pending_command()
    if row is None:
        return jsonify({"command": "none"})
    try:
        params = json.loads(row["params_json"] or "{}")
    except json.JSONDecodeError:
        params = {}
    return jsonify({
        "command": row["command"],
        "params": params,
        "command_id": row["id"],
    })


@bp.post("/event")
@require_device_token
def event():
    data = request.get_json(silent=True) or {}
    event_type = data.get("event_type")
    if event_type not in models.EVENT_TYPES:
        return jsonify({"error": f"unknown event_type: {event_type}"}), 400

    try:
        models.log_event(
            event_type,
            stage=data.get("stage"),
            pills_target=data.get("pills_target"),
            pills_actual=data.get("pills_actual"),
            fault_reason=data.get("fault_reason"),
            payload=data,
        )
    except Exception:
        logger.exception("Failed to log event")
        return jsonify({"error": "internal_error"}), 500

    if _TELEGRAM_APP is not None:
        if event_type == "dose_started":
            _dispatch(notifications.notify_dose_started(_TELEGRAM_APP, data))
        elif event_type == "dose_completed":
            _dispatch(notifications.notify_dose_completed(_TELEGRAM_APP, data))
        elif event_type == "fault":
            _dispatch(notifications.notify_fault(_TELEGRAM_APP, data))

    return jsonify({"ok": True})


@bp.post("/command_result")
@require_device_token
def command_result():
    data = request.get_json(silent=True) or {}
    command_id = data.get("command_id")
    status = data.get("status")
    detail = data.get("detail")
    if not isinstance(command_id, int) or status not in ("executed", "failed"):
        return jsonify({"error": "invalid_payload"}), 400
    models.update_command_status(command_id, status, detail)
    return jsonify({"ok": True})


@bp.get("/schedule")
@require_device_token
def schedule():
    rows = models.get_schedules()
    return jsonify({
        "schedules": [
            {
                "slot": r["slot"],
                "hour": r["hour"],
                "minute": r["minute"],
                "pills_stage1": r["pills_stage1"],
                "pills_stage2": r["pills_stage2"],
                "pills_stage3": r["pills_stage3"],
                "enabled": bool(r["enabled"]),
            }
            for r in rows
        ]
    })


@bp.get("/health")
def health():
    return jsonify({"status": "ok"})


def create_app(telegram_app=None, loop: asyncio.AbstractEventLoop | None = None) -> Flask:
    app = Flask(__name__)
    CORS(app)
    app.register_blueprint(bp)
    configure(telegram_app, loop)
    return app
