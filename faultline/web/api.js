/**
 * Faultline API Client
 * Centralized client for the Faultline control-plane REST API.
 * Supports loading, error, empty states, and standard HTTP requests.
 */

(function (root) {
  const API_BASE = "";

  /**
   * Internal request helper
   * @param {string} path - URL path (e.g. "/api/scenarios")
   * @param {RequestInit & { body?: any }} options - Fetch options
   * @returns {Promise<any>}
   */
  async function apiRequest(path, options = {}) {
    const url = path.startsWith("http") ? path : `${API_BASE}${path}`;
    const headers = {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    };

    let body = options.body;
    if (body !== undefined && typeof body === "object" && !(body instanceof FormData)) {
      body = JSON.stringify(body);
    }

    const response = await fetch(url, {
      ...options,
      headers,
      body,
    });

    if (response.status === 204) {
      return null;
    }

    const text = await response.text();
    let parsed;
    try {
      parsed = text ? JSON.parse(text) : {};
    } catch {
      parsed = { raw: text };
    }

    if (!response.ok) {
      const message = parsed.error || `HTTP ${response.status} ${response.statusText}`;
      const err = new Error(message);
      err.status = response.status;
      err.body = parsed;
      throw err;
    }

    return parsed;
  }

  const api = {
    request: apiRequest,

    // ==========================================
    // SCENARIOS
    // ==========================================
    getScenarios: () => apiRequest("/api/scenarios"),
    getScenario: (scenarioId) => apiRequest(`/api/scenarios/${encodeURIComponent(scenarioId)}`),
    createScenario: (definition) =>
      apiRequest("/api/scenarios", {
        method: "POST",
        body: { definition },
      }),
    saveScenarioVersion: (scenarioId, definition) =>
      apiRequest(`/api/scenarios/${encodeURIComponent(scenarioId)}/versions`, {
        method: "POST",
        body: { definition },
      }),
    validateScenario: (definition) =>
      apiRequest("/api/scenarios/validate", {
        method: "POST",
        body: { definition },
      }),
    generateFromGaps: (projectId = "faultline-demo") =>
      apiRequest("/api/scenarios/generate-from-gaps", {
        method: "POST",
        body: { project_id: projectId },
      }),

    // ==========================================
    // RUNS
    // ==========================================
    getRuns: () => apiRequest("/api/runs"),
    getRun: (runId) => apiRequest(`/api/runs/${encodeURIComponent(runId)}`),
    launchRun: (scenarioId, implementation = "vulnerable") =>
      apiRequest("/api/runs", {
        method: "POST",
        body: { scenario_id: scenarioId, implementation },
      }),
    cancelRun: (runId) =>
      apiRequest(`/api/runs/${encodeURIComponent(runId)}/cancel`, {
        method: "POST",
        body: {},
      }),
    branchRun: (runId, overrides = {}) =>
      apiRequest(`/api/runs/${encodeURIComponent(runId)}/branch`, {
        method: "POST",
        body: { overrides },
      }),
    setBaseline: (runId) =>
      apiRequest(`/api/runs/${encodeURIComponent(runId)}/baseline`, {
        method: "POST",
      }),
    diffRuns: (runId, baselineId) =>
      apiRequest(`/api/runs/${encodeURIComponent(runId)}/diff/${encodeURIComponent(baselineId)}`),
    getTelemetry: (runId) =>
      apiRequest(`/api/runs/${encodeURIComponent(runId)}/telemetry`),

    // ==========================================
    // ANNOTATIONS & ACTION TASKS
    // ==========================================
    getAnnotations: (runId) =>
      apiRequest(`/api/runs/${encodeURIComponent(runId)}/annotations`),
    addAnnotation: (runId, author, comment) =>
      apiRequest(`/api/runs/${encodeURIComponent(runId)}/annotations`, {
        method: "POST",
        body: { author, comment },
      }),
    getTasks: (runId) =>
      apiRequest(`/api/runs/${encodeURIComponent(runId)}/tasks`),
    addTask: (runId, title, priority = "medium", assignee = "Developer") =>
      apiRequest(`/api/runs/${encodeURIComponent(runId)}/tasks`, {
        method: "POST",
        body: { title, priority, assignee },
      }),
    updateTask: (taskId, status) =>
      apiRequest(`/api/tasks/${encodeURIComponent(taskId)}`, {
        method: "PATCH",
        body: { status },
      }),

    // ==========================================
    // SUITES & TEMPLATES
    // ==========================================
    getSuite: (suiteId = "critical-integrations") =>
      apiRequest(`/api/suites/${encodeURIComponent(suiteId)}`),
    getTemplates: () =>
      apiRequest("/api/templates"),
    instantiateTemplate: (templateId, scenarioId) =>
      apiRequest(`/api/templates/${encodeURIComponent(templateId)}/instantiate`, {
        method: "POST",
        body: { scenario_id: scenarioId },
      }),

    // ==========================================
    // COVERAGE, PROJECTS & SYSTEM METRICS
    // ==========================================
    getCoverage: () => apiRequest("/api/coverage"),
    getMetrics: () => apiRequest("/api/metrics"),
    getProjects: () => apiRequest("/api/projects"),
    getAuditEvents: (projectId = "faultline-demo") =>
      apiRequest(`/api/projects/${encodeURIComponent(projectId)}/audit-events`),
    pruneRuns: (projectId, maxAgeDays = 30, keepBaselines = true, dryRun = false) =>
      apiRequest(`/api/projects/${encodeURIComponent(projectId)}/prune`, {
        method: "POST",
        body: { max_age_days: maxAgeDays, keep_baselines: keepBaselines, dry_run: dryRun },
      }),
    importOpenApi: (specText) =>
      apiRequest("/api/workflows/import-openapi", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: typeof specText === "string" ? specText : JSON.stringify(specText),
      }),
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { api };
  }
  root.api = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
