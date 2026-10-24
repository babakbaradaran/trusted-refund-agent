"""Real tools + real store + scripted model, end to end below the API."""
import pytest

from refund_agent.agent.orchestrator import InvalidTransition
from refund_agent.agent.scripted_llm import ScriptedLLM
from refund_agent.domain.models import RunStatus


def test_large_refund_pauses_for_approval_then_pays(orch, payments):
    run = orch.start("Order 1002 arrived with a cracked headband, it is broken.", "agent")
    assert run.status == RunStatus.AWAITING_APPROVAL
    assert str(run.proposal.amount) == "1249.00" and payments.payouts == []

    done = orch.approve(run.run_id, by="supervisor", note="photos checked")
    assert done.status == RunStatus.COMPLETED and done.refund_id.startswith("rf_")
    assert "$1249.00" in done.final_reply and len(payments.payouts) == 1


def test_state_survives_reload_from_store(orch):
    run = orch.start("Order 1002 is broken", "agent")
    reloaded = orch.store.get(run.run_id)
    assert reloaded.status == RunStatus.AWAITING_APPROVAL
    assert reloaded.facts == run.facts and len(reloaded.transcript) == len(run.transcript)


def test_reject_moves_no_money(orch, payments):
    run = orch.start("Order 1002 is broken", "agent")
    out = orch.reject(run.run_id, by="supervisor", note="customer abusing returns")
    assert out.status == RunStatus.REJECTED and payments.payouts == [] and out.final_reply is None


def test_cannot_approve_twice(orch, payments):
    run = orch.start("Order 1002 is broken", "agent")
    orch.approve(run.run_id, by="supervisor")
    with pytest.raises(InvalidTransition):
        orch.approve(run.run_id, by="supervisor")
    assert len(payments.payouts) == 1


def test_supervisor_can_edit_the_reply(orch):
    run = orch.start("Order 1002 is broken", "agent")
    done = orch.approve(run.run_id, by="supervisor",
                        customer_reply="Hi Omar, your refund of $1249.00 for order 1002 is on its way.")
    assert done.final_reply.startswith("Hi Omar, your refund")


def test_edited_reply_is_still_checked(orch):
    run = orch.start("Order 1002 is broken", "agent")
    with pytest.raises(ValueError, match="not in any tool result"):
        orch.approve(run.run_id, by="supervisor", customer_reply="Refund of $5000.00 coming.")
    assert orch.store.get(run.run_id).status == RunStatus.AWAITING_APPROVAL


@pytest.mark.parametrize("message,status,decision", [
    ("Order 1001 arrived broken", RunStatus.COMPLETED, "refund"),           # $42 auto-approved
    ("Order 1003 blanket is damaged", RunStatus.COMPLETED, "deny"),          # outside window
    ("I changed my mind about order 1004", RunStatus.COMPLETED, "deny"),     # final sale
    ("Order 1005 never arrived", RunStatus.AWAITING_APPROVAL, "refund"),
    ("Order 1006 grinder is broken", RunStatus.COMPLETED, "deny"),           # already refunded
    ("Hi, I want a refund please", RunStatus.COMPLETED, "needs_info"),      # no order id
    ("Order 9999 is broken", RunStatus.COMPLETED, "needs_info"),            # unknown order
])
def test_decisions_across_the_dataset(orch, message, status, decision):
    run = orch.start(message, "agent")
    assert (run.status, run.proposal.decision) == (status, decision)


# ---- injected failures -------------------------------------------------------------------------

def test_stale_data_escalates(orch, payments):
    run = orch.start("Order 1001 arrived broken", "agent", faults=["stale_data"])
    assert run.status == RunStatus.ESCALATED and payments.payouts == []
    assert run.facts["errors"] == [{"tool": "lookup_order", "code": "STALE_DATA"}]


def test_lookup_timeout_is_retried_then_escalated(orch):
    run = orch.start("Order 1001 arrived broken", "agent", faults=["lookup_timeout"])
    call = next(e for e in run.events if e.name == "lookup_order")
    assert call.data["error_code"] == "TIMEOUT" and call.data["attempts"] == 2
    assert run.status == RunStatus.ESCALATED


def test_invalid_tool_response_is_not_trusted(orch, payments):
    run = orch.start("Order 1001 arrived broken", "agent", faults=["invalid_tool_response"])
    assert run.status == RunStatus.ESCALATED and payments.payouts == []


def test_model_down_fails_safely(orch, payments):
    run = orch.start("Order 1001 arrived broken", "agent", faults=["model_down"])
    assert run.status == RunStatus.FAILED and "MODEL_UNAVAILABLE" in run.error and payments.payouts == []


def test_malformed_model_output_recovers(orch):
    run = orch.start("Order 1001 arrived broken", "agent", faults=["malformed_model_output"])
    assert run.status == RunStatus.COMPLETED
    assert [e.name for e in run.events].count("invalid_resolution") == 1


def test_payments_down_keeps_run_retryable(orch, payments):
    run = orch.start("Order 1002 is broken", "agent", faults=["payments_down"])
    out = orch.approve(run.run_id, by="supervisor")
    assert out.status == RunStatus.AWAITING_APPROVAL and "NOT sent" in out.error and payments.payouts == []


def test_auto_approved_refund_falls_back_to_human_when_payments_down(orch, payments):
    run = orch.start("Order 1001 arrived broken", "agent", faults=["payments_down"])
    assert run.status == RunStatus.AWAITING_APPROVAL and payments.payouts == []


def test_sloppy_model_is_caught_by_guardrails(make_orchestrator, payments):
    run = make_orchestrator(lambda: ScriptedLLM("sloppy")).start("Order 1002 is broken", "agent")
    assert run.status == RunStatus.ESCALATED and payments.payouts == []
    assert "without an eligibility check" in run.error


def test_every_run_has_a_trace(orch):
    run = orch.start("Order 1002 is broken", "agent")
    kinds = [e.kind for e in run.events]
    assert kinds[0] == "guardrail" and "llm_call" in kinds and "tool_call" in kinds
    assert all(e.duration_ms is not None for e in run.events if e.kind in ("llm_call", "tool_call"))
