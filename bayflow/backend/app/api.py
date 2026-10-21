"""BayFlow REST API (all routes live under /api)."""
import json
import time as _time
import uuid
from collections import defaultdict, deque
from datetime import date, datetime, time, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.security import HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from . import agent
from .db import get_db
from .models import (Booking, Call, CallbackTask, BookingHistory, Estimate, EstimateItem, IdempotencyRecord, InventoryItem,
                     Membership, Notification, PartAllocation, PurchaseOrder, PurchaseOrderItem,
                     QCInspection, Service, Shop, StockMovement, User, Vehicle, now)
from .security import hash_password, make_token, read_token, verify_password
from .workflow import *  # noqa: F403

r = APIRouter(prefix="/api")
bearer = HTTPBearer(auto_error=False)
EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
QC_CHECKLIST = ["Brakes", "Fluids", "Lights", "Test drive", "Cleanliness", "Work matches estimate"]


# ---------------------------------------------------------------- schemas
class RegisterIn(BaseModel):
    email: str = Field(pattern=EMAIL)
    password: str = Field(min_length=8)
    name: str = Field(min_length=1)
    phone: str = ""


class LoginIn(BaseModel):
    email: str
    password: str


class ShopIn(BaseModel):
    name: str = Field(min_length=1)
    city: str = Field(min_length=1)
    address: str = ""
    phone: str = ""
    open_time: str = Field("09:00", pattern=r"^\d\d:\d\d$")
    close_time: str = Field("17:00", pattern=r"^\d\d:\d\d$")
    slot_minutes: int = Field(60, ge=15, le=480)
    capacity: int = Field(2, ge=1, le=50)
    closed_weekdays: str = "6"
    closed_dates: str = ""


class ShopPatch(BaseModel):
    name: Optional[str] = None
    city: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    open_time: Optional[str] = Field(None, pattern=r"^\d\d:\d\d$")
    close_time: Optional[str] = Field(None, pattern=r"^\d\d:\d\d$")
    slot_minutes: Optional[int] = Field(None, ge=15, le=480)
    capacity: Optional[int] = Field(None, ge=1, le=50)
    closed_weekdays: Optional[str] = None
    closed_dates: Optional[str] = None
    active: Optional[bool] = None


class ServiceIn(BaseModel):
    name: str = Field(min_length=1)
    price: int = Field(0, ge=0)
    duration_min: int = Field(60, ge=5)
    active: bool = True


class StaffIn(BaseModel):
    email: str = Field(pattern=EMAIL)
    name: str = Field(min_length=1)
    password: str = Field(min_length=8)
    role: Literal["SERVICE_ADVISOR", "TECHNICIAN", "PARTS_PERSON", "QC_INSPECTOR"]


class StaffPatch(BaseModel):
    active: Optional[bool] = None
    role: Optional[Literal["SERVICE_ADVISOR", "TECHNICIAN", "PARTS_PERSON", "QC_INSPECTOR"]] = None


class VehicleIn(BaseModel):
    make: str = Field(min_length=1)
    model: str = Field(min_length=1)
    year: int = Field(ge=1950, le=2100)
    plate: str = ""
    odometer: int = Field(0, ge=0)


class BookingIn(BaseModel):
    shop_id: int
    service_id: int
    slot_start: str
    vehicle_id: Optional[int] = None
    vehicle: Optional[VehicleIn] = None
    notes: str = ""


class EstItem(BaseModel):
    kind: Literal["PART", "LABOUR"]
    description: str = Field(min_length=1)
    qty: int = Field(gt=0)
    unit_price: int = Field(ge=0)
    inventory_item_id: Optional[int] = None


class EstimateIn(BaseModel):
    diagnosis: str = ""
    items: list[EstItem] = Field(min_length=1)


class RespondIn(BaseModel):
    accept: bool
    reason: str = ""


class ReasonIn(BaseModel):
    reason: str = ""


class AssignIn(BaseModel):
    technician_id: int


class QCPassIn(BaseModel):
    checklist: dict[str, bool]


class QCFailIn(BaseModel):
    issue: str = Field(min_length=3)
    checklist: dict[str, bool] = {}


class ItemIn(BaseModel):
    sku: str = Field(min_length=1)
    name: str = Field(min_length=1)
    on_hand: int = Field(0, ge=0)
    reorder_level: int = Field(0, ge=0)


class AdjustIn(BaseModel):
    delta: int
    reason: str = "manual adjustment"


class POLine(BaseModel):
    item_id: int
    qty: int = Field(gt=0)


class POIn(BaseModel):
    supplier: str = Field(min_length=1)
    items: list[POLine] = Field(min_length=1)


class RecvLine(BaseModel):
    po_item_id: int
    qty: int = Field(gt=0)


class ReceiveIn(BaseModel):
    lines: list[RecvLine] = Field(min_length=1)


# ---------------------------------------------------------------- auth helpers
def current_user(cred=Depends(bearer), db=Depends(get_db)) -> User:
    if not cred:
        raise HTTPException(401, "Not authenticated")
    uid = read_token(cred.credentials)
    u = db.get(User, uid) if uid else None
    if not u:
        raise HTTPException(401, "Invalid or expired token")
    return u


def member(db, user, shop_id, roles=None) -> Membership:
    m = db.scalar(select(Membership).where(Membership.user_id == user.id, Membership.shop_id == shop_id,
                                           Membership.active == True))  # noqa: E712
    if not m or (roles and m.role not in roles):
        raise HTTPException(403, "You do not have access to this shop or action")
    return m


def booking_ctx(db, user, bid, lock=False):
    q = select(Booking).where(Booking.id == bid)
    if lock:
        q = q.with_for_update()
    b = db.scalar(q)
    if not b:
        raise HTTPException(404, "Booking not found")
    if b.customer_id == user.id:
        return b, CUSTOMER
    m = db.scalar(select(Membership).where(Membership.user_id == user.id, Membership.shop_id == b.shop_id,
                                           Membership.active == True))  # noqa: E712
    if not m:
        raise HTTPException(404, "Booking not found")
    return b, m.role


def visible(b, role, user):
    if role in (CUSTOMER, OWNER, ADVISOR):
        return True
    if role == TECH:
        return b.technician_id == user.id
    if role == QC:
        return b.status in (QC_PENDING, QC_IN_PROGRESS) or b.qc_inspector_id == user.id
    if role == PARTS:
        return b.status in (ESTIMATE_APPROVED, PARTS_PENDING, PARTS_READY, IN_REPAIR)
    return False


def idem_get(db, user, key, endpoint):
    if not key:
        return None
    rec = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.user_id == user.id,
                                                    IdempotencyRecord.key == key,
                                                    IdempotencyRecord.endpoint == endpoint))
    return rec.resource_id if rec else None


def idem_put(db, user, key, endpoint, rid):
    if key:
        db.add(IdempotencyRecord(user_id=user.id, key=key, endpoint=endpoint, resource_id=rid))


# ---------------------------------------------------------------- serializers
def user_out(db, u):
    ms = db.execute(select(Membership, Shop).join(Shop, Shop.id == Membership.shop_id)
                    .where(Membership.user_id == u.id, Membership.active == True)).all()  # noqa: E712
    return {"id": u.id, "email": u.email, "name": u.name, "kind": u.kind, "is_owner": u.is_owner,
            "memberships": [{"id": m.id, "shop_id": s.id, "shop_name": s.name, "role": m.role} for m, s in ms]}


def shop_out(s):
    return {"id": s.id, "name": s.name, "city": s.city, "address": s.address, "phone": s.phone,
            "open_time": s.open_time, "close_time": s.close_time, "slot_minutes": s.slot_minutes,
            "capacity": s.capacity, "closed_weekdays": s.closed_weekdays, "closed_dates": s.closed_dates,
            "active": s.active}


def veh_out(v):
    return {"id": v.id, "make": v.make, "model": v.model, "year": v.year, "plate": v.plate,
            "odometer": v.odometer, "label": f"{v.year} {v.make} {v.model} {v.plate}".strip()}


def summary(db, b):
    svc, veh, cust = db.get(Service, b.service_id), db.get(Vehicle, b.vehicle_id), db.get(User, b.customer_id)
    return {"id": b.id, "shop_id": b.shop_id, "status": b.status, "slot_start": b.slot_start.isoformat(),
            "service": svc.name if svc else "", "vehicle": veh_out(veh)["label"] if veh else "",
            "customer": cust.name if cust else "", "technician_id": b.technician_id}


def card(db, b, role):
    est = db.scalars(select(Estimate).where(Estimate.booking_id == b.id).order_by(Estimate.version)).all()
    if role == CUSTOMER:
        est = [e for e in est if e.status != "DRAFT"]
    ests = []
    for e in est:
        items = db.scalars(select(EstimateItem).where(EstimateItem.estimate_id == e.id)).all()
        ests.append({"id": e.id, "version": e.version, "status": e.status, "diagnosis": e.diagnosis,
                     "total": e.total, "decided_at": e.decided_at.isoformat() if e.decided_at else None,
                     "decision_reason": e.decision_reason,
                     "items": [{"kind": i.kind, "description": i.description, "qty": i.qty,
                                "unit_price": i.unit_price, "inventory_item_id": i.inventory_item_id} for i in items]})
    hist = db.scalars(select(BookingHistory).where(BookingHistory.booking_id == b.id).order_by(BookingHistory.id)).all()
    allocs = db.execute(select(PartAllocation, InventoryItem).join(InventoryItem, InventoryItem.id == PartAllocation.item_id)
                        .where(PartAllocation.booking_id == b.id)).all()
    qcs = db.scalars(select(QCInspection).where(QCInspection.booking_id == b.id).order_by(QCInspection.id)).all()
    tech = db.get(User, b.technician_id) if b.technician_id else None
    out = summary(db, b)
    out.update({
        "notes": b.notes, "role": role, "technician": tech.name if tech else None,
        "qc_inspector_id": b.qc_inspector_id, "estimates": ests,
        "history": [{"from": h.from_status, "to": h.to_status, "reason": h.reason, "at": h.at.isoformat(),
                     "actor": (db.get(User, h.actor_id).name if db.get(User, h.actor_id) else "")} for h in hist],
        "allocations": [{"item": i.name, "needed": a.qty_needed, "reserved": a.qty_reserved, "status": a.status}
                        for a, i in allocs],
        "qc": [{"id": q.id, "status": q.status, "issue": q.issue, "checklist": json.loads(q.checklist or "{}")} for q in qcs],
    })
    if role != CUSTOMER:
        cust = db.get(User, b.customer_id)
        out["customer_phone"] = cust.phone if cust else ""
    return out


def item_out(i):
    return {"id": i.id, "sku": i.sku, "name": i.name, "on_hand": i.on_hand, "reserved": i.reserved,
            "available": i.on_hand - i.reserved, "reorder_level": i.reorder_level}


# ---------------------------------------------------------------- auth routes
@r.post("/auth/register-owner")
def register_owner(body: RegisterIn, db=Depends(get_db)):
    email = body.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "Email already registered")
    u = User(email=email, name=body.name, phone=body.phone, password_hash=hash_password(body.password),
             kind="staff", is_owner=True)
    db.add(u)
    db.commit()
    return {"token": make_token(u.id), "user": user_out(db, u)}


@r.post("/auth/customer-access")
def customer_access(body: RegisterIn, db=Depends(get_db)):
    """Log in an existing customer or create the account (account reuse during booking)."""
    email = body.email.lower()
    u = db.scalar(select(User).where(User.email == email))
    if u:
        if u.kind != "customer" or not verify_password(body.password, u.password_hash):
            raise HTTPException(401, "Invalid credentials")
    else:
        u = User(email=email, name=body.name, phone=body.phone, password_hash=hash_password(body.password), kind="customer")
        db.add(u)
        db.commit()
    return {"token": make_token(u.id), "user": user_out(db, u)}


@r.post("/auth/login")
def login(body: LoginIn, db=Depends(get_db)):
    u = db.scalar(select(User).where(User.email == body.email.lower()))
    if not u or not verify_password(body.password, u.password_hash):
        raise HTTPException(401, "Invalid credentials")
    return {"token": make_token(u.id), "user": user_out(db, u)}


@r.get("/me")
def me(user=Depends(current_user), db=Depends(get_db)):
    return user_out(db, user)


# ---------------------------------------------------------------- shops (owner)
@r.post("/shops", status_code=201)
def create_shop(body: ShopIn, user=Depends(current_user), db=Depends(get_db)):
    if not user.is_owner:
        raise HTTPException(403, "Only owners can create shops")
    s = Shop(owner_id=user.id, **body.model_dump())
    db.add(s)
    db.flush()
    db.add(Membership(user_id=user.id, shop_id=s.id, role=OWNER))
    db.commit()
    return shop_out(s)


@r.get("/shops/mine")
def my_shops(user=Depends(current_user), db=Depends(get_db)):
    rows = db.execute(select(Shop, Membership).join(Membership, Membership.shop_id == Shop.id)
                      .where(Membership.user_id == user.id, Membership.active == True)).all()  # noqa: E712
    return [dict(shop_out(s), role=m.role) for s, m in rows]


@r.patch("/shops/{sid}")
def patch_shop(sid: int, body: ShopPatch, user=Depends(current_user), db=Depends(get_db)):
    member(db, user, sid, {OWNER})
    s = db.get(Shop, sid)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(s, k, v)
    db.commit()
    return shop_out(s)


@r.post("/shops/{sid}/services", status_code=201)
def add_service(sid: int, body: ServiceIn, user=Depends(current_user), db=Depends(get_db)):
    member(db, user, sid, {OWNER})
    s = Service(shop_id=sid, **body.model_dump())
    db.add(s)
    db.commit()
    return {"id": s.id, **body.model_dump()}


@r.patch("/shops/{sid}/services/{svc_id}")
def edit_service(sid: int, svc_id: int, body: ServiceIn, user=Depends(current_user), db=Depends(get_db)):
    member(db, user, sid, {OWNER})
    s = db.scalar(select(Service).where(Service.id == svc_id, Service.shop_id == sid))
    if not s:
        raise HTTPException(404, "Service not found")
    for k, v in body.model_dump().items():
        setattr(s, k, v)
    db.commit()
    return {"id": s.id, **body.model_dump()}


@r.get("/shops/{sid}/services")
def staff_services(sid: int, user=Depends(current_user), db=Depends(get_db)):
    member(db, user, sid)
    return [{"id": s.id, "name": s.name, "price": s.price, "duration_min": s.duration_min, "active": s.active}
            for s in db.scalars(select(Service).where(Service.shop_id == sid))]


@r.post("/shops/{sid}/staff", status_code=201)
def add_staff(sid: int, body: StaffIn, user=Depends(current_user), db=Depends(get_db)):
    member(db, user, sid, {OWNER})
    email = body.email.lower()
    u = db.scalar(select(User).where(User.email == email))
    if u and u.kind != "staff":
        raise HTTPException(409, "That email belongs to a customer account")
    if not u:
        u = User(email=email, name=body.name, password_hash=hash_password(body.password), kind="staff")
        db.add(u)
        db.flush()
    m = db.scalar(select(Membership).where(Membership.user_id == u.id, Membership.shop_id == sid))
    if m:
        if m.role == OWNER:
            raise HTTPException(409, "Cannot change the owner's membership")
        m.role, m.active = body.role, True
    else:
        m = Membership(user_id=u.id, shop_id=sid, role=body.role)
        db.add(m)
    db.commit()
    return {"membership_id": m.id, "user_id": u.id, "role": m.role}


@r.get("/shops/{sid}/staff")
def list_staff(sid: int, user=Depends(current_user), db=Depends(get_db)):
    member(db, user, sid, {OWNER, ADVISOR})
    rows = db.execute(select(Membership, User).join(User, User.id == Membership.user_id)
                      .where(Membership.shop_id == sid)).all()
    return [{"membership_id": m.id, "user_id": u.id, "name": u.name, "email": u.email, "role": m.role,
             "active": m.active} for m, u in rows]


@r.patch("/shops/{sid}/staff/{mid}")
def patch_staff(sid: int, mid: int, body: StaffPatch, user=Depends(current_user), db=Depends(get_db)):
    member(db, user, sid, {OWNER})
    m = db.scalar(select(Membership).where(Membership.id == mid, Membership.shop_id == sid))
    if not m:
        raise HTTPException(404, "Team member not found")
    if m.role == OWNER:
        raise HTTPException(409, "Cannot change the owner's membership")
    if body.active is not None:
        m.active = body.active
    if body.role:
        m.role = body.role
    db.commit()
    return {"membership_id": m.id, "role": m.role, "active": m.active}


@r.get("/shops/{sid}/overview")
def overview(sid: int, user=Depends(current_user), db=Depends(get_db)):
    member(db, user, sid, {OWNER})
    by = dict(db.execute(select(Booking.status, func.count()).where(Booking.shop_id == sid).group_by(Booking.status)).all())
    staff = dict(db.execute(select(Membership.role, func.count()).where(Membership.shop_id == sid, Membership.active == True)  # noqa: E712
                            .group_by(Membership.role)).all())
    items = db.scalars(select(InventoryItem).where(InventoryItem.shop_id == sid)).all()
    return {"bookings_by_status": by, "total_bookings": sum(by.values()), "staff_by_role": staff,
            "low_stock": [item_out(i) for i in items if i.on_hand - i.reserved <= i.reorder_level],
            "stock_items": len(items)}


# ---------------------------------------------------------------- public directory + slots
@r.get("/public/shops")
def public_shops(city: Optional[str] = None, q: Optional[str] = None, db=Depends(get_db)):
    stmt = select(Shop).where(Shop.active == True)  # noqa: E712
    if city:
        stmt = stmt.where(func.lower(Shop.city) == city.lower())
    if q:
        stmt = stmt.where(func.lower(Shop.name).contains(q.lower()) | func.lower(Shop.city).contains(q.lower()))
    return [shop_out(s) for s in db.scalars(stmt.order_by(Shop.name))]


@r.get("/public/shops/{sid}")
def public_shop(sid: int, db=Depends(get_db)):
    s = db.get(Shop, sid)
    if not s or not s.active:
        raise HTTPException(404, "Shop not found")
    svcs = db.scalars(select(Service).where(Service.shop_id == sid, Service.active == True))  # noqa: E712
    return dict(shop_out(s), services=[{"id": x.id, "name": x.name, "price": x.price,
                                         "duration_min": x.duration_min} for x in svcs])


def _hm(s):
    h, m = s.split(":")
    return time(int(h), int(m))


def day_slots(shop, d: date):
    if str(d.weekday()) in [x.strip() for x in shop.closed_weekdays.split(",") if x.strip()]:
        return []
    if d.isoformat() in [x.strip() for x in shop.closed_dates.split(",")]:
        return []
    out, cur = [], datetime.combine(d, _hm(shop.open_time))
    end = datetime.combine(d, _hm(shop.close_time))
    while cur + timedelta(minutes=shop.slot_minutes) <= end:
        out.append(cur)
        cur += timedelta(minutes=shop.slot_minutes)
    return out


def booked_count(db, shop_id, slot):
    return db.scalar(select(func.count()).select_from(Booking).where(
        Booking.shop_id == shop_id, Booking.slot_start == slot, Booking.status != CANCELLED)) or 0


@r.get("/public/shops/{sid}/slots")
def slots(sid: int, date: str, db=Depends(get_db)):
    s = db.get(Shop, sid)
    if not s or not s.active:
        raise HTTPException(404, "Shop not found")
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    out = []
    for st in day_slots(s, d):
        if st <= now():
            continue
        out.append({"start": st.isoformat(), "available": max(0, s.capacity - booked_count(db, sid, st))})
    return out


# ---------------------------------------------------------------- vehicles
@r.get("/vehicles")
def my_vehicles(user=Depends(current_user), db=Depends(get_db)):
    return [veh_out(v) for v in db.scalars(select(Vehicle).where(Vehicle.owner_id == user.id))]


@r.post("/vehicles", status_code=201)
def add_vehicle(body: VehicleIn, user=Depends(current_user), db=Depends(get_db)):
    v = Vehicle(owner_id=user.id, **body.model_dump())
    db.add(v)
    db.commit()
    return veh_out(v)


# ---------------------------------------------------------------- bookings
@r.post("/bookings", status_code=201)
def create_booking(body: BookingIn, user=Depends(current_user), db=Depends(get_db),
                   idempotency_key: Optional[str] = Header(None)):
    if user.kind != "customer":
        raise HTTPException(403, "Only customers can book. Use a customer account.")
    with WRITE_LOCK:
        existing = idem_get(db, user, idempotency_key, "booking")
        if existing:
            b = db.get(Booking, existing)
            return card(db, b, CUSTOMER)
        shop = db.scalar(select(Shop).where(Shop.id == body.shop_id).with_for_update())
        if not shop or not shop.active:
            raise HTTPException(404, "Shop not found")
        svc = db.scalar(select(Service).where(Service.id == body.service_id, Service.shop_id == shop.id, Service.active == True))  # noqa: E712
        if not svc:
            raise HTTPException(404, "Service not offered by this shop")
        try:
            slot = datetime.fromisoformat(body.slot_start)
        except ValueError:
            raise HTTPException(422, "Invalid slot_start")
        if slot <= now() or slot not in day_slots(shop, slot.date()):
            raise HTTPException(422, "Not a valid bookable slot for this shop")
        if booked_count(db, shop.id, slot) >= shop.capacity:
            raise HTTPException(409, "This slot is fully booked")
        if body.vehicle_id:
            veh = db.scalar(select(Vehicle).where(Vehicle.id == body.vehicle_id, Vehicle.owner_id == user.id))
            if not veh:
                raise HTTPException(404, "Vehicle not found")
        elif body.vehicle:
            veh = Vehicle(owner_id=user.id, **body.vehicle.model_dump())
            db.add(veh)
            db.flush()
        else:
            raise HTTPException(422, "Vehicle details required")
        b = Booking(shop_id=shop.id, customer_id=user.id, vehicle_id=veh.id, service_id=svc.id,
                    slot_start=slot, notes=body.notes)
        db.add(b)
        db.flush()
        db.add(BookingHistory(booking_id=b.id, actor_id=user.id, from_status=None, to_status=PENDING, reason="Booked online"))
        from .workflow import _notify_status
        _notify_status(db, b, PENDING)
        idem_put(db, user, idempotency_key, "booking", b.id)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Duplicate request")
        return card(db, b, CUSTOMER)


@r.get("/bookings")
def my_bookings(user=Depends(current_user), db=Depends(get_db)):
    rows = db.scalars(select(Booking).where(Booking.customer_id == user.id).order_by(Booking.id.desc()))
    return [summary(db, b) for b in rows]


@r.get("/shops/{sid}/bookings")
def shop_bookings(sid: int, status: Optional[str] = None, user=Depends(current_user), db=Depends(get_db)):
    m = member(db, user, sid)
    stmt = select(Booking).where(Booking.shop_id == sid)
    if status:
        stmt = stmt.where(Booking.status == status)
    rows = [b for b in db.scalars(stmt.order_by(Booking.slot_start.desc()).limit(300)) if visible(b, m.role, user)]
    return [summary(db, b) for b in rows]


@r.get("/bookings/{bid}")
def get_booking(bid: int, user=Depends(current_user), db=Depends(get_db)):
    b, role = booking_ctx(db, user, bid)
    if not visible(b, role, user):
        raise HTTPException(404, "Booking not found")
    return card(db, b, role)


def _do(db, user, bid, to, reason="", after=None):
    with WRITE_LOCK:
        b, role = booking_ctx(db, user, bid, lock=True)
        transition(db, b, to, user, role, reason)
        if after:
            after(b)
        db.commit()
        return card(db, b, role)


def _latest(db, bid, *statuses):
    return db.scalar(select(Estimate).where(Estimate.booking_id == bid, Estimate.status.in_(statuses))
                     .order_by(Estimate.version.desc()))


def _release(db, b, actor):
    for a in db.scalars(select(PartAllocation).where(PartAllocation.booking_id == b.id, PartAllocation.status == "ACTIVE")):
        if a.qty_reserved:
            item = db.scalar(select(InventoryItem).where(InventoryItem.id == a.item_id).with_for_update())
            item.reserved -= a.qty_reserved
            db.add(StockMovement(shop_id=b.shop_id, item_id=item.id, kind="RELEASE", qty=a.qty_reserved,
                                 booking_id=b.id, actor_id=actor.id, note="booking cancelled"))
        a.status = "RELEASED"


@r.post("/bookings/{bid}/confirm")
def confirm(bid: int, user=Depends(current_user), db=Depends(get_db)):
    def check(b):
        shop = db.get(Shop, b.shop_id)
        if booked_count(db, b.shop_id, b.slot_start) > shop.capacity:
            raise HTTPException(409, "Slot is over capacity")
    return _do(db, user, bid, CONFIRMED, "Confirmed by advisor", check)


@r.post("/bookings/{bid}/assign")
def assign(bid: int, body: AssignIn, user=Depends(current_user), db=Depends(get_db)):
    with WRITE_LOCK:
        b, role = booking_ctx(db, user, bid, lock=True)
        if role not in ADMIN:
            raise HTTPException(403, "Only the owner or a service advisor can assign technicians")
        tm = db.scalar(select(Membership).where(Membership.user_id == body.technician_id, Membership.shop_id == b.shop_id,
                                                Membership.role == TECH, Membership.active == True))  # noqa: E712
        if not tm:
            raise HTTPException(422, "Technician does not belong to this shop")
        b.technician_id = body.technician_id
        transition(db, b, ASSIGNED, user, role, "Technician assigned")
        db.commit()
        return card(db, b, role)


@r.post("/bookings/{bid}/start-inspection")
def start_inspection(bid: int, user=Depends(current_user), db=Depends(get_db)):
    return _do(db, user, bid, INSPECTING, "Inspection started")


@r.post("/bookings/{bid}/estimate")
def submit_estimate(bid: int, body: EstimateIn, user=Depends(current_user), db=Depends(get_db)):
    with WRITE_LOCK:
        b, role = booking_ctx(db, user, bid, lock=True)
        if role != TECH or b.technician_id != user.id:
            raise HTTPException(403, "Only the assigned technician can submit an estimate")
        if b.status != INSPECTING:
            raise HTTPException(409, f"Cannot submit an estimate while booking is {b.status}")
        for it in body.items:
            if it.kind == "PART":
                inv = db.get(InventoryItem, it.inventory_item_id) if it.inventory_item_id else None
                if not inv or inv.shop_id != b.shop_id:
                    raise HTTPException(422, "PART lines need an inventory item from this shop")
        ver = (db.scalar(select(func.max(Estimate.version)).where(Estimate.booking_id == bid)) or 0) + 1
        for old in db.scalars(select(Estimate).where(Estimate.booking_id == bid, Estimate.status.in_(["DRAFT", "SENT"]))):
            old.status = "SUPERSEDED"
        e = Estimate(booking_id=bid, version=ver, diagnosis=body.diagnosis, created_by=user.id,
                     total=sum(i.qty * i.unit_price for i in body.items))
        db.add(e)
        db.flush()
        for i in body.items:
            db.add(EstimateItem(estimate_id=e.id, **i.model_dump()))
        transition(db, b, ESTIMATE_REVIEW, user, role, f"Estimate v{ver} submitted")
        db.commit()
        return card(db, b, role)


@r.post("/bookings/{bid}/send-estimate")
def send_estimate(bid: int, user=Depends(current_user), db=Depends(get_db)):
    def after(b):
        e = _latest(db, b.id, "DRAFT")
        if not e:
            raise HTTPException(409, "No draft estimate to send")
        e.status = "SENT"
    return _do(db, user, bid, AWAITING_CUSTOMER, "Estimate sent to customer", after)


@r.post("/bookings/{bid}/estimate/respond")
def respond_estimate(bid: int, body: RespondIn, user=Depends(current_user), db=Depends(get_db)):
    def after(b):
        e = _latest(db, b.id, "SENT")
        if not e:
            raise HTTPException(409, "No estimate awaiting a decision")
        e.status = "ACCEPTED" if body.accept else "REJECTED"
        e.decided_at = now()
        e.decision_reason = body.reason
    to = ESTIMATE_APPROVED if body.accept else ESTIMATE_REJECTED
    return _do(db, user, bid, to, body.reason or ("Estimate accepted" if body.accept else "Estimate rejected"), after)


@r.post("/bookings/{bid}/request-revision")
def request_revision(bid: int, body: ReasonIn = ReasonIn(), user=Depends(current_user), db=Depends(get_db)):
    return _do(db, user, bid, INSPECTING, body.reason or "Estimate revision requested")


@r.post("/bookings/{bid}/parts/allocate")
def allocate_parts(bid: int, user=Depends(current_user), db=Depends(get_db)):
    with WRITE_LOCK:
        b, role = booking_ctx(db, user, bid, lock=True)
        if role != PARTS:
            raise HTTPException(403, "Only a parts person can allocate parts")
        if b.status not in (ESTIMATE_APPROVED, PARTS_PENDING):
            raise HTTPException(409, f"Cannot allocate parts while booking is {b.status}")
        est = _latest(db, b.id, "ACCEPTED")
        if not est:
            raise HTTPException(409, "No approved estimate")
        need = {}
        for it in db.scalars(select(EstimateItem).where(EstimateItem.estimate_id == est.id, EstimateItem.kind == "PART")):
            need[it.inventory_item_id] = need.get(it.inventory_item_id, 0) + it.qty
        for iid, qty in need.items():
            if not db.scalar(select(PartAllocation).where(PartAllocation.booking_id == b.id, PartAllocation.item_id == iid)):
                db.add(PartAllocation(booking_id=b.id, item_id=iid, qty_needed=qty))
        db.flush()
        allocs = db.scalars(select(PartAllocation).where(PartAllocation.booking_id == b.id, PartAllocation.status == "ACTIVE")).all()
        for a in allocs:
            item = db.scalar(select(InventoryItem).where(InventoryItem.id == a.item_id).with_for_update())
            take = min(item.on_hand - item.reserved, a.qty_needed - a.qty_reserved)
            if take > 0:
                item.reserved += take
                a.qty_reserved += take
                db.add(StockMovement(shop_id=b.shop_id, item_id=item.id, kind="RESERVE", qty=take, booking_id=b.id,
                                     actor_id=user.id, note="reserved for job"))
        ready = all(a.qty_reserved >= a.qty_needed for a in allocs)
        target = PARTS_READY if ready else PARTS_PENDING
        if not (b.status == PARTS_PENDING and target == PARTS_PENDING):
            transition(db, b, target, user, role, "All parts reserved" if ready else "Waiting for missing parts")
        db.commit()
        return card(db, b, role)


@r.post("/bookings/{bid}/start-repair")
def start_repair(bid: int, user=Depends(current_user), db=Depends(get_db)):
    return _do(db, user, bid, IN_REPAIR, "Repair started")


@r.post("/bookings/{bid}/submit-qc")
def submit_qc(bid: int, user=Depends(current_user), db=Depends(get_db)):
    def consume(b):
        for a in db.scalars(select(PartAllocation).where(PartAllocation.booking_id == b.id, PartAllocation.status == "ACTIVE")):
            item = db.scalar(select(InventoryItem).where(InventoryItem.id == a.item_id).with_for_update())
            item.on_hand -= a.qty_reserved
            item.reserved -= a.qty_reserved
            db.add(StockMovement(shop_id=b.shop_id, item_id=item.id, kind="CONSUME", qty=a.qty_reserved,
                                 booking_id=b.id, actor_id=user.id, note="used in repair"))
            a.status = "CONSUMED"
    return _do(db, user, bid, QC_PENDING, "Repair finished, sent to QC", consume)


@r.get("/qc/checklist")
def qc_checklist():
    return QC_CHECKLIST


@r.post("/bookings/{bid}/qc/claim")
def qc_claim(bid: int, user=Depends(current_user), db=Depends(get_db)):
    def after(b):
        b.qc_inspector_id = user.id
        db.add(QCInspection(booking_id=b.id, inspector_id=user.id))
    return _do(db, user, bid, QC_IN_PROGRESS, "QC inspection started", after)


def _qc_record(db, b):
    return db.scalar(select(QCInspection).where(QCInspection.booking_id == b.id, QCInspection.status == "IN_PROGRESS")
                     .order_by(QCInspection.id.desc()))


@r.post("/bookings/{bid}/qc/pass")
def qc_pass(bid: int, body: QCPassIn, user=Depends(current_user), db=Depends(get_db)):
    missing = [k for k in QC_CHECKLIST if not body.checklist.get(k)]
    if missing:
        raise HTTPException(422, "All checklist items must be ticked to pass: " + ", ".join(missing))

    def after(b):
        q = _qc_record(db, b)
        q.status, q.checklist, q.finished_at = "PASSED", json.dumps(body.checklist), now()
    return _do(db, user, bid, READY_FOR_PICKUP, "QC passed", after)


@r.post("/bookings/{bid}/qc/fail")
def qc_fail(bid: int, body: QCFailIn, user=Depends(current_user), db=Depends(get_db)):
    def after(b):
        q = _qc_record(db, b)
        q.status, q.issue, q.checklist, q.finished_at = "FAILED", body.issue, json.dumps(body.checklist), now()
        b.qc_inspector_id = None
    return _do(db, user, bid, IN_REPAIR, "QC failed: " + body.issue, after)


@r.post("/bookings/{bid}/complete")
def complete(bid: int, user=Depends(current_user), db=Depends(get_db)):
    return _do(db, user, bid, COMPLETED, "Vehicle collected")


@r.post("/bookings/{bid}/cancel")
def cancel(bid: int, body: ReasonIn = ReasonIn(), user=Depends(current_user), db=Depends(get_db)):
    return _do(db, user, bid, CANCELLED, body.reason or "Cancelled",
               lambda b: _release(db, b, user))


# ---------------------------------------------------------------- inventory + purchase orders
INV_READ = {OWNER, ADVISOR, PARTS, TECH}
INV_WRITE = {OWNER, PARTS}


@r.get("/shops/{sid}/inventory")
def inventory(sid: int, user=Depends(current_user), db=Depends(get_db)):
    member(db, user, sid, INV_READ)
    return [item_out(i) for i in db.scalars(select(InventoryItem).where(InventoryItem.shop_id == sid).order_by(InventoryItem.name))]


@r.post("/shops/{sid}/inventory", status_code=201)
def add_item(sid: int, body: ItemIn, user=Depends(current_user), db=Depends(get_db)):
    member(db, user, sid, INV_WRITE)
    with WRITE_LOCK:
        if db.scalar(select(InventoryItem).where(InventoryItem.shop_id == sid, InventoryItem.sku == body.sku)):
            raise HTTPException(409, "SKU already exists in this shop")
        i = InventoryItem(shop_id=sid, sku=body.sku, name=body.name, on_hand=body.on_hand, reorder_level=body.reorder_level)
        db.add(i)
        db.flush()
        if body.on_hand:
            db.add(StockMovement(shop_id=sid, item_id=i.id, kind="ADJUST", qty=body.on_hand, actor_id=user.id, note="opening stock"))
        db.commit()
        return item_out(i)


@r.post("/shops/{sid}/inventory/{iid}/adjust")
def adjust_item(sid: int, iid: int, body: AdjustIn, user=Depends(current_user), db=Depends(get_db)):
    member(db, user, sid, INV_WRITE)
    with WRITE_LOCK:
        i = db.scalar(select(InventoryItem).where(InventoryItem.id == iid, InventoryItem.shop_id == sid).with_for_update())
        if not i:
            raise HTTPException(404, "Item not found")
        if i.on_hand + body.delta < i.reserved:
            raise HTTPException(409, "Adjustment would drop stock below reserved quantity")
        i.on_hand += body.delta
        db.add(StockMovement(shop_id=sid, item_id=i.id, kind="ADJUST", qty=body.delta, actor_id=user.id, note=body.reason))
        db.commit()
        return item_out(i)


@r.get("/shops/{sid}/stock-movements")
def movements(sid: int, item_id: Optional[int] = None, user=Depends(current_user), db=Depends(get_db)):
    member(db, user, sid, {OWNER, PARTS})
    stmt = select(StockMovement).where(StockMovement.shop_id == sid)
    if item_id:
        stmt = stmt.where(StockMovement.item_id == item_id)
    return [{"id": m.id, "item_id": m.item_id, "kind": m.kind, "qty": m.qty, "booking_id": m.booking_id,
             "po_id": m.po_id, "note": m.note, "at": m.at.isoformat()}
            for m in db.scalars(stmt.order_by(StockMovement.id.desc()).limit(200))]


def po_out(db, po):
    lines = db.execute(select(PurchaseOrderItem, InventoryItem).join(InventoryItem, InventoryItem.id == PurchaseOrderItem.item_id)
                       .where(PurchaseOrderItem.po_id == po.id)).all()
    return {"id": po.id, "supplier": po.supplier, "status": po.status, "created_at": po.created_at.isoformat(),
            "lines": [{"po_item_id": l.id, "item_id": i.id, "item": i.name, "ordered": l.ordered, "received": l.received}
                      for l, i in lines]}


@r.post("/shops/{sid}/purchase-orders", status_code=201)
def create_po(sid: int, body: POIn, user=Depends(current_user), db=Depends(get_db),
              idempotency_key: Optional[str] = Header(None)):
    member(db, user, sid, INV_WRITE)
    with WRITE_LOCK:
        ex = idem_get(db, user, idempotency_key, "po-create")
        if ex:
            return po_out(db, db.get(PurchaseOrder, ex))
        for l in body.items:
            it = db.get(InventoryItem, l.item_id)
            if not it or it.shop_id != sid:
                raise HTTPException(422, "Item does not belong to this shop")
        po = PurchaseOrder(shop_id=sid, supplier=body.supplier, created_by=user.id)
        db.add(po)
        db.flush()
        for l in body.items:
            db.add(PurchaseOrderItem(po_id=po.id, item_id=l.item_id, ordered=l.qty))
        idem_put(db, user, idempotency_key, "po-create", po.id)
        db.commit()
        return po_out(db, po)


@r.get("/shops/{sid}/purchase-orders")
def list_pos(sid: int, user=Depends(current_user), db=Depends(get_db)):
    member(db, user, sid, INV_WRITE)
    return [po_out(db, p) for p in db.scalars(select(PurchaseOrder).where(PurchaseOrder.shop_id == sid).order_by(PurchaseOrder.id.desc()))]


@r.post("/shops/{sid}/purchase-orders/{po_id}/receive")
def receive_po(sid: int, po_id: int, body: ReceiveIn, user=Depends(current_user), db=Depends(get_db),
               idempotency_key: Optional[str] = Header(None)):
    member(db, user, sid, INV_WRITE)
    with WRITE_LOCK:
        po = db.scalar(select(PurchaseOrder).where(PurchaseOrder.id == po_id, PurchaseOrder.shop_id == sid).with_for_update())
        if not po:
            raise HTTPException(404, "Purchase order not found")
        if idem_get(db, user, idempotency_key, f"po-receive-{po_id}"):
            return po_out(db, po)
        if po.status == "RECEIVED":
            raise HTTPException(409, "Purchase order already fully received")
        for l in body.lines:
            li = db.scalar(select(PurchaseOrderItem).where(PurchaseOrderItem.id == l.po_item_id, PurchaseOrderItem.po_id == po.id))
            if not li:
                raise HTTPException(404, "Line not found on this purchase order")
            if l.qty > li.ordered - li.received:
                raise HTTPException(422, f"Cannot receive {l.qty}; only {li.ordered - li.received} outstanding")
            item = db.scalar(select(InventoryItem).where(InventoryItem.id == li.item_id).with_for_update())
            li.received += l.qty
            item.on_hand += l.qty
            db.add(StockMovement(shop_id=sid, item_id=item.id, kind="RECEIPT", qty=l.qty, po_id=po.id,
                                 actor_id=user.id, note=f"PO #{po.id} from {po.supplier}"))
        db.flush()
        lines = db.scalars(select(PurchaseOrderItem).where(PurchaseOrderItem.po_id == po.id)).all()
        po.status = "RECEIVED" if all(x.received >= x.ordered for x in lines) else "PARTIAL"
        idem_put(db, user, idempotency_key, f"po-receive-{po_id}", po.id)
        db.commit()
        return po_out(db, po)


# ---------------------------------------------------------------- notifications
@r.get("/notifications")
def notifications(user=Depends(current_user), db=Depends(get_db)):
    rows = db.scalars(select(Notification).where(Notification.user_id == user.id).order_by(Notification.id.desc()).limit(50)).all()
    unread = db.scalar(select(func.count()).select_from(Notification).where(Notification.user_id == user.id, Notification.read == False)) or 0  # noqa: E712
    return {"unread": unread, "items": [{"id": n.id, "message": n.message, "booking_id": n.booking_id,
                                         "read": n.read, "at": n.at.isoformat()} for n in rows]}


@r.post("/notifications/read-all")
def read_all(user=Depends(current_user), db=Depends(get_db)):
    for n in db.scalars(select(Notification).where(Notification.user_id == user.id, Notification.read == False)):  # noqa: E712
        n.read = True
    db.commit()
    return {"ok": True}


@r.post("/notifications/{nid}/read")
def read_one(nid: int, user=Depends(current_user), db=Depends(get_db)):
    n = db.scalar(select(Notification).where(Notification.id == nid, Notification.user_id == user.id))
    if not n:
        raise HTTPException(404, "Not found")
    n.read = True
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- AI front desk (call agent)
RATE_LIMIT, RATE_WINDOW = 40, 60  # messages per client per minute
_hits = defaultdict(deque)


def _rate_check(request: Request):
    ip = request.client.host if request.client else "unknown"
    q, t = _hits[ip], _time.time()
    while q and q[0] < t - RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise HTTPException(429, "Too many messages, please slow down")
    q.append(t)


def optional_customer(cred=Depends(bearer), db=Depends(get_db)):
    if not cred:
        return None
    uid = read_token(cred.credentials)
    u = db.get(User, uid) if uid else None
    return u if u and u.kind == "customer" else None


class SayIn(BaseModel):
    message: str = Field(min_length=1, max_length=500)


def _log(call, who, text):
    t = json.loads(call.transcript or "[]")
    t.append({"who": who, "text": text, "at": now().isoformat()})
    call.transcript = json.dumps(t)


def _open_call(db, sid, token):
    call = db.scalar(select(Call).where(Call.token == token, Call.shop_id == sid))
    if not call:
        raise HTTPException(404, "Call not found")
    if call.ended_at:
        raise HTTPException(409, "This call has ended")
    return call


@r.post("/public/shops/{sid}/call/start", status_code=201)
def call_start(sid: int, request: Request, user=Depends(optional_customer), db=Depends(get_db)):
    _rate_check(request)
    shop = db.get(Shop, sid)
    if not shop or not shop.active:
        raise HTTPException(404, "Shop not found")
    call = Call(token=uuid.uuid4().hex, shop_id=sid, user_id=user.id if user else None)
    db.add(call)
    db.flush()
    text = agent.greeting(agent.Ctx(db, shop, user, call))
    _log(call, "agent", text)
    db.commit()
    return {"call_id": call.token, "greeting": text, "signed_in": bool(user), "ai_mode": bool(__import__("os").getenv("ANTHROPIC_API_KEY"))}


@r.post("/public/shops/{sid}/call/{token}/say")
def call_say(sid: int, token: str, body: SayIn, request: Request, db=Depends(get_db)):
    _rate_check(request)
    call = _open_call(db, sid, token)
    shop = db.get(Shop, sid)
    user = db.get(User, call.user_id) if call.user_id else None
    history = [{"role": "user" if x["who"] == "you" else "assistant", "text": x["text"]} for x in json.loads(call.transcript or "[]")]
    _log(call, "you", body.message)
    res = agent.respond(agent.Ctx(db, shop, user, call), body.message, history)
    _log(call, "agent", res["text"])
    call.intents = ",".join(filter(None, [call.intents, *res["intents"]]))
    db.commit()
    return {"reply": res["text"], "end": res["end"]}


@r.post("/public/shops/{sid}/call/{token}/end")
def call_end(sid: int, token: str, db=Depends(get_db)):
    call = db.scalar(select(Call).where(Call.token == token, Call.shop_id == sid))
    if not call:
        raise HTTPException(404, "Call not found")
    if not call.ended_at:
        topics = sorted({i for i in call.intents.split(",") if i})
        turns = [x for x in json.loads(call.transcript or "[]") if x["who"] == "you"]
        tasks = db.scalars(select(CallbackTask).where(CallbackTask.call_id == call.id)).all()
        first = turns[0]["text"][:100] if turns else "(caller said nothing)"
        call.summary = (f"{len(turns)} caller message(s). Topics: {', '.join(topics) or 'none'}. First message: \"{first}\"."
                        + (f" Callback requested (task #{tasks[0].id})." if tasks else ""))
        call.ended_at = now()
        db.commit()
    return {"summary": call.summary}


@r.get("/shops/{sid}/calls")
def list_calls(sid: int, user=Depends(current_user), db=Depends(get_db)):
    member(db, user, sid, {OWNER, ADVISOR})
    out = []
    for c in db.scalars(select(Call).where(Call.shop_id == sid).order_by(Call.id.desc()).limit(100)):
        u = db.get(User, c.user_id) if c.user_id else None
        out.append({"id": c.id, "caller": u.name if u else "Guest", "started_at": c.started_at.isoformat(),
                    "ended": bool(c.ended_at), "summary": c.summary, "transcript": json.loads(c.transcript or "[]")})
    return out


@r.get("/shops/{sid}/tasks")
def list_tasks(sid: int, user=Depends(current_user), db=Depends(get_db)):
    member(db, user, sid, {OWNER, ADVISOR})
    return [{"id": t.id, "name": t.name, "phone": t.phone, "note": t.note, "status": t.status,
             "created_at": t.created_at.isoformat()}
            for t in db.scalars(select(CallbackTask).where(CallbackTask.shop_id == sid).order_by(CallbackTask.id.desc()).limit(100))]


class TaskPatch(BaseModel):
    status: Literal["OPEN", "DONE"]


@r.patch("/shops/{sid}/tasks/{tid}")
def patch_task(sid: int, tid: int, body: TaskPatch, user=Depends(current_user), db=Depends(get_db)):
    member(db, user, sid, {OWNER, ADVISOR})
    t = db.scalar(select(CallbackTask).where(CallbackTask.id == tid, CallbackTask.shop_id == sid))
    if not t:
        raise HTTPException(404, "Task not found")
    t.status = body.status
    db.commit()
    return {"id": t.id, "status": t.status}


# ---------------------------------------------------------------- owner analytics
@r.get("/shops/{sid}/analytics")
def analytics(sid: int, days: int = 30, user=Depends(current_user), db=Depends(get_db)):
    member(db, user, sid, {OWNER})
    since = now() - timedelta(days=max(1, min(days, 365)))
    bs = db.scalars(select(Booking).where(Booking.shop_id == sid, Booking.created_at >= since)).all()
    done = [b for b in bs if b.status == COMPLETED]
    revenue, turn = 0, []
    for b in done:
        e = db.scalar(select(Estimate).where(Estimate.booking_id == b.id, Estimate.status == "ACCEPTED").order_by(Estimate.version.desc()))
        revenue += e.total if e else 0
        h = db.scalar(select(BookingHistory).where(BookingHistory.booking_id == b.id, BookingHistory.to_status == COMPLETED))
        if h:
            turn.append((h.at - b.created_at).total_seconds() / 3600)
    ids = [b.id for b in bs]
    qcs = db.scalars(select(QCInspection).where(QCInspection.booking_id.in_(ids), QCInspection.status.in_(["PASSED", "FAILED"]))).all() if ids else []
    names = {s.id: s.name for s in db.scalars(select(Service).where(Service.shop_id == sid))}
    top = defaultdict(int)
    for b in bs:
        top[names.get(b.service_id, "?")] += 1
    load = defaultdict(int)
    for b in bs:
        if b.technician_id and b.status not in (COMPLETED, CANCELLED):
            load[db.get(User, b.technician_id).name] += 1
    return {"days": days, "bookings": len(bs), "completed": len(done), "revenue_pkr": revenue,
            "avg_job_value_pkr": round(revenue / len(done)) if done else 0,
            "avg_turnaround_hours": round(sum(turn) / len(turn), 1) if turn else None,
            "qc_fail_rate": round(sum(q.status == "FAILED" for q in qcs) / len(qcs), 2) if qcs else None,
            "cancel_rate": round(sum(b.status == CANCELLED for b in bs) / len(bs), 2) if bs else None,
            "top_services": sorted(top.items(), key=lambda x: -x[1])[:5],
            "technician_workload": dict(load),
            "calls": db.scalar(select(func.count()).select_from(Call).where(Call.shop_id == sid, Call.started_at >= since)) or 0,
            "open_callbacks": db.scalar(select(func.count()).select_from(CallbackTask).where(CallbackTask.shop_id == sid, CallbackTask.status == "OPEN")) or 0}
