"""MQTT bridge. Run separately from the API process."""
import json
import os
import ssl

import paho.mqtt.client as mqtt
from app import authorize, record_event

SITE = os.getenv("LOCKER_SITE", "site01")
PREFIX = f"smartlocker/{SITE}/"


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe(PREFIX + "+/auth/request", qos=1)
        client.subscribe(PREFIX + "+/event", qos=1)


def on_message(client, userdata, message):
    parts = message.topic.split("/")
    if len(parts) < 4 or parts[0] != "smartlocker" or parts[1] != SITE:
        return
    locker_id = parts[2]
    try:
        payload = json.loads(message.payload)
        if message.topic.endswith("/auth/request"):
            response = authorize(locker_id, payload)
            client.publish(PREFIX + locker_id + "/auth/response", json.dumps(response), qos=1, retain=False)
        elif message.topic.endswith("/event"):
            record_event(locker_id, payload)
    except (ValueError, TypeError) as exc:
        print("Rejected MQTT message:", exc)


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="smartlocker-backend")
if os.getenv("MQTT_USER"):
    client.username_pw_set(os.environ["MQTT_USER"], os.environ["MQTT_PASSWORD"])
if os.getenv("MQTT_TLS", "1") != "0":
    client.tls_set(ca_certs=os.environ["MQTT_CA_FILE"], tls_version=ssl.PROTOCOL_TLS_CLIENT)
client.on_connect = on_connect
client.on_message = on_message
client.connect(os.getenv("MQTT_HOST", "localhost"), int(os.getenv("MQTT_PORT", "8883")), 30)
client.loop_forever()
