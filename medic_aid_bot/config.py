"""Loads configuration from .env and exposes typed constants."""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)
else:
    load_dotenv()


def _get(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN", "")
DEVICE_TOKEN = _get("DEVICE_TOKEN", "dev-token")
DATABASE_PATH = _get("DATABASE_PATH", "medicaid.db")
FLASK_HOST = _get("FLASK_HOST", "0.0.0.0")
# Railway, Render, Heroku etc. inject $PORT — prefer that when present.
FLASK_PORT = int(_get("PORT", _get("FLASK_PORT", "5000")))
MISSED_DOSE_WINDOW_MINUTES = int(_get("MISSED_DOSE_WINDOW_MINUTES", "15"))
SNOOZE_DEFAULT_MINUTES = int(_get("SNOOZE_DEFAULT_MINUTES", "10"))
SNOOZE_MAX_MINUTES = int(_get("SNOOZE_MAX_MINUTES", "30"))
TIMEZONE_NAME = _get("TIMEZONE", "Africa/Lagos")
LOG_PATH = _get("LOG_PATH", "medicaid.log")

TZ = ZoneInfo(TIMEZONE_NAME)
