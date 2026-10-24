"""Claude adapter: one Messages API call per agent step. The orchestrator owns the loop and the state."""
from __future__ import annotations

from typing import Any

import anthropic

from .llm import LLMError, ModelTurn, ToolCall

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeLLM:
    def __init__(self, model: str = "claude-opus-5-5", client: anthropic.Anthropic | None = None,
                 effort: str = "low", max_tokens: int = 8000, timeout_s: float = 60.0):
        self.name = model
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.client = client or anthropic.Anthropic(timeout=timeout_s, max_retries=2)

    def next_turn(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                tools=[{**t, "strict": True} for t in tools],
                messages=messages,
                output_config={"effort": self.effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",  # if this model declines, the API retries on a suitable fallback model
            )
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            raise LLMError("MODEL_UNAVAILABLE", str(exc)) from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("MODEL_RATE_LIMITED", exc.message) from exc
        except anthropic.APIStatusError as exc:
            code = "MODEL_UNAVAILABLE" if exc.status_code >= 500 else "MODEL_REQUEST_REJECTED"
            raise LLMError(code, exc.message) from exc

        if resp.stop_reason == "refusal":
            raise LLMError("MODEL_REFUSED", "Model declined the request")
        if resp.stop_reason == "max_tokens":
            raise LLMError("MODEL_OUTPUT_TRUNCATED", "Model hit max_tokens before finishing")

        content = _echo_safe([b.model_dump(mode="json", exclude_none=True) for b in resp.content])
        calls = [ToolCall(b["id"], b["name"], b["input"]) for b in content if b["type"] == "tool_use"]
        text = "".join(b.get("text", "") for b in content if b["type"] == "text")
        usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
        return ModelTurn(tool_calls=calls, text=text, assistant_content=content,
                         stop_reason=resp.stop_reason or "", usage=usage)


def _echo_safe(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """After a mid-output fallback, blocks before the last `fallback` marker (other than text) must not be resent."""
    marks = [i for i, b in enumerate(content) if b["type"] == "fallback"]
    if not marks:
        return content
    cut = marks[-1]
    head = [b for b in content[:cut] if b["type"] == "text"]
    return head + content[cut + 1:]
