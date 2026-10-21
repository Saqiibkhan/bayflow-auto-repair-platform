"""AI Front Desk.

Narrow, read-mostly tools that only expose public shop data (or the signed-in caller's own bookings).
Two interchangeable "brains":
  * rule_reply  - deterministic, no external service (default; the call never depends on a paid provider)
  * llm_reply   - Claude with tool calling, used only when ANTHROPIC_API_KEY is set; any failure falls back to rules.
"""
import json
import os
import re
from dataclasses import dataclass
from datetime import date as _date, datetime, timedelta

import httpx
from sqlalchemy import select

from .models import Booking, CallbackTask, Estimate, Service, now
from .workflow import ADMIN, notify, staff_ids

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")


@dataclass
class Ctx:
    db: object
    shop: object
    user: object  # signed-in customer or None
    call: object


# ------------------------------------------------------------------ formatting
def tfmt(hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    h12 = h % 12 or 12
    return f"{h12} {'AM' if h < 12 else 'PM'}" if m == 0 else f"{h12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def pkr(n):
    return f"PKR {int(n):,}"


def _closed_days(shop):
    return {int(x) for x in shop.closed_weekdays.split(",") if x.strip().isdigit()}


def days_text(shop):
    closed = _closed_days(shop)
    open_ = [i for i in range(7) if i not in closed]
    if not open_:
        return "no regular days (currently closed)", ""
    if len(open_) == 7:
        return "every day", ""
    if open_ == list(range(open_[0], open_[-1] + 1)) and len(open_) > 2:
        t = f"{DAYS[open_[0]]} to {DAYS[open_[-1]]}"
    else:
        t = ", ".join(DAYS[i] for i in open_)
    return t, "We are closed on " + ", ".join(DAYS[i] for i in sorted(closed)) + "."


STATUS_TEXT = {
    "PENDING": "is waiting for the service advisor to confirm it",
    "CONFIRMED": "is confirmed and waiting for a technician to be assigned",
    "ASSIGNED": "has a technician assigned and is waiting for inspection",
    "INSPECTING": "is being inspected by the technician",
    "ESTIMATE_REVIEW": "has an estimate being reviewed by the shop",
    "AWAITING_CUSTOMER": "has an estimate waiting for your approval",
    "ESTIMATE_APPROVED": "has an approved estimate and the shop is checking parts",
    "ESTIMATE_REJECTED": "had its estimate rejected and the shop is preparing a revision",
    "PARTS_PENDING": "is waiting for parts to arrive",
    "PARTS_READY": "has all parts ready and repair will begin shortly",
    "IN_REPAIR": "is being repaired right now",
    "QC_PENDING": "is waiting for quality control",
    "QC_IN_PROGRESS": "is going through quality control",
    "READY_FOR_PICKUP": "is ready for pickup",
    "COMPLETED": "is completed",
    "CANCELLED": "was cancelled",
}


# ------------------------------------------------------------------ tools
def t_shop_info(c, **_):
    s = c.shop
    dt, closed = days_text(s)
    upcoming = [d for d in s.closed_dates.split(",") if d.strip() and d.strip() >= _date.today().isoformat()]
    return {"name": s.name, "city": s.city, "address": s.address, "phone": s.phone, "opens": s.open_time,
            "closes": s.close_time, "open_days": dt, "closed_note": closed, "appointment_length_min": s.slot_minutes,
            "upcoming_closures": upcoming[:5]}


def t_list_services(c, **_):
    rows = c.db.scalars(select(Service).where(Service.shop_id == c.shop.id, Service.active == True)).all()  # noqa: E712
    return {"services": [{"name": x.name, "price_pkr": x.price, "duration_min": x.duration_min} for x in rows]}


def _tokens(s):
    return {w[:-1] if w.endswith("s") and len(w) > 3 else w for w in re.findall(r"[a-z0-9]+", s.lower())} - {
        "the", "a", "an", "and", "of", "for", "to", "do", "you", "your", "how", "much", "is", "what", "does", "it",
        "service", "price", "cost", "i", "my", "me", "on", "in", "can", "get", "want", "need", "have", "are"}


def t_find_service(c, query="", **_):
    q = _tokens(query)
    best, score = None, 0
    for x in t_list_services(c)["services"]:
        sc = len(q & _tokens(x["name"]))
        if sc > score:
            best, score = x, sc
    return {"match": best}


def t_check_availability(c, date="", **_):
    from . import api as A
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return {"error": "date must be YYYY-MM-DD"}
    if d < _date.today():
        return {"date": date, "past": True}
    slots = []
    for st in A.day_slots(c.shop, d):
        if st <= now():
            continue
        slots.append({"time": st.strftime("%H:%M"), "available": max(0, c.shop.capacity - A.booked_count(c.db, c.shop.id, st))})
    return {"date": d.isoformat(), "weekday": DAYS[d.weekday()], "open": bool(A.day_slots(c.shop, d)), "slots": slots}


def t_next_available(c, **_):
    for i in range(0, 15):
        d = _date.today() + timedelta(days=i)
        r = t_check_availability(c, date=d.isoformat())
        free = [s for s in r.get("slots", []) if s["available"]]
        if free:
            return {"date": r["date"], "weekday": r["weekday"], "first_time": free[0]["time"], "free_slots": len(free)}
    return {"none_in_next_days": 14}


def t_my_bookings(c, **_):
    if not c.user:
        return {"error": "not_signed_in"}
    rows = c.db.scalars(select(Booking).where(Booking.customer_id == c.user.id, Booking.shop_id == c.shop.id)
                        .order_by(Booking.id.desc()).limit(3)).all()
    out = []
    for b in rows:
        svc = c.db.get(Service, b.service_id)
        item = {"id": b.id, "service": svc.name if svc else "", "status": b.status,
                "status_text": STATUS_TEXT.get(b.status, b.status), "appointment": b.slot_start.strftime("%A %d %b, %H:%M")}
        if b.status == "AWAITING_CUSTOMER":
            e = c.db.scalar(select(Estimate).where(Estimate.booking_id == b.id, Estimate.status == "SENT")
                            .order_by(Estimate.version.desc()))
            if e:
                item["estimate_total_pkr"] = e.total
        out.append(item)
    return {"bookings": out}


def t_create_callback(c, note="", phone="", **_):
    phone = (phone or (c.user.phone if c.user else "") or "").strip()
    if len(re.sub(r"\D", "", phone)) < 7:
        return {"error": "phone_required"}
    name = c.user.name if c.user else "Caller"
    t = CallbackTask(shop_id=c.shop.id, call_id=c.call.id, name=name, phone=phone, note=(note or "Caller asked for a callback")[:500])
    c.db.add(t)
    c.db.flush()
    for uid in staff_ids(c.db, c.shop.id, ADMIN):
        notify(c.db, uid, f"Callback requested by {name} ({phone}): {t.note[:120]}")
    return {"task_id": t.id, "phone": phone}


TOOLS = {"get_shop_info": t_shop_info, "list_services": t_list_services, "find_service": t_find_service,
         "check_availability": t_check_availability, "next_available": t_next_available,
         "get_my_bookings": t_my_bookings, "create_callback_task": t_create_callback}


def run_tool(c, name, args):
    fn = TOOLS.get(name)
    if not fn:
        return {"error": "unknown tool"}
    try:
        return fn(c, **(args or {}))
    except TypeError:
        return {"error": "bad arguments"}


# ------------------------------------------------------------------ rule-based brain
def parse_date(msg):
    m = msg.lower()
    today = _date.today()
    iso = re.search(r"\d{4}-\d{2}-\d{2}", m)
    if iso:
        try:
            return datetime.strptime(iso.group(0), "%Y-%m-%d").date()
        except ValueError:
            return None
    if "day after tomorrow" in m:
        return today + timedelta(days=2)
    if "tomorrow" in m:
        return today + timedelta(days=1)
    if re.search(r"\b(today|tonight)\b", m):
        return today
    for i, name in enumerate(x.lower() for x in DAYS):
        if re.search(rf"\b{name}\b", m):
            delta = (i - today.weekday()) % 7
            if delta == 0 and "next" in m:
                delta = 7
            return today + timedelta(days=delta)
    return None


def _services_sentence(c):
    sv = t_list_services(c)["services"]
    if not sv:
        return "I don't have a service list for this garage yet."
    return "We offer " + "; ".join(f"{x['name']} at {pkr(x['price_pkr'])}" for x in sv) + "."


def _availability_sentence(c, d):
    r = t_check_availability(c, date=d.isoformat())
    label = f"{DAYS[d.weekday()]} {d.strftime('%d %b')}"
    if r.get("past"):
        return f"{label} has already passed. Tell me another date."
    if not r["open"]:
        nxt = t_next_available(c)
        extra = f" The next day with space is {nxt['weekday']} {nxt['date']}." if "date" in nxt else ""
        return f"We are closed on {label}.{extra}"
    free = [s for s in r["slots"] if s["available"]]
    if not free:
        nxt = t_next_available(c)
        extra = f" The next day with space is {nxt['weekday']} {nxt['date']}." if "date" in nxt else ""
        return f"Sorry, {label} is fully booked.{extra}"
    times = ", ".join(tfmt(s["time"]) for s in free[:4])
    more = f" and {len(free) - 4} more" if len(free) > 4 else ""
    return f"On {label} we have {len(free)} open time slots, for example {times}{more}. You can book it with the Book a service button."


def _handle_phone(c, msg, state):
    if re.search(r"\b(cancel|never ?mind|no thanks|forget it)\b", msg.lower()):
        state.clear()
        return {"text": "No problem, I won't arrange a callback. Anything else I can help with?", "intents": ["callback_cancelled"], "end": False}
    digits = re.sub(r"\D", "", msg)
    if len(digits) < 7:
        return {"text": "Sorry, I didn't catch a phone number. Please say or type the number we should call you on, or say cancel.", "intents": ["callback"], "end": False}
    res = t_create_callback(c, note=state.get("note", ""), phone=msg.strip())
    state.clear()
    return {"text": f"Thank you. I've asked a service advisor to call you back on {res['phone']}. Is there anything else I can help with?",
            "intents": ["callback"], "end": False}


def rule_reply(c, msg, state):
    m = msg.lower().strip()
    d = parse_date(m)
    shop = c.shop

    def out(text, intent, end=False):
        return {"text": text, "intents": [intent], "end": end}

    if re.search(r"call (me )?back|callback|talk to|speak (to|with)|human|advisor|manager|real person|someone", m):
        note = f"Caller said: {msg.strip()[:300]}"
        if (c.user and c.user.phone) :
            t_create_callback(c, note=note)
            return out(f"Done. I've asked a service advisor to call you back on {c.user.phone}. Anything else?", "callback")
        state.update({"await_phone": True, "note": note})
        return out("Sure, I can arrange that. What phone number should the service advisor call you on?", "callback")
    if re.search(r"my (booking|car|vehicle|appointment|estimate|repair|job)|status|progress|is it ready|pick ?up", m):
        r = t_my_bookings(c)
        if r.get("error"):
            return out("I can only share booking details after you sign in on the website, to protect your privacy. Please sign in, then call again. Or say call me back and I'll ask an advisor to ring you.", "status_denied")
        if not r["bookings"]:
            return out("I don't see any bookings under your account at this garage.", "status")
        b = r["bookings"][0]
        extra = f" The estimate total is {pkr(b['estimate_total_pkr'])}. Please open My bookings to accept or reject it." if b.get("estimate_total_pkr") else ""
        return out(f"Your booking number {b['id']} for {b['service']} on {b['appointment']} {b['status_text']}.{extra}", "status")
    if d and re.search(r"slot|availab|free|open|close|book|appointment|space|room|come|visit|time", m):
        return out(_availability_sentence(c, d), "availability")
    if re.search(r"price|cost|how much|charge|rate|fee|rupee|pkr", m):
        f = t_find_service(c, query=m)["match"]
        if f:
            return out(f"{f['name']} costs {pkr(f['price_pkr'])} and takes about {f['duration_min']} minutes. Final cost depends on the technician's inspection and estimate.", "price")
        return out(_services_sentence(c) + " Final cost depends on the inspection estimate.", "price")
    if re.search(r"service|offer|what (do|can) you|provide|repair|fix|oil|brake|\bac\b|tune|diagnos|mainten", m):
        f = t_find_service(c, query=m)["match"]
        if f:
            return out(f"Yes, we offer {f['name']}. It's {pkr(f['price_pkr'])} and takes about {f['duration_min']} minutes.", "services")
        pre = "I don't see that specific service listed. " if re.search(r"do you (do|fix|repair|have|offer)", m) else ""
        return out(pre + _services_sentence(c), "services")
    if re.search(r"\b(hours?|opening|open|close|closing|timings?|working|when are you)\b", m):
        dt, closed = days_text(shop)
        up = t_shop_info(c)["upcoming_closures"]
        extra = f" Upcoming closures: {', '.join(up)}." if up else ""
        return out(f"We're open {dt} from {tfmt(shop.open_time)} to {tfmt(shop.close_time)}. {closed}{extra}".replace("  ", " ").strip(), "hours")
    if re.search(r"address|location|where|located|direction|find you|city", m):
        return out(f"{shop.name} is at {shop.address or 'our listed address'}, {shop.city}.", "address")
    if re.search(r"phone|number|contact|reach|email", m):
        return out(f"You can reach the shop on {shop.phone or 'the phone number on our listing'}.", "contact")
    if re.search(r"book|appointment|schedule|reserve", m):
        nxt = t_next_available(c)
        extra = f" The next open day is {nxt['weekday']} {nxt['date']} from {tfmt(nxt['first_time'])}." if "date" in nxt else ""
        return out("You can book online with the Book a service button: choose a service, a time slot, and your vehicle details. Tell me a date and I'll check availability." + extra, "booking_help")
    if re.search(r"thank|bye|goodbye|that'?s all|that is all|nothing else", m):
        return out(f"You're welcome! Thank you for calling {shop.name}. Goodbye!", "farewell", end=True)
    if re.match(r"^(hi|hello|hey|salam|assalam|good (morning|afternoon|evening))\b", m):
        return out("Hello! How can I help you today? You can ask about our hours, services, prices, or appointment availability.", "greeting")
    return out("I'm not sure about that one, and I don't want to guess. I can tell you our hours, services and prices, check availability for a date, or arrange a callback from a service advisor. What would you like?", "unknown")


# ------------------------------------------------------------------ optional LLM brain
LLM_TOOLS = [
    {"name": "get_shop_info", "description": "Address, phone, opening hours, open days, closures.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_services", "description": "Active services with PKR prices and durations.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "find_service", "description": "Find the service matching a description.", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "check_availability", "description": "Open appointment slots on a date (YYYY-MM-DD).", "input_schema": {"type": "object", "properties": {"date": {"type": "string"}}, "required": ["date"]}},
    {"name": "next_available", "description": "Next day with free slots.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_my_bookings", "description": "Status of the signed-in caller's own bookings at this shop. Returns not_signed_in for guests.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "create_callback_task", "description": "Ask a service advisor to call the caller back. Needs a phone number unless the caller is signed in with one on file.", "input_schema": {"type": "object", "properties": {"note": {"type": "string"}, "phone": {"type": "string"}}, "required": ["note"]}},
]


def _system(c):
    return (f"You are the AI front desk voice assistant for {c.shop.name} in {c.shop.city}. Today is {_date.today().isoformat()}. "
            "You are on a phone call: answer in one to three short spoken sentences, no markdown, no lists. "
            "Say you are an AI assistant if asked. Only state shop facts that come from the tools; never invent prices, hours, "
            "availability or booking details. Booking details are only for signed-in callers via get_my_bookings. "
            "If you cannot help or the caller wants a human, offer create_callback_task (ask for a phone number if needed). "
            "You cannot book, cancel or change appointments; point to the Book a service button instead.")


def _llm_call(payload):
    r = httpx.post("https://api.anthropic.com/v1/messages", json=payload, timeout=20,
                   headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"})
    r.raise_for_status()
    return r.json()


def llm_reply(c, history, message):
    msgs = [{"role": h["role"], "content": h["text"]} for h in history[-10:] if h.get("role") in ("user", "assistant")]
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    msgs.append({"role": "user", "content": message})
    for _ in range(5):
        data = _llm_call({"model": MODEL, "max_tokens": 400, "system": _system(c), "tools": LLM_TOOLS, "messages": msgs})
        if data.get("stop_reason") == "tool_use":
            msgs.append({"role": "assistant", "content": data["content"]})
            res = [{"type": "tool_result", "tool_use_id": b["id"], "content": json.dumps(run_tool(c, b["name"], b.get("input")))}
                   for b in data["content"] if b.get("type") == "tool_use"]
            msgs.append({"role": "user", "content": res})
        else:
            return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text").strip() or None
    return None


def greeting(c):
    who = f", {c.user.name.split()[0]}" if c.user else ""
    return (f"Hello{who}, you've reached {c.shop.name}. I'm the garage's AI front desk assistant. "
            "I can tell you our hours, services and prices, check appointment availability, "
            + ("share the status of your booking, " if c.user else "")
            + "or arrange a callback from a service advisor. How can I help?")


def respond(c, message, history):
    state = json.loads(c.call.state or "{}")
    r = None
    if state.get("await_phone"):
        r = _handle_phone(c, message, state)
    else:
        if os.getenv("ANTHROPIC_API_KEY"):
            try:
                txt = llm_reply(c, history, message)
                if txt:
                    r = {"text": txt, "intents": ["ai"], "end": False}
            except Exception:
                r = None
        if r is None:
            r = rule_reply(c, message, state)
    c.call.state = json.dumps(state)
    return r
