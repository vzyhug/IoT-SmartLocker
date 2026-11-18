"""Ephemeral HTTPS smoke for the local FastAPI admin API. Requires OpenSSL."""
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory(prefix="locker-https-") as temp:
        root = Path(temp)
        key, cert = root / "server.key", root / "server.crt"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "1", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = os.environ.copy()
        env.update(LOCKER_DB=str(root / "https.db"), LOCKER_ADMIN_TOKEN="https-smoke-admin",
                   LOCKER_OTP_PEPPER="https-smoke-pepper")
        api = subprocess.Popen([
            sys.executable, "-m", "uvicorn", "app:api", "--app-dir", str(Path(__file__).parent),
            "--host", "127.0.0.1", "--port", str(port), "--ssl-keyfile", str(key), "--ssl-certfile", str(cert),
        ], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        context = ssl.create_default_context(cafile=str(cert))
        base = f"https://127.0.0.1:{port}"
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(base + "/dashboard", context=context, timeout=1) as response:
                        if response.status == 200:
                            break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.1)
            else:
                raise RuntimeError("HTTPS API did not start with the temporary certificate")

            try:
                urllib.request.urlopen(base + "/dashboard/data", context=context, timeout=3)
                raise RuntimeError("Dashboard data endpoint unexpectedly allowed an anonymous request")
            except urllib.error.HTTPError as exc:
                if exc.code != 401:
                    raise
            request = urllib.request.Request(base + "/dashboard/data", headers={
                "Authorization": "Bearer https-smoke-admin"})
            with urllib.request.urlopen(request, context=context, timeout=3) as response:
                data = json.loads(response.read())
                if response.status != 200 or "orders" not in data:
                    raise RuntimeError("Authenticated HTTPS dashboard response was invalid")

            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/dashboard", timeout=2)
                raise RuntimeError("TLS-only API unexpectedly accepted plaintext HTTP")
            except (OSError, urllib.error.URLError):
                pass
            print("PASS: HTTPS certificate verification, admin authentication, and plaintext HTTP rejection")
        finally:
            api.terminate()
            api.wait(timeout=5)


if __name__ == "__main__":
    main()
