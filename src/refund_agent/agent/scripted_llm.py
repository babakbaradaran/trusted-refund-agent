"""A rule-based stand-in for the model. Free, offline and repeatable, so tests and CI never need an API key.

It talks the same message format as Claude: it reads the conversation, then returns tool calls.
`variant="sloppy"` imitates a bad model (skips the policy check, invents a payout) so we can prove
the guardrails and the eval scorers catch it.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

from .llm import ModelTurn, ToolCall, tool_use_block

REASON_WORDS = [
    ("damaged", r"broken|damaged|cracked|smashed|defect|doesn'?t work|not working|stopped working"),
    ("wrong_item", r"wrong (item|size|colou?r|product)|not what i ordered|different item"),
    ("not_delivered", r"never (arrived|came|got)|not (arrived|delivered|received)|hasn'?t arrived|where is my"),
    ("changed_mind", r"changed my mind|don'?t (want|need) it|no longer (want|need)|return it"),
]
ORDER_RE = re.compile(r"(?:order|#)\s*(?:number|no\.?|id)?\s*#?\s*(\d{4,6})\b", re.I)
BARE_ID_RE = re.compile(r"\b(\d{4,6})\b")


def find_order_id(text: str) -> str | None:
    m = ORDER_RE.search(text) or BARE_ID_RE.search(text)
    return m.group(1) if m else None


def find_reason(text: str) -> str | None:
    for code, pattern in REASON_WORDS:
        if re.search(pattern, text, re.I):
            return code
    return None


class ScriptedLLM:
    def __init__(self, variant: str = "careful"):
        self.variant = variant
        self.name = f"scripted-{variant}"
        self._n = 0

    def next_turn(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        request = _first_user_text(messages)
        results = _tool_results(messages)
        call = self._decide(request, results)
        self._n += 1
        call.id = f"toolu_scripted_{self._n:03d}"
        return ModelTurn(tool_calls=[call], text="", assistant_content=[tool_use_block(call)],
                         stop_reason="tool_use")

    def _decide(self, request: str, results: dict[str, list[dict]]) -> ToolCall:
        order_id = find_order_id(request)
        reason = find_reason(request)
        if not order_id:
            return _submit("needs_info", "", "0.00", reason or "",
                           "Thanks for reaching out. Could you share your order number so I can look into this?",
                           "No order number in the message.")

        lookups = results.get("lookup_order", [])
        if not lookups:
            return ToolCall("", "lookup_order", {"order_id": order_id})
        order = lookups[-1]
        if order.get("error") == "ORDER_NOT_FOUND":
            return _submit("needs_info", order_id, "0.00", reason or "",
                           f"I couldn't find order {order_id}. Could you double-check the order number?",
                           "Order id not found.")
        if order.get("error"):
            return _submit("escalate", order_id, "0.00", reason or "",
                           "Thanks for your patience. I've passed your request to a colleague who will "
                           "follow up shortly.",
                           f"lookup_order failed with {order['error']}; not safe to decide.")

        name = order["customer_first_name"]
        if self.variant == "sloppy":
            return _submit("refund", order_id, order["amount"], reason or "damaged",
                           f"Hi {name}, good news: we've already refunded ${order['amount']} for order "
                           f"{order_id}, plus a $20 goodwill credit.", "Customer seems upset.")

        if not reason:
            return _submit("needs_info", order_id, "0.00", "",
                           f"Hi {name}, thanks for getting in touch about your {order['item_name']}. "
                           "Could you tell me what went wrong with it?", "Reason for refund unclear.")

        checks = results.get("check_refund_eligibility", [])
        if not checks:
            return ToolCall("", "check_refund_eligibility", {"order_id": order_id, "reason": reason})
        elig = checks[-1]
        if elig.get("error"):
            return _submit("escalate", order_id, "0.00", reason,
                           f"Hi {name}, thanks for your patience. A colleague will review your request "
                           f"for order {order_id} and get back to you.",
                           f"check_refund_eligibility failed with {elig['error']}.")
        if elig["eligible"]:
            amount = elig["eligible_amount"]
            return _submit("refund", order_id, amount, reason,
                           f"Hi {name}, I'm sorry about your {order['item_name']}. You qualify for a refund "
                           f"of ${Decimal(amount):,.2f} for order {order_id}. We'll confirm as soon as it "
                           "is processed.",
                           f"Eligible under rule {elig['rule']}.")
        return _submit("deny", order_id, "0.00", reason,
                       f"Hi {name}, thank you for contacting us about order {order_id}. I'm sorry, but we "
                       f"can't offer a refund here: {elig['explanation']}",
                       f"Not eligible under rule {elig['rule']}.")


def _submit(decision: str, order_id: str, amount: str, reason: str, reply: str, rationale: str) -> ToolCall:
    return ToolCall("", "submit_resolution", {
        "decision": decision, "order_id": order_id, "amount": amount, "reason": reason,
        "customer_reply": reply, "rationale": rationale})


def _first_user_text(messages: list[dict[str, Any]]) -> str:
    content = messages[0]["content"]
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content if b.get("type") == "text")


def _tool_results(messages: list[dict[str, Any]]) -> dict[str, list[dict]]:
    """Pair each tool_use with its tool_result, grouped by tool name, in order."""
    names: dict[str, str] = {}
    out: dict[str, list[dict]] = {}
    for msg in messages:
        if not isinstance(msg["content"], list):
            continue
        for block in msg["content"]:
            if block.get("type") == "tool_use":
                names[block["id"]] = block["name"]
            elif block.get("type") == "tool_result":
                name = names.get(block["tool_use_id"], "?")
                out.setdefault(name, []).append(json.loads(block["content"]))
    return out
