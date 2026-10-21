const $ = (selector) => document.querySelector(selector);
const state = { scenarios: [], runs: [], selectedRunId: null, pollTimer: null, replay: { currentStep: 0, timer: null, events: [] } };

async function request(path, options) {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("show");
  window.setTimeout(() => element.classList.remove("show"), 2600);
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}

function renderHistory() {
  const body = $("#history-body");
  if (!state.runs.length) {
    body.innerHTML = '<tr><td colspan="6" class="empty-row">No runs yet. Launch a scenario to see its evidence here.</td></tr>';
  } else {
    body.innerHTML = state.runs.map((run) => `
      <tr data-run-id="${escapeHtml(run.id)}">
        <td class="history-name">${escapeHtml(run.scenario)}</td>
        <td><span class="impl-badge">${escapeHtml(run.implementation)}</span></td>
        <td>${escapeHtml(run.status)}</td>
        <td><span class="verdict-badge ${escapeHtml(run.verdict)}">${escapeHtml(run.verdict)}</span></td>
        <td>${escapeHtml(formatTime(run.started_at))}</td>
        <td class="run-id-short">${escapeHtml(run.id.slice(0, 8))}</td>
      </tr>`).join("");
    body.querySelectorAll("[data-run-id]").forEach((row) => row.addEventListener("click", () => openRun(row.dataset.runId)));
  }
  $("#metric-runs").textContent = String(state.runs.length);
  $("#metric-failures").textContent = String(state.runs.filter((run) => run.verdict === "failed").length);
  $("#metric-passes").textContent = String(state.runs.filter((run) => run.verdict === "passed").length);
}

function renderRun(run) {
  $("#active-run").hidden = false;
  $("#run-title").textContent = `${run.scenario.name} · ${run.implementation}`;
  const status = $("#run-status");
  status.textContent = run.status === "completed" ? run.verdict.toUpperCase() : run.status.toUpperCase();
  status.className = `run-status-pill ${run.status === "completed" ? run.verdict : run.status}`;
  const summary = run.summary || {};
  const isWebhook = run.scenario.fault === "duplicate_payment_webhook";
  $("#run-overview").innerHTML = [
    ["Run", run.id],
    ["Order", summary.order_id || run.scenario.order_id],
    [isWebhook ? "Deliveries" : "Attempts", isWebhook ? (summary.deliveries ?? "—") : (summary.attempts ?? "—")],
    [isWebhook ? "Fulfillments" : "Charges", isWebhook ? (summary.fulfillment_count ?? "—") : (summary.charge_count ?? "—")],
  ].map(([label, value]) => `<span class="overview-chip">${escapeHtml(label)}<strong>${escapeHtml(value)}</strong></span>`).join("");

  const charges = summary.charges || [];
  const fulfillments = summary.fulfillments || [];
  const refunds = summary.refunds || [];
  const ledgerLabel = $("#ledger-label");
  const ledgerSub = $("#ledger-sub");
  if (isWebhook) {
    if (ledgerLabel) ledgerLabel.textContent = "Fulfillment side-effect ledger";
    if (ledgerSub) ledgerSub.textContent = "Order fulfillment side effects recorded by the checkout service";
    $("#charge-list").innerHTML = fulfillments.length ? fulfillments.map((ful) => `
      <span class="charge-chip"><i></i><strong>${escapeHtml(ful.id)}</strong><span>${escapeHtml(ful.order_id)}</span><small>${escapeHtml(ful.status)}</small></span>`).join("")
      : '<span class="ledger-empty">No fulfillment recorded yet.</span>';
  } else {
    if (ledgerLabel) ledgerLabel.textContent = "Provider side-effect ledger";
    if (ledgerSub) ledgerSub.textContent = "Committed charges and refunds observed by the test provider";
    const chargeItems = charges.map((charge) => `
      <span class="charge-chip"><i></i><strong>${escapeHtml(charge.id)}</strong><span>${escapeHtml((charge.amount_cents / 100).toLocaleString(undefined, { style: "currency", currency: "USD" }))}</span><small>${escapeHtml(charge.status)}</small></span>`).join("");
    const refundItems = refunds.map((ref) => `
      <span class="charge-chip refund-chip"><i class="refund-dot"></i><strong>${escapeHtml(ref.id)}</strong><span>${escapeHtml(ref.charge_id)}</span><small>refunded</small></span>`).join("");
    $("#charge-list").innerHTML = (chargeItems + refundItems) || '<span class="ledger-empty">No committed charge recorded yet.</span>';
  }

  const evidenceEvents = (item) => item.evidence.map((evidence) => {
    const event = run.events.find((candidate) => candidate.type === evidence ||
      (evidence.startsWith("charge:") && candidate.type === "PROVIDER_CHARGE_COMMITTED" && candidate.payload.charge_id === evidence.slice(7)) ||
      (evidence.startsWith("refund:") && candidate.type === "PROVIDER_REFUND_COMMITTED" && candidate.payload.refund_id === evidence.slice(7)) ||
      (evidence.startsWith("fulfillment:") && candidate.type === "ORDER_FULFILLMENT_COMMITTED" && candidate.payload.fulfillment_id === evidence.slice(12)));
    return event ? `<a class="evidence-link" href="#event-${event.sequence}">View event ${event.sequence}</a>` : "";
  }).filter(Boolean).join(" ");
  $("#assertion-list").innerHTML = run.assertions.length ? run.assertions.map((item) => `
    <div class="assertion-row"><span class="assertion-state ${escapeHtml(item.status)}">${item.status === "passed" ? "✓" : "×"}</span>
      <div class="assertion-copy"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.observed)}</small><span>${evidenceEvents(item)}</span></div></div>`).join("")
    : '<div class="assertion-row"><span class="assertion-state">·</span><div class="assertion-copy"><strong>Waiting for evaluation</strong><small>Assertions appear when the run finishes.</small></div></div>';

  $("#event-list").innerHTML = run.events.map((event) => {
    const tone = event.type.includes("DROPPED") || event.type.includes("UNKNOWN") ? "fault" :
      (event.type.includes("COMPLETED") || event.type.includes("COMMITTED") ? "good" : "");
    const detail = Object.entries(event.payload || {}).map(([key, value]) => `${key}: ${value}`).join(" · ");
    return `<li class="event-item ${tone}" id="event-${event.sequence}"><strong>${String(event.sequence).padStart(2, "0")} · ${escapeHtml(event.type.replaceAll("_", " "))}</strong><small>${escapeHtml(detail || formatTime(event.recorded_at))}</small></li>`;
  }).join("") || '<li class="event-item"><strong>Waiting for events</strong><small>The runner is preparing the local test environment.</small></li>';
  $("#copy-run-id").dataset.runId = run.id;
  $("#download-repro").href = `/api/runs/${encodeURIComponent(run.id)}/reproduce`;
  state.replay.events = run.events;
  setReplayStep(run.events.length, false);
  const active = ["queued", "running", "cancel_requested"].includes(run.status);
  $("#cancel-run").hidden = !["queued", "running"].includes(run.status);
  $("#cancel-run").dataset.runId = run.id;
  if (active) schedulePoll(run.id);
}

function setReplayStep(step, scroll = true) {
  const count = state.replay.events.length;
  if (!count) {
    const label = $("#replay-step-label");
    if (label) label.textContent = "No events";
    return;
  }
  const clamped = Math.max(1, Math.min(step, count));
  state.replay.currentStep = clamped;
  const slider = $("#replay-slider");
  if (slider) {
    slider.max = count;
    slider.value = clamped;
  }
  const label = $("#replay-step-label");
  if (label) label.textContent = `Event ${clamped} of ${count}`;

  document.querySelectorAll(".event-item").forEach((item, idx) => {
    const seq = idx + 1;
    item.classList.toggle("dimmed", seq > clamped);
    item.classList.toggle("active-step", seq === clamped);
    if (seq === clamped && scroll) {
      item.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  });
}

function toggleReplayPlay() {
  const btn = $("#replay-play");
  if (state.replay.timer) {
    window.clearInterval(state.replay.timer);
    state.replay.timer = null;
    if (btn) btn.textContent = "▶ Play";
    return;
  }
  if (state.replay.currentStep >= state.replay.events.length) {
    setReplayStep(1, false);
  }
  if (btn) btn.textContent = "❚❚ Pause";
  state.replay.timer = window.setInterval(() => {
    if (state.replay.currentStep < state.replay.events.length) {
      setReplayStep(state.replay.currentStep + 1);
    } else {
      window.clearInterval(state.replay.timer);
      state.replay.timer = null;
      if (btn) btn.textContent = "▶ Play";
    }
  }, 750);
}

async function openRun(runId) {
  try {
    const { run } = await request(`/api/runs/${encodeURIComponent(runId)}`);
    if (!run) throw new Error("Run not found");
    state.selectedRunId = runId;
    renderRun(run);
    $("#active-run").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) { toast(error.message); }
}

function schedulePoll(runId) {
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  state.pollTimer = window.setTimeout(async () => {
    try {
      await refreshRuns();
      if (state.selectedRunId === runId) await openRun(runId);
    } catch {
      if (state.selectedRunId === runId) state.pollTimer = window.setTimeout(() => schedulePoll(runId), 1800);
    }
  }, 650);
}

async function refreshRuns() {
  const { runs } = await request("/api/runs");
  state.runs = runs;
  renderHistory();
}

async function launchRun(implementation, scenarioId) {
  const buttons = [...document.querySelectorAll(".run-button")];
  buttons.forEach((button) => { button.disabled = true; });
  try {
    const targetId = scenarioId || $("#scenario-select").value;
    const scenario = state.scenarios.find((item) => item.id === targetId) || state.scenarios[0];
    if (!scenario) throw new Error("No runnable scenario is available");
    const { run_id: runId } = await request("/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario_id: scenario.id, implementation }),
    });
    state.selectedRunId = runId;
    await refreshRuns();
    await openRun(runId);
    toast(`${scenario.name} (${implementation}) run started`);
  } catch (error) { toast(error.message); }
  finally { buttons.forEach((button) => { button.disabled = false; }); }
}

async function initialize() {
  try {
    const [scenarioResponse] = await Promise.all([request("/api/scenarios"), refreshRuns()]);
    state.scenarios = scenarioResponse.scenarios;
    $("#scenario-count").textContent = String(state.scenarios.length);
    const select = $("#scenario-select");
    select.innerHTML = '<option value="">Create a new scenario…</option>' + state.scenarios.map((scenario) => `<option value="${escapeHtml(scenario.id)}">${escapeHtml(scenario.name)} · v${scenario.version}</option>`).join("");
    select.value = state.scenarios[0]?.id || "";
    setScenarioEditor();
    if (state.runs.length) await openRun(state.runs[0].id);
  } catch (error) { toast(`Faultline could not load: ${error.message}`); }
}

function updateWorkflowCanvas(fault) {
  const canvas = $("#workflow-canvas");
  if (!canvas) return;
  if (fault === "duplicate_payment_webhook") {
    canvas.innerHTML = `
      <li><span>1</span><div><strong>Webhook delivers payment event</strong><small>Sends payment.succeeded webhook</small></div></li>
      <li><span>2</span><div><strong>Checkout starts fulfillment</strong><small>Records order fulfillment side effect</small></div></li>
      <li class="workflow-fault"><span>3</span><div><strong>Duplicate webhook redelivery</strong><small>Simulates network redelivery of same event</small></div></li>
      <li><span>4</span><div><strong>Checkout verifies deduplication</strong><small>Idempotently ignores duplicate event</small></div></li>
      <li><span>5</span><div><strong>Evaluate invariants</strong><small>Checks single fulfillment & terminal state</small></div></li>
    `;
  } else if (fault === "persistence_failure_after_payment") {
    canvas.innerHTML = `
      <li><span>1</span><div><strong>Checkout starts payment</strong><small>Sends charge request to provider</small></div></li>
      <li><span>2</span><div><strong>Provider commits charge</strong><small>Payment provider ledger records $42.00</small></div></li>
      <li class="workflow-fault"><span>3</span><div><strong>Database persistence fails</strong><small>Simulates checkout DB commit failure</small></div></li>
      <li><span>4</span><div><strong>Checkout runs compensation</strong><small>Issues compensating refund to provider</small></div></li>
      <li><span>5</span><div><strong>Evaluate invariants</strong><small>Checks zero orphaned charges & terminal state</small></div></li>
    `;
  } else {
    canvas.innerHTML = `
      <li><span>1</span><div><strong>Checkout starts payment</strong><small>Uses the order and amount above</small></div></li>
      <li><span>2</span><div><strong>Provider commits charge</strong><small>Local test provider records the side effect</small></div></li>
      <li class="workflow-fault"><span>3</span><div><strong>Drop provider response</strong><small>Simulates an unknown payment outcome</small></div></li>
      <li><span>4</span><div><strong>Checkout retries</strong><small>Runs the configured retry count</small></div></li>
      <li><span>5</span><div><strong>Evaluate invariants</strong><small>Checks duplicate charges and terminal state</small></div></li>
    `;
  }
}

function setScenarioEditor() {
  const selected = state.scenarios.find((item) => item.id === $("#scenario-select").value);
  const definition = selected?.definition || { scenario_version: 1, name: "new-payment-scenario", description: "", order_id: "order-new", amount_cents: 2500, fault: "payment_response_lost_after_commit", retries: 1, assertion: "no_duplicate_charge" };
  state.editorDefinition = definition;
  document.querySelectorAll("[data-field]").forEach((input) => {
    const field = input.dataset.field;
    input.value = definition[field] ?? (field === "timeout_seconds" ? 15 : "");
  });
  updateWorkflowCanvas(definition.fault);
  updateScenarioPreview();
  $("#save-scenario").textContent = selected ? "Save new version" : "Create scenario";
}

function updateScenarioPreview() {
  if (!state.editorDefinition) return;
  const definition = { ...state.editorDefinition };
  document.querySelectorAll("[data-field]").forEach((input) => {
    const field = input.dataset.field;
    definition[field] = input.type === "number" ? Number(input.value) : input.value;
  });
  state.editorDefinition = definition;
  $("#scenario-json").value = JSON.stringify(definition, null, 2);
}

async function submitScenario(save) {
  const feedback = $("#scenario-feedback");
  try {
    updateScenarioPreview();
    const definition = state.editorDefinition;
    if (!save) {
      const result = await request("/api/scenarios/validate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ definition }) });
      feedback.textContent = result.valid ? "Definition is valid." : "Definition is invalid.";
      return;
    }
    const selected = $("#scenario-select").value;
    const path = selected ? `/api/scenarios/${encodeURIComponent(selected)}/versions` : "/api/scenarios";
    const result = await request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ definition }) });
    feedback.textContent = `Saved ${selected ? `version ${result.version}` : "version 1"}.`;
    await initialize();
    if (result.scenario_id) $("#scenario-select").value = result.scenario_id;
    setScenarioEditor();
  } catch (error) { feedback.textContent = error.message; }
}

document.querySelectorAll(".run-button").forEach((button) => button.addEventListener("click", () => launchRun(button.dataset.implementation, button.dataset.scenario)));
$("#refresh-button").addEventListener("click", () => initialize());
$("#history-refresh").addEventListener("click", () => refreshRuns().catch((error) => toast(error.message)));
$("#copy-run-id").addEventListener("click", async (event) => {
  const runId = event.currentTarget.dataset.runId;
  if (!runId) return;
  try { await navigator.clipboard.writeText(runId); toast("Run ID copied"); }
  catch { toast(runId); }
});
$("#scenario-select").addEventListener("change", setScenarioEditor);
$("#validate-scenario").addEventListener("click", () => submitScenario(false));
$("#save-scenario").addEventListener("click", () => submitScenario(true));
document.querySelectorAll("[data-field]").forEach((input) => input.addEventListener("input", updateScenarioPreview));
$("#cancel-run").addEventListener("click", async (event) => {
  const runId = event.currentTarget.dataset.runId;
  try {
    await request(`/api/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    toast("Cancellation requested");
    await openRun(runId);
  } catch (error) { toast(error.message); }
});

$("#replay-prev")?.addEventListener("click", () => {
  if (state.replay.timer) toggleReplayPlay();
  setReplayStep(state.replay.currentStep - 1);
});
$("#replay-next")?.addEventListener("click", () => {
  if (state.replay.timer) toggleReplayPlay();
  setReplayStep(state.replay.currentStep + 1);
});
$("#replay-reset")?.addEventListener("click", () => {
  if (state.replay.timer) toggleReplayPlay();
  setReplayStep(1);
});
$("#replay-play")?.addEventListener("click", toggleReplayPlay);
$("#replay-slider")?.addEventListener("input", (e) => {
  if (state.replay.timer) toggleReplayPlay();
  setReplayStep(Number(e.target.value));
});

async function runSuite(implementation) {
  const buttons = [...document.querySelectorAll(".suite-run-btn")];
  buttons.forEach((b) => { b.disabled = true; });
  try {
    toast(`Starting Critical Integrations Suite (${implementation})...`);
    const { suite } = await request("/api/suites/critical-integrations");
    for (const scId of suite.scenarios) {
      await launchRun(implementation, scId);
      for (let i = 0; i < 40; i++) {
        await new Promise((r) => setTimeout(r, 200));
        const { run } = await request(`/api/runs/${encodeURIComponent(state.selectedRunId)}`);
        if (["completed", "failed", "cancelled", "timed_out"].includes(run.status)) {
          break;
        }
      }
    }
    toast(`Critical Integrations Suite (${implementation}) completed`);
  } catch (err) {
    toast(`Suite error: ${err.message}`);
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
}

function updateDiffBaselineSelect() {
  const select = $("#diff-baseline-select");
  if (!select) return;
  const currentRunId = state.selectedRunId;
  const candidates = state.runs.filter((r) => r.id !== currentRunId);
  if (!candidates.length) {
    select.innerHTML = '<option value="">No other runs available</option>';
    return;
  }
  select.innerHTML = candidates.map((r) =>
    `<option value="${escapeHtml(r.id)}">${escapeHtml(r.scenario)} · ${escapeHtml(r.implementation)} (${escapeHtml(r.verdict)}) · ${escapeHtml(r.id.slice(0, 8))}</option>`
  ).join("");
}

async function executeRunDiff() {
  const baselineId = $("#diff-baseline-select")?.value;
  if (!baselineId || !state.selectedRunId) {
    toast("Select a baseline run to compare");
    return;
  }
  try {
    const { diff } = await request(`/api/runs/${encodeURIComponent(state.selectedRunId)}/diff/${encodeURIComponent(baselineId)}`);
    renderDiff(diff);
  } catch (err) {
    toast(`Diff error: ${err.message}`);
  }
}

function renderDiff(diff) {
  const container = $("#diff-content");
  if (!container) return;
  const matchClass = diff.verdict_match ? "good" : "fault";
  let html = `
    <div class="diff-summary ${matchClass}">
      <strong>Verdict: Current [${escapeHtml(diff.run_a.verdict)}] vs Baseline [${escapeHtml(diff.run_b.verdict)}]</strong>
      <small>${diff.verdict_match ? "Verdicts match" : "Verdicts diverged"}</small>
    </div>
  `;
  if (diff.first_event_divergence) {
    const d = diff.first_event_divergence;
    html += `
      <div class="diff-divergence">
        <strong>Earliest event divergence at step #${escapeHtml(d.sequence)}:</strong>
        <div>Current: <code>${escapeHtml(d.run_a_event)}</code></div>
        <div>Baseline: <code>${escapeHtml(d.run_b_event)}</code></div>
      </div>
    `;
  } else {
    html += '<div class="diff-divergence"><strong>No event stream divergence detected.</strong></div>';
  }
  html += '<table class="diff-table"><thead><tr><th>Invariant</th><th>Current Run</th><th>Baseline Run</th><th>Status</th></tr></thead><tbody>';
  diff.invariants.forEach((inv) => {
    html += `
      <tr class="${inv.diverged ? "row-diverged" : ""}">
        <td><strong>${escapeHtml(inv.name)}</strong></td>
        <td><span class="verdict-badge ${escapeHtml(inv.run_a_status)}">${escapeHtml(inv.run_a_status)}</span></td>
        <td><span class="verdict-badge ${escapeHtml(inv.run_b_status)}">${escapeHtml(inv.run_b_status)}</span></td>
        <td>${inv.diverged ? '<span class="diff-tag diverged">Diverged</span>' : '<span class="diff-tag matched">Matched</span>'}</td>
      </tr>
    `;
  });
  html += "</tbody></table>";
  container.innerHTML = html;
}

document.querySelectorAll(".suite-run-btn").forEach((btn) => {
  btn.addEventListener("click", () => runSuite(btn.dataset.implementation));
});
$("#diff-toggle-btn")?.addEventListener("click", () => {
  const panel = $("#run-diff-card");
  if (panel) {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) updateDiffBaselineSelect();
  }
});
$("#diff-execute-btn")?.addEventListener("click", executeRunDiff);

initialize();
