from datetime import date, timedelta

import pytest

from app import agent, api
from tests.conftest import H, World


def start(c, w, tok=None):
    r = c.post(f"/api/public/shops/{w.shop}/call/start", headers=H(tok) if tok else {})
    assert r.status_code == 201, r.text
    return r.json()


def say(c, w, cid, msg):
    r = c.post(f"/api/public/shops/{w.shop}/call/{cid}/say", json={"message": msg})
    assert r.status_code == 200, r.text
    return r.json()["reply"]


def test_call_greets_and_answers_shop_questions(client):
    w = World(client)
    client.patch(f"/api/shops/{w.shop}", headers=H(w.owner), json={"address": "12 Mall Road", "phone": "042-111", "closed_weekdays": "6"})
    d = start(client, w)
    assert "Shop a" in d["greeting"] and "AI" in d["greeting"] and d["signed_in"] is False
    cid = d["call_id"]
    hours = say(client, w, cid, "What are your opening hours?")
    assert "9 AM" in hours and "5 PM" in hours and "Monday to Saturday" in hours and "Sunday" in hours
    assert "12 Mall Road" in say(client, w, cid, "where are you located")
    assert "042-111" in say(client, w, cid, "what's your phone number")
    assert "Brake job" in say(client, w, cid, "what services do you offer")
    assert "5,000" in say(client, w, cid, "how much is the brake job")
    assert "Brake job" in say(client, w, cid, "do you do brake work")


def test_availability_questions(client):
    w = World(client)
    cid = start(client, w)["call_id"]
    assert "open time slots" in say(client, w, cid, "do you have any slots tomorrow")
    d = (date.today() + timedelta(days=3)).isoformat()
    client.patch(f"/api/shops/{w.shop}", headers=H(w.owner), json={"closed_dates": d})
    assert "closed" in say(client, w, cid, f"are you open on {d}").lower()
    assert "passed" in say(client, w, cid, "slots on 2020-01-01")
    # fill a slot completely -> agent must not claim it is free
    w2 = World(client, "b", capacity=1)
    client.patch(f"/api/shops/{w2.shop}", headers=H(w2.owner), json={"slot_minutes": 480, "open_time": "09:00", "close_time": "17:00"})
    assert w2.book().status_code == 201
    c2 = start(client, w2)["call_id"]
    assert "fully booked" in say(client, w2, c2, "any slot tomorrow?")


def test_unknown_questions_are_not_guessed(client):
    w = World(client)
    cid = start(client, w)["call_id"]
    r = say(client, w, cid, "does the mechanic named Bilal have a beard")
    assert "not sure" in r and "callback" in r
    bye = client.post(f"/api/public/shops/{w.shop}/call/{cid}/say", json={"message": "thanks, bye"}).json()
    assert bye["end"] is True and "Goodbye" in bye["reply"]


def test_booking_status_requires_sign_in(client):
    w = World(client)
    bid = w.to_estimate_sent()
    guest = start(client, w)["call_id"]
    r = say(client, w, guest, "what is the status of my booking")
    assert "sign in" in r and str(bid) not in r and "Brake" not in r
    cid = start(client, w, w.cust)
    assert cid["signed_in"] is True
    r = say(client, w, cid["call_id"], "what's the status of my car")
    assert f"booking number {bid}" in r and "waiting for your approval" in r and "PKR 7,500" in r
    # another customer sees nothing about it
    other = w.customer("other@t.io")
    oc = start(client, w, other)["call_id"]
    assert "don't see any bookings" in say(client, w, oc, "where is my car")


def test_callback_flow_creates_task_and_notifies_advisor(client):
    w = World(client)
    cid = start(client, w)["call_id"]
    assert "phone number" in say(client, w, cid, "I want to talk to a human")
    assert "didn't catch" in say(client, w, cid, "umm later")
    assert "call you back" in say(client, w, cid, "0300 1234567")
    tasks = client.get(f"/api/shops/{w.shop}/tasks", headers=H(w.adv)).json()
    assert len(tasks) == 1 and tasks[0]["phone"] == "0300 1234567" and tasks[0]["status"] == "OPEN"
    assert any("Callback requested" in n["message"] for n in client.get("/api/notifications", headers=H(w.adv)).json()["items"])
    assert client.patch(f"/api/shops/{w.shop}/tasks/{tasks[0]['id']}", headers=H(w.adv), json={"status": "DONE"}).json()["status"] == "DONE"
    # cancelling the phone prompt
    c2 = start(client, w)["call_id"]
    say(client, w, c2, "call me back please")
    assert "won't arrange" in say(client, w, c2, "never mind")
    assert len(client.get(f"/api/shops/{w.shop}/tasks", headers=H(w.adv)).json()) == 1


def test_end_call_summary_and_logs_access(client):
    w = World(client)
    cid = start(client, w)["call_id"]
    say(client, w, cid, "what are your hours")
    s = client.post(f"/api/public/shops/{w.shop}/call/{cid}/end").json()["summary"]
    assert "hours" in s and "1 caller message" in s
    assert client.post(f"/api/public/shops/{w.shop}/call/{cid}/say", json={"message": "hi"}).status_code == 409
    calls = client.get(f"/api/shops/{w.shop}/calls", headers=H(w.adv)).json()
    assert len(calls) == 1 and calls[0]["ended"] and calls[0]["transcript"][0]["who"] == "agent"
    assert client.get(f"/api/shops/{w.shop}/calls", headers=H(w.tech)).status_code == 403
    assert client.get(f"/api/shops/{w.shop}/calls", headers=H(w.cust)).status_code == 403
    assert client.get(f"/api/shops/{w.shop}/tasks", headers=H(w.parts)).status_code == 403


def test_call_tokens_are_scoped_to_their_shop(client):
    a, b = World(client, "a"), World(client, "b")
    cid = start(client, a)["call_id"]
    assert client.post(f"/api/public/shops/{b.shop}/call/{cid}/say", json={"message": "hi"}).status_code == 404
    assert client.post(f"/api/public/shops/{a.shop}/call/nope/say", json={"message": "hi"}).status_code == 404
    assert client.post(f"/api/public/shops/{a.shop}/call/{cid}/say", json={"message": ""}).status_code == 422
    assert client.get(f"/api/shops/{a.shop}/calls", headers=H(b.owner)).status_code == 403
    assert client.post("/api/public/shops/9999/call/start").status_code == 404


def test_rate_limit(client, monkeypatch):
    w = World(client)
    cid = start(client, w)["call_id"]
    monkeypatch.setattr(api, "RATE_LIMIT", 3)
    api._hits.clear()
    codes = [client.post(f"/api/public/shops/{w.shop}/call/{cid}/say", json={"message": "hours"}).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    api._hits.clear()


def test_llm_brain_uses_tools_and_falls_back(client, monkeypatch):
    w = World(client)
    cid = start(client, w)["call_id"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    seen = []

    def fake(payload):
        seen.append(payload)
        if len(seen) == 1:
            return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "t1", "name": "list_services", "input": {}}]}
        tool_out = payload["messages"][-1]["content"][0]["content"]
        assert "Brake job" in tool_out  # the model only sees real shop data
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "We do brake jobs for 5000 rupees."}]}

    monkeypatch.setattr(agent, "_llm_call", fake)
    assert say(client, w, cid, "do you fix brakes?") == "We do brake jobs for 5000 rupees."
    assert "Never invent" in seen[0]["system"] or "never invent" in seen[0]["system"]
    # provider failure -> deterministic fallback, call keeps working
    monkeypatch.setattr(agent, "_llm_call", lambda p: (_ for _ in ()).throw(RuntimeError("down")))
    assert "9 AM" in say(client, w, cid, "what are your hours")
    # tools cannot be tricked into other shops / unknown tools
    assert agent.run_tool(agent.Ctx(None, None, None, None), "drop_tables", {}) == {"error": "unknown tool"}


def test_owner_analytics(client):
    w = World(client)
    b = w.to_repair(qty=2)
    w.post(w.tech, f"bookings/{b}/submit-qc")
    w.post(w.qc, f"bookings/{b}/qc/claim")
    w.post(w.qc, f"bookings/{b}/qc/fail", {"issue": "squeal"})
    w.post(w.tech, f"bookings/{b}/submit-qc")
    w.post(w.qc, f"bookings/{b}/qc/claim")
    ok = {k: True for k in client.get("/api/qc/checklist").json()}
    w.post(w.qc, f"bookings/{b}/qc/pass", {"checklist": ok})
    w.post(w.adv, f"bookings/{b}/complete")
    b2 = w.book(i=1).json()["id"]
    w.post(w.adv, f"bookings/{b2}/cancel")
    a = client.get(f"/api/shops/{w.shop}/analytics", headers=H(w.owner)).json()
    assert a["completed"] == 1 and a["revenue_pkr"] == 7500 and a["avg_job_value_pkr"] == 7500
    assert a["qc_fail_rate"] == 0.5 and a["cancel_rate"] == 0.5 and a["bookings"] == 2
    assert a["top_services"][0] == ["Brake job", 2]
    assert client.get(f"/api/shops/{w.shop}/analytics", headers=H(w.adv)).status_code == 403
