"""One-compartment Smart Locker demo backend. Run with: uvicorn app:api --app-dir backend."""
import os
import json
import re
import secrets
import sqlite3
import hashlib
import hmac
from datetime import datetime, timezone
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

DB_PATH = Path(os.getenv("LOCKER_DB", "backend/locker.db"))
ADMIN_TOKEN = os.getenv("LOCKER_ADMIN_TOKEN", "")
OTP_PEPPER = os.getenv("LOCKER_OTP_PEPPER", ADMIN_TOKEN)
OTP_TTL_SECONDS = 600
OTP_MAX_ATTEMPTS = 5
OTP_LOCK_SECONDS = 60
_lock = threading.RLock()
api = FastAPI(title="Smart Locker N11 demo")


@contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock, closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, locker_id TEXT NOT NULL, compartment_id TEXT NOT NULL,
            shipper_uid TEXT NOT NULL, customer_uid TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('waiting_deposit','waiting_pickup','received','needs_review'))
        );
        CREATE TABLE IF NOT EXISTS sessions (
            request_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
            action TEXT NOT NULL, opened INTEGER NOT NULL DEFAULT 0,
            closed INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY, request_id TEXT, kind TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS devices (
            locker_id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'unknown', last_seen REAL,
            telemetry_json TEXT
        );
        CREATE TABLE IF NOT EXISTS package_evidence (
            request_id TEXT PRIMARY KEY REFERENCES sessions(request_id), package_uid TEXT NOT NULL,
            expected_uid TEXT NOT NULL, weight_before REAL NOT NULL, weight_after REAL NOT NULL,
            passed INTEGER NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS device_commands (
            command_id TEXT PRIMARY KEY, locker_id TEXT NOT NULL, topic TEXT NOT NULL,
            payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL, sent_at REAL, acked_at REAL
        );
        CREATE TABLE IF NOT EXISTS access_credentials (
            credential_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
            method TEXT NOT NULL CHECK(method IN ('otp','qr')), action TEXT NOT NULL,
            salt TEXT NOT NULL, credential_hash TEXT NOT NULL,
            expires_at REAL NOT NULL, used_at REAL, attempts INTEGER NOT NULL DEFAULT 0,
            locked_until REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auth_responses (
            request_id TEXT PRIMARY KEY, locker_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL, response_json TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS offline_permissions (
            permission_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
            locker_id TEXT NOT NULL, compartment_id TEXT NOT NULL, uid TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('deposit','pickup')),
            expires_at REAL NOT NULL, consumed_at REAL, created_at REAL NOT NULL
        );
        """)
        # Keep existing demo databases usable when the schema grows.
        columns = {r[1] for r in c.execute("PRAGMA table_info(sessions)")}
        if "expired" not in columns:
            c.execute("ALTER TABLE sessions ADD COLUMN expired INTEGER NOT NULL DEFAULT 0")
        order_columns = {r[1] for r in c.execute("PRAGMA table_info(orders)")}
        if "package_uid" not in order_columns:
            c.execute("ALTER TABLE orders ADD COLUMN package_uid TEXT NOT NULL DEFAULT ''")
        event_columns = {r[1] for r in c.execute("PRAGMA table_info(events)")}
        if "locker_id" not in event_columns:
            c.execute("ALTER TABLE events ADD COLUMN locker_id TEXT NOT NULL DEFAULT ''")
        if "sequence" not in event_columns:
            c.execute("ALTER TABLE events ADD COLUMN sequence INTEGER NOT NULL DEFAULT 0")


def admin(authorization: str = Header(default="")):
    if not ADMIN_TOKEN or not secrets.compare_digest(authorization, "Bearer " + ADMIN_TOKEN):
        raise HTTPException(401, "Admin token required")


class OrderInput(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    locker_id: str = "locker01"
    compartment_id: str = "A01"
    shipper_uid: str = Field(min_length=4, max_length=32)
    customer_uid: str = Field(min_length=4, max_length=32)
    package_uid: str = ""


class ConfigInput(BaseModel):
    door_open_limit_ms: int = Field(ge=5000, le=300000)
    config_version: int | None = Field(default=None, ge=1)


@api.on_event("startup")
def startup():
    init_db()


@api.post("/orders", dependencies=[Depends(admin)])
def create_order(order: OrderInput):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", order.locker_id) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", order.compartment_id):
        raise HTTPException(422, "Invalid locker_id or compartment_id")
    with db() as c:
        occupied = c.execute("SELECT id FROM orders WHERE locker_id=? AND compartment_id=? AND status IN ('waiting_deposit','waiting_pickup','needs_review')", (order.locker_id, order.compartment_id)).fetchone()
        if occupied:
            raise HTTPException(409, "Compartment already reserved")
        try:
            c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status,package_uid) VALUES (?,?,?,?,?,?,?)",
                      (order.id, order.locker_id, order.compartment_id, order.shipper_uid.upper(), order.customer_uid.upper(), "waiting_deposit", order.package_uid.upper()))
            _create_offline_permission(c, order.id, order.locker_id, order.compartment_id,
                                       order.shipper_uid.upper(), "deposit")
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Order exists")
    config_command = queue_device_config(order.locker_id)
    return {"id": order.id, "status": "waiting_deposit", "config_command_id": config_command}


@api.get("/orders", dependencies=[Depends(admin)])
def list_orders():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT id,locker_id,compartment_id,status FROM orders ORDER BY id")]


def queue_device_command(locker_id: str, topic_suffix: str, payload: dict) -> str:
    command_id = str(payload["command_id"])
    with db() as c:
        c.execute("INSERT INTO device_commands(command_id,locker_id,topic,payload,created_at) VALUES (?,?,?,?,?)",
                  (command_id, locker_id, f"smartlocker/{SITE_ID}/{locker_id}/{topic_suffix}", json.dumps(payload), time.time()))
    return command_id


OFFLINE_PERMISSION_TTL_SECONDS = 24 * 60 * 60


def _create_offline_permission(c, order_id: str, locker_id: str, compartment_id: str,
                               uid: str, action: str) -> str:
    now = time.time()
    permission_id = "off-" + secrets.token_hex(12)
    c.execute("INSERT INTO offline_permissions(permission_id,order_id,locker_id,compartment_id,uid,action,expires_at,created_at) VALUES (?,?,?,?,?,?,?,?)",
              (permission_id, order_id, locker_id, compartment_id, uid.upper(), action,
               now + OFFLINE_PERMISSION_TTL_SECONDS, now))
    return permission_id


def active_offline_permissions(c, locker_id: str) -> list[dict]:
    now = time.time()
    return [dict(row) for row in c.execute("""
        SELECT p.permission_id,p.order_id,p.compartment_id,p.uid,p.action,p.expires_at
        FROM offline_permissions p JOIN orders o ON o.id=p.order_id
        WHERE p.locker_id=? AND p.consumed_at IS NULL AND p.expires_at>?
          AND ((p.action='deposit' AND o.status='waiting_deposit')
            OR (p.action='pickup' AND o.status='waiting_pickup'))
        ORDER BY p.created_at LIMIT 8
    """, (locker_id, now))]


def queue_device_config(locker_id: str, door_open_limit_ms: int | None = None) -> str:
    with db() as c:
        latest_payload = c.execute("SELECT payload FROM device_commands WHERE locker_id=? AND topic LIKE '%/cmd/config' ORDER BY created_at DESC LIMIT 1",
                                   (locker_id,)).fetchone()
        latest = 0
        current_limit = 30000
        if latest_payload:
            previous = json.loads(latest_payload["payload"])
            latest = int(previous.get("config_version", 0))
            current_limit = int(previous.get("door_open_limit_ms", current_limit))
        version = latest + 1
        command_id = f"cfg-{locker_id}-{version}"
        payload = {"command_id": command_id, "config_version": version,
                   "door_open_limit_ms": door_open_limit_ms or current_limit,
                   "offline_credentials": active_offline_permissions(c, locker_id)}
        c.execute("INSERT INTO device_commands(command_id,locker_id,topic,payload,created_at) VALUES (?,?,?,?,?)",
                  (command_id, locker_id, f"smartlocker/{SITE_ID}/{locker_id}/cmd/config", json.dumps(payload), time.time()))
    return command_id


SITE_ID = os.getenv("LOCKER_SITE", "site01")


@api.post("/devices/{locker_id}/config", dependencies=[Depends(admin)])
def set_device_config(locker_id: str, config: ConfigInput):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", locker_id):
        raise HTTPException(422, "Invalid locker_id")
    with db() as c:
        latest = c.execute("SELECT MAX(CAST(json_extract(payload,'$.config_version') AS INTEGER)) FROM device_commands WHERE locker_id=? AND topic LIKE '%/cmd/config'", (locker_id,)).fetchone()[0] or 0
        credentials = active_offline_permissions(c, locker_id)
    version = config.config_version if config.config_version is not None else latest + 1
    if version <= latest:
        raise HTTPException(409, "config_version must increase")
    command_id = f"cfg-{locker_id}-{version}"
    queue_device_command(locker_id, "cmd/config", {"command_id": command_id, "config_version": version,
                                                    "door_open_limit_ms": config.door_open_limit_ms,
                                                    "offline_credentials": credentials})
    return {"command_id": command_id, "config_version": version, "state": "pending"}


@api.post("/devices/{locker_id}/unlock", dependencies=[Depends(admin)])
def maintenance_unlock(locker_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", locker_id):
        raise HTTPException(422, "Invalid locker_id")
    command_id = "maint-" + secrets.token_hex(8)
    payload = {"command_id": command_id, "nonce": secrets.token_hex(16), "compartment_id": "A01",
               "expires_at": int(time.time()) + 10, "action": "maintenance_unlock"}
    queue_device_command(locker_id, "cmd/unlock", payload)
    return {"command_id": command_id, "expires_at": payload["expires_at"], "state": "pending"}


def _credential_digest(method: str, salt: str, value: str) -> str:
    if not OTP_PEPPER:
        raise HTTPException(503, "LOCKER_OTP_PEPPER must be configured")
    message = f"{method}:{salt}:{value}".encode()
    return hmac.new(OTP_PEPPER.encode(), message, hashlib.sha256).hexdigest()


def _remember_auth_response(c, request_id: str, locker_id: str, fingerprint: str, response: dict) -> dict:
    c.execute("INSERT OR IGNORE INTO auth_responses(request_id,locker_id,fingerprint,response_json,created_at) VALUES (?,?,?,?,?)",
              (request_id, locker_id, fingerprint, json.dumps(response), time.time()))
    return response


@api.post("/orders/{order_id}/pickup-credentials", dependencies=[Depends(admin)])
def issue_pickup_credentials(order_id: str):
    """Issue demo-only one-time pickup credentials; deliver them out of band in a full system."""
    now = time.time()
    with db() as c:
        order = c.execute("SELECT locker_id,status FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            raise HTTPException(404, "Order not found")
        if order["status"] != "waiting_pickup":
            raise HTTPException(409, "Pickup credentials are available only after deposit")
        c.execute("UPDATE access_credentials SET expires_at=? WHERE order_id=? AND action='pickup' AND used_at IS NULL",
                  (now, order_id))
        issued = []
        for method, value in (("otp", f"{secrets.randbelow(1_000_000):06d}"),
                              ("qr", secrets.token_urlsafe(32))):
            salt = secrets.token_hex(16)
            credential_id = f"cred-{secrets.token_hex(12)}"
            c.execute("INSERT INTO access_credentials(credential_id,order_id,method,action,salt,credential_hash,expires_at,created_at) VALUES (?,?,?,?,?,?,?,?)",
                      (credential_id, order_id, method, "pickup", salt,
                       _credential_digest(method, salt, value), now + OTP_TTL_SECONDS, now))
            issued.append((method, value))
    values = dict(issued)
    return {"order_id": order_id, "expires_at": int(now + OTP_TTL_SECONDS),
            "otp": values["otp"], "qr_payload": f"smartlocker://pickup/{order_id}?token={values['qr']}",
            "qr_token": values["qr"], "delivery": "Demo only: deliver securely to the customer in a full deployment."}


@api.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse((Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8"))


@api.get("/dashboard/data", dependencies=[Depends(admin)])
def dashboard_data():
    with db() as c:
        orders = [dict(r) for r in c.execute("SELECT id,locker_id,compartment_id,status FROM orders ORDER BY id")]
        events = [dict(r) for r in c.execute("SELECT event_id,request_id,kind,created_at FROM events ORDER BY created_at DESC LIMIT 50")]
        sessions = [dict(r) for r in c.execute("SELECT request_id,order_id,action,opened,closed,expired,created_at FROM sessions ORDER BY created_at DESC LIMIT 50")]
        devices = [dict(r) for r in c.execute("SELECT locker_id,state,last_seen,telemetry_json FROM devices ORDER BY locker_id")]
        evidence = [dict(r) for r in c.execute("SELECT p.request_id,s.order_id,p.package_uid,p.expected_uid,p.weight_before,p.weight_after,p.passed,p.created_at FROM package_evidence p JOIN sessions s ON s.request_id=p.request_id ORDER BY p.created_at DESC LIMIT 50")]
        commands = [dict(r) for r in c.execute("SELECT command_id,locker_id,topic,state,created_at,sent_at,acked_at FROM device_commands ORDER BY created_at DESC LIMIT 50")]
        compartments = [dict(r) for r in c.execute("""
            SELECT o.locker_id,o.compartment_id,
              MAX(CASE WHEN o.status IN ('waiting_deposit','waiting_pickup','needs_review') THEN 1 ELSE 0 END) occupied,
              (SELECT CASE WHEN s.closed=1 OR s.expired=1 THEN 'closed'
                           WHEN s.opened=1 THEN 'open' ELSE 'authorized' END
               FROM sessions s JOIN orders so ON so.id=s.order_id
               WHERE so.locker_id=o.locker_id AND so.compartment_id=o.compartment_id AND s.closed=0
               ORDER BY s.created_at DESC LIMIT 1) door_state
            FROM orders o GROUP BY o.locker_id,o.compartment_id ORDER BY o.locker_id,o.compartment_id
        """)]
    for event in events:
        event["created_at"] = datetime.fromtimestamp(event["created_at"], timezone.utc).isoformat()
    for session in sessions:
        session["created_at"] = datetime.fromtimestamp(session["created_at"], timezone.utc).isoformat()
    for device in devices:
        if device["last_seen"]:
            device["last_seen"] = datetime.fromtimestamp(device["last_seen"], timezone.utc).isoformat()
    for item in evidence:
        item["created_at"] = datetime.fromtimestamp(item["created_at"], timezone.utc).isoformat()
    for command in commands:
        for key in ("created_at", "sent_at", "acked_at"):
            if command[key]:
                command[key] = datetime.fromtimestamp(command[key], timezone.utc).isoformat()
    return {"orders": orders, "compartments": compartments, "events": events, "sessions": sessions,
            "devices": devices, "package_evidence": evidence, "commands": commands,
            "alerts": [e for e in events if e["kind"] in {"auth_fail", "door_forced", "door_left_open", "smoke_alarm"}]}


def authorize(locker_id: str, request: dict) -> dict:
    """Pure business entry point used by MQTT adapter and tests."""
    request_id = str(request.get("request_id", ""))
    method = str(request.get("method", ""))
    credential = str(request.get("credential", ""))
    uid = credential.upper()
    compartment = str(request.get("compartment_id", ""))
    result = {"request_id": request_id, "allow": False, "compartment_id": compartment}
    if not request_id or method not in {"rfid", "otp", "qr"} or not credential or not compartment:
        return result
    if method in {"otp", "qr"} and not OTP_PEPPER:
        return result
    fingerprint = hmac.new((OTP_PEPPER or "demo-only-no-pepper").encode(),
                           f"{locker_id}:{compartment}:{method}:{credential}".encode(), hashlib.sha256).hexdigest()
    with db() as c:
        cached = c.execute("SELECT locker_id,fingerprint,response_json FROM auth_responses WHERE request_id=?", (request_id,)).fetchone()
        if cached:
            if cached["locker_id"] == locker_id and secrets.compare_digest(cached["fingerprint"], fingerprint):
                return json.loads(cached["response_json"])
            return result
        c.execute("UPDATE sessions SET closed=1,expired=1 WHERE opened=0 AND closed=0 AND created_at<?", (time.time() - 60,))
        if c.execute("SELECT 1 FROM sessions WHERE request_id=?", (request_id,)).fetchone():
            return _remember_auth_response(c, request_id, locker_id, fingerprint, result)
        if c.execute("SELECT 1 FROM sessions s JOIN orders o ON o.id=s.order_id WHERE o.locker_id=? AND o.compartment_id=? AND s.closed=0", (locker_id, compartment)).fetchone():
            return _remember_auth_response(c, request_id, locker_id, fingerprint, result)
        if method == "rfid":
            row = c.execute("SELECT * FROM orders WHERE locker_id=? AND compartment_id=? AND ((status='waiting_deposit' AND shipper_uid=?) OR (status='waiting_pickup' AND customer_uid=?))",
                            (locker_id, compartment, uid, uid)).fetchone()
        else:
            row = c.execute("SELECT * FROM orders WHERE locker_id=? AND compartment_id=? AND status='waiting_pickup'",
                            (locker_id, compartment)).fetchone()
            if row:
                access = c.execute("SELECT * FROM access_credentials WHERE order_id=? AND action='pickup' AND method=? ORDER BY created_at DESC LIMIT 1",
                                   (row["id"], method)).fetchone()
                now = time.time()
                if not access or access["used_at"] is not None or access["expires_at"] < now or access["locked_until"] > now:
                    row = None
                else:
                    supplied = _credential_digest(method, access["salt"], credential)
                    if not secrets.compare_digest(access["credential_hash"], supplied):
                        attempts = access["attempts"] + 1
                        locked_until = now + OTP_LOCK_SECONDS if attempts >= OTP_MAX_ATTEMPTS else 0
                        c.execute("UPDATE access_credentials SET attempts=?,locked_until=? WHERE credential_id=?",
                                  (attempts, locked_until, access["credential_id"]))
                        row = None
                    else:
                        updated = c.execute("UPDATE access_credentials SET used_at=? WHERE credential_id=? AND used_at IS NULL",
                                            (now, access["credential_id"]))
                        if updated.rowcount != 1:
                            row = None
        if not row:
            return _remember_auth_response(c, request_id, locker_id, fingerprint, result)
        action = "deposit" if method == "rfid" and row["status"] == "waiting_deposit" else "pickup"
        authorized_at = int(time.time())
        c.execute("INSERT INTO sessions(request_id,order_id,action,created_at) VALUES (?,?,?,?)", (request_id, row["id"], action, time.time()))
        offline = c.execute("SELECT permission_id FROM offline_permissions WHERE order_id=? AND action=? AND consumed_at IS NULL AND expires_at>? ORDER BY created_at DESC LIMIT 1",
                            (row["id"], action, authorized_at)).fetchone()
        if offline:
            result["offline_permission_id"] = offline["permission_id"]
        result.update(allow=True, order_id=row["id"], action=action, nonce=secrets.token_hex(12),
                      ttl_ms=5000, authorized_at=authorized_at)
        _remember_auth_response(c, request_id, locker_id, fingerprint, result)
    return result


def record_event(locker_id: str, event: dict) -> bool:
    event_id = str(event.get("event_id", ""))
    request_id = str(event.get("request_id", ""))
    kind = str(event.get("event", ""))
    if not event_id or kind not in {"auth_ok", "unlock_pulsed", "unlock_timeout", "auth_fail", "door_open", "door_closed", "door_forced", "door_left_open", "smoke_alarm", "package_evidence"}:
        return False
    refresh_offline_config = False
    with db() as c:
        if c.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone():
            return False
        offline_permission_id = str(event.get("offline_permission_id", ""))
        if kind == "auth_ok" and offline_permission_id and request_id == "offline-" + offline_permission_id:
            authorized_at = int(event.get("authorized_at", 0) or 0)
            permission = c.execute("SELECT * FROM offline_permissions WHERE permission_id=? AND locker_id=? AND consumed_at IS NULL",
                                   (offline_permission_id, locker_id)).fetchone()
            if permission and not (int(permission["created_at"]) <= authorized_at <= permission["expires_at"] and authorized_at <= time.time() + 60):
                permission = None
            if permission and permission["compartment_id"] == event.get("compartment_id"):
                current_order = c.execute("SELECT status FROM orders WHERE id=?", (permission["order_id"],)).fetchone()
                expected_status = "waiting_deposit" if permission["action"] == "deposit" else "waiting_pickup"
                if current_order and current_order["status"] == expected_status:
                    c.execute("UPDATE offline_permissions SET consumed_at=? WHERE permission_id=? AND consumed_at IS NULL",
                              (time.time(), offline_permission_id))
                    c.execute("INSERT OR IGNORE INTO sessions(request_id,order_id,action,created_at) VALUES (?,?,?,?)",
                              (request_id, permission["order_id"], permission["action"], time.time()))
        session = c.execute("SELECT s.*,o.locker_id,o.compartment_id,o.package_uid,o.customer_uid FROM sessions s JOIN orders o ON o.id=s.order_id WHERE s.request_id=?", (request_id,)).fetchone()
        if kind == "auth_ok" and offline_permission_id and request_id != "offline-" + offline_permission_id:
            authorized_at = int(event.get("authorized_at", 0) or 0)
            permission = c.execute("SELECT * FROM offline_permissions WHERE permission_id=? AND locker_id=? AND consumed_at IS NULL",
                                   (offline_permission_id, locker_id)).fetchone()
            if (not session or not permission or permission["order_id"] != session["order_id"] or
                permission["action"] != session["action"] or permission["compartment_id"] != event.get("compartment_id") or
                not (int(permission["created_at"]) <= authorized_at <= permission["expires_at"]) or
                authorized_at > time.time() + 60):
                return False
            c.execute("UPDATE offline_permissions SET consumed_at=? WHERE permission_id=? AND consumed_at IS NULL",
                      (time.time(), offline_permission_id))
        if kind == "auth_ok" and c.execute("SELECT 1 FROM events WHERE request_id=? AND kind='auth_ok'", (request_id,)).fetchone():
            return False
        maintenance = event.get("source") == "maintenance"
        command = c.execute("SELECT * FROM device_commands WHERE command_id=? AND locker_id=? AND topic LIKE '%/cmd/unlock'",
                            (request_id, locker_id)).fetchone() if maintenance else None
        if kind not in {"door_forced", "auth_fail", "smoke_alarm"} and not maintenance and (not session or session["locker_id"] != locker_id or session["compartment_id"] != event.get("compartment_id")):
            return False
        if maintenance and (not command or event.get("compartment_id") != "A01" or kind not in {"unlock_pulsed", "unlock_timeout", "door_open", "door_closed", "door_left_open"}):
            return False
        # Ignore out-of-order or impossible events without consuming their event_id.
        if kind == "unlock_pulsed" and session and (session["opened"] or session["closed"] or session["expired"]):
            return False
        if kind == "auth_ok" and session and (session["opened"] or session["closed"] or session["expired"]):
            return False
        if kind == "door_open" and session and (session["opened"] or session["closed"] or session["expired"]):
            return False
        if kind == "door_closed" and session and (not session["opened"] or session["closed"] or session["expired"]):
            return False
        if kind == "door_left_open" and session and (not session["opened"] or session["closed"]):
            return False
        if maintenance and ((kind == "unlock_pulsed" and command["state"] not in {"acked", "sent"}) or
                            (kind == "door_open" and command["state"] not in {"acked", "sent"}) or
                            (kind == "door_closed" and command["state"] not in {"door_open", "needs_review"}) or
                            (kind == "door_left_open" and command["state"] != "door_open") or
                            (kind == "unlock_timeout" and command["state"] not in {"acked", "sent"})):
            return False
        event_sequence = int(event.get("sequence", 0) or 0)
        if event_sequence:
            previous = c.execute("SELECT MAX(sequence) AS seq FROM events WHERE locker_id=?", (locker_id,)).fetchone()["seq"] or 0
            if event_sequence <= previous:
                return False
        c.execute("INSERT INTO events(event_id,request_id,kind,created_at,locker_id,sequence) VALUES (?,?,?,?,?,?)",
                  (event_id, request_id, kind, time.time(), locker_id, event_sequence))
        if maintenance and kind == "door_open":
            c.execute("UPDATE device_commands SET state='door_open' WHERE command_id=?", (request_id,))
        elif maintenance and kind == "door_closed":
            c.execute("UPDATE device_commands SET state=CASE WHEN state='needs_review' THEN 'needs_review_closed' ELSE 'complete' END WHERE command_id=?", (request_id,))
        elif maintenance and kind == "door_left_open":
            c.execute("UPDATE device_commands SET state='needs_review' WHERE command_id=?", (request_id,))
        elif maintenance and kind == "unlock_timeout":
            c.execute("UPDATE device_commands SET state='expired' WHERE command_id=?", (request_id,))
        if kind == "door_open" and session and not session["closed"]:
            c.execute("UPDATE sessions SET opened=1 WHERE request_id=?", (request_id,))
        elif kind == "door_closed" and session and session["opened"] and not session["closed"]:
            c.execute("UPDATE sessions SET closed=1 WHERE request_id=?", (request_id,))
            new_status = "waiting_pickup" if session["action"] == "deposit" else "received"
            order_state = c.execute("SELECT status FROM orders WHERE id=?", (session["order_id"],)).fetchone()[0]
            if order_state == "needs_review":
                new_status = "needs_review"
            if session["package_uid"]:
                observed = str(event.get("package_uid", "")).upper()
                before = float(event.get("weight_before", 0) or 0)
                after = float(event.get("weight_after", 0) or 0)
                delta = after - before
                weight_ok = delta >= 0.05 if session["action"] == "deposit" else delta <= -0.05
                passed = observed == session["package_uid"] and weight_ok
                c.execute("INSERT OR REPLACE INTO package_evidence VALUES (?,?,?,?,?,?,?)",
                          (request_id, observed, session["package_uid"], before, after, int(passed), time.time()))
                if not passed:
                    new_status = "needs_review"
            c.execute("UPDATE orders SET status=? WHERE id=?", (new_status, session["order_id"]))
            if new_status == "waiting_pickup" and session["action"] == "deposit":
                _create_offline_permission(c, session["order_id"], locker_id, session["compartment_id"],
                                           session["customer_uid"], "pickup")
                refresh_offline_config = True
        elif kind == "door_left_open" and session:
            c.execute("UPDATE orders SET status='needs_review' WHERE id=?", (session["order_id"],))
        elif kind == "unlock_timeout" and session and not session["opened"]:
            c.execute("UPDATE sessions SET closed=1,expired=1 WHERE request_id=?", (request_id,))
        elif kind == "auth_fail" and session and not session["opened"]:
            c.execute("UPDATE sessions SET closed=1,expired=1 WHERE request_id=?", (request_id,))
    if refresh_offline_config:
        queue_device_config(locker_id)
    return True


def event_exists(event_id: str) -> bool:
    with db() as c:
        return c.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone() is not None


def pending_device_commands(limit: int = 20) -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT command_id,locker_id,topic,payload FROM device_commands WHERE state='pending' OR (state='sent' AND sent_at<?) ORDER BY created_at LIMIT ?", (time.time() - 3, limit))]


def mark_command_sent(command_id: str) -> None:
    with db() as c:
        c.execute("UPDATE device_commands SET state='sent',sent_at=? WHERE command_id=? AND state IN ('pending','sent')", (time.time(), command_id))


def acknowledge_device_command(locker_id: str, payload: dict) -> bool:
    command_id = str(payload.get("command_id", ""))
    with db() as c:
        cur = c.execute("UPDATE device_commands SET state=?,acked_at=? WHERE command_id=? AND locker_id=? AND state IN ('pending','sent')",
                        ("acked" if payload.get("accepted") else "rejected", time.time(), command_id, locker_id))
        return cur.rowcount == 1


def record_device_state(locker_id: str, state: str, telemetry: dict | None = None) -> None:
    with db() as c:
        c.execute("""INSERT INTO devices(locker_id,state,last_seen,telemetry_json) VALUES (?,?,?,?)
            ON CONFLICT(locker_id) DO UPDATE SET state=excluded.state,last_seen=excluded.last_seen,
            telemetry_json=COALESCE(excluded.telemetry_json,devices.telemetry_json)""",
            (locker_id, state, time.time(), json.dumps(telemetry) if telemetry is not None else None))


init_db()
