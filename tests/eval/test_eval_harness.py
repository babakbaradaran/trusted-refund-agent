"""The eval must pass for the real agent and must fail for a known-bad one (otherwise it measures nothing)."""
import json

import pytest

from evals.run_eval import EVAL_DIR, compare, evaluate, load_cases
from evals.scorers import groundedness, tool_accuracy

pytestmark = pytest.mark.eval


@pytest.fixture(scope="module")
def careful():
    return evaluate("scripted", repeats=2)


@pytest.fixture(scope="module")
def sloppy():
    return evaluate("scripted-sloppy")


def test_dataset_covers_every_category_and_fault():
    cases = load_cases()
    assert {c["category"] for c in cases} == {"happy_path", "policy_denial", "needs_info", "safety", "failure"}
    faults = {f for c in cases for f in c.get("faults", [])}
    assert faults == {"stale_data", "lookup_timeout", "invalid_tool_response", "model_down",
                      "malformed_model_output", "payments_down"}
    assert len({c["id"] for c in cases}) == len(cases)


def test_agent_meets_every_gate(careful):
    failed = {k: v for k, v in careful["criteria"].items() if not v["meets_gate"]}
    assert careful["gates_ok"], failed


def test_agent_is_consistent_across_repeats(careful):
    assert careful["consistency_rate"] == 1.0


def test_no_regression_against_baseline(careful):
    baseline = json.loads((EVAL_DIR / "baseline.json").read_text())
    assert compare(careful, baseline) == []


def test_bad_model_is_caught(sloppy):
    assert not sloppy["gates_ok"]
    assert sloppy["criteria"]["groundedness"]["rate"] < 0.5
    assert sloppy["criteria"]["tool_accuracy"]["rate"] < 0.5


def test_bad_model_shows_up_as_regression(sloppy):
    baseline = json.loads((EVAL_DIR / "baseline.json").read_text())
    assert {r.split(":")[0] for r in compare(sloppy, baseline)} >= {"groundedness", "tool_accuracy"}


def test_groundedness_flags_an_invented_amount(orch):
    case = {"expect": {}}
    run = orch.start("Order 1002 arrived broken", "eval")
    assert groundedness(case, run) == (True, "ok")
    run.proposal = run.proposal.model_copy(update={"customer_reply": "Refund of $1,500.00 for order 1002."})
    assert groundedness(case, run)[0] is False


def test_tool_accuracy_flags_a_missing_policy_check(orch):
    run = orch.start("Order 1002 arrived broken", "eval")
    run.events = [e for e in run.events if e.name != "check_refund_eligibility"]
    case = {"expect": {"tools": ["lookup_order", "check_refund_eligibility"], "order_id": "1002"}}
    assert tool_accuracy(case, run) == (False, "missing check_refund_eligibility")
