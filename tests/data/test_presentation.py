"""What the API and page show must match the source file, formatted for people."""
import csv
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from refund_agent.api.main import create_app
from refund_agent.api.schemas import OrderPresentation, format_date, format_money
from refund_agent.config import ROOT, Settings
from refund_agent.data_access.repository import parse_money

AGENT = {"X-API-Key": "agent-key-123"}


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app(Settings()))


@pytest.mark.parametrize("amount,text", [
    ("42.00", "$42.00"), ("1249.00", "$1,249.00"), ("0.00", "$0.00"), ("1234567.50", "$1,234,567.50"),
])
def test_format_money(amount, text):
    assert format_money(Decimal(amount)) == text


def test_format_date():
    from datetime import date
    assert format_date(date(2026, 10, 3)) == "Oct 3, 2026"
    assert format_date(None) == "—"


def test_order_1002_is_presented_correctly(client):
    body = client.get("/api/orders/1002", headers=AGENT).json()
    assert body == {
        "order_id": "1002", "item": "Noise-Cancelling Headphones", "status": "Delivered",
        "amount": "$1,249.00", "refunded": "$0.00", "refundable_balance": "$1,249.00",
        "ordered_on": "Sep 28, 2026", "delivered_on": "Oct 3, 2026", "final_sale": "No",
        "customer": "Omar Haddad", "customer_email": "o***@example.com",
    }


def test_partial_refund_balance(client):
    body = client.get("/api/orders/1008", headers=AGENT).json()
    assert (body["amount"], body["refunded"], body["refundable_balance"]) == ("$499.99", "$100.00", "$399.99")


def test_not_yet_delivered_shows_dash(client):
    assert client.get("/api/orders/1005", headers=AGENT).json()["delivered_on"] == "—"


def test_quarantined_rows_are_not_served(client):
    assert client.get("/api/orders/1013", headers=AGENT).status_code == 404


def test_every_served_amount_matches_the_source_file(client, repo):
    with open(ROOT / "data" / "orders.csv", newline="", encoding="utf-8") as fh:
        source = {r["order_id"]: r for r in csv.DictReader(fh)}
    for order_id in repo.orders:
        shown = client.get(f"/api/orders/{order_id}", headers=AGENT).json()
        assert parse_money(shown["amount"]) == parse_money(source[order_id]["amount"]), order_id


def test_full_email_never_leaves_the_api(client, repo):
    for order_id in repo.orders:
        text = client.get(f"/api/orders/{order_id}", headers=AGENT).text
        assert not any(c.email in text for c in repo.customers.values())


def test_data_quality_report_lists_rejected_rows(client):
    issues = client.get("/api/data-quality", headers={"X-API-Key": "supervisor-key-456"}).json()["issues"]
    assert sorted(i["record_id"] for i in issues) == ["1011", "1012", "1013", "1014", "1015"]


def test_presentation_model_rejects_nothing_from_clean_data(repo):
    for order in repo.orders.values():
        OrderPresentation.of(order, repo.get_customer(order.customer_id))
