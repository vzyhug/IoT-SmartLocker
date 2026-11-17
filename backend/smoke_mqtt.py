"""Optional local-broker smoke test: python backend/smoke_mqtt.py."""
import json
import hashlib
import hmac
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from contextlib import closing

import paho.mqtt.client as mqtt


def main():
    with tempfile.TemporaryDirectory() as temp:
        db_path = str(Path(temp) / "locker.db")
        env = os.environ.copy()
        env.update(LOCKER_DB=db_path, MQTT_TLS="0", MQTT_HOST="127.0.0.1", MQTT_PORT="1883",
                   MQTT_CLIENT_ID=f"smartlocker-smoke-{os.getpid()}", LOCKER_ADMIN_TOKEN="smoke-admin",
                   LOCKER_OTP_PEPPER="smoke-otp-pepper")
        worker = subprocess.Popen([sys.executable, str(Path(__file__).with_name("mqtt_worker.py"))], env=env)
        received = []
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.on_message = lambda c, u, m: received.append(json.loads(m.payload))
        client.on_connect = lambda c, u, f, rc, p: setattr(c, "_smoke_connected", rc == 0)
        try:
            client.connect("127.0.0.1", 1883, 10)
            client.loop_start()
            deadline = time.time() + 10
            while not getattr(client, "_smoke_connected", False) and time.time() < deadline:
                time.sleep(0.05)
            if not getattr(client, "_smoke_connected", False):
                raise RuntimeError("MQTT smoke client could not connect")
            client.subscribe("smartlocker/site01/locker01/auth/response", qos=1)
            client.subscribe("smartlocker/site01/locker01/event/ack", qos=1)
            client.subscribe("smartlocker/site01/locker01/cmd/config", qos=1)
            client.subscribe("smartlocker/site01/locker01/cmd/unlock", qos=1)
            deadline = time.time() + 10
            tables = set()
            while time.time() < deadline:
                if Path(db_path).exists():
                    with closing(sqlite3.connect(db_path)) as c:
                        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if {"device_commands", "access_credentials", "auth_responses"}.issubset(tables):
                        break
                time.sleep(0.05)
            if not {"device_commands", "access_credentials", "auth_responses"}.issubset(tables):
                raise RuntimeError("Backend database schema readiness timed out")
            time.sleep(0.2)
            with closing(sqlite3.connect(db_path)) as c:
                c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status) VALUES (?,?,?,?,?,?)", ("smoke-1", "locker01", "A01", "AABBCCDD", "11223344", "waiting_deposit"))
                c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status) VALUES (?,?,?,?,?,?)", ("smoke-otp", "locker01", "A02", "AABBCCDD", "11223344", "waiting_pickup"))
                otp, salt = "731904", "smoke-salt"
                otp_hash = hmac.new(b"smoke-otp-pepper", f"otp:{salt}:{otp}".encode(), hashlib.sha256).hexdigest()
                c.execute("INSERT INTO access_credentials(credential_id,order_id,method,action,salt,credential_hash,expires_at,created_at) VALUES (?,?,?,?,?,?,?,?)",
                          ("smoke-otp-credential", "smoke-otp", "otp", "pickup", salt, otp_hash, time.time() + 600, time.time()))
                command = {"command_id": "smoke-config", "config_version": 1, "door_open_limit_ms": 45000}
                c.execute("INSERT INTO device_commands(command_id,locker_id,topic,payload,created_at) VALUES (?,?,?,?,?)",
                          (command["command_id"], "locker01", "smartlocker/site01/locker01/cmd/config", json.dumps(command), time.time()))
                c.commit()
            until = time.time() + 5
            while time.time() < until and not any(x.get("command_id") == "smoke-config" for x in received):
                time.sleep(0.05)
            if not any(x.get("command_id") == "smoke-config" for x in received):
                raise RuntimeError("Backend command outbox did not publish config over MQTT")
            client.publish("smartlocker/site01/locker01/ack", json.dumps({"command_id": "smoke-config", "accepted": True}), qos=1)
            until = time.time() + 3
            while time.time() < until:
                with closing(sqlite3.connect(db_path)) as c:
                    state = c.execute("SELECT state FROM device_commands WHERE command_id='smoke-config'").fetchone()[0]
                if state == "acked":
                    break
                time.sleep(0.05)
            if state != "acked":
                raise RuntimeError("Backend did not accept device command ACK")
            topic = "smartlocker/site01/locker01/"

            maint = {"command_id": "maint-smoke", "nonce": "smoke-once", "expires_at": int(time.time()) + 8,
                     "compartment_id": "A01", "action": "maintenance_unlock"}
            with closing(sqlite3.connect(db_path)) as c:
                c.execute("INSERT INTO device_commands(command_id,locker_id,topic,payload,created_at) VALUES (?,?,?,?,?)",
                          (maint["command_id"], "locker01", topic + "cmd/unlock", json.dumps(maint), time.time()))
                c.commit()
            until = time.time() + 5
            while time.time() < until and not any(x.get("command_id") == "maint-smoke" for x in received):
                time.sleep(0.05)
            if not any(x.get("command_id") == "maint-smoke" for x in received):
                raise RuntimeError("Maintenance command was not published")
            client.publish(topic + "ack", json.dumps({"command_id": "maint-smoke", "accepted": True}), qos=1)
            until = time.time() + 3
            while time.time() < until:
                with closing(sqlite3.connect(db_path)) as c:
                    command_state = c.execute("SELECT state FROM device_commands WHERE command_id='maint-smoke'").fetchone()[0]
                if command_state == "acked":
                    break
                time.sleep(0.05)
            if command_state != "acked":
                raise RuntimeError("Maintenance command ACK was not recorded")

            def authorize(request_id, credential, method="rfid", compartment="A01"):
                start = len(received)
                message = {"request_id": request_id, "compartment_id": compartment, "method": method, "credential": credential}
                client.publish(topic + "auth/request", json.dumps(message), qos=1)
                until = time.time() + 5
                while time.time() < until:
                    for response in received[start:]:
                        if response.get("request_id") == request_id:
                            return response
                    time.sleep(0.05)
                raise RuntimeError(f"Timed out waiting for {request_id}")

            def event(event_id, request_id, kind, source=None):
                payload = {"event_id": event_id, "request_id": request_id, "locker_id": "locker01", "compartment_id": "A01", "event": kind}
                if source:
                    payload["source"] = source
                client.publish(topic + "event", json.dumps(payload), qos=1)

            def order_status():
                with closing(sqlite3.connect(db_path)) as c:
                    return c.execute("SELECT status FROM orders WHERE id='smoke-1'").fetchone()[0]

            event("maint-e1", "maint-smoke", "unlock_pulsed", "maintenance")
            event("maint-e2", "maint-smoke", "door_open", "maintenance")
            event("maint-e3", "maint-smoke", "door_closed", "maintenance")
            until = time.time() + 3
            while time.time() < until:
                with closing(sqlite3.connect(db_path)) as c:
                    command_state = c.execute("SELECT state FROM device_commands WHERE command_id='maint-smoke'").fetchone()[0]
                if command_state == "complete":
                    break
                time.sleep(0.05)
            if command_state != "complete":
                raise RuntimeError(f"Maintenance door cycle was not audited (state={command_state})")

            if authorize("smoke-denied", "11223344").get("allow"):
                raise RuntimeError("Customer was incorrectly authorized before deposit")
            deposit = authorize("smoke-deposit", "AABBCCDD")
            if not deposit.get("allow") or deposit.get("action") != "deposit":
                raise RuntimeError("Broker/worker did not authorize shipper deposit")
            if authorize("smoke-replay", "AABBCCDD").get("allow"):
                raise RuntimeError("A second active session was incorrectly authorized")
            event("smoke-close-early", "smoke-deposit", "door_closed")
            time.sleep(0.2)
            status = order_status()
            if status != "waiting_deposit":
                raise RuntimeError("Out-of-order door_closed changed order status")
            event("smoke-open", "smoke-deposit", "door_open")
            event("smoke-close", "smoke-deposit", "door_closed")
            until = time.time() + 3
            while time.time() < until:
                status = order_status()
                if status == "waiting_pickup":
                    break
                time.sleep(0.05)
            if status != "waiting_pickup":
                raise RuntimeError(f"Deposit events did not advance order (status={status})")
            ack_deadline = time.time() + 3
            while time.time() < ack_deadline and not any(x.get("event_id") == "smoke-close" and x.get("accepted") for x in received):
                time.sleep(0.05)
            if not any(x.get("event_id") == "smoke-close" and x.get("accepted") for x in received):
                raise RuntimeError("Backend did not send application ACK after storing event")
            pickup = authorize("smoke-pickup", "11223344")
            if not pickup.get("allow") or pickup.get("action") != "pickup":
                raise RuntimeError("Backend did not authorize customer pickup")
            event("smoke-pickup-open", "smoke-pickup", "door_open")
            event("smoke-pickup-close", "smoke-pickup", "door_closed")
            until = time.time() + 3
            while time.time() < until:
                status = order_status()
                if status == "received":
                    break
                time.sleep(0.05)
            if status != "received":
                raise RuntimeError(f"Pickup events did not complete order (status={status})")
            otp_auth = authorize("smoke-otp-auth", otp, method="otp", compartment="A02")
            if not otp_auth.get("allow") or otp_auth.get("action") != "pickup":
                raise RuntimeError("Backend worker did not authorize a valid OTP pickup")
            if authorize("smoke-otp-replay", otp, method="otp", compartment="A02").get("allow"):
                raise RuntimeError("A consumed OTP was accepted for a second request")
            print("PASS: config/maintenance outbox and audit; RFID and OTP authorization, replay guard, ordered door events and application ACK")
        finally:
            client.loop_stop()
            client.disconnect()
            worker.terminate()
            worker.wait(timeout=5)


if __name__ == "__main__":
    main()
