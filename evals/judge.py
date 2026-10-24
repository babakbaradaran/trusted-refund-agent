"""Optional model-graded check of the customer reply. Off by default; turn on with --judge.

Rule-based scorers cover facts and tools. A judge model covers what rules can't: whether the
reply actually answers the customer and sounds right. Its verdicts are noisy, so the runner
reports them separately and never gates CI on them alone.
"""
from __future__ import annotations

import os

from pydantic import BaseModel

JUDGE_PROMPT = """You grade a customer-support reply about a refund request.

Customer message:
{message}

Facts from the shop's systems (the only things the reply may state as fact):
{facts}

Decision the system made: {decision}

Reply to grade:
{reply}

Answer each question strictly. "addresses_request": does the reply respond to what the customer asked?
"consistent_with_decision": does it match the decision (no refund promised on a denial, etc.)?
"no_invented_facts": does every fact in it appear in the facts above? "tone_ok": polite, clear, not robotic?"""


class JudgeVerdict(BaseModel):
    addresses_request: bool
    consistent_with_decision: bool
    no_invented_facts: bool
    tone_ok: bool
    reason: str


class ClaudeJudge:
    def __init__(self, model: str | None = None):
        import anthropic

        self.model = model or os.environ.get("JUDGE_MODEL", "claude-opus-5-5")
        self.client = anthropic.Anthropic()

    def grade(self, message: str, facts: dict, decision: str, reply: str) -> JudgeVerdict:
        resp = self.client.messages.parse(
            model=self.model,
            max_tokens=2000,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": JUDGE_PROMPT.format(
                message=message, facts=facts, decision=decision, reply=reply)}],
            output_format=JudgeVerdict,
        )
        return resp.parsed_output
