# MEDIC-AID Telegram Bot

A two-way Telegram bot that bridges an ESP32-based automated pill dispenser
with patients and caregivers. The bot delivers reminders, accepts manual
dispense and schedule commands, and forwards device events as real-time
notifications.

## Architecture

```
[ESP32 Wokwi sim] ←──── HTTP polling (every 2s) ────→ [Flask API server]
                  ────→ HTTP POST events ──────────→         │
                                                              │
                                                       [Bot logic]
                                                              │
                                                       [SQLite DB]
                                                              │
                                                   ┌──────────┴──────────┐
                                                   ▼                     ▼
                                          [Patient channel]    [Caregiver channel]
                                              (Telegram)            (Telegram)
```

Two components run in a single Python process:

- An async **Telegram bot worker** (long-polling Telegram for user commands).
- A threaded **Flask HTTP server** that the ESP32 polls and posts events to.

Both share the same SQLite database. Notifications dispatched from Flask
threads are scheduled onto the Telegram event loop with
`asyncio.run_coroutine_threadsafe`.

## Project layout

```
medic_aid_bot/
├── .env.example
├── requirements.txt
├── main.py
├── config.py
├── db.py
├── models.py
├── telegram_handlers/
│   ├── common.py
│   ├── caregiver.py
│   ├── patient.py
│   └── notifications.py
├── api/
│   └── routes.py
├── jobs/
│   └── scheduled.py
└── tests/
    └── test_basic.py
```

## Setup

1. Clone the repo and create a virtual environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r medic_aid_bot/requirements.txt
   ```

2. Get a bot token from [@BotFather](https://t.me/BotFather):
   - Send `/newbot`, follow the prompts, copy the token it gives you.

3. Copy the example env file and fill in your secrets:

   ```bash
   cp medic_aid_bot/.env.example medic_aid_bot/.env
   # edit medic_aid_bot/.env
   ```

   - `TELEGRAM_BOT_TOKEN` — from BotFather.
   - `DEVICE_TOKEN` — generate any long random string (`openssl rand -hex 32`).
     This goes into both the bot's `.env` and the ESP32 firmware.
   - `TIMEZONE` — e.g. `Africa/Lagos`. All schedule logic uses this.

4. Run the bot:

   ```bash
   python -m medic_aid_bot.main
   ```

   On first start the SQLite schema is created and the default schedule is
   seeded (morning 08:00, afternoon 14:00, night 20:00).

## Registering users

Each user starts a chat with the bot and sends `/start`:

- The **patient** taps "I'm the patient". They are registered immediately.
- The **caregiver** taps "I'm a caregiver" and is asked for the patient's
  username (e.g. `@alice`) or numeric chat id. The patient must register first.

The first caregiver in the system is whoever links to a registered patient.
After that, additional caregivers can link the same way.

## Command reference

**Patient**

| Command | Description |
| ------- | ----------- |
| `/start`, `/help`, `/status` | Onboarding, help, current state |
| `/confirm` | Confirm you've collected your dose |
| `/snooze <minutes>` | Snooze a reminder (1 – `SNOOZE_MAX_MINUTES`) |

**Caregiver**

| Command | Description |
| ------- | ----------- |
| `/dispense` | Manually trigger the next scheduled dose |
| `/dispense_stage <n> <pills>` | Dispense a specific stage (n=1–3, pills=1–5) |
| `/set_schedule <slot> <HH:MM> <p1> <p2> <p3>` | Update a schedule entry |
| `/schedule` | Show the configured schedule |
| `/history [days]` | Recent event log (default 7 days) |
| `/adherence` | 7-day adherence summary |

## Device HTTP API

All endpoints require the `X-Device-Token` header matching `DEVICE_TOKEN`.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET`  | `/api/poll` | Returns the next pending command (or `{"command":"none"}`) |
| `POST` | `/api/event` | ESP32 reports state-machine events |
| `POST` | `/api/command_result` | ESP32 reports whether a command succeeded |
| `GET`  | `/api/schedule` | ESP32 fetches current schedule |
| `GET`  | `/api/health` | Liveness probe (no auth) |

Event types accepted on `/api/event`: `dose_started`, `stage_dispensed`,
`dose_completed`, `fault`, `manual_dispense`, `schedule_updated`,
`patient_confirmed`, `missed_dose`.

Example event payload:

```json
{
  "event_type": "dose_completed",
  "stage": null,
  "pills_target": 4,
  "pills_actual": 4,
  "timestamp": "2026-05-12T08:00:15Z"
}
```

## Background jobs

- **Missed-dose checker** — every minute. A `dose_completed` event older than
  `MISSED_DOSE_WINDOW_MINUTES` with no `patient_confirmed` since produces a
  single `missed_dose` event and sends both patient and caregiver a reminder.
- **Daily summary** — every day at 21:00 in the configured timezone, sends a
  per-caregiver adherence summary.

## Testing locally with the ESP32 (ngrok)

The Flask server listens on `FLASK_PORT` (default `5000`). To expose it to a
real ESP32 or a Wokwi simulation:

```bash
ngrok http 5000
```

Use the printed `https://…ngrok-free.app` URL as the bot's base URL in the
ESP32 firmware, and embed the same `DEVICE_TOKEN` value.

## Deployment

Any small VPS works (Contabo, DigitalOcean, Railway, Render, etc.). Minimum
spec: 1 vCPU / 1 GB RAM / 10 GB disk.

A minimal systemd unit:

```ini
[Unit]
Description=MEDIC-AID Telegram bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/medic-aid
ExecStart=/opt/medic-aid/.venv/bin/python -m medic_aid_bot.main
Restart=on-failure
RestartSec=5
User=medicaid

[Install]
WantedBy=multi-user.target
```

For production exposure, terminate TLS at a reverse proxy (Caddy, nginx) in
front of port 5000.

## Running the tests

```bash
pip install pytest pytest-asyncio
pytest medic_aid_bot/tests -v
```

The tests use a fresh in-memory SQLite database per test and stub out the
Telegram bot so no token or network access is required.
