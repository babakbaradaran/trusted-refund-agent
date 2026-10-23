from datetime import date
from decimal import Decimal

import pytest

from refund_agent.config import ROOT
from refund_agent.data_access.repository import ShopRepository
from refund_agent.domain.policy import evaluate_eligibility, needs_human_approval

TODAY = date(2026, 10, 20)


@pytest.fixture(scope="module")
def repo():
    return ShopRepository(ROOT / "data")


def check(repo, order_id, reason):
    return evaluate_eligibility(repo.get_order(order_id), reason, repo.policy, TODAY)


@pytest.mark.parametrize("order_id,reason,eligible,amount,rule", [
    ("1001", "damaged", True, "42.00", "damaged_within_30d"),
    ("1002", "damaged", True, "1249.00", "damaged_within_30d"),
    ("1003", "damaged", False, "0.00", "window_expired"),          # delivered 75 days ago
    ("1004", "changed_mind", False, "0.00", "final_sale"),
    ("1004", "damaged", True, "24.99", "damaged_within_30d"),      # final sale but damaged is allowed
    ("1005", "not_delivered", True, "135.00", "not_delivered_within_60d"),
    ("1005", "damaged", False, "0.00", "not_delivered_yet"),
    ("1006", "damaged", False, "0.00", "already_refunded"),
    ("1007", "changed_mind", True, "52.20", "changed_mind_within_14d"),  # 90% of 58.00
    ("1008", "wrong_item", True, "399.99", "wrong_item_within_30d"),     # 499.99 minus 100 already refunded
    ("1009", "not_delivered", False, "0.00", "too_early"),
    ("1010", "not_delivered", False, "0.00", "marked_delivered"),
])
def test_eligibility_table(repo, order_id, reason, eligible, amount, rule):
    result = check(repo, order_id, reason)
    assert (result.eligible, result.eligible_amount, result.rule) == (eligible, Decimal(amount), rule)


def test_explanation_is_always_present(repo):
    for order_id in repo.orders:
        for reason in repo.policy.reasons:
            assert check(repo, order_id, reason).explanation


@pytest.mark.parametrize("amount,expected", [("50.00", False), ("50.01", True), ("0.00", False)])
def test_approval_threshold(repo, amount, expected):
    assert needs_human_approval(Decimal(amount), repo.policy) is expected
