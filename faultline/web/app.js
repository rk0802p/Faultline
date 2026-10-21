const $ = (selector) => document.querySelector(selector);
const state = {
  scenarios: [],
  runs: [],
  selectedRunId: null,
  pollTimer: null,
  replay: { currentStep: 0, timer: null, events: [] },
  timelineFilter: "all",
  searchQuery: "",
  annotations: [],
  tasks: [],
};

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
  const cleanup = run.cleanup || { status: "untracked", owned_resources: [] };
  const cleanupLabel = cleanup.status ? cleanup.status.toUpperCase() : "UNTRACKED";
  const ownedCount = cleanup.owned_resources?.length || 0;
  $("#run-overview").innerHTML = [
    ["Run", run.id],
    ["Order", summary.order_id || run.scenario.order_id],
    [isWebhook ? "Deliveries" : "Attempts", isWebhook ? (summary.deliveries ?? "—") : (summary.attempts ?? "—")],
    [isWebhook ? "Fulfillments" : "Charges", isWebhook ? (summary.fulfillment_count ?? "—") : (summary.charge_count ?? "—")],
    ["Cleanup (Spec §6.14)", `${cleanupLabel} (${ownedCount} owned)`],
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

  renderEventList(run.events);
  $("#copy-run-id").dataset.runId = run.id;
  $("#download-repro").href = `/api/runs/${encodeURIComponent(run.id)}/reproduce`;
  state.replay.events = run.events;
  setReplayStep(run.events.length, false);
  const active = ["queued", "running", "cancel_requested"].includes(run.status);
  $("#cancel-run").hidden = !["queued", "running"].includes(run.status);
  $("#cancel-run").dataset.runId = run.id;
  if (active) schedulePoll(run.id);
}

function renderEventList(events) {
  const filtered = (events || []).filter((event) => {
    const q = (state.searchQuery || "").toLowerCase();
    if (q) {
      const match = (event.type + " " + JSON.stringify(event.payload || {})).toLowerCase();
      if (!match.includes(q)) return false;
    }
    const f = state.timelineFilter;
    if (f === "faults") {
      return event.type.includes("FAULT") || event.type.includes("DROPPED") || event.type.includes("UNKNOWN") || event.type.includes("FAILED");
    }
    if (f === "side_effects") {
      return event.type.includes("COMMITTED") || event.type.includes("FULFILLMENT") || event.type.includes("REFUND") || event.type.includes("COMPENSATION");
    }
    if (f === "lifecycle") {
      return event.type.includes("RUN_") || event.type.includes("CHECKOUT_STARTED") || event.type.includes("CHECKOUT_REACHED") || event.type.includes("ASSERTIONS");
    }
    return true;
  });

  const listEl = $("#event-list");
  if (!listEl) return;
  listEl.innerHTML = filtered.map((event) => {
    const tone = event.type.includes("DROPPED") || event.type.includes("UNKNOWN") || event.type.includes("FAILED") ? "fault" :
      (event.type.includes("COMPLETED") || event.type.includes("COMMITTED") ? "good" : "");
    const detail = Object.entries(event.payload || {}).map(([key, value]) => `${key}: ${value}`).join(" · ");
    return `<li class="event-item ${tone}" id="event-${event.sequence}"><strong>${String(event.sequence).padStart(2, "0")} · ${escapeHtml(event.type.replaceAll("_", " "))}</strong><small>${escapeHtml(detail || formatTime(event.recorded_at))}</small></li>`;
  }).join("") || '<li class="event-item"><strong>No matching events</strong><small>The event stream or filter returned no matches.</small></li>';
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
    await Promise.all([loadAnnotations(runId), loadTasks(runId)]);
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
  await refreshCoverage();
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

async function loadTemplates() {
  const container = $("#template-grid");
  if (!container) return;
  try {
    const { templates } = await request("/api/templates");
    container.innerHTML = templates.map((tpl) => `
      <article class="scenario-card template-card" style="display:flex;flex-direction:column;justify-content:space-between">
        <div>
          <div class="scenario-card-top">
            <span class="scenario-symbol">⚡</span>
            <span class="scenario-tag">${escapeHtml(tpl.category.toUpperCase())}</span>
          </div>
          <h3>${escapeHtml(tpl.name)}</h3>
          <p class="scenario-description">${escapeHtml(tpl.description)}</p>
          <div style="font-size:10px;color:#78869c;margin:8px 0">
            <strong>Invariants:</strong> ${tpl.expected_invariants.map((inv) => `<code style="background:#eef2f8;padding:1px 4px;border-radius:3px">${escapeHtml(inv)}</code>`).join(" ")}
          </div>
          <div style="font-size:10px;color:#8592a6">
            <strong>Cleanup:</strong> ${escapeHtml(tpl.cleanup_strategy)}
          </div>
        </div>
        <div style="margin-top:14px">
          <button class="button button-primary instantiate-btn" data-template-id="${escapeHtml(tpl.id)}" style="width:100%">
            <span>+</span> Instantiate scenario
          </button>
        </div>
      </article>
    `).join("");

    container.querySelectorAll(".instantiate-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const templateId = btn.dataset.templateId;
        const scId = `${templateId.replace("-tpl", "")}-${Date.now().toString(36)}`;
        try {
          btn.disabled = true;
          btn.textContent = "Instantiating...";
          await request(`/api/templates/${encodeURIComponent(templateId)}/instantiate`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ scenario_id: scId }),
          });
          toast(`Created scenario '${scId}' from template!`);
          const res = await request("/api/scenarios");
          state.scenarios = res.scenarios;
          $("#scenario-count").textContent = String(state.scenarios.length);
          const select = $("#scenario-select");
          select.innerHTML = '<option value="">Create a new scenario…</option>' + state.scenarios.map((scenario) => `<option value="${escapeHtml(scenario.id)}">${escapeHtml(scenario.name)} · v${scenario.version}</option>`).join("");
          select.value = scId;
          setScenarioEditor();
        } catch (err) {
          toast(`Failed: ${err.message}`);
        } finally {
          btn.disabled = false;
          btn.innerHTML = "<span>+</span> Instantiate scenario";
        }
      });
    });
  } catch (err) {
    console.error("Failed to load templates", err);
  }
}

function setupOpenApiImporter() {
  const btn = $("#import-openapi-btn");
  const input = $("#openapi-input");
  const results = $("#openapi-results");
  if (!btn || !input || !results) return;

  btn.addEventListener("click", async () => {
    const raw = input.value.trim();
    if (!raw) {
      toast("Please paste an OpenAPI JSON specification");
      return;
    }
    try {
      btn.disabled = true;
      btn.textContent = "Analyzing...";
      const { workflow } = await request("/api/workflows/import-openapi", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: raw,
      });
      results.hidden = false;
      results.innerHTML = `
        <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;padding:12px">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <strong>${escapeHtml(workflow.title)} (v${escapeHtml(workflow.version)})</strong>
            <span class="impl-badge" style="background:#dbeafe;color:#1e40af">${workflow.endpoints_count} endpoints discovered</span>
          </div>
          <div style="margin-top:8px;font-size:11px">
            <strong>Recommended Failure Scenarios:</strong>
            <ul style="margin:6px 0 0 16px;padding:0">
              ${workflow.recommended_scenarios.map((rec) => `
                <li style="margin-bottom:4px">
                  <strong>${escapeHtml(rec.name)}</strong> · <span style="color:#64748b">${escapeHtml(rec.rationale)}</span>
                </li>
              `).join("")}
            </ul>
          </div>
        </div>
      `;
      toast(`Successfully mapped ${workflow.endpoints_count} endpoints!`);
    } catch (err) {
      toast(`Import failed: ${err.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = "Import workflow";
    }
  });
}

async function checkServicesHealth() {
  const pill = $("#services-health-pill");
  if (!pill) return;
  try {
    const { services } = await request("/api/projects/faultline-demo/services");
    const healthyCount = services.filter((s) => s.status === "healthy").length;
    if (healthyCount === services.length) {
      pill.innerHTML = `<i></i> SERVICES HEALTHY (${healthyCount}/${services.length})`;
      pill.style.background = "#eefaf2";
      pill.style.color = "#15803d";
    } else {
      pill.innerHTML = `<i></i> SERVICES DEGRADED (${healthyCount}/${services.length})`;
      pill.style.background = "#fff1f2";
      pill.style.color = "#be123c";
    }
  } catch {
    pill.innerHTML = `<i></i> SERVICES OFFLINE`;
  }
}

async function loadAuditEvents() {
  const list = $("#audit-log-list");
  if (!list) return;
  try {
    const { audit_events } = await request("/api/projects/faultline-demo/audit-events");
    if (!audit_events.length) {
      list.innerHTML = '<span style="font-size:11px;color:#94a3b8">No audit events recorded yet.</span>';
      return;
    }
    list.innerHTML = audit_events.map((ev) => `
      <div style="font-size:11px;padding:6px 0;border-bottom:1px solid #f1f5f9;display:flex;justify-content:space-between;align-items:center">
        <div>
          <strong style="color:#0f172a">${escapeHtml(ev.action)}</strong>
          <span style="color:#64748b">on ${escapeHtml(ev.target_type)}:<code>${escapeHtml(ev.target_id)}</code></span>
          <small style="color:#94a3b8">by ${escapeHtml(ev.actor)}</small>
        </div>
        <time style="font-size:10px;color:#94a3b8">${escapeHtml(formatTime(ev.created_at))}</time>
      </div>
    `).join("");
  } catch (err) {
    list.innerHTML = `<span style="font-size:11px;color:#ef4444">Failed: ${escapeHtml(err.message)}</span>`;
  }
}

function setupAuditModal() {
  const openBtn = $("#open-audit-btn");
  const closeBtn = $("#close-audit-btn");
  const modal = $("#audit-modal");
  if (!openBtn || !modal) return;
  openBtn.addEventListener("click", async () => {
    modal.hidden = !modal.hidden;
    if (!modal.hidden) await loadAuditEvents();
  });
  closeBtn?.addEventListener("click", () => {
    modal.hidden = true;
  });
}

function setupMetricsModal() {
  const openBtn = $("#open-metrics-btn");
  const closeBtn = $("#close-metrics-btn");
  const modal = $("#metrics-modal");
  const content = $("#metrics-content");
  if (!openBtn || !modal) return;
  openBtn.addEventListener("click", async () => {
    modal.hidden = !modal.hidden;
    if (!modal.hidden) await loadOperationalMetrics();
  });
  closeBtn?.addEventListener("click", () => {
    modal.hidden = true;
  });

  async function loadOperationalMetrics() {
    try {
      content.innerHTML = '<span style="color:#64748b;font-size:11px">Fetching operational metrics...</span>';
      const m = await request("/api/metrics");
      const r = m.runs || {};
      const a = m.assertions || {};
      const w = m.worker_health || {};
      content.innerHTML = `
        <div style="background:#f8fafc;padding:10px;border-radius:6px;border:1px solid #e2e8f0">
          <strong style="font-size:11px;color:#475569">RUNNER &amp; WORKER</strong>
          <div style="font-size:16px;font-weight:700;color:#0f172a;margin-top:4px">${escapeHtml(String(w.status || 'healthy')).toUpperCase()}</div>
          <small style="color:#64748b;font-size:10px">Queue: ${w.queue_depth || 0} · Active: ${w.active_workers || 0}</small>
        </div>
        <div style="background:#f8fafc;padding:10px;border-radius:6px;border:1px solid #e2e8f0">
          <strong style="font-size:11px;color:#475569">RUN EXECUTION STATS</strong>
          <div style="font-size:16px;font-weight:700;color:#0f172a;margin-top:4px">${r.total || 0} runs</div>
          <small style="color:#64748b;font-size:10px">✓ ${r.completed || 0} · ✕ ${r.failed || 0} · ⏱ ${r.timed_out || 0} · ⊘ ${r.cancelled || 0}</small>
        </div>
        <div style="background:#f8fafc;padding:10px;border-radius:6px;border:1px solid #e2e8f0">
          <strong style="font-size:11px;color:#475569">INVARIANT PASS RATE</strong>
          <div style="font-size:16px;font-weight:700;color:#0f172a;margin-top:4px">${a.pass_rate_percent || 100}%</div>
          <small style="color:#64748b;font-size:10px">${a.passed || 0} passed / ${a.total || 0} assertions</small>
        </div>
        <div style="background:#f8fafc;padding:10px;border-radius:6px;border:1px solid #e2e8f0">
          <strong style="font-size:11px;color:#475569">SYSTEM UPTIME</strong>
          <div style="font-size:16px;font-weight:700;color:#0f172a;margin-top:4px">${Math.floor(m.uptime_seconds || 0)}s</div>
          <small style="color:#64748b;font-size:10px">Control plane running</small>
        </div>
      `;
    } catch (err) {
      content.innerHTML = `<span style="color:#dc2626;font-size:11px">Failed to load metrics: ${escapeHtml(err.message)}</span>`;
    }
  }
}

function setupPruneModal() {
  const openBtn = $("#open-prune-btn");
  const closeBtn = $("#close-prune-btn");
  const modal = $("#prune-modal");
  const execBtn = $("#execute-prune-btn");
  const daysInput = $("#prune-days-input");
  const keepBaselinesBox = $("#prune-keep-baselines");
  const dryRunBox = $("#prune-dry-run");
  const resultsDiv = $("#prune-results");
  if (!openBtn || !modal) return;
  openBtn.addEventListener("click", () => {
    modal.hidden = !modal.hidden;
    if (resultsDiv) resultsDiv.hidden = true;
  });
  closeBtn?.addEventListener("click", () => {
    modal.hidden = true;
  });
  execBtn?.addEventListener("click", async () => {
    try {
      execBtn.disabled = true;
      execBtn.textContent = "Pruning...";
      const days = parseInt(daysInput.value, 10) || 30;
      const keepBaselines = keepBaselinesBox.checked;
      const dryRun = dryRunBox.checked;
      const res = await request("/api/projects/faultline-demo/prune", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ max_age_days: days, keep_baselines: keepBaselines, dry_run: dryRun }),
      });
      resultsDiv.hidden = false;
      const mode = res.dry_run ? "Preview (Dry Run)" : "Completed";
      resultsDiv.innerHTML = `<strong>Prune ${mode}:</strong> Pruned ${res.pruned_run_count} runs (older than ${res.cutoff}), preserved ${res.skipped_baselines} baselines.`;
      toast(`Pruning finished: ${res.pruned_run_count} run(s) pruned.`);
      if (!res.dry_run && res.pruned_run_count > 0) {
        await refresh();
      }
    } catch (err) {
      toast(`Prune error: ${err.message}`);
    } finally {
      execBtn.disabled = false;
      execBtn.textContent = "Execute Prune";
    }
  });
}

function setupGenerateFromGaps() {
  const btn = $("#generate-gaps-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    try {
      btn.disabled = true;
      btn.textContent = "Analyzing gaps...";
      const { count } = await request("/api/scenarios/generate-from-gaps", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: "faultline-demo" }),
      });
      if (count > 0) {
        toast(`Generated ${count} new scenario(s) from coverage gaps!`);
        const res = await request("/api/scenarios");
        state.scenarios = res.scenarios;
        $("#scenario-count").textContent = String(state.scenarios.length);
        const select = $("#scenario-select");
        select.innerHTML = '<option value="">Create a new scenario…</option>' + state.scenarios.map((scenario) => `<option value="${escapeHtml(scenario.id)}">${escapeHtml(scenario.name)} · v${scenario.version}</option>`).join("");
      } else {
        toast("No new gaps found to generate.");
      }
      await refreshCoverage();
    } catch (err) {
      toast(`Failed: ${err.message}`);
    } finally {
      btn.disabled = false;
      btn.innerHTML = "<span>⚡</span> Generate scenarios from coverage gaps";
    }
  });
}

async function loadRunTelemetry(runId) {
  const panel = $("#telemetry-card");
  const traceIdEl = $("#telemetry-trace-id");
  const countEl = $("#telemetry-span-count");
  const listEl = $("#telemetry-spans-list");
  if (!panel || !traceIdEl || !listEl) return;
  try {
    listEl.innerHTML = '<span style="font-size:11px;color:#94a3b8">Loading OpenTelemetry trace...</span>';
    const data = await request(`/api/runs/${encodeURIComponent(runId)}/telemetry`);
    traceIdEl.textContent = data.traceId;
    countEl.textContent = String(data.spanCount);

    const copyBtn = $("#copy-traceparent-btn");
    if (copyBtn) {
      copyBtn.onclick = () => {
        const traceparent = `00-${data.traceId}-${data.rootSpanId || data.traceId.slice(0, 16)}-01`;
        navigator.clipboard.writeText(traceparent).then(() => toast("Copied traceparent to clipboard"));
      };
    }

    if (!data.spans || !data.spans.length) {
      listEl.innerHTML = '<span style="font-size:11px;color:#94a3b8">No spans recorded for this run.</span>';
      return;
    }

    const maxDuration = Math.max(...data.spans.map((s) => s.durationMs || 1), 1);
    listEl.innerHTML = data.spans.map((s) => {
      const isRoot = !s.parentSpanId;
      const barWidth = Math.max(8, Math.round(((s.durationMs || 1) / maxDuration) * 100));
      const statusColor = s.statusCode === "OK" ? "#16a34a" : "#dc2626";
      const kindColor = s.kind === "SERVER" ? "#2563eb" : (s.kind === "CLIENT" ? "#7c3aed" : "#0d9488");
      const attrBadges = Object.entries(s.attributes || {})
        .slice(0, 4)
        .map(([k, v]) => `<span style="font-size:9px;background:#f8fafc;border:1px solid #e2e8f0;padding:1px 4px;border-radius:3px;color:#64748b">${escapeHtml(k)}=${escapeHtml(String(v))}</span>`)
        .join(" ");

      return `
        <div style="padding:6px 8px;border-radius:6px;background:${isRoot ? '#f8fafc' : '#ffffff'};border:1px solid #e2e8f0;font-size:11px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
            <div style="display:flex;align-items:center;gap:6px">
              <span style="font-size:9px;font-weight:600;padding:1px 5px;border-radius:3px;background:${kindColor}15;color:${kindColor}">${escapeHtml(s.kind)}</span>
              <strong style="color:#0f172a">${escapeHtml(s.name)}</strong>
              <code style="font-size:9px;color:#94a3b8">span:${escapeHtml(s.spanId.slice(0, 8))}</code>
            </div>
            <div style="display:flex;align-items:center;gap:8px">
              <span style="font-weight:600;color:${statusColor}">${escapeHtml(s.statusCode)}</span>
              <span style="color:#64748b;font-size:10px">${s.durationMs}ms</span>
            </div>
          </div>
          <div style="background:#f1f5f9;height:4px;border-radius:2px;overflow:hidden;margin:4px 0">
            <div style="background:${kindColor};width:${barWidth}%;height:100%"></div>
          </div>
          <div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:4px">${attrBadges}</div>
        </div>
      `;
    }).join("");
  } catch (err) {
    listEl.innerHTML = `<span style="font-size:11px;color:#ef4444">Failed loading telemetry: ${escapeHtml(err.message)}</span>`;
  }
}

function setupTelemetry() {
  const btn = $("#telemetry-btn");
  const closeBtn = $("#close-telemetry-btn");
  const card = $("#telemetry-card");
  if (!btn || !card) return;
  btn.addEventListener("click", async () => {
    card.hidden = !card.hidden;
    if (!card.hidden && state.selectedRunId) {
      await loadRunTelemetry(state.selectedRunId);
    }
  });
  closeBtn?.addEventListener("click", () => { card.hidden = true; });
}

function setupBranching() {
  const btn = $("#branch-checkpoint-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    if (!state.selectedRunId) return;
    const impl = window.prompt("Fork alternative run from latest checkpoint.\\nEnter implementation ('fixed' or 'vulnerable'):", "fixed");
    if (!impl || !["fixed", "vulnerable"].includes(impl.trim().toLowerCase())) return;
    try {
      btn.disabled = true;
      toast("Branching new run from checkpoint...");
      const { run } = await request(`/api/runs/${encodeURIComponent(state.selectedRunId)}/branch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ overrides: { implementation: impl.trim().toLowerCase() } }),
      });
      toast(`Branched new run: ${run.id.slice(0, 8)}`);
      await refreshRuns();
      await openRun(run.id);
    } catch (err) {
      toast(`Branch error: ${err.message}`);
    } finally {
      btn.disabled = false;
    }
  });
}

function setupBaseline() {
  const btn = $("#set-baseline-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    if (!state.selectedRunId) return;
    try {
      btn.disabled = true;
      await request(`/api/runs/${encodeURIComponent(state.selectedRunId)}/baseline`, { method: "POST" });
      toast("Run designated as scenario baseline 🎯");
      await refreshRuns();
      await openRun(state.selectedRunId);
    } catch (err) {
      toast(`Baseline error: ${err.message}`);
    } finally {
      btn.disabled = false;
    }
  });
}

async function initialize() {
  try {
    const [scenarioResponse] = await Promise.all([request("/api/scenarios"), refreshRuns(), refreshCoverage(), loadTemplates(), checkServicesHealth()]);
    state.scenarios = scenarioResponse.scenarios;
    $("#scenario-count").textContent = String(state.scenarios.length);
    const select = $("#scenario-select");
    select.innerHTML = '<option value="">Create a new scenario…</option>' + state.scenarios.map((scenario) => `<option value="${escapeHtml(scenario.id)}">${escapeHtml(scenario.name)} · v${scenario.version}</option>`).join("");
    select.value = state.scenarios[0]?.id || "";
    setScenarioEditor();
    setupOpenApiImporter();
    setupAuditModal();
    setupMetricsModal();
    setupPruneModal();
    setupGenerateFromGaps();
    setupTelemetry();
    setupBranching();
    setupBaseline();
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

async function loadAnnotations(runId) {
  try {
    const { annotations } = await request(`/api/runs/${encodeURIComponent(runId)}/annotations`);
    state.annotations = annotations;
    renderAnnotations();
  } catch {}
}

function renderAnnotations() {
  const list = $("#annotation-list");
  if (!list) return;
  if (!state.annotations.length) {
    list.innerHTML = '<span class="empty-row" style="padding:10px;font-size:10px">No notes added yet. Record an observation below.</span>';
    return;
  }
  list.innerHTML = state.annotations.map((a) => `
    <div class="annotation-item">
      <div class="annotation-meta"><span><strong>${escapeHtml(a.author)}</strong></span><span>${escapeHtml(formatTime(a.created_at))}</span></div>
      <div>${escapeHtml(a.comment)}</div>
    </div>
  `).join("");
}

async function handleAddAnnotation(event) {
  event.preventDefault();
  if (!state.selectedRunId) return;
  const authorInput = $("#annotation-author");
  const textInput = $("#annotation-text");
  const comment = textInput.value.trim();
  if (!comment) return;
  try {
    const { annotation } = await request(`/api/runs/${encodeURIComponent(state.selectedRunId)}/annotations`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ author: authorInput.value.trim() || "Developer", comment }),
    });
    state.annotations.push(annotation);
    renderAnnotations();
    textInput.value = "";
    toast("Note added to run");
  } catch (err) {
    toast(`Error adding note: ${err.message}`);
  }
}

async function loadTasks(runId) {
  try {
    const { tasks } = await request(`/api/runs/${encodeURIComponent(runId)}/tasks`);
    state.tasks = tasks;
    renderTasks();
  } catch {}
}

function renderTasks() {
  const list = $("#task-list");
  if (!list) return;
  if (!state.tasks.length) {
    list.innerHTML = '<span class="empty-row" style="padding:10px;font-size:10px">No tasks created. Add an action item below.</span>';
    return;
  }
  list.innerHTML = state.tasks.map((t) => `
    <div class="task-item ${t.status === 'resolved' ? 'resolved' : ''}">
      <input type="checkbox" ${t.status === 'resolved' ? 'checked' : ''} data-task-id="${escapeHtml(t.id)}">
      <span class="task-badge ${escapeHtml(t.priority)}">${escapeHtml(t.priority)}</span>
      <span style="flex:1"><strong>${escapeHtml(t.title)}</strong></span>
      <small style="color:#8592a6">${escapeHtml(t.assignee)}</small>
    </div>
  `).join("");
  list.querySelectorAll("input[type=checkbox]").forEach((cb) => {
    cb.addEventListener("change", async (e) => {
      const taskId = e.target.dataset.taskId;
      const newStatus = e.target.checked ? "resolved" : "open";
      try {
        await request(`/api/tasks/${encodeURIComponent(taskId)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: newStatus }),
        });
        const task = state.tasks.find((t) => t.id === taskId);
        if (task) task.status = newStatus;
        renderTasks();
        toast(`Task marked ${newStatus}`);
      } catch (err) {
        toast(`Error: ${err.message}`);
      }
    });
  });
}

async function handleAddTask(event) {
  event.preventDefault();
  if (!state.selectedRunId) return;
  const titleInput = $("#task-title");
  const prioSelect = $("#task-priority");
  const title = titleInput.value.trim();
  if (!title) return;
  try {
    const { task } = await request(`/api/runs/${encodeURIComponent(state.selectedRunId)}/tasks`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, priority: prioSelect.value, assignee: "Developer" }),
    });
    state.tasks.unshift(task);
    renderTasks();
    titleInput.value = "";
    toast("Action item created");
  } catch (err) {
    toast(`Error: ${err.message}`);
  }
}

async function refreshCoverage() {
  try {
    const data = await request("/api/coverage");
    renderCoverage(data);
  } catch {}
}

function renderCoverage(data) {
  const pct = $("#coverage-pct");
  const tested = $("#coverage-tested");
  const passed = $("#coverage-passed");
  const defects = $("#coverage-defects");
  const tbody = $("#coverage-body");
  if (!tbody || !data) return;

  if (pct) pct.textContent = data.summary.coverage_percentage + "%";
  if (tested) tested.textContent = data.summary.tested_cells;
  if (passed) passed.textContent = data.summary.passed_cells;
  if (defects) defects.textContent = data.summary.defect_exposed_cells;

  tbody.innerHTML = data.matrix.map((row) => {
    const cellsHtml = row.cells.map((c) => {
      let label = "Untested";
      let icon = "○";
      if (!c.applicable) {
        label = "N/A";
        icon = "—";
      } else if (c.status === "passed") {
        label = `Passed (${c.runs_count})`;
        icon = "✓";
      } else if (c.status === "defect_exposed") {
        label = `Exposed (${c.runs_count})`;
        icon = "⌁";
      }
      return `<td><span class="cov-cell ${c.status}">${icon} ${escapeHtml(label)}</span></td>`;
    }).join("");
    return `
      <tr>
        <td><strong>${escapeHtml(row.step.name)}</strong><br><small style="color:#8592a6">${escapeHtml(row.step.description)}</small></td>
        ${cellsHtml}
      </tr>
    `;
  }).join("");
}

document.querySelectorAll(".filter-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.querySelectorAll(".filter-chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    state.timelineFilter = chip.dataset.filter;
    renderEventList(state.replay.events);
  });
});

$("#event-search")?.addEventListener("input", (e) => {
  state.searchQuery = e.target.value.trim();
  renderEventList(state.replay.events);
});

$("#annotation-form")?.addEventListener("submit", handleAddAnnotation);
$("#task-form")?.addEventListener("submit", handleAddTask);

initialize();
