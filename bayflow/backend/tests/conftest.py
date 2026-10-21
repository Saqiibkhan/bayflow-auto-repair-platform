import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["AUTO_CREATE_TABLES"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return TestClient(app)


def H(tok, **extra):
    return {"Authorization": f"Bearer {tok}", **extra}


def next_open_day(offset=1):
    from datetime import date, timedelta
    return date.today() + timedelta(days=offset)


class World:
    """One owner, one shop with all roles, a customer, stock and a service."""

    def __init__(self, c, tag="a", stock=10, capacity=2):
        self.c = c
        t = c.post("/api/auth/register-owner", json={"email": f"owner-{tag}@t.io", "password": "password1", "name": "Owner " + tag}).json()
        self.owner = t["token"]
        self.shop = c.post("/api/shops", headers=H(self.owner), json={
            "name": f"Shop {tag}", "city": "Lahore", "capacity": capacity, "closed_weekdays": ""}).json()["id"]
        self.svc = c.post(f"/api/shops/{self.shop}/services", headers=H(self.owner), json={"name": "Brake job", "price": 5000}).json()["id"]
        self.staff, self.uid = {}, {}
        for role in ["SERVICE_ADVISOR", "TECHNICIAN", "PARTS_PERSON", "QC_INSPECTOR"]:
            email = f"{role.lower()}-{tag}@t.io"
            res = c.post(f"/api/shops/{self.shop}/staff", headers=H(self.owner),
                         json={"email": email, "name": role, "password": "password1", "role": role})
            assert res.status_code == 201, res.text
            self.uid[role] = res.json()["user_id"]
            self.staff[role] = c.post("/api/auth/login", json={"email": email, "password": "password1"}).json()["token"]
        self.adv, self.tech, self.parts, self.qc = (self.staff[k] for k in ["SERVICE_ADVISOR", "TECHNICIAN", "PARTS_PERSON", "QC_INSPECTOR"])
        self.item = c.post(f"/api/shops/{self.shop}/inventory", headers=H(self.parts),
                           json={"sku": "PAD", "name": "Brake pads", "on_hand": stock, "reorder_level": 2}).json()["id"]
        self.cust = self.customer(f"cust-{tag}@t.io")

    def customer(self, email):
        return self.c.post("/api/auth/customer-access", json={"email": email, "password": "password1", "name": "Cust"}).json()["token"]

    def slot(self, i=0, day=1):
        d = next_open_day(day).isoformat()
        s = self.c.get(f"/api/public/shops/{self.shop}/slots", params={"date": d}).json()
        return s[i]["start"]

    def book(self, tok=None, i=0, key=None):
        hdr = H(tok or self.cust, **({"Idempotency-Key": key} if key else {}))
        return self.c.post("/api/bookings", headers=hdr, json={
            "shop_id": self.shop, "service_id": self.svc, "slot_start": self.slot(i),
            "vehicle": {"make": "Honda", "model": "Civic", "year": 2020, "plate": "ABC-123"}})

    def post(self, tok, path, json=None, **kw):
        return self.c.post(f"/api/{path}", headers=H(tok), json=json, **kw)

    def to_estimate_sent(self, qty=2):
        b = self.book().json()["id"]
        assert self.post(self.adv, f"bookings/{b}/confirm").status_code == 200
        assert self.post(self.adv, f"bookings/{b}/assign", {"technician_id": self.uid["TECHNICIAN"]}).status_code == 200
        assert self.post(self.tech, f"bookings/{b}/start-inspection").status_code == 200
        r = self.post(self.tech, f"bookings/{b}/estimate", {"diagnosis": "Worn pads", "items": [
            {"kind": "PART", "description": "Brake pads", "qty": qty, "unit_price": 3000, "inventory_item_id": self.item},
            {"kind": "LABOUR", "description": "Fitting", "qty": 1, "unit_price": 1500}]})
        assert r.status_code == 200, r.text
        assert self.post(self.adv, f"bookings/{b}/send-estimate").status_code == 200
        return b

    def to_repair(self, qty=2):
        b = self.to_estimate_sent(qty)
        assert self.post(self.cust, f"bookings/{b}/estimate/respond", {"accept": True}).status_code == 200
        r = self.post(self.parts, f"bookings/{b}/parts/allocate")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "PARTS_READY"
        assert self.post(self.tech, f"bookings/{b}/start-repair").status_code == 200
        return b

    def inv(self):
        return next(i for i in self.c.get(f"/api/shops/{self.shop}/inventory", headers=H(self.parts)).json() if i["id"] == self.item)

    def status(self, b):
        return self.c.get(f"/api/bookings/{b}", headers=H(self.adv)).json()["status"]
