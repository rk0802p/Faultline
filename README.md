# Faultline

**Faultline** is an integration failure testing platform that tests whether distributed systems remain correct when a dependency fails at an awkward point. Rather than merely verifying HTTP status codes, Faultline injects controlled, deterministic dependency failures against critical business workflows (such as e-commerce checkout, payment captures, and webhook ingestion), records cross-service execution evidence, evaluates business invariants, and produces reproducible test reports.

---

## Architecture Overview

```text
 ┌─────────────────────────────────────────────────────────────┐
 │                 Faultline Control Plane                     │
 │  ┌────────────────────────┐    ┌─────────────────────────┐  │
 │  │    REST API Server     │    │  Visual Web Dashboard   │  │
 │  │      (Port 8000)       │    │   (Timeline & Water)    │  │
 │  └───────────┬────────────┘    └────────────┬────────────┘  │
 │              │                              │               │
 │  ┌───────────▼──────────────────────────────▼────────────┐  │
 │  │         Execution Runner & State Machine              │  │
 │  │   (queued -> running -> evaluating -> completed)      │  │
 │  └───────────┬──────────────────────────────┬────────────┘  │
 │              │                              │               │
 │  ┌───────────▼────────────┐    ┌────────────▼────────────┐  │
 │  │    Invariant Engine    │    │   OpenTelemetry Tracer  │  │
 │  │  (Domain Assertions)   │    │  (W3C traceparent Prop) │  │
 │  └───────────┬────────────┘    └────────────┬────────────┘  │
 └──────────────┼──────────────────────────────┼───────────────┘
                │ Evidence & State             │
 ┌──────────────▼──────────────────────────────▼───────────────┐
 │               SQLite Storage Layer (WAL Mode)               │
 │    runs · events · assertions · snapshots · telemetry       │
 └──────────────────────────────┬──────────────────────────────┘
                                │
                  Fault Injection & HTTP Calls
                                │
        ┌───────────────────────┴───────────────────────┐
        ▼                                               ▼
┌──────────────────────────────┐        ┌──────────────────────────────┐
│       Checkout Service       │───────>│    Test Payment Provider     │
│         (Port 8766)          │  HTTP  │         (Port 8767)          │
│   (Vulnerable vs. Fixed)     │        │   (Controlled Loss Adapter)  │
└──────────────────────────────┘        └──────────────────────────────┘
```

### Architecture & Infrastructure Note
- **Core Engine:** Built purely with Python 3.10+ standard library and SQLite WAL mode. Background tasks execute asynchronously via in-process threads, requiring zero third-party dependencies.
- **Real Execution vs. Simulated Adapters:** Faultline executes real HTTP requests over live sockets with strict TLS verification, loopback protection, and response evidence capture for network operations (e.g. `http_timeout`, webhook deliveries, and HTTP checkout calls). For operations requiring internal state checkpoints (such as checkout persistence failures and generic database steps), deterministic application-level adapters simulate faults with explicit labeling (`DB_OPERATION_SIMULATED`).
- **Sidecar Infrastructure (Docker Compose):** Redis and Shopify Toxiproxy containers are provided as optional demonstration infrastructure. The core engine executes runs asynchronously via in-process threads and deterministic adapters to guarantee reproducible outcomes without requiring external daemons.

---

## Key Features

- **Controlled Failure Injection (Spec §7):** Simulates lost responses after commitment, duplicated delivery of payment webhooks, and partial persistence failures after payment captures.
- **Deterministic Invariant Verification (Spec §9):** Evaluates multi-dimensional domain assertions (`no_duplicate_charge`, `idempotent_webhook_processing`, `charge_compensated_on_failure`) directly against recorded evidence trails.
- **Execution Timeline & Point-in-Time State Reconstruction (Spec §6.3 & §15):** Step-by-step playback, event scrubbing, and incremental snapshots (`state_snapshots`) allowing zero-side-effect state inspection.
- **Checkpoint Branching (Spec §4.55 & §25.2):** Create a linked branch run with recorded checkpoint snapshot context and validated scenario/implementation overrides to explore remediation logic while preserving parent run immutability.
- **OpenTelemetry Distributed Tracing & W3C Trace Context (Spec §11.5 & §14):** End-to-end span hierarchy (scenario root, HTTP client, fault interceptor, invariant evaluator, environment cleanup) with outgoing `traceparent` headers and OTLP-compliant JSON endpoints (`/api/runs/:runId/telemetry` and `/api/runs/:runId/traces`).
- **Regression Suites & Baseline Diffing (Spec §6.10 & §14):** Group scenarios into regression suites (`test_suites`), execute synchronous or background suite runs, designate baseline runs, and detect regression states (`stable_pass`, `regression_resolved`, `regression_introduced`, `stable_failure`).
- **Automated Workflow Discovery (Spec §6.7):** Import OpenAPI 3.0 / Swagger specs to automatically discover endpoints, map dependency topologies, and generate integration test scenarios.
- **Failure Coverage Matrix & Gap Analysis (Spec §6.5):** Visual matrix mapping workflow steps to failure classes, with one-click automated scenario generation for uncovered reliability gaps.
- **Governance & Collaboration (Spec §6.6, §6.13, §16):** Run annotations, defect remediation action tasks, best-effort secret redaction (credentials, tokens, PANs), and an append-only audit trail (`audit_events`).
- **Operational Metrics & Retention Governance (Spec §16 & §21.2):** Live operational metrics endpoint (`GET /api/metrics`) supporting both JSON and Prometheus scrapable formats, accompanied by a configurable artifact retention and pruning policy.
- **Programmatic Python SDK (Spec §11.6 & §20):** High-level `FaultlineClient` for integrating resilience scenarios and trace contexts directly into developer test suites.

---

## Quickstart

### Prerequisites
- Python 3.10 or higher.
- Standard library only (no `pip install` required for local execution).

### 1. Local Monolith (Single Command)

```sh
# Clone the repository
git clone https://github.com/rk0802p/Faultline.git
cd Faultline

# Start the Faultline server daemon
python -m faultline
```

**Expected Startup Output:**
```text
Faultline is ready at http://127.0.0.1:8000
Local test targets: checkout 127.0.0.1:8766, payment provider 127.0.0.1:8767
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser to access the visual workspace dashboard.

### 2. Local Multi-Container Development (Docker Compose)

To run the decomposed multi-container demonstration environment:

```sh
docker compose up --build
```

This starts:
- `control-plane`: API, Web Dashboard, and Runner bound to `127.0.0.1:8000`
- `checkout-service`: Standalone checkout microservice bound to `127.0.0.1:8766`
- `payment-provider`: Standalone payment provider microservice bound to `127.0.0.1:8767`
- `toxiproxy`: Optional Shopify Toxiproxy network proxy bound to `127.0.0.1:8474`
- `redis`: Optional Redis demonstration container bound to `127.0.0.1:6379`

*Note: All container ports are explicitly bound to `127.0.0.1` to prevent unintentional exposure on external host interfaces.*

---

## Seeded Failure Scenarios

| Scenario ID | Fault Class | Invariant Evaluated | Vulnerable Behavior | Fixed Behavior |
| :--- | :--- | :--- | :--- | :--- |
| `payment-response-lost` | Lost HTTP response after charge commit | `no_duplicate_charge` | Blind retry creates 2 separate charges | Retry passes idempotency key; charge is reused |
| `duplicate-payment-webhook` | Network redelivery of payment webhook | `idempotent_webhook_processing` | Creates multiple fulfillment records | Deduplicates event ID; single fulfillment record |
| `partial-persistence-failure` | Database write failure after payment commit | `charge_compensated_on_failure` | Leaves orphaned uncompensated charge | Issues automated compensating refund |

---

## CLI Usage

The Faultline CLI (`faultline.cli` or the `faultline` command after installation) provides headless automation for CI/CD pipelines, benchmarks, and maintenance:

### Execute Scenarios & Suites
```sh
# Run a single scenario against vulnerable or fixed implementation
python -m faultline.cli run --scenario payment-response-lost --implementation fixed

# Run an entire test suite and output JUnit XML for CI test reporting
python -m faultline.cli run --suite critical-integrations --implementation fixed --report-format junit --output test-results.xml
```

### Defect Detection Benchmark
```sh
# Run automated benchmark against all seeded defects to verify detection and false-positive rates
python -m faultline.cli benchmark
```

**Illustrative Benchmark Output:**
```text
============================================================
Faultline Integration Reliability Benchmark (Spec §17.4)
============================================================
Benchmark Results across 3 Integration Scenarios:
------------------------------------------------------------
  * payment-response-lost:
      Vulnerable run: PASS (defect caught)
      Fixed run:      PASS (verified)
  * duplicate-payment-webhook:
      Vulnerable run: PASS (defect caught)
      Fixed run:      PASS (verified)
  * partial-persistence-failure:
      Vulnerable run: PASS (defect caught)
      Fixed run:      PASS (verified)
------------------------------------------------------------
Defect Detection Rate:           100.0% (3/3 caught)
Fixed Verification Pass Rate:    100.0% (3/3 passed)
Fixed Verification Failure Rate: 0.0% (0/3)
Evaluator Determinism:           NOT MEASURED (use --measure-determinism)
Scenarios Verified:              3/3 consistent outcomes
============================================================
Benchmark PASSED: All seeded integration defects detected and verified.
```

To run with repeated determinism verification:
```sh
python -m faultline.cli benchmark --measure-determinism
```

### Retrospective Reports & Reusable Templates
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

# Prune old run artifacts and traces older than 30 days (supports --dry-run)
python -m faultline.cli prune --project faultline-demo --days 30 --dry-run
```

### CLI Exit Codes
- `0`: All invariant assertions passed (or fixed implementation verified)
- `1`: Invariant assertion failed (domain defect caught in the application under test)
- `2`: System, execution, or environment error (service down, invalid payload, etc.)
- `3`: Scenario execution timed out
- `4`: Run was cancelled

---

## Programmatic Python SDK

You can import `FaultlineClient` directly into your application test suites:

```python
from faultline import FaultlineClient, FaultlineInvariantError

client = FaultlineClient(base_url="http://127.0.0.1:8000")

# Run fixed implementation and assert invariant success
run = client.run_scenario("payment-response-lost", implementation="fixed", assert_pass=True)
print(f"Run {run['id']} passed with verdict: {run['verdict']}")

# Verify that vulnerable implementation triggers an invariant error
try:
    client.run_scenario("payment-response-lost", implementation="vulnerable", assert_pass=True)
except FaultlineInvariantError as exc:
    print(f"Caught expected defect: {exc.failed_assertions[0]['name']}")

# Inject W3C Trace Context into downstream HTTP requests
with client.trace_context(run["id"]) as headers:
    # headers contains {'traceparent': '00-{trace_id}-{span_id}-01'}
    pass
```

A runnable reference demo is available at [`examples/checkout_demo.py`](examples/checkout_demo.py).

---

## Comprehensive REST API Reference

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| **System & Health** | | |
| `GET` | `/api/health` | Service health status check |
| `GET` | `/api/metrics` | Operational metrics (supports `?format=prometheus` and `Accept: text/plain`) |
| **Projects & Governance** | | |
| `GET` | `/api/projects` | List active projects |
| `GET` | `/api/projects/:projectId/services` | Service topology registry and health status |
| `GET` | `/api/projects/:projectId/audit-events` | Immutable audit log trail |
| `POST` | `/api/projects/:projectId/prune` | Clean up old runs, snapshots, and traces by retention policy |
| `GET` | `/api/projects/:projectId/tasks` | List defect action tasks |
| **Scenarios & Templates** | | |
| `GET` | `/api/scenarios` | List registered failure scenarios |
| `GET` | `/api/scenarios/supported-faults` | List supported faults, schema metadata, and execution pipelines |
| `POST` | `/api/scenarios` | Register a new scenario definition |
| `POST` | `/api/scenarios/validate` | Validate scenario syntax and invariant rules |
| `POST` | `/api/scenarios/import-incident` | Import production incident JSON into a reproducible scenario |
| `POST` | `/api/scenarios/generate-from-gaps` | Auto-generate scenarios from uncovered matrix gaps |
| `GET` | `/api/scenarios/:scenarioId` | Retrieve scenario metadata and definition |
| `GET` | `/api/scenarios/:scenarioId/versions` | List immutable versions for a scenario |
| `POST` | `/api/scenarios/:scenarioId/versions` | Save a new immutable scenario version |
| `POST` | `/api/scenarios/:scenarioId/versions/:version/clone` | Clone a scenario version into a new scenario |
| `GET` | `/api/scenarios/:scenarioId/baseline-diff` | Compare latest run against designated baseline |
| `GET` | `/api/templates` | List reusable failure templates |
| `GET` | `/api/templates/:templateId` | Get template parameter schema and default invariants |
| `POST` | `/api/templates/:templateId/instantiate` | Instantiate template into an active scenario |
| `POST` | `/api/workflows/import-openapi` | Parse OpenAPI 3.0 / Swagger spec and generate workflow |
| `GET` | `/api/coverage` | Failure coverage matrix mapping workflow steps to faults |
| **Runs & Replay** | | |
| `POST` | `/api/runs` | Submit asynchronous scenario run |
| `GET` | `/api/runs` | List historical scenario runs |
| `GET` | `/api/runs/:runId` | Retrieve run details, assertions, and summary |
| `POST` | `/api/runs/:runId/cancel` | Cancel an active or queued run |
| `POST` | `/api/runs/:runId/branch` | Create a linked branch run with checkpoint snapshot context and validated overrides |
| `POST` | `/api/runs/:runId/baseline` | Designate a run as regression baseline |
| `GET` | `/api/runs/:runId/events` | List ordered execution events for a run |
| `GET` | `/api/runs/:runId/assertions` | List invariant evaluation results |
| `GET` | `/api/runs/:runId/diff/:otherRunId` | Side-by-side comparison of two runs |
| `GET` | `/api/runs/:runId/snapshots` | List point-in-time state snapshots |
| `GET` | `/api/runs/:runId/state` | Reconstruct application state at specific sequence |
| `GET` | `/api/runs/:runId/telemetry` | OpenTelemetry distributed trace spans |
| `GET` | `/api/runs/:runId/traces` | OTLP-compliant JSON trace export |
| `GET` | `/api/runs/:runId/reproduce` | Export standalone reproduction fixture bundle |
| `GET` | `/api/runs/:runId/report` | Retrospective run report (JSON or Markdown) |
| **Collaboration & Suites** | | |
| `GET` | `/api/runs/:runId/annotations` | List review annotations on a run |
| `POST` | `/api/runs/:runId/annotations` | Add review annotation to a run |
| `POST` | `/api/runs/:runId/tasks` | Create defect remediation action task linked to a run |
| `PATCH` | `/api/tasks/:taskId` | Update task status or assignee |
| `GET` | `/api/suites` | List regression test suites |
| `POST` | `/api/suites` | Create a test suite |
| `GET` | `/api/suites/:suiteId` | Get test suite details and member scenarios |
| `POST` | `/api/suites/:suiteId/scenarios` | Add scenario to a test suite |
| `POST` | `/api/suites/:suiteId/runs` | Execute test suite |
| `GET` | `/api/suites/:suiteId/results` | Historical test suite execution results |

---

## Automated Verification & CI Results

The repository includes a comprehensive automated test suite with **95 automated tests and 5 subtests passing** across unit, platform, CLI, and SDK test files:

```sh
# Run full automated test suite
python -m unittest discover -s tests -v

# Run syntax compilation and JavaScript validation checks
python -m compileall -q faultline tests examples
node --check faultline/web/app.js faultline/web/api.js
```

### GitHub Actions CI Workflow
The CI pipeline (`.github/workflows/ci.yml`) runs on every push and pull request:
1. Python bytecode compilation (`compileall`) and client JavaScript validation (`node --check`).
2. Complete unit, platform, and SDK test suite execution (95+ passing tests and subtests).
3. Live platform daemon startup.
4. Critical Integrations regression suite execution with JUnit XML report generation.
5. Automated defect benchmark execution verifying 100% detection rate.
6. Operational metrics and Prometheus export verification.
7. Artifact retention pruning dry-run verification.
8. Programmatic SDK checkout demo execution.

---

## Safety and Security Posture (Spec §16)

- **Loopback & Container Destination Boundary:** Outbound network destinations are enforced strictly at connection boundaries (`assert_loopback_safe()`). Only loopback (`127.0.0.1`, `localhost`, `::1`) or explicitly configured container host aliases (`FAULTLINE_ALLOWED_HOSTS`) are accepted. The wildcard bind address (`0.0.0.0`), unsupported schemes (`ftp://`, `file://`), embedded credentials, and arbitrary external URLs are rejected with `PermissionError`.
- **Local vs. Container Mode:** In local development, the server listener binds strictly to `127.0.0.1`. In container environments (such as Docker Compose), `FAULTLINE_BIND_ALL=1` (or `FAULTLINE_ALLOW_EXTERNAL=1`) permits the HTTP server listener to bind to `0.0.0.0` within the private container bridge network. This setting governs the listener interface only; outbound destination calls remain strictly restricted to loopback and configured internal container service ports.
- **Best-Effort Sensitive Data Redaction:** Diagnostic payloads and event streams undergo recursive pattern-based redaction for recognized tokens (`Bearer`, `sk_live_*`, `pk_test_*`), passwords, credentials, and payment card numbers (`[REDACTED_PAN]`). Review redaction coverage before introducing sensitive production data.
- **Append-Only Audit Logging:** Scenario version mutations, run submissions, cancellations, and data retention pruning emit immutable entries into `audit_events`.
