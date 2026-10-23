"""Loads raw shop exports, cleans them, and keeps bad rows aside as data-quality issues."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..domain.models import Customer, Order, RefundPolicy

TRUE_WORDS = {"yes", "y", "true", "1"}
FALSE_WORDS = {"no", "n", "false", "0", ""}


@dataclass(frozen=True)
class DataQualityIssue:
    source: str
    row: int
    record_id: str
    error: str


def parse_money(raw: str) -> Decimal:
    text = raw.strip().replace("$", "").replace(",", "")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"not a money value: {raw!r}") from exc
    return value.quantize(Decimal("0.01"))


def parse_bool(raw: str) -> bool:
    word = raw.strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise ValueError(f"not a yes/no value: {raw!r}")


def parse_date(raw: str) -> date | None:
    text = raw.strip()
    return date.fromisoformat(text) if text else None


def normalize_order_row(row: dict[str, str]) -> dict[str, Any]:
    """Raw CSV strings -> typed values. Raises ValueError on unreadable fields."""
    return {
        "order_id": row["order_id"].strip(),
        "customer_id": row["customer_id"].strip(),
        "order_date": parse_date(row["order_date"]),
        "delivered_date": parse_date(row["delivered_date"]),
        "status": row["status"].strip().lower(),
        "item_name": row["item_name"].strip(),
        "category": row["category"].strip().lower(),
        "amount": parse_money(row["amount"]),
        "currency": row["currency"].strip().upper(),
        "final_sale": parse_bool(row["final_sale"]),
        "refunded_amount": parse_money(row["refunded_amount"] or "0"),
    }


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[0]}***@{domain}"


class ShopRepository:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.orders: dict[str, Order] = {}
        self.customers: dict[str, Customer] = {}
        self.issues: list[DataQualityIssue] = []
        self.exported_at: datetime
        self.policy: RefundPolicy
        self._load()

    def _load(self) -> None:
        with open(self.data_dir / "orders.csv", newline="", encoding="utf-8") as fh:
            for line_no, row in enumerate(csv.DictReader(fh), start=2):
                record_id = (row.get("order_id") or "").strip()
                try:
                    order = Order.model_validate(normalize_order_row(row))
                except (ValueError, ValidationError) as exc:
                    self.issues.append(DataQualityIssue("orders.csv", line_no, record_id, _short(exc)))
                    continue
                self.orders[order.order_id] = order

        for i, raw in enumerate(json.loads((self.data_dir / "customers.json").read_text("utf-8"))):
            try:
                cust = Customer.model_validate(raw)
            except ValidationError as exc:
                self.issues.append(DataQualityIssue("customers.json", i, str(raw.get("customer_id")), _short(exc)))
                continue
            self.customers[cust.customer_id] = cust

        for order in list(self.orders.values()):
            if order.customer_id not in self.customers:
                self.issues.append(DataQualityIssue("orders.csv", 0, order.order_id, "unknown customer_id"))
                del self.orders[order.order_id]

        meta = json.loads((self.data_dir / "orders_meta.json").read_text("utf-8"))
        self.exported_at = datetime.fromisoformat(meta["exported_at"].replace("Z", "+00:00"))
        self.policy = RefundPolicy.model_validate_json((self.data_dir / "refund_policy.json").read_text("utf-8"))

    def get_order(self, order_id: str) -> Order | None:
        return self.orders.get(order_id)

    def get_customer(self, customer_id: str) -> Customer | None:
        return self.customers.get(customer_id)

    def data_age(self, now: datetime) -> timedelta:
        return now - self.exported_at

    def is_stale(self, now: datetime) -> bool:
        return self.data_age(now) > timedelta(hours=self.policy.max_data_age_hours)


def _short(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'row'}: {e['msg']}" for e in exc.errors())
    return str(exc)
