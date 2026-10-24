"""Checks that run in code, not in the prompt: before the model sees a request, and after it answers."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from ..domain.models import Decision, Proposal

INJECTION_PATTERNS = [
    r"ignore (all |any |the |your )?(previous |prior |above )?(instructions|rules|policy|policies)",
    r"(reveal|show|print|repeat).{0,30}(system prompt|instructions|your rules)",
    r"you are now|developer mode|jailbreak|act as (an? )?(admin|administrator|supervisor)",
    r"(approve|issue|send).{0,40}refund.{0,40}(without|no) (approval|checks?|review)",
]
DATA_GRAB_PATTERNS = [
    r"(list|give|show|send).{0,30}(all|every|other) (customers?|orders|users)",
    r"(email|address|phone|card) (address |number )?(of|for) (another|other|all|every)",
    r"credit card|card number|cvv|password",
]
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
MONEY_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
ORDER_MENTION_RE = re.compile(r"(?:order|#)\s*#?\s*(\d{4,6})\b", re.I)
PROMISE_RE = re.compile(r"\b(already|have|has been|was)\s+(refunded|processed|sent|issued)\b", re.I)


@dataclass
class GuardResult:
    blocked: bool
    category: str = ""
    matched: str = ""


def check_request(message: str) -> GuardResult:
    text = message.lower()
    for category, patterns in (("prompt_injection", INJECTION_PATTERNS), ("data_exfiltration", DATA_GRAB_PATTERNS)):
        for pattern in patterns:
            m = re.search(pattern, text)
            if m:
                return GuardResult(True, category, m.group(0))
    return GuardResult(False)


def known_amounts(facts: dict[str, Any]) -> set[Decimal]:
    amounts: set[Decimal] = set()
    for order in facts.get("orders", {}).values():
        amounts.update({Decimal(order["amount"]), Decimal(order["refunded_amount"])})
    for elig in facts.get("eligibility", {}).values():
        amounts.add(Decimal(elig["eligible_amount"]))
    return amounts


def check_reply(reply: str, facts: dict[str, Any]) -> list[str]:
    """Return problems with the customer reply. Empty list means it is safe to send."""
    problems = []
    if any("*" not in e for e in EMAIL_RE.findall(reply)):
        problems.append("reply contains an email address")
    amounts = known_amounts(facts)
    for raw in MONEY_RE.findall(reply):
        try:
            value = Decimal(raw.replace(",", ""))
        except InvalidOperation:
            continue
        if value not in amounts:
            problems.append(f"amount ${raw} is not in any tool result")
    seen_orders = set(facts.get("orders", {})) | set(facts.get("missing_orders", []))
    for oid in ORDER_MENTION_RE.findall(reply):
        if oid not in seen_orders:
            problems.append(f"order {oid} was never looked up")
    if PROMISE_RE.search(reply):
        problems.append("reply claims money was already sent")
    return problems


@dataclass
class Verdict:
    decision: Decision
    problems: list[str] = field(default_factory=list)

    @property
    def overridden(self) -> bool:
        return bool(self.problems)


def check_proposal(p: Proposal, facts: dict[str, Any]) -> Verdict:
    """The model proposes; this decides whether the proposal is backed by tool results."""
    problems = check_reply(p.customer_reply, facts)
    errors = {e["code"] for e in facts.get("errors", [])}

    if "STALE_DATA" in errors and p.decision != Decision.ESCALATE:
        problems.append("data is stale; only escalation is allowed")
    if p.decision == Decision.REFUND:
        elig = facts.get("eligibility", {}).get(p.order_id or "")
        if elig is None:
            problems.append("refund proposed without an eligibility check")
        elif not elig["eligible"]:
            problems.append("refund proposed but policy says not eligible")
        elif p.amount <= 0 or p.amount > Decimal(elig["eligible_amount"]):
            problems.append(f"refund {p.amount} does not match eligible amount {elig['eligible_amount']}")
    elif p.decision == Decision.DENY:
        elig = facts.get("eligibility", {}).get(p.order_id or "")
        if elig is None or elig["eligible"]:
            problems.append("denial is not backed by an eligibility check")
    elif p.decision == Decision.NEEDS_INFO:
        if p.order_id and p.order_id in facts.get("orders", {}) and p.reason:
            problems.append("asked for more info although order and reason were known")

    return Verdict(Decision.ESCALATE if problems else p.decision, problems)
