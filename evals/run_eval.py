"""Run the eval dataset against a model and write JSON + Markdown reports.

    python -m evals.run_eval --model scripted
    python -m evals.run_eval --model claude --repeats 3 --judge   # needs ANTHROPIC_API_KEY

Exit code 1 if a gate is missed or a criterion dropped versus the baseline.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from refund_agent.agent.orchestrator import Orchestrator
from refund_agent.agent.prompts import PROMPT_VERSION
from refund_agent.agent.scripted_llm import ScriptedLLM
from refund_agent.config import ROOT, Settings
from refund_agent.data_access.repository import ShopRepository
from refund_agent.state.store import RunStore
from refund_agent.tools.refund_tools import PaymentsGateway

from .scorers import CRITERIA, model_reply, score_run

EVAL_DIR = Path(__file__).resolve().parent
GATES = {"task_completion": 0.90, "groundedness": 1.0, "relevance": 0.85,
         "tool_accuracy": 0.90, "safety": 1.0, "failure_handling": 1.0}
REGRESSION_TOLERANCE = 0.05


def load_cases(path: Path = EVAL_DIR / "dataset.jsonl") -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def make_llm(model: str):
    if model == "scripted":
        return ScriptedLLM()
    if model == "scripted-sloppy":
        return ScriptedLLM("sloppy")
    if model.startswith("claude"):
        from refund_agent.agent.claude_llm import ClaudeLLM
        return ClaudeLLM(model="claude-opus-5-5" if model == "claude" else model)
    raise ValueError(f"unknown model {model!r}")


def run_case(case: dict, model: str, repo: ShopRepository, settings: Settings):
    payments = PaymentsGateway()
    orch = Orchestrator(repo, RunStore(), payments, settings, lambda: make_llm(model))
    started = time.perf_counter()
    run = orch.start(case["message"], created_by="eval", faults=case.get("faults", []))
    return run, len(payments.payouts), round((time.perf_counter() - started) * 1000)


def evaluate(model: str, repeats: int = 1, case_ids: list[str] | None = None, judge=None) -> dict:
    settings = Settings(tool_timeout_s=0.3, allow_fault_injection=True)
    repo = ShopRepository(settings.data_dir)
    cases = [c for c in load_cases() if not case_ids or c["id"] in case_ids]
    results = []
    for case in cases:
        attempts = []
        for _ in range(repeats):
            run, payouts, ms = run_case(case, model, repo, settings)
            scores = score_run(case, run, payouts)
            attempt = {
                "run_id": run.run_id, "status": run.status.value, "duration_ms": ms,
                "decision": run.proposal.decision.value if run.proposal else None,
                "amount": str(run.proposal.amount) if run.proposal else None,
                "reply": model_reply(run), "error": run.error,
                "tools": [e.name for e in run.events if e.kind == "tool_call"],
                "scores": {k: (None if v is None else {"passed": v[0], "note": v[1]}) for k, v in scores.items()},
            }
            if judge and attempt["reply"] and run.status.value not in ("blocked", "failed"):
                attempt["judge"] = judge.grade(case["message"], run.facts, attempt["decision"] or "",
                                               attempt["reply"]).model_dump()
            attempts.append(attempt)
        passed_all = all(s is None or s["passed"] for a in attempts for s in a["scores"].values())
        results.append({"id": case["id"], "category": case["category"], "attempts": attempts,
                        "pass_all_repeats": passed_all,
                        "consistent": len({(a["status"], a["decision"], a["amount"]) for a in attempts}) == 1})

    criteria = {}
    for name in CRITERIA:
        scored = [a["scores"][name] for r in results for a in r["attempts"] if a["scores"][name] is not None]
        rate = sum(s["passed"] for s in scored) / len(scored) if scored else 1.0
        criteria[name] = {"rate": round(rate, 3), "scored": len(scored), "gate": GATES[name],
                          "meets_gate": rate >= GATES[name]}
    judge_summary = None
    if judge:
        verdicts = [a["judge"] for r in results for a in r["attempts"] if "judge" in a]
        keys = ["addresses_request", "consistent_with_decision", "no_invented_facts", "tone_ok"]
        judge_summary = {k: round(sum(v[k] for v in verdicts) / max(len(verdicts), 1), 3) for k in keys}

    return {
        "model": model, "prompt_version": PROMPT_VERSION, "repeats": repeats,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "cases": len(results),
        "criteria": criteria,
        "pass_all_repeats_rate": round(sum(r["pass_all_repeats"] for r in results) / len(results), 3),
        "consistency_rate": round(sum(r["consistent"] for r in results) / len(results), 3),
        "status_counts": dict(Counter(a["status"] for r in results for a in r["attempts"])),
        "gates_ok": all(c["meets_gate"] for c in criteria.values()),
        "judge": judge_summary,
        "results": results,
    }


def compare(report: dict, baseline: dict) -> list[str]:
    out = []
    for name, c in report["criteria"].items():
        before = baseline.get("criteria", {}).get(name, {}).get("rate")
        if before is not None and c["rate"] < before - REGRESSION_TOLERANCE:
            out.append(f"{name}: {before:.0%} -> {c['rate']:.0%}")
    return out


def to_markdown(report: dict, regressions: list[str]) -> str:
    lines = [f"# Eval report: {report['model']}", "",
             f"Prompt {report['prompt_version']} · {report['cases']} cases × {report['repeats']} repeat(s) · "
             f"{report['generated_at']}", "",
             f"**Gates: {'PASS' if report['gates_ok'] else 'FAIL'}** · "
             f"all-repeats pass rate {report['pass_all_repeats_rate']:.0%} · "
             f"consistency {report['consistency_rate']:.0%}", "",
             "| Criterion | Pass rate | Gate | Scored runs |", "|---|---|---|---|"]
    for name, c in report["criteria"].items():
        mark = "ok" if c["meets_gate"] else "**below gate**"
        lines.append(f"| {name} | {c['rate']:.0%} | {c['gate']:.0%} ({mark}) | {c['scored']} |")
    if report.get("judge"):
        lines += ["", "Judge model (informational): " +
                  ", ".join(f"{k} {v:.0%}" for k, v in report["judge"].items())]
    lines += ["", "## Regressions vs baseline", ""]
    lines += [f"- {r}" for r in regressions] or ["None."]
    lines += ["", "## Cases", "", "| Case | Category | Result | Status / decision / amount | Notes |",
              "|---|---|---|---|---|"]
    for r in report["results"]:
        outcomes = {f"{a['status']} / {a['decision'] or '-'} / {a['amount'] or '-'}" for a in r["attempts"]}
        notes = sorted({f"{k}: {s['note']}" for a in r["attempts"] for k, s in a["scores"].items()
                        if s and not s["passed"]})
        lines.append(f"| {r['id']} | {r['category']} | {'pass' if r['pass_all_repeats'] else 'FAIL'} | "
                     f"{'<br>'.join(sorted(outcomes))} | {'<br>'.join(notes)} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="scripted")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--cases", nargs="*")
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "reports" / "eval"))
    ap.add_argument("--baseline", default=str(EVAL_DIR / "baseline.json"))
    ap.add_argument("--update-baseline", action="store_true")
    args = ap.parse_args(argv)

    judge = None
    if args.judge:
        from .judge import ClaudeJudge
        judge = ClaudeJudge()
    report = evaluate(args.model, args.repeats, args.cases, judge)
    baseline_path = Path(args.baseline)
    regressions = compare(report, json.loads(baseline_path.read_text())) if baseline_path.exists() else []

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{args.model}.json").write_text(json.dumps(report, indent=2, default=str), "utf-8")
    (out / f"{args.model}.md").write_text(to_markdown(report, regressions), "utf-8")
    if args.update_baseline:
        slim = {k: report[k] for k in ("model", "prompt_version", "generated_at", "criteria")}
        baseline_path.write_text(json.dumps(slim, indent=2) + "\n", "utf-8")

    for name, c in report["criteria"].items():
        print(f"{name:18} {c['rate']:6.0%}  gate {c['gate']:.0%}  {'ok' if c['meets_gate'] else 'BELOW GATE'}")
    print(f"pass^k {report['pass_all_repeats_rate']:.0%}  consistency {report['consistency_rate']:.0%}")
    for r in regressions:
        print(f"REGRESSION {r}")
    print(f"report: {out / (args.model + '.md')}")
    return 0 if report["gates_ok"] and not regressions else 1


if __name__ == "__main__":
    sys.exit(main())
