"""One-compartment Smart Locker demo backend. Run with: uvicorn app:api --app-dir backend."""
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

DB_PATH = Path(os.getenv("LOCKER_DB", "backend/locker.db"))
ADMIN_TOKEN = os.getenv("LOCKER_ADMIN_TOKEN", "")
_lock = threading.RLock()
api = FastAPI(title="Smart Locker N11 demo")


@contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock, sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()


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
        """)


def admin(authorization: str = Header(default="")):
    if not ADMIN_TOKEN or not secrets.compare_digest(authorization, "Bearer " + ADMIN_TOKEN):
        raise HTTPException(401, "Admin token required")


class OrderInput(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    locker_id: str = "locker01"
    compartment_id: str = "A01"
    shipper_uid: str = Field(min_length=4, max_length=32)
    customer_uid: str = Field(min_length=4, max_length=32)


@api.on_event("startup")
def startup():
    init_db()


@api.post("/orders", dependencies=[Depends(admin)])
def create_order(order: OrderInput):
    with db() as c:
        occupied = c.execute("SELECT id FROM orders WHERE locker_id=? AND compartment_id=? AND status IN ('waiting_deposit','waiting_pickup','needs_review')", (order.locker_id, order.compartment_id)).fetchone()
        if occupied:
            raise HTTPException(409, "Compartment already reserved")
        try:
            c.execute("INSERT INTO orders VALUES (?,?,?,?,?,?)", (order.id, order.locker_id, order.compartment_id, order.shipper_uid.upper(), order.customer_uid.upper(), "waiting_deposit"))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Order exists")
    return {"id": order.id, "status": "waiting_deposit"}


@api.get("/orders", dependencies=[Depends(admin)])
def list_orders():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT id,locker_id,compartment_id,status FROM orders ORDER BY id")]


def authorize(locker_id: str, request: dict) -> dict:
    """Pure business entry point used by MQTT adapter and tests."""
    request_id = str(request.get("request_id", ""))
    uid = str(request.get("credential", "")).upper()
    compartment = str(request.get("compartment_id", ""))
    result = {"request_id": request_id, "allow": False, "compartment_id": compartment}
    if not request_id or request.get("method") != "rfid" or not uid or not compartment:
        return result
    with db() as c:
        c.execute("UPDATE sessions SET closed=1 WHERE opened=0 AND closed=0 AND created_at<?", (time.time() - 60,))
        if c.execute("SELECT 1 FROM sessions WHERE request_id=?", (request_id,)).fetchone():
            return result
        if c.execute("SELECT 1 FROM sessions s JOIN orders o ON o.id=s.order_id WHERE o.locker_id=? AND o.compartment_id=? AND s.closed=0", (locker_id, compartment)).fetchone():
            return result
        row = c.execute("SELECT * FROM orders WHERE locker_id=? AND compartment_id=? AND ((status='waiting_deposit' AND shipper_uid=?) OR (status='waiting_pickup' AND customer_uid=?))", (locker_id, compartment, uid, uid)).fetchone()
        if not row:
            return result
        action = "deposit" if row["status"] == "waiting_deposit" else "pickup"
        c.execute("INSERT INTO sessions(request_id,order_id,action,created_at) VALUES (?,?,?,?)", (request_id, row["id"], action, time.time()))
        result.update(allow=True, order_id=row["id"], action=action, nonce=secrets.token_hex(12), ttl_ms=5000)
    return result


def record_event(locker_id: str, event: dict) -> bool:
    event_id = str(event.get("event_id", ""))
    request_id = str(event.get("request_id", ""))
    kind = str(event.get("event", ""))
    if not event_id or kind not in {"unlock_pulsed", "door_open", "door_closed", "door_forced", "door_left_open"}:
        return False
    with db() as c:
        if c.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone():
            return False
        session = c.execute("SELECT s.*,o.locker_id,o.compartment_id FROM sessions s JOIN orders o ON o.id=s.order_id WHERE s.request_id=?", (request_id,)).fetchone()
        if kind != "door_forced" and (not session or session["locker_id"] != locker_id or session["compartment_id"] != event.get("compartment_id")):
            return False
        c.execute("INSERT INTO events VALUES (?,?,?,?)", (event_id, request_id, kind, time.time()))
        if kind == "door_open" and not session["closed"]:
            c.execute("UPDATE sessions SET opened=1 WHERE request_id=?", (request_id,))
        elif kind == "door_closed" and session["opened"] and not session["closed"]:
            c.execute("UPDATE sessions SET closed=1 WHERE request_id=?", (request_id,))
            new_status = "waiting_pickup" if session["action"] == "deposit" else "received"
            c.execute("UPDATE orders SET status=? WHERE id=?", (new_status, session["order_id"]))
    return True


init_db()
