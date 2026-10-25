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
    r"(list|give|show|send|tell).{0,30}(all|every|other) (customers?|users|people|buyers)",
    r"(list|give|show|send|export).{0,15}(all|every) orders",
    r"(email|address|phone|card) (address |number )?(of|for) (another|other|all|every|someone)",
    r"(email|address|phone).{0,30}(buyer|owner|customer|person) (of|for|who)",
    r"(give|send|tell|show|what is|what's).{0,25}(card number|cvv|password)",
]
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
NUMBER = r"(\d[\d,]*(?:\.\d{1,2})?)(?!\d)"
MONEY_RE = re.compile(rf"(?:\$|\busd)\s*{NUMBER}|{NUMBER}\s?(?:dollars|usd)\b", re.I)
# "order 1002", "Order: 1002", "order ID #1002", "order-1002", "#1002"; not dates like 2026-10-01
ORDER_MENTION_RE = re.compile(r"(?:\borders?\b[\s:#-]*(?:number|no\.?|id)?[\s:#-]*|#)(\d{4,6})\b(?![-/]\d)", re.I)
PROMISE_RE = re.compile(r"(\b(already|have|has|had|was|were)|'ve)\s+(already\s+)?(been\s+)?"
                        r"(refunded|processed|sent|issued)\b", re.I)
# "...once the refund has been issued" is about the future, so it is not a promise.
FUTURE_LEAD_RE = re.compile(r"\b(once|when|after|as soon as|until)\b(\s+[^\s,.;:!?]+){0,5}\s*$", re.I)


SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)|\n")


def _sentence_around(text: str, i: int) -> str:
    starts = [m.end() for m in SENTENCE_END_RE.finditer(text, 0, i)]
    end = SENTENCE_END_RE.search(text, i)
    return text[starts[-1] if starts else 0:end.start() if end else len(text)]


def claims_money_sent(reply: str, past_refunds: set[Decimal] = frozenset()) -> bool:
    """`past_refunds`: earlier refunds on the order. "minus the $100.00 already refunded" is a fact, not a promise."""
    for m in PROMISE_RE.finditer(reply):
        if FUTURE_LEAD_RE.search(reply[:m.start()]):
            continue
        sentence = _sentence_around(reply, m.start())
        if past_refunds and {_to_decimal(a) for a in money_mentions(sentence)} & past_refunds:
            continue
        return True
    return False


def _to_decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None


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


def money_mentions(text: str) -> list[str]:
    return [a or b for a, b in MONEY_RE.findall(text)]


def check_reply(reply: str, facts: dict[str, Any], may_mention_past_refund: bool = False) -> list[str]:
    """Return problems with the customer reply. Empty list means it is safe to send.

    `may_mention_past_refund` is for denials ("this order was already refunded") and for replies
    a supervisor approves, which are sent only after the payment succeeds.
    """
    problems = []
    if any("*" not in e for e in EMAIL_RE.findall(reply)):
        problems.append("reply contains an email address")
    amounts = known_amounts(facts)
    for raw in money_mentions(reply):
        try:
            value = Decimal(raw.replace(",", ""))
        except InvalidOperation:
            continue
        if value not in amounts:
            problems.append(f"amount ${raw} is not in any tool result")
    # the customer's own order number is fine to repeat, even if the lookup failed
    seen_orders = (set(facts.get("orders", {})) | set(facts.get("missing_orders", []))
                   | set(facts.get("request_orders", [])))
    for oid in ORDER_MENTION_RE.findall(reply):
        if oid not in seen_orders:
            problems.append(f"order {oid} was never looked up")
    past = {Decimal(o["refunded_amount"]) for o in facts.get("orders", {}).values()} - {Decimal("0")}
    if not may_mention_past_refund and claims_money_sent(reply, past):
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
    problems = check_reply(p.customer_reply, facts, may_mention_past_refund=p.decision == Decision.DENY)
    errors = {e["code"] for e in facts.get("errors", [])}

    if "STALE_DATA" in errors and p.decision != Decision.ESCALATE:
        problems.append("data is stale; only escalation is allowed")
    if p.decision in (Decision.REFUND, Decision.DENY) and p.order_id not in facts.get("orders", {}):
        problems.append(f"{p.decision.value} for an order that was never looked up")
    if p.decision == Decision.REFUND:
        elig = facts.get("eligibility", {}).get(p.order_id or "")
        if elig is None:
            problems.append("refund proposed without an eligibility check")
        elif not elig["eligible"]:
            problems.append("refund proposed but policy says not eligible")
        elif p.reason is None or p.reason.value != elig["reason"]:
            problems.append(f"refund reason {p.reason} differs from the checked reason {elig['reason']}")
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
