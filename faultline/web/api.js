/**
 * Faultline API Client
 * Centralized client for the Faultline control-plane REST API.
 * Supports loading, error, empty states, and standard HTTP requests.
 * Fully generic without hardcoded project, suite, or demo fallbacks.
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
    // SCENARIOS & SCHEMA REGISTRY
    // ==========================================
    getScenarios: () => apiRequest("/api/scenarios"),
    getScenario: (scenarioId) => {
      if (!scenarioId) throw new Error("scenarioId is required for getScenario");
      return apiRequest(`/api/scenarios/${encodeURIComponent(scenarioId)}`);
    },
    getSupportedFaults: () => apiRequest("/api/scenarios/supported-faults"),
    createScenario: (definition) =>
      apiRequest("/api/scenarios", {
        method: "POST",
        body: { definition },
      }),
    saveScenarioVersion: (scenarioId, definition) => {
      if (!scenarioId) throw new Error("scenarioId is required for saveScenarioVersion");
      return apiRequest(`/api/scenarios/${encodeURIComponent(scenarioId)}/versions`, {
        method: "POST",
        body: { definition },
      });
    },
    validateScenario: (definition) =>
      apiRequest("/api/scenarios/validate", {
        method: "POST",
        body: { definition },
      }),
    generateFromGaps: (projectId) => {
      if (!projectId) throw new Error("projectId is required for generateFromGaps");
      return apiRequest("/api/scenarios/generate-from-gaps", {
        method: "POST",
        body: { project_id: projectId },
      });
    },

    // ==========================================
    // RUNS
    // ==========================================
    getRuns: () => apiRequest("/api/runs"),
    getRun: (runId) => {
      if (!runId) throw new Error("runId is required for getRun");
      return apiRequest(`/api/runs/${encodeURIComponent(runId)}`);
    },
    launchRun: (scenarioId, implementation = "vulnerable") => {
      if (!scenarioId) throw new Error("scenarioId is required for launchRun");
      return apiRequest("/api/runs", {
        method: "POST",
        body: { scenario_id: scenarioId, implementation },
      });
    },
    cancelRun: (runId) => {
      if (!runId) throw new Error("runId is required for cancelRun");
      return apiRequest(`/api/runs/${encodeURIComponent(runId)}/cancel`, {
        method: "POST",
        body: {},
      });
    },
    branchRun: (runId, overrides = {}) => {
      if (!runId) throw new Error("runId is required for branchRun");
      return apiRequest(`/api/runs/${encodeURIComponent(runId)}/branch`, {
        method: "POST",
        body: { overrides },
      });
    },
    setBaseline: (runId) => {
      if (!runId) throw new Error("runId is required for setBaseline");
      return apiRequest(`/api/runs/${encodeURIComponent(runId)}/baseline`, {
        method: "POST",
      });
    },
    diffRuns: (runId, baselineId) => {
      if (!runId || !baselineId) throw new Error("runId and baselineId are required for diffRuns");
      return apiRequest(`/api/runs/${encodeURIComponent(runId)}/diff/${encodeURIComponent(baselineId)}`);
    },
    getTelemetry: (runId) => {
      if (!runId) throw new Error("runId is required for getTelemetry");
      return apiRequest(`/api/runs/${encodeURIComponent(runId)}/telemetry`);
    },

    // ==========================================
    // ANNOTATIONS & ACTION TASKS
    // ==========================================
    getAnnotations: (runId) => {
      if (!runId) throw new Error("runId is required for getAnnotations");
      return apiRequest(`/api/runs/${encodeURIComponent(runId)}/annotations`);
    },
    addAnnotation: (runId, author, comment) => {
      if (!runId) throw new Error("runId is required for addAnnotation");
      return apiRequest(`/api/runs/${encodeURIComponent(runId)}/annotations`, {
        method: "POST",
        body: { author, comment },
      });
    },
    getTasks: (runId) => {
      if (!runId) throw new Error("runId is required for getTasks");
      return apiRequest(`/api/runs/${encodeURIComponent(runId)}/tasks`);
    },
    addTask: (runId, title, priority = "medium", assignee = "Developer") => {
      if (!runId) throw new Error("runId is required for addTask");
      return apiRequest(`/api/runs/${encodeURIComponent(runId)}/tasks`, {
        method: "POST",
        body: { title, priority, assignee },
      });
    },
    updateTask: (taskId, status) => {
      if (!taskId) throw new Error("taskId is required for updateTask");
      return apiRequest(`/api/tasks/${encodeURIComponent(taskId)}`, {
        method: "PATCH",
        body: { status },
      });
    },

    // ==========================================
    // SUITES & TEMPLATES
    // ==========================================
    getSuites: () => apiRequest("/api/suites"),
    getSuite: (suiteId) => {
      if (!suiteId) throw new Error("suiteId is required for getSuite");
      return apiRequest(`/api/suites/${encodeURIComponent(suiteId)}`);
    },
    getTemplates: () => apiRequest("/api/templates"),
    instantiateTemplate: (templateId, scenarioId) => {
      if (!templateId || !scenarioId) throw new Error("templateId and scenarioId are required for instantiateTemplate");
      return apiRequest(`/api/templates/${encodeURIComponent(templateId)}/instantiate`, {
        method: "POST",
        body: { scenario_id: scenarioId },
      });
    },

    // ==========================================
    // COVERAGE, PROJECTS & SYSTEM METRICS
    // ==========================================
    getCoverage: () => apiRequest("/api/coverage"),
    getMetrics: () => apiRequest("/api/metrics"),
    getProjects: () => apiRequest("/api/projects"),
    getAuditEvents: (projectId) => {
      if (!projectId) throw new Error("projectId is required for getAuditEvents");
      return apiRequest(`/api/projects/${encodeURIComponent(projectId)}/audit-events`);
    },
    pruneRuns: (projectId, maxAgeDays = 30, keepBaselines = true, dryRun = false) => {
      if (!projectId) throw new Error("projectId is required for pruneRuns");
      return apiRequest(`/api/projects/${encodeURIComponent(projectId)}/prune`, {
        method: "POST",
        body: { max_age_days: maxAgeDays, keep_baselines: keepBaselines, dry_run: dryRun },
      });
    },
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
