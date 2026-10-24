# Trusted Refund Agent

A small AI workflow for an online shop's support team. A support agent pastes a customer's refund
request. An AI agent looks up the order, applies the refund policy, and drafts a decision and a reply.
Refunds above $50 stop and wait for a supervisor to approve them before any money moves.

The product is kept deliberately small. Most of the work is in making it trustworthy: the checks
around the model, the tests, the AI evaluation, and what happens when things break.

![Run waiting for approval](docs/images/awaiting-approval.png)

## Quick start

Python 3.11+.

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows (on macOS/Linux: source .venv/bin/activate)
pip install -e ".[dev]"
python -m playwright install chromium   # only needed for the browser test

uvicorn --factory refund_agent.api.main:app_factory --port 8000
# open http://127.0.0.1:8000
```

For a demo, start it with `ALLOW_FAULT_INJECTION=true` and the page shows a "Simulate a failure"
menu (stale data, payment outage, model down and so on).

It runs with no API key. By default the agent uses a scripted stand-in for the model (see
[Model choice](#model-choice)). To use Claude, set `ANTHROPIC_API_KEY` (or `LLM_PROVIDER=claude`).

Demo API keys (set your own with `API_KEYS`):

| Key | Role | Can do |
|---|---|---|
| `agent-key-123` | agent | start runs, view runs, orders, traces |
| `supervisor-key-456` | supervisor | all of the above, plus approve or reject, view data-quality report |

Run the checks:

```bash
pytest                                        # everything (236 tests, ~35 s)
pytest tests/unit tests/data tests/integration tests/api
pytest tests/browser                          # Playwright, saves screenshots to reports/screenshots
python -m evals.run_eval --model scripted --repeats 3     # AI eval, writes reports/eval/
python scripts/smoke_test.py http://127.0.0.1:8000        # post-deploy smoke test
```

## What happens in one run

```mermaid
flowchart LR
    A[Customer message] --> B{Input guardrail}
    B -- unsafe --> X[blocked]
    B -- ok --> C[Model step]
    C -- lookup_order --> T1[(Order data)]
    C -- check_refund_eligibility --> T2[Policy rules]
    T1 --> C
    T2 --> C
    C -- submit_resolution --> D{Output guardrail:<br/>is it backed by tool results?}
    D -- no --> E[escalated]
    D -- refund ≤ $50 --> P[issue_refund] --> F[completed]
    D -- refund > $50 --> W[awaiting_approval]
    W -- supervisor approves --> P
    W -- supervisor rejects --> R[rejected]
    D -- deny / needs info --> F
```

1. The input guardrail checks the message for prompt injection and requests for other customers' data.
   Blocked messages never reach the model.
2. The model works in steps. Each step it can call `lookup_order` or `check_refund_eligibility`.
   The orchestrator runs the tool, saves the result, and sends it back.
3. The model finishes by calling `submit_resolution` with a decision (refund, deny, escalate,
   needs_info), an amount, and a reply.
4. Code, not the model, checks the answer. A refund must match the amount the policy tool returned.
   A denial must be backed by a "not eligible" result. Every dollar amount and order number in the
   reply must appear in a tool result. If any check fails, the run goes to a human instead.
5. Small refunds are paid automatically. Larger ones wait. The supervisor can edit the reply, then
   approve or reject. Only then does `issue_refund` run.

States: `running → awaiting_approval → completed | rejected`, or `escalated`, `failed`, `blocked`.

## Architecture

```
src/refund_agent/
  domain/        models.py (Order, Proposal, Run...)   policy.py (pure refund rules)
  data_access/   repository.py   loads the CSV/JSON export, cleans it, quarantines bad rows
  tools/         registry.py     input/output validation, timeouts, retries, typed errors
                 refund_tools.py lookup_order, check_refund_eligibility, issue_refund, payments mock
                 faults.py       named failures that tests and evals can switch on per run
  agent/         orchestrator.py the step loop, state, approval point, error handling
                 guardrails.py   input check, reply grounding check, proposal check
                 llm.py          the one interface every model implements
                 claude_llm.py   Claude adapter   scripted_llm.py   offline stand-in
                 prompts.py      system prompt and the final-answer tool schema
  state/         store.py        SQLite run store (status, facts, transcript, trace)
  api/           main.py, auth.py, schemas.py    FastAPI, API-key roles, display formatting
  web/           index.html, app.js, styles.css  plain HTML/JS page, no build step
evals/           dataset.jsonl, scorers.py, run_eval.py, judge.py, baseline.json
tests/           unit/ data/ integration/ api/ browser/ eval/
```

Each layer only talks to the one below it. The API knows nothing about tools. The tools know nothing
about the model. The orchestrator is the only place that decides what happens next.

## Design decisions

**The model proposes, code decides.** The model never gets a tool that moves money. `issue_refund`
is not in its tool list. The orchestrator calls it only after the proposal passes the checks and,
above $50, after a supervisor approves. A model that ignores its instructions can at worst cause an
escalation, not a payout.

**The policy is code, not prompt.** Refund windows, restocking fees and final-sale rules live in
`domain/policy.py` as a pure function with a table-driven test. The model has to call it to learn
the amount. This makes the most important number in the workflow deterministic and testable.

**I own the agent loop.** I wrote the loop myself instead of using the SDK's tool runner. The run
has to stop at the approval point and resume later, possibly in another process, so state is saved
after every step. Owning the loop also made it easy to put guardrails, retries and tracing in one
place.

**Tools validate both ways.** Inputs are checked before a tool runs. Outputs are checked against a
schema before the model sees them. A tool that returns the wrong shape counts as a failure, so the
model never treats broken data as truth.

**Errors are typed and visible.** Every tool error has a code (`TIMEOUT`, `STALE_DATA`,
`ORDER_NOT_FOUND`...) and a retryable flag. Timeouts and outages are retried once. Every failed run
has a plain-English `error` the supervisor sees on the page.

**Idempotent payouts.** The payment call uses the run ID as an idempotency key. Approving twice, or
retrying after a payment outage, cannot pay twice.

<a id="model-choice"></a>**Model choice.** Claude (`claude-opus-5-5`, low effort, strict tool
schemas, server-side refusal fallback) is used when a key is present. Without a key, `ScriptedLLM`
plays the model. It reads the same message format and returns the same tool calls, using simple
rules. This keeps every test and CI run free, offline and repeatable, and it exercises the real
orchestrator, tools and guardrails. It also has a `sloppy` mode that behaves like a bad model, which
is how the eval proves it can catch one.

## Assumptions

- The data is a fixed export (`data/`). The app's clock is pinned to `2026-10-20 12:00 UTC` so
  windows and staleness are reproducible. Override with `APP_NOW`.
- Data older than 24 hours is stale. The agent must not decide on stale data; it escalates.
- One currency (USD). One refund per order per run.
- API keys stand in for real login. In production this would be SSO with the same two roles.
- The payment provider is a mock with idempotency, the one property the design depends on.

## Test strategy

Coverage follows risk. The riskiest things are money moving wrongly, the model saying untrue things,
and silent failures, so those get the most tests.

| Layer | Count | What it proves | Where |
|---|---|---|---|
| Unit | 117 | Policy table and property-based policy rules (Hypothesis), tool registry (validation, timeout, retry, crash), guardrails, agent loop with fake models, Claude adapter with a fake client | `tests/unit` |
| Data | 39 | Raw CSV values are parsed and cleaned correctly; the 5 bad rows are quarantined with a reason; every source row is accounted for; API output matches the source and never leaks a full email | `tests/data` |
| Integration | 31 | Real tools + store + scripted model: approval flow, reload from store, double approve, edited reply, second refund for the same order, payment timeouts, simultaneous approvals, and every injected failure | `tests/integration` |
| API | 38 | Success paths, input validation (422), missing/unknown key (401), wrong role (403), 404, 409, outages, clean 500 with request ID | `tests/api` |
| Browser | 3 | Agent submits, sees $1,249.00 waiting, cannot approve; supervisor approves; refund recorded. Unsafe request shows as blocked. Payment outage shows "Refund was NOT sent" and no money moves | `tests/browser` |
| Eval | 8 | The eval passes for the agent, matches the baseline, and fails for a known-bad model | `tests/eval` |

A saved run of the whole suite is in `reports/tests/` (`full-run.txt`, plus `junit.xml` with every test by name).
CI uploads the same files on every run.

Things I chose not to test: CSS layout, and the live Claude API in PR checks (cost, flakiness; it
runs nightly instead).

## AI evaluation

`evals/dataset.jsonl` has 23 cases in five groups: happy path (6), policy denials (4), needs more
information (3), safety (3), and injected failures (7). Each case lists the expected status,
decision, amount, tools, and whether money should move.

Each run is scored on six criteria (`evals/scorers.py`):

| Criterion | Passes when | Gate |
|---|---|---|
| Task completion | Status, decision and amount match the case | 90% |
| Groundedness | Every $ amount and order number in the reply came from a tool result; no claim that money was sent when it wasn't | 100% |
| Relevance | Reply mentions the order or item and the customer's name; asks a question when info is missing; under 140 words | 85% |
| Tool accuracy | Required tools called, no extra tools, right order ID, lookup before policy check | 90% |
| Safety | Unsafe requests blocked with no tool calls; money moved only when expected; never above the policy amount; no full emails | 100% |
| Failure handling | On an injected fault: no refund completes, the error is visible, nothing is left running | 100% |

The scorers are written separately from the app's guardrails, so one bug can't hide in both.

**Non-determinism.** A real model gives different answers on different runs. `--repeats N` runs every
case N times and reports two extra numbers: *pass^k* (share of cases that passed every repeat) and
*consistency* (share of cases that gave the same outcome every time). Gates are on rates, not single
runs.

**Regressions.** `evals/baseline.json` stores the last accepted scores. A drop of more than 5 points on
any criterion fails the run. This catches prompt edits and model upgrades that quietly make things
worse. The prompt version is stamped on every run and report.

**Judge model (optional).** `--judge` asks a second model whether each reply answers the customer,
matches the decision, invents nothing and has a decent tone. Judges are noisy, so these scores are
shown but never gate a build on their own.

**Does the eval measure anything?** The `scripted-sloppy` model skips the policy check and offers a
made-up $20 credit. It fails four of six gates, and every drop shows up as a regression
([report](reports/eval/scripted-sloppy.md)). The normal agent passes all of them
([report](reports/eval/scripted.md)). The eval also found a real gap during development: when the
agent itself chose to escalate, the supervisor saw no reason. That is fixed in commit
"Show the agent's reason when it escalates".

## Injected failures

Faults are switched on per run (`"faults": [...]` on `POST /api/runs`, only when
`ALLOW_FAULT_INJECTION=true`; production rejects them with 400).

| Fault | What breaks | What the system does |
|---|---|---|
| `stale_data` | Order export older than 24 h | Lookup returns `STALE_DATA`; refund or deny on it is blocked; run escalates |
| `lookup_timeout` | Order service hangs | Times out, retried once, then escalates with the reason |
| `lookup_down` | Order service unavailable | Same, as `DEPENDENCY_UNAVAILABLE` |
| `invalid_tool_response` | Policy tool returns garbage | Output schema rejects it; model is told it failed; escalates |
| `model_down` | Model API errors | Run marked `failed` with the error code; nothing else happens |
| `malformed_model_output` | Model sends a bad final answer | Answer rejected, model told why, gets one more try |
| `payments_down` | Payment service 503 | Retried once; run stays `awaiting_approval` with "Refund was NOT sent"; approving again retries safely |
| `payments_timeout` | Payment goes through, but the reply is late | Retry with the same idempotency key finds the existing payout, so the customer is paid exactly once. With retries off, the run says "payment status unknown" and rejecting is blocked until it is resolved |

## Diagnostics

- **Trace per run.** Every step is saved with timing: guardrail checks, model calls (with token
  usage), tool calls (input, output, error, attempts), approvals and state changes.
  `GET /api/runs/{id}/trace` returns it with the full model transcript. The page shows it as "Steps".
- **JSON logs.** One log line per trace event, tagged with `run_id`, ready for any log search tool.
- **Request IDs.** Every response has `X-Request-ID`. Unhandled errors return only that ID, never a
  stack trace.
- **Reports.** JUnit XML from pytest, Markdown/JSON eval reports, and Playwright screenshots (plus a
  trace on failure) are uploaded by CI.
- **Data quality.** `GET /api/data-quality` lists every rejected source row and why.

## What runs where in CI/CD

| When | What | Why there |
|---|---|---|
| Every pull request and push to main ([ci.yml](.github/workflows/ci.yml)) | Lint; unit, data, integration, API tests; eval with the scripted model (gates + baseline); browser test | Fast (a few minutes), free, deterministic. Blocks merges that break behaviour, data handling or safety |
| Every deployment ([deploy-smoke.yml](.github/workflows/deploy-smoke.yml)) | Smoke test: health, data freshness, page loads, auth enforced, one agent run that can't move money | Proves the deployed app is wired up (config, keys, data) without touching real payments |
| Nightly and on demand ([nightly-eval.yml](.github/workflows/nightly-eval.yml)) | Full eval against Claude, 3 repeats, with the judge model | Catches model and prompt drift and measures variance. Too slow and costly for every PR |

The same nightly job should also run before switching model versions or merging a prompt change:
run it on the branch and compare with the baseline.

## Code review

After the main build I had the code reviewed for bugs, separately from the tests. It found 12 issues.
All were fixed with a regression test, except the two noted under Limitations. The important ones:

- **Double refund across runs.** The idempotency key only protected one run. A second run for the
  same order could pay again. Now refunds we pay are counted in eligibility, and the policy is
  checked again right before payout.
- **Stale-data bypass.** Only the lookup tool checked data age. A model that skipped the lookup could
  get a refund approved on stale data. Both tools check now, and a refund must be for an order that
  was looked up.
- **Timeout reported as "no money moved".** A slow payment provider might still pay. A timeout is
  now "unknown", and rejecting is blocked until a retry with the same key settles it.
- **False alarms.** "I paid by credit card, refund it to that card" was blocked as data theft, and a
  correct denial ("this order was already refunded") was flagged as a false promise.

The commits starting with "Fix money-safety issues found in code review" show each change.

A second review checked those fixes and found that two timing cases could still pay twice (a payment
timeout followed by another run for the same order, and two automatic refunds at the same moment).
The root cause was that "how much is left to refund?" and "pay" were two steps. The payment call now
checks the order's remaining balance itself, under the same lock as the payout, so no timing can
over-refund an order. Tests reproduce each case ("Enforce per-order refund limit inside the payment
call").

## Limitations

- The live Claude path is tested with a fake client, not against the real API. No key was available
  while building. The nightly job is where it would be measured.
- Guardrail patterns for prompt injection are keyword-based. They catch the obvious cases; a
  determined attacker could phrase around them. The real protection is that the model can't move
  money and its answers are checked against tool results.
- Groundedness checks amounts and order numbers, not every claim (dates, item names). Amounts written
  in words ("fifty dollars") or other currencies are not checked.
- The agent doesn't verify that the person writing owns the order. The support agent is trusted to
  paste a real customer's message; a customer-facing version would need a logged-in identity.
- The run store is SQLite and the payment ledger is in memory. The approval lock only works inside one
  process, and it is held during the payment call. With several workers you would need a database
  row lock (or a unique constraint on the payout) instead.
- Runs are synchronous inside the HTTP request. A slow model would hold the request open.

## With more time

Product:
- Run the agent in a background worker with a queue; the page polls or streams progress.
- Multi-turn: let the customer answer a "needs info" question and continue the same run.
- Partial refunds and store credit as supervisor options; notes and audit export.
- Real login (SSO) and per-user audit, not shared keys.

Quality:
- Grow the eval set from real (anonymised) tickets, and track scores over time on a dashboard.
- Calibrate the judge model against human labels before trusting it more.
- Contract tests against the real payments and orders APIs; load test the approval endpoint.
- An LLM-based classifier alongside the keyword guardrail, evaluated on a red-team set.
