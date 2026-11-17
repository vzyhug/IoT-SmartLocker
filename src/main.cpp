#include <Arduino.h>
#include <ArduinoJson.h>
#include <MFRC522.h>
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
#include "secrets.h"
#endif

// Check the actual board and wiring before powering the lock.
constexpr uint8_t RFID_SS = 5, RFID_RST = 27, REED_PIN = 32;
constexpr uint8_t LOCK_PIN = 26, GREEN_LED = 25, RED_LED = 33;
constexpr unsigned long LOCK_PULSE_MS = 500, AUTH_TIMEOUT_MS = 5000;
constexpr unsigned long SESSION_TIMEOUT_MS = 30000;
MFRC522 reader(RFID_SS, RFID_RST);
#ifdef WOKWI_SIM
WiFiClient transport;
#else
WiFiClientSecure transport;
#endif
PubSubClient mqtt(transport);
String prefix = String("smartlocker/") + SITE_ID + "/" + LOCKER_ID + "/";
String pendingRequest, activeRequest;
unsigned long requestAt = 0, unlockAt = 0, lastReconnect = 0, doorOpenedAt = 0;
bool awaitingAuth = false, unlocking = false, sessionActive = false, sawDoorOpen = false, leftOpenReported = false;
bool previousDoorOpen = false;

String newId(const char* kind) { return String(LOCKER_ID) + "-" + kind + "-" + String(esp_random(), HEX); }

void event(const char* name) {
  Serial.printf("Event: %s\n", name);
  JsonDocument doc;
  doc["event_id"] = newId("evt");
  doc["request_id"] = activeRequest;
  doc["event"] = name;
  doc["compartment_id"] = "A01";
  String body;
  serializeJson(doc, body);
  mqtt.publish((prefix + "event").c_str(), body.c_str());
}

void onMessage(char* topic, byte* payload, unsigned int length) {
  if (String(topic) != prefix + "auth/response" || !awaitingAuth) return;
  JsonDocument doc;
  if (deserializeJson(doc, payload, length)) return;
  if (String(doc["request_id"] | "") != pendingRequest) return;
  awaitingAuth = false;
  Serial.printf("Authorization: %s\n", (doc["allow"] | false) ? "allowed" : "denied");
  if (!(doc["allow"] | false) || millis() - requestAt > AUTH_TIMEOUT_MS ||
      String(doc["compartment_id"] | "") != "A01") { digitalWrite(RED_LED, HIGH); return; }
  activeRequest = pendingRequest;
  sessionActive = true;
  sawDoorOpen = false;
  leftOpenReported = false;
  unlocking = true;
  unlockAt = millis();
  digitalWrite(LOCK_PIN, HIGH);
  digitalWrite(GREEN_LED, HIGH);
  event("unlock_pulsed");
}

void setup() {
  Serial.begin(115200);
  pinMode(LOCK_PIN, OUTPUT); digitalWrite(LOCK_PIN, LOW);
  pinMode(GREEN_LED, OUTPUT); pinMode(RED_LED, OUTPUT);
  pinMode(REED_PIN, INPUT_PULLUP);
  previousDoorOpen = digitalRead(REED_PIN) == HIGH; // Adjust for reed wiring.
  SPI.begin(); reader.PCD_Init();
  WiFi.mode(WIFI_STA);
#ifndef WOKWI_SIM
  transport.setCACert(MQTT_CA_CERT);
#endif
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
  mqtt.setBufferSize(512);
}

void loop() {
  unsigned long now = millis();
  if (now - lastReconnect > 3000) {
    lastReconnect = now;
    if (WiFi.status() != WL_CONNECTED) WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    else if (!mqtt.connected()) {
#ifdef WOKWI_SIM
      bool connected = mqtt.connect(LOCKER_ID);
#else
      bool connected = mqtt.connect(LOCKER_ID, MQTT_USER, MQTT_PASSWORD);
#endif
      if (connected) {
        Serial.println("MQTT connected");
        mqtt.subscribe((prefix + "auth/response").c_str(), 1);
      } else Serial.printf("MQTT connection failed: %d\n", mqtt.state());
    }
  }
  mqtt.loop();
  if (unlocking && now - unlockAt >= LOCK_PULSE_MS) { digitalWrite(LOCK_PIN, LOW); unlocking = false; }
  if (awaitingAuth && now - requestAt > AUTH_TIMEOUT_MS) awaitingAuth = false;
  bool doorOpen = digitalRead(REED_PIN) == HIGH;
  if (doorOpen != previousDoorOpen) {
    delay(30); doorOpen = digitalRead(REED_PIN) == HIGH;
    if (doorOpen != previousDoorOpen) {
      previousDoorOpen = doorOpen;
      if (sessionActive) {
        if (doorOpen) { sawDoorOpen = true; doorOpenedAt = now; event("door_open"); }
        else if (sawDoorOpen) { event("door_closed"); sessionActive = false; digitalWrite(GREEN_LED, LOW); }
      } else if (doorOpen) event("door_forced");
    }
  }
  if (sessionActive && !sawDoorOpen && now - unlockAt > SESSION_TIMEOUT_MS) {
    sessionActive = false;
    digitalWrite(GREEN_LED, LOW);
  }
  if (sessionActive && sawDoorOpen && doorOpen && !leftOpenReported && now - doorOpenedAt > SESSION_TIMEOUT_MS) {
    event("door_left_open");
    leftOpenReported = true;
    digitalWrite(RED_LED, HIGH);
  }
  if (reader.PICC_IsNewCardPresent() && reader.PICC_ReadCardSerial() && mqtt.connected() && !awaitingAuth && !sessionActive) {
    String uid;
    for (byte i = 0; i < reader.uid.size; i++) {
      if (reader.uid.uidByte[i] < 16) uid += "0";
      uid += String(reader.uid.uidByte[i], HEX);
    }
    uid.toUpperCase();
    Serial.printf("RFID UID: %s\n", uid.c_str());
    pendingRequest = newId("req");
    JsonDocument doc;
    doc["request_id"] = pendingRequest;
    doc["compartment_id"] = "A01";
    doc["method"] = "rfid";
    doc["credential"] = uid;
    String body; serializeJson(doc, body);
    if (mqtt.publish((prefix + "auth/request").c_str(), body.c_str())) { awaitingAuth = true; requestAt = millis(); }
    reader.PICC_HaltA();
  }
}
