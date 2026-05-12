"""Outbound notification builders triggered from device events."""
from __future__ import annotations

import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from .. import config, models

logger = logging.getLogger(__name__)


def snooze_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(
            f"Snooze {config.SNOOZE_DEFAULT_MINUTES} min",
            callback_data=f"snooze:{config.SNOOZE_DEFAULT_MINUTES}",
        )]]
    )


def fault_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Mark as resolved", callback_data="fault:resolve")]]
    )


def _hhmm(ts: datetime | str | None) -> str:
    if ts is None:
        ts = datetime.now(config.TZ)
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(config.TZ).strftime("%H:%M")
    return ts.strftime("%H:%M")


async def _send(application: Application, chat_id: int, text: str, **kwargs) -> None:
    try:
        await application.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception:
        logger.exception("Failed to send notification to %s", chat_id)


async def notify_dose_started(application: Application, payload: dict) -> None:
    ts = _hhmm(payload.get("timestamp"))
    s1 = payload.get("pills_stage1", payload.get("stage1", 0))
    s2 = payload.get("pills_stage2", payload.get("stage2", 0))
    s3 = payload.get("pills_stage3", payload.get("stage3", 0))
    patient_text = "💊 Time for your medication! Please come to the dispenser."
    caregiver_text = (
        f"Dose started at {ts} (Stage 1: {s1} pills, "
        f"Stage 2: {s2} pills, Stage 3: {s3} pills)."
    )
    for p in models.list_patients():
        await _send(application, p["telegram_chat_id"], patient_text,
                    reply_markup=snooze_keyboard())
    for c in models.all_caregivers():
        await _send(application, c["telegram_chat_id"], caregiver_text)


async def notify_dose_completed(application: Application, payload: dict) -> None:
    ts = _hhmm(payload.get("timestamp"))
    actual = payload.get("pills_actual", payload.get("pills_target", "?"))
    patient_text = (
        "✓ Your medication has been dispensed. "
        "Please collect it from the tray and tap /confirm when done."
    )
    caregiver_text = (
        f"✓ Dose completed at {ts}. {actual} pills dispensed successfully."
    )
    for p in models.list_patients():
        await _send(application, p["telegram_chat_id"], patient_text)
    for c in models.all_caregivers():
        await _send(application, c["telegram_chat_id"], caregiver_text)


async def notify_fault(application: Application, payload: dict) -> None:
    ts = _hhmm(payload.get("timestamp"))
    stage = payload.get("stage", "?")
    reason = payload.get("fault_reason", "unknown")
    patient_text = "⚠ There was a problem dispensing. Please call your caregiver."
    caregiver_text = (
        f"🚨 FAULT at {ts}, Stage {stage}: {reason}. "
        "Please attend to the dispenser."
    )
    for p in models.list_patients():
        await _send(application, p["telegram_chat_id"], patient_text)
    for c in models.all_caregivers():
        await _send(application, c["telegram_chat_id"], caregiver_text,
                    reply_markup=fault_keyboard())


async def notify_missed_dose(application: Application, slot: str, when: datetime) -> None:
    ts = when.strftime("%H:%M")
    patient_text = "Reminder: have you taken your dose? Tap /confirm."
    caregiver_text = (
        f"⚠ Patient has not confirmed the {slot} dose from {ts}. "
        "Consider checking in."
    )
    for p in models.list_patients():
        await _send(application, p["telegram_chat_id"], patient_text)
    for c in models.all_caregivers():
        await _send(application, c["telegram_chat_id"], caregiver_text)


# ----- inline-keyboard callback handlers -----

async def on_snooze_button(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from datetime import timedelta
    query = update.callback_query
    await query.answer()
    try:
        minutes = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        minutes = config.SNOOZE_DEFAULT_MINUTES
    minutes = max(1, min(minutes, config.SNOOZE_MAX_MINUTES))
    chat_id = query.message.chat_id
    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(
        chat_id=chat_id, text=f"⏰ Snoozed for {minutes} minute(s)."
    )
    job_queue = context.application.job_queue
    if job_queue is not None:
        async def _remind(ctx):
            await ctx.bot.send_message(
                chat_id=chat_id,
                text="🔔 Snooze reminder: time to take your medication. Tap /confirm when done.",
            )
        job_queue.run_once(_remind, when=timedelta(minutes=minutes), chat_id=chat_id)


async def on_fault_resolve(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Marked as resolved.")
    await query.edit_message_reply_markup(reply_markup=None)


def get_handlers() -> list:
    return [
        CallbackQueryHandler(on_snooze_button, pattern=r"^snooze:"),
        CallbackQueryHandler(on_fault_resolve, pattern=r"^fault:resolve$"),
    ]
