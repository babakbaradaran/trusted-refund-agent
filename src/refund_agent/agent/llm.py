"""The one interface the orchestrator uses to talk to any model (Claude, scripted, or a test double)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ModelTurn:
    tool_calls: list[ToolCall]
    text: str
    assistant_content: list[dict[str, Any]]  # appended to the transcript as-is
    stop_reason: str
    usage: dict[str, int] = field(default_factory=dict)


class LLMError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class LLM(Protocol):
    name: str

    def next_turn(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn: ...


def tool_use_block(call: ToolCall) -> dict[str, Any]:
    return {"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}
