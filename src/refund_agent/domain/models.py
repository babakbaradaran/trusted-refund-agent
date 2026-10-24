"""Domain types shared by data access, tools, agent and API."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ORDER_ID_PATTERN = r"^\d{4,6}$"


class OrderStatus(StrEnum):
    PLACED = "placed"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class Order(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str = Field(pattern=ORDER_ID_PATTERN)
    customer_id: str = Field(pattern=r"^C-\d{2,}$")
    order_date: date
    delivered_date: date | None = None
    status: OrderStatus
    item_name: str = Field(min_length=1)
    category: str
    amount: Decimal = Field(gt=0, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    final_sale: bool
    refunded_amount: Decimal = Field(ge=0, decimal_places=2)

    @model_validator(mode="after")
    def _consistent(self) -> Order:
        if self.status == OrderStatus.DELIVERED and self.delivered_date is None:
            raise ValueError("delivered order needs delivered_date")
        if self.delivered_date and self.delivered_date < self.order_date:
            raise ValueError("delivered_date is before order_date")
        if self.refunded_amount > self.amount:
            raise ValueError("refunded_amount exceeds amount")
        return self

    @property
    def refundable_balance(self) -> Decimal:
        return self.amount - self.refunded_amount


class Customer(BaseModel):
    model_config = ConfigDict(frozen=True)

    customer_id: str
    name: str
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    tier: str


class RefundReason(StrEnum):
    DAMAGED = "damaged"
    WRONG_ITEM = "wrong_item"
    NOT_DELIVERED = "not_delivered"
    CHANGED_MIND = "changed_mind"


class ReasonRule(BaseModel):
    window_days: int = Field(gt=0)
    refund_percent: int = Field(gt=0, le=100)
    allow_final_sale: bool
    needs_delivery: bool
    min_days_since_order: int = 0


class RefundPolicy(BaseModel):
    version: str
    auto_approve_limit: Decimal
    max_data_age_hours: int
    reasons: dict[RefundReason, ReasonRule]


class Eligibility(BaseModel):
    order_id: str
    reason: RefundReason
    eligible: bool
    eligible_amount: Decimal = Field(ge=0)
    rule: str
    explanation: str


class Decision(StrEnum):
    REFUND = "refund"
    DENY = "deny"
    ESCALATE = "escalate"
    NEEDS_INFO = "needs_info"


class Proposal(BaseModel):
    """What the agent wants to do. Code checks it before anything happens."""

    decision: Decision
    order_id: str | None = Field(default=None, pattern=ORDER_ID_PATTERN)
    amount: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    reason: RefundReason | None = None
    customer_reply: str = Field(min_length=1, max_length=1200)
    rationale: str = Field(default="", max_length=600)

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_str(cls, v: Any) -> Any:
        return Decimal(str(v)) if isinstance(v, (int, float)) else v


class RunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    REJECTED = "rejected"
    ESCALATED = "escalated"
    FAILED = "failed"
    BLOCKED = "blocked"


class TraceEvent(BaseModel):
    ts: datetime
    step: int
    kind: str  # llm_call | tool_call | guardrail | state | approval | error
    name: str
    data: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float | None = None


class Approval(BaseModel):
    by: str
    at: datetime
    approved: bool
    note: str = ""


class Run(BaseModel):
    run_id: str
    created_at: datetime
    updated_at: datetime
    created_by: str
    status: RunStatus
    customer_message: str
    faults: list[str] = Field(default_factory=list)
    model: str = ""
    proposal: Proposal | None = None
    final_reply: str | None = None
    refund_id: str | None = None
    error: str | None = None
    approval: Approval | None = None
    facts: dict[str, Any] = Field(default_factory=dict)  # tool outputs the agent has seen
    transcript: list[dict[str, Any]] = Field(default_factory=list)  # model conversation
    events: list[TraceEvent] = Field(default_factory=list)
