"""Common command handlers: /start, /help, /status."""
from __future__ import annotations

import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .. import config, models

logger = logging.getLogger(__name__)

LINKING_PATIENT = 1

ROLE_PATIENT = "patient"
ROLE_CAREGIVER = "caregiver"

HELP_PATIENT = (
    "*Patient commands*\n"
    "/status — see the device state and next dose\n"
    "/confirm — confirm you've taken your dose\n"
    "/snooze <minutes> — snooze a reminder (max {max})\n"
    "/help — show this help"
).format(max=config.SNOOZE_MAX_MINUTES)

HELP_CAREGIVER = (
    "*Caregiver commands*\n"
    "/status — device status & next dose\n"
    "/schedule — show the configured schedule\n"
    "/set_schedule <slot> <HH:MM> <p1> <p2> <p3>\n"
    "/dispense — manually dispense the next scheduled dose now\n"
    "/dispense_stage <n> <pills> — dispense a specific stage\n"
    "/history [days] — recent event log (default 7 days)\n"
    "/adherence — 7-day adherence summary\n"
    "/help — show this help"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    user = update.effective_user
    existing = models.get_user_by_chat(chat.id)
    if existing is not None:
        await update.message.reply_text(
            f"Welcome back, {existing['display_name'] or 'friend'}! "
            f"You are registered as a {existing['role']}. Use /help to see commands."
        )
        return ConversationHandler.END

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("I'm the patient", callback_data="role:patient"),
                InlineKeyboardButton("I'm a caregiver", callback_data="role:caregiver"),
            ]
        ]
    )
    display = user.username or user.full_name if user else None
    context.user_data["display_name"] = display
    await update.message.reply_text(
        "Welcome to MEDIC-AID. Are you the patient or a caregiver?",
        reply_markup=keyboard,
    )
    return LINKING_PATIENT


async def role_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    role = query.data.split(":", 1)[1]
    chat_id = query.message.chat_id
    display_name = context.user_data.get("display_name") or query.from_user.username or query.from_user.full_name

    if role == ROLE_PATIENT:
        models.register_user(chat_id, ROLE_PATIENT, display_name=display_name)
        await query.edit_message_text(
            f"✓ Registered as patient ({display_name}).\n"
            "Your caregiver can now link to you. Use /help for commands."
        )
        return ConversationHandler.END

    # caregiver path: must link to a patient
    models.register_user(chat_id, ROLE_CAREGIVER, display_name=display_name)
    await query.edit_message_text(
        "Registered as caregiver. Please send the patient's Telegram username "
        "(e.g. @alice) or their numeric chat ID to link to them. Reply /cancel to abort."
    )
    return LINKING_PATIENT


async def receive_patient_identifier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    if text.lower() in {"/cancel", "cancel"}:
        await update.message.reply_text("Linking cancelled. Send /start to try again.")
        return ConversationHandler.END

    patient = models.find_patient_by_identifier(text)
    if patient is None:
        await update.message.reply_text(
            "I couldn't find a patient with that identifier. Make sure the patient has "
            "registered first via /start. Try again or send /cancel."
        )
        return LINKING_PATIENT

    models.link_caregiver_to_patient(chat_id, patient["id"])
    await update.message.reply_text(
        f"✓ Linked to patient {patient['display_name'] or patient['telegram_chat_id']}. "
        f"Use /help to see your commands."
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = models.get_user_by_chat(update.effective_chat.id)
    if user is None:
        await update.message.reply_text(
            "Please /start first to register as a patient or caregiver."
        )
        return
    text = HELP_CAREGIVER if user["role"] == ROLE_CAREGIVER else HELP_PATIENT
    await update.message.reply_text(text, parse_mode="Markdown")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    last = models.last_event()
    next_sched = models.next_schedule()
    pending = models.pending_commands()

    lines = ["*MEDIC-AID status*"]
    if last is None:
        lines.append("No events recorded yet.")
    else:
        ts = last["timestamp"]
        if isinstance(ts, datetime):
            ts_str = ts.strftime("%Y-%m-%d %H:%M")
        else:
            ts_str = str(ts)
        lines.append(f"Last event: `{last['event_type']}` at {ts_str}")
        if last["fault_reason"]:
            lines.append(f"Fault reason: {last['fault_reason']}")

    if next_sched is not None:
        lines.append(
            f"Next dose: *{next_sched['slot']}* at "
            f"{next_sched['hour']:02d}:{next_sched['minute']:02d} "
            f"({next_sched['pills_stage1']}/{next_sched['pills_stage2']}/{next_sched['pills_stage3']})"
        )
    else:
        lines.append("No schedule configured.")

    lines.append(f"Pending commands in queue: {len(pending)}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


def get_handlers() -> list:
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            LINKING_PATIENT: [
                CallbackQueryHandler(role_callback, pattern=r"^role:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_patient_identifier),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_user=True,
    )
    return [
        conv,
        CommandHandler("help", help_cmd),
        CommandHandler("status", status_cmd),
    ]
