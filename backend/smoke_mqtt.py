"""Optional local-broker smoke test: python backend/smoke_mqtt.py."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

import paho.mqtt.client as mqtt


def main():
    with tempfile.TemporaryDirectory() as temp:
        db_path = str(Path(temp) / "locker.db")
        env = os.environ.copy()
        env.update(LOCKER_DB=db_path, MQTT_TLS="0", MQTT_HOST="127.0.0.1", MQTT_PORT="1883")
        worker = subprocess.Popen([sys.executable, str(Path(__file__).with_name("mqtt_worker.py"))], env=env)
        received = []
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.on_message = lambda c, u, m: received.append(json.loads(m.payload))
        try:
            client.connect("127.0.0.1", 1883, 10)
            client.subscribe("smartlocker/site01/locker01/auth/response")
            client.loop_start()
            deadline = time.time() + 5
            while time.time() < deadline:
                if Path(db_path).exists():
                    with closing(sqlite3.connect(db_path)) as c:
                        if c.execute("SELECT name FROM sqlite_master WHERE name='orders'").fetchone():
                            break
                time.sleep(0.05)
            else:
                raise RuntimeError("MQTT worker did not initialize DB")
            with closing(sqlite3.connect(db_path)) as c:
                with c:
                    c.execute("INSERT INTO orders VALUES (?,?,?,?,?,?)", ("smoke-1", "locker01", "A01", "AABBCCDD", "11223344", "waiting_deposit"))
            topic = "smartlocker/site01/locker01/"
            request = {"request_id": "smoke-req-1", "compartment_id": "A01", "method": "rfid", "credential": "AABBCCDD"}
            while not received and time.time() < deadline:
                client.publish(topic + "auth/request", json.dumps(request), qos=1)
                time.sleep(0.2)
            if not received or not received[-1].get("allow"):
                raise RuntimeError("Broker/worker did not authorize the demo shipper")
            for event_id, kind in (("smoke-open", "door_open"), ("smoke-close", "door_closed")):
                client.publish(topic + "event", json.dumps({"event_id": event_id, "request_id": request["request_id"], "compartment_id": "A01", "event": kind}), qos=1)
            while time.time() < deadline:
                with closing(sqlite3.connect(db_path)) as c:
                    status = c.execute("SELECT status FROM orders WHERE id='smoke-1'").fetchone()[0]
                if status == "waiting_pickup":
                    print("PASS: MQTT authorize, door events and order transition")
                    return
                time.sleep(0.1)
            raise RuntimeError(f"Order status stayed {status}")
        finally:
            client.loop_stop()
            client.disconnect()
            worker.terminate()
            worker.wait(timeout=5)


if __name__ == "__main__":
    main()
