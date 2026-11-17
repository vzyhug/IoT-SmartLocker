import os
import tempfile

path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["LOCKER_DB"] = path
from app import db, init_db, authorize, record_event  # noqa: E402


def test_deposit_pickup_and_duplicate_event():
    init_db()
    with db() as c:
        c.execute("INSERT INTO orders VALUES (?,?,?,?,?,?)", ("o1", "locker01", "A01", "AABBCCDD", "11223344", "waiting_deposit"))

    req = {"request_id": "r1", "compartment_id": "A01", "method": "rfid", "credential": "AABBCCDD"}
    assert authorize("locker01", req)["allow"]
    assert not authorize("locker01", req)["allow"]
    assert not authorize("locker01", {**req, "request_id": "r2", "credential": "11223344"})["allow"]
    assert record_event("locker01", {"event_id": "e0", "request_id": "r1", "compartment_id": "A01", "event": "door_closed"})
    with db() as c:
        assert c.execute("SELECT status FROM orders WHERE id='o1'").fetchone()[0] == "waiting_deposit"
    assert record_event("locker01", {"event_id": "e1", "request_id": "r1", "compartment_id": "A01", "event": "door_open"})
    assert record_event("locker01", {"event_id": "e2", "request_id": "r1", "compartment_id": "A01", "event": "door_closed"})
    assert not record_event("locker01", {"event_id": "e2", "request_id": "r1", "compartment_id": "A01", "event": "door_closed"})
    with db() as c:
        assert c.execute("SELECT status FROM orders WHERE id='o1'").fetchone()[0] == "waiting_pickup"
    assert authorize("locker01", {**req, "request_id": "r3", "credential": "11223344"})["allow"]
    assert record_event("locker01", {"event_id": "e3", "request_id": "r3", "compartment_id": "A01", "event": "door_open"})
    assert record_event("locker01", {"event_id": "e4", "request_id": "r3", "compartment_id": "A01", "event": "door_closed"})
    with db() as c:
        assert c.execute("SELECT status FROM orders WHERE id='o1'").fetchone()[0] == "received"
