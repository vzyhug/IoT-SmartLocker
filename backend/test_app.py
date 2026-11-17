import os
import json
import tempfile
import time

path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["LOCKER_DB"] = path
from app import db, init_db, authorize, record_event  # noqa: E402
import app as backend_app  # noqa: E402


def test_deposit_pickup_and_duplicate_event():
    init_db()
    with db() as c:
        c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status) VALUES (?,?,?,?,?,?)", ("o1", "locker01", "A01", "AABBCCDD", "11223344", "waiting_deposit"))

    req = {"request_id": "r1", "compartment_id": "A01", "method": "rfid", "credential": "AABBCCDD"}
    first_auth = authorize("locker01", req)
    assert first_auth["allow"]
    assert authorize("locker01", req) == first_auth  # QoS 1 retry returns the same response/nonce.
    assert not authorize("locker01", {**req, "request_id": "r2", "credential": "11223344"})["allow"]
    assert record_event("locker01", {"event_id": "e-auth", "request_id": "r1", "compartment_id": "A01", "event": "auth_ok"})
    assert not record_event("locker01", {"event_id": "e0", "request_id": "r1", "compartment_id": "A01", "event": "door_closed"})
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


def test_slot_reservation_and_invalid_event_order():
    init_db()
    with db() as c:
        c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status) VALUES (?,?,?,?,?,?)", ("o2", "locker01", "A02", "AABBCCDD", "11223344", "waiting_deposit"))
        c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status) VALUES (?,?,?,?,?,?)", ("o3", "locker01", "A02", "DDEEFF00", "55667788", "waiting_deposit"))
    req = {"request_id": "slot-r2", "compartment_id": "A02", "method": "rfid", "credential": "AABBCCDD"}
    assert authorize("locker01", req)["allow"]
    assert not authorize("locker01", {**req, "request_id": "r3", "credential": "DDEEFF00"})["allow"]
    bad = {"request_id": "slot-r2", "compartment_id": "A02", "event": "door_open"}
    assert not record_event("other-locker", {**bad, "event_id": "bad1"})
    assert not record_event("locker01", {**bad, "event_id": "bad2", "event": "door_closed"})
    assert record_event("locker01", {**bad, "event_id": "good1"})
    assert record_event("locker01", {**bad, "event_id": "good2", "event": "door_closed"})
    with db() as c:
        assert c.execute("SELECT status FROM orders WHERE id='o2'").fetchone()[0] == "waiting_pickup"
        assert c.execute("SELECT status FROM orders WHERE id='o3'").fetchone()[0] == "waiting_deposit"


def test_expired_unopened_session_releases_compartment():
    init_db()
    with db() as c:
        c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status) VALUES (?,?,?,?,?,?)", ("o4", "locker01", "A03", "AABBCCDD", "11223344", "waiting_deposit"))
    req = {"request_id": "r4", "compartment_id": "A03", "method": "rfid", "credential": "AABBCCDD"}
    assert authorize("locker01", req)["allow"]
    with db() as c:
        c.execute("UPDATE sessions SET created_at=? WHERE request_id='r4'", (time.time() - 61,))
    again = authorize("locker01", {**req, "request_id": "r5"})
    assert again["allow"]
    with db() as c:
        assert c.execute("SELECT expired FROM sessions WHERE request_id='r4'").fetchone()[0] == 1


def test_dashboard_requires_admin_for_data_and_reports_compartments(monkeypatch):
    from fastapi.testclient import TestClient
    init_db()
    with db() as c:
        c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status) VALUES (?,?,?,?,?,?)", ("o5", "locker01", "A04", "AABBCCDD", "11223344", "waiting_deposit"))
    monkeypatch.setattr(backend_app, "ADMIN_TOKEN", "test-admin")
    with TestClient(backend_app.api) as client:
        assert client.get("/dashboard").status_code == 200
        assert client.get("/dashboard/data").status_code == 401
        response = client.get("/dashboard/data", headers={"Authorization": "Bearer test-admin"})
        assert response.status_code == 200
        assert response.json()["compartments"][-1]["compartment_id"] == "A04"


def test_virtual_package_uid_and_weight_evidence():
    init_db()
    with db() as c:
        c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status,package_uid) VALUES (?,?,?,?,?,?,?)",
                  ("pkg1", "locker01", "A05", "AABBCCDD", "11223344", "waiting_deposit", "DEADBEEF"))
    req = {"request_id": "pkg-r1", "compartment_id": "A05", "method": "rfid", "credential": "AABBCCDD"}
    assert authorize("locker01", req)["allow"]
    assert record_event("locker01", {"event_id": "pkg-e1", "request_id": "pkg-r1", "compartment_id": "A05", "event": "door_open", "sequence": 1})
    assert record_event("locker01", {"event_id": "pkg-e2", "request_id": "pkg-r1", "compartment_id": "A05", "event": "door_closed", "sequence": 2,
                                      "package_uid": "DEADBEEF", "weight_before": 0.0, "weight_after": 0.2})
    with db() as c:
        assert c.execute("SELECT status FROM orders WHERE id='pkg1'").fetchone()[0] == "waiting_pickup"
        proof = c.execute("SELECT package_uid,expected_uid,passed FROM package_evidence WHERE request_id='pkg-r1'").fetchone()
        assert tuple(proof) == ("DEADBEEF", "DEADBEEF", 1)


def test_sequence_replay_is_rejected():
    init_db()
    with db() as c:
        c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status) VALUES (?,?,?,?,?,?)",
                  ("seq1", "locker01", "A06", "AABBCCDD", "11223344", "waiting_deposit"))
    assert authorize("locker01", {"request_id": "seq-r1", "compartment_id": "A06", "method": "rfid", "credential": "AABBCCDD"})["allow"]
    assert record_event("locker01", {"event_id": "seq-e1", "request_id": "seq-r1", "compartment_id": "A06", "event": "door_open", "sequence": 10})
    assert not record_event("locker01", {"event_id": "seq-e2", "request_id": "seq-r1", "compartment_id": "A06", "event": "door_closed", "sequence": 9})


def test_admin_config_and_unlock_commands_are_queued_and_acked(monkeypatch):
    from fastapi.testclient import TestClient
    init_db()
    monkeypatch.setattr(backend_app, "ADMIN_TOKEN", "test-admin")
    headers = {"Authorization": "Bearer test-admin"}
    with TestClient(backend_app.api) as client:
        assert client.post("/devices/config-test/config", json={"door_open_limit_ms": 45000}).status_code == 401
        config = client.post("/devices/config-test/config", json={"door_open_limit_ms": 45000}, headers=headers)
        assert client.post("/devices/config-test/config", json={"door_open_limit_ms": 30000, "config_version": 1}, headers=headers).status_code == 409
        next_config = client.post("/devices/config-test/config", json={"door_open_limit_ms": 60000}, headers=headers)
        assert next_config.json()["config_version"] == 2
        assert client.post("/devices/bad+id/unlock", headers=headers).status_code == 422
        unlock = client.post("/devices/config-test/unlock", headers=headers)
    assert config.status_code == 200
    assert config.json()["config_version"] == 1
    assert unlock.status_code == 200
    queued = {command["command_id"]: command for command in backend_app.pending_device_commands()}
    config_payload = json.loads(queued[config.json()["command_id"]]["payload"])
    unlock_payload = json.loads(queued[unlock.json()["command_id"]]["payload"])
    assert config_payload["door_open_limit_ms"] == 45000
    assert unlock_payload["action"] == "maintenance_unlock"
    backend_app.mark_command_sent(unlock.json()["command_id"])
    assert backend_app.acknowledge_device_command("config-test", {"command_id": unlock.json()["command_id"], "accepted": True})


def test_maintenance_unlock_audit_door_cycle():
    init_db()
    payload = {"command_id": "maint-test-1", "nonce": "n1", "expires_at": int(time.time()) + 10,
               "action": "maintenance_unlock", "compartment_id": "A01"}
    with db() as c:
        c.execute("INSERT INTO device_commands(command_id,locker_id,topic,payload,state,created_at) VALUES (?,?,?,?,?,?)",
                  (payload["command_id"], "locker01", "smartlocker/site01/locker01/cmd/unlock", json.dumps(payload), "acked", time.time()))
    for n, kind in enumerate(("unlock_pulsed", "door_open", "door_closed"), start=11):
        assert record_event("locker01", {"event_id": f"maint-e{n}", "request_id": payload["command_id"],
                                           "locker_id": "locker01", "compartment_id": "A01", "event": kind,
                                           "source": "maintenance", "sequence": n})
    with db() as c:
        assert c.execute("SELECT state FROM device_commands WHERE command_id=?", (payload["command_id"],)).fetchone()[0] == "complete"


def test_closing_door_after_left_open_keeps_review_state():
    init_db()
    with db() as c:
        c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status) VALUES (?,?,?,?,?,?)",
                  ("left1", "locker01", "A07", "AABBCCDD", "11223344", "waiting_deposit"))
    req = {"request_id": "left-r1", "compartment_id": "A07", "method": "rfid", "credential": "AABBCCDD"}
    assert authorize("locker01", req)["allow"]
    assert record_event("locker01", {"event_id": "left-e1", "request_id": "left-r1", "compartment_id": "A07", "event": "door_open"})
    assert record_event("locker01", {"event_id": "left-e2", "request_id": "left-r1", "compartment_id": "A07", "event": "door_left_open"})
    assert record_event("locker01", {"event_id": "left-e3", "request_id": "left-r1", "compartment_id": "A07", "event": "door_closed"})
    with db() as c:
        assert c.execute("SELECT status FROM orders WHERE id='left1'").fetchone()[0] == "needs_review"


def test_maintenance_door_left_open_can_still_be_closed_and_audited():
    init_db()
    payload = {"command_id": "maint-test-left", "nonce": "n2", "expires_at": int(time.time()) + 10,
               "action": "maintenance_unlock", "compartment_id": "A01"}
    with db() as c:
        c.execute("INSERT INTO device_commands(command_id,locker_id,topic,payload,state,created_at) VALUES (?,?,?,?,?,?)",
                  (payload["command_id"], "locker01", "smartlocker/site01/locker01/cmd/unlock", json.dumps(payload), "acked", time.time()))
    for n, kind in enumerate(("unlock_pulsed", "door_open", "door_left_open", "door_closed"), start=20):
        assert record_event("locker01", {"event_id": f"maint-left-e{n}", "request_id": payload["command_id"],
                                           "compartment_id": "A01", "event": kind, "source": "maintenance", "sequence": n})
    with db() as c:
        assert c.execute("SELECT state FROM device_commands WHERE command_id=?", (payload["command_id"],)).fetchone()[0] == "needs_review_closed"


def test_wrong_package_uid_or_no_weight_change_requires_review():
    init_db()
    cases = [("pkg-bad-uid", "A08", "BAADF00D", 0.0, 0.2),
             ("pkg-no-weight", "A09", "DEADBEEF", 0.2, 0.2)]
    for order_id, compartment, observed_uid, before, after in cases:
        with db() as c:
            c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status,package_uid) VALUES (?,?,?,?,?,?,?)",
                      (order_id, "locker01", compartment, "AABBCCDD", "11223344", "waiting_deposit", "DEADBEEF"))
        request_id = order_id + "-req"
        assert authorize("locker01", {"request_id": request_id, "compartment_id": compartment, "method": "rfid", "credential": "AABBCCDD"})["allow"]
        assert record_event("locker01", {"event_id": order_id + "-open", "request_id": request_id,
                                           "compartment_id": compartment, "event": "door_open"})
        assert record_event("locker01", {"event_id": order_id + "-close", "request_id": request_id,
                                           "compartment_id": compartment, "event": "door_closed", "package_uid": observed_uid,
                                           "weight_before": before, "weight_after": after})
        with db() as c:
            assert c.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()[0] == "needs_review"
            assert c.execute("SELECT passed FROM package_evidence WHERE request_id=?", (request_id,)).fetchone()[0] == 0


def test_customer_pickup_requires_matching_package_and_weight_removal():
    init_db()
    with db() as c:
        c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status,package_uid) VALUES (?,?,?,?,?,?,?)",
                  ("pkg-pickup", "locker01", "A10", "AABBCCDD", "11223344", "waiting_pickup", "DEADBEEF"))
    request_id = "pkg-pickup-req"
    assert authorize("locker01", {"request_id": request_id, "compartment_id": "A10", "method": "rfid", "credential": "11223344"})["allow"]
    assert record_event("locker01", {"event_id": "pkg-pickup-open", "request_id": request_id, "compartment_id": "A10", "event": "door_open"})
    assert record_event("locker01", {"event_id": "pkg-pickup-close", "request_id": request_id, "compartment_id": "A10", "event": "door_closed",
                                      "package_uid": "DEADBEEF", "weight_before": 0.2, "weight_after": 0.0})
    with db() as c:
        assert c.execute("SELECT status FROM orders WHERE id='pkg-pickup'").fetchone()[0] == "received"
        assert c.execute("SELECT passed FROM package_evidence WHERE request_id=?", (request_id,)).fetchone()[0] == 1


def test_pickup_otp_is_hashed_one_time_and_qos_retry_idempotent(monkeypatch):
    from fastapi.testclient import TestClient
    init_db()
    monkeypatch.setattr(backend_app, "ADMIN_TOKEN", "test-admin")
    monkeypatch.setattr(backend_app, "OTP_PEPPER", "test-otp-pepper-not-for-production")
    with db() as c:
        c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status) VALUES (?,?,?,?,?,?)",
                  ("otp-order", "locker-otp", "OTP01", "AABBCCDD", "11223344", "waiting_pickup"))
    with TestClient(backend_app.api) as client:
        issued = client.post("/orders/otp-order/pickup-credentials",
                             headers={"Authorization": "Bearer test-admin"})
    assert issued.status_code == 200
    credentials = issued.json()
    assert len(credentials["otp"]) == 6 and credentials["otp"].isdigit()
    assert credentials["qr_payload"].startswith("smartlocker://pickup/otp-order?token=")
    with db() as c:
        stored = c.execute("SELECT credential_hash FROM access_credentials WHERE order_id='otp-order' AND method='otp'").fetchone()[0]
    assert stored != credentials["otp"]

    request = {"request_id": "otp-auth-1", "compartment_id": "OTP01", "method": "otp", "credential": credentials["otp"]}
    allowed = authorize("locker-otp", request)
    assert allowed["allow"] and allowed["action"] == "pickup"
    assert authorize("locker-otp", request) == allowed
    assert not authorize("locker-otp", {**request, "request_id": "otp-auth-2"})["allow"]


def test_pickup_otp_locks_after_five_bad_attempts_and_qr_is_single_use(monkeypatch):
    from fastapi.testclient import TestClient
    init_db()
    monkeypatch.setattr(backend_app, "ADMIN_TOKEN", "test-admin")
    monkeypatch.setattr(backend_app, "OTP_PEPPER", "test-otp-pepper-not-for-production")
    with db() as c:
        c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status) VALUES (?,?,?,?,?,?)",
                  ("otp-lock", "locker-otp-2", "OTP01", "AABBCCDD", "11223344", "waiting_pickup"))
        c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status) VALUES (?,?,?,?,?,?)",
                  ("qr-order", "locker-otp-2", "QR01", "AABBCCDD", "11223344", "waiting_pickup"))
    with TestClient(backend_app.api) as client:
        otp = client.post("/orders/otp-lock/pickup-credentials", headers={"Authorization": "Bearer test-admin"}).json()["otp"]
        qr_token = client.post("/orders/qr-order/pickup-credentials", headers={"Authorization": "Bearer test-admin"}).json()["qr_token"]
    wrong_otp = "000000" if otp != "000000" else "000001"
    for i in range(5):
        denied = authorize("locker-otp-2", {"request_id": f"bad-otp-{i}", "compartment_id": "OTP01", "method": "otp", "credential": wrong_otp})
        assert not denied["allow"]
    assert not authorize("locker-otp-2", {"request_id": "locked-otp", "compartment_id": "OTP01", "method": "otp", "credential": otp})["allow"]

    qr_request = {"request_id": "qr-auth-1", "compartment_id": "QR01", "method": "qr", "credential": qr_token}
    qr_allowed = authorize("locker-otp-2", qr_request)
    assert qr_allowed["allow"] and qr_allowed["action"] == "pickup"
    assert not authorize("locker-otp-2", {**qr_request, "request_id": "qr-auth-2"})["allow"]


def test_pickup_credentials_cannot_be_issued_before_deposit(monkeypatch):
    from fastapi.testclient import TestClient
    init_db()
    monkeypatch.setattr(backend_app, "ADMIN_TOKEN", "test-admin")
    monkeypatch.setattr(backend_app, "OTP_PEPPER", "test-otp-pepper-not-for-production")
    with db() as c:
        c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status) VALUES (?,?,?,?,?,?)",
                  ("early-otp", "locker-otp-3", "OTP01", "AABBCCDD", "11223344", "waiting_deposit"))
    with TestClient(backend_app.api) as client:
        response = client.post("/orders/early-otp/pickup-credentials",
                               headers={"Authorization": "Bearer test-admin"})
    assert response.status_code == 409


def test_pickup_credential_expiry_is_enforced(monkeypatch):
    from fastapi.testclient import TestClient
    init_db()
    monkeypatch.setattr(backend_app, "ADMIN_TOKEN", "test-admin")
    monkeypatch.setattr(backend_app, "OTP_PEPPER", "test-otp-pepper-not-for-production")
    with db() as c:
        c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status) VALUES (?,?,?,?,?,?)",
                  ("expired-otp", "locker-expiry", "OTP01", "AABBCCDD", "11223344", "waiting_pickup"))
    with TestClient(backend_app.api) as client:
        issued = client.post("/orders/expired-otp/pickup-credentials",
                             headers={"Authorization": "Bearer test-admin"}).json()
    with db() as c:
        c.execute("UPDATE access_credentials SET expires_at=? WHERE order_id='expired-otp'", (time.time() - 1,))
    denied = authorize("locker-expiry", {"request_id": "expired-auth", "compartment_id": "OTP01",
                                          "method": "otp", "credential": issued["otp"]})
    assert not denied["allow"]


def test_offline_permission_is_one_time_and_reconciles_after_reconnect():
    init_db()
    order_id = "offline-reconcile"
    with db() as c:
        c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status) VALUES (?,?,?,?,?,?)",
                  (order_id, "locker-offline", "A01", "AABBCCDD", "11223344", "waiting_deposit"))
        permission_id = backend_app._create_offline_permission(c, order_id, "locker-offline", "A01", "AABBCCDD", "deposit")
    config_id = backend_app.queue_device_config("locker-offline")
    with db() as c:
        config = c.execute("SELECT payload FROM device_commands WHERE command_id=?", (config_id,)).fetchone()
        assert permission_id in config["payload"]
    req = "offline-" + permission_id
    authorized_at = int(time.time())
    assert record_event("locker-offline", {"event_id": "off-auth-" + permission_id, "request_id": req,
                         "compartment_id": "A01", "event": "auth_ok", "offline_permission_id": permission_id,
                         "authorized_at": authorized_at})
    assert not record_event("locker-offline", {"event_id": "off-auth-replay-" + permission_id, "request_id": req,
                             "compartment_id": "A01", "event": "auth_ok", "offline_permission_id": permission_id,
                             "authorized_at": authorized_at})
    assert record_event("locker-offline", {"event_id": "off-open-" + permission_id, "request_id": req,
                         "compartment_id": "A01", "event": "door_open", "offline_permission_id": permission_id,
                         "authorized_at": authorized_at})
    assert record_event("locker-offline", {"event_id": "off-close-" + permission_id, "request_id": req,
                         "compartment_id": "A01", "event": "door_closed", "offline_permission_id": permission_id,
                         "authorized_at": authorized_at})
    with db() as c:
        assert c.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()[0] == "waiting_pickup"
        assert c.execute("SELECT consumed_at FROM offline_permissions WHERE permission_id=?", (permission_id,)).fetchone()[0]
        assert c.execute("SELECT COUNT(*) FROM offline_permissions WHERE order_id=? AND action='pickup'", (order_id,)).fetchone()[0] == 1


def test_rfid_online_timeout_can_fall_back_to_synced_offline_permission():
    init_db()
    order_id = "offline-fallback"
    locker_id = "locker-fallback"
    with db() as c:
        c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status) VALUES (?,?,?,?,?,?)",
                  (order_id, locker_id, "A01", "AABBCCDD", "11223344", "waiting_deposit"))
        permission_id = backend_app._create_offline_permission(c, order_id, locker_id, "A01", "AABBCCDD", "deposit")
    online_request = {"request_id": "fallback-online", "compartment_id": "A01",
                      "method": "rfid", "credential": "AABBCCDD"}
    online_response = authorize(locker_id, online_request)
    assert online_response["allow"] and online_response["offline_permission_id"] == permission_id
    assert online_response["authorized_at"] > 0
    with db() as c:
        assert c.execute("SELECT consumed_at FROM offline_permissions WHERE permission_id=?", (permission_id,)).fetchone()[0] is None

    # The device times out waiting for the backend and durably queues auth_fail
    # before it uses the same synchronized permission locally.
    assert record_event(locker_id, {"event_id": "fallback-timeout", "request_id": "fallback-online",
                                    "compartment_id": "A01", "event": "auth_fail"})
    offline_request = "offline-" + permission_id
    authorized_at = int(time.time())
    assert record_event(locker_id, {"event_id": "fallback-offline-auth", "request_id": offline_request,
                                    "compartment_id": "A01", "event": "auth_ok",
                                    "offline_permission_id": permission_id, "authorized_at": authorized_at})
    assert record_event(locker_id, {"event_id": "fallback-offline-open", "request_id": offline_request,
                                    "compartment_id": "A01", "event": "door_open",
                                    "offline_permission_id": permission_id, "authorized_at": authorized_at})
    assert record_event(locker_id, {"event_id": "fallback-offline-close", "request_id": offline_request,
                                    "compartment_id": "A01", "event": "door_closed",
                                    "offline_permission_id": permission_id, "authorized_at": authorized_at})
    with db() as c:
        assert c.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()[0] == "waiting_pickup"
        assert c.execute("SELECT consumed_at FROM offline_permissions WHERE permission_id=?", (permission_id,)).fetchone()[0]


def test_online_auth_ok_consumes_associated_offline_grant_once():
    init_db()
    order_id = "online-consume-offline"
    locker_id = "locker-online-consume"
    request_id = "online-consume-auth"
    with db() as c:
        c.execute("INSERT INTO orders(id,locker_id,compartment_id,shipper_uid,customer_uid,status) VALUES (?,?,?,?,?,?)",
                  (order_id, locker_id, "A01", "AABBCCDD", "11223344", "waiting_deposit"))
        permission_id = backend_app._create_offline_permission(c, order_id, locker_id, "A01", "AABBCCDD", "deposit")
    response = authorize(locker_id, {"request_id": request_id, "compartment_id": "A01",
                                     "method": "rfid", "credential": "AABBCCDD"})
    assert response["allow"] and response["offline_permission_id"] == permission_id
    assert record_event(locker_id, {"event_id": "online-consume-event", "request_id": request_id,
                                   "compartment_id": "A01", "event": "auth_ok",
                                   "offline_permission_id": permission_id,
                                   "authorized_at": response["authorized_at"]})
    assert not record_event(locker_id, {"event_id": "online-consume-replay", "request_id": request_id,
                                        "compartment_id": "A01", "event": "auth_ok",
                                        "offline_permission_id": permission_id,
                                        "authorized_at": response["authorized_at"]})
    with db() as c:
        assert c.execute("SELECT consumed_at FROM offline_permissions WHERE permission_id=?", (permission_id,)).fetchone()[0]
