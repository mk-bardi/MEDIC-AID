"""Entry point: starts the Telegram bot and the Flask device API in one process."""
from __future__ import annotations

import asyncio
import logging
import threading
from logging.handlers import RotatingFileHandler

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import Application

from . import config
from .api.routes import create_app
from .db import get_conn
from .jobs.scheduled import register_jobs
from .telegram_handlers import caregiver, common, notifications, patient


def configure_logging() -> None:
    handler = RotatingFileHandler(
        config.LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)


def build_application() -> Application:
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    for h in common.get_handlers():
        app.add_handler(h)
    for h in caregiver.get_handlers():
        app.add_handler(h)
    for h in patient.get_handlers():
        app.add_handler(h)
    for h in notifications.get_handlers():
        app.add_handler(h)
    return app


def start_flask(telegram_app: Application, loop: asyncio.AbstractEventLoop) -> threading.Thread:
    flask_app = create_app(telegram_app=telegram_app, loop=loop)

    def _run():
        flask_app.run(
            host=config.FLASK_HOST,
            port=config.FLASK_PORT,
            debug=False,
            use_reloader=False,
            threaded=True,
        )

    thread = threading.Thread(target=_run, name="flask-device-api", daemon=True)
    thread.start()
    return thread


async def run() -> None:
    get_conn()  # initialise schema
    telegram_app = build_application()

    loop = asyncio.get_running_loop()
    start_flask(telegram_app, loop)

    scheduler = AsyncIOScheduler(timezone=config.TZ)
    register_jobs(scheduler, telegram_app)
    scheduler.start()

    await telegram_app.initialize()
    await telegram_app.start()
    try:
        await telegram_app.updater.start_polling()
        # Run forever
        stop_event = asyncio.Event()
        await stop_event.wait()
    finally:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        scheduler.shutdown(wait=False)


def main() -> None:
    configure_logging()
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger(__name__).info("Shutting down.")


if __name__ == "__main__":
    main()
