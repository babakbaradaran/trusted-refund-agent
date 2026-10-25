"""Claude adapter against a fake client: request shape, response parsing and error mapping.

No API key is needed. The live model is covered by the nightly eval instead.
"""
import anthropic
import httpx2
import pytest
from anthropic.types.beta import BetaMessage

from refund_agent.agent.claude_llm import ClaudeLLM, _echo_safe
from refund_agent.agent.llm import LLMError

REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def message(content, stop_reason="tool_use"):
    return BetaMessage.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 120, "output_tokens": 30},
    })


class FakeClient:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.beta = self
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


TOOLS = [{"name": "lookup_order", "description": "x",
          "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}}]


def test_request_shape():
    client = FakeClient(message([{"type": "text", "text": "done"}], "end_turn"))
    ClaudeLLM(client=client).next_turn("sys", [{"role": "user", "content": "hi"}], TOOLS)
    sent = client.calls[0]
    assert sent["model"] == "claude-opus-5-5" and sent["system"] == "sys"
    assert sent["tools"][0]["strict"] is True
    assert sent["output_config"] == {"effort": "low"}
    assert sent["fallbacks"] == "default" and sent["betas"] == ["server-side-fallback-2026-07-01"]
    assert "thinking" not in sent
    assert sent["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}


def test_tool_use_is_parsed_and_content_kept_for_the_transcript():
    content = [
        {"type": "thinking", "thinking": "", "signature": "sig123"},
        {"type": "text", "text": "Looking it up."},
        {"type": "tool_use", "id": "toolu_1", "name": "lookup_order", "input": {"order_id": "1002"}},
    ]
    turn = ClaudeLLM(client=FakeClient(message(content))).next_turn("s", [], TOOLS)
    assert [(c.id, c.name, c.input) for c in turn.tool_calls] == [("toolu_1", "lookup_order", {"order_id": "1002"})]
    assert turn.text == "Looking it up."
    assert [b["type"] for b in turn.assistant_content] == ["thinking", "text", "tool_use"]
    assert turn.assistant_content[0]["signature"] == "sig123"  # thinking blocks go back unchanged
    assert turn.usage == {"input_tokens": 120, "output_tokens": 30}


@pytest.mark.parametrize("error,code", [
    (anthropic.APIConnectionError(request=REQ), "MODEL_UNAVAILABLE"),
    (anthropic.APITimeoutError(request=REQ), "MODEL_UNAVAILABLE"),
    (anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=REQ), body=None),
     "MODEL_RATE_LIMITED"),
    (anthropic.InternalServerError("oops", response=httpx2.Response(529, request=REQ), body=None),
     "MODEL_UNAVAILABLE"),
    (anthropic.BadRequestError("bad", response=httpx2.Response(400, request=REQ), body=None),
     "MODEL_REQUEST_REJECTED"),
])
def test_sdk_errors_map_to_llm_errors(error, code):
    with pytest.raises(LLMError) as exc:
        ClaudeLLM(client=FakeClient(error)).next_turn("s", [], TOOLS)
    assert exc.value.code == code


@pytest.mark.parametrize("stop_reason,code", [("refusal", "MODEL_REFUSED"), ("max_tokens", "MODEL_OUTPUT_TRUNCATED")])
def test_bad_stop_reasons(stop_reason, code):
    client = FakeClient(message([{"type": "text", "text": "partial"}], stop_reason))
    with pytest.raises(LLMError) as exc:
        ClaudeLLM(client=client).next_turn("s", [], TOOLS)
    assert exc.value.code == code


def test_echo_safe_drops_blocks_before_a_mid_output_fallback():
    content = [
        {"type": "thinking", "thinking": "", "signature": "a"},
        {"type": "text", "text": "Let me"},
        {"type": "tool_use", "id": "t0", "name": "lookup_order", "input": {}},
        {"type": "fallback"},
        {"type": "tool_use", "id": "t1", "name": "lookup_order", "input": {"order_id": "1"}},
    ]
    assert [b.get("id") or b["type"] for b in _echo_safe(content)] == ["text", "t1"]


def test_echo_safe_leaves_normal_content_alone():
    content = [{"type": "text", "text": "hi"}]
    assert _echo_safe(content) is content


def test_full_agent_loop_with_fake_claude(make_orchestrator):
    """The orchestrator drives the Claude adapter end to end with canned API responses."""
    replies = iter([
        message([{"type": "tool_use", "id": "t1", "name": "lookup_order", "input": {"order_id": "1001"}}]),
        message([{"type": "tool_use", "id": "t2", "name": "check_refund_eligibility",
                  "input": {"order_id": "1001", "reason": "damaged"}}]),
        message([{"type": "tool_use", "id": "t3", "name": "submit_resolution", "input": {
            "decision": "refund", "order_id": "1001", "amount": "42.00", "reason": "damaged",
            "customer_reply": "Hi Maya, you qualify for a refund of $42.00 for order 1001.",
            "rationale": "Damaged within 30 days."}}]),
    ])

    class SequenceClient(FakeClient):
        def create(self, **kwargs):
            self.calls.append(kwargs)
            return next(replies)

    client = SequenceClient(None)
    run = make_orchestrator(lambda: ClaudeLLM(client=client)).start("Order 1001 arrived broken", "agent")
    assert run.status.value == "completed" and run.refund_id
    # The transcript list is shared, so by now it also holds the final assistant turn.
    roles = [m["role"] for m in client.calls[-1]["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant"]
    results = client.calls[-1]["messages"][4]["content"]
    assert results[0]["type"] == "tool_result" and results[0]["tool_use_id"] == "t2"


def test_other_sdk_errors_become_llm_errors():
    with pytest.raises(LLMError) as exc:
        ClaudeLLM(client=FakeClient(anthropic.APIError("weird", request=REQ, body=None))).next_turn("s", [], TOOLS)
    assert exc.value.code == "MODEL_ERROR"
