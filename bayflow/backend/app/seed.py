"""Repeatable seed: python -m app.seed [--reset]"""
import sys

from sqlalchemy import select

from .db import Base, SessionLocal, engine
from .models import InventoryItem, Membership, Service, Shop, StockMovement, User, Vehicle
from .security import hash_password

PASSWORD = "Demo@1234"

SHOPS = [
    ("Lahore Auto Care", "Lahore", "Gulberg III, Main Boulevard", "042-111-000-001", "lhr"),
    ("Karachi Motor Works", "Karachi", "Shahrah-e-Faisal", "021-111-000-002", "khi"),
    ("Islamabad Garage Hub", "Islamabad", "G-9 Markaz", "051-111-000-003", "isb"),
]
SERVICES = [("Oil & Filter Change", 4500, 45), ("Brake Inspection & Pads", 9000, 90),
            ("AC Service", 7500, 60), ("Full Diagnostic", 3000, 60), ("Engine Tune-up", 12000, 120)]
PARTS = [("OIL-5W30", "Engine Oil 5W-30 (L)", 40, 10), ("FLT-OIL", "Oil Filter", 25, 5),
         ("BRK-PAD-F", "Front Brake Pads (set)", 8, 3), ("SPK-PLG", "Spark Plug", 30, 8),
         ("AC-GAS", "AC Gas R134a (can)", 12, 4), ("BAT-12V", "Car Battery 12V", 5, 2)]


def user(db, email, name, kind="staff", owner=False, phone=""):
    u = db.scalar(select(User).where(User.email == email))
    if not u:
        u = User(email=email, name=name, phone=phone, password_hash=hash_password(PASSWORD), kind=kind, is_owner=owner)
        db.add(u)
        db.flush()
    return u


def seed(reset=False):
    if reset:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if db.scalar(select(Shop)):
            print("Already seeded (use --reset to rebuild).")
            return
        owner = user(db, "owner@bayflow.demo", "Hamza Owner", owner=True)
        multi = user(db, "multi@bayflow.demo", "Maryam (Advisor @ Lahore, Technician @ Karachi)")
        for name, city, addr, phone, code in SHOPS:
            s = Shop(name=name, city=city, address=addr, phone=phone, owner_id=owner.id, capacity=2,
                     open_time="09:00", close_time="17:00", slot_minutes=60, closed_weekdays="6")
            db.add(s)
            db.flush()
            db.add(Membership(user_id=owner.id, shop_id=s.id, role="OWNER"))
            for role, label in [("SERVICE_ADVISOR", "advisor"), ("TECHNICIAN", "tech"), ("PARTS_PERSON", "parts"),
                                ("QC_INSPECTOR", "qc")]:
                u = user(db, f"{label}.{code}@bayflow.demo", f"{label.title()} {city}")
                db.add(Membership(user_id=u.id, shop_id=s.id, role=role))
            if code == "lhr":
                db.add(Membership(user_id=multi.id, shop_id=s.id, role="SERVICE_ADVISOR"))
            if code == "khi":
                db.add(Membership(user_id=multi.id, shop_id=s.id, role="TECHNICIAN"))
            for n, p, d in SERVICES:
                db.add(Service(shop_id=s.id, name=n, price=p, duration_min=d))
            for sku, n, qty, low in PARTS:
                i = InventoryItem(shop_id=s.id, sku=sku, name=n, on_hand=qty, reorder_level=low)
                db.add(i)
                db.flush()
                db.add(StockMovement(shop_id=s.id, item_id=i.id, kind="ADJUST", qty=qty, note="opening stock"))
        for n, e in [("Ali Raza", "ali@customer.demo"), ("Sara Khan", "sara@customer.demo")]:
            c = user(db, e, n, kind="customer", phone="0300-0000000")
            db.add(Vehicle(owner_id=c.id, make="Suzuki", model="Cultus", year=2019, plate="LEA-1234", odometer=60000))
        db.commit()
    print("Seeded 3 shops, staff for all roles, 2 customers. Password for every account:", PASSWORD)


if __name__ == "__main__":
    seed(reset="--reset" in sys.argv)
