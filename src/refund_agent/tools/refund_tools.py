"""The tools: order lookup and eligibility (read-only, model can call) and issue_refund (code calls after approval)."""
from __future__ import annotations

import threading
import time
import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from ..data_access.repository import ShopRepository, mask_email
from ..domain.models import ORDER_ID_PATTERN, Eligibility, Order, RefundReason
from ..domain.policy import evaluate_eligibility
from .faults import Fault
from .registry import ACTION_POOL, ToolError, ToolRegistry, ToolSpec


class LookupOrderInput(BaseModel):
    order_id: str = Field(pattern=ORDER_ID_PATTERN, description="Numeric order id, e.g. '1042'")


class OrderView(BaseModel):
    order_id: str
    status: str
    item_name: str
    amount: Decimal
    currency: str
    order_date: date
    delivered_date: date | None
    final_sale: bool
    refunded_amount: Decimal
    customer_first_name: str
    customer_email_masked: str
    data_exported_at: datetime


class EligibilityInput(BaseModel):
    order_id: str = Field(pattern=ORDER_ID_PATTERN)
    reason: RefundReason = Field(description="Why the customer wants a refund")


class IssueRefundInput(BaseModel):
    order_id: str = Field(pattern=ORDER_ID_PATTERN)
    amount: Decimal = Field(gt=0, decimal_places=2)
    idempotency_key: str = Field(min_length=8)


class RefundReceipt(BaseModel):
    refund_id: str = Field(pattern=r"^rf_[0-9a-f]{12}$")
    order_id: str
    amount: Decimal
    duplicate: bool = False


class PaymentsGateway:
    """In-memory stand-in for a payment provider. Same idempotency key -> same refund, never a second payout."""

    def __init__(self) -> None:
        self._ledger: dict[str, RefundReceipt] = {}
        self._lock = threading.Lock()

    def refund(self, order_id: str, amount: Decimal, key: str, limit: Decimal) -> RefundReceipt:
        """Pay once per key, and never more than `limit` in total for the order.

        The check and the payout happen under one lock, so no timing of runs, retries or
        approvals can over-refund an order.
        """
        with self._lock:
            if key in self._ledger:
                return self._ledger[key].model_copy(update={"duplicate": True})
            already = self._total(order_id)
            if already + amount > limit:
                raise ToolError("OVER_REFUND", f"Order {order_id} has {limit - already} left to refund; "
                                               f"refusing {amount}")
            receipt = RefundReceipt(refund_id=f"rf_{uuid.uuid4().hex[:12]}", order_id=order_id, amount=amount)
            self._ledger[key] = receipt
            return receipt

    @property
    def payouts(self) -> list[RefundReceipt]:
        return list(self._ledger.values())

    def refunded_total(self, order_id: str) -> Decimal:
        with self._lock:
            return self._total(order_id)

    def _total(self, order_id: str) -> Decimal:
        return sum((r.amount for r in self._ledger.values() if r.order_id == order_id), Decimal("0.00"))


def with_our_refunds(order: Order, payments: PaymentsGateway) -> Order:
    """The export only knows refunds made before it was taken. Add the ones we paid since."""
    extra = payments.refunded_total(order.order_id)
    return order.model_copy(update={"refunded_amount": order.refunded_amount + extra}) if extra else order


def build_agent_tools(repo: ShopRepository, payments: PaymentsGateway, now: datetime, faults: set[Fault],
                      timeout_s: float, retries: int = 1) -> ToolRegistry:
    """Read-only tools the model may call during a run."""
    reg = ToolRegistry(timeout_s=timeout_s, retries=retries)

    def current_order(order_id: str) -> Order:
        if Fault.STALE_DATA in faults or repo.is_stale(now):
            hours = int(repo.data_age(now).total_seconds() // 3600)
            raise ToolError("STALE_DATA", f"Order data is {hours}h old or flagged stale; "
                                          f"limit is {repo.policy.max_data_age_hours}h. Do not act on it.")
        order = repo.get_order(order_id)
        if order is None:
            raise ToolError("ORDER_NOT_FOUND", f"No order {order_id} exists")
        return with_our_refunds(order, payments)

    def lookup_order(args: LookupOrderInput) -> dict:
        if Fault.LOOKUP_TIMEOUT in faults:
            time.sleep(timeout_s * 3)
        if Fault.LOOKUP_DOWN in faults:
            raise ToolError("DEPENDENCY_UNAVAILABLE", "Order service is not responding", retryable=True)
        order = current_order(args.order_id)
        customer = repo.get_customer(order.customer_id)
        return OrderView(
            **order.model_dump(include={"order_id", "status", "item_name", "amount", "currency", "order_date",
                                        "delivered_date", "final_sale", "refunded_amount"}),
            customer_first_name=customer.name.split()[0],
            customer_email_masked=mask_email(customer.email),
            data_exported_at=repo.exported_at,
        ).model_dump()

    def check_refund_eligibility(args: EligibilityInput) -> dict:
        if Fault.INVALID_TOOL_RESPONSE in faults:
            return {"order_id": args.order_id, "eligible": "maybe", "eligible_amount": "-5"}
        order = current_order(args.order_id)
        return evaluate_eligibility(order, args.reason, repo.policy, now.date()).model_dump()

    reg.register(ToolSpec(
        "lookup_order",
        "Look up one order by id. Returns item, amount, dates, status and the customer's first name.",
        LookupOrderInput, OrderView, lookup_order))
    reg.register(ToolSpec(
        "check_refund_eligibility",
        "Apply the refund policy to an order for a given reason. Returns eligible, eligible_amount and why. "
        "This is the only source of truth for refund amounts.",
        EligibilityInput, Eligibility, check_refund_eligibility))
    return reg


def build_action_tools(payments: PaymentsGateway, repo: ShopRepository, faults: set[Fault], timeout_s: float,
                       retries: int = 1) -> ToolRegistry:
    """Tools with side effects. Only the orchestrator calls these, and only after approval."""
    reg = ToolRegistry(timeout_s=timeout_s, retries=retries, pool=ACTION_POOL)

    def issue_refund(args: IssueRefundInput) -> dict:
        if Fault.PAYMENTS_DOWN in faults:
            raise ToolError("DEPENDENCY_UNAVAILABLE", "Payment service returned 503", retryable=True)
        order = repo.get_order(args.order_id)
        if order is None:
            raise ToolError("ORDER_NOT_FOUND", f"No order {args.order_id} exists")
        receipt = payments.refund(args.order_id, args.amount, args.idempotency_key, limit=order.refundable_balance)
        if Fault.PAYMENTS_TIMEOUT in faults and not receipt.duplicate:
            time.sleep(timeout_s * 3)  # money moved, but we stop waiting before the answer arrives
        return receipt.model_dump()

    reg.register(ToolSpec("issue_refund", "Send money back to the customer.", IssueRefundInput,
                          RefundReceipt, issue_refund))
    return reg
