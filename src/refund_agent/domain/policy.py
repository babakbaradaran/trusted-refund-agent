"""Refund policy rules. Pure functions: same input, same answer."""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .models import Eligibility, Order, OrderStatus, RefundPolicy, RefundReason

CENT = Decimal("0.01")


def evaluate_eligibility(order: Order, reason: RefundReason, policy: RefundPolicy, today: date) -> Eligibility:
    reason = RefundReason(reason)
    rule = policy.reasons[reason]

    def no(rule_id: str, why: str) -> Eligibility:
        return Eligibility(order_id=order.order_id, reason=reason, eligible=False,
                           eligible_amount=Decimal("0.00"), rule=rule_id, explanation=why)

    if order.status == OrderStatus.CANCELLED:
        return no("cancelled", "Order was cancelled, nothing was charged.")
    if order.refundable_balance <= 0:
        return no("already_refunded", "Order has already been fully refunded.")
    if order.final_sale and not rule.allow_final_sale:
        return no("final_sale", "Final-sale items can only be refunded if damaged or wrong.")

    if rule.needs_delivery:
        if order.delivered_date is None:
            return no("not_delivered_yet", "Order has not been delivered yet.")
        start = order.delivered_date
    else:
        if order.status == OrderStatus.DELIVERED:
            return no("marked_delivered", "Carrier marked this order as delivered.")
        days_waiting = (today - order.order_date).days
        if days_waiting < rule.min_days_since_order:
            return no("too_early", f"Order is {days_waiting} days old; wait {rule.min_days_since_order} days.")
        start = order.order_date

    age = (today - start).days
    if age > rule.window_days:
        return no("window_expired", f"{age} days since {'delivery' if rule.needs_delivery else 'order'}; "
                                    f"limit is {rule.window_days} days.")

    amount = (order.refundable_balance * rule.refund_percent / 100).quantize(CENT, ROUND_HALF_UP)
    pct = "" if rule.refund_percent == 100 else f" ({rule.refund_percent}% after restocking fee)"
    return Eligibility(order_id=order.order_id, reason=reason, eligible=True, eligible_amount=amount,
                       rule=f"{reason.value}_within_{rule.window_days}d",
                       explanation=f"Eligible: {age} days, within {rule.window_days}-day window{pct}.")


def needs_human_approval(amount: Decimal, policy: RefundPolicy) -> bool:
    return amount > policy.auto_approve_limit
