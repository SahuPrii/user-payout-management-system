"""
SQLite persistence layer.

Design notes
------------
- We use a single SQLite file for simplicity/portability of the assignment.
  In production this would be Postgres/MySQL, but every query here is
  written in plain, portable SQL so the swap is mechanical.
- `ledger_entries` is the append-only source of truth for money movement
  (event-sourced style). `users.balance_cache` is a denormalized, always-
  consistent-with-the-ledger cache that is updated in the *same* DB
  transaction as every ledger insert, so reads are O(1) but the ledger can
  always be replayed to audit or rebuild the cache if it ever drifts.
- Idempotency for the advance payout job is enforced at the row level:
  `sales.advance_paid_at` starts NULL and is only written by an UPDATE that
  also matches `advance_paid_at IS NULL`. If the job runs twice (or two
  workers run it concurrently), the second UPDATE affects 0 rows and is a
  safe no-op. This is enforced by SQLite's own transaction/locking, no
  extra "distributed lock" is needed for a single-writer DB.
"""
import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "payout_system.db")
DB_PATH = os.path.abspath(DB_PATH)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT,
    balance_cache REAL NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brands (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sales (
    id               TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL REFERENCES users(id),
    brand_id         TEXT NOT NULL REFERENCES brands(id),
    earning          REAL NOT NULL CHECK (earning >= 0),
    status           TEXT NOT NULL CHECK (status IN ('pending','approved','rejected')) DEFAULT 'pending',
    advance_amount   REAL NOT NULL DEFAULT 0,
    advance_paid_at  TEXT,          -- NULL until advance payout job pays it out; guards idempotency
    reconciled_at    TEXT,          -- NULL until an admin reconciles it
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sales_user_status ON sales(user_id, status);
CREATE INDEX IF NOT EXISTS idx_sales_advance_pending ON sales(status, advance_paid_at);

-- Append-only financial ledger. Every rupee credited/debited to a user is
-- recorded here. Sum(amount) for a user == their balance.
CREATE TABLE IF NOT EXISTS ledger_entries (
    id             TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL REFERENCES users(id),
    amount         REAL NOT NULL,   -- positive = credit, negative = debit
    type           TEXT NOT NULL CHECK (type IN (
                        'ADVANCE_CREDIT',
                        'FINAL_SETTLEMENT',
                        'WITHDRAWAL_DEBIT',
                        'WITHDRAWAL_RECOVERY_CREDIT'
                    )),
    reference_type TEXT NOT NULL,   -- 'sale' | 'withdrawal'
    reference_id   TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger_entries(user_id, created_at);

CREATE TABLE IF NOT EXISTS withdrawals (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id),
    amount     REAL NOT NULL CHECK (amount > 0),
    status     TEXT NOT NULL CHECK (status IN ('processing','success','failed','cancelled','rejected')) DEFAULT 'processing',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_withdrawals_user_time ON withdrawals(user_id, created_at);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(reset: bool = False):
    if reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction():
    """Yields a connection inside a BEGIN IMMEDIATE transaction so writers
    serialize instead of racing (needed for the advance-payout idempotency
    guarantee under concurrent job runs)."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
