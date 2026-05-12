"""Patient-only command handlers."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from functools import wraps

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from .. import config, models
from . import notifications

logger = logging.getLogger(__name__)


def patient_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = models.get_user_by_chat(update.effective_chat.id)
        if user is None:
            await update.message.reply_text("Please /start first to register.")
            return
        if user["role"] != "patient":
            await update.message.reply_text(
                "This command is only available to patients."
            )
            return
        return await func(update, context, user)
    return wrapper


@patient_only
async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, user) -> None:
    models.log_event("patient_confirmed", payload={"chat_id": update.effective_chat.id})
    await update.message.reply_text(
        "👍 Thank you for confirming your dose. Stay well!"
    )
    now = datetime.now(config.TZ).strftime("%H:%M")
    for cg in models.list_caregivers_for_patient(user["id"]):
        try:
            await context.bot.send_message(
                chat_id=cg["telegram_chat_id"],
                text=f"✓ Patient confirmed dose at {now}.",
            )
        except Exception:
            logger.exception("Failed to notify caregiver %s", cg["telegram_chat_id"])


@patient_only
async def snooze(update: Update, context: ContextTypes.DEFAULT_TYPE, user) -> None:
    args = context.args or []
    minutes = config.SNOOZE_DEFAULT_MINUTES
    if args:
        try:
            minutes = int(args[0])
        except ValueError:
            await update.message.reply_text("Invalid number. Usage: /snooze <minutes>")
            return
    if not (1 <= minutes <= config.SNOOZE_MAX_MINUTES):
        await update.message.reply_text(
            f"Snooze must be between 1 and {config.SNOOZE_MAX_MINUTES} minutes."
        )
        return

    chat_id = update.effective_chat.id
    await update.message.reply_text(f"⏰ Snoozed for {minutes} minute(s). I'll remind you.")

    job_queue = context.application.job_queue if hasattr(context, "application") else context.job_queue
    if job_queue is not None:
        job_queue.run_once(
            _send_snooze_reminder,
            when=timedelta(minutes=minutes),
            chat_id=chat_id,
            name=f"snooze:{chat_id}",
        )


async def _send_snooze_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🔔 Snooze reminder: have you taken your medication? "
            "Tap /confirm when done."
        ),
        reply_markup=notifications.snooze_keyboard(),
    )


def get_handlers() -> list:
    return [
        CommandHandler("confirm", confirm),
        CommandHandler("snooze", snooze),
    ]
