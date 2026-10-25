"""Real tools + real store + scripted model, end to end below the API."""
import pytest

from refund_agent.agent.orchestrator import REJECTED_REPLY, InvalidTransition
from refund_agent.agent.scripted_llm import ScriptedLLM
from refund_agent.domain.models import RunStatus


def test_large_refund_pauses_for_approval_then_pays(orch, payments):
    run = orch.start("Order 1002 arrived with a cracked headband, it is broken.", "agent")
    assert run.status == RunStatus.AWAITING_APPROVAL
    assert str(run.proposal.amount) == "1249.00" and payments.payouts == []

    done = orch.approve(run.run_id, by="supervisor", note="photos checked")
    assert done.status == RunStatus.COMPLETED and done.refund_id.startswith("rf_")
    assert "$1,249.00" in done.final_reply and len(payments.payouts) == 1


def test_state_survives_reload_from_store(orch):
    run = orch.start("Order 1002 is broken", "agent")
    reloaded = orch.store.get(run.run_id)
    assert reloaded.status == RunStatus.AWAITING_APPROVAL
    assert reloaded.facts == run.facts and len(reloaded.transcript) == len(run.transcript)


def test_reject_moves_no_money(orch, payments):
    run = orch.start("Order 1002 is broken", "agent")
    out = orch.reject(run.run_id, by="supervisor", note="customer abusing returns")
    assert out.status == RunStatus.REJECTED and payments.payouts == []
    # the customer still gets an answer, and the internal note is not in it
    assert out.final_reply == REJECTED_REPLY and "abusing" not in out.final_reply


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


# injected failures

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


# found in code review

def test_same_order_cannot_be_refunded_twice_across_runs(orch, payments):
    first = orch.start("Order 1001 arrived broken", "agent")
    second = orch.start("Order 1001 arrived broken", "agent")
    assert first.status == RunStatus.COMPLETED and first.refund_id
    assert second.proposal.decision == "deny" and "already been fully refunded" in second.final_reply
    assert len(payments.payouts) == 1


def test_second_approval_for_same_order_is_stopped_at_payout(orch, payments):
    a = orch.start("Order 1002 is broken", "agent")
    b = orch.start("Order 1002 is broken", "agent")  # both proposed before either was paid
    orch.approve(a.run_id, by="supervisor")
    out = orch.approve(b.run_id, by="supervisor")
    assert out.status == RunStatus.ESCALATED and "order changed since the proposal" in out.error
    assert len(payments.payouts) == 1


def test_payment_timeout_with_retry_reconciles_through_idempotency(orch, payments):
    run = orch.start("Order 1002 is broken", "agent", faults=["payments_timeout"])
    out = orch.approve(run.run_id, by="supervisor")
    call = next(e for e in out.events if e.name == "issue_refund")
    assert call.data["attempts"] == 2 and call.data["output"]["duplicate"] is True
    assert out.status == RunStatus.COMPLETED and len(payments.payouts) == 1


def test_payment_timeout_without_retry_is_marked_unknown(repo, payments):
    from refund_agent.agent.orchestrator import Orchestrator
    from refund_agent.config import Settings
    from refund_agent.state.store import RunStore

    orch = Orchestrator(repo, RunStore(), payments, Settings(tool_timeout_s=0.2, tool_retries=0), ScriptedLLM)
    run = orch.start("Order 1002 is broken", "agent", faults=["payments_timeout"])
    out = orch.approve(run.run_id, by="supervisor")
    assert out.status == RunStatus.AWAITING_APPROVAL and "status unknown" in out.error
    assert len(payments.payouts) == 1  # the provider did pay
    with pytest.raises(InvalidTransition, match="may have gone through"):
        orch.reject(run.run_id, by="supervisor", note="changed my mind")
    done = orch.approve(run.run_id, by="supervisor")  # same key: finds the existing payout
    assert done.status == RunStatus.COMPLETED and len(payments.payouts) == 1


# concurrency

def _race(fn, args_list):
    import threading
    barrier = threading.Barrier(len(args_list))
    results = []

    def worker(args):
        barrier.wait()
        try:
            results.append(fn(*args).status)
        except InvalidTransition:
            results.append("conflict")

    threads = [threading.Thread(target=worker, args=(a,)) for a in args_list]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_two_supervisors_approving_at_once_pay_once(orch, payments):
    run = orch.start("Order 1002 is broken", "agent")
    results = _race(orch.approve, [(run.run_id, "sup-a"), (run.run_id, "sup-b")])
    assert sorted(map(str, results)) == ["completed", "conflict"]
    assert len(payments.payouts) == 1


def test_two_runs_for_one_order_approved_at_once_pay_once(orch, payments):
    a = orch.start("Order 1002 is broken", "agent")
    b = orch.start("Order 1002 is broken", "agent")
    results = _race(orch.approve, [(a.run_id, "sup-a"), (b.run_id, "sup-b")])
    assert sorted(map(str, results)) == ["completed", "escalated"]
    assert len(payments.payouts) == 1


# found in the second review

def _orch(repo, payments, retries):
    from refund_agent.agent.orchestrator import Orchestrator
    from refund_agent.config import Settings
    from refund_agent.state.store import RunStore
    return Orchestrator(repo, RunStore(), payments, Settings(tool_timeout_s=0.2, tool_retries=retries), ScriptedLLM)


def test_timeout_then_other_run_pays_then_retry_is_refused(repo, payments, monkeypatch):
    """Run A's payment times out without reaching the provider; run B pays; A's retry must not pay too."""
    orch = _orch(repo, payments, retries=0)
    a = orch.start("Order 1002 is broken", "agent")
    b = orch.start("Order 1002 is broken", "agent")

    real_refund = payments.refund

    def lost_request(*args, **kwargs):  # provider never receives A's first request
        import time
        time.sleep(0.5)
        raise RuntimeError("connection reset")

    monkeypatch.setattr(payments, "refund", lost_request)
    assert orch.approve(a.run_id, by="sup").facts["payment_unknown"] is True
    monkeypatch.setattr(payments, "refund", real_refund)

    assert orch.approve(b.run_id, by="sup").status == RunStatus.COMPLETED
    retry = orch.approve(a.run_id, by="sup")
    assert retry.status == RunStatus.ESCALATED and "payment service refused" in retry.error
    assert len(payments.payouts) == 1


def test_timeout_followed_by_503_is_still_unknown(repo, payments, monkeypatch):
    orch = _orch(repo, payments, retries=1)
    run = orch.start("Order 1002 is broken", "agent")
    real_refund, calls = payments.refund, []

    def paid_late_then_503(*args, **kwargs):
        import time
        calls.append(1)
        if len(calls) == 1:
            receipt = real_refund(*args, **kwargs)  # money moves...
            time.sleep(0.5)                          # ...but the answer is too late
            return receipt
        from refund_agent.tools.registry import ToolError
        raise ToolError("DEPENDENCY_UNAVAILABLE", "503", retryable=True)

    monkeypatch.setattr(payments, "refund", paid_late_then_503)
    out = orch.approve(run.run_id, by="sup")
    assert out.status == RunStatus.AWAITING_APPROVAL and "status unknown" in out.error
    with pytest.raises(InvalidTransition):
        orch.reject(run.run_id, by="sup", note="no")
    monkeypatch.setattr(payments, "refund", real_refund)
    done = orch.approve(run.run_id, by="sup")
    assert done.status == RunStatus.COMPLETED and len(payments.payouts) == 1


def test_auto_approved_refunds_racing_for_one_order_pay_once(repo, payments):
    import threading
    orch = _orch(repo, payments, retries=1)
    barrier = threading.Barrier(4)

    def go():
        barrier.wait()
        orch.start("Order 1001 arrived broken", "agent")  # $42, auto-approved

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(payments.payouts) == 1
