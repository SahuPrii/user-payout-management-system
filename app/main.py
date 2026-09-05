from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.db import init_db
from app.schemas import (
    CreateUserRequest, CreateBrandRequest, CreateSaleRequest,
    ReconcileSaleRequest, CreateWithdrawalRequest, UpdateWithdrawalStatusRequest,
)
from app import service


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="User Payout Management System",
    description="LLD implementation: advance payouts, reconciliation, withdrawals, and failed-payout recovery.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.post("/users", tags=["users"])
def create_user(body: CreateUserRequest):
    return service.create_user(body.name, body.email)


@app.get("/users/{user_id}/balance", tags=["users"])
def get_balance(user_id: str):
    return service.get_balance(user_id)


@app.get("/users/{user_id}/ledger", tags=["users"])
def get_ledger(user_id: str):
    return service.get_ledger(user_id)


@app.post("/brands", tags=["brands"])
def create_brand(body: CreateBrandRequest):
    return service.create_brand(body.name)


@app.post("/sales", tags=["sales"])
def create_sale(body: CreateSaleRequest):
    return service.create_sale(body.user_id, body.brand_id, body.earning)


@app.get("/sales/{user_id}", tags=["sales"])
def list_sales(user_id: str):
    return service.list_sales(user_id)


@app.post("/sales/{sale_id}/reconcile", tags=["sales"])
def reconcile_sale(sale_id: str, body: ReconcileSaleRequest):
    return service.reconcile_sale(sale_id, body.status)


@app.post("/payouts/advance/run", tags=["payouts"])
def run_advance_payout_job():
    """Simulates the scheduled job that pays 10% advance on all eligible
    pending sales. Safe to call repeatedly (idempotent)."""
    return {"processed": service.run_advance_payout_job()}


@app.post("/withdrawals", tags=["withdrawals"])
def create_withdrawal(body: CreateWithdrawalRequest):
    return service.create_withdrawal(body.user_id, body.amount)

@app.post("/withdrawals/{withdrawal_id}/status", tags=["withdrawals"])
def update_withdrawal_status(withdrawal_id: str, body: UpdateWithdrawalStatusRequest):
    """Simulates a payment-gateway webhook updating a withdrawal's outcome."""
    return service.update_withdrawal_status(withdrawal_id, body.status)

@app.get("/withdrawals/user/{user_id}", tags=["withdrawals"])
def list_withdrawals(user_id: str):
    return service.list_withdrawals(user_id)
@app.get("/", tags=["meta"])
def root():
    return {"status": "ok", "docs": "/docs"}
