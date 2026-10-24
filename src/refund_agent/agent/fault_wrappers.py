"""Wraps any model to simulate model-side failures for tests and evals."""
from __future__ import annotations

from typing import Any

from ..tools.faults import Fault
from .llm import LLM, LLMError, ModelTurn


class FaultyLLM:
    def __init__(self, inner: LLM, faults: set[Fault]):
        self.inner = inner
        self.faults = faults
        self.name = inner.name
        self._corrupted = False

    def next_turn(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        if Fault.MODEL_DOWN in self.faults:
            raise LLMError("MODEL_UNAVAILABLE", "Injected fault: model API returned 529 overloaded")
        turn = self.inner.next_turn(system, messages, tools)
        if Fault.MALFORMED_MODEL_OUTPUT in self.faults and not self._corrupted:
            for call in turn.tool_calls:
                if call.name == "submit_resolution":
                    call.input = {**call.input, "amount": "lots", "decision": "refund-everything"}
                    for block in turn.assistant_content:
                        if block.get("id") == call.id:
                            block["input"] = call.input
                    self._corrupted = True
        return turn
