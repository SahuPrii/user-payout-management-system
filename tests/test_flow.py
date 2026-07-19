import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient
from app.db import init_db, DB_PATH
from app.main import app


@pytest.fixture()
def client():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db(reset=True)
    return TestClient(app)


def _make_user_brand_sale(client, earning=40):
    user = client.post("/users", json={"name": "Jane"}).json()
    brand = client.post("/brands", json={"name": "brand_x"}).json()
    sale = client.post("/sales", json={"user_id": user["id"], "brand_id": brand["id"], "earning": earning}).json()
    return user, brand, sale


def test_advance_payout_is_10_percent(client):
    user, brand, sale = _make_user_brand_sale(client, earning=30)
    client.post("/payouts/advance/run")
    balance = client.get(f"/users/{user['id']}/balance").json()
    assert balance["balance"] == 3.0


def test_advance_payout_job_is_idempotent(client):
    user, brand, sale = _make_user_brand_sale(client, earning=30)
    client.post("/payouts/advance/run")
    client.post("/payouts/advance/run")
    client.post("/payouts/advance/run")
    balance = client.get(f"/users/{user['id']}/balance").json()
    assert balance["balance"] == 3.0  # not 9.0


def test_approved_sale_final_payout(client):
    # PDF Case 1: earning=30, advance=3 -> approved -> final adjustment +27
    user, brand, sale = _make_user_brand_sale(client, earning=30)
    client.post("/payouts/advance/run")
    result = client.post(f"/sales/{sale['id']}/reconcile", json={"status": "approved"}).json()
    assert result["final_adjustment"] == 27
    balance = client.get(f"/users/{user['id']}/balance").json()
    assert balance["balance"] == 30  # 3 advance + 27 final = 30 total


def test_rejected_sale_final_payout(client):
    # PDF Case 2: earning=50, advance=5 -> rejected -> adjustment -5
    user, brand, sale = _make_user_brand_sale(client, earning=50)
    client.post("/payouts/advance/run")
    result = client.post(f"/sales/{sale['id']}/reconcile", json={"status": "rejected"}).json()
    assert result["final_adjustment"] == -5
    balance = client.get(f"/users/{user['id']}/balance").json()
    assert balance["balance"] == 0  # 5 advance - 5 clawback = 0


def test_cannot_reconcile_twice(client):
    user, brand, sale = _make_user_brand_sale(client, earning=40)
    client.post("/payouts/advance/run")
    client.post(f"/sales/{sale['id']}/reconcile", json={"status": "approved"})
    second = client.post(f"/sales/{sale['id']}/reconcile", json={"status": "rejected"})
    assert second.status_code == 409


def test_withdrawal_24h_cooldown(client):
    user, brand, sale = _make_user_brand_sale(client, earning=100)
    client.post("/payouts/advance/run")
    client.post(f"/sales/{sale['id']}/reconcile", json={"status": "approved"})
    first = client.post("/withdrawals", json={"user_id": user["id"], "amount": 10})
    assert first.status_code == 200
    second = client.post("/withdrawals", json={"user_id": user["id"], "amount": 5})
    assert second.status_code == 429


def test_withdrawal_insufficient_balance(client):
    user, brand, sale = _make_user_brand_sale(client, earning=10)
    resp = client.post("/withdrawals", json={"user_id": user["id"], "amount": 1000})
    assert resp.status_code == 400


def test_failed_withdrawal_recovers_balance_and_allows_retry(client):
    user, brand, sale = _make_user_brand_sale(client, earning=100)
    client.post("/payouts/advance/run")
    client.post(f"/sales/{sale['id']}/reconcile", json={"status": "approved"})
    balance_before = client.get(f"/users/{user['id']}/balance").json()["balance"]

    wd = client.post("/withdrawals", json={"user_id": user["id"], "amount": balance_before}).json()
    balance_mid = client.get(f"/users/{user['id']}/balance").json()["balance"]
    assert balance_mid == 0

    client.post(f"/withdrawals/{wd['id']}/status", json={"status": "failed"})
    balance_after = client.get(f"/users/{user['id']}/balance").json()["balance"]
    assert balance_after == balance_before  # credited back

    retry = client.post("/withdrawals", json={"user_id": user["id"], "amount": balance_before})
    assert retry.status_code == 200  # allowed immediately, not blocked by cooldown


def test_cannot_double_resolve_withdrawal(client):
    user, brand, sale = _make_user_brand_sale(client, earning=100)
    client.post("/payouts/advance/run")
    client.post(f"/sales/{sale['id']}/reconcile", json={"status": "approved"})
    wd = client.post("/withdrawals", json={"user_id": user["id"], "amount": 5}).json()
    client.post(f"/withdrawals/{wd['id']}/status", json={"status": "success"})
    second = client.post(f"/withdrawals/{wd['id']}/status", json={"status": "failed"})
    assert second.status_code == 409


def test_full_pdf_example_matches(client):
    user = client.post("/users", json={"name": "john_doe"}).json()
    brand = client.post("/brands", json={"name": "brand_1"}).json()
    sales = [client.post("/sales", json={"user_id": user["id"], "brand_id": brand["id"], "earning": 40}).json()
             for _ in range(3)]

    adv = client.post("/payouts/advance/run").json()
    assert sum(p["advance_paid"] for p in adv["processed"]) == 12

    client.post(f"/sales/{sales[0]['id']}/reconcile", json={"status": "rejected"})
    client.post(f"/sales/{sales[1]['id']}/reconcile", json={"status": "approved"})
    client.post(f"/sales/{sales[2]['id']}/reconcile", json={"status": "approved"})

    balance = client.get(f"/users/{user['id']}/balance").json()["balance"]
    # advance 12 total, then -4 + 36 + 36 = 68 final => running balance 80
    assert balance == 80
