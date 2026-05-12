"""Scheduled background jobs (missed-dose checker, daily summary)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .. import config, models
from ..telegram_handlers import notifications

logger = logging.getLogger(__name__)


def _to_dt(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


async def check_missed_doses(application) -> list[int]:
    """Scan for dose_completed events older than the missed window with no confirmation."""
    window = timedelta(minutes=config.MISSED_DOSE_WINDOW_MINUTES)
    now = datetime.utcnow()
    cutoff = now - window
    horizon = now - timedelta(hours=24)

    cur = models.get_conn().execute(
        "SELECT * FROM events WHERE event_type='dose_completed' AND timestamp >= ? "
        "AND timestamp <= ? ORDER BY timestamp ASC;",
        (horizon, cutoff),
    )
    completed = cur.fetchall()

    new_missed: list[int] = []
    for ev in completed:
        ev_ts = _to_dt(ev["timestamp"])
        if ev_ts is None:
            continue
        # window end is the next dose_completed event, or now
        cur = models.get_conn().execute(
            "SELECT timestamp FROM events WHERE event_type='dose_completed' AND id > ? "
            "ORDER BY id ASC LIMIT 1;",
            (ev["id"],),
        )
        nxt = cur.fetchone()
        window_end_dt = _to_dt(nxt["timestamp"]) if nxt else now

        # already confirmed?
        cur = models.get_conn().execute(
            "SELECT 1 FROM events WHERE event_type='patient_confirmed' "
            "AND timestamp >= ? AND timestamp < ? LIMIT 1;",
            (ev_ts, window_end_dt),
        )
        if cur.fetchone():
            continue

        # already flagged missed?
        cur = models.get_conn().execute(
            "SELECT 1 FROM events WHERE event_type='missed_dose' "
            "AND timestamp >= ? AND timestamp < ? LIMIT 1;",
            (ev_ts, window_end_dt),
        )
        if cur.fetchone():
            continue

        slot = _slot_for_time(ev_ts)
        missed_id = models.log_event(
            "missed_dose",
            payload={"linked_dose_completed_id": ev["id"], "slot": slot},
        )
        new_missed.append(missed_id)
        logger.info("Missed dose detected for %s at %s", slot, ev_ts)
        if application is not None:
            await notifications.notify_missed_dose(application, slot, ev_ts)
    return new_missed


def _slot_for_time(ts: datetime) -> str:
    rows = models.get_schedules()
    if not rows:
        return "unknown"
    target = ts.hour * 60 + ts.minute
    best = min(rows, key=lambda r: abs((r["hour"] * 60 + r["minute"]) - target))
    return best["slot"]


async def daily_adherence_summary(application) -> None:
    """Send each caregiver a summary of today's adherence."""
    if application is None:
        return
    now = datetime.now(config.TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_naive = start.replace(tzinfo=None).astimezone(None) if start.tzinfo else start
    events = models.events_in_range(start.replace(tzinfo=None), now.replace(tzinfo=None))

    started = sum(1 for e in events if e["event_type"] == "dose_started")
    completed = sum(1 for e in events if e["event_type"] == "dose_completed")
    missed = sum(1 for e in events if e["event_type"] == "missed_dose")
    scheduled = started or completed + missed
    pct = (completed / scheduled * 100) if scheduled else 0.0

    text = (
        f"📊 Daily summary ({now.strftime('%Y-%m-%d')})\n"
        f"Today: {completed} of {scheduled} doses completed. "
        f"Adherence: {pct:.0f}%."
    )
    if missed:
        text += f" Missed: {missed}."
    for cg in models.all_caregivers():
        try:
            await application.bot.send_message(
                chat_id=cg["telegram_chat_id"], text=text
            )
        except Exception:
            logger.exception("Failed to send daily summary to %s", cg["telegram_chat_id"])


def register_jobs(scheduler, application) -> None:
    """Register the scheduler jobs against the given async application."""
    async def _missed_job():
        await check_missed_doses(application)

    async def _daily_job():
        await daily_adherence_summary(application)

    scheduler.add_job(
        _missed_job, trigger="interval", minutes=1, id="missed_dose_check",
        replace_existing=True, misfire_grace_time=60,
    )
    scheduler.add_job(
        _daily_job, trigger="cron", hour=21, minute=0, id="daily_summary",
        replace_existing=True, timezone=config.TZ,
    )
