/**
 * Faultline — A laboratory for software failures
 * Client Application Logic
 * Raycast-style Command Palette + PostHog-inspired technical personality
 * Scenario-driven, generic, and decoupled from hardcoded workflows.
 */

// Global selector helpers
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

// Application State
const state = {
  activeView: "overview",
  activeProjectId: "faultline-demo",
  scenarios: [],
  scenariosLoading: false,
  scenariosError: null,
  runs: [],
  selectedRunId: null,
  currentRun: null,
  selectedForCompare: new Set(),
  annotations: [],
  tasks: [],
  editorDefinition: null,
  pollTimer: null,
  timelineFilter: "all",
  searchQuery: "",
  replay: {
    events: [],
    currentStep: 1,
    timer: null,
  },
  commandPalette: {
    open: false,
    selectedIndex: 0,
    filteredItems: [],
  },
};

// =========================================================
// API CLIENT RESOLUTION
// =========================================================
// Resolves the centralized API client defined in api.js,
// with safe fallback for any execution environment.
const apiClient = (typeof window !== "undefined" && window.api)
  ? window.api
  : (typeof api !== "undefined" ? api : null);

/**
 * Fallback request helper in case api.js is not loaded
 */
async function fallbackRequest(path, options = {}) {
  if (apiClient && typeof apiClient.request === "function") {
    return apiClient.request(path, options);
  }
  const headers = {
    "Content-Type": "application/json",
    ...(options.headers || {}),
  };
  let body = options.body;
  if (body !== undefined && typeof body === "object" && !(body instanceof FormData)) {
    body = JSON.stringify(body);
  }
  const response = await fetch(path, { ...options, headers, body });
  if (response.status === 204) return null;
  const text = await response.text();
  let parsed;
  try {
    parsed = text ? JSON.parse(text) : {};
  } catch {
    parsed = { raw: text };
  }
  if (!response.ok) {
    const message = parsed.error || `HTTP ${response.status} ${response.statusText}`;
    throw new Error(message);
  }
  return parsed;
}

// Proxy object guaranteeing all API operations route through centralized contracts
const apiService = {
  getScenarios: () => (apiClient ? apiClient.getScenarios() : fallbackRequest("/api/scenarios")),
  getScenario: (id) => (apiClient ? apiClient.getScenario(id) : fallbackRequest(`/api/scenarios/${encodeURIComponent(id)}`)),
  createScenario: (def) => (apiClient ? apiClient.createScenario(def) : fallbackRequest("/api/scenarios", { method: "POST", body: { definition: def } })),
  saveScenarioVersion: (id, def) => (apiClient ? apiClient.saveScenarioVersion(id, def) : fallbackRequest(`/api/scenarios/${encodeURIComponent(id)}/versions`, { method: "POST", body: { definition: def } })),
  validateScenario: (def) => (apiClient ? apiClient.validateScenario(def) : fallbackRequest("/api/scenarios/validate", { method: "POST", body: { definition: def } })),
  generateFromGaps: (pId) => (apiClient ? apiClient.generateFromGaps(pId) : fallbackRequest("/api/scenarios/generate-from-gaps", { method: "POST", body: { project_id: pId } })),

  getRuns: () => (apiClient ? apiClient.getRuns() : fallbackRequest("/api/runs")),
  getRun: (id) => (apiClient ? apiClient.getRun(id) : fallbackRequest(`/api/runs/${encodeURIComponent(id)}`)),
  launchRun: (scId, impl) => (apiClient ? apiClient.launchRun(scId, impl) : fallbackRequest("/api/runs", { method: "POST", body: { scenario_id: scId, implementation: impl } })),
  cancelRun: (id) => (apiClient ? apiClient.cancelRun(id) : fallbackRequest(`/api/runs/${encodeURIComponent(id)}/cancel`, { method: "POST", body: {} })),
  branchRun: (id, overrides) => (apiClient ? apiClient.branchRun(id, overrides) : fallbackRequest(`/api/runs/${encodeURIComponent(id)}/branch`, { method: "POST", body: { overrides } })),
  setBaseline: (id) => (apiClient ? apiClient.setBaseline(id) : fallbackRequest(`/api/runs/${encodeURIComponent(id)}/baseline`, { method: "POST" })),
  diffRuns: (idA, idB) => (apiClient ? apiClient.diffRuns(idA, idB) : fallbackRequest(`/api/runs/${encodeURIComponent(idA)}/diff/${encodeURIComponent(idB)}`)),
  getTelemetry: (id) => (apiClient ? apiClient.getTelemetry(id) : fallbackRequest(`/api/runs/${encodeURIComponent(id)}/telemetry`)),

  getAnnotations: (id) => (apiClient ? apiClient.getAnnotations(id) : fallbackRequest(`/api/runs/${encodeURIComponent(id)}/annotations`)),
  addAnnotation: (id, author, comment) => (apiClient ? apiClient.addAnnotation(id, author, comment) : fallbackRequest(`/api/runs/${encodeURIComponent(id)}/annotations`, { method: "POST", body: { author, comment } })),
  getTasks: (id) => (apiClient ? apiClient.getTasks(id) : fallbackRequest(`/api/runs/${encodeURIComponent(id)}/tasks`)),
  addTask: (id, title, prio, assignee) => (apiClient ? apiClient.addTask(id, title, prio, assignee) : fallbackRequest(`/api/runs/${encodeURIComponent(id)}/tasks`, { method: "POST", body: { title, priority: prio, assignee } })),
  updateTask: (tId, status) => (apiClient ? apiClient.updateTask(tId, status) : fallbackRequest(`/api/tasks/${encodeURIComponent(tId)}`, { method: "PATCH", body: { status } })),

  getSuite: (sId) => (apiClient ? apiClient.getSuite(sId) : fallbackRequest(`/api/suites/${encodeURIComponent(sId)}`)),
  getTemplates: () => (apiClient ? apiClient.getTemplates() : fallbackRequest("/api/templates")),
  instantiateTemplate: (tId, scId) => (apiClient ? apiClient.instantiateTemplate(tId, scId) : fallbackRequest(`/api/templates/${encodeURIComponent(tId)}/instantiate`, { method: "POST", body: { scenario_id: scId } })),

  getCoverage: () => (apiClient ? apiClient.getCoverage() : fallbackRequest("/api/coverage")),
  getMetrics: () => (apiClient ? apiClient.getMetrics() : fallbackRequest("/api/metrics")),
  getProjects: () => (apiClient ? apiClient.getProjects() : fallbackRequest("/api/projects")),
  getAuditEvents: (pId) => (apiClient ? apiClient.getAuditEvents(pId) : fallbackRequest(`/api/projects/${encodeURIComponent(pId)}/audit-events`)),
  pruneRuns: (pId, days, keep, dry) => (apiClient ? apiClient.pruneRuns(pId, days, keep, dry) : fallbackRequest(`/api/projects/${encodeURIComponent(pId)}/prune`, { method: "POST", body: { max_age_days: days, keep_baselines: keep, dry_run: dry } })),
  importOpenApi: (spec) => (apiClient ? apiClient.importOpenApi(spec) : fallbackRequest("/api/workflows/import-openapi", { method: "POST", headers: { "Content-Type": "application/json" }, body: spec })),
};

// Utility string escaping
function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function toast(message) {
  const element = $("#toast");
  if (!element) return;
  element.textContent = message;
  element.classList.add("show");
  window.clearTimeout(element._timer);
  element._timer = window.setTimeout(() => element.classList.remove("show"), 3200);
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (isNaN(date.getTime())) return String(value);
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// =========================================================
// VIEW NAVIGATION (5 SIGNATURE SCREENS)
// =========================================================
function switchView(viewName) {
  const validViews = ["overview", "studio", "liverun", "investigation", "map"];
  if (!validViews.includes(viewName)) viewName = "overview";
  state.activeView = viewName;

  if (window.location.hash.replace("#", "") !== viewName) {
    history.replaceState(null, "", `#${viewName}`);
  }

  $$(".nav-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.view === viewName);
  });

  $$(".page-view").forEach((view) => {
    view.classList.toggle("active", view.id === `view-${viewName}`);
  });

  const breadcrumb = $("#topbar-current-view");
  if (breadcrumb) {
    const titles = {
      overview: "OVERVIEW",
      studio: "EXPERIMENT STUDIO",
      liverun: "LIVE RUN TIMELINE",
      investigation: "INVESTIGATION",
      map: "RELIABILITY MAP",
    };
    breadcrumb.textContent = titles[viewName] || "OVERVIEW";
  }

  window.scrollTo({ top: 0, behavior: "smooth" });
}

function setupViewNavigation() {
  $$(".nav-item").forEach((item) => {
    item.addEventListener("click", (e) => {
      e.preventDefault();
      const view = item.dataset.view;
      if (view) switchView(view);
    });
  });

  $$("[data-switch-view]").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.preventDefault();
      const target = el.dataset.switchView;
      if (target) switchView(target);
    });
  });

  window.addEventListener("hashchange", () => {
    const hash = window.location.hash.replace("#", "");
    if (hash) switchView(hash);
  });

  const initialHash = window.location.hash.replace("#", "");
  if (initialHash) switchView(initialHash);
}

// =========================================================
// RAYCAST-STYLE COMMAND PALETTE (⌘K / Ctrl+K)
// SCENARIO-DRIVEN DYNAMIC ACTIONS
// =========================================================
function getCommandItems() {
  const items = [
    // Navigation
    { group: "Navigation", title: "Go to Overview (The Control Room)", shortcut: "G O", action: () => switchView("overview") },
    { group: "Navigation", title: "Go to Experiment Studio", shortcut: "G S", action: () => switchView("studio") },
    { group: "Navigation", title: "Go to Live Run Timeline", shortcut: "G L", action: () => switchView("liverun") },
    { group: "Navigation", title: "Go to Run Investigation", shortcut: "G I", action: () => switchView("investigation") },
    { group: "Navigation", title: "Go to Reliability Map & Coverage", shortcut: "G M", action: () => switchView("map") },

    // Primary Actions
    { group: "Actions", title: "Run Critical Integrations Suite (Fixed)", shortcut: "R S", action: () => runSuite("fixed") },
    { group: "Actions", title: "Run Critical Integrations Suite (Vulnerable)", shortcut: "R V", action: () => runSuite("vulnerable") },
    { group: "Actions", title: "Generate Scenarios from Coverage Gaps", shortcut: "G G", action: () => $("#generate-gaps-btn")?.click() },
    { group: "Actions", title: "Open Operational Metrics", shortcut: "O M", action: () => $("#open-metrics-btn")?.click() },
    { group: "Actions", title: "Open Retention Pruning Policy", shortcut: "O P", action: () => $("#open-prune-btn")?.click() },
    { group: "Actions", title: "Open Governance Audit Log", shortcut: "O A", action: () => $("#open-audit-btn")?.click() },
  ];

  // Dynamic Scenarios from Backend Registry
  if (state.scenarios && state.scenarios.length > 0) {
    state.scenarios.forEach((sc) => {
      const name = sc.name || sc.id;
      items.push({
        group: "Registered Scenarios",
        title: `Run '${name}' (Vulnerable)`,
        action: () => launchRun("vulnerable", sc.id),
      });
      items.push({
        group: "Registered Scenarios",
        title: `Run '${name}' (Fixed)`,
        action: () => launchRun("fixed", sc.id),
      });
    });
  }

  // Dynamic Recent Runs
  state.runs.slice(0, 5).forEach((r) => {
    items.push({
      group: "Recent Runs",
      title: `Inspect ${r.scenario} · ${r.implementation} (${r.verdict})`,
      shortcut: r.id.slice(0, 8),
      action: async () => {
        await openRun(r.id);
        switchView("liverun");
      },
    });
  });

  return items;
}

function openCommandPalette() {
  const backdrop = $("#command-palette-backdrop");
  const input = $("#command-input");
  if (!backdrop || !input) return;
  state.commandPalette.open = true;
  state.commandPalette.selectedIndex = 0;
  backdrop.classList.add("open");
  input.value = "";
  renderCommandResults("");
  input.focus();
}

function closeCommandPalette() {
  const backdrop = $("#command-palette-backdrop");
  if (!backdrop) return;
  state.commandPalette.open = false;
  backdrop.classList.remove("open");
}

function renderCommandResults(query) {
  const resultsEl = $("#command-results");
  if (!resultsEl) return;
  const q = (query || "").toLowerCase().trim();
  const allItems = getCommandItems();

  const filtered = allItems.filter((item) =>
    !q || item.title.toLowerCase().includes(q) || item.group.toLowerCase().includes(q)
  );
  state.commandPalette.filteredItems = filtered;
  if (state.commandPalette.selectedIndex >= filtered.length) {
    state.commandPalette.selectedIndex = 0;
  }

  if (!filtered.length) {
    resultsEl.innerHTML = '<div style="padding:16px;text-align:center;color:var(--ink-400);font-size:12px">No matching commands found.</div>';
    return;
  }

  const groups = {};
  filtered.forEach((item, idx) => {
    if (!groups[item.group]) groups[item.group] = [];
    groups[item.group].push({ ...item, originalIndex: idx });
  });

  let html = "";
  Object.entries(groups).forEach(([groupName, items]) => {
    html += `<div class="command-group-title">${escapeHtml(groupName)}</div>`;
    items.forEach((item) => {
      const isSelected = item.originalIndex === state.commandPalette.selectedIndex;
      html += `
        <div class="command-item ${isSelected ? 'selected' : ''}" data-cmd-index="${item.originalIndex}">
          <span class="command-item-icon">⚡</span>
          <span>${escapeHtml(item.title)}</span>
          ${item.shortcut ? `<span class="command-item-shortcut">${escapeHtml(item.shortcut)}</span>` : ''}
        </div>
      `;
    });
  });

  resultsEl.innerHTML = html;

  resultsEl.querySelectorAll(".command-item").forEach((el) => {
    el.addEventListener("click", () => {
      const idx = Number(el.dataset.cmdIndex);
      executeCommand(idx);
    });
  });
}

function executeCommand(index) {
  const item = state.commandPalette.filteredItems[index];
  if (!item) return;
  closeCommandPalette();
  try {
    item.action();
  } catch (err) {
    toast(`Action error: ${err.message}`);
  }
}

function setupCommandPalette() {
  const trigger = $("#cmd-k-trigger");
  const backdrop = $("#command-palette-backdrop");
  const input = $("#command-input");

  trigger?.addEventListener("click", openCommandPalette);

  backdrop?.addEventListener("click", (e) => {
    if (e.target === backdrop) closeCommandPalette();
  });

  window.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      if (state.commandPalette.open) closeCommandPalette();
      else openCommandPalette();
    } else if (e.key === "Escape" && state.commandPalette.open) {
      e.preventDefault();
      closeCommandPalette();
    }
  });

  input?.addEventListener("input", (e) => {
    renderCommandResults(e.target.value);
  });

  input?.addEventListener("keydown", (e) => {
    const count = state.commandPalette.filteredItems.length;
    if (!count) return;

    if (e.key === "ArrowDown") {
      e.preventDefault();
      state.commandPalette.selectedIndex = (state.commandPalette.selectedIndex + 1) % count;
      renderCommandResults(input.value);
      scrollSelectedCommandIntoView();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      state.commandPalette.selectedIndex = (state.commandPalette.selectedIndex - 1 + count) % count;
      renderCommandResults(input.value);
      scrollSelectedCommandIntoView();
    } else if (e.key === "Enter") {
      e.preventDefault();
      executeCommand(state.commandPalette.selectedIndex);
    }
  });

  function scrollSelectedCommandIntoView() {
    const selected = $("#command-results .command-item.selected");
    selected?.scrollIntoView({ block: "nearest" });
  }
}

// =========================================================
// RUN EXECUTION & HISTORY
// =========================================================
function updateCompareButton() {
  const btn = $("#history-compare-btn");
  if (!btn) return;
  const count = state.selectedForCompare.size;
  btn.textContent = `Compare selected (${count}/2)`;
  btn.disabled = count !== 2;
}

function renderHistory() {
  const body = $("#history-body");
  if (!body) return;
  if (!state.runs.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty-row" style="text-align:center;padding:24px;color:var(--ink-400)">No runs yet. Launch a scenario to observe evidence.</td></tr>';
    return;
  }

  body.innerHTML = state.runs.map((run) => {
    const isSelected = state.selectedForCompare.has(run.id);
    const isCurrent = state.selectedRunId === run.id;
    return `
      <tr data-run-id="${escapeHtml(run.id)}" style="${isCurrent ? 'background:var(--amber-50)' : ''}">
        <td><input type="checkbox" class="run-compare-checkbox" data-compare-id="${escapeHtml(run.id)}" ${isSelected ? 'checked' : ''} onclick="event.stopPropagation()"></td>
        <td><strong>${escapeHtml(run.scenario)}</strong></td>
        <td><span class="impl-badge" style="background:var(--surface-subtle);color:var(--ink-600);border:1px solid var(--border-subtle);font-family:var(--font-mono);font-size:10px;padding:2px 6px;border-radius:3px">${escapeHtml(run.implementation)}</span></td>
        <td><span style="font-family:var(--font-mono);font-size:10px;text-transform:uppercase">${escapeHtml(run.status)}</span></td>
        <td><span class="verdict-badge ${escapeHtml(run.verdict)}">${escapeHtml(run.verdict)}</span></td>
        <td style="font-family:var(--font-mono);font-size:11px;color:var(--ink-400)">${escapeHtml(formatTime(run.started_at))}</td>
        <td><button class="button button-secondary inspect-run-btn" data-run-id="${escapeHtml(run.id)}" style="font-size:10px;padding:2px 8px;height:24px" onclick="event.stopPropagation()">Inspect ↗</button></td>
      </tr>
    `;
  }).join("");

  body.querySelectorAll("tr[data-run-id]").forEach((row) => {
    row.addEventListener("click", () => {
      openRun(row.dataset.runId);
      switchView("liverun");
    });
  });

  body.querySelectorAll(".inspect-run-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      openRun(btn.dataset.runId);
      switchView("liverun");
    });
  });

  body.querySelectorAll(".run-compare-checkbox").forEach((cb) => {
    cb.addEventListener("change", () => {
      const id = cb.dataset.compareId;
      if (cb.checked) {
        if (state.selectedForCompare.size >= 2) {
          const oldest = state.selectedForCompare.values().next().value;
          state.selectedForCompare.delete(oldest);
          const oldCb = body.querySelector(`.run-compare-checkbox[data-compare-id="${oldest}"]`);
          if (oldCb) oldCb.checked = false;
        }
        state.selectedForCompare.add(id);
      } else {
        state.selectedForCompare.delete(id);
      }
      updateCompareButton();
    });
  });

  updateCompareButton();
  updateMetricsRibbon();
}

function updateMetricsRibbon() {
  const runsCount = $("#metric-runs");
  const failCount = $("#metric-failures");
  const passCount = $("#metric-passes");
  if (runsCount) runsCount.textContent = state.runs.length;
  if (failCount) failCount.textContent = state.runs.filter((r) => r.verdict === "failed").length;
  if (passCount) passCount.textContent = state.runs.filter((r) => r.verdict === "passed").length;
}

async function refreshRuns() {
  try {
    const res = await apiService.getRuns();
    state.runs = res.runs || [];
    renderHistory();
    await refreshCoverage();
  } catch (err) {
    toast(`Failed to load runs: ${err.message}`);
  }
}

async function launchRun(implementation, scenarioId) {
  const buttons = $$(".run-button, .suite-run-btn");
  buttons.forEach((b) => { b.disabled = true; });
  try {
    const targetId = scenarioId || $("#scenario-select")?.value;
    const scenario = state.scenarios.find((item) => item.id === targetId) || state.scenarios[0];
    if (!scenario) throw new Error("No runnable scenario is available");

    const scenarioName = scenario.name || scenario.id;
    toast(`Launching ${scenarioName} (${implementation})...`);
    const res = await apiService.launchRun(scenario.id, implementation);
    const runId = res.run_id;

    state.selectedRunId = runId;
    switchView("liverun");
    await refreshRuns();
    await openRun(runId);
    schedulePoll(runId);
    toast(`${scenarioName} run started!`);
  } catch (error) {
    toast(`Launch error: ${error.message}`);
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
}

function schedulePoll(runId) {
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  state.pollTimer = window.setTimeout(async () => {
    try {
      const { run } = await apiService.getRun(runId);
      if (state.selectedRunId === runId) renderRun(run);
      if (["completed", "failed", "cancelled", "timed_out"].includes(run.status)) {
        await refreshRuns();
      } else {
        schedulePoll(runId);
      }
    } catch {
      schedulePoll(runId);
    }
  }, 650);
}

// =========================================================
// RUN INSPECTION & TIMELINE REPLAY
// =========================================================
async function openRun(runId) {
  try {
    const { run } = await apiService.getRun(runId);
    if (!run) throw new Error("Run not found");
    state.selectedRunId = runId;
    state.currentRun = run;
    renderRun(run);
    await Promise.all([loadAnnotations(runId), loadTasks(runId)]);
  } catch (error) {
    toast(error.message);
  }
}

function renderRun(run) {
  state.currentRun = run;
  const panel = $("#active-run");
  if (panel) panel.hidden = false;

  const title = $("#run-title");
  if (title) title.textContent = `${run.scenario?.name || run.scenario_id} (${run.implementation})`;

  const status = $("#run-status");
  if (status) {
    status.textContent = `${run.status.toUpperCase()} · ${run.verdict ? run.verdict.toUpperCase() : 'PENDING'}`;
    status.className = `run-status-pill ${run.verdict || run.status}`;
  }

  const cancelBtn = $("#cancel-run");
  if (cancelBtn) {
    const active = ["queued", "running", "cancel_requested"].includes(run.status);
    cancelBtn.hidden = !active;
    cancelBtn.dataset.runId = run.id;
  }

  // Overview Chips
  const overview = $("#run-overview");
  if (overview) {
    const faultLabel = run.scenario?.fault || run.scenario?.fault_type || 'configured';
    overview.innerHTML = `
      <span class="overview-chip" style="font-family:var(--font-mono);font-size:11px;background:#fff;border:1px solid var(--border-subtle);padding:4px 8px;border-radius:4px">Run ID: <strong>${run.id.slice(0, 8)}</strong></span>
      <span class="overview-chip" style="font-family:var(--font-mono);font-size:11px;background:#fff;border:1px solid var(--border-subtle);padding:4px 8px;border-radius:4px">Target: <strong>${escapeHtml(faultLabel)}</strong></span>
      <span class="overview-chip" style="font-family:var(--font-mono);font-size:11px;background:#fff;border:1px solid var(--border-subtle);padding:4px 8px;border-radius:4px">Events: <strong>${run.events?.length || 0}</strong></span>
      <span class="overview-chip" style="font-family:var(--font-mono);font-size:11px;background:#fff;border:1px solid var(--border-subtle);padding:4px 8px;border-radius:4px">Invariants: <strong>${run.assertions?.length || 0}</strong></span>
    `;
  }

  // Generic Side-Effect Ledger Strip
  const summary = run.summary || {};
  const charges = summary.charges || [];
  const refunds = summary.refunds || [];
  const fulfillments = summary.fulfillments || [];
  const sideEffects = summary.side_effects || [];
  const chargeList = $("#charge-list");
  if (chargeList) {
    if (!charges.length && !refunds.length && !fulfillments.length && !sideEffects.length) {
      chargeList.innerHTML = '<span style="font-size:11px;color:var(--ink-400)">No side-effect ledger items recorded yet.</span>';
    } else {
      let items = "";
      charges.forEach((c) => {
        items += `<span class="charge-chip"><i></i> Charge: <strong>$${(c.amount_cents / 100).toFixed(2)}</strong> (${escapeHtml(c.id)})</span>`;
      });
      refunds.forEach((r) => {
        items += `<span class="charge-chip" style="border-color:var(--crimson-400);background:var(--crimson-50);color:var(--crimson-900)"><i style="background:var(--crimson-500)"></i> Refund: <strong>$${(r.amount_cents / 100).toFixed(2)}</strong></span>`;
      });
      fulfillments.forEach((f) => {
        items += `<span class="charge-chip" style="border-color:var(--indigo-100);background:var(--indigo-100);color:var(--indigo-600)"><i></i> Fulfillment: <strong>${escapeHtml(f.id)}</strong></span>`;
      });
      sideEffects.forEach((se) => {
        items += `<span class="charge-chip"><i></i> ${escapeHtml(se.name || se.type || 'Effect')}: <strong>${escapeHtml(String(se.value || se.id || ''))}</strong></span>`;
      });
      chargeList.innerHTML = items;
    }
  }

  // Assertions
  const assertionList = $("#assertion-list");
  if (assertionList) {
    const assertions = run.assertions || [];
    if (!assertions.length) {
      assertionList.innerHTML = '<span style="font-size:11px;color:var(--ink-400)">Awaiting completion to evaluate business invariants...</span>';
    } else {
      assertionList.innerHTML = assertions.map((a) => `
        <div class="assertion-card ${a.status}">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
            <strong>${escapeHtml(a.name)}</strong>
            <span class="verdict-badge ${a.status}">${a.status}</span>
          </div>
          <small><strong>Expected:</strong> ${escapeHtml(a.expected || 'Rule condition holds')}</small>
          <small><strong>Observed:</strong> ${escapeHtml(a.observed || 'Verified')}</small>
        </div>
      `).join("");
    }
  }

  // Timeline Events
  state.replay.events = run.events || [];
  renderEventList(state.replay.events);
  updateReplaySliderRange(state.replay.events.length);
  setReplayStep(state.replay.events.length, false);

  // Setup reproduction link & copy buttons
  const reproLink = $("#download-repro");
  if (reproLink) {
    reproLink.href = `/api/runs/${encodeURIComponent(run.id)}/reproduce`;
    reproLink.download = `faultline-repro-${run.id.slice(0, 8)}.json`;
  }
  const copyBtn = $("#copy-run-id");
  if (copyBtn) copyBtn.dataset.runId = run.id;
}

function renderEventList(events) {
  const list = $("#event-list");
  if (!list) return;
  const q = (state.searchQuery || "").toLowerCase();
  const f = state.timelineFilter;

  const filtered = (events || []).filter((event) => {
    if (q) {
      const str = (event.type + " " + JSON.stringify(event.payload || {})).toLowerCase();
      if (!str.includes(q)) return false;
    }
    if (f === "faults") {
      return event.type.includes("FAULT") || event.type.includes("DROPPED") || event.type.includes("FAILED");
    }
    if (f === "side_effects") {
      return event.type.includes("CHARGE") || event.type.includes("REFUND") || event.type.includes("FULFILLMENT") || event.type.includes("EFFECT");
    }
    if (f === "lifecycle") {
      return event.type.includes("RUN") || event.type.includes("STEP") || event.type.includes("ASSERTION");
    }
    return true;
  });

  if (!filtered.length) {
    list.innerHTML = '<li style="padding:16px;color:var(--ink-400);font-size:11px">No events match criteria.</li>';
    return;
  }

  list.innerHTML = filtered.map((event) => {
    const isFault = event.type.includes("FAULT") || event.type.includes("DROPPED") || event.type.includes("FAILED");
    const isGood = event.type.includes("COMPENSATED") || event.type.includes("REFUND") || (event.type.includes("ASSERTION") && state.currentRun?.verdict === "passed");
    const markerClass = isFault ? "fault" : (isGood ? "good" : "");

    return `
      <li class="event-item ${markerClass}" data-seq="${event.sequence}">
        <div class="event-header">
          <span class="event-type-badge">#${event.sequence} · ${escapeHtml(event.type)}</span>
          <span class="event-time">${escapeHtml(formatTime(event.created_at))}</span>
        </div>
        ${event.payload ? `<div class="event-payload-box">${escapeHtml(JSON.stringify(event.payload, null, 2))}</div>` : ''}
      </li>
    `;
  }).join("");

  list.querySelectorAll(".event-item").forEach((item) => {
    item.addEventListener("click", () => {
      item.classList.toggle("expanded");
    });
  });
}

function updateReplaySliderRange(count) {
  const slider = $("#replay-slider");
  if (!slider) return;
  slider.min = "1";
  slider.max = String(Math.max(1, count));
  slider.value = String(count);
}

function setReplayStep(step, scroll = true) {
  const count = state.replay.events.length;
  if (!count) return;
  const clamped = Math.max(1, Math.min(step, count));
  state.replay.currentStep = clamped;

  const slider = $("#replay-slider");
  if (slider) slider.value = String(clamped);

  const label = $("#replay-step-label");
  if (label) label.textContent = `Event ${clamped} of ${count}`;

  $$(".event-item").forEach((item) => {
    const seq = Number(item.dataset.seq);
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

function jumpToFirstFault() {
  const events = state.replay.events || [];
  const ev = events.find((e) =>
    e.type.includes("FAULT") || e.type.includes("DROPPED") || e.type.includes("UNKNOWN") || e.type.includes("FAILED")
  );
  if (ev) {
    if (state.replay.timer) toggleReplayPlay();
    setReplayStep(ev.sequence);
    toast(`Jumped to fault event #${ev.sequence}`);
  } else {
    toast("No fault events found in this run");
  }
}

function jumpToFirstRetry() {
  const events = state.replay.events || [];
  const ev = events.find((e) =>
    e.type.includes("RETRY") || e.type.includes("REDELIVERY") || (e.payload && (e.payload.attempt > 1 || e.payload.retries > 0))
  );
  if (ev) {
    if (state.replay.timer) toggleReplayPlay();
    setReplayStep(ev.sequence);
    toast(`Jumped to retry event #${ev.sequence}`);
  } else {
    toast("No retry events found in this run");
  }
}

function jumpToFirstViolation() {
  const failedAssertion = (state.currentRun?.assertions || []).find((a) => a.status === "failed");
  if (!failedAssertion) {
    toast("No invariant violations in this run (all passed)");
    return;
  }
  const events = state.replay.events || [];
  let targetSeq = null;
  if (failedAssertion.evidence && failedAssertion.evidence.length) {
    for (const evRef of failedAssertion.evidence) {
      const match = events.find((e) =>
        e.type === evRef ||
        (evRef.startsWith("charge:") && e.payload?.charge_id === evRef.slice(7)) ||
        (evRef.startsWith("refund:") && e.payload?.refund_id === evRef.slice(7))
      );
      if (match) {
        targetSeq = match.sequence;
        break;
      }
    }
  }
  if (!targetSeq) {
    const evalEvent = events.find((e) => e.type.includes("ASSERTION"));
    if (evalEvent) targetSeq = evalEvent.sequence;
  }
  if (targetSeq) {
    if (state.replay.timer) toggleReplayPlay();
    setReplayStep(targetSeq);
    toast(`Jumped to violation: ${failedAssertion.name}`);
  } else {
    toast(`Violation: ${failedAssertion.name}`);
  }
}

// =========================================================
// CRITICAL INTEGRATIONS SUITE
// =========================================================
async function runSuite(implementation) {
  const buttons = $$(".suite-run-btn");
  buttons.forEach((b) => { b.disabled = true; });
  try {
    toast(`Starting Critical Integrations Suite (${implementation})...`);
    const { suite } = await apiService.getSuite("critical-integrations");
    switchView("liverun");
    for (const scId of suite.scenarios) {
      await launchRun(implementation, scId);
      for (let i = 0; i < 40; i++) {
        await new Promise((r) => setTimeout(r, 200));
        const { run } = await apiService.getRun(state.selectedRunId);
        if (["completed", "failed", "cancelled", "timed_out"].includes(run.status)) {
          break;
        }
      }
    }
    toast(`Critical Integrations Suite (${implementation}) completed!`);
  } catch (err) {
    toast(`Suite error: ${err.message}`);
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
}

// =========================================================
// SCENARIO STUDIO & PIPELINE DESIGNER
// =========================================================
function setScenarioEditor() {
  const select = $("#scenario-select");
  if (!select) return;
  const selected = state.scenarios.find((item) => item.id === select.value);
  const definition = selected?.definition || {
    scenario_version: 1,
    name: "new-scenario",
    description: "",
    fault: "payment_response_lost_after_commit",
    retries: 1,
    assertion: "service_recovers",
  };
  state.editorDefinition = definition;
  $$("[data-field]").forEach((input) => {
    const field = input.dataset.field;
    input.value = definition[field] ?? (field === "timeout_seconds" ? 15 : "");
  });
  updateWorkflowCanvas(definition);
  updateScenarioPreview();
  const saveBtn = $("#save-scenario");
  if (saveBtn) saveBtn.textContent = selected ? "Save new version" : "Create scenario";
}

function updateWorkflowCanvas(definition) {
  const canvas = $("#workflow-canvas");
  if (!canvas) return;

  // Generic Step Handler: if the scenario defines workflow steps, render them dynamically
  const steps = definition?.workflow || definition?.steps;
  if (Array.isArray(steps) && steps.length > 0) {
    canvas.innerHTML = steps.map((step, idx) => {
      const isFault = step.id === definition.fault?.target_step || step.type?.includes("fault") || step.fault;
      return `
        <li class="${isFault ? 'workflow-fault' : ''}">
          <span>${idx + 1}</span>
          <div>
            <strong>${escapeHtml(step.name || step.id || `Step ${idx + 1}`)}</strong>
            <small>${escapeHtml(step.description || step.type || 'Workflow operation')}</small>
          </div>
        </li>
      `;
    }).join("");
    return;
  }

  // Fallback / standard reference workflows
  const fault = typeof definition === "string" ? definition : definition?.fault;
  if (fault === "duplicate_payment_webhook") {
    canvas.innerHTML = `
      <li><span>1</span><div><strong>Webhook delivers payment event</strong><small>Sends payment.succeeded webhook</small></div></li>
      <li><span>2</span><div><strong>Checkout starts fulfillment</strong><small>Records order fulfillment side-effect</small></div></li>
      <li class="workflow-fault"><span>3</span><div><strong>Duplicate webhook redelivery</strong><small>Simulates network redelivery of same event</small></div></li>
      <li><span>4</span><div><strong>Checkout verifies deduplication</strong><small>Idempotently ignores duplicate event</small></div></li>
      <li><span>5</span><div><strong>Evaluate invariants</strong><small>Checks single fulfillment &amp; terminal state</small></div></li>
    `;
  } else if (fault === "persistence_failure_after_payment") {
    canvas.innerHTML = `
      <li><span>1</span><div><strong>Checkout starts payment</strong><small>Sends charge request to provider</small></div></li>
      <li><span>2</span><div><strong>Provider commits charge</strong><small>Payment provider ledger records $42.00</small></div></li>
      <li class="workflow-fault"><span>3</span><div><strong>Database persistence fails</strong><small>Simulates checkout DB commit failure</small></div></li>
      <li><span>4</span><div><strong>Checkout runs compensation</strong><small>Issues compensating refund to provider</small></div></li>
      <li><span>5</span><div><strong>Evaluate invariants</strong><small>Checks zero orphaned charges &amp; terminal state</small></div></li>
    `;
  } else {
    canvas.innerHTML = `
      <li><span>1</span><div><strong>Checkout starts payment</strong><small>Uses the order and amount above</small></div></li>
      <li><span>2</span><div><strong>Provider commits charge</strong><small>Local test provider records side-effect</small></div></li>
      <li class="workflow-fault"><span>3</span><div><strong>Drop provider response</strong><small>Simulates an unknown payment outcome</small></div></li>
      <li><span>4</span><div><strong>Checkout retries</strong><small>Runs the configured retry count</small></div></li>
      <li><span>5</span><div><strong>Evaluate invariants</strong><small>Checks duplicate charges and terminal state</small></div></li>
    `;
  }
}

function updateScenarioPreview() {
  if (!state.editorDefinition) return;
  const definition = { ...state.editorDefinition };
  $$("[data-field]").forEach((input) => {
    const field = input.dataset.field;
    definition[field] = input.type === "number" ? Number(input.value) : input.value;
  });
  state.editorDefinition = definition;
  const jsonEl = $("#scenario-json");
  if (jsonEl) jsonEl.value = JSON.stringify(definition, null, 2);
}

async function submitScenario(save) {
  const feedback = $("#scenario-feedback");
  try {
    updateScenarioPreview();
    const definition = state.editorDefinition;
    if (!save) {
      const result = await apiService.validateScenario(definition);
      if (feedback) feedback.textContent = result.valid ? "Definition is valid." : "Definition is invalid.";
      return;
    }
    const selected = $("#scenario-select")?.value;
    const result = selected
      ? await apiService.saveScenarioVersion(selected, definition)
      : await apiService.createScenario(definition);

    if (feedback) feedback.textContent = `Saved ${selected ? `version ${result.version}` : "version 1"}.`;
    await refreshScenarios();
    toast("Scenario saved successfully!");
  } catch (error) {
    if (feedback) feedback.textContent = error.message;
  }
}

/**
 * Make backend the source of truth for scenarios.
 * Populates scenario selector, command palette, and overview quick cards.
 */
async function refreshScenarios() {
  state.scenariosLoading = true;
  state.scenariosError = null;
  const countEl = $("#scenario-count");
  const select = $("#scenario-select");
  const grid = $(".scenarios-grid");

  try {
    const res = await apiService.getScenarios();
    state.scenarios = res.scenarios || [];
    state.scenariosLoading = false;

    if (countEl) countEl.textContent = String(state.scenarios.length);

    // Populate Experiment Studio Select
    if (select) {
      if (!state.scenarios.length) {
        select.innerHTML = '<option value="">No scenarios available (create one below)</option>';
      } else {
        select.innerHTML = '<option value="">Create a new scenario…</option>' +
          state.scenarios.map((s) => `<option value="${escapeHtml(s.id)}">${escapeHtml(s.name || s.id)} · v${s.version || 1}</option>`).join("");
      }
    }

    // Populate Overview Quick Cards dynamically from backend scenarios
    if (grid) {
      if (!state.scenarios.length) {
        grid.innerHTML = '<div class="empty-state" style="padding:24px;text-align:center;color:var(--ink-400);grid-column:1/-1">No registered scenarios found. Create one in the Experiment Studio or generate from templates.</div>';
      } else {
        grid.innerHTML = state.scenarios.map((s) => {
          const cat = s.definition?.category || s.category || "INTEGRATION FAILURE";
          const desc = s.definition?.description || s.description || "Simulates controlled fault injection to verify recovery.";
          return `
            <article class="scenario-card" data-scenario="${escapeHtml(s.id)}">
              <div>
                <div class="scenario-card-header">
                  <span class="scenario-tag">${escapeHtml(cat.toUpperCase())}</span>
                  <span class="scenario-version" style="font-family:var(--font-mono);font-size:10px;color:var(--ink-400)">v${s.version || 1}</span>
                </div>
                <h3>${escapeHtml(s.name || s.id)}</h3>
                <p class="scenario-description">${escapeHtml(desc)}</p>
              </div>
              <div class="scenario-actions">
                <button class="button button-primary run-button" data-scenario="${escapeHtml(s.id)}" data-implementation="vulnerable">
                  <span>⚡</span> Run Vulnerable
                </button>
                <button class="button button-secondary run-button" data-scenario="${escapeHtml(s.id)}" data-implementation="fixed">
                  <span>🛡</span> Run Fixed
                </button>
              </div>
            </article>
          `;
        }).join("");

        // Re-bind quick launch buttons
        grid.querySelectorAll(".run-button").forEach((btn) => {
          btn.addEventListener("click", () => launchRun(btn.dataset.implementation, btn.dataset.scenario));
        });
      }
    }
  } catch (err) {
    state.scenariosLoading = false;
    state.scenariosError = err.message;
    if (countEl) countEl.textContent = "!";
    if (grid) {
      grid.innerHTML = `<div class="error-state" style="padding:16px;color:var(--crimson-600);grid-column:1/-1;border:1px solid var(--crimson-200);border-radius:var(--radius-sm);background:var(--crimson-50)">Failed to load scenarios from control plane: ${escapeHtml(err.message)}</div>`;
    }
  }
}

// =========================================================
// OPENTELEMETRY TRACES
// =========================================================
async function loadRunTelemetry(runId) {
  const traceIdEl = $("#telemetry-trace-id");
  const countEl = $("#telemetry-span-count");
  const listEl = $("#telemetry-spans-list");
  if (!traceIdEl || !listEl) return;

  try {
    listEl.innerHTML = '<span style="font-size:11px;color:var(--ink-400)">Loading OpenTelemetry trace spans...</span>';
    const data = await apiService.getTelemetry(runId);
    traceIdEl.textContent = data.traceId;
    if (countEl) countEl.textContent = String(data.spanCount);

    const copyBtn = $("#copy-traceparent-btn");
    if (copyBtn) {
      copyBtn.onclick = () => {
        const traceparent = `00-${data.traceId}-${data.rootSpanId || data.traceId.slice(0, 16)}-01`;
        navigator.clipboard.writeText(traceparent).then(() => toast("Copied W3C traceparent header"));
      };
    }

    if (!data.spans || !data.spans.length) {
      listEl.innerHTML = '<span style="font-size:11px;color:var(--ink-400)">No spans recorded for this run.</span>';
      return;
    }

    const maxDuration = Math.max(...data.spans.map((s) => s.durationMs || 1), 1);
    listEl.innerHTML = data.spans.map((s) => {
      const barWidth = Math.max(8, Math.round(((s.durationMs || 1) / maxDuration) * 100));
      const statusColor = s.statusCode === "OK" ? "var(--emerald-600)" : "var(--crimson-600)";
      return `
        <div class="waterfall-span">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div>
              <strong style="font-family:var(--font-mono);font-size:11px">${escapeHtml(s.name)}</strong>
              <small style="color:var(--ink-400);margin-left:6px">${escapeHtml(s.kind)}</small>
            </div>
            <div style="font-family:var(--font-mono);font-size:11px">
              <span style="color:${statusColor};font-weight:700">${escapeHtml(s.statusCode)}</span>
              <span style="color:var(--ink-400);margin-left:8px">${s.durationMs}ms</span>
            </div>
          </div>
          <div class="waterfall-bar-track">
            <div class="waterfall-bar-fill" style="width:${barWidth}%"></div>
          </div>
        </div>
      `;
    }).join("");
  } catch (err) {
    listEl.innerHTML = `<span style="font-size:11px;color:var(--crimson-600)">Failed loading telemetry: ${escapeHtml(err.message)}</span>`;
  }
}

// =========================================================
// RUN COMPARISON & DIFF
// =========================================================
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
    const { diff } = await apiService.diffRuns(state.selectedRunId, baselineId);
    renderDiff(diff);
  } catch (err) {
    toast(`Diff error: ${err.message}`);
  }
}

function renderDiff(diff) {
  const container = $("#diff-content");
  if (!container) return;
  let html = `
    <div style="padding:10px 14px;border-radius:var(--radius-sm);background:${diff.verdict_match ? 'var(--emerald-50)' : 'var(--crimson-50)'};border:1px solid ${diff.verdict_match ? 'var(--emerald-500)' : 'var(--crimson-500)'};margin-bottom:12px;display:flex;justify-content:space-between;align-items:center">
      <strong>Verdict: Current [${escapeHtml(diff.run_a.verdict)}] vs Baseline [${escapeHtml(diff.run_b.verdict)}]</strong>
      <span class="verdict-badge ${diff.verdict_match ? 'passed' : 'failed'}">${diff.verdict_match ? "Matched" : "Diverged"}</span>
    </div>
  `;
  if (diff.first_event_divergence) {
    const d = diff.first_event_divergence;
    html += `
      <div style="padding:10px 12px;background:var(--surface-subtle);border:1px solid var(--border-subtle);border-radius:var(--radius-sm);font-size:11px;margin-bottom:12px">
        <strong>Earliest event divergence at step #${escapeHtml(d.sequence)}:</strong>
        <div>Current Run: <code>${escapeHtml(d.run_a_event)}</code></div>
        <div>Baseline Run: <code>${escapeHtml(d.run_b_event)}</code></div>
      </div>
    `;
  } else {
    html += '<div style="padding:8px 12px;font-size:11px;color:var(--emerald-700)">No event stream divergence detected.</div>';
  }

  html += '<table class="diff-table"><thead><tr><th>Invariant</th><th>Current Run</th><th>Baseline Run</th><th>Status</th></tr></thead><tbody>';
  diff.invariants.forEach((inv) => {
    html += `
      <tr class="${inv.diverged ? "row-diverged" : ""}">
        <td><strong>${escapeHtml(inv.name)}</strong></td>
        <td><span class="verdict-badge ${escapeHtml(inv.run_a_status)}">${escapeHtml(inv.run_a_status)}</span></td>
        <td><span class="verdict-badge ${escapeHtml(inv.run_b_status)}">${escapeHtml(inv.run_b_status)}</span></td>
        <td>${inv.diverged ? '<span class="verdict-badge failed">Diverged</span>' : '<span class="verdict-badge passed">Matched</span>'}</td>
      </tr>
    `;
  });
  html += "</tbody></table>";
  container.innerHTML = html;
}

// =========================================================
// RELIABILITY COVERAGE MATRIX & GAPS
// =========================================================
async function refreshCoverage() {
  try {
    const data = await apiService.getCoverage();
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
        label = `Defect (${c.runs_count})`;
        icon = "⌁";
      }
      return `<td><span class="cov-cell ${c.status}" data-scenario="${escapeHtml(c.scenario_id || '')}" data-applicable="${c.applicable}">${icon} ${escapeHtml(label)}</span></td>`;
    }).join("");
    return `
      <tr>
        <td><strong>${escapeHtml(row.step.name)}</strong><br><small style="color:var(--ink-400)">${escapeHtml(row.step.description)}</small></td>
        ${cellsHtml}
      </tr>
    `;
  }).join("");

  tbody.querySelectorAll(".cov-cell").forEach((cell) => {
    cell.addEventListener("click", () => {
      const sc = cell.dataset.scenario;
      if (sc && cell.dataset.applicable === "true") {
        switchView("studio");
        const select = $("#scenario-select");
        if (select) {
          select.value = sc;
          setScenarioEditor();
        }
        toast(`Loaded scenario '${sc}' into Studio`);
      }
    });
  });
}

async function loadTemplates() {
  const container = $("#template-grid");
  if (!container) return;
  try {
    const { templates } = await apiService.getTemplates();
    container.innerHTML = templates.map((tpl) => `
      <article class="scenario-card" style="display:flex;flex-direction:column;justify-content:space-between">
        <div>
          <div class="scenario-card-header">
            <span class="scenario-tag">${escapeHtml(tpl.category.toUpperCase())}</span>
          </div>
          <h3>${escapeHtml(tpl.name)}</h3>
          <p class="scenario-description">${escapeHtml(tpl.description)}</p>
          <div style="font-size:11px;color:var(--ink-500);margin-bottom:8px">
            <strong>Invariants:</strong> ${tpl.expected_invariants.map((inv) => `<code style="background:var(--surface-subtle);padding:1px 4px;border-radius:3px">${escapeHtml(inv)}</code>`).join(" ")}
          </div>
        </div>
        <button class="button button-primary instantiate-btn" data-template-id="${escapeHtml(tpl.id)}" style="width:100%;justify-content:center">
          <span>+</span> Instantiate scenario
        </button>
      </article>
    `).join("");

    container.querySelectorAll(".instantiate-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const templateId = btn.dataset.templateId;
        const scId = `${templateId.replace("-tpl", "")}-${Date.now().toString(36)}`;
        try {
          btn.disabled = true;
          await apiService.instantiateTemplate(templateId, scId);
          toast(`Created scenario '${scId}' from template!`);
          await refreshScenarios();
          switchView("studio");
          const select = $("#scenario-select");
          if (select) {
            select.value = scId;
            setScenarioEditor();
          }
        } catch (err) {
          toast(`Failed: ${err.message}`);
        } finally {
          btn.disabled = false;
        }
      });
    });
  } catch (err) {
    console.error("Failed to load templates", err);
  }
}

// =========================================================
// COLLABORATION: ANNOTATIONS & ACTION TASKS
// =========================================================
async function loadAnnotations(runId) {
  try {
    const { annotations } = await apiService.getAnnotations(runId);
    state.annotations = annotations || [];
    renderAnnotations();
  } catch {}
}

function renderAnnotations() {
  const list = $("#annotation-list");
  if (!list) return;
  if (!state.annotations.length) {
    list.innerHTML = '<span style="font-size:11px;color:var(--ink-400)">No notes added yet.</span>';
    return;
  }
  list.innerHTML = state.annotations.map((a) => `
    <div style="background:var(--surface-subtle);border:1px solid var(--border-subtle);border-radius:var(--radius-sm);padding:6px 10px;font-size:11px;margin-bottom:6px">
      <div style="display:flex;justify-content:space-between;color:var(--ink-500);font-size:10px;margin-bottom:2px">
        <strong>${escapeHtml(a.author)}</strong>
        <time>${escapeHtml(formatTime(a.created_at))}</time>
      </div>
      <div>${escapeHtml(a.comment)}</div>
    </div>
  `).join("");
}

async function handleAddAnnotation(event) {
  event.preventDefault();
  if (!state.selectedRunId) return;
  const authorInput = $("#annotation-author");
  const textInput = $("#annotation-text");
  const comment = textInput?.value.trim();
  if (!comment) return;
  try {
    const { annotation } = await apiService.addAnnotation(
      state.selectedRunId,
      authorInput?.value.trim() || "Developer",
      comment
    );
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
    const { tasks } = await apiService.getTasks(runId);
    state.tasks = tasks || [];
    renderTasks();
  } catch {}
}

function renderTasks() {
  const list = $("#task-list");
  if (!list) return;
  if (!state.tasks.length) {
    list.innerHTML = '<span style="font-size:11px;color:var(--ink-400)">No tasks created for this run.</span>';
    return;
  }
  list.innerHTML = state.tasks.map((t) => `
    <div class="task-item ${t.status === 'resolved' ? 'resolved' : ''}" style="display:flex;align-items:center;gap:8px;padding:6px 10px;background:var(--surface-subtle);border:1px solid var(--border-subtle);border-radius:var(--radius-sm);font-size:11px;margin-bottom:6px">
      <input type="checkbox" ${t.status === 'resolved' ? 'checked' : ''} data-task-id="${escapeHtml(t.id)}">
      <span class="task-badge ${escapeHtml(t.priority)}">${escapeHtml(t.priority)}</span>
      <span style="flex:1"><strong>${escapeHtml(t.title)}</strong></span>
      <small style="color:var(--ink-400)">${escapeHtml(t.assignee)}</small>
    </div>
  `).join("");

  list.querySelectorAll("input[type=checkbox]").forEach((cb) => {
    cb.addEventListener("change", async (e) => {
      const taskId = e.target.dataset.taskId;
      const newStatus = e.target.checked ? "resolved" : "open";
      try {
        await apiService.updateTask(taskId, newStatus);
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
  const title = titleInput?.value.trim();
  if (!title) return;
  try {
    const { task } = await apiService.addTask(
      state.selectedRunId,
      title,
      prioSelect?.value || "medium",
      "Developer"
    );
    state.tasks.unshift(task);
    renderTasks();
    titleInput.value = "";
    toast("Action task created");
  } catch (err) {
    toast(`Error: ${err.message}`);
  }
}

// =========================================================
// CHECKPOINTS, BASELINES & OPENAPI
// =========================================================
function setupBranching() {
  const btn = $("#branch-checkpoint-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    if (!state.selectedRunId) return;
    const impl = window.prompt("Fork alternative execution branch.\\nEnter implementation ('fixed' or 'vulnerable'):", "fixed");
    if (!impl || !["fixed", "vulnerable"].includes(impl.trim().toLowerCase())) return;
    try {
      btn.disabled = true;
      toast("Branching execution from checkpoint...");
      const { run } = await apiService.branchRun(state.selectedRunId, { implementation: impl.trim().toLowerCase() });
      toast(`Branched new run: ${run.id.slice(0, 8)}`);
      await refreshRuns();
      await openRun(run.id);
      switchView("liverun");
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
      await apiService.setBaseline(state.selectedRunId);
      toast("Run designated as baseline 🎯");
      await refreshRuns();
    } catch (err) {
      toast(`Baseline error: ${err.message}`);
    } finally {
      btn.disabled = false;
    }
  });
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
      const { workflow } = await apiService.importOpenApi(raw);
      results.hidden = false;
      results.innerHTML = `
        <div style="background:var(--surface-subtle);border:1px solid var(--border-subtle);border-radius:var(--radius-sm);padding:12px">
          <strong>${escapeHtml(workflow.title)} (v${escapeHtml(workflow.version)})</strong>
          <span style="font-size:11px;color:var(--emerald-700);margin-left:8px">${workflow.endpoints_count} endpoints mapped</span>
          <div style="margin-top:8px;font-size:11px">
            <strong>Recommended Failure Scenarios:</strong>
            <ul style="margin:4px 0 0 16px;padding:0">
              ${workflow.recommended_scenarios.map((rec) => `<li><strong>${escapeHtml(rec.name)}</strong>: ${escapeHtml(rec.rationale)}</li>`).join("")}
            </ul>
          </div>
        </div>
      `;
      toast(`Mapped ${workflow.endpoints_count} endpoints!`);
    } catch (err) {
      toast(`Import failed: ${err.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = "Import workflow";
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
      const { count } = await apiService.generateFromGaps(state.activeProjectId);
      if (count > 0) {
        toast(`Generated ${count} new scenario(s) from coverage gaps!`);
        await refreshScenarios();
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

// =========================================================
// UTILITIES: METRICS, AUDIT, PRUNE (MODALS)
// =========================================================
function setupModals() {
  $$(".modal-overlay").forEach((modal) => {
    modal.hidden = true;
    modal.addEventListener("click", (e) => {
      if (e.target === modal) {
        modal.hidden = true;
      }
    });
  });

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      $$(".modal-overlay").forEach((modal) => {
        modal.hidden = true;
      });
    }
  });

  // Metrics Modal
  const openMetrics = $("#open-metrics-btn");
  const closeMetrics = $("#close-metrics-btn");
  const metricsModal = $("#metrics-modal");
  openMetrics?.addEventListener("click", async () => {
    if (metricsModal) metricsModal.hidden = false;
    try {
      const content = $("#metrics-content");
      if (content) content.innerHTML = '<span style="font-size:11px;color:var(--ink-400)">Loading metrics...</span>';
      const m = await apiService.getMetrics();
      const r = m.runs || {};
      const a = m.assertions || {};
      const w = m.worker_health || {};
      if (content) {
        content.innerHTML = `
          <div style="background:var(--surface-subtle);padding:10px;border-radius:var(--radius-sm)">
            <strong style="font-size:11px;color:var(--ink-500)">WORKER STATUS</strong>
            <div style="font-size:16px;font-weight:700;color:var(--emerald-600)">${escapeHtml(String(w.status || 'healthy')).toUpperCase()}</div>
            <small style="font-size:10px;color:var(--ink-400)">Active: ${w.active_workers || 0}</small>
          </div>
          <div style="background:var(--surface-subtle);padding:10px;border-radius:var(--radius-sm)">
            <strong style="font-size:11px;color:var(--ink-500)">TOTAL RUNS</strong>
            <div style="font-size:16px;font-weight:700">${r.total || 0}</div>
            <small style="font-size:10px;color:var(--ink-400)">✓ ${r.completed || 0} · ✕ ${r.failed || 0}</small>
          </div>
          <div style="background:var(--surface-subtle);padding:10px;border-radius:var(--radius-sm)">
            <strong style="font-size:11px;color:var(--ink-500)">PASS RATE</strong>
            <div style="font-size:16px;font-weight:700">${a.pass_rate_percent || 100}%</div>
            <small style="font-size:10px;color:var(--ink-400)">${a.passed || 0}/${a.total || 0} passed</small>
          </div>
          <div style="background:var(--surface-subtle);padding:10px;border-radius:var(--radius-sm)">
            <strong style="font-size:11px;color:var(--ink-500)">UPTIME</strong>
            <div style="font-size:16px;font-weight:700">${Math.floor(m.uptime_seconds || 0)}s</div>
            <small style="font-size:10px;color:var(--ink-400)">Control plane active</small>
          </div>
        `;
      }
    } catch (err) {
      toast(`Metrics error: ${err.message}`);
    }
  });
  closeMetrics?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (metricsModal) metricsModal.hidden = true;
  });

  // Audit Modal
  const openAudit = $("#open-audit-btn");
  const closeAudit = $("#close-audit-btn");
  const auditModal = $("#audit-modal");
  openAudit?.addEventListener("click", async () => {
    if (auditModal) auditModal.hidden = false;
    const list = $("#audit-log-list");
    if (!list) return;
    try {
      const { audit_events } = await apiService.getAuditEvents(state.activeProjectId);
      if (!audit_events.length) {
        list.innerHTML = '<span style="font-size:11px;color:var(--ink-400)">No audit events recorded yet.</span>';
        return;
      }
      list.innerHTML = audit_events.map((ev) => `
        <div style="font-size:11px;padding:6px 0;border-bottom:1px solid var(--border-subtle);display:flex;justify-content:space-between">
          <div><strong>${escapeHtml(ev.action)}</strong> on ${escapeHtml(ev.target_type)}:<code>${escapeHtml(ev.target_id)}</code> by ${escapeHtml(ev.actor)}</div>
          <time style="color:var(--ink-400)">${escapeHtml(formatTime(ev.created_at))}</time>
        </div>
      `).join("");
    } catch (err) {
      list.innerHTML = `<span style="color:var(--crimson-600)">Error: ${escapeHtml(err.message)}</span>`;
    }
  });
  closeAudit?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (auditModal) auditModal.hidden = true;
  });

  // Prune Modal
  const openPrune = $("#open-prune-btn");
  const closePrune = $("#close-prune-btn");
  const pruneModal = $("#prune-modal");
  const execPrune = $("#execute-prune-btn");
  openPrune?.addEventListener("click", () => {
    if (pruneModal) pruneModal.hidden = false;
    const res = $("#prune-results");
    if (res) res.hidden = true;
  });
  closePrune?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (pruneModal) pruneModal.hidden = true;
  });

  execPrune?.addEventListener("click", async () => {
    try {
      execPrune.disabled = true;
      execPrune.textContent = "Pruning...";
      const days = parseInt($("#prune-days-input")?.value, 10) || 30;
      const keepBaselines = $("#prune-keep-baselines")?.checked ?? true;
      const dryRun = $("#prune-dry-run")?.checked ?? false;
      const res = await apiService.pruneRuns(state.activeProjectId, days, keepBaselines, dryRun);
      const resultsDiv = $("#prune-results");
      if (resultsDiv) {
        resultsDiv.hidden = false;
        resultsDiv.textContent = `Pruned ${res.pruned_run_count} run(s) older than ${res.cutoff}; preserved ${res.skipped_baselines} baselines.`;
      }
      toast(`Pruning complete: ${res.pruned_run_count} runs pruned.`);
      if (!dryRun && res.pruned_run_count > 0) await refreshRuns();
    } catch (err) {
      toast(`Prune error: ${err.message}`);
    } finally {
      execPrune.disabled = false;
      execPrune.textContent = "Execute Prune";
    }
  });
}

// =========================================================
// INITIALIZATION
// =========================================================
async function initialize() {
  setupViewNavigation();
  setupCommandPalette();
  setupBranching();
  setupBaseline();
  setupOpenApiImporter();
  setupGenerateFromGaps();
  setupModals();

  // Button Listeners
  $$(".run-button").forEach((btn) => {
    btn.addEventListener("click", () => launchRun(btn.dataset.implementation, btn.dataset.scenario));
  });

  $("#studio-run-vulnerable")?.addEventListener("click", () => {
    launchRun("vulnerable", $("#scenario-select")?.value);
  });
  $("#studio-run-fixed")?.addEventListener("click", () => {
    launchRun("fixed", $("#scenario-select")?.value);
  });

  $$(".suite-run-btn").forEach((btn) => {
    btn.addEventListener("click", () => runSuite(btn.dataset.implementation));
  });

  $("#refresh-button")?.addEventListener("click", async () => {
    await Promise.all([refreshRuns(), refreshCoverage(), refreshScenarios()]);
    toast("Platform state refreshed");
  });

  $("#history-refresh")?.addEventListener("click", () => refreshRuns());

  $("#scenario-select")?.addEventListener("change", setScenarioEditor);
  $("#validate-scenario")?.addEventListener("click", () => submitScenario(false));
  $("#save-scenario")?.addEventListener("click", () => submitScenario(true));
  $$("[data-field]").forEach((input) => input.addEventListener("input", updateScenarioPreview));

  // Replay Listeners
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

  $("#jump-first-fault")?.addEventListener("click", jumpToFirstFault);
  $("#jump-first-retry")?.addEventListener("click", jumpToFirstRetry);
  $("#jump-first-violation")?.addEventListener("click", jumpToFirstViolation);

  // Filter chips
  $$(".filter-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      $$(".filter-chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      state.timelineFilter = chip.dataset.filter;
      renderEventList(state.replay.events);
    });
  });

  $("#event-search")?.addEventListener("input", (e) => {
    state.searchQuery = e.target.value.trim();
    renderEventList(state.replay.events);
  });

  // Diff
  $("#history-compare-btn")?.addEventListener("click", async () => {
    const ids = Array.from(state.selectedForCompare);
    if (ids.length !== 2) return;
    const [runA, runB] = ids;
    await openRun(runA);
    updateDiffBaselineSelect();
    const select = $("#diff-baseline-select");
    if (select) select.value = runB;
    await executeRunDiff();
    switchView("investigation");
    toast(`Comparing run ${runA.slice(0, 8)} with ${runB.slice(0, 8)}`);
  });

  $("#diff-execute-btn")?.addEventListener("click", executeRunDiff);
  $("#diff-toggle-btn")?.addEventListener("click", () => {
    switchView("investigation");
    updateDiffBaselineSelect();
    executeRunDiff();
  });

  // Telemetry
  $("#telemetry-btn")?.addEventListener("click", async () => {
    switchView("investigation");
    if (state.selectedRunId) await loadRunTelemetry(state.selectedRunId);
  });
  $("#close-telemetry-btn")?.addEventListener("click", () => {
    const card = $("#telemetry-card");
    if (card) card.hidden = true;
  });

  // Annotations & Tasks
  $("#annotation-form")?.addEventListener("submit", handleAddAnnotation);
  $("#task-form")?.addEventListener("submit", handleAddTask);

  // Copy Run ID
  $("#copy-run-id")?.addEventListener("click", async (e) => {
    const runId = e.currentTarget.dataset.runId;
    if (!runId) return;
    try {
      await navigator.clipboard.writeText(runId);
      toast("Run ID copied to clipboard");
    } catch {
      toast(runId);
    }
  });

  // Cancel Run
  $("#cancel-run")?.addEventListener("click", async (e) => {
    const runId = e.currentTarget.dataset.runId;
    if (!runId) return;
    try {
      await apiService.cancelRun(runId);
      toast("Cancellation requested");
      await openRun(runId);
    } catch (err) {
      toast(err.message);
    }
  });

  // Discover Projects & Load Initial Data
  try {
    try {
      const projRes = await apiService.getProjects();
      if (projRes?.projects?.length) {
        state.activeProjectId = projRes.projects[0].id;
      }
    } catch {}

    await Promise.all([refreshScenarios(), refreshRuns(), refreshCoverage(), loadTemplates()]);
    if (state.scenarios.length) {
      const select = $("#scenario-select");
      if (select) select.value = state.scenarios[0].id;
      setScenarioEditor();
    }
    if (state.runs.length) {
      await openRun(state.runs[0].id);
    }
  } catch (err) {
    toast(`Faultline initial load error: ${err.message}`);
  }
}

// Start application
if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize);
  } else {
    initialize();
  }
}
