"""Caregiver-only command handlers."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from .. import config, models

logger = logging.getLogger(__name__)


def caregiver_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = models.get_user_by_chat(update.effective_chat.id)
        if user is None:
            await update.message.reply_text(
                "Please /start first to register."
            )
            return
        if user["role"] != "caregiver":
            await update.message.reply_text(
                "Sorry, this command is only available to caregivers."
            )
            return
        return await func(update, context)
    return wrapper


def _parse_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@caregiver_only
async def dispense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sched = models.next_schedule()
    if sched is not None:
        p1, p2, p3 = sched["pills_stage1"], sched["pills_stage2"], sched["pills_stage3"]
    else:
        p1, p2, p3 = 1, 1, 1
    cmd_id = models.queue_command(
        "dispense_all",
        params={"stage1": p1, "stage2": p2, "stage3": p3},
        issued_by_chat_id=update.effective_chat.id,
    )
    models.log_event(
        "manual_dispense",
        pills_target=p1 + p2 + p3,
        payload={"command_id": cmd_id, "stage1": p1, "stage2": p2, "stage3": p3},
    )
    await update.message.reply_text(
        f"✓ Dispense command queued ({p1}/{p2}/{p3}). "
        "The device will execute it within a few seconds."
    )


@caregiver_only
async def dispense_stage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if len(args) != 2:
        await update.message.reply_text(
            "Usage: /dispense_stage <stage 1-3> <pills 1-5>"
        )
        return
    stage = _parse_int(args[0])
    pills = _parse_int(args[1])
    if stage not in (1, 2, 3):
        await update.message.reply_text("Invalid stage. Must be 1, 2, or 3.")
        return
    if pills is None or not (1 <= pills <= 5):
        await update.message.reply_text("Invalid pill count. Must be 1 to 5.")
        return
    cmd_id = models.queue_command(
        "dispense_stage",
        params={"stage": stage, "pills": pills},
        issued_by_chat_id=update.effective_chat.id,
    )
    models.log_event(
        "manual_dispense",
        stage=stage,
        pills_target=pills,
        payload={"command_id": cmd_id},
    )
    await update.message.reply_text(
        f"✓ Stage {stage} ({pills} pills) command queued."
    )


@caregiver_only
async def set_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if len(args) != 5:
        await update.message.reply_text(
            "Usage: /set_schedule <morning|afternoon|night> <HH:MM> <p1> <p2> <p3>"
        )
        return
    slot, time_str, p1s, p2s, p3s = args
    if slot not in ("morning", "afternoon", "night"):
        await update.message.reply_text("Invalid slot. Use morning, afternoon, or night.")
        return
    if ":" not in time_str:
        await update.message.reply_text("Invalid time. Use HH:MM (24-hour).")
        return
    h_str, m_str = time_str.split(":", 1)
    hour = _parse_int(h_str)
    minute = _parse_int(m_str)
    if hour is None or not (0 <= hour <= 23) or minute is None or not (0 <= minute <= 59):
        await update.message.reply_text("Invalid time. Use HH:MM (24-hour).")
        return
    pills = [_parse_int(p1s), _parse_int(p2s), _parse_int(p3s)]
    if any(p is None or not (0 <= p <= 5) for p in pills):
        await update.message.reply_text("Invalid pill count. Each value must be 0 to 5.")
        return

    models.update_schedule(slot, hour, minute, pills[0], pills[1], pills[2])
    cmd_id = models.queue_command(
        "update_schedule",
        params={
            "slot": slot,
            "hour": hour,
            "minute": minute,
            "pills_stage1": pills[0],
            "pills_stage2": pills[1],
            "pills_stage3": pills[2],
        },
        issued_by_chat_id=update.effective_chat.id,
    )
    models.log_event(
        "schedule_updated",
        payload={"command_id": cmd_id, "slot": slot, "hour": hour, "minute": minute, "pills": pills},
    )
    await update.message.reply_text(
        f"✓ Schedule updated: {slot} {hour:02d}:{minute:02d} "
        f"({pills[0]}/{pills[1]}/{pills[2]}). Device will refresh on next poll."
    )


@caregiver_only
async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = models.get_schedules()
    if not rows:
        await update.message.reply_text("No schedule configured.")
        return
    lines = ["*Current schedule*", "```", "Slot       Time   S1 S2 S3"]
    for r in rows:
        lines.append(
            f"{r['slot']:<10} {r['hour']:02d}:{r['minute']:02d}  "
            f"{r['pills_stage1']:>2} {r['pills_stage2']:>2} {r['pills_stage3']:>2}"
        )
    lines.append("```")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


_ICONS = {
    "dose_started": "🔔",
    "stage_dispensed": "•",
    "dose_completed": "✓",
    "fault": "⚠",
    "manual_dispense": "💊",
    "schedule_updated": "🗓",
    "patient_confirmed": "👍",
    "missed_dose": "❌",
}


@caregiver_only
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    days = 7
    if args:
        parsed = _parse_int(args[0])
        if parsed is None or not (1 <= parsed <= 90):
            await update.message.reply_text("Invalid days argument. Use 1 to 90.")
            return
        days = parsed
    events = models.recent_events(days)
    if not events:
        await update.message.reply_text(f"No events in the last {days} day(s).")
        return

    grouped: dict[str, list] = defaultdict(list)
    for ev in events:
        ts = ev["timestamp"]
        if not isinstance(ts, datetime):
            try:
                ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except ValueError:
                ts = None
        day_key = ts.strftime("%Y-%m-%d") if ts else "unknown"
        time_str = ts.strftime("%H:%M") if ts else "--:--"
        icon = _ICONS.get(ev["event_type"], "•")
        detail = ev["event_type"]
        if ev["fault_reason"]:
            detail += f" ({ev['fault_reason']})"
        elif ev["stage"]:
            detail += f" stage {ev['stage']}"
        grouped[day_key].append(f"  {icon} {time_str}  {detail}")

    lines = [f"*Event history (last {days}d)*"]
    for day in sorted(grouped):
        lines.append(f"\n*{day}*")
        lines.extend(grouped[day])
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    await update.message.reply_text(text, parse_mode="Markdown")


@caregiver_only
async def adherence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    start = datetime.now(config.TZ) - timedelta(days=7)
    end = datetime.now(config.TZ) + timedelta(minutes=1)
    events = models.events_in_range(start.replace(tzinfo=None), end.replace(tzinfo=None))

    started = sum(1 for e in events if e["event_type"] == "dose_started")
    completed = sum(1 for e in events if e["event_type"] == "dose_completed")
    faults = [e for e in events if e["event_type"] == "fault"]
    missed = [e for e in events if e["event_type"] == "missed_dose"]

    scheduled = started or completed + len(missed)
    pct = (completed / scheduled * 100) if scheduled else 0.0

    lines = [
        "*7-day adherence*",
        f"Scheduled doses: {scheduled}",
        f"Completed: {completed}",
        f"Missed: {len(missed)}",
        f"Faults: {len(faults)}",
        f"Adherence: *{pct:.0f}%*",
    ]
    if missed:
        lines.append("\nMissed:")
        for m in missed[-5:]:
            ts = m["timestamp"]
            ts_str = ts.strftime("%Y-%m-%d %H:%M") if isinstance(ts, datetime) else str(ts)
            lines.append(f"  ❌ {ts_str}")
    if faults:
        lines.append("\nFaults:")
        for f in faults[-5:]:
            ts = f["timestamp"]
            ts_str = ts.strftime("%Y-%m-%d %H:%M") if isinstance(ts, datetime) else str(ts)
            lines.append(f"  ⚠ {ts_str} — {f['fault_reason'] or 'unknown'}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


def get_handlers() -> list:
    return [
        CommandHandler("dispense", dispense),
        CommandHandler("dispense_stage", dispense_stage),
        CommandHandler("set_schedule", set_schedule),
        CommandHandler("schedule", schedule_cmd),
        CommandHandler("history", history),
        CommandHandler("adherence", adherence),
    ]
