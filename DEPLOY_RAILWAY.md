# Deploying MEDIC-AID on Railway + Wokwi

This walks you from a fresh GitHub repo to a live Telegram bot that the Wokwi
ESP32 simulator can talk to. ~15 minutes end to end.

---

## Part A — Telegram setup (do this first)

1. Open Telegram, search for **@BotFather**, send `/newbot`.
2. Give it a name (`MEDIC-AID Bot`) and a username ending in `bot`
   (e.g. `medic_aid_demo_bot`).
3. BotFather replies with an HTTP token like
   `7891234567:AAH...xyz`. **Save it** — this is `TELEGRAM_BOT_TOKEN`.
4. Generate a long random string for `DEVICE_TOKEN`:
   ```bash
   openssl rand -hex 32
   ```
   Save it — you'll paste this into both Railway and the ESP32 firmware.

---

## Part B — Push the code to GitHub

Railway deploys from a GitHub repo, so push this branch first:

```bash
git push -u origin claude/medic-aid-telegram-bot-oVs01
```

(If you'd rather deploy `main`, merge the branch first.)

---

## Part C — Deploy on Railway

1. Go to [railway.app](https://railway.app/) and sign in with GitHub.
2. Click **New Project → Deploy from GitHub repo** and pick your `MEDIC-AID`
   repo. Authorise Railway to read it if prompted.
3. Railway auto-detects Python via Nixpacks (the included `nixpacks.toml`,
   `Procfile`, and root `requirements.txt` make this deterministic). It will
   start the first build immediately — **let it fail**, we need to set env
   vars first.
4. Open the service → **Variables** tab → **+ New Variable** and add:

   | Variable | Value |
   | -------- | ----- |
   | `TELEGRAM_BOT_TOKEN` | the token from BotFather |
   | `DEVICE_TOKEN`       | the random hex string from Part A |
   | `TIMEZONE`           | your IANA zone, e.g. `Africa/Lagos` |
   | `MISSED_DOSE_WINDOW_MINUTES` | `15` (optional, default 15) |
   | `SNOOZE_DEFAULT_MINUTES`     | `10` (optional) |
   | `SNOOZE_MAX_MINUTES`         | `30` (optional) |
   | `DATABASE_PATH`              | `/data/medicaid.db` (see step 6) |

   You do **not** need to set `PORT` — Railway injects it automatically and
   `config.py` already prefers `$PORT` over `FLASK_PORT`.

5. **Settings → Networking → Generate Domain.** Railway gives you something
   like `medicaid-bot-production.up.railway.app`. Copy the full
   `https://…` URL — this is your `BOT_BASE_URL` for the ESP32.

6. *(Recommended)* **Settings → Volumes → + New Volume.**
   Mount path: `/data`. Without a volume, the SQLite file lives on
   ephemeral disk and is wiped on every redeploy, taking your registered
   users and event history with it. The `DATABASE_PATH` env var above
   tells the app to store the DB on the volume.

7. **Deployments → Redeploy.** Watch the logs:
   - You should see `Connecting to bot…` then no errors.
   - Visit `https://<your-domain>/api/health` — it should return
     `{"status":"ok"}`.

---

## Part D — Register the first users on Telegram

1. In Telegram, open your bot and send `/start`.
2. **Patient first:** tap **"I'm the patient"**. The bot confirms registration.
   Note your Telegram username (e.g. `@alice`) — the caregiver will need it.
3. From a *different* Telegram account (or after `/start`-ing again as a new
   user), tap **"I'm a caregiver"**. The bot will ask for the patient's
   identifier — send the patient's `@username` or numeric chat ID.
4. Confirm with `/help` — you should see the caregiver-only commands.

---

## Part E — Wire up the Wokwi simulator

Your existing Wokwi project at
<https://wokwi.com/projects/463772405856432129> already has the steppers,
drop-sensor buttons, RTC, buzzer and LED wired up exactly as the firmware
expects. **Leave `diagram.json` alone** — no changes needed.

1. Open the project, click `sketch.ino`, select all, and replace its
   contents with `firmware/esp32_medicaid.ino` from this repo. The new
   sketch is your original state-machine code plus Wi-Fi, polling, and
   bot-protocol functions — all the same pins, the same dispense logic,
   the same fault detection.
2. Near the top of the sketch, in the `USER CONFIG` block, set the two
   placeholders:
   ```cpp
   const char* BOT_BASE_URL = "https://YOUR-APP.up.railway.app";
   const char* DEVICE_TOKEN = "the same hex string you put in Railway";
   ```
   Leave `WIFI_SSID = "Wokwi-GUEST"` and `WIFI_PASSWORD = ""` — those are
   Wokwi's simulated network.
3. Open **Library Manager** (left sidebar → Libraries tab → `+`) and add:
   - **ArduinoJson** (Benoit Blanchon)
   - **RTClib** (Adafruit) — if not already present
   `WiFi`, `HTTPClient`, `WiFiClientSecure`, and `Stepper` ship with the
   ESP32 core, no install needed.
4. Press **▶ Start simulation**. In the serial monitor you should see:
   ```
   === MEDIC-AID (bot-connected) booting ===
   [WIFI] Connecting to Wokwi-GUEST .... connected, IP=10.13.37.2
   [SCHED] synced 3 entries from bot
     - 08:00  1/2/1
     - 14:00  1/0/1
     - 20:00  2/1/0
   === Ready ===
   [IDLE] HH:MM:SS  wifi=1
   ```
5. From the caregiver Telegram chat, send `/dispense_stage 1 2`. Within
   2 seconds the Wokwi serial monitor should print:
   ```
   [CMD] dispense_stage (id=42)
   [DOSE] manual-stage start  2/0/0
   [STAGE 1] dispensing 2 pill(s)
   [STAGE 1] slot 1 advanced — waiting for drop...
   ```
   **In the Wokwi window, click the green DROP 1 button twice** to simulate
   pills falling into the tray (the original firmware uses these buttons as
   stand-ins for IR drop sensors). The dose then completes and both your
   Telegram accounts get notifications.
6. To test a full three-stage dose, send `/dispense`. You'll need to press
   the matching DROP button(s) once per pill in each stage. If you let the
   drop-timeout (3 s) elapse 8 times without pressing the button, the
   firmware will fault with `empty_bottle` and the caregiver receives a
   🚨 fault alert.

---

## Part F — End-to-end smoke test

From the caregiver Telegram chat:

| Step | Command | Expected |
| ---- | ------- | -------- |
| 1 | `/status` | Shows last event + next dose |
| 2 | `/schedule` | Lists morning/afternoon/night |
| 3 | `/dispense` | Wokwi runs all three stages; both accounts get notifications |
| 4 | `/set_schedule morning 09:00 1 1 1` | Confirms update; Wokwi logs `[schedule] loaded 3 entries` on next poll |
| 5 | `/history 1` | Lists today's events |
| 6 | `/adherence` | 7-day summary |

From the patient account:
- After step 3 completes, send `/confirm`. The caregiver receives
  "Patient confirmed dose at HH:MM."

---

## Troubleshooting

**Railway build fails with `ModuleNotFoundError: telegram`.**
The root `requirements.txt` includes the inner file with `-r
medic_aid_bot/requirements.txt`. Make sure both files are committed and
pushed.

**`/api/health` returns 502.**
Check Railway logs. Most often `TELEGRAM_BOT_TOKEN` is missing or wrong —
the app refuses to start without one.

**Wokwi serial shows `[event] failed to post …` repeatedly.**
- Wrong `BOT_BASE_URL` (include `https://`, no trailing slash).
- Wrong or missing `DEVICE_TOKEN` — must match Railway exactly.
- Railway service is asleep on the free plan — visit `/api/health` once
  to wake it.

**Bot replies "This command is only available to caregivers."**
You're sending caregiver commands from the patient account. Either link a
caregiver account (Part D step 3) or `/start` over and pick the caregiver
role.

**Events fire but no Telegram notifications arrive.**
The notifier fans out to *every* registered patient / caregiver. If neither
role has registered yet, the events are still logged but nobody is
notified. Run Part D first.

**SQLite resets every redeploy.**
Add a Railway volume mounted at `/data` and set
`DATABASE_PATH=/data/medicaid.db` (Part C step 6).

---

## What the data flow looks like once live

```
Telegram user ──/dispense──► Bot ──INSERT command_queue──► SQLite
                                                              ▲
                                                              │ every 2s
                                                              │
                                            Wokwi ESP32 ──GET /api/poll──┘
                                                  │
                                                  ├── runs motors
                                                  │
                                                  └─POST /api/event──► Bot ──send_message──► Telegram patient + caregiver
```
