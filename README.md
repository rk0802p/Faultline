# Faultline

**Faultline** is an integration failure testing platform that tests whether distributed systems remain correct when an integration or dependency fails at an awkward point. It injects controlled, deterministic dependency failures against critical business workflows (such as e-commerce checkout, payment captures, and webhook ingestion), records cross-service evidence, evaluates business invariants, and produces reproducible test reports.

---

## Key Features

- **Controlled Failure Injection (Spec §7):** Simulates lost responses after commitment, duplicated delivery of payment webhooks, and partial persistence failures after payment captures.
- **Deterministic Invariant Verification (Spec §9):** Evaluates multi-dimensional domain assertions (`no_duplicate_charge`, `idempotent_webhook_processing`, `charge_compensated_on_failure`) directly against recorded evidence trails.
- **Execution Timeline & Point-in-Time State Reconstruction (Spec §6.3 & §15):** Step-by-step playback, event scrubbing, and incremental snapshots (`state_snapshots`) allowing zero-side-effect state inspection.
- **Checkpoint Branching (Spec §4.55 & §25.2):** Fork execution from any historical state snapshot to explore alternative retry configurations or remediation logic while keeping the parent run immutable.
- **OpenTelemetry Distributed Tracing & W3C Trace Context (Spec §11.5 & §14):** End-to-end span hierarchy (scenario root, HTTP client, fault interceptor, invariant evaluator, environment cleanup) with outgoing `traceparent` headers and OTLP-compliant JSON endpoints (`/api/runs/:runId/telemetry` and `/api/runs/:runId/traces`).
- **Regression Suites & Baseline Diffing (Spec §6.10 & §14):** Group scenarios into regression suites (`test_suites`), execute synchronous or background suite runs, designate baseline runs, and detect regression states (`stable_pass`, `regression_resolved`, `regression_introduced`, `stable_failure`).
- **Automated Workflow Discovery (Spec §6.7):** Import OpenAPI 3.0 / Swagger specs to automatically discover endpoints, map dependency topologies, and generate integration test scenarios.
- **Failure Coverage Matrix & Gap Analysis (Spec §6.5):** Visual matrix mapping workflow steps to failure classes, with one-click automated scenario generation for uncovered reliability gaps.
- **Governance & Collaboration (Spec §6.6, §6.13, §16):** Run annotations, defect remediation action tasks, secret redaction (credentials, tokens, PANs), and an append-only audit trail (`audit_events`).
- **Operational Metrics & Retention Governance (Spec §16 & §21.2):** Live operational metrics endpoint (`GET /api/metrics`) supporting both JSON and Prometheus scrapable formats, accompanied by a configurable artifact retention and pruning policy.
- **Multi-Container Decomposition (Spec §20 & §21):** Production-ready Docker Compose orchestration decoupling the control plane from independent target microservices, Redis, and Shopify Toxiproxy.

---

## Quickstart

### 1. Local Monolith (Zero Dependencies)

Faultline is built using Python 3.10+ and standard library modules with embedded SQLite; no third-party package installation is required:

```sh
python -m faultline
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser. The server starts the control-plane API, the interactive Web dashboard, and two local services:
- **Checkout service** on `127.0.0.1:8766`
- **Test payment provider** on `127.0.0.1:8767`

### 2. Multi-Container Microservices (Docker Compose)

Deploy the fully decomposed multi-service architecture including Toxiproxy and Redis:

```sh
docker compose up --build
```

This launches:
- `control-plane`: API, Web Dashboard, and Runner on `http://127.0.0.1:8000`
- `checkout-service`: Standalone checkout microservice on `127.0.0.1:8766`
- `payment-provider`: Standalone payment provider microservice on `127.0.0.1:8767`
- `toxiproxy`: Shopify Toxiproxy network fault proxy on `127.0.0.1:8474`
- `redis`: Distributed queue & cache container on `127.0.0.1:6379`

---

## Seeded Failure Scenarios

| Scenario ID | Fault Class | Invariant Evaluated | Vulnerable Behavior | Fixed Behavior |
| :--- | :--- | :--- | :--- | :--- |
| `payment-response-lost` | Lost HTTP response after charge commit | `no_duplicate_charge` | Blind retry creates 2 separate charges | Retry passes idempotent key; charges reused |
| `duplicate-payment-webhook` | Network redelivery of payment webhook | `idempotent_webhook_processing` | Creates multiple fulfillment records | Deduplicates event ID; single fulfillment |
| `partial-persistence-failure` | Database write failure after payment commit | `charge_compensated_on_failure` | Leaves orphaned uncompensated charge | Issues automated compensating refund |

---

## CLI Usage

The Faultline CLI (`faultline.cli`) provides complete headless control for CI/CD pipelines, benchmarks, and maintenance:

### Execute Scenarios & Suites
```sh
# Run a single scenario against fixed or vulnerable implementation
python -m faultline.cli run --scenario payment-response-lost --implementation fixed

# Run an entire test suite and output JUnit XML for CI test reporting
python -m faultline.cli run --suite critical-integrations --implementation fixed --report-format junit --output test-results.xml
```

### Defect Detection Benchmark
```sh
# Run automated benchmark against all seeded defects to verify detection and false-positive rates
python -m faultline.cli benchmark
```

### Inspect Reports & Templates
```sh
# Generate Markdown or JSON post-run retrospective reports
python -m faultline.cli report --report-format markdown

# List catalog of pre-configured scenario templates
python -m faultline.cli templates
```

### Operational Metrics & Retention Governance
```sh
# Inspect live control plane uptime, worker health, and assertion pass rates
python -m faultline.cli metrics

# Prune old run artifacts and traces older than 30 days while preserving baselines
python -m faultline.cli prune --project faultline-demo --days 30
```

### CLI Exit Codes
- `0`: All assertions passed (or fixed implementation verified)
- `1`: Invariant assertion failed (defect caught)
- `2`: System or environment execution error
- `3`: Run timed out
- `4`: Run was cancelled

---

## REST API Overview

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/health` | Service health status check |
| `GET` | `/api/metrics` | System operational metrics (supports `?format=prometheus` and `Accept: text/plain`) |
| `GET` | `/api/projects` | List active projects |
| `GET` | `/api/projects/:projectId/services` | Service topology registry and health status |
| `GET` | `/api/projects/:projectId/audit-events` | Immutable audit log trail |
| `POST` | `/api/projects/:projectId/prune` | Clean up old runs, snapshots, and traces by retention policy |
| `GET` | `/api/scenarios` | List registered failure scenarios |
| `POST` | `/api/scenarios` | Register a new scenario definition |
| `POST` | `/api/scenarios/validate` | Validate scenario syntax and invariants |
| `POST` | `/api/scenarios/:scenarioId/versions` | Save a new immutable scenario version |
| `POST` | `/api/scenarios/:scenarioId/versions/:version/clone` | Clone a scenario version into a new scenario |
| `GET` | `/api/scenarios/:scenarioId/baseline-diff` | Compare latest run against designated baseline |
| `POST` | `/api/runs` | Submit asynchronous scenario run |
| `GET` | `/api/runs/:runId` | Retrieve run details, assertions, and summary |
| `POST` | `/api/runs/:runId/cancel` | Cancel an active run |
| `POST` | `/api/runs/:runId/branch` | Fork a new run from a specific state snapshot |
| `POST` | `/api/runs/:runId/baseline` | Designate a run as regression baseline |
| `GET` | `/api/runs/:runId/snapshots` | List point-in-time state snapshots |
| `GET` | `/api/runs/:runId/state` | Reconstruct application state at specific sequence |
| `GET` | `/api/runs/:runId/telemetry` | OpenTelemetry distributed trace spans |
| `GET` | `/api/runs/:runId/report` | Retrospective run report (JSON or Markdown) |
| `GET` | `/api/suites` | List regression test suites |
| `POST` | `/api/suites` | Create a test suite |
| `POST` | `/api/suites/:suiteId/runs` | Execute test suite |
| `GET` | `/api/suites/:suiteId/results` | Historical test suite execution results |
| `GET` | `/api/templates` | List reusable failure templates |
| `POST` | `/api/templates/:templateId/instantiate` | Instantiate template into a scenario |
| `POST` | `/api/workflows/import-openapi` | Parse OpenAPI/Swagger spec and generate workflow |
| `GET` | `/api/coverage` | Failure coverage matrix |
| `POST` | `/api/scenarios/generate-from-gaps` | Auto-generate scenarios from uncovered matrix gaps |

---

## Automated Verification & Tests

Faultline includes a comprehensive automated test suite with **100% passing tests** covering CLI workflows, platform APIs, tracing, state reconstruction, baseline diffing, operational metrics, and retention pruning:

```sh
# Run full automated test suite
python -m unittest discover -s tests -v

# Run syntax and compilation checks
python -m compileall -q faultline tests
node --check faultline/web/app.js
```

---

## Safety and Security Posture (Spec §16)

- **Loopback & Container Host Restriction:** All network requests are strictly bounded to loopback (`127.0.0.1`, `localhost`) or explicitly configured container hosts (`FAULTLINE_ALLOWED_HOSTS`). Arbitrary outbound requests are rejected with `PermissionError`.
- **Sensitive Data Redaction:** All HTTP payloads, headers, ledgers, and events automatically redact credentials, API keys, bearer tokens, and PAN card numbers before persistence and display.
- **Audit Trails:** Scenario modifications, version creations, cancellations, and data pruning emit immutable entries into `audit_events`.
