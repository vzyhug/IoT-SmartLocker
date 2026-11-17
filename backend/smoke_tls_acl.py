"""Ephemeral Mosquitto TLS/ACL integration check. Requires Mosquitto and OpenSSL on PATH."""
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt


def run(command):
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def main():
    broker = shutil.which("mosquitto") or r"C:\Program Files\Mosquitto\mosquitto.exe"
    passwd_tool = shutil.which("mosquitto_passwd") or r"C:\Program Files\Mosquitto\mosquitto_passwd.exe"
    openssl = shutil.which("openssl")
    if not all(Path(x).exists() for x in (broker, passwd_tool)) or not openssl:
        raise SystemExit("Requires mosquitto, mosquitto_passwd and openssl on PATH (or Mosquitto default install path).")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="locker-tls-") as temp:
        root = Path(temp)
        ca_key, ca_cert = root / "ca.key", root / "ca.crt"
        server_key, server_csr, server_cert = root / "server.key", root / "server.csr", root / "server.crt"
        ext = root / "server.ext"
        run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-keyout", str(ca_key), "-out", str(ca_cert), "-subj", "/CN=SmartLocker-Test-CA"])
        run([openssl, "req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(server_key), "-out", str(server_csr), "-subj", "/CN=localhost"])
        ext.write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n", encoding="ascii")
        run([openssl, "x509", "-req", "-in", str(server_csr), "-CA", str(ca_cert), "-CAkey", str(ca_key), "-CAcreateserial", "-days", "1", "-out", str(server_cert), "-extfile", str(ext)])
        passwd = root / "passwd"
        run([passwd_tool, "-b", "-c", str(passwd), "locker01", "locker-test-password"])
        run([passwd_tool, "-b", str(passwd), "backend", "backend-test-password"])
        acl = root / "acl"
        acl.write_text("""user locker01
topic write smartlocker/site01/locker01/auth/request
topic write smartlocker/site01/locker01/event
topic write smartlocker/site01/locker01/status
topic write smartlocker/site01/locker01/telemetry
topic write smartlocker/site01/locker01/ack
topic read smartlocker/site01/locker01/auth/response
topic read smartlocker/site01/locker01/event/ack
topic read smartlocker/site01/locker01/cmd/config
topic read smartlocker/site01/locker01/cmd/unlock
user backend
topic read smartlocker/site01/+/auth/request
topic read smartlocker/site01/+/event
topic read smartlocker/site01/+/status
topic read smartlocker/site01/+/telemetry
topic read smartlocker/site01/+/ack
topic write smartlocker/site01/+/auth/response
topic write smartlocker/site01/+/event/ack
topic write smartlocker/site01/+/cmd/config
topic write smartlocker/site01/+/cmd/unlock
""", encoding="utf-8")
        config = root / "mosquitto.conf"
        config.write_text("\n".join((
            f"listener {port} 127.0.0.1", "protocol mqtt", "allow_anonymous false",
            f"cafile {ca_cert.as_posix()}", f"certfile {server_cert.as_posix()}",
            f"keyfile {server_key.as_posix()}", f"password_file {passwd.as_posix()}",
            f"acl_file {acl.as_posix()}", "persistence false", "log_type all", "")), encoding="utf-8")
        log = (root / "broker.log").open("w", encoding="utf-8")
        process = subprocess.Popen([broker, "-c", str(config, ), "-v"], stdout=log, stderr=subprocess.STDOUT)
        clients = []

        def connect(client_id, username, password):
            messages, connected = [], threading.Event()
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
            c.username_pw_set(username, password)
            c.tls_set(ca_certs=str(ca_cert), tls_version=ssl.PROTOCOL_TLS_CLIENT)
            c.on_connect = lambda client, userdata, flags, reason, props: connected.set() if reason == 0 else None
            c.on_message = lambda client, userdata, msg: messages.append((msg.topic, msg.payload.decode()))
            c.connect("localhost", port, 10)
            c.loop_start()
            if not connected.wait(5):
                raise RuntimeError(f"TLS MQTT connection failed for {username}")
            clients.append(c)
            return c, messages

        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                if process.poll() is not None:
                    log.flush()
                    raise RuntimeError("Mosquitto exited during TLS smoke setup: " + (root / "broker.log").read_text(encoding="utf-8", errors="replace"))
                try:
                    locker, locker_msgs = connect("locker-tls-smoke", "locker01", "locker-test-password")
                    backend, backend_msgs = connect("backend-tls-smoke", "backend", "backend-test-password")
                    break
                except (OSError, RuntimeError):
                    time.sleep(0.15)
            else:
                raise RuntimeError("Timed out waiting for temporary TLS broker")
            locker.subscribe("smartlocker/site01/locker01/auth/response", qos=1)
            locker.subscribe("smartlocker/site01/locker02/auth/response", qos=1)  # Must not cross ACL boundary.
            backend.subscribe("smartlocker/site01/+/auth/request", qos=1)
            time.sleep(0.3)
            locker.publish("smartlocker/site01/locker01/auth/request", '{"request_id":"tls-ok"}', qos=1).wait_for_publish(3)
            until = time.time() + 3
            while time.time() < until and not any(t.endswith("auth/request") for t, _ in backend_msgs):
                time.sleep(0.03)
            assert any(t == "smartlocker/site01/locker01/auth/request" for t, _ in backend_msgs), "authorized publish was not delivered"
            backend.publish("smartlocker/site01/locker01/auth/response", '{"request_id":"tls-ok","allow":true}', qos=1).wait_for_publish(3)
            backend.publish("smartlocker/site01/locker02/auth/response", '{"request_id":"cross-denied"}', qos=1).wait_for_publish(3)
            until = time.time() + 2
            while time.time() < until and not any("tls-ok" in body for _, body in locker_msgs):
                time.sleep(0.03)
            assert any("tls-ok" in body for _, body in locker_msgs), "authorized subscribe did not receive response"
            assert not any("cross-denied" in body for _, body in locker_msgs), "device received another locker's topic"
            denied = locker.publish("smartlocker/site01/locker02/auth/request", '{"request_id":"cross-denied"}', qos=1)
            denied.wait_for_publish(3)
            time.sleep(0.4)
            assert not any(t == "smartlocker/site01/locker02/auth/request" for t, _ in backend_msgs), "ACL allowed cross-locker publish"
            plaintext = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="plaintext-tls-smoke")
            plain_connected = threading.Event()
            plain_reason = []
            plaintext.on_connect = lambda client, userdata, flags, reason, props: (plain_reason.append(reason), plain_connected.set())
            try:
                plaintext.connect("localhost", port, 3)
                plaintext.loop_start()
                if plain_connected.wait(1.5) and int(plain_reason[0]) == 0:
                    raise AssertionError("TLS listener accepted a plaintext MQTT connection")
            except (OSError, mqtt.MQTTException):
                pass
            finally:
                plaintext.loop_stop()
                plaintext.disconnect()
            print("PASS: TLS CA/hostname validation, plaintext rejected on TLS listener, authenticated MQTT and cross-locker ACL denial")
        finally:
            for client in clients:
                client.loop_stop()
                client.disconnect()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            log.close()


if __name__ == "__main__":
    main()
