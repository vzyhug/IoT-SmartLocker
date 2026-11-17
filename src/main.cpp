#include <Arduino.h>
#include <ArduinoJson.h>
#include <MFRC522.h>
#include <Preferences.h>
#include <PubSubClient.h>
#include <SPI.h>
#include <WiFi.h>

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

// One compartment. Physical wiring and lock driver must match include/HARDWARE.md.
constexpr uint8_t RFID_SS = 5, RFID_RST = 27, REED_PIN = 32;
constexpr uint8_t LOCK_PIN = 26, GREEN_LED = 25, RED_LED = 33;
constexpr uint32_t LOCK_PULSE_MS = 500;
constexpr uint32_t AUTH_TIMEOUT_MS = 5000;
constexpr uint32_t WAIT_OPEN_MS = 30000;
constexpr uint32_t DOOR_OPEN_LIMIT_MS = 30000;
constexpr uint32_t DEBOUNCE_MS = 50;
constexpr uint32_t CARD_COOLDOWN_MS = 1200;
constexpr uint32_t CONNECT_RETRY_MS = 3000;
constexpr char COMPARTMENT_ID[] = "A01";

enum class Phase : uint8_t { Idle, WaitingAuth, WaitingDoor, DoorOpen };
Phase phase = Phase::Idle;
MFRC522 reader(RFID_SS, RFID_RST);
Preferences prefs;
#ifdef WOKWI_SIM
WiFiClient transport;
#else
WiFiClientSecure transport;
#endif
PubSubClient mqtt(transport);
const String prefix = String("smartlocker/") + SITE_ID + "/" + LOCKER_ID + "/";

String pendingRequest, activeRequest, lastNonce;
uint32_t bootNumber = 0, requestSequence = 0, eventSequence = 0;
uint32_t authAt = 0, grantedAt = 0, openedAt = 0, cardAt = 0, connectAt = 0;
uint32_t rawChangedAt = 0;
bool rawDoorOpen = false, stableDoorOpen = false, leftOpenReported = false;
bool wifiStarted = false;
#ifndef WOKWI_SIM
bool timeSyncRequested = false;
#endif

void lockOff() { digitalWrite(LOCK_PIN, LOW); }

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

void publishEvent(const char* kind, const String& requestId) {
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
  String body;
  serializeJson(doc, body);
  bool sent = mqtt.connected() && mqtt.publish((prefix + "event").c_str(), body.c_str());
  Serial.printf("Event %s: %s\n", kind, sent ? "published" : "not delivered");
  // Durable offline queue and database ACK belong to the CN06 integration phase.
}

void setIdle() {
  phase = Phase::Idle;
  pendingRequest = "";
  activeRequest = "";
  digitalWrite(GREEN_LED, LOW);
}

void onMessage(char* topic, byte* payload, unsigned int length) {
  if (String(topic) != prefix + "auth/response" || phase != Phase::WaitingAuth) return;
  JsonDocument doc;
  if (deserializeJson(doc, payload, length)) return;
  if (String(doc["request_id"] | "") != pendingRequest) return;
  if (millis() - authAt > AUTH_TIMEOUT_MS) { setIdle(); return; }

  bool allowed = doc["allow"] | false;
  String compartment = doc["compartment_id"] | "";
  String nonce = doc["nonce"] | "";
  String order = doc["order_id"] | "";
  String action = doc["action"] | "";
  uint32_t ttl = doc["ttl_ms"] | 0;
  if (!allowed || compartment != COMPARTMENT_ID || nonce.isEmpty() ||
      nonce == lastNonce || order.isEmpty() ||
      (action != "deposit" && action != "pickup") || ttl == 0 || ttl > AUTH_TIMEOUT_MS ||
      rawDoorOpen || stableDoorOpen) {
    Serial.println("Authorization denied or invalid response");
    digitalWrite(RED_LED, HIGH);
    setIdle();
    return;
  }

  lastNonce = nonce;
  activeRequest = pendingRequest;
  pendingRequest = "";
  grantedAt = millis();
  leftOpenReported = false;
  phase = Phase::WaitingDoor;
  digitalWrite(RED_LED, LOW);
  digitalWrite(GREEN_LED, HIGH);
  digitalWrite(LOCK_PIN, HIGH);
  if (!lockTimer || esp_timer_start_once(lockTimer, LOCK_PULSE_MS * 1000ULL) != ESP_OK) {
    lockOff();
    digitalWrite(RED_LED, HIGH);
    setIdle();
    Serial.println("Lock timer failed; unlock cancelled");
    return;
  }
  publishEvent("unlock_pulsed", activeRequest);
  Serial.printf("Access granted: %s, %s\n", action.c_str(), order.c_str());
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
      publishEvent("door_open", activeRequest);
    } else {
      setIdle();
      digitalWrite(RED_LED, HIGH);
      publishEvent("door_forced", "");
    }
  } else if (phase == Phase::DoorOpen) {
    publishEvent("door_closed", activeRequest);
    digitalWrite(RED_LED, LOW);
    setIdle();
  } else if (phase == Phase::Idle) {
    digitalWrite(RED_LED, LOW);
  }
}

void updateNetwork(uint32_t now) {
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
  if (mqtt.connected()) return;
#ifndef WOKWI_SIM
  if (!timeSyncRequested) {
    configTime(0, 0, NTP_SERVER);
    timeSyncRequested = true;
    Serial.println("Waiting for trusted time before MQTT/TLS");
  }
  if (time(nullptr) < 1700000000) return;
#endif
#ifdef WOKWI_SIM
  bool connected = mqtt.connect(LOCKER_ID);
#else
  bool connected = mqtt.connect(LOCKER_ID, MQTT_USER, MQTT_PASSWORD);
#endif
  if (connected) {
    mqtt.subscribe((prefix + "auth/response").c_str(), 1);
    Serial.println("MQTT connected");
  } else Serial.printf("MQTT connect failed: %d\n", mqtt.state());
}

void scanCard(uint32_t now) {
  if (phase != Phase::Idle || stableDoorOpen || !mqtt.connected() ||
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
  pendingRequest = makeRequestId();
  JsonDocument doc;
  doc["request_id"] = pendingRequest;
  doc["compartment_id"] = COMPARTMENT_ID;
  doc["method"] = "rfid";
  doc["credential"] = uid;
  String body;
  serializeJson(doc, body);
  digitalWrite(RED_LED, LOW);
  if (mqtt.publish((prefix + "auth/request").c_str(), body.c_str())) {
    phase = Phase::WaitingAuth;
    authAt = now;
    Serial.println("Authorization requested");
  } else pendingRequest = "";
}

void setup() {
  Serial.begin(115200);
  pinMode(LOCK_PIN, OUTPUT);
  lockOff(); // Fail secure at startup.
  pinMode(GREEN_LED, OUTPUT);
  pinMode(RED_LED, OUTPUT);
  digitalWrite(GREEN_LED, LOW);
  digitalWrite(RED_LED, LOW);
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
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
  mqtt.setBufferSize(768);
  mqtt.setSocketTimeout(2);
  Serial.printf("Locker %s boot %u\n", LOCKER_ID, bootNumber);
}

void loop() {
  uint32_t now = millis();
  updateDoor(now);
  if (phase == Phase::WaitingAuth && now - authAt > AUTH_TIMEOUT_MS) {
    Serial.println("Authorization timed out");
    digitalWrite(RED_LED, HIGH);
    setIdle();
  }
  if (phase == Phase::WaitingDoor && now - grantedAt > WAIT_OPEN_MS) {
    Serial.println("Door was not opened; session timed out");
    setIdle();
  }
  if (phase == Phase::DoorOpen && !leftOpenReported && now - openedAt > DOOR_OPEN_LIMIT_MS) {
    leftOpenReported = true;
    digitalWrite(RED_LED, HIGH);
    publishEvent("door_left_open", activeRequest);
  }
  updateNetwork(now);
  mqtt.loop();
  scanCard(now);
}
