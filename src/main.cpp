#include <Arduino.h>
#include <ArduinoJson.h>
#include <LittleFS.h>
#include <LiquidCrystal_I2C.h>
#include <MFRC522.h>
#include <MQTTClient.h>
#include <Preferences.h>
#include <HX711.h>
#include <SPI.h>
#include <WiFi.h>
#include <time.h>

#ifdef WOKWI_SIM
#include <WiFiClient.h>
#define WIFI_SSID "Wokwi-GUEST"
#define WIFI_PASSWORD ""
#define MQTT_HOST "host.wokwi.internal"
#define MQTT_PORT 1883
#define SITE_ID "site01"
#define LOCKER_ID "locker01"
#else
#include <WiFiClientSecure.h>
#include <time.h>
#include "secrets.h"
#endif

// One compartment; pins map to Wokwi virtual parts documented in include/HARDWARE.md.
constexpr uint8_t RFID_SS = 5, RFID_RST = 27, REED_PIN = 32;
constexpr uint8_t LOCK_PIN = 26, GREEN_LED = 25, RED_LED = 33;
constexpr uint8_t BUZZER_PIN = 4, OCCUPANCY_LIGHT_PIN = 14, PIR_PIN = 34, SMOKE_PIN = 35;
constexpr uint8_t SCALE_DT_PIN = 16, SCALE_SCK_PIN = 17;
constexpr uint32_t LOCK_PULSE_MS = 500;
constexpr uint32_t AUTH_TIMEOUT_MS = 5000;
constexpr uint32_t WAIT_OPEN_MS = 30000;
constexpr uint32_t DOOR_OPEN_LIMIT_MS = 30000;
constexpr uint32_t DEBOUNCE_MS = 50;
constexpr uint32_t CARD_COOLDOWN_MS = 1200;
constexpr uint32_t CONNECT_RETRY_MS = 1000;
constexpr uint32_t TELEMETRY_INTERVAL_MS = 30000;
constexpr uint32_t EVENT_RETRY_MS = 2000;
constexpr uint8_t EVENT_QUEUE_LIMIT = 64;
constexpr uint32_t OCCUPANCY_LIGHT_MS = 30000;
constexpr char COMPARTMENT_ID[] = "A01";
constexpr char EVENT_QUEUE_FILE[] = "/events.jsonl";
constexpr char EVENT_QUEUE_TMP[] = "/events.tmp";
constexpr char EVENT_QUEUE_BAK[] = "/events.bak";

enum class Phase : uint8_t { Idle, WaitingAuth, WaitingDoor, DoorOpen, MaintenanceWaitingDoor, MaintenanceDoorOpen };
Phase phase = Phase::Idle;
MFRC522 reader(RFID_SS, RFID_RST);
LiquidCrystal_I2C lcd(0x27, 16, 2);
HX711 scale;
Preferences prefs;
#ifdef WOKWI_SIM
WiFiClient transport;
#else
WiFiClientSecure transport;
#endif
MQTTClient mqtt(4096);
const String prefix = String("smartlocker/") + SITE_ID + "/" + LOCKER_ID + "/";

String pendingRequest, pendingUid, activeRequest, lastNonce, packageUid;
String activeOfflinePermission;
String offlinePermissionsJson = "[]";
uint32_t activeOfflineAuthorizedAt = 0;
float weightBeforeKg = 0.0f;
uint32_t bootNumber = 0, requestSequence = 0, eventSequence = 0;
uint32_t authAt = 0, grantedAt = 0, openedAt = 0, cardAt = 0, connectAt = 0;
uint32_t telemetryAt = 0, eventRetryAt = 0, sequence = 0, configVersion = 0;
uint32_t motionAt = 0;
uint32_t screenActivityAt = 0;
uint32_t doorOpenLimitMs = DOOR_OPEN_LIMIT_MS;
uint8_t mqttBackoffSeconds = 1;
uint32_t rawChangedAt = 0;
bool rawDoorOpen = false, stableDoorOpen = false, leftOpenReported = false;
bool wifiStarted = false;
bool smokeAlarmActive = false;
bool timeSyncRequested = false;
uint32_t timeSyncAt = 0;

bool offlinePermissionWasUsed(const String& permissionId) {
  JsonDocument used;
  if (deserializeJson(used, prefs.getString("offused", "[]")) || !used.is<JsonArray>()) return true;
  for (JsonVariantConst id : used.as<JsonArrayConst>()) if (String(id.as<const char*>()) == permissionId) return true;
  return false;
}

bool consumeOfflinePermission(const String& permissionId) {
  JsonDocument used;
  if (deserializeJson(used, prefs.getString("offused", "[]")) || !used.is<JsonArray>()) return false;
  for (JsonVariantConst id : used.as<JsonArrayConst>()) if (String(id.as<const char*>()) == permissionId) return false;
  used.add(permissionId);
  String serialized;
  serializeJson(used, serialized);
  return prefs.putString("offused", serialized) == serialized.length() && prefs.getString("offused", "") == serialized;
}

void lockOff() { digitalWrite(LOCK_PIN, LOW); }
void screen(const char* line1, const char* line2 = "") {
  lcd.backlight();
  screenActivityAt = millis();
  lcd.clear(); lcd.setCursor(0, 0); lcd.print(line1); lcd.setCursor(0, 1); lcd.print(line2);
}
void beep(uint16_t frequency, uint16_t duration) { tone(BUZZER_PIN, frequency, duration); }

void lockPulseDone(void*) { lockOff(); }
esp_timer_handle_t lockTimer = nullptr;

String makeRequestId() {
  return String(LOCKER_ID) + "-" + bootNumber + "-req-" + ++requestSequence;
}

String makeEventId() {
  // Persist before use so reboot cannot reuse an event ID.
  ++eventSequence;
  if (prefs.putUInt("eventSeq", eventSequence) != sizeof(uint32_t)) return "";
  return String(LOCKER_ID) + "-evt-" + eventSequence;
}

uint8_t queuedEventCount() {
  if (!LittleFS.exists(EVENT_QUEUE_FILE)) return 0;
  File f = LittleFS.open(EVENT_QUEUE_FILE, FILE_READ);
  if (!f) return EVENT_QUEUE_LIMIT;
  uint8_t count = 0;
  while (f.available()) if (f.read() == '\n' && count < EVENT_QUEUE_LIMIT) ++count;
  f.close();
  return count;
}

bool queueEvent(const String& payload) {
  if (queuedEventCount() >= EVENT_QUEUE_LIMIT) {
    Serial.println("Event queue full; event was not persisted");
    digitalWrite(RED_LED, HIGH);
    return false;
  }
  File f = LittleFS.open(EVENT_QUEUE_FILE, FILE_APPEND, true);
  if (!f) { Serial.println("Event queue write failed"); return false; }
  bool ok = f.println(payload) > 0;
  f.flush();
  f.close();
  return ok;
}

void acknowledgeEvent(const String& eventId) {
  if (!LittleFS.exists(EVENT_QUEUE_FILE)) return;
  File in = LittleFS.open(EVENT_QUEUE_FILE, FILE_READ);
  File out = LittleFS.open(EVENT_QUEUE_TMP, FILE_WRITE, true);
  if (!in || !out) { if (in) in.close(); if (out) out.close(); return; }
  while (in.available()) {
    String line = in.readStringUntil('\n');
    JsonDocument doc;
    if (deserializeJson(doc, line) || String(doc["event_id"] | "") != eventId) out.println(line);
  }
  in.close(); out.flush(); out.close();
  LittleFS.remove(EVENT_QUEUE_BAK);
  if (!LittleFS.rename(EVENT_QUEUE_FILE, EVENT_QUEUE_BAK)) {
    LittleFS.remove(EVENT_QUEUE_TMP);
    Serial.println("Event queue rotation failed; keeping original queue");
    return;
  }
  if (!LittleFS.rename(EVENT_QUEUE_TMP, EVENT_QUEUE_FILE)) {
    LittleFS.rename(EVENT_QUEUE_BAK, EVENT_QUEUE_FILE);
    Serial.println("Event queue replacement failed; restored original queue");
    return;
  }
  LittleFS.remove(EVENT_QUEUE_BAK);
}

void flushEventQueue() {
  if (!mqtt.connected() || millis() - eventRetryAt < EVENT_RETRY_MS || !LittleFS.exists(EVENT_QUEUE_FILE)) return;
  eventRetryAt = millis();
  File f = LittleFS.open(EVENT_QUEUE_FILE, FILE_READ);
  if (!f) return;
  String line = f.readStringUntil('\n');
  f.close();
  if (line.isEmpty()) return;
  JsonDocument doc;
  if (deserializeJson(doc, line)) { Serial.println("Invalid event queue record"); return; }
  if (mqtt.publish((prefix + "event").c_str(), line, false, 1))
    Serial.printf("Event sent; awaiting DB ACK: %s\n", String(doc["event_id"] | "?").c_str());
}

void publishEvent(const char* kind, const String& requestId, const String& itemUid = "", float beforeKg = 0.0f, float afterKg = 0.0f) {
  JsonDocument doc;
  String eventId = makeEventId();
  if (eventId.isEmpty()) {
    Serial.println("NVS error: event not sent");
    digitalWrite(RED_LED, HIGH);
    return;
  }
  doc["event_id"] = eventId;
  doc["request_id"] = requestId;
  doc["locker_id"] = LOCKER_ID;
  doc["compartment_id"] = COMPARTMENT_ID;
  doc["event"] = kind;
  if (requestId.startsWith("maint-")) doc["source"] = "maintenance";
  ++sequence;
  if (prefs.putUInt("sequence", sequence) != sizeof(uint32_t)) {
    Serial.println("NVS sequence error: event not queued");
    digitalWrite(RED_LED, HIGH);
    return;
  }
  doc["sequence"] = sequence;
  doc["uptime_ms"] = millis();
  if (!activeOfflinePermission.isEmpty()) {
    doc["offline_permission_id"] = activeOfflinePermission;
    doc["authorized_at"] = activeOfflineAuthorizedAt;
  }
  if (strcmp(kind, "door_closed") == 0) {
    doc["package_uid"] = itemUid;
    doc["weight_before"] = beforeKg;
    doc["weight_after"] = afterKg;
    Serial.printf("Package evidence: UID=%s weight_before=%.3f kg weight_after=%.3f kg delta=%.3f kg\n",
                  itemUid.c_str(), beforeKg, afterKg, afterKg - beforeKg);
  }
  String body;
  serializeJson(doc, body);
  bool queued = queueEvent(body);
  Serial.printf("Event %s: %s\n", kind, queued ? "queued" : "queue failed");
  flushEventQueue();
}

void setIdle();

bool tryOfflineAccess(const String& uid, uint32_t now) {
  JsonDocument cached;
  if (deserializeJson(cached, offlinePermissionsJson) || !cached.is<JsonArray>()) {
    Serial.println("Offline access unavailable: no synchronized permissions");
    return false;
  }
  time_t nowEpoch = time(nullptr);
  JsonObjectConst grant;
  for (JsonObjectConst candidate : cached.as<JsonArrayConst>()) {
    if (String(candidate["uid"] | "").equalsIgnoreCase(uid) &&
        String(candidate["compartment_id"] | "") == COMPARTMENT_ID &&
        (uint32_t)(candidate["expires_at"] | 0) > (uint32_t)nowEpoch && nowEpoch > 1700000000) {
      String permissionId = candidate["permission_id"] | "";
      if (permissionId.isEmpty() || offlinePermissionWasUsed(permissionId)) continue;
      grant = candidate;
      break;
    }
  }
  if (grant.isNull()) {
    Serial.println("Offline access denied: no valid permission or trusted clock");
    digitalWrite(RED_LED, HIGH); beep(380, 180); screen("Offline denied", "No valid grant");
    return false;
  }
  activeOfflinePermission = String(grant["permission_id"] | "");
  activeOfflineAuthorizedAt = (uint32_t)nowEpoch;
  String action = grant["action"] | "";
  String order = grant["order_id"] | "";
  if ((action != "deposit" && action != "pickup") || order.isEmpty()) {
    activeOfflinePermission = ""; activeOfflineAuthorizedAt = 0;
    Serial.println("Offline access blocked: invalid synchronized permission");
    return false;
  }
  if (!consumeOfflinePermission(activeOfflinePermission)) {
    activeOfflinePermission = ""; activeOfflineAuthorizedAt = 0;
    Serial.println("Offline access blocked: could not persist one-time use");
    return false;
  }
  pendingRequest = "";
  pendingUid = "";
  packageUid = "";
  weightBeforeKg = scale.is_ready() ? scale.get_units(5) : 0.0f;
  activeRequest = "offline-" + activeOfflinePermission;
  phase = Phase::WaitingDoor; grantedAt = now; leftOpenReported = false;
  digitalWrite(RED_LED, LOW); digitalWrite(GREEN_LED, HIGH); digitalWrite(LOCK_PIN, HIGH);
  beep(1600, 100); screen(action == "deposit" ? "Offline deposit" : "Offline pickup", "Open Door now");
  publishEvent("auth_ok", activeRequest);
  if (!lockTimer || esp_timer_start_once(lockTimer, LOCK_PULSE_MS * 1000ULL) != ESP_OK) {
    lockOff(); digitalWrite(RED_LED, HIGH); publishEvent("unlock_timeout", activeRequest); setIdle();
    Serial.println("Offline unlock timer failed");
    return true;
  }
  publishEvent("unlock_pulsed", activeRequest);
  Serial.printf("Offline access granted: %s, %s\n", action.c_str(), order.c_str());
  return true;
}

void setIdle() {
  phase = Phase::Idle;
  pendingRequest = "";
  pendingUid = "";
  activeRequest = "";
  activeOfflinePermission = "";
  activeOfflineAuthorizedAt = 0;
  digitalWrite(GREEN_LED, LOW);
}

void onMessage(String& topic, String& payload) {
  if (topic == prefix + "event/ack") {
    JsonDocument ack;
    if (!deserializeJson(ack, payload) && (ack["accepted"] | false))
      acknowledgeEvent(String(ack["event_id"] | ""));
    return;
  }
  if (topic == prefix + "cmd/config") {
    JsonDocument cfg;
    if (deserializeJson(cfg, payload)) return;
    uint32_t version = cfg["config_version"] | 0;
    uint32_t openLimit = cfg["door_open_limit_ms"] | DOOR_OPEN_LIMIT_MS;
    String commandId = cfg["command_id"] | "";
    JsonDocument ack; ack["command"] = "config"; ack["command_id"] = commandId;
    ack["config_version"] = version;
    if (version == configVersion) {
      ack["accepted"] = true;
      String out; serializeJson(ack, out); mqtt.publish((prefix + "ack").c_str(), out, false, 1);
      return;
    }
    if (version < configVersion || openLimit < 5000 || openLimit > 300000) {
      ack["accepted"] = false;
      String out; serializeJson(ack, out); mqtt.publish((prefix + "ack").c_str(), out, false, 1);
      return;
    }
    String offlineJson = "[]";
    if (cfg["offline_credentials"].is<JsonArray>()) serializeJson(cfg["offline_credentials"], offlineJson);
    bool saved = prefs.putUInt("cfgver", version) == sizeof(uint32_t) &&
                 prefs.putUInt("openlim", openLimit) == sizeof(uint32_t) &&
                 prefs.putString("offperms", offlineJson) == offlineJson.length() &&
                 prefs.getUInt("cfgver", 0) == version && prefs.getUInt("openlim", 0) == openLimit;
    if (!saved) {
      ack["accepted"] = false;
      String out; serializeJson(ack, out); mqtt.publish((prefix + "ack").c_str(), out, false, 1);
      return;
    }
    configVersion = version;
    doorOpenLimitMs = openLimit;
    offlinePermissionsJson = offlineJson;
    ack["accepted"] = true;
    String out; serializeJson(ack, out); mqtt.publish((prefix + "ack").c_str(), out, false, 1);
    Serial.printf("Configuration applied: version %lu\n", (unsigned long)configVersion);
    return;
  }
  if (topic == prefix + "cmd/unlock") {
    JsonDocument cmd;
    if (deserializeJson(cmd, payload)) return;
    String commandId = cmd["command_id"] | "";
    String nonce = cmd["nonce"] | "";
    uint32_t expiry = cmd["expires_at"] | 0;
    time_t nowEpoch = time(nullptr);
    bool allowed = commandId.startsWith("maint-") && !nonce.isEmpty() &&
      nonce != prefs.getString("cmdNonce", "") && cmd["action"] == "maintenance_unlock" &&
      String(cmd["compartment_id"] | "") == COMPARTMENT_ID && nowEpoch > 1700000000 &&
      expiry >= (uint32_t)nowEpoch && expiry <= (uint32_t)nowEpoch + 15 &&
      phase == Phase::Idle && !rawDoorOpen && !stableDoorOpen;
    JsonDocument ack; ack["command"] = "unlock"; ack["command_id"] = commandId;
    ack["accepted"] = allowed;
    if (allowed) {
      prefs.putString("cmdNonce", nonce);
      if (prefs.getString("cmdNonce", "") != nonce) allowed = false;
      ack["accepted"] = allowed;
    }
    if (!allowed) {
      String out; serializeJson(ack, out); mqtt.publish((prefix + "ack").c_str(), out, false, 1);
      Serial.println("Maintenance unlock rejected (expired, replayed, wrong state or invalid clock)");
      return;
    }
    activeRequest = commandId;
    grantedAt = millis();
    leftOpenReported = false;
    phase = Phase::MaintenanceWaitingDoor;
    digitalWrite(GREEN_LED, HIGH); digitalWrite(LOCK_PIN, HIGH); beep(1600, 100);
    screen("Admin unlock", "Open Door now");
    if (!lockTimer || esp_timer_start_once(lockTimer, LOCK_PULSE_MS * 1000ULL) != ESP_OK) {
      lockOff(); setIdle(); ack["accepted"] = false;
    } else {
      publishEvent("unlock_pulsed", activeRequest);
      Serial.printf("Maintenance unlock accepted: %s\n", commandId.c_str());
    }
    String out; serializeJson(ack, out); mqtt.publish((prefix + "ack").c_str(), out, false, 1);
    return;
  }
  if (topic != prefix + "auth/response" || phase != Phase::WaitingAuth) return;
  JsonDocument doc;
  if (deserializeJson(doc, payload)) return;
  if (String(doc["request_id"] | "") != pendingRequest) return;
  if (millis() - authAt > AUTH_TIMEOUT_MS) { setIdle(); return; }

  bool allowed = doc["allow"] | false;
  String compartment = doc["compartment_id"] | "";
  String nonce = doc["nonce"] | "";
  String order = doc["order_id"] | "";
  String action = doc["action"] | "";
  String offlinePermissionId = doc["offline_permission_id"] | "";
  uint32_t authorizationEpoch = doc["authorized_at"] | 0;
  uint32_t ttl = doc["ttl_ms"] | 0;
  if (!allowed || compartment != COMPARTMENT_ID || nonce.isEmpty() ||
      nonce == lastNonce || order.isEmpty() ||
      (action != "deposit" && action != "pickup") || ttl == 0 || ttl > AUTH_TIMEOUT_MS ||
      rawDoorOpen || stableDoorOpen) {
    Serial.println("Authorization denied or invalid response");
    digitalWrite(RED_LED, HIGH);
    beep(380, 180);
    publishEvent("auth_fail", pendingRequest);
    screen("Access denied", "Try again");
    setIdle();
    return;
  }

  lastNonce = nonce;
  prefs.putString("authNonce", nonce);
  if (prefs.getString("authNonce", "") != nonce) {
    Serial.println("NVS failed to persist authorization nonce; unlock blocked");
    publishEvent("auth_fail", pendingRequest);
    setIdle();
    return;
  }
  if (!offlinePermissionId.isEmpty()) {
    if (!consumeOfflinePermission(offlinePermissionId)) {
      Serial.println("NVS failed to consume synchronized offline permission; unlock blocked");
      publishEvent("auth_fail", pendingRequest); setIdle(); return;
    }
  }
  packageUid = "";
  weightBeforeKg = scale.is_ready() ? scale.get_units(5) : 0.0f;
  activeRequest = pendingRequest;
  activeOfflinePermission = offlinePermissionId;
  activeOfflineAuthorizedAt = authorizationEpoch;
  pendingRequest = "";
  pendingUid = "";
  grantedAt = millis();
  leftOpenReported = false;
  phase = Phase::WaitingDoor;
  digitalWrite(RED_LED, LOW);
  digitalWrite(GREEN_LED, HIGH);
  digitalWrite(LOCK_PIN, HIGH);
  uint32_t authToUnlockMs = millis() - authAt;
  beep(1600, 100);
  screen(action == "deposit" ? "Deposit allowed" : "Pickup allowed", "Open Door now");
  publishEvent("auth_ok", activeRequest);
  if (!lockTimer || esp_timer_start_once(lockTimer, LOCK_PULSE_MS * 1000ULL) != ESP_OK) {
    lockOff();
    digitalWrite(RED_LED, HIGH);
    publishEvent("unlock_timeout", activeRequest);
    setIdle();
    Serial.println("Lock timer failed; unlock cancelled");
    return;
  }
  publishEvent("unlock_pulsed", activeRequest);
  Serial.printf("Access granted: %s, %s\n", action.c_str(), order.c_str());
  Serial.printf("PCN01 auth-to-unlock latency: %lu ms\n", (unsigned long)authToUnlockMs);
}

void updateDoor(uint32_t now) {
  bool current = digitalRead(REED_PIN) == HIGH;
  if (current != rawDoorOpen) { rawDoorOpen = current; rawChangedAt = now; }
  if (current == stableDoorOpen || now - rawChangedAt < DEBOUNCE_MS) return;
  stableDoorOpen = current;
  if (current) {
    if (phase == Phase::WaitingDoor) {
      phase = Phase::DoorOpen;
      openedAt = now;
      screen("Door open", "Scan package tag");
      publishEvent("door_open", activeRequest);
    } else if (phase == Phase::MaintenanceWaitingDoor) {
      phase = Phase::MaintenanceDoorOpen;
      openedAt = now;
      screen("Admin door open", "Close Door");
      publishEvent("door_open", activeRequest);
    } else {
      setIdle();
      digitalWrite(RED_LED, HIGH);
      publishEvent("door_forced", "");
      beep(700, 500);
      screen("WARNING", "Forced door");
    }
  } else if (phase == Phase::DoorOpen) {
    float afterKg = scale.is_ready() ? scale.get_units(5) : weightBeforeKg;
    publishEvent("door_closed", activeRequest, packageUid, weightBeforeKg, afterKg);
    activeOfflinePermission = "";
    screen("Door closed", "Syncing event");
    digitalWrite(RED_LED, LOW);
    setIdle();
  } else if (phase == Phase::MaintenanceDoorOpen) {
    publishEvent("door_closed", activeRequest);
    screen("Admin closed", "Event syncing");
    setIdle();
  } else if (phase == Phase::Idle) {
    digitalWrite(RED_LED, LOW);
  }
}

void updateNetwork(uint32_t now) {
  if (now < connectAt) return;
  if (now - connectAt < CONNECT_RETRY_MS) return;
  connectAt = now;
  if (WiFi.status() != WL_CONNECTED) {
    if (!wifiStarted) {
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
      wifiStarted = true;
      Serial.println("Connecting Wi-Fi");
    } else WiFi.reconnect();
    return;
  }
  if (time(nullptr) < 1700000000 && (!timeSyncRequested || now - timeSyncAt >= 30000)) {
#ifdef WOKWI_SIM
    configTime(0, 0, "pool.ntp.org", "time.nist.gov");
#else
    configTime(0, 0, NTP_SERVER);
#endif
    timeSyncRequested = true;
    timeSyncAt = now;
  }
  if (mqtt.connected()) return;
#ifndef WOKWI_SIM
  if (time(nullptr) < 1700000000) return;
#endif
#ifdef WOKWI_SIM
  bool connected = mqtt.connect(LOCKER_ID);
#else
  bool connected = mqtt.connect(LOCKER_ID, MQTT_USER, MQTT_PASSWORD);
#endif
  if (connected) {
    mqtt.subscribe((prefix + "auth/response").c_str(), 1);
    mqtt.subscribe((prefix + "event/ack").c_str(), 1);
    mqtt.subscribe((prefix + "cmd/config").c_str(), 1);
    mqtt.subscribe((prefix + "cmd/unlock").c_str(), 1);
    JsonDocument status;
    status["state"] = "online"; status["boot"] = bootNumber; status["sequence"] = sequence;
    String body; serializeJson(status, body);
    mqtt.publish((prefix + "status").c_str(), body, true, 1);
    mqttBackoffSeconds = 1;
    Serial.println("MQTT connected");
    flushEventQueue();
  } else {
    Serial.printf("MQTT connect failed; retry in %u s\n", mqttBackoffSeconds);
    connectAt = now + mqttBackoffSeconds * 1000UL;
    mqttBackoffSeconds = min<uint8_t>(60, mqttBackoffSeconds * 2);
  }
}

void scanCard(uint32_t now) {
  if ((phase != Phase::Idle && phase != Phase::DoorOpen) || (phase == Phase::Idle && stableDoorOpen) ||
      now - cardAt < CARD_COOLDOWN_MS || !reader.PICC_IsNewCardPresent() ||
      !reader.PICC_ReadCardSerial()) return;
  cardAt = now;
  String uid;
  for (byte i = 0; i < reader.uid.size; i++) {
    if (reader.uid.uidByte[i] < 16) uid += '0';
    uid += String(reader.uid.uidByte[i], HEX);
  }
  uid.toUpperCase();
#ifdef WOKWI_SIM
  Serial.printf("RFID UID: %s\n", uid.c_str());
#endif
  reader.PICC_HaltA();
  if (phase == Phase::DoorOpen) {
    packageUid = uid;
    Serial.printf("Package RFID UID: %s\n", uid.c_str());
    screen("Package scanned", "Close Door");
    beep(1300, 80);
    return;
  }
  if (!mqtt.connected()) {
    tryOfflineAccess(uid, now);
    return;
  }
  pendingRequest = makeRequestId();
  pendingUid = uid;
  JsonDocument doc;
  doc["request_id"] = pendingRequest;
  doc["compartment_id"] = COMPARTMENT_ID;
  doc["method"] = "rfid";
  doc["credential"] = uid;
  String body;
  serializeJson(doc, body);
  digitalWrite(RED_LED, LOW);
  screen("Checking card...", "Please wait");
  authAt = millis();
  if (mqtt.publish((prefix + "auth/request").c_str(), body, false, 1)) {
    phase = Phase::WaitingAuth;
    Serial.println("Authorization requested");
  } else {
    pendingRequest = "";
    pendingUid = "";
    tryOfflineAccess(uid, now);
  }
}

#ifdef WOKWI_SIM
void requestVirtualCredential(String method, String credential, uint32_t now) {
  credential.trim();
  method.toLowerCase();
  if ((method != "otp" && method != "qr") || credential.isEmpty() || credential.length() > 80 ||
      phase != Phase::Idle || stableDoorOpen || !mqtt.connected()) return;
  if (method == "otp") {
    if (credential.length() != 6) return;
    for (size_t i = 0; i < credential.length(); i++)
      if (credential[i] < '0' || credential[i] > '9') return;
  }
  pendingRequest = makeRequestId();
  JsonDocument doc;
  doc["request_id"] = pendingRequest;
  doc["compartment_id"] = COMPARTMENT_ID;
  doc["method"] = method;
  doc["credential"] = credential;
  String body;
  serializeJson(doc, body);
  digitalWrite(RED_LED, LOW);
  screen(method == "otp" ? "Checking OTP..." : "Checking QR...", "Please wait");
  if (mqtt.publish((prefix + "auth/request").c_str(), body, false, 1)) {
    phase = Phase::WaitingAuth;
    authAt = now;
    Serial.println("Virtual credential authorization requested");
  } else {
    pendingRequest = "";
    screen("Request failed", "Try again");
  }
  credential = "";
}

void readVirtualCredential(uint32_t now) {
  static String input;
  while (Serial.available()) {
    char ch = (char)Serial.read();
    if (ch == '\r' || ch == '\n') {
      input.trim();
      if (input.startsWith("OTP:")) requestVirtualCredential("otp", input.substring(4), now);
      else if (input.startsWith("QR:")) {
        String token = input.substring(3);
        int marker = token.indexOf("token=");
        if (marker >= 0) token = token.substring(marker + 6);
        int ampersand = token.indexOf('&');
        if (ampersand >= 0) token = token.substring(0, ampersand);
        requestVirtualCredential("qr", token, now);
      }
      input = "";
    } else if (input.length() < 96) input += ch;
    else input = "";
  }
}
#endif

void updateSensors(uint32_t now) {
  if (digitalRead(PIR_PIN) == HIGH) motionAt = now;
  digitalWrite(OCCUPANCY_LIGHT_PIN, (now - motionAt < OCCUPANCY_LIGHT_MS) ? HIGH : LOW);
  bool smokeDetected = digitalRead(SMOKE_PIN) == LOW;
  if (smokeDetected && !smokeAlarmActive) {
    smokeAlarmActive = true;
    digitalWrite(RED_LED, HIGH);
    beep(900, 700);
    screen("SMOKE ALERT", "Check locker");
    publishEvent("smoke_alarm", "");
    Serial.println("Smoke/gas threshold exceeded");
  } else if (!smokeDetected && smokeAlarmActive) {
    smokeAlarmActive = false;
    if (phase == Phase::Idle) digitalWrite(RED_LED, LOW);
    screen("Sensor clear", "Scan RFID");
  }
}

void setup() {
  Serial.begin(115200);
#ifdef WOKWI_SIM
  Serial.println("Wokwi virtual auth input: send OTP:<6 digits> or QR:<token> in Serial Monitor");
#endif
  pinMode(LOCK_PIN, OUTPUT);
  lockOff(); // Fail secure at startup.
  pinMode(GREEN_LED, OUTPUT);
  pinMode(RED_LED, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(OCCUPANCY_LIGHT_PIN, OUTPUT);
  pinMode(PIR_PIN, INPUT);
  pinMode(SMOKE_PIN, INPUT);
  digitalWrite(GREEN_LED, LOW);
  digitalWrite(RED_LED, LOW);
  digitalWrite(OCCUPANCY_LIGHT_PIN, LOW);
  pinMode(REED_PIN, INPUT_PULLUP);
  rawDoorOpen = stableDoorOpen = digitalRead(REED_PIN) == HIGH;
  rawChangedAt = millis();
  if (!prefs.begin("locker", false)) {
    Serial.println("Fatal: NVS unavailable");
    while (true) delay(1000);
  }
  bootNumber = prefs.getUInt("boot", 0) + 1;
  if (bootNumber == 0 || prefs.putUInt("boot", bootNumber) != sizeof(uint32_t)) {
    Serial.println("Fatal: boot counter could not be persisted");
    while (true) delay(1000);
  }
  eventSequence = prefs.getUInt("eventSeq", 0);
  sequence = prefs.getUInt("sequence", 0);
  configVersion = prefs.getUInt("cfgver", 0);
  offlinePermissionsJson = prefs.getString("offperms", "[]");
  lastNonce = prefs.getString("authNonce", "");
  doorOpenLimitMs = prefs.getUInt("openlim", DOOR_OPEN_LIMIT_MS);
  if (doorOpenLimitMs < 5000 || doorOpenLimitMs > 300000) doorOpenLimitMs = DOOR_OPEN_LIMIT_MS;
  if (!LittleFS.begin(false)) {
    if (prefs.getBool("fsInit", false)) {
      Serial.println("Fatal: LittleFS unavailable; refusing to format persisted event queue");
      while (true) delay(1000);
    }
    Serial.println("LittleFS is uninitialized; formatting only on first boot");
    if (!LittleFS.format() || !LittleFS.begin(false) || !prefs.putBool("fsInit", true)) {
      Serial.println("Fatal: LittleFS first-boot initialization failed");
      while (true) delay(1000);
    }
  } else {
    prefs.putBool("fsInit", true);
  }
  if (!LittleFS.exists(EVENT_QUEUE_FILE) && LittleFS.exists(EVENT_QUEUE_BAK))
    LittleFS.rename(EVENT_QUEUE_BAK, EVENT_QUEUE_FILE);
  else if (LittleFS.exists(EVENT_QUEUE_FILE) && LittleFS.exists(EVENT_QUEUE_BAK))
    LittleFS.remove(EVENT_QUEUE_BAK);
  lcd.init(); lcd.backlight(); screen("Smart Locker", "Scan RFID");
  scale.begin(SCALE_DT_PIN, SCALE_SCK_PIN);
  scale.set_scale(420.0f);
  if (scale.wait_ready_timeout(1500)) scale.tare(5);
  else Serial.println("HX711 not ready; package weight verification unavailable");
  esp_timer_create_args_t timerArgs = {};
  timerArgs.callback = &lockPulseDone;
  timerArgs.name = "lock_off";
  if (esp_timer_create(&timerArgs, &lockTimer) != ESP_OK) {
    Serial.println("Fatal: lock timer unavailable");
    while (true) delay(1000);
  }
  SPI.begin();
  reader.PCD_Init();
  WiFi.mode(WIFI_STA);
#ifndef WOKWI_SIM
  transport.setCACert(MQTT_CA_CERT);
#endif
  mqtt.begin(MQTT_HOST, MQTT_PORT, transport);
  mqtt.setOptions(30, false, 2000);
  mqtt.setWill((prefix + "status").c_str(), "{\"state\":\"offline\"}", true, 1);
  mqtt.onMessage(onMessage);
  Serial.printf("Locker %s boot %u\n", LOCKER_ID, bootNumber);
}

void loop() {
  uint32_t now = millis();
  updateSensors(now);
  updateDoor(now);
  if (phase == Phase::WaitingAuth && now - authAt > AUTH_TIMEOUT_MS) {
    Serial.println("Authorization timed out");
    String timedOutRequest = pendingRequest;
    String timedOutUid = pendingUid;
    publishEvent("auth_fail", timedOutRequest);
    setIdle();
    if (!timedOutUid.isEmpty() && tryOfflineAccess(timedOutUid, now)) {
      Serial.println("Backend did not respond; using synchronized offline RFID permission");
    } else {
      digitalWrite(RED_LED, HIGH);
      beep(380, 180);
      screen("Auth timeout", "Scan again");
    }
  }
  if ((phase == Phase::WaitingDoor || phase == Phase::MaintenanceWaitingDoor) && now - grantedAt > WAIT_OPEN_MS) {
    Serial.println("Door was not opened; session timed out");
    publishEvent("unlock_timeout", activeRequest);
    screen("Open timeout", "Scan again");
    setIdle();
  }
  if ((phase == Phase::DoorOpen || phase == Phase::MaintenanceDoorOpen) && !leftOpenReported && now - openedAt > doorOpenLimitMs) {
    leftOpenReported = true;
    digitalWrite(RED_LED, HIGH);
    publishEvent("door_left_open", activeRequest);
  }
  updateNetwork(now);
  mqtt.loop();
#ifdef WOKWI_SIM
  readVirtualCredential(now);
#endif
  flushEventQueue();
  if (mqtt.connected() && now - telemetryAt >= TELEMETRY_INTERVAL_MS) {
    telemetryAt = now;
    JsonDocument tele;
    tele["sequence"] = ++sequence; tele["uptime_ms"] = now;
    tele["door"] = stableDoorOpen ? "open" : "closed"; tele["phase"] = (uint8_t)phase;
    tele["occupancy_motion"] = digitalRead(PIR_PIN) == HIGH;
    tele["smoke_alarm"] = smokeAlarmActive;
    if (scale.is_ready()) tele["weight_kg"] = scale.get_units(3);
    String body; serializeJson(tele, body);
    mqtt.publish((prefix + "telemetry").c_str(), body, false, 0);
    prefs.putUInt("sequence", sequence);
  }
  if (phase == Phase::Idle && now - screenActivityAt >= 60000) lcd.noBacklight();
  scanCard(now);
}
