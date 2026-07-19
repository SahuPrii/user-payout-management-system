from pydantic import BaseModel, Field
from typing import Optional, Literal


class CreateUserRequest(BaseModel):
    name: str
    email: Optional[str] = None


class CreateBrandRequest(BaseModel):
    name: str


class CreateSaleRequest(BaseModel):
    user_id: str
    brand_id: str
    earning: float = Field(gt=0)


class ReconcileSaleRequest(BaseModel):
    status: Literal["approved", "rejected"]


class CreateWithdrawalRequest(BaseModel):
    user_id: str
    amount: float = Field(gt=0)


class UpdateWithdrawalStatusRequest(BaseModel):
    status: Literal["success", "failed", "cancelled", "rejected"]
