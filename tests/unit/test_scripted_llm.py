import pytest

from refund_agent.agent.scripted_llm import ScriptedLLM, find_order_id, find_reason


@pytest.mark.parametrize("text,expected", [
    ("order 1042 is broken", "1042"), ("Order #1002 never came", "1002"), ("order no. 1005", "1005"),
    ("my 1001 thing", "1001"), ("hello there", None),
])
def test_find_order_id(text, expected):
    assert find_order_id(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("it arrived cracked", "damaged"), ("there is a tear in it", "damaged"), ("you sent the wrong size", "wrong_item"),
    ("it never arrived", "not_delivered"), ("I changed my mind", "changed_mind"), ("hi", None),
])
def test_find_reason(text, expected):
    assert find_reason(text) == expected


def test_first_step_is_order_lookup():
    turn = ScriptedLLM().next_turn("", [{"role": "user", "content": "order 1001 is broken"}], [])
    assert turn.tool_calls[0].name == "lookup_order" and turn.tool_calls[0].input == {"order_id": "1001"}
    assert turn.assistant_content[0]["type"] == "tool_use"
