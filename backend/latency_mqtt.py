"""Measure local MQTT auth request/response latency over 20 distinct demo orders."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path

import paho.mqtt.client as mqtt


def main():
    with tempfile.TemporaryDirectory(prefix="locker-latency-") as temp:
        db_path = str(Path(temp) / "latency.db")
        env = os.environ.copy()
        env.update(LOCKER_DB=db_path, MQTT_TLS="0", MQTT_HOST="127.0.0.1", MQTT_PORT="1883",
                   MQTT_CLIENT_ID=f"smartlocker-latency-{os.getpid()}")
        worker = subprocess.Popen([sys.executable, str(Path(__file__).with_name("mqtt_worker.py"))], env=env)
        connected, cv = threading.Event(), threading.Condition()
        replies = {}
        messages = []
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"locker-latency-client-{os.getpid()}")
        client.on_connect = lambda c, u, f, reason, props: connected.set() if reason == 0 else None

        def on_message(c, u, message):
            body = json.loads(message.payload)
            messages.append(body)
            with cv:
                replies[body.get("request_id")] = time.perf_counter(), body
                cv.notify_all()

        client.on_message = on_message
        try:
            client.connect("127.0.0.1", 1883, 10)
            client.loop_start()
            if not connected.wait(5):
                raise RuntimeError("latency client could not connect to local Mosquitto")
            client.subscribe("smartlocker/site01/locker01/auth/response", qos=1)
            client.subscribe("smartlocker/site01/locker01/cmd/config", qos=1)
            deadline = time.time() + 5
            while not Path(db_path).exists() and time.time() < deadline:
                time.sleep(0.05)
            with closing(sqlite3.connect(db_path)) as conn:
                ready = {"command_id": "latency-ready", "config_version": 1, "door_open_limit_ms": 30000}
                conn.execute("INSERT INTO device_commands(command_id,locker_id,topic,payload,created_at) VALUES (?,?,?,?,?)",
                             (ready["command_id"], "locker01", "smartlocker/site01/locker01/cmd/config", json.dumps(ready), time.time()))
                for index in range(20):
                    conn.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status) VALUES (?,?,?,?,?,?)",
                                 (f"lat-{index}", "locker01", f"A{index:02}", f"{index + 1:08X}", "FFFFFFFF", "waiting_deposit"))
                conn.commit()
            deadline = time.time() + 8
            while time.time() < deadline and not any(x.get("command_id") == "latency-ready" for x in messages):
                time.sleep(0.03)
            if not any(x.get("command_id") == "latency-ready" for x in messages):
                raise RuntimeError("MQTT worker readiness handshake failed")
            client.publish("smartlocker/site01/locker01/ack", json.dumps({"command_id": "latency-ready", "accepted": True}), qos=1).wait_for_publish(3)
            latencies = []
            for index in range(20):
                request_id = f"latency-{index}"
                body = {"request_id": request_id, "compartment_id": f"A{index:02}", "method": "rfid", "credential": f"{index + 1:08X}"}
                start = time.perf_counter()
                client.publish("smartlocker/site01/locker01/auth/request", json.dumps(body), qos=1).wait_for_publish(3)
                with cv:
                    if not cv.wait_for(lambda: request_id in replies, timeout=3):
                        raise RuntimeError(f"No auth response for {request_id}")
                    end, response = replies.pop(request_id)
                if not response.get("allow"):
                    raise RuntimeError(f"Valid latency request was denied: {request_id}")
                latencies.append((end - start) * 1000)
            ordered = sorted(latencies)
            p50 = ordered[len(ordered) // 2]
            worst = max(latencies)
            print(f"20/20 auth responses; MQTT publish-to-response p50={p50:.1f}ms max={worst:.1f}ms")
            if worst >= 2000:
                raise RuntimeError("At least one local MQTT authorization round trip exceeded 2 seconds")
            print("PASS: local MQTT authorization response budget (<2 s) for this machine and broker")
        finally:
            client.loop_stop()
            client.disconnect()
            worker.terminate()
            worker.wait(timeout=5)


if __name__ == "__main__":
    main()
