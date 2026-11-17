#pragma once
// Copy to secrets.h and replace placeholders. Never commit secrets.h.
#define WIFI_SSID "YOUR_WIFI"
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD"
#define MQTT_HOST "192.168.1.10"
#define MQTT_PORT 8883
#define MQTT_USER "locker01"
#define MQTT_PASSWORD "CHANGE_ME"
#define SITE_ID "site01"
#define LOCKER_ID "locker01"
#define MQTT_CA_CERT R"EOF(-----BEGIN CERTIFICATE-----
PASTE_BROKER_CA_CERTIFICATE_HERE
-----END CERTIFICATE-----)EOF"
