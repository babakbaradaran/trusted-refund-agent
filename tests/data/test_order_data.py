"""Raw shop export -> clean domain records. Bad rows must be quarantined, never half-loaded."""
from decimal import Decimal

import pytest

from refund_agent.config import ROOT
from refund_agent.data_access.repository import (
    ShopRepository,
    mask_email,
    normalize_order_row,
    parse_bool,
    parse_money,
)


@pytest.fixture(scope="module")
def repo():
    return ShopRepository(ROOT / "data")


@pytest.mark.parametrize("raw,expected", [
    ("$42.00", Decimal("42.00")),
    ('$1,249.00', Decimal("1249.00")),
    (" 58.00 ", Decimal("58.00")),
    ("310", Decimal("310.00")),
    ("-$12.00", Decimal("-12.00")),
])
def test_parse_money(raw, expected):
    assert parse_money(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "$1.2.3"])
def test_parse_money_rejects_garbage(raw):
    with pytest.raises(ValueError):
        parse_money(raw)


@pytest.mark.parametrize("raw,expected", [("yes", True), ("NO", False), (" y ", True), ("", False)])
def test_parse_bool(raw, expected):
    assert parse_bool(raw) is expected


def test_parse_bool_rejects_unknown():
    with pytest.raises(ValueError):
        parse_bool("maybe")


def test_valid_rows_load(repo):
    assert sorted(repo.orders) == [f"{n}" for n in range(1001, 1011)]


def test_messy_row_is_normalized(repo):
    order = repo.get_order("1007")  # amount " 58.00 ", currency "usd", final_sale "NO"
    assert order.amount == Decimal("58.00")
    assert order.currency == "USD"
    assert order.final_sale is False


def test_thousands_separator_and_mixed_case_status(repo):
    order = repo.get_order("1002")
    assert order.amount == Decimal("1249.00")
    assert order.status == "delivered"


@pytest.mark.parametrize("order_id,expected_error", [
    ("1011", "customer_id"),
    ("1012", "delivered_date is before order_date"),
    ("1013", "amount"),
    ("1014", "Invalid isoformat"),
    ("1015", "status"),
])
def test_bad_rows_are_quarantined_with_reason(repo, order_id, expected_error):
    assert repo.get_order(order_id) is None
    issue = next(i for i in repo.issues if i.record_id == order_id)
    assert expected_error in issue.error


def test_every_source_row_is_accounted_for(repo):
    with open(ROOT / "data" / "orders.csv", encoding="utf-8") as fh:
        source_rows = sum(1 for _ in fh) - 1
    assert len(repo.orders) + len([i for i in repo.issues if i.source == "orders.csv"]) == source_rows


def test_every_order_points_to_a_known_customer(repo):
    assert all(o.customer_id in repo.customers for o in repo.orders.values())


def test_policy_loads(repo):
    assert repo.policy.auto_approve_limit == Decimal("50.00")
    assert set(repo.policy.reasons) == {"damaged", "wrong_item", "not_delivered", "changed_mind"}


def test_mask_email():
    assert mask_email("maya.chen@example.com") == "m***@example.com"


def test_normalize_keeps_missing_delivery_date_as_none():
    row = {"order_id": "2000", "customer_id": "C-01", "order_date": "2026-10-01", "delivered_date": "",
           "status": "SHIPPED", "item_name": " Lamp ", "category": "Home", "amount": "$10",
           "currency": "usd", "final_sale": "no", "refunded_amount": ""}
    out = normalize_order_row(row)
    assert out["delivered_date"] is None
    assert out["status"] == "shipped" and out["item_name"] == "Lamp" and out["category"] == "home"
    assert out["refunded_amount"] == Decimal("0.00")
