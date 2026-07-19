"""
Reproduces the worked example from the assignment PDF end-to-end, using the
actual FastAPI app (no mocking) via Starlette's TestClient. This exercises
the real HTTP endpoints and the real SQLite-backed service layer.

Run:  python demo.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from app.db import init_db, DB_PATH
from fastapi.testclient import TestClient
from app.main import app


def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db(reset=True)
    client = TestClient(app)

    print("=== 1. Setup: user + brand ===")
    user = client.post("/users", json={"name": "John Doe", "email": "john@example.com"}).json()
    brand = client.post("/brands", json={"name": "brand_1"}).json()
    print("user:", user)
    print("brand:", brand)

    print("\n=== 2. Create 3 pending sales of earning=40 each (per PDF example) ===")
    sales = []
    for _ in range(3):
        s = client.post("/sales", json={"user_id": user["id"], "brand_id": brand["id"], "earning": 40}).json()
        sales.append(s)
        print(s)

    print("\n=== 3. Run advance payout job (expect 10% of 40 = 4 per sale, total 12) ===")
    result = client.post("/payouts/advance/run").json()
    print(result)

    balance = client.get(f"/users/{user['id']}/balance").json()
    print("Balance after advance:", balance)
    assert balance["balance"] == 12, f"expected 12, got {balance['balance']}"

    print("\n=== 4. Re-run advance job again to prove idempotency (should process 0) ===")
    result2 = client.post("/payouts/advance/run").json()
    print(result2)
    assert result2["processed"] == [], "advance job must not double-pay!"

    print("\n=== 5. Reconciliation: sale[0]=rejected, sale[1]=approved, sale[2]=approved ===")
    r0 = client.post(f"/sales/{sales[0]['id']}/reconcile", json={"status": "rejected"}).json()
    r1 = client.post(f"/sales/{sales[1]['id']}/reconcile", json={"status": "approved"}).json()
    r2 = client.post(f"/sales/{sales[2]['id']}/reconcile", json={"status": "approved"}).json()
    for r in (r0, r1, r2):
        print(r)

    final_balance = client.get(f"/users/{user['id']}/balance").json()
    print("\nFinal balance:", final_balance)
    # PDF example expects a final payout total of 68 on top of what was already
    # advanced -- i.e. running balance = 12 (advance) - 4 (rejected adj) + 36 + 36 = 80
    # which equals 68 (final payout) + 12 (advance already held) = 80.
    assert final_balance["balance"] == 80, f"expected 80, got {final_balance['balance']}"
    print("Matches PDF example: -4 + 36 + 36 = 68 final payout (80 total incl. advance already paid) ✔")

    print("\n=== 6. Withdrawal flow ===")
    wd = client.post("/withdrawals", json={"user_id": user["id"], "amount": 80}).json()
    print("Withdrawal created:", wd)

    print("\n=== 7. Attempt second withdrawal within 24h (should be blocked, 429) ===")
    blocked = client.post("/withdrawals", json={"user_id": user["id"], "amount": 1})
    print(blocked.status_code, blocked.json())
    assert blocked.status_code == 429

    print("\n=== 8. Simulate the withdrawal failing (Question 2 recovery) ===")
    fail = client.post(f"/withdrawals/{wd['id']}/status", json={"status": "failed"}).json()
    print(fail)
    balance_after_fail = client.get(f"/users/{user['id']}/balance").json()
    print("Balance after failed withdrawal (should be back to 80):", balance_after_fail)
    assert balance_after_fail["balance"] == 80

    print("\n=== 9. Retry withdrawal immediately after failure (should now succeed) ===")
    wd2 = client.post("/withdrawals", json={"user_id": user["id"], "amount": 80}).json()
    print("Retried withdrawal:", wd2)
    assert wd2["status"] == "processing"

    print("\n=== 10. Full ledger for audit ===")
    for entry in client.get(f"/users/{user['id']}/ledger").json():
        print(entry)

    print("\nAll assertions passed. ✔")


if __name__ == "__main__":
    main()
