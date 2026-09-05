# User Payout Management System — LLD

A low-level design and working implementation of an affiliate-sales payout system:
10% advance payouts on pending sales, reconciliation-driven final payouts,
24-hour withdrawal throttling, and recovery of failed/cancelled/rejected payouts.

Stack: **Python 3.11+, FastAPI, SQLite** (stdlib `sqlite3`, no ORM — so every
query is visible and portable to Postgres/MySQL with no rewrite).

 1. Domain model & ER diagram

```
 users                 sales                    ledger_entries              withdrawals
 ─────                 ─────                    ───────────────             ───────────
 id (PK)         ┌──   id (PK)                  id (PK)               ┌──   id (PK)
 name            │     user_id (FK -> users) ───┤user_id (FK -> users)│     user_id (FK -> users)
 email           │     brand_id (FK -> brands)   amount                     amount
 balance_cache  ◄┘     earning                   type                       status
 created_at            status                    reference_type             created_at
                       advance_amount            reference_id  ────────►    updated_at
                       advance_paid_at           created_at        (references sale
                       reconciled_at                                or withdrawal id,
                       created_at                                    polymorphic FK)

 brands
 ──────
 id (PK)
 name
```

**Relationships**
- `users 1 --- N sales` , `users 1 --- N ledger_entries`, `users 1 --- N withdrawals`
- `brands 1 --- N sales`
- `ledger_entries.reference_id` is a polymorphic pointer to either a `sale` or
  a `withdrawal` (disambiguated by `reference_type`) — this keeps a single,
  append-only audit trail for **all** money movement instead of splitting it
  across per-feature tables.

**Indexes** (see `app/db.py`):
- `sales(user_id, status)` — list a user's pending/approved/rejected sales fast.
- `sales(status, advance_paid_at)` — the advance-payout job's core query:
  `WHERE status='pending' AND advance_paid_at IS NULL`.
- `ledger_entries(user_id, created_at)` — ledger/statement pagination.
- `withdrawals(user_id, created_at)` — the 24-hour cooldown check.

---

## 2. Why an append-only ledger instead of just a `balance` column?

This is the central design decision, so it's worth calling out explicitly:

| | Mutable balance column only | Ledger + cached balance (chosen) |
|---|---|---|
| Auditability | You can't tell *why* the balance is what it is | Every rupee has a row: type, source, timestamp |
| Idempotency bugs | Easy to double-credit on retries | Each credit/debit is a discrete, traceable event |
| Debugging prod incidents | Hard — no history | Replay the ledger to reconstruct state at any point |
| Read performance | O(1) | O(1) too — `users.balance_cache` is kept in lock-step in the same transaction as every ledger write |
| Complexity | Lower | Slightly higher (one extra table + a helper) |

Given this is *money*, auditability wins. The cache keeps reads cheap, so
there's effectively no runtime cost, only a small amount of extra code.

---

## 3. Class / module design (Python)

Even though this uses functions + a thin service layer rather than heavy OOP
(idiomatic for FastAPI), the responsibilities map to clear "classes" of concern:

```
app/
├── db.py         # Connection management, schema, transaction() context manager
│                 #   -> equivalent to a Repository/DAO layer
├── schemas.py     # Pydantic request/response models
│                 #   -> equivalent to DTOs
├── service.py     # PayoutService (module-level functions grouped by concern):
│   ├── create_user / create_brand / create_sale / list_sales
│   ├── run_advance_payout_job()         # AdvancePayoutService
│   ├── reconcile_sale()                 # ReconciliationService
│   ├── create_withdrawal()              # WithdrawalService
│   ├── update_withdrawal_status()       # WithdrawalService (webhook handler)
│   ├── get_balance() / get_ledger()     # LedgerService
│   └── _post_ledger_entry()             # shared primitive used by all of the above
└── main.py       # FastAPI routes (Controller layer) — thin, delegates to service.py
```

If this were to grow, `service.py` would be split into
`AdvancePayoutService`, `ReconciliationService`, `WithdrawalService`, and
`LedgerService` classes with `PayoutService` as a facade — the current file
is already organized in exactly those sections so that split is mechanical.

---

## 4. API reference

| Method | Path | Purpose |
|---|---|---|
| POST | `/users` | Create a user |
| GET | `/users/{id}/balance` | Current withdrawable balance |
| GET | `/users/{id}/ledger` | Full audit trail of credits/debits |
| POST | `/brands` | Create a brand |
| POST | `/sales` | Create a `pending` sale |
| GET | `/sales/{user_id}` | List a user's sales |
| POST | `/sales/{sale_id}/reconcile` | Admin reconciles a sale → `approved`/`rejected` |
| POST | `/payouts/advance/run` | Trigger the advance-payout batch job (idempotent) |
| POST | `/withdrawals` | Request a withdrawal (enforces 24h cooldown + balance check) |
| POST | `/withdrawals/{id}/status` | Payment-gateway webhook: `success`/`failed`/`cancelled`/`rejected` |
| GET | `/withdrawals/user/{user_id}` | List a user's withdrawals |

Interactive docs auto-generated at `/docs` (Swagger) once the server is running.

### Example: reproducing the PDF's worked example

```bash
uvicorn app.main:app --reload
# in another shell:
curl -X POST localhost:8000/users -H 'content-type: application/json' -d '{"name":"john_doe"}'
curl -X POST localhost:8000/brands -H 'content-type: application/json' -d '{"name":"brand_1"}'
# create 3 sales of earning=40 for that user/brand...
curl -X POST localhost:8000/payouts/advance/run       # pays 4 x 3 = 12 advance
curl -X POST localhost:8000/sales/{id1}/reconcile -d '{"status":"rejected"}'   # -4
curl -X POST localhost:8000/sales/{id2}/reconcile -d '{"status":"approved"}'  # +36
curl -X POST localhost:8000/sales/{id3}/reconcile -d '{"status":"approved"}'  # +36
curl localhost:8000/users/{user_id}/balance             # -> 80 (12 advance + 68 final)
```

`demo.py` runs exactly this scenario against the real app in-process and
asserts every intermediate number matches the PDF.

---

## 5. Key business rules & how they're enforced

### 5.1 Advance payout (10%, exactly once per sale)
- `sales.advance_paid_at` starts `NULL`.
- The job runs: `UPDATE sales SET advance_amount=?, advance_paid_at=? WHERE id=? AND advance_paid_at IS NULL`.
- If the job (or a retried queue message, or two workers) runs again, the
  `WHERE advance_paid_at IS NULL` guard makes the second UPDATE affect 0 rows
  — **no double payment**, without needing a distributed lock, because
  writers are serialized via `BEGIN IMMEDIATE` transactions.

### 5.2 Final payout on reconciliation
- A sale can only be reconciled once: `reconcile_sale` rejects
  (`409 Conflict`) any sale whose status isn't currently `pending`.
- Approved: `final = earning - advance_paid`.
- Rejected: `final = -advance_paid` (clawback of money the user wasn't entitled to).
- Both cases post a single `FINAL_SETTLEMENT` ledger row — so a sale's total
  lifetime payout is always `advance_amount + final_adjustment`, which equals
  `earning` for approved sales and `0` for rejected ones. This invariant is
  covered by `tests/test_flow.py`.

### 5.3 Withdrawal cooldown (1 per 24h)
- Enforced by checking for any withdrawal in the last 24h whose status is
  `processing` or `success`.
- **Design choice:** a withdrawal that later resolves as `failed` /
  `cancelled` / `rejected` does **not** count against the cooldown — see 5.4.

### 5.4 Failed-payout recovery (Question 2)
- When a `processing` withdrawal is updated to `failed`/`cancelled`/`rejected`,
  a `WITHDRAWAL_RECOVERY_CREDIT` ledger entry restores the amount to the
  user's balance immediately, and — because failed withdrawals don't hold
  the 24h lock — the user can re-request a withdrawal right away rather than
  waiting a full day for money they never received.
- Trade-off: this assumes "failed" is detected reasonably quickly (e.g. a
  same-day gateway webhook). If a payout can fail *after* a very long delay,
  the same mechanism still works, it just credits back balance whenever the
  webhook eventually arrives.

---

## 6. Edge cases handled

| Case | Behavior |
|---|---|
| Advance-payout job run multiple times (cron retry, duplicate queue message, concurrent workers) | Idempotent — no double payment (§5.1) |
| Reconciling a sale that's already `approved`/`rejected` | `409 Conflict` |
| Reconciling with an invalid status (e.g. `"pending"`) | `400 Bad Request` |
| Sale earning = 0 | Advance = 0; final settlement of 0 is not written to the ledger at all (avoids noise) |
| Withdrawal amount > balance | `400 Bad Request` |
| Second withdrawal within 24h of an active/successful one | `429 Too Many Requests` with the last withdrawal's timestamp |
| Resolving a withdrawal that isn't `processing` (double webhook delivery) | `409 Conflict` — prevents double-crediting the recovery amount |
| Withdrawal fails/cancels/rejects | Balance restored, cooldown released, ledger entry recorded |
| Creating a sale for a nonexistent user/brand | `404 Not Found` |
| Concurrent advance-payout job runs | Safe due to `BEGIN IMMEDIATE` transaction serialization + the `IS NULL` guard |

---

## 7. Trade-offs & things I'd change for a "real" production system

1. **SQLite → Postgres.** SQLite was chosen for zero-setup portability in an
   assignment context. All SQL is standard and the schema/queries port to
   Postgres with only a connection-string change; I'd add row-level
   `SELECT ... FOR UPDATE` locking on `sales`/`withdrawals` instead of
   relying on SQLite's whole-database `BEGIN IMMEDIATE` lock, since Postgres
   supports fine-grained locking under real concurrency.
2. **Advance payout job as a real scheduled job.** Here it's a POST endpoint
   you trigger manually/via cron; in production it'd be a Celery/Cloud
   Scheduler task with monitoring and alerting on failures.
3. **Withdrawal → payment gateway integration.** `POST /withdrawals/{id}/status`
   simulates a gateway webhook. A real system would call out to a payment
   provider on withdrawal creation and receive an async webhook, with
   signature verification and retry/backoff handling.
4. **Cooldown definition.** I interpreted "one withdrawal every 24 hours" as
   one *outstanding or successful* withdrawal per 24h, explicitly excluding
   failed/cancelled/rejected ones (otherwise Question 2's "allow another
   withdrawal" guarantee would be contradicted). An alternative, stricter
   reading — any withdrawal *request* locks the window regardless of outcome
   — is a one-line change (drop the `status IN (...)` filter) if that's the
   intended interpretation.
5. **Multi-currency / rounding.** Amounts are rounded to 2 decimals in Python;
   a production ledger would store integer minor units (paise) to avoid
   floating-point rounding drift entirely.
6. **AuthN/AuthZ.** Omitted for brevity — every endpoint would need
   user-session or admin-role checks (e.g. only admins can call `/reconcile`
   and `/payouts/advance/run`).

## 8. Running it

```bash
pip install -r requirements.txt

# Run the API
uvicorn app.main:app --reload
# -> docs at http://127.0.0.1:8000/docs

# Run the scripted demo of the PDF's worked example
python demo.py

# Run the test suite
pytest -v
```

## 9. Project structure

```
payout-system/
├── app/
│   ├── main.py       # FastAPI routes
│   ├── service.py     # Business logic (advance payout, reconciliation, withdrawals, ledger)
│   ├── db.py          # Schema + connection/transaction helpers
│   └── schemas.py      # Pydantic request models
├── tests/
│   └── test_flow.py    # Unit/integration tests incl. the PDF's exact example
├── demo.py            # End-to-end script reproducing the PDF example, with assertions
├── requirements.txt
└── README.md
```
