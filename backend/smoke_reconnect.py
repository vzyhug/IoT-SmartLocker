"""Verify the backend MQTT worker survives broker absence and broker restart."""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path

import paho.mqtt.client as mqtt


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_broker(executable, config):
    return subprocess.Popen([executable, "-c", str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    broker = shutil.which("mosquitto") or r"C:\Program Files\Mosquitto\mosquitto.exe"
    if not Path(broker).exists():
        raise SystemExit("Mosquitto executable was not found")
    port = free_port()
    with tempfile.TemporaryDirectory(prefix="locker-reconnect-") as temp:
        root = Path(temp)
        db_path, config = root / "worker.db", root / "mosquitto.conf"
        config.write_text(f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n", encoding="utf-8")
        env = os.environ.copy()
        env.update(LOCKER_DB=str(db_path), MQTT_TLS="0", MQTT_HOST="127.0.0.1", MQTT_PORT=str(port),
                   MQTT_CLIENT_ID=f"worker-reconnect-{os.getpid()}")
        worker = subprocess.Popen([sys.executable, str(Path(__file__).with_name("mqtt_worker.py"))], env=env)
        broker_process = None
        client = None
        messages, connected = [], threading.Event()
        try:
            deadline = time.time() + 5
            schema_ready = False
            while time.time() < deadline:
                if db_path.exists():
                    try:
                        with closing(sqlite3.connect(db_path, timeout=0.2)) as conn:
                            schema_ready = conn.execute(
                                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='device_commands'"
                            ).fetchone() is not None
                    except sqlite3.OperationalError:
                        pass
                if schema_ready:
                    break
                time.sleep(0.05)
            if not schema_ready:
                raise RuntimeError("worker did not initialize the command outbox while broker was down")
            with closing(sqlite3.connect(db_path)) as conn:
                payload = {"command_id": "reconnect-1", "config_version": 1, "door_open_limit_ms": 45000}
                conn.execute("INSERT INTO device_commands(command_id,locker_id,topic,payload,created_at) VALUES (?,?,?,?,?)",
                             (payload["command_id"], "locker01", "smartlocker/site01/locker01/cmd/config", json.dumps(payload), time.time()))
                conn.commit()
            time.sleep(1.2)
            broker_process = start_broker(broker, config)
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"reconnect-probe-{os.getpid()}")

            def on_connect(c, u, flags, reason, props):
                if reason == 0:
                    connected.set()
                    c.subscribe("smartlocker/site01/locker01/cmd/config", qos=1)

            client.on_connect = on_connect
            client.on_message = lambda c, u, m: messages.append(json.loads(m.payload))
            client.connect("127.0.0.1", port, 10)
            client.loop_start()
            until = time.time() + 8
            while time.time() < until and not any(x.get("command_id") == "reconnect-1" for x in messages):
                time.sleep(0.05)
            if not any(x.get("command_id") == "reconnect-1" for x in messages):
                raise RuntimeError("worker failed to connect and publish command after broker startup")
            client.publish("smartlocker/site01/locker01/ack", json.dumps({"command_id": "reconnect-1", "accepted": True}), qos=1).wait_for_publish(3)
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute("INSERT INTO device_commands(command_id,locker_id,topic,payload,created_at) VALUES (?,?,?,?,?)",
                             ("reconnect-2", "locker01", "smartlocker/site01/locker01/cmd/config",
                              json.dumps({"command_id": "reconnect-2", "config_version": 2, "door_open_limit_ms": 60000}), time.time()))
                conn.commit()
            broker_process.terminate()
            broker_process.wait(timeout=5)
            time.sleep(1.3)
            connected.clear()
            broker_process = start_broker(broker, config)
            until = time.time() + 10
            while time.time() < until and not any(x.get("command_id") == "reconnect-2" for x in messages):
                time.sleep(0.05)
            if not any(x.get("command_id") == "reconnect-2" for x in messages):
                raise RuntimeError("worker failed to reconnect and deliver queued command after broker restart")
            client.publish("smartlocker/site01/locker01/ack", json.dumps({"command_id": "reconnect-2", "accepted": True}), qos=1).wait_for_publish(3)
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute("INSERT INTO device_commands(command_id,locker_id,topic,payload,created_at) VALUES (?,?,?,?,?)",
                             ("reconnect-3", "locker01", "smartlocker/site01/locker01/cmd/config",
                              json.dumps({"command_id": "reconnect-3", "config_version": 3, "door_open_limit_ms": 75000}), time.time()))
                conn.commit()
            worker.terminate()
            worker.wait(timeout=5)
            worker = subprocess.Popen([sys.executable, str(Path(__file__).with_name("mqtt_worker.py"))], env=env)
            until = time.time() + 8
            while time.time() < until and not any(x.get("command_id") == "reconnect-3" for x in messages):
                time.sleep(0.05)
            if not any(x.get("command_id") == "reconnect-3" for x in messages):
                raise RuntimeError("restarted backend worker failed to resume the persistent command outbox")
            print("PASS: worker starts before broker, reconnects after broker restart and resumes SQLite outbox after worker restart")
        finally:
            if client:
                client.loop_stop()
                client.disconnect()
            if broker_process and broker_process.poll() is None:
                broker_process.terminate()
                broker_process.wait(timeout=5)
            worker.terminate()
            worker.wait(timeout=5)


if __name__ == "__main__":
    main()
