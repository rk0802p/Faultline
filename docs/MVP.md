# Faultline product plan

## Product goal

Faultline helps developers test whether an application remains correct when an integration fails at an awkward point. It coordinates a workflow, injects a bounded dependency failure, evaluates business invariants, and stores evidence that can be inspected after the run.

## Architecture in the first product slice

- **Control plane:** local HTTP API for projects, scenarios, run submission, history, and reports.
- **Web app:** workspace overview with scenario launch controls, run status, invariant results, and persisted event timeline.
- **Execution worker:** background thread per submitted run. It uses a fixed loopback target and does not accept user-provided executable code or arbitrary URLs.
- **Sample checkout:** a real local HTTP service that retries a payment operation after an unknown outcome.
- **HTTP fault adapter:** the test payment provider commits the charge and closes the connection before returning a response.
- **Evidence store:** SQLite stores projects, scenario definitions, run state, ordered events, assertions, and the sanitized charge ledger.

The project uses Python 3.10+ and the standard library for this initial modular monolith. SQLite replaces PostgreSQL for the first local product slice so a contributor can start it without services or package installs. This is an implementation choice, not a requirement that future hosted deployments use Python or SQLite.

## Phase 0 — Product foundation

**Status: Complete.**

- [x] Convert the product brief into a bounded first vertical slice and acceptance criteria.
- [x] Add a runnable product entry point and documented local setup.
- [x] Keep the initial environment local-only and dependency-free.
- [x] Establish control-plane, execution, adapter, data, and web boundaries in a modular monolith.

**Exit:** a new contributor can start the product and understand the workflow and safe operating boundary.

## Phase 1 — Complete vertical slice

**Status: Complete for the payment response-lost scenario.**

1. The dashboard submits a versioned scenario through the control-plane API.
2. The worker validates the scenario and starts a run with a persisted lifecycle.
3. A local checkout service calls the local payment provider over HTTP.
4. The provider records a successful charge, then the HTTP fault adapter drops the response.
5. Checkout retries. The vulnerable implementation creates a duplicate; the fixed implementation reuses an idempotency key.
6. Faultline records ordered events and a charge ledger, evaluates four invariants, and shows the report in the UI and run history.

Delivered:

- [x] Project and scenario data model.
- [x] Versioned scenario definition and pre-run validation.
- [x] Controlled checkout HTTP service and payment provider.
- [x] HTTP response-loss fault after provider commit.
- [x] Asynchronous run submission, run status, persisted event history, and cleanup event.
- [x] Four business invariants with evidence references.
- [x] Results UI with run history, assertions, and timeline.
- [x] Automated checks for the seeded duplicate-charge bug and fixed behavior.

**Exit:** Faultline catches the seeded duplicate-charge bug and records supporting evidence. The vulnerable run fails `no_duplicate_charge`; the fixed retry passes with one charge.

## Phase 2 — Usable authoring and debugging

**Status: Complete for the current payment response-lost workflow.**

- [x] Create scenarios, edit definitions into immutable versions, and receive pre-save validation feedback.
- [x] Pin each run to the scenario version used and retain a SHA-256 definition checksum.
- [x] Record sanitized checkout/payment HTTP request and response bodies in the event timeline.
- [x] Download a JSON reproduction bundle with the scenario, run evidence, and replay command.
- [x] Execute runs through the same API using `python -m faultline.cli run --scenario payment-response-lost --implementation fixed`.
- [x] Enforce run transitions through queued, running, cancellation requested, and terminal states; support cancellation and scenario timeouts.
- [x] Keep run status updates reconnectable by polling and retry transient API connection failures.
- [x] Link each invariant's evidence directly to its corresponding event in the timeline.
- [x] Provide a visual scenario designer for the current supported workflow, with an inspectable JSON definition.

**Exit:** a developer can author, validate, execute, inspect, cancel, and reproduce scenarios through both the product and CLI. Met for the current supported workflow.

## Phase 3 — Integration depth

**Status: Core failure scenarios complete.**

- [x] Add duplicate payment webhook scenario and test harness (`duplicate-payment-webhook`).
- [x] Detect duplicate fulfillment side effects on redelivery (vulnerable fails, fixed deduplicates).
- [x] Add persistence failure scenario after side effect (`partial-persistence-failure`).
- [x] Verify compensating refunds reconcile charges when order persistence fails.
- [x] Add interactive step-by-step playback and seek controls to the execution timeline.
- [x] Provide Dockerfile and Docker Compose infrastructure configurations.
- [ ] Add failure coverage matrix and reusable scenario templates.
- [ ] Report cleanup ownership and failures separately from scenario verdicts.

**Exit:** multiple failure classes expose cross-service correctness defects with recorded evidence.

## Phase 4 — CI and regression workflows

- Add regression suites, GitHub Actions integration, JUnit reports, and historical comparisons.
- Enforce allowlists, resource/time limits, secret redaction, and cleanup in the CI execution path.

**Exit:** a seeded regression fails CI with a downloadable, sanitized reproduction artifact.

## Full MVP scope from the brief

The completed Phase 2 slice is the authoring and debugging foundation; it is not the full MVP. Remaining MVP work includes broadening beyond the payment response-lost workflow, Docker Compose deployment, reusable replay across supported scenario types, and full automated end-to-end coverage. Production chaos injection, arbitrary executable scenario code, Kubernetes orchestration, and dozens of adapters remain out of scope.

## GitHub checkpoints

- Use the Phase 1 completion as the first reviewable product checkpoint.
- Create one issue per Phase 2 capability and keep pull requests tied to its acceptance criterion.
- Keep each supported scenario definition, adapter behavior, assertions, and expected evidence together in review.
- Require a reproduction command and test outcome in behavior-changing pull requests.
- Keep local commits on `main`; the project owner will create the GitHub remote and push when ready.
