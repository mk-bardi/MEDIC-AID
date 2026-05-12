// MEDIC-AID dispenser firmware — Arduino/C++ for ESP32 (Wokwi-compatible).
//
// This is a reference skeleton that talks to the Python bot over HTTPS.
// Replace the pin numbers and state-machine details with your own dispenser
// hardware. The networking and bot-protocol bits are the parts you should
// keep largely as-is.
//
// Required libraries (install via Arduino IDE Library Manager or PlatformIO):
//   - WiFi             (built in to ESP32 core)
//   - HTTPClient       (built in to ESP32 core)
//   - WiFiClientSecure (built in)
//   - ArduinoJson      (Benoit Blanchon)
//
// In Wokwi the simulated Wi-Fi uses SSID "Wokwi-GUEST" with an empty password.

#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <ArduinoJson.h>

// ---------- USER CONFIG ----------
const char* WIFI_SSID     = "Wokwi-GUEST";
const char* WIFI_PASSWORD = "";

// Set this to your Railway URL (no trailing slash).
const char* BOT_BASE_URL  = "https://YOUR-APP.up.railway.app";

// Must match DEVICE_TOKEN in the bot's Railway env vars.
const char* DEVICE_TOKEN  = "REPLACE-WITH-LONG-RANDOM-STRING";

const unsigned long POLL_INTERVAL_MS = 2000;   // poll every 2s
// ---------------------------------

unsigned long lastPollMs = 0;

struct ScheduleEntry {
  String slot;
  int hour;
  int minute;
  int p1, p2, p3;
};
ScheduleEntry schedule[3];
int scheduleCount = 0;

// ---------- Wi-Fi ----------
void connectWifi() {
  Serial.printf("Connecting to %s ", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
    Serial.print(".");
  }
  Serial.printf(" connected, IP=%s\n", WiFi.localIP().toString().c_str());
}

// ---------- HTTP helpers ----------
// Wokwi's TLS is permissive; for production use a real CA bundle.
WiFiClientSecure makeTlsClient() {
  WiFiClientSecure client;
  client.setInsecure();
  return client;
}

bool httpGet(const String& path, String& outBody) {
  WiFiClientSecure client = makeTlsClient();
  HTTPClient http;
  String url = String(BOT_BASE_URL) + path;
  if (!http.begin(client, url)) return false;
  http.addHeader("X-Device-Token", DEVICE_TOKEN);
  int code = http.GET();
  if (code <= 0) { http.end(); return false; }
  outBody = http.getString();
  http.end();
  return code == 200;
}

bool httpPostJson(const String& path, const String& body) {
  WiFiClientSecure client = makeTlsClient();
  HTTPClient http;
  String url = String(BOT_BASE_URL) + path;
  if (!http.begin(client, url)) return false;
  http.addHeader("X-Device-Token", DEVICE_TOKEN);
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(body);
  http.end();
  return code >= 200 && code < 300;
}

// ---------- Event reporting ----------
void postEvent(const char* eventType,
               int stage = -1,
               int pillsTarget = -1,
               int pillsActual = -1,
               const char* faultReason = nullptr) {
  StaticJsonDocument<256> doc;
  doc["event_type"] = eventType;
  if (stage >= 0)        doc["stage"] = stage;
  if (pillsTarget >= 0)  doc["pills_target"] = pillsTarget;
  if (pillsActual >= 0)  doc["pills_actual"] = pillsActual;
  if (faultReason)       doc["fault_reason"] = faultReason;
  String body;
  serializeJson(doc, body);
  if (!httpPostJson("/api/event", body)) {
    Serial.printf("[event] failed to post %s\n", eventType);
  }
}

void postCommandResult(int commandId, const char* status, const char* detail) {
  StaticJsonDocument<128> doc;
  doc["command_id"] = commandId;
  doc["status"] = status;
  if (detail) doc["detail"] = detail;
  String body; serializeJson(doc, body);
  httpPostJson("/api/command_result", body);
}

// ---------- Schedule refresh ----------
void fetchSchedule() {
  String body;
  if (!httpGet("/api/schedule", body)) {
    Serial.println("[schedule] fetch failed");
    return;
  }
  StaticJsonDocument<1024> doc;
  if (deserializeJson(doc, body)) return;
  scheduleCount = 0;
  for (JsonObject s : doc["schedules"].as<JsonArray>()) {
    if (scheduleCount >= 3) break;
    schedule[scheduleCount++] = {
      s["slot"].as<String>(),
      s["hour"], s["minute"],
      s["pills_stage1"], s["pills_stage2"], s["pills_stage3"]
    };
  }
  Serial.printf("[schedule] loaded %d entries\n", scheduleCount);
}

// ---------- Dispenser hardware stubs ----------
// Replace these with your actual servo/motor/sensor code from the Wokwi project.
bool dispenseStage(int stage, int pills) {
  Serial.printf("[motor] dispense stage %d, %d pills\n", stage, pills);
  delay(300 * pills);
  // return false here if the drop sensor doesn't see pills move.
  return true;
}

void runDose(int p1, int p2, int p3) {
  postEvent("dose_started", -1, p1 + p2 + p3);
  int total = 0;
  int stagePills[3] = {p1, p2, p3};
  for (int i = 0; i < 3; i++) {
    if (stagePills[i] <= 0) continue;
    if (!dispenseStage(i + 1, stagePills[i])) {
      postEvent("fault", i + 1, stagePills[i], total, "no_drop_detected");
      return;
    }
    postEvent("stage_dispensed", i + 1, stagePills[i], stagePills[i]);
    total += stagePills[i];
  }
  postEvent("dose_completed", -1, p1 + p2 + p3, total);
}

// ---------- Command dispatch ----------
void handleCommand(JsonObject cmd) {
  String name = cmd["command"].as<String>();
  int id = cmd["command_id"] | -1;
  JsonObject params = cmd["params"].as<JsonObject>();

  if (name == "none") return;

  Serial.printf("[cmd] %s (id=%d)\n", name.c_str(), id);

  if (name == "dispense_all") {
    int p1 = params["stage1"] | 1;
    int p2 = params["stage2"] | 1;
    int p3 = params["stage3"] | 1;
    runDose(p1, p2, p3);
    postCommandResult(id, "executed", "dispense_all done");
  } else if (name == "dispense_stage") {
    int stage = params["stage"] | 1;
    int pills = params["pills"] | 1;
    bool ok = dispenseStage(stage, pills);
    postEvent(ok ? "stage_dispensed" : "fault",
              stage, pills, ok ? pills : 0,
              ok ? nullptr : "no_drop_detected");
    postCommandResult(id, ok ? "executed" : "failed", nullptr);
  } else if (name == "update_schedule") {
    fetchSchedule();
    postCommandResult(id, "executed", "schedule refreshed");
  } else {
    postCommandResult(id, "failed", "unknown command");
  }
}

void pollOnce() {
  String body;
  if (!httpGet("/api/poll", body)) return;
  StaticJsonDocument<512> doc;
  if (deserializeJson(doc, body)) return;
  handleCommand(doc.as<JsonObject>());
}

// ---------- Time-based schedule check ----------
// In production, sync NTP and trigger a dose when the wall clock matches a
// schedule entry. Wokwi has no RTC by default; for the simulation, the easiest
// way to test is to send /dispense from Telegram.
void checkSchedule() { /* TODO: NTP + cron-style match */ }

// ---------- setup / loop ----------
void setup() {
  Serial.begin(115200);
  delay(200);
  connectWifi();
  fetchSchedule();
}

void loop() {
  unsigned long now = millis();
  if (now - lastPollMs >= POLL_INTERVAL_MS) {
    lastPollMs = now;
    pollOnce();
  }
  checkSchedule();
  delay(10);
}
