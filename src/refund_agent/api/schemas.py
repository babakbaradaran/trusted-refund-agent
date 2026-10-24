"""Request and response shapes for the HTTP API, plus formatting for display."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..data_access.repository import mask_email
from ..domain.models import Approval, Customer, Order, Proposal, Run, RunStatus, TraceEvent

CURRENCY_SYMBOL = {"USD": "$"}


def format_money(amount: Decimal, currency: str = "USD") -> str:
    return f"{CURRENCY_SYMBOL.get(currency, currency + ' ')}{amount:,.2f}"


def format_date(d: date | None) -> str:
    return d.strftime("%b %d, %Y").replace(" 0", " ") if d else "—"


class CreateRunRequest(BaseModel):
    customer_message: str = Field(min_length=3, max_length=2000)
    faults: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("customer_message")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("customer_message is blank")
        return v.strip()


class ApproveRequest(BaseModel):
    note: str = Field(default="", max_length=500)
    customer_reply: str | None = Field(default=None, min_length=1, max_length=1200)


class RejectRequest(BaseModel):
    note: str = Field(min_length=3, max_length=500)


class RunView(BaseModel):
    run_id: str
    status: RunStatus
    needs_action: bool
    created_at: datetime
    updated_at: datetime
    created_by: str
    customer_message: str
    model: str
    faults: list[str]
    proposal: Proposal | None
    proposal_amount_display: str | None
    final_reply: str | None
    refund_id: str | None
    error: str | None
    approval: Approval | None
    step_count: int

    @classmethod
    def of(cls, run: Run) -> RunView:
        amount = format_money(run.proposal.amount) if run.proposal else None
        return cls(**run.model_dump(include=set(cls.model_fields) & set(Run.model_fields)),
                   needs_action=run.status in (RunStatus.AWAITING_APPROVAL, RunStatus.ESCALATED),
                   proposal_amount_display=amount,
                   step_count=len({e.step for e in run.events if e.step > 0}))


class TraceView(BaseModel):
    run_id: str
    status: RunStatus
    facts: dict[str, Any]
    events: list[TraceEvent]
    transcript: list[dict[str, Any]]


class OrderPresentation(BaseModel):
    order_id: str
    item: str
    status: str
    amount: str
    refunded: str
    refundable_balance: str
    ordered_on: str
    delivered_on: str
    final_sale: str
    customer: str
    customer_email: str

    @classmethod
    def of(cls, order: Order, customer: Customer) -> OrderPresentation:
        return cls(
            order_id=order.order_id, item=order.item_name, status=order.status.value.capitalize(),
            amount=format_money(order.amount, order.currency),
            refunded=format_money(order.refunded_amount, order.currency),
            refundable_balance=format_money(order.refundable_balance, order.currency),
            ordered_on=format_date(order.order_date), delivered_on=format_date(order.delivered_date),
            final_sale="Yes" if order.final_sale else "No",
            customer=customer.name, customer_email=mask_email(customer.email))
