const $ = (id) => document.getElementById(id);
let current = null;

function key() { return $("role").value; }
function isSupervisor() { return key().startsWith("supervisor"); }

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", "X-API-Key": key(), ...(options.headers || {}) },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join("; ") : body.detail;
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return body;
}

function showError(el, err) { el.textContent = err ? err.message || err : ""; el.hidden = !err; }

async function loadRuns() {
  const runs = await api("/api/runs");
  $("runs").replaceChildren(...runs.map((r) => {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.innerHTML = `<span class="msg"></span><span class="pill ${r.status}">${r.status.replace("_", " ")}</span>`;
    b.querySelector(".msg").textContent = r.customer_message;
    b.onclick = () => showRun(r.run_id);
    li.append(b);
    return li;
  }));
}

async function showRun(runId) {
  const run = await api(`/api/runs/${runId}`);
  const trace = await api(`/api/runs/${runId}/trace`);
  current = run;
  $("empty").hidden = true;
  $("detail").hidden = false;
  $("status").textContent = run.status.replace("_", " ");
  $("status").className = `pill ${run.status}`;
  $("run-id").textContent = `${run.run_id} · ${run.model}`;
  showError($("error"), run.error);

  const p = run.proposal;
  $("proposal").hidden = !p;
  $("order-card").hidden = true;
  if (p) {
    $("p-decision").textContent = p.decision.replace("_", " ");
    $("p-amount").textContent = run.proposal_amount_display;
    $("p-order").textContent = p.order_id || "—";
    $("p-rationale").textContent = p.rationale || "—";
    if (p.order_id) {
      api(`/api/orders/${p.order_id}`).then((o) => {
        $("order-card").textContent =
          `${o.item} · ${o.amount} · ordered ${o.ordered_on} · delivered ${o.delivered_on} · ` +
          `final sale: ${o.final_sale} · ${o.customer} (${o.customer_email})`;
        $("order-card").hidden = false;
      }).catch(() => {});
    }
  }

  const awaiting = run.status === "awaiting_approval";
  $("reply").value = run.final_reply || (p ? p.customer_reply : "");
  $("reply").readOnly = !(awaiting && isSupervisor());
  $("actions").hidden = !awaiting;
  $("approve").disabled = $("reject").disabled = !isSupervisor();
  $("actions-hint").textContent = isSupervisor()
    ? "You can edit the reply before approving. Money moves only after you approve."
    : "Waiting for a supervisor. Switch role to Supervisor to decide.";

  $("trace").replaceChildren(...trace.events.map((e) => {
    const li = document.createElement("li");
    const bad = e.data.ok === false || e.kind === "error" || (e.data.problems || []).length || e.data.blocked;
    if (bad) li.className = "bad";
    const ms = e.duration_ms != null ? ` (${Math.round(e.duration_ms)} ms)` : "";
    const extra = e.data.error_code || (e.data.problems || []).join("; ") || e.data.category || "";
    li.textContent = `${e.kind}: ${e.name}${ms}${extra ? " — " + extra : ""}`;
    return li;
  }));
}

$("new-run").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  showError($("form-error"), null);
  $("submit").disabled = true;
  try {
    const run = await api("/api/runs", {
      method: "POST", body: JSON.stringify({ customer_message: $("message").value }),
    });
    await showRun(run.run_id);
    await loadRuns();
  } catch (err) {
    showError($("form-error"), err);
  } finally {
    $("submit").disabled = false;
  }
});

async function decide(action) {
  const body = { note: $("note").value };
  if (action === "approve" && $("reply").value !== current.proposal.customer_reply) {
    body.customer_reply = $("reply").value;
  }
  if (action === "reject" && body.note.trim().length < 3) {
    return showError($("error"), "Write a short note (3+ characters) before rejecting.");
  }
  try {
    await api(`/api/runs/${current.run_id}/${action}`, { method: "POST", body: JSON.stringify(body) });
    $("note").value = "";
  } catch (err) {
    await showRun(current.run_id);
    return showError($("error"), err);
  }
  await showRun(current.run_id);
  await loadRuns();
}

$("approve").onclick = () => decide("approve");
$("reject").onclick = () => decide("reject");
$("role").onchange = () => { loadRuns(); if (current) showRun(current.run_id); };
document.querySelectorAll("[data-sample]").forEach((b) => { b.onclick = () => { $("message").value = b.dataset.sample; }; });
loadRuns();
