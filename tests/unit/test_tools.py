import time
from decimal import Decimal

from pydantic import BaseModel

from refund_agent.tools.faults import Fault
from refund_agent.tools.refund_tools import PaymentsGateway, build_action_tools, build_agent_tools
from refund_agent.tools.registry import ToolError, ToolRegistry, ToolSpec
from tests.conftest import NOW


class In(BaseModel):
    x: int


class Out(BaseModel):
    y: int


def make_registry(func, timeout=0.2, retries=1):
    reg = ToolRegistry(timeout_s=timeout, retries=retries)
    reg.register(ToolSpec("t", "test tool", In, Out, func))
    return reg


def test_success_returns_validated_output():
    result = make_registry(lambda a: {"y": a.x * 2}).call("t", {"x": 4})
    assert result.ok and result.data == {"y": 8} and result.attempts == 1


def test_invalid_arguments_never_reach_the_tool():
    called = []
    result = make_registry(lambda a: called.append(a)).call("t", {"x": "four"})
    assert not result.ok and result.error_code == "INVALID_ARGUMENTS" and not called


def test_unknown_tool():
    assert make_registry(lambda a: {"y": 1}).call("nope", {}).error_code == "UNKNOWN_TOOL"


def test_wrong_output_shape_is_an_error_not_data():
    result = make_registry(lambda a: {"y": "lots"}).call("t", {"x": 1})
    assert result.error_code == "INVALID_TOOL_RESPONSE" and result.data is None


def test_crash_is_contained():
    result = make_registry(lambda a: 1 / 0).call("t", {"x": 1})
    assert result.error_code == "TOOL_CRASHED" and "ZeroDivisionError" in result.error_message


def test_timeout_is_retried_once_then_reported():
    result = make_registry(lambda a: time.sleep(0.5), timeout=0.05).call("t", {"x": 1})
    assert result.error_code == "TIMEOUT" and result.retryable and result.attempts == 2


def test_retryable_error_recovers_on_second_attempt():
    calls = []

    def flaky(a):
        calls.append(1)
        if len(calls) == 1:
            raise ToolError("DEPENDENCY_UNAVAILABLE", "blip", retryable=True)
        return {"y": 1}

    result = make_registry(flaky).call("t", {"x": 1})
    assert result.ok and result.attempts == 2


def test_non_retryable_error_is_not_retried():
    calls = []

    def missing(a):
        calls.append(1)
        raise ToolError("ORDER_NOT_FOUND", "no")

    assert make_registry(missing).call("t", {"x": 1}).error_code == "ORDER_NOT_FOUND" and len(calls) == 1


def test_schema_is_closed_for_strict_tool_use():
    schema = make_registry(lambda a: {"y": 1}).schemas()[0]
    assert schema["input_schema"]["additionalProperties"] is False
    assert schema["input_schema"]["required"] == ["x"]


# refund tools

def agent_tools(repo, faults=()):
    return build_agent_tools(repo, PaymentsGateway(), NOW, set(faults), timeout_s=0.2)


def test_lookup_order_hides_private_customer_data(repo):
    data = agent_tools(repo).call("lookup_order", {"order_id": "1001"}).data
    assert data["customer_first_name"] == "Maya"
    assert data["customer_email_masked"] == "m***@example.com"
    assert "maya.chen@example.com" not in str(data) and "customer_id" not in data
    assert data["amount"] == "42.00"


def test_lookup_unknown_order(repo):
    assert agent_tools(repo).call("lookup_order", {"order_id": "9999"}).error_code == "ORDER_NOT_FOUND"


def test_lookup_rejects_malformed_order_id(repo):
    assert agent_tools(repo).call("lookup_order", {"order_id": "1001; DROP"}).error_code == "INVALID_ARGUMENTS"


def test_stale_data_fault(repo):
    result = agent_tools(repo, [Fault.STALE_DATA]).call("lookup_order", {"order_id": "1001"})
    assert result.error_code == "STALE_DATA" and not result.retryable


def test_real_staleness_check_uses_export_time(repo):
    from datetime import timedelta
    tools = build_agent_tools(repo, PaymentsGateway(), NOW + timedelta(days=2), set(), timeout_s=0.2)
    assert tools.call("lookup_order", {"order_id": "1001"}).error_code == "STALE_DATA"


def test_lookup_timeout_fault(repo):
    result = agent_tools(repo, [Fault.LOOKUP_TIMEOUT]).call("lookup_order", {"order_id": "1001"})
    assert result.error_code == "TIMEOUT" and result.attempts == 2


def test_invalid_tool_response_fault_is_caught(repo):
    result = agent_tools(repo, [Fault.INVALID_TOOL_RESPONSE]).call(
        "check_refund_eligibility", {"order_id": "1001", "reason": "damaged"})
    assert result.error_code == "INVALID_TOOL_RESPONSE"


def test_eligibility_tool(repo):
    data = agent_tools(repo).call("check_refund_eligibility", {"order_id": "1007", "reason": "changed_mind"}).data
    assert data["eligible"] is True and data["eligible_amount"] == "52.20"


def test_eligibility_rejects_unknown_reason(repo):
    result = agent_tools(repo).call("check_refund_eligibility", {"order_id": "1001", "reason": "bored"})
    assert result.error_code == "INVALID_ARGUMENTS"


def test_issue_refund_is_idempotent():
    gw = PaymentsGateway()
    tools = build_action_tools(gw, set(), timeout_s=0.2)
    args = {"order_id": "1002", "amount": "1249.00", "idempotency_key": "run_abc123"}
    first, second = tools.call("issue_refund", args), tools.call("issue_refund", args)
    assert first.data["refund_id"] == second.data["refund_id"] and second.data["duplicate"] is True
    assert len(gw.payouts) == 1 and gw.payouts[0].amount == Decimal("1249.00")


def test_payments_down_moves_no_money():
    gw = PaymentsGateway()
    result = build_action_tools(gw, {Fault.PAYMENTS_DOWN}, timeout_s=0.2).call(
        "issue_refund", {"order_id": "1002", "amount": "10.00", "idempotency_key": "run_abc123"})
    assert result.error_code == "DEPENDENCY_UNAVAILABLE" and result.attempts == 2 and gw.payouts == []


def test_eligibility_tool_also_refuses_stale_data(repo):
    result = agent_tools(repo, [Fault.STALE_DATA]).call(
        "check_refund_eligibility", {"order_id": "1001", "reason": "damaged"})
    assert result.error_code == "STALE_DATA"


def test_eligibility_counts_refunds_we_already_paid(repo):
    gw = PaymentsGateway()
    gw.refund("1008", Decimal("100.00"), "run_earlier1")
    tools = build_agent_tools(repo, gw, NOW, set(), timeout_s=0.2)
    data = tools.call("check_refund_eligibility", {"order_id": "1008", "reason": "wrong_item"}).data
    assert data["eligible_amount"] == "299.99"  # 499.99 - 100 in the export - 100 paid by us
