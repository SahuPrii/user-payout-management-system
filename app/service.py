"""
Business logic for the User Payout Management System.

All money-moving operations go through `_post_ledger_entry`, which writes
the append-only ledger row AND updates `users.balance_cache` inside the
same transaction, so the cache can never drift from the ledger.
"""
import uuid
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException

from app.db import transaction, get_conn

ADVANCE_PCT = 0.10
WITHDRAWAL_COOLDOWN = timedelta(hours=24)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _post_ledger_entry(conn, user_id: str, amount: float, type_: str,
                        reference_type: str, reference_id: str):
    """Insert a ledger row and update the user's cached balance. Must be
    called with an already-open transaction `conn`."""
    conn.execute(
        "INSERT INTO ledger_entries (id, user_id, amount, type, reference_type, reference_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_new_id("ledg"), user_id, amount, type_, reference_type, reference_id, _now()),
    )
    conn.execute(
        "UPDATE users SET balance_cache = balance_cache + ? WHERE id = ?",
        (amount, user_id),
    )


# ---------------------------------------------------------------------------
# Users / Brands / Sales (basic CRUD)
# ---------------------------------------------------------------------------

def create_user(name: str, email: str | None):
    with transaction() as conn:
        uid = _new_id("user")
        conn.execute(
            "INSERT INTO users (id, name, email, balance_cache, created_at) VALUES (?, ?, ?, 0, ?)",
            (uid, name, email, _now()),
        )
        return {"id": uid, "name": name, "email": email, "balance": 0}


def create_brand(name: str):
    with transaction() as conn:
        bid = _new_id("brand")
        conn.execute("INSERT INTO brands (id, name) VALUES (?, ?)", (bid, name))
        return {"id": bid, "name": name}


def create_sale(user_id: str, brand_id: str, earning: float):
    with transaction() as conn:
        _require_user(conn, user_id)
        _require_brand(conn, brand_id)
        sid = _new_id("sale")
        conn.execute(
            "INSERT INTO sales (id, user_id, brand_id, earning, status, advance_amount, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', 0, ?)",
            (sid, user_id, brand_id, earning, _now()),
        )
        return _sale_row_to_dict(conn.execute("SELECT * FROM sales WHERE id=?", (sid,)).fetchone())


def list_sales(user_id: str):
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM sales WHERE user_id=? ORDER BY created_at", (user_id,)).fetchall()
        return [_sale_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def _require_user(conn, user_id):
    row = conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"user {user_id} not found")


def _require_brand(conn, brand_id):
    row = conn.execute("SELECT id FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"brand {brand_id} not found")


def _sale_row_to_dict(row):
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "brand_id": row["brand_id"],
        "earning": row["earning"],
        "status": row["status"],
        "advance_amount": row["advance_amount"],
        "advance_paid_at": row["advance_paid_at"],
        "reconciled_at": row["reconciled_at"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# 1. Advance Payout job
# ---------------------------------------------------------------------------

def run_advance_payout_job():
    """Pays 10% advance on every pending sale that hasn't been advanced yet.

    Idempotent by design: the UPDATE guard `advance_paid_at IS NULL` means
    re-running this job (accidentally, or by a retried cron/queue message)
    never double-pays a sale, even under concurrent execution, because
    SQLite serializes writers inside BEGIN IMMEDIATE transactions.
    """
    processed = []
    with transaction() as conn:
        candidates = conn.execute(
            "SELECT id, user_id, earning FROM sales WHERE status='pending' AND advance_paid_at IS NULL"
        ).fetchall()

        for sale in candidates:
            advance = round(sale["earning"] * ADVANCE_PCT, 2)
            cur = conn.execute(
                "UPDATE sales SET advance_amount=?, advance_paid_at=? "
                "WHERE id=? AND advance_paid_at IS NULL",
                (advance, _now(), sale["id"]),
            )
            if cur.rowcount == 0:
                # Someone else (a concurrent run) already advanced this sale.
                continue
            _post_ledger_entry(conn, sale["user_id"], advance, "ADVANCE_CREDIT", "sale", sale["id"])
            processed.append({"sale_id": sale["id"], "user_id": sale["user_id"], "advance_paid": advance})
    return processed


# ---------------------------------------------------------------------------
# 2. Reconciliation -> Final Payout
# ---------------------------------------------------------------------------

def reconcile_sale(sale_id: str, new_status: str):
    if new_status not in ("approved", "rejected"):
        raise HTTPException(400, "status must be 'approved' or 'rejected'")

    with transaction() as conn:
        sale = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
        if not sale:
            raise HTTPException(404, "sale not found")
        if sale["status"] != "pending":
            # Reconciliation is a one-way transition; re-reconciling would
            # double-post ledger entries. Treat as a client error.
            raise HTTPException(409, f"sale already reconciled as '{sale['status']}'")

        advance = sale["advance_amount"] or 0.0
        if new_status == "approved":
            final_amount = round(sale["earning"] - advance, 2)
        else:  # rejected
            final_amount = round(-advance, 2)

        conn.execute(
            "UPDATE sales SET status=?, reconciled_at=? WHERE id=?",
            (new_status, _now(), sale_id),
        )
        if final_amount != 0:
            _post_ledger_entry(conn, sale["user_id"], final_amount, "FINAL_SETTLEMENT", "sale", sale_id)

        return {
            "sale_id": sale_id,
            "status": new_status,
            "earning": sale["earning"],
            "advance_paid": advance,
            "final_adjustment": final_amount,
        }


# ---------------------------------------------------------------------------
# Balance / Ledger
# ---------------------------------------------------------------------------

def get_balance(user_id: str):
    conn = get_conn()
    try:
        row = conn.execute("SELECT balance_cache FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "user not found")
        return {"user_id": user_id, "balance": row["balance_cache"]}
    finally:
        conn.close()


def get_ledger(user_id: str):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM ledger_entries WHERE user_id=? ORDER BY created_at", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. Withdrawals (+ Question 2: failed payout recovery)
# ---------------------------------------------------------------------------

def create_withdrawal(user_id: str, amount: float):
    with transaction() as conn:
        _require_user(conn, user_id)

        # 24-hour cooldown: only withdrawals that are still 'processing' or
        # already 'success' count as consuming the window. A withdrawal
        # that later failed/was cancelled/rejected frees the slot
        # immediately (see Question 2) rather than blocking the user for
        # a full day for money they never actually received.
        cutoff = (datetime.now(timezone.utc) - WITHDRAWAL_COOLDOWN).isoformat()
        last = conn.execute(
            "SELECT id, created_at FROM withdrawals "
            "WHERE user_id=? AND status IN ('processing','success') AND created_at > ? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, cutoff),
        ).fetchone()
        if last:
            raise HTTPException(
                429,
                f"Only one withdrawal is allowed every 24 hours. "
                f"Last withdrawal was at {last['created_at']}.",
            )

        balance_row = conn.execute("SELECT balance_cache FROM users WHERE id=?", (user_id,)).fetchone()
        if balance_row["balance_cache"] < amount:
            raise HTTPException(400, "insufficient withdrawable balance")

        wid = _new_id("wd")
        now = _now()
        conn.execute(
            "INSERT INTO withdrawals (id, user_id, amount, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'processing', ?, ?)",
            (wid, user_id, amount, now, now),
        )
        _post_ledger_entry(conn, user_id, -amount, "WITHDRAWAL_DEBIT", "withdrawal", wid)
        return {"id": wid, "user_id": user_id, "amount": amount, "status": "processing"}


def update_withdrawal_status(withdrawal_id: str, new_status: str):
    with transaction() as conn:
        wd = conn.execute("SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,)).fetchone()
        if not wd:
            raise HTTPException(404, "withdrawal not found")
        if wd["status"] != "processing":
            raise HTTPException(409, f"withdrawal already resolved as '{wd['status']}'")

        conn.execute(
            "UPDATE withdrawals SET status=?, updated_at=? WHERE id=?",
            (new_status, _now(), withdrawal_id),
        )

        if new_status in ("failed", "cancelled", "rejected"):
            # Question 2: credit the money back so the user can withdraw it again.
            _post_ledger_entry(
                conn, wd["user_id"], wd["amount"], "WITHDRAWAL_RECOVERY_CREDIT",
                "withdrawal", withdrawal_id,
            )

        return {"id": withdrawal_id, "status": new_status}


def list_withdrawals(user_id: str):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM withdrawals WHERE user_id=? ORDER BY created_at DESC", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
