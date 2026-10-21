from concurrent.futures import ThreadPoolExecutor

import pytest

from app.models import BookingHistory
from app.db import SessionLocal
from tests.conftest import H, World


def test_requires_auth(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/bookings").status_code == 401


def test_full_lifecycle(client):
    w = World(client)
    b = w.to_repair(qty=2)
    assert w.inv()["reserved"] == 2 and w.inv()["on_hand"] == 10
    assert w.post(w.tech, f"bookings/{b}/submit-qc").status_code == 200
    assert w.inv()["on_hand"] == 8 and w.inv()["reserved"] == 0  # consumed
    assert w.post(w.qc, f"bookings/{b}/qc/claim").status_code == 200
    ok = {k: True for k in client.get("/api/qc/checklist").json()}
    assert w.post(w.qc, f"bookings/{b}/qc/pass", {"checklist": ok}).json()["status"] == "READY_FOR_PICKUP"
    assert w.post(w.adv, f"bookings/{b}/complete").json()["status"] == "COMPLETED"
    card = client.get(f"/api/bookings/{b}", headers=H(w.cust)).json()
    assert [h["to"] for h in card["history"]][0] == "PENDING" and card["history"][-1]["to"] == "COMPLETED"
    assert card["estimates"][0]["status"] == "ACCEPTED" and card["estimates"][0]["decided_at"]
    n = client.get("/api/notifications", headers=H(w.cust)).json()
    assert n["unread"] >= 4
    client.post("/api/notifications/read-all", headers=H(w.cust))
    assert client.get("/api/notifications", headers=H(w.cust)).json()["unread"] == 0
    moves = client.get(f"/api/shops/{w.shop}/stock-movements", headers=H(w.parts)).json()
    assert {m["kind"] for m in moves} >= {"ADJUST", "RESERVE", "CONSUME"}


def test_qc_failure_loop(client):
    w = World(client)
    b = w.to_repair()
    w.post(w.tech, f"bookings/{b}/submit-qc")
    w.post(w.qc, f"bookings/{b}/qc/claim")
    r = w.post(w.qc, f"bookings/{b}/qc/fail", {"issue": "Brake squeal remains"})
    assert r.json()["status"] == "IN_REPAIR" and r.json()["qc"][0]["status"] == "FAILED"
    assert w.post(w.tech, f"bookings/{b}/submit-qc").json()["status"] == "QC_PENDING"
    assert w.post(w.qc, f"bookings/{b}/qc/claim").status_code == 200
    ok = {k: True for k in client.get("/api/qc/checklist").json()}
    assert w.post(w.qc, f"bookings/{b}/qc/pass", {"checklist": ok}).json()["status"] == "READY_FOR_PICKUP"
    assert w.inv()["on_hand"] == 8  # parts only consumed once
    # incomplete checklist cannot pass
    b2 = w.to_repair()
    w.post(w.tech, f"bookings/{b2}/submit-qc")
    w.post(w.qc, f"bookings/{b2}/qc/claim")
    assert w.post(w.qc, f"bookings/{b2}/qc/pass", {"checklist": {"Brakes": True}}).status_code == 422


def test_estimate_rejected_revise_and_cancel(client):
    w = World(client)
    b = w.to_estimate_sent()
    assert w.post(w.cust, f"bookings/{b}/estimate/respond", {"accept": False, "reason": "too costly"}).json()["status"] == "ESTIMATE_REJECTED"
    assert w.post(w.adv, f"bookings/{b}/request-revision", {"reason": "cheaper option"}).json()["status"] == "INSPECTING"
    r = w.post(w.tech, f"bookings/{b}/estimate", {"diagnosis": "Cheaper pads", "items": [
        {"kind": "LABOUR", "description": "Fitting", "qty": 1, "unit_price": 1000}]})
    assert r.json()["estimates"][-1]["version"] == 2 and r.json()["estimates"][0]["status"] == "REJECTED"
    w.post(w.adv, f"bookings/{b}/send-estimate")
    assert w.post(w.cust, f"bookings/{b}/estimate/respond", {"accept": True}).json()["status"] == "ESTIMATE_APPROVED"
    # no parts needed -> straight to ready
    assert w.post(w.parts, f"bookings/{b}/parts/allocate").json()["status"] == "PARTS_READY"
    # second booking: reject then cancel
    b2 = w.to_estimate_sent()
    w.post(w.cust, f"bookings/{b2}/estimate/respond", {"accept": False})
    assert w.post(w.adv, f"bookings/{b2}/cancel", {"reason": "customer declined"}).json()["status"] == "CANCELLED"


def test_shortage_partial_receiving(client):
    w = World(client, stock=2)
    b = w.to_estimate_sent(qty=5)
    w.post(w.cust, f"bookings/{b}/estimate/respond", {"accept": True})
    r = w.post(w.parts, f"bookings/{b}/parts/allocate").json()
    assert r["status"] == "PARTS_PENDING" and r["allocations"][0]["reserved"] == 2
    po = w.post(w.parts, f"shops/{w.shop}/purchase-orders", {"supplier": "Auto Mart", "items": [{"item_id": w.item, "qty": 3}]}).json()
    line = po["lines"][0]["po_item_id"]
    po = w.post(w.parts, f"shops/{w.shop}/purchase-orders/{po['id']}/receive", {"lines": [{"po_item_id": line, "qty": 1}]}).json()
    assert po["status"] == "PARTIAL" and po["lines"][0]["received"] == 1 and w.inv()["on_hand"] == 3
    assert w.post(w.parts, f"shops/{w.shop}/purchase-orders/{po['id']}/receive", {"lines": [{"po_item_id": line, "qty": 3}]}).status_code == 422
    assert w.post(w.parts, f"bookings/{b}/parts/allocate").json()["status"] == "PARTS_PENDING"
    po = w.post(w.parts, f"shops/{w.shop}/purchase-orders/{po['id']}/receive", {"lines": [{"po_item_id": line, "qty": 2}]}).json()
    assert po["status"] == "RECEIVED" and w.inv()["on_hand"] == 5
    assert w.post(w.parts, f"bookings/{b}/parts/allocate").json()["status"] == "PARTS_READY"
    assert w.inv()["reserved"] == 5 and w.inv()["available"] == 0


def test_cancel_releases_reserved_stock_once(client):
    w = World(client)
    b = w.to_estimate_sent(qty=3)
    w.post(w.cust, f"bookings/{b}/estimate/respond", {"accept": True})
    w.post(w.parts, f"bookings/{b}/parts/allocate")
    assert w.inv()["reserved"] == 3
    assert w.post(w.cust, f"bookings/{b}/cancel").json()["status"] == "CANCELLED"
    assert w.inv()["reserved"] == 0 and w.inv()["available"] == 10
    assert w.post(w.cust, f"bookings/{b}/cancel").status_code == 409
    assert w.post(w.adv, f"bookings/{b}/cancel").status_code == 409
    assert w.inv()["reserved"] == 0
    # cancelled slot capacity is freed
    assert w.book(i=0).status_code == 201


def test_cannot_cancel_after_repair_starts(client):
    w = World(client)
    b = w.to_repair()
    assert w.post(w.cust, f"bookings/{b}/cancel").status_code == 409
    assert w.post(w.adv, f"bookings/{b}/cancel").status_code == 409


def test_tenant_and_customer_isolation(client):
    a, b_ = World(client, "a"), World(client, "b")
    bid = a.to_estimate_sent()
    # Shop B staff guessing Shop A booking id
    for tok in (b_.adv, b_.tech, b_.owner, b_.parts, b_.qc):
        assert client.get(f"/api/bookings/{bid}", headers=H(tok)).status_code == 404
        assert client.post(f"/api/bookings/{bid}/cancel", headers=H(tok)).status_code == 404
    # Shop B staff on Shop A endpoints
    assert client.get(f"/api/shops/{a.shop}/bookings", headers=H(b_.adv)).status_code == 403
    assert client.get(f"/api/shops/{a.shop}/inventory", headers=H(b_.parts)).status_code == 403
    assert client.get(f"/api/shops/{a.shop}/purchase-orders", headers=H(b_.parts)).status_code == 403
    assert client.get(f"/api/shops/{a.shop}/overview", headers=H(b_.owner)).status_code == 403
    assert client.patch(f"/api/shops/{a.shop}", headers=H(b_.owner), json={"name": "hacked"}).status_code == 403
    # other customer
    other = a.customer("other@t.io")
    assert client.get(f"/api/bookings/{bid}", headers=H(other)).status_code == 404
    assert client.post(f"/api/bookings/{bid}/estimate/respond", headers=H(other), json={"accept": True}).status_code == 404
    assert client.get("/api/bookings", headers=H(other)).json() == []
    assert client.get("/api/vehicles", headers=H(other)).json() == []
    # cannot book with someone else's vehicle
    vid = client.get("/api/vehicles", headers=H(a.cust)).json()[0]["id"]
    r = client.post("/api/bookings", headers=H(other), json={"shop_id": a.shop, "service_id": a.svc, "slot_start": a.slot(3), "vehicle_id": vid})
    assert r.status_code == 404
    # service from another shop is rejected
    r = client.post("/api/bookings", headers=H(a.cust), json={"shop_id": a.shop, "service_id": b_.svc, "slot_start": a.slot(3), "vehicle_id": vid})
    assert r.status_code == 404
    # cross-shop staff / inventory references
    assert a.post(a.owner, f"shops/{a.shop}/staff/{999}", None).status_code in (404, 405)
    r = a.post(a.parts, f"shops/{a.shop}/purchase-orders", {"supplier": "x", "items": [{"item_id": b_.item, "qty": 1}]})
    assert r.status_code == 422


def test_role_and_assignment_guards(client):
    w = World(client)
    b = w.book().json()["id"]
    assert w.post(w.cust, f"bookings/{b}/confirm").status_code == 403
    assert w.post(w.tech, f"bookings/{b}/confirm").status_code == 403
    assert w.post(w.adv, f"bookings/{b}/start-inspection").status_code == 409  # illegal from PENDING
    w.post(w.adv, f"bookings/{b}/confirm")
    assert w.post(w.adv, f"bookings/{b}/confirm").status_code == 409  # duplicate request
    # a second technician at the same shop who is not assigned
    w.post(w.owner, f"shops/{w.shop}/staff", {"email": "tech2@t.io", "name": "T2", "password": "password1", "role": "TECHNICIAN"})
    t2 = client.post("/api/auth/login", json={"email": "tech2@t.io", "password": "password1"}).json()["token"]
    w.post(w.adv, f"bookings/{b}/assign", {"technician_id": w.uid["TECHNICIAN"]})
    assert w.post(t2, f"bookings/{b}/start-inspection").status_code == 403
    assert client.get(f"/api/bookings/{b}", headers=H(t2)).status_code == 404  # not visible to unassigned tech
    w.post(w.tech, f"bookings/{b}/start-inspection")
    est = {"diagnosis": "x", "items": [{"kind": "LABOUR", "description": "l", "qty": 1, "unit_price": 10}]}
    assert w.post(t2, f"bookings/{b}/estimate", est).status_code == 403
    assert w.post(w.adv, f"bookings/{b}/estimate", est).status_code == 403
    assert w.post(w.adv, f"bookings/{b}/assign", {"technician_id": w.uid["PARTS_PERSON"]}).status_code in (409, 422)
    # customer cannot approve before an estimate is sent / advisor cannot approve for customer
    assert w.post(w.adv, f"bookings/{b}/estimate/respond", {"accept": True}).status_code in (403, 409)
    # QC inspector cannot pass a job someone else claimed
    b2 = w.to_repair()
    w.post(w.tech, f"bookings/{b2}/submit-qc")
    w.post(w.owner, f"shops/{w.shop}/staff", {"email": "qc2@t.io", "name": "Q2", "password": "password1", "role": "QC_INSPECTOR"})
    q2 = client.post("/api/auth/login", json={"email": "qc2@t.io", "password": "password1"}).json()["token"]
    w.post(w.qc, f"bookings/{b2}/qc/claim")
    ok = {k: True for k in client.get("/api/qc/checklist").json()}
    assert w.post(q2, f"bookings/{b2}/qc/pass", {"checklist": ok}).status_code == 403


def test_same_user_different_roles_in_different_shops(client):
    a, b_ = World(client, "a"), World(client, "b")
    email = "multi@t.io"
    a.post(a.owner, f"shops/{a.shop}/staff", {"email": email, "name": "M", "password": "password1", "role": "SERVICE_ADVISOR"})
    b_.post(b_.owner, f"shops/{b_.shop}/staff", {"email": email, "name": "M", "password": "password1", "role": "TECHNICIAN"})
    tok = client.post("/api/auth/login", json={"email": email, "password": "password1"}).json()["token"]
    roles = {m["shop_id"]: m["role"] for m in client.get("/api/me", headers=H(tok)).json()["memberships"]}
    assert roles == {a.shop: "SERVICE_ADVISOR", b_.shop: "TECHNICIAN"}
    bid = a.book().json()["id"]
    assert a.post(tok, f"bookings/{bid}/confirm").status_code == 200  # advisor at A
    bid_b = b_.book().json()["id"]
    assert b_.post(tok, f"bookings/{bid_b}/confirm").status_code == 403  # only technician at B


def test_deactivated_staff_loses_access(client):
    w = World(client)
    mid = next(m["membership_id"] for m in client.get(f"/api/shops/{w.shop}/staff", headers=H(w.owner)).json() if m["role"] == "SERVICE_ADVISOR")
    assert client.get(f"/api/shops/{w.shop}/bookings", headers=H(w.adv)).status_code == 200
    assert client.patch(f"/api/shops/{w.shop}/staff/{mid}", headers=H(w.owner), json={"active": False}).status_code == 200
    assert client.get(f"/api/shops/{w.shop}/bookings", headers=H(w.adv)).status_code == 403


def test_slot_capacity_under_concurrency(client):
    w = World(client, capacity=2)
    slot = w.slot(0)
    tokens = [w.customer(f"c{i}@t.io") for i in range(10)]

    def go(tok):
        return client.post("/api/bookings", headers=H(tok), json={
            "shop_id": w.shop, "service_id": w.svc, "slot_start": slot,
            "vehicle": {"make": "A", "model": "B", "year": 2020}}).status_code

    with ThreadPoolExecutor(10) as ex:
        codes = list(ex.map(go, tokens))
    assert codes.count(201) == 2 and codes.count(409) == 8
    slots = client.get(f"/api/public/shops/{w.shop}/slots", params={"date": slot[:10]}).json()
    assert next(s for s in slots if s["start"] == slot)["available"] == 0


def test_idempotency_and_no_duplicates(client):
    w = World(client)
    r1, r2 = w.book(key="k1"), w.book(key="k1")
    assert r1.json()["id"] == r2.json()["id"]
    assert len(client.get("/api/bookings", headers=H(w.cust)).json()) == 1
    body = {"supplier": "S", "items": [{"item_id": w.item, "qty": 4}]}
    p1 = client.post(f"/api/shops/{w.shop}/purchase-orders", headers=H(w.parts, **{"Idempotency-Key": "po1"}), json=body).json()
    p2 = client.post(f"/api/shops/{w.shop}/purchase-orders", headers=H(w.parts, **{"Idempotency-Key": "po1"}), json=body).json()
    assert p1["id"] == p2["id"]
    assert len(client.get(f"/api/shops/{w.shop}/purchase-orders", headers=H(w.parts)).json()) == 1
    line = p1["lines"][0]["po_item_id"]
    rb = {"lines": [{"po_item_id": line, "qty": 2}]}
    for _ in range(3):
        client.post(f"/api/shops/{w.shop}/purchase-orders/{p1['id']}/receive", headers=H(w.parts, **{"Idempotency-Key": "rcv1"}), json=rb)
    assert w.inv()["on_hand"] == 12  # 10 + 2, not 10 + 6


def test_concurrent_parts_allocation_never_oversells(client):
    w = World(client, stock=3)
    bookings = []
    for i in range(3):
        b = w.book(i=i).json()["id"]
        w.post(w.adv, f"bookings/{b}/confirm")
        w.post(w.adv, f"bookings/{b}/assign", {"technician_id": w.uid["TECHNICIAN"]})
        w.post(w.tech, f"bookings/{b}/start-inspection")
        w.post(w.tech, f"bookings/{b}/estimate", {"diagnosis": "d", "items": [
            {"kind": "PART", "description": "pads", "qty": 2, "unit_price": 100, "inventory_item_id": w.item}]})
        w.post(w.adv, f"bookings/{b}/send-estimate")
        w.post(w.cust, f"bookings/{b}/estimate/respond", {"accept": True})
        bookings.append(b)
    with ThreadPoolExecutor(3) as ex:
        list(ex.map(lambda b: w.post(w.parts, f"bookings/{b}/parts/allocate"), bookings))
    inv = w.inv()
    assert inv["reserved"] == 3 and inv["on_hand"] == 3 and inv["available"] == 0
    st = sorted(w.status(b) for b in bookings)
    assert st.count("PARTS_READY") == 1 and st.count("PARTS_PENDING") == 2


def test_booking_history_is_append_only(client):
    w = World(client)
    w.book()
    with SessionLocal() as db:
        h = db.query(BookingHistory).first()
        h.reason = "tamper"
        with pytest.raises(ValueError):
            db.commit()
        db.rollback()
        db.delete(h)
        with pytest.raises(ValueError):
            db.commit()


def test_invalid_slots_rejected(client):
    w = World(client)
    base = {"shop_id": w.shop, "service_id": w.svc, "vehicle": {"make": "A", "model": "B", "year": 2020}}
    for bad in ["2020-01-01T10:00:00", w.slot(0).replace("T09", "T09").replace(":00:00", ":30:00"), "garbage"]:
        assert client.post("/api/bookings", headers=H(w.cust), json={**base, "slot_start": bad}).status_code == 422
    # staff account cannot book
    assert client.post("/api/bookings", headers=H(w.adv), json={**base, "slot_start": w.slot(0)}).status_code == 403


def test_directory_and_shop_management(client):
    w = World(client)
    assert any(s["id"] == w.shop for s in client.get("/api/public/shops", params={"city": "lahore"}).json())
    assert client.get("/api/public/shops", params={"q": "zzzz"}).json() == []
    r = client.patch(f"/api/shops/{w.shop}", headers=H(w.owner), json={"capacity": 5, "slot_minutes": 30})
    assert r.json()["capacity"] == 5
    assert len(client.get(f"/api/public/shops/{w.shop}/slots", params={"date": w.slot(0)[:10]}).json()) > 10
    s2 = client.post("/api/shops", headers=H(w.owner), json={"name": "Second", "city": "Karachi"}).json()["id"]
    mine = client.get("/api/shops/mine", headers=H(w.owner)).json()
    assert {m["id"] for m in mine} == {w.shop, s2}
    assert client.post("/api/shops", headers=H(w.adv), json={"name": "x", "city": "y"}).status_code == 403
    assert client.get(f"/api/shops/{w.shop}/overview", headers=H(w.owner)).json()["staff_by_role"]["TECHNICIAN"] == 1
    # customer account is reusable, wrong password rejected
    assert client.post("/api/auth/customer-access", json={"email": "cust-a@t.io", "password": "wrongpass1", "name": "x"}).status_code == 401


def test_seed_script(client):
    from app.seed import seed
    seed(reset=True)
    c = client
    tok = c.post("/api/auth/login", json={"email": "owner@bayflow.demo", "password": "Demo@1234"}).json()["token"]
    shops = c.get("/api/shops/mine", headers=H(tok)).json()
    assert len(shops) == 3
    roles = set()
    for code in ["lhr", "khi", "isb"]:
        for label in ["advisor", "tech", "parts", "qc"]:
            t = c.post("/api/auth/login", json={"email": f"{label}.{code}@bayflow.demo", "password": "Demo@1234"})
            assert t.status_code == 200
            roles |= {m["role"] for m in t.json()["user"]["memberships"]}
    assert roles == {"SERVICE_ADVISOR", "TECHNICIAN", "PARTS_PERSON", "QC_INSPECTOR"}
    seed()  # repeatable
    assert len(c.get("/api/public/shops").json()) == 3
