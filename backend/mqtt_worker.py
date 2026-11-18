"""MQTT bridge. Run separately from the API process."""
import json
import os
import signal
import ssl
import time

import paho.mqtt.client as mqtt
from app import (acknowledge_device_command, authorize, event_exists, mark_command_sent,
                 pending_device_commands, record_device_state, record_event)

SITE = os.getenv("LOCKER_SITE", "site01")
PREFIX = f"smartlocker/{SITE}/"
RUNNING = True


def stop_worker(signum, frame):
    global RUNNING
    RUNNING = False


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe(PREFIX + "+/auth/request", qos=1)
        client.subscribe(PREFIX + "+/event", qos=1)
        client.subscribe(PREFIX + "+/status", qos=1)
        client.subscribe(PREFIX + "+/telemetry", qos=0)
        client.subscribe(PREFIX + "+/ack", qos=1)


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
            event_id = str(payload.get("event_id", ""))
            if record_event(locker_id, payload) or (event_id and event_exists(event_id)):
                client.publish(PREFIX + locker_id + "/event/ack",
                               json.dumps({"event_id": event_id, "accepted": True}), qos=1, retain=False)
        elif message.topic.endswith("/status"):
            state = str(payload.get("state", "unknown"))
            record_device_state(locker_id, state)
        elif message.topic.endswith("/telemetry"):
            record_device_state(locker_id, "online", payload)
        elif message.topic.endswith("/ack"):
            if payload.get("command_id"):
                accepted = acknowledge_device_command(locker_id, payload)
                print("Device command ACK:", locker_id, payload, "matched=" + str(accepted))
    except (ValueError, TypeError) as exc:
        print("Rejected MQTT message:", exc)


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=os.getenv("MQTT_CLIENT_ID", "smartlocker-backend"))
if os.getenv("MQTT_USER"):
    client.username_pw_set(os.environ["MQTT_USER"], os.environ["MQTT_PASSWORD"])
if os.getenv("MQTT_TLS", "1") != "0":
    client.tls_set(ca_certs=os.environ["MQTT_CA_FILE"], tls_version=ssl.PROTOCOL_TLS_CLIENT)
client.on_connect = on_connect
client.on_message = on_message
client.reconnect_delay_set(min_delay=1, max_delay=60)
client.connect_async(os.getenv("MQTT_HOST", "localhost"), int(os.getenv("MQTT_PORT", "8883")), 30)
signal.signal(signal.SIGTERM, stop_worker)
client.loop_start()
try:
    while RUNNING:
        if client.is_connected():
            for command in pending_device_commands():
                payload = json.loads(command["payload"])
                if payload.get("expires_at", int(time.time()) + 1) < int(time.time()):
                    acknowledge_device_command(command["locker_id"], {"command_id": command["command_id"], "accepted": False})
                    continue
                info = client.publish(command["topic"], command["payload"], qos=1,
                                       retain=command["topic"].endswith("/cmd/config"))
                if info.rc == mqtt.MQTT_ERR_SUCCESS:
                    mark_command_sent(command["command_id"])
        time.sleep(0.5)
except KeyboardInterrupt:
    pass
finally:
    client.loop_stop()
    client.disconnect()
