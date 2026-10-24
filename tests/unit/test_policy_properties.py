"""Rules that must hold for any order, not just the ten in the sample file. Hypothesis generates the orders."""
from datetime import date, timedelta
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from refund_agent.config import ROOT
from refund_agent.data_access.repository import ShopRepository
from refund_agent.domain.models import Order, OrderStatus, RefundReason
from refund_agent.domain.policy import evaluate_eligibility

POLICY = ShopRepository(ROOT / "data").policy
TODAY = date(2026, 10, 20)

money = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("5000"), places=2)


@st.composite
def orders(draw):
    amount = draw(money)
    refunded = draw(st.decimals(min_value=Decimal("0"), max_value=amount, places=2))
    ordered = TODAY - timedelta(days=draw(st.integers(0, 200)))
    status = draw(st.sampled_from(list(OrderStatus)))
    delivered = None
    if status == OrderStatus.DELIVERED:
        delivered = ordered + timedelta(days=draw(st.integers(0, (TODAY - ordered).days)))
    return Order(order_id="2000", customer_id="C-01", order_date=ordered, delivered_date=delivered,
                 status=status, item_name="Thing", category="misc", amount=amount, currency="USD",
                 final_sale=draw(st.booleans()), refunded_amount=refunded)


reasons = st.sampled_from(list(RefundReason))


@settings(max_examples=500, deadline=None)
@given(orders(), reasons)
def test_never_more_than_what_is_left(order, reason):
    result = evaluate_eligibility(order, reason, POLICY, TODAY)
    assert Decimal("0") <= result.eligible_amount <= order.refundable_balance


@settings(max_examples=500, deadline=None)
@given(orders(), reasons)
def test_eligible_exactly_when_amount_is_positive(order, reason):
    result = evaluate_eligibility(order, reason, POLICY, TODAY)
    assert result.eligible == (result.eligible_amount > 0)


@settings(max_examples=300, deadline=None)
@given(orders(), reasons)
def test_amount_has_cents_precision(order, reason):
    amount = evaluate_eligibility(order, reason, POLICY, TODAY).eligible_amount
    assert amount == amount.quantize(Decimal("0.01"))


@settings(max_examples=300, deadline=None)
@given(orders())
def test_changed_mind_never_pays_more_than_damaged(order):
    mind = evaluate_eligibility(order, RefundReason.CHANGED_MIND, POLICY, TODAY)
    damaged = evaluate_eligibility(order, RefundReason.DAMAGED, POLICY, TODAY)
    if mind.eligible:
        assert damaged.eligible and mind.eligible_amount <= damaged.eligible_amount


@settings(max_examples=300, deadline=None)
@given(orders(), reasons, st.integers(1, 400))
def test_waiting_longer_never_makes_an_order_eligible(order, reason, extra_days):
    """Except 'not delivered', which has a minimum wait, time only closes windows."""
    if reason == RefundReason.NOT_DELIVERED:
        return
    now = evaluate_eligibility(order, reason, POLICY, TODAY)
    later = evaluate_eligibility(order, reason, POLICY, TODAY + timedelta(days=extra_days))
    assert now.eligible or not later.eligible
