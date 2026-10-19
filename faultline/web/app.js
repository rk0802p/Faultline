const $ = (selector) => document.querySelector(selector);
const state = { scenarios: [], runs: [], selectedRunId: null, pollTimer: null };

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
  $("#run-overview").innerHTML = [
    ["Run", run.id], ["Order", summary.order_id || run.scenario.order_id],
    ["Attempts", summary.attempts ?? "—"], ["Charges", summary.charge_count ?? "—"],
  ].map(([label, value]) => `<span class="overview-chip">${escapeHtml(label)}<strong>${escapeHtml(value)}</strong></span>`).join("");
  const charges = summary.charges || [];
  $("#charge-list").innerHTML = charges.length ? charges.map((charge) => `
    <span class="charge-chip"><i></i><strong>${escapeHtml(charge.id)}</strong><span>${escapeHtml((charge.amount_cents / 100).toLocaleString(undefined, { style: "currency", currency: "USD" }))}</span><small>${escapeHtml(charge.status)}</small></span>`).join("")
    : '<span class="ledger-empty">No committed charge recorded yet.</span>';

  $("#assertion-list").innerHTML = run.assertions.length ? run.assertions.map((item) => `
    <div class="assertion-row"><span class="assertion-state ${escapeHtml(item.status)}">${item.status === "passed" ? "✓" : "×"}</span>
      <div class="assertion-copy"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.observed)}</small></div></div>`).join("")
    : '<div class="assertion-row"><span class="assertion-state">·</span><div class="assertion-copy"><strong>Waiting for evaluation</strong><small>Assertions appear when the run finishes.</small></div></div>';

  $("#event-list").innerHTML = run.events.map((event) => {
    const tone = event.type.includes("DROPPED") || event.type.includes("UNKNOWN") ? "fault" :
      (event.type.includes("COMPLETED") || event.type.includes("COMMITTED") ? "good" : "");
    const detail = Object.entries(event.payload || {}).map(([key, value]) => `${key}: ${value}`).join(" · ");
    return `<li class="event-item ${tone}"><strong>${String(event.sequence).padStart(2, "0")} · ${escapeHtml(event.type.replaceAll("_", " "))}</strong><small>${escapeHtml(detail || formatTime(event.recorded_at))}</small></li>`;
  }).join("") || '<li class="event-item"><strong>Waiting for events</strong><small>The runner is preparing the local test environment.</small></li>';
  $("#copy-run-id").dataset.runId = run.id;
  $("#download-repro").href = `/api/runs/${encodeURIComponent(run.id)}/reproduce`;
  if (run.status === "queued" || run.status === "running") schedulePoll(run.id);
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
    await refreshRuns();
    if (state.selectedRunId === runId) await openRun(runId);
  }, 650);
}

async function refreshRuns() {
  const { runs } = await request("/api/runs");
  state.runs = runs;
  renderHistory();
}

async function launchRun(implementation) {
  const buttons = [...document.querySelectorAll(".run-button")];
  buttons.forEach((button) => { button.disabled = true; });
  try {
    const scenario = state.scenarios.find((item) => item.id === $("#scenario-select").value) || state.scenarios[0];
    if (!scenario) throw new Error("No runnable scenario is available");
    const { run_id: runId } = await request("/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario_id: scenario.id, implementation }),
    });
    state.selectedRunId = runId;
    await refreshRuns();
    await openRun(runId);
    toast(`${implementation} run started`);
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

function setScenarioEditor() {
  const selected = state.scenarios.find((item) => item.id === $("#scenario-select").value);
  const definition = selected?.definition || { scenario_version: 1, name: "new-payment-scenario", description: "", order_id: "order-new", amount_cents: 2500, fault: "payment_response_lost_after_commit", retries: 1, assertion: "no_duplicate_charge" };
  $("#scenario-json").value = JSON.stringify(definition, null, 2);
  $("#save-scenario").textContent = selected ? "Save new version" : "Create scenario";
}

async function submitScenario(save) {
  const feedback = $("#scenario-feedback");
  try {
    const definition = JSON.parse($("#scenario-json").value);
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

document.querySelectorAll(".run-button").forEach((button) => button.addEventListener("click", () => launchRun(button.dataset.implementation)));
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

initialize();
