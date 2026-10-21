"""Central booking state machine, role guards and notifications."""
import threading

from fastapi import HTTPException
from sqlalchemy import select

from .models import Booking, BookingHistory, Membership, Notification

# Serialises critical sections inside one process (SQLite). On PostgreSQL the
# SELECT ... FOR UPDATE row locks in the API give the same guarantee across processes.
WRITE_LOCK = threading.Lock()

PENDING, CONFIRMED, ASSIGNED, INSPECTING = "PENDING", "CONFIRMED", "ASSIGNED", "INSPECTING"
ESTIMATE_REVIEW, AWAITING_CUSTOMER = "ESTIMATE_REVIEW", "AWAITING_CUSTOMER"
ESTIMATE_APPROVED, ESTIMATE_REJECTED = "ESTIMATE_APPROVED", "ESTIMATE_REJECTED"
PARTS_PENDING, PARTS_READY, IN_REPAIR = "PARTS_PENDING", "PARTS_READY", "IN_REPAIR"
QC_PENDING, QC_IN_PROGRESS = "QC_PENDING", "QC_IN_PROGRESS"
READY_FOR_PICKUP, COMPLETED, CANCELLED = "READY_FOR_PICKUP", "COMPLETED", "CANCELLED"

OWNER, ADVISOR, TECH, PARTS, QC, CUSTOMER = (
    "OWNER", "SERVICE_ADVISOR", "TECHNICIAN", "PARTS_PERSON", "QC_INSPECTOR", "CUSTOMER")
STAFF_ROLES = [ADVISOR, TECH, PARTS, QC]
ADMIN = {OWNER, ADVISOR}

CANCELLABLE = [PENDING, CONFIRMED, ASSIGNED, INSPECTING, ESTIMATE_REVIEW, AWAITING_CUSTOMER,
               ESTIMATE_APPROVED, ESTIMATE_REJECTED, PARTS_PENDING, PARTS_READY]

# (from, to) -> (allowed roles, assignment requirement)
RULES = {
    (PENDING, CONFIRMED): (ADMIN, None),
    (CONFIRMED, ASSIGNED): (ADMIN, None),
    (ASSIGNED, INSPECTING): ({TECH}, "tech"),
    (INSPECTING, ESTIMATE_REVIEW): ({TECH}, "tech"),
    (ESTIMATE_REVIEW, AWAITING_CUSTOMER): (ADMIN, None),
    (AWAITING_CUSTOMER, ESTIMATE_APPROVED): ({CUSTOMER}, None),
    (AWAITING_CUSTOMER, ESTIMATE_REJECTED): ({CUSTOMER}, None),
    (ESTIMATE_REJECTED, INSPECTING): (ADMIN, None),
    (ESTIMATE_APPROVED, PARTS_PENDING): ({PARTS}, None),
    (ESTIMATE_APPROVED, PARTS_READY): ({PARTS}, None),
    (PARTS_PENDING, PARTS_READY): ({PARTS}, None),
    (PARTS_READY, IN_REPAIR): ({TECH}, "tech"),
    (IN_REPAIR, QC_PENDING): ({TECH}, "tech"),
    (QC_PENDING, QC_IN_PROGRESS): ({QC}, None),
    (QC_IN_PROGRESS, READY_FOR_PICKUP): ({QC}, "qc"),
    (QC_IN_PROGRESS, IN_REPAIR): ({QC}, "qc"),
    (READY_FOR_PICKUP, COMPLETED): (ADMIN, None),
}
for _s in CANCELLABLE:
    RULES[(_s, CANCELLED)] = (ADMIN | {CUSTOMER}, None)

TECH_ASSIGNED = "__tech__"
# status -> (message for customer or None, [(roles, message)])
NOTES = {
    PENDING: (None, [(ADMIN, "New booking #{id} awaiting confirmation")]),
    CONFIRMED: ("Your booking #{id} has been confirmed", []),
    ASSIGNED: ("A technician has been assigned to booking #{id}", [({TECH_ASSIGNED}, "You were assigned booking #{id}")]),
    INSPECTING: ("Inspection of your vehicle has started (booking #{id})", []),
    ESTIMATE_REVIEW: (None, [({ADVISOR, OWNER}, "Estimate for booking #{id} is ready for review")]),
    AWAITING_CUSTOMER: ("An estimate for booking #{id} is waiting for your approval", []),
    ESTIMATE_APPROVED: (None, [({PARTS}, "Estimate approved for booking #{id}: check/allocate parts"),
                               ({ADVISOR, OWNER}, "Customer approved the estimate for booking #{id}")]),
    ESTIMATE_REJECTED: (None, [({ADVISOR, OWNER}, "Customer rejected the estimate for booking #{id}")]),
    PARTS_PENDING: (None, [({ADVISOR, OWNER}, "Booking #{id} is waiting for parts")]),
    PARTS_READY: (None, [({TECH_ASSIGNED}, "Parts are ready for booking #{id}")]),
    IN_REPAIR: ("Repair work is in progress on booking #{id}", [({TECH_ASSIGNED}, "Booking #{id} is in repair")]),
    QC_PENDING: (None, [({QC}, "Booking #{id} is waiting for quality control")]),
    READY_FOR_PICKUP: ("Your vehicle is ready for pickup (booking #{id})", [({ADVISOR, OWNER}, "Booking #{id} ready for pickup")]),
    COMPLETED: ("Booking #{id} is completed. Thank you!", []),
    CANCELLED: ("Booking #{id} was cancelled", [({ADVISOR, OWNER, TECH_ASSIGNED}, "Booking #{id} was cancelled")]),
}


def staff_ids(db, shop_id, roles):
    return list(db.scalars(select(Membership.user_id).where(
        Membership.shop_id == shop_id, Membership.active == True, Membership.role.in_(list(roles)))))  # noqa: E712


def notify(db, user_id, message, booking_id=None):
    db.add(Notification(user_id=user_id, message=message, booking_id=booking_id))


def _notify_status(db, b: Booking, to: str):
    cust, staff = NOTES.get(to, (None, []))
    if cust:
        notify(db, b.customer_id, cust.format(id=b.id), b.id)
    seen = set()
    for roles, msg in staff:
        ids = []
        if TECH_ASSIGNED in roles and b.technician_id:
            ids.append(b.technician_id)
        ids += staff_ids(db, b.shop_id, [r for r in roles if r != TECH_ASSIGNED])
        for uid in ids:
            if uid not in seen:
                seen.add(uid)
                notify(db, uid, msg.format(id=b.id), b.id)


def transition(db, b: Booking, to: str, actor, role: str, reason: str = ""):
    """The only place a booking status may change."""
    rule = RULES.get((b.status, to))
    if not rule:
        raise HTTPException(409, f"Illegal transition {b.status} -> {to}")
    roles, assign = rule
    if role not in roles:
        raise HTTPException(403, f"Role {role} may not move a booking {b.status} -> {to}")
    if assign == "tech" and b.technician_id != actor.id:
        raise HTTPException(403, "Only the assigned technician can do this")
    if assign == "qc" and b.qc_inspector_id != actor.id:
        raise HTTPException(403, "Only the QC inspector who claimed this job can do this")
    frm = b.status
    b.status = to
    db.add(BookingHistory(booking_id=b.id, actor_id=actor.id, from_status=frm, to_status=to, reason=reason or ""))
    _notify_status(db, b, to)
