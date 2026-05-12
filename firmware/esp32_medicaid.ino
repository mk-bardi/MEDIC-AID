/*
 * ============================================================
 *  MEDIC-AID — 3-Stage Automated Pill Dispenser
 *  Wokwi Simulation Firmware (ESP32) — Bot-Connected build
 *  Author: MKBARDI
 * ============================================================
 *
 *  This is the original MEDIC-AID firmware merged with the
 *  Telegram-bot HTTP protocol (poll/event/schedule/command_result).
 *  All hardware behaviour from the original sketch is preserved:
 *
 *    Stage 1 stepper : GPIO 14, 27, 26, 25
 *    Stage 2 stepper : GPIO 33, 32, 18, 19
 *    Stage 3 stepper : GPIO  5, 17, 16,  4
 *    Drop sensors    : GPIO 34 (1), 35 (2), 36 (3)
 *    RTC (I2C)       : SDA=21, SCL=22
 *    Buzzer          : GPIO 23
 *    Status LED      : GPIO 13
 *
 *  Required Wokwi libraries (Library Manager → "+"):
 *    RTClib            (Adafruit)
 *    Stepper           (Arduino built-in for ESP32 core)
 *    ArduinoJson       (Benoit Blanchon)
 *  WiFi / HTTPClient / WiFiClientSecure ship with the ESP32 core.
 * ============================================================
 */

#include <Wire.h>
#include <RTClib.h>
#include <Stepper.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <ArduinoJson.h>

// ---------- USER CONFIG (edit these two lines) ----------
const char* BOT_BASE_URL = "https://YOUR-APP.up.railway.app";
const char* DEVICE_TOKEN = "REPLACE-WITH-SAME-DEVICE-TOKEN-AS-RAILWAY";
// Wokwi simulated Wi-Fi:
const char* WIFI_SSID     = "Wokwi-GUEST";
const char* WIFI_PASSWORD = "";
// ---------------------------------------------------------

// ---------- CONFIG ----------
const int STEPS_PER_REV  = 200;
const int STEPS_PER_SLOT = 50;
const int MAX_SLOTS_PER_DOSE = 8;
const unsigned long DROP_TIMEOUT_MS = 3000;
const unsigned long ALERT_DURATION_MS = 5000;
const unsigned long POLL_INTERVAL_MS = 2000;

// ---------- PINS ----------
const int STEPPER_PINS[3][4] = {
  {14, 27, 26, 25},
  {33, 32, 18, 19},
  { 5, 17, 16,  4}
};
const int DROP_PINS[3] = {34, 35, 36};
const int BUZZER_PIN = 23;
const int LED_PIN    = 13;

// ---------- OBJECTS ----------
RTC_DS1307 rtc;
Stepper steppers[3] = {
  Stepper(STEPS_PER_REV, STEPPER_PINS[0][0], STEPPER_PINS[0][1], STEPPER_PINS[0][2], STEPPER_PINS[0][3]),
  Stepper(STEPS_PER_REV, STEPPER_PINS[1][0], STEPPER_PINS[1][1], STEPPER_PINS[1][2], STEPPER_PINS[1][3]),
  Stepper(STEPS_PER_REV, STEPPER_PINS[2][0], STEPPER_PINS[2][1], STEPPER_PINS[2][2], STEPPER_PINS[2][3])
};

// ---------- SCHEDULE ----------
struct DoseSchedule {
  int hour;
  int minute;
  int pills[3];
};
DoseSchedule schedule[] = {
  { 8, 0, 1, 2, 1},
  {14, 0, 1, 0, 1},
  {20, 0, 2, 1, 0}
};
const int NUM_DOSES = sizeof(schedule) / sizeof(schedule[0]);

// Whichever dose is currently running (copied from schedule[] or built from a
// bot command). Letting the state machine work off this copy means manual
// commands and scheduled doses share the same code path.
DoseSchedule activeDose = {0, 0, {0, 0, 0}};

// ---------- STATE ----------
enum State { IDLE, ALERT_PATIENT, DISPENSE_STAGE, WAIT_FOR_DROP, COMPLETE, FAULT };
State state = IDLE;

int currentStage = 0;
int pillsTarget = 0;
int pillsDropped = 0;
int slotsAdvanced = 0;
int lastTriggeredMinute = -1;
unsigned long stateEnteredAt = 0;
unsigned long lastDropEdgeAt = 0;
int lastDropReading[3] = {HIGH, HIGH, HIGH};

// Track which bot command (if any) launched the current dose so we can post
// /api/command_result when it finishes. -1 means scheduled / no command.
int activeCommandId = -1;

// Last fault reason for reporting.
const char* lastFaultReason = "unknown";
int lastFaultStage = 0;

// ---------- HELPERS ----------
void enterState(State s) {
  state = s;
  stateEnteredAt = millis();
}

void buzz(int durationMs) {
  digitalWrite(BUZZER_PIN, HIGH);
  delay(durationMs);
  digitalWrite(BUZZER_PIN, LOW);
}

bool checkDropEdge(int stage) {
  int reading = digitalRead(DROP_PINS[stage]);
  bool edge = (lastDropReading[stage] == HIGH && reading == LOW);
  lastDropReading[stage] = reading;
  if (edge && millis() - lastDropEdgeAt > 100) {
    lastDropEdgeAt = millis();
    return true;
  }
  return false;
}

int findDueDose(const DateTime& now) {
  for (int i = 0; i < NUM_DOSES; i++) {
    if (now.hour() == schedule[i].hour && now.minute() == schedule[i].minute) {
      return i;
    }
  }
  return -1;
}

// =========================================================
//   Wi-Fi + bot HTTP helpers
// =========================================================
bool wifiReady = false;
unsigned long lastPollMs = 0;

void connectWifi() {
  Serial.printf("[WIFI] Connecting to %s ", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    delay(250);
    Serial.print(".");
  }
  wifiReady = (WiFi.status() == WL_CONNECTED);
  if (wifiReady) {
    Serial.printf(" connected, IP=%s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println(" FAILED — continuing offline");
  }
}

// Wokwi's TLS is permissive in simulation; for production replace setInsecure()
// with a real CA bundle.
bool httpGet(const String& path, String& outBody) {
  if (!wifiReady) return false;
  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient http;
  String url = String(BOT_BASE_URL) + path;
  if (!http.begin(client, url)) return false;
  http.addHeader("X-Device-Token", DEVICE_TOKEN);
  int code = http.GET();
  if (code != 200) {
    if (code <= 0) Serial.printf("[HTTP] GET %s failed: %d\n", path.c_str(), code);
    http.end();
    return false;
  }
  outBody = http.getString();
  http.end();
  return true;
}

bool httpPostJson(const String& path, const String& body) {
  if (!wifiReady) return false;
  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient http;
  String url = String(BOT_BASE_URL) + path;
  if (!http.begin(client, url)) return false;
  http.addHeader("X-Device-Token", DEVICE_TOKEN);
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(body);
  http.end();
  if (code < 200 || code >= 300) {
    Serial.printf("[HTTP] POST %s -> %d\n", path.c_str(), code);
    return false;
  }
  return true;
}

void postEvent(const char* eventType,
               int stage = -1,
               int pillsTargetVal = -1,
               int pillsActual = -1,
               const char* faultReason = nullptr) {
  StaticJsonDocument<256> doc;
  doc["event_type"] = eventType;
  if (stage >= 0)          doc["stage"] = stage + 1;       // human-friendly 1-indexed
  if (pillsTargetVal >= 0) doc["pills_target"] = pillsTargetVal;
  if (pillsActual >= 0)    doc["pills_actual"] = pillsActual;
  if (faultReason)         doc["fault_reason"] = faultReason;
  String body;
  serializeJson(doc, body);
  httpPostJson("/api/event", body);
}

void postCommandResult(int commandId, const char* status, const char* detail) {
  if (commandId < 0) return;
  StaticJsonDocument<160> doc;
  doc["command_id"] = commandId;
  doc["status"] = status;
  if (detail) doc["detail"] = detail;
  String body; serializeJson(doc, body);
  httpPostJson("/api/command_result", body);
}

// =========================================================
//   Schedule sync from the bot
// =========================================================
void fetchSchedule() {
  String body;
  if (!httpGet("/api/schedule", body)) return;
  StaticJsonDocument<1024> doc;
  if (deserializeJson(doc, body)) {
    Serial.println("[SCHED] parse failed");
    return;
  }
  JsonArray arr = doc["schedules"].as<JsonArray>();
  int i = 0;
  for (JsonObject s : arr) {
    if (i >= NUM_DOSES) break;
    schedule[i].hour     = s["hour"]   | schedule[i].hour;
    schedule[i].minute   = s["minute"] | schedule[i].minute;
    schedule[i].pills[0] = s["pills_stage1"] | schedule[i].pills[0];
    schedule[i].pills[1] = s["pills_stage2"] | schedule[i].pills[1];
    schedule[i].pills[2] = s["pills_stage3"] | schedule[i].pills[2];
    i++;
  }
  Serial.printf("[SCHED] synced %d entries from bot\n", i);
  for (int j = 0; j < i; j++) {
    Serial.printf("  - %02d:%02d  %d/%d/%d\n",
                  schedule[j].hour, schedule[j].minute,
                  schedule[j].pills[0], schedule[j].pills[1], schedule[j].pills[2]);
  }
}

// =========================================================
//   Dose lifecycle (works for both scheduled + manual doses)
// =========================================================
void startActiveDose(const char* sourceLabel) {
  currentStage = 0;
  while (currentStage < 3 && activeDose.pills[currentStage] == 0) currentStage++;
  if (currentStage >= 3) {
    Serial.println("[DOSE] no pills in any stage — skipping");
    postCommandResult(activeCommandId, "failed", "no pills configured");
    activeCommandId = -1;
    enterState(IDLE);
    return;
  }
  Serial.printf("[DOSE] %s start  %d/%d/%d\n",
                sourceLabel,
                activeDose.pills[0], activeDose.pills[1], activeDose.pills[2]);

  StaticJsonDocument<192> doc;
  doc["event_type"] = "dose_started";
  doc["pills_stage1"] = activeDose.pills[0];
  doc["pills_stage2"] = activeDose.pills[1];
  doc["pills_stage3"] = activeDose.pills[2];
  String body; serializeJson(doc, body);
  httpPostJson("/api/event", body);

  digitalWrite(LED_PIN, HIGH);
  enterState(ALERT_PATIENT);
}

void startStage(int stage) {
  pillsTarget = activeDose.pills[stage];
  pillsDropped = 0;
  slotsAdvanced = 0;
  Serial.printf("[STAGE %d] dispensing %d pill(s)\n", stage + 1, pillsTarget);
  enterState(DISPENSE_STAGE);
}

void advanceToNextStage() {
  postEvent("stage_dispensed", currentStage, pillsTarget, pillsDropped);
  currentStage++;
  while (currentStage < 3 && activeDose.pills[currentStage] == 0) currentStage++;
  if (currentStage >= 3) {
    int total = activeDose.pills[0] + activeDose.pills[1] + activeDose.pills[2];
    Serial.println("[DOSE] all stages complete");
    postEvent("dose_completed", -1, total, total);
    if (activeCommandId >= 0) {
      postCommandResult(activeCommandId, "executed", "dose completed");
      activeCommandId = -1;
    }
    digitalWrite(LED_PIN, LOW);
    enterState(COMPLETE);
  } else {
    startStage(currentStage);
  }
}

// =========================================================
//   Command dispatch (called from pollOnce)
// =========================================================
void handleBotCommand(JsonDocument& doc) {
  const char* name = doc["command"] | "none";
  int cmdId = doc["command_id"] | -1;
  JsonObject params = doc["params"].as<JsonObject>();

  if (strcmp(name, "none") == 0) return;

  if (state != IDLE) {
    Serial.printf("[CMD] %s ignored — dispenser busy\n", name);
    postCommandResult(cmdId, "failed", "device busy");
    return;
  }

  Serial.printf("[CMD] %s (id=%d)\n", name, cmdId);

  if (strcmp(name, "dispense_all") == 0) {
    activeDose.hour = 0; activeDose.minute = 0;
    activeDose.pills[0] = params["stage1"] | 1;
    activeDose.pills[1] = params["stage2"] | 1;
    activeDose.pills[2] = params["stage3"] | 1;
    activeCommandId = cmdId;
    startActiveDose("manual-all");
  }
  else if (strcmp(name, "dispense_stage") == 0) {
    int stage = (params["stage"] | 1) - 1;   // bot sends 1-indexed
    int pills = params["pills"] | 1;
    if (stage < 0 || stage > 2) {
      postCommandResult(cmdId, "failed", "invalid stage");
      return;
    }
    activeDose.hour = 0; activeDose.minute = 0;
    activeDose.pills[0] = 0; activeDose.pills[1] = 0; activeDose.pills[2] = 0;
    activeDose.pills[stage] = pills;
    activeCommandId = cmdId;
    startActiveDose("manual-stage");
  }
  else if (strcmp(name, "update_schedule") == 0) {
    fetchSchedule();
    postCommandResult(cmdId, "executed", "schedule refreshed");
  }
  else {
    Serial.printf("[CMD] unknown: %s\n", name);
    postCommandResult(cmdId, "failed", "unknown command");
  }
}

void pollOnce() {
  String body;
  if (!httpGet("/api/poll", body)) return;
  StaticJsonDocument<512> doc;
  if (deserializeJson(doc, body)) {
    Serial.println("[POLL] parse failed");
    return;
  }
  handleBotCommand(doc);
}

// =========================================================
//   setup / loop
// =========================================================
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== MEDIC-AID (bot-connected) booting ===");

  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(LED_PIN, OUTPUT);
  for (int i = 0; i < 3; i++) {
    pinMode(DROP_PINS[i], INPUT_PULLUP);
    steppers[i].setSpeed(30);
  }

  Wire.begin(21, 22);
  if (!rtc.begin()) {
    Serial.println("[ERR] RTC not found!");
    while (1) delay(1000);
  }
  if (!rtc.isrunning()) {
    Serial.println("[RTC] not running — setting to compile time");
    rtc.adjust(DateTime(F(__DATE__), F(__TIME__)));
  }

  connectWifi();
  fetchSchedule();
  Serial.println("=== Ready ===\n");
}

void loop() {
  DateTime now = rtc.now();

  // Poll the bot at most every POLL_INTERVAL_MS. Only when IDLE so we never
  // interrupt an in-progress dose with a slow HTTPS round-trip.
  if (state == IDLE && wifiReady && millis() - lastPollMs >= POLL_INTERVAL_MS) {
    lastPollMs = millis();
    pollOnce();
  }

  switch (state) {

    case IDLE: {
      int due = findDueDose(now);
      if (due >= 0 && now.minute() != lastTriggeredMinute) {
        lastTriggeredMinute = now.minute();
        activeDose = schedule[due];
        activeCommandId = -1;       // not a bot-triggered dose
        startActiveDose("scheduled");
      }
      static unsigned long lastPrint = 0;
      if (millis() - lastPrint > 5000) {
        lastPrint = millis();
        Serial.printf("[IDLE] %02d:%02d:%02d  wifi=%d\n",
                      now.hour(), now.minute(), now.second(), wifiReady);
      }
      break;
    }

    case ALERT_PATIENT: {
      static unsigned long lastBeep = 0;
      if (millis() - lastBeep > 800) {
        lastBeep = millis();
        buzz(150);
      }
      if (millis() - stateEnteredAt >= ALERT_DURATION_MS) {
        startStage(currentStage);
      }
      break;
    }

    case DISPENSE_STAGE: {
      steppers[currentStage].step(STEPS_PER_SLOT);
      slotsAdvanced++;
      Serial.printf("[STAGE %d] slot %d advanced — waiting for drop...\n",
                    currentStage + 1, slotsAdvanced);
      lastDropReading[currentStage] = digitalRead(DROP_PINS[currentStage]);
      enterState(WAIT_FOR_DROP);
      break;
    }

    case WAIT_FOR_DROP: {
      if (checkDropEdge(currentStage)) {
        pillsDropped++;
        Serial.printf("[STAGE %d] pill detected (%d/%d)\n",
                      currentStage + 1, pillsDropped, pillsTarget);
        if (pillsDropped >= pillsTarget) {
          advanceToNextStage();
        } else {
          enterState(DISPENSE_STAGE);
        }
      } else if (millis() - stateEnteredAt > DROP_TIMEOUT_MS) {
        if (slotsAdvanced >= MAX_SLOTS_PER_DOSE) {
          lastFaultStage = currentStage;
          lastFaultReason = (pillsDropped == 0) ? "empty_bottle" : "jam_suspected";
          Serial.printf("[STAGE %d] FAULT — %s after %d slots\n",
                        currentStage + 1, lastFaultReason, slotsAdvanced);
          postEvent("fault", currentStage, pillsTarget, pillsDropped, lastFaultReason);
          if (activeCommandId >= 0) {
            postCommandResult(activeCommandId, "failed", lastFaultReason);
            activeCommandId = -1;
          }
          enterState(FAULT);
        } else {
          enterState(DISPENSE_STAGE);
        }
      }
      break;
    }

    case COMPLETE: {
      buzz(100); delay(100);
      buzz(100); delay(100);
      buzz(100);
      enterState(IDLE);
      break;
    }

    case FAULT: {
      digitalWrite(LED_PIN, HIGH);
      buzz(800);
      digitalWrite(LED_PIN, LOW);
      delay(400);
      if (millis() - stateEnteredAt > 10000) enterState(IDLE);
      break;
    }
  }
}

/* ============================================================
 *  ESP32-C3 BEETLE PIN MIGRATION (for real hardware)
 *  The C3 has limited GPIOs. Suggested remap:
 *    Stage 1 stepper : GPIO  0,  1,  2,  3
 *    Stage 2 stepper : GPIO  4,  5,  6,  7
 *    Stage 3 stepper : GPIO 10, 18, 19, 20
 *    Drop sensors    : GPIO  8,  9, 21
 *    RTC (I2C)       : SDA=8,  SCL=9
 *    Buzzer / LED    : remaining pins
 * ============================================================ */
