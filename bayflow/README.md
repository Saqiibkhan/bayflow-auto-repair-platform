# BayFlow – multi-tenant auto repair shop platform

Customer portal + shop POS + workflow API in one deployable app (FastAPI + SQLAlchemy + Alembic, PostgreSQL in production / SQLite locally, vanilla-JS single-page frontend served by the API).

## Run locally (no Docker)
```bash
cp .env.example .env            # optional, defaults work
cd backend
pip install -r requirements.txt
alembic upgrade head            # migrations
python -m app.seed              # repeatable seed (add --reset to rebuild)
uvicorn app.main:app --reload   # http://localhost:8000  (API docs: /docs)
```

## Run with Docker (PostgreSQL)
```bash
cp .env.example .env
docker compose up --build       # migrates on start
docker compose exec app python -m app.seed
```

## Tests
```bash
cd backend && python -m pytest -q
```
28 tests: AI front desk (answers, privacy, callbacks, rate limit, LLM fallback), owner analytics, plus full lifecycle, QC failure loop, estimate-rejected path, partial PO receiving, cancellation stock release (exactly once), tenant isolation incl. guessed IDs, role/assignment guards, same user with different roles per shop, concurrent slot booking, concurrent stock allocation, idempotency, append-only history, seed script.

## Demo accounts (password `Demo@1234` for all)
| Role | Email (Lahore / Karachi / Islamabad = lhr / khi / isb) |
|---|---|
| Owner (all 3 shops) | owner@bayflow.demo |
| Service Advisor | advisor.lhr@bayflow.demo |
| Technician | tech.lhr@bayflow.demo |
| Parts Person | parts.lhr@bayflow.demo |
| QC Inspector | qc.lhr@bayflow.demo |
| Multi-role user | multi@bayflow.demo (Advisor @ Lahore, Technician @ Karachi) |
| Customers | ali@customer.demo, sara@customer.demo |

Sign in at `/#/login`. Customers can also create an account during the booking wizard.

## Demo script (≈5 min)
1. **Happy path:** as Ali book a service at Lahore → advisor confirms + assigns → technician inspects and submits an estimate (add a part) → advisor sends → Ali accepts → parts person allocates → technician repairs and sends to QC → QC claims, ticks the checklist, passes → advisor completes.
2. **Estimate rejected:** Ali rejects → advisor "Request revision" → technician submits v2 → Ali accepts (or advisor cancels).
3. **QC failure:** QC inspector fails the job with an issue → it returns to IN_REPAIR → technician re-submits → second QC pass.
4. **Inventory:** parts person → Orders → create PO for 10, receive 4 (PARTIAL), later 6 (RECEIVED).
5. **Isolation:** log in as `advisor.khi@…` – Lahore bookings are invisible (404) even by ID.

## AI Front Desk (call agent)
Every garage card has a **📞 Call** button (also on the garage page). It opens a call window where the agent greets you, speaks its answers (browser text-to-speech) and listens (browser speech recognition in Chrome/Edge; otherwise type). It answers from live shop data:
hours and open days, upcoming closures, address, phone, services and PKR prices, appointment availability for a date ("tomorrow", "Friday", `2026-11-20`), next free day, and **your own booking status if you are signed in** (guests are refused, so private data is never read out to anonymous callers). It never invents answers: unknown questions get an offer of a callback. "Talk to a human / call me back" creates a **callback task** (and notifies advisors); staff see call transcripts, summaries and tasks in the **Calls** tab.
- Default brain is deterministic and needs no external service. Set `ANTHROPIC_API_KEY` (optionally `ANTHROPIC_MODEL`) to let Claude hold the conversation using the same narrow tools; on any provider failure it falls back to the built-in brain.
- Public endpoints are rate-limited (40 messages/min per client). The agent cannot book, cancel or change anything.
- Owner **Overview** now includes 30-day analytics: revenue, average job value, turnaround, QC failure rate, cancellation rate, top services, technician workload, calls and open callbacks.

## Architecture
- `app/workflow.py` – the only place a booking status changes: `RULES[(from,to)] = (roles, assignment)`, writes append-only `booking_history`, fires notifications.
- `app/api.py` – routes. Shop scope always comes from the caller's `shop_memberships` row (never trusted from the client). Roles are per shop membership; customers are global accounts limited to their own bookings. Non-members get 404 on bookings (no existence leak).
- Inventory: `inventory_items` (on_hand / reserved / available) + `stock_movements` ledger (ADJUST, RECEIPT, RESERVE, RELEASE, CONSUME) – separate from booking history. `parts_allocations` per booking; cancel releases only ACTIVE allocations so a release happens once.
- Concurrency: critical sections use `SELECT … FOR UPDATE` (PostgreSQL) plus an in-process lock (SQLite / single worker).
- Idempotency: `Idempotency-Key` header on booking create, PO create and PO receive.
- Estimates are versioned; the accepted version keeps `decided_at`; older drafts become SUPERSEDED, rejected ones are kept.

## Status transitions
`PENDING → CONFIRMED → ASSIGNED → INSPECTING → ESTIMATE_REVIEW → AWAITING_CUSTOMER → ESTIMATE_APPROVED | ESTIMATE_REJECTED`; approved → `PARTS_PENDING/PARTS_READY → IN_REPAIR → QC_PENDING → QC_IN_PROGRESS → READY_FOR_PICKUP | IN_REPAIR (fail) → COMPLETED`. `ESTIMATE_REJECTED → INSPECTING` (revision). `CANCELLED` from any state before repair starts.

## Not included (stretch / bonus)
Real phone-line (PSTN) calling, rescheduling by the agent, inspection photo upload, invoice PDFs/payments, analytics beyond the owner overview, Urdu UI, WhatsApp. The data model and API leave room for them (see the brief's P1–P3 list).
