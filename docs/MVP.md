# MVP plan

## Product goal

Prove the core Faultline promise with one reproducible local workflow: apply a failure after a test payment provider commits a charge but before checkout receives the response, then evaluate business state and preserve evidence for a regression run.

## First vertical slice

### Workflow

1. Create a checkout order with a stable logical order ID.
2. Call a controlled payment provider that records successful charges in a ledger.
3. On the first provider call, commit the charge and simulate a lost response.
4. Let checkout retry the uncertain operation.
5. Inspect the provider ledger and evaluate `no_duplicate_charge`.
6. Repeat using a stable idempotency key and verify that only one charge exists.

### Initial invariants

- At most one successful charge exists for a logical order.
- A completed order has a successful payment.
- The scenario reaches a known terminal outcome.

The first simulation implements only the duplicate-charge invariant. The other invariants belong in the next runner/assertion milestone, once the scenario and evidence models exist.

## Milestones

### Completed at project kickoff

Phase 0 and Phase 1 are complete for the local demo slice. The production platform stack is intentionally not locked yet; Python 3.10+ is only the dependency-free runtime for this proof of concept.

- [x] Document the product slice, acceptance criteria, safe local scope, and next checkpoints.
- [x] Document the one-command local run path and Python runtime.
- [x] Provide a versioned scenario definition with validation.
- [x] Reproduce the lost-response duplicate-charge bug and the idempotency fix against a deterministic test-provider ledger.
- [x] Report order ID, attempts, charges, invariant verdict, run ID, and ordered evidence events.
- [x] Add automated checks for the seeded defect, fixed behavior, scenario validation, and CLI result codes.

The first implementation is an in-process domain simulation, not a real HTTP adapter or network fault proxy. Those remain in the scenario-runner/integration milestone.

### M0 — Project foundation

- Agree on the vertical slice and its acceptance criteria.
- Keep the starting demo dependency-free and local.
- Use Python 3.10+ for the local proof of concept; defer the production platform stack until contributor/runtime needs are clearer. Do not split into services or packages prematurely.

**Status: Complete.** A new contributor can understand the intended workflow and run the demo with the documented runtime.

### M1 — Reproducible checkout defect

- Provide vulnerable and idempotent checkout variants.
- Provide a deterministic payment ledger and response-loss fault point.
- Print an explicit invariant verdict and useful evidence (order ID, attempts, and charges).

**Status: Complete.** Vulnerable behavior produces two successful charges and fails the invariant; fixed behavior produces one and passes. Automated checks cover this known defect and the regression behavior.

### M2 — Scenario runner and evidence

- Define and validate a small versioned scenario format.
- Model run status and ordered events.
- Implement one controlled HTTP/provider fault adapter.
- Persist run events and assertion results.

**Exit:** one scenario runs repeatably and returns an evidence-backed verdict.

### M3 — Usable inspection

- Add scenario/project management and a run results view.
- Show run status, event timeline, assertion results, and evidence after completion.
- Add basic timeline replay.

**Exit:** a developer can inspect a completed run without executing it again.

### M4 — Regression and CI

- Export a sanitized reproduction bundle.
- Add CLI result codes for pass, assertion failure, and execution failure.
- Run the deterministic scenario in isolated CI.

**Exit:** the vulnerable case fails and the fixed case passes from the same scenario definition in local and CI workflows.

### M5 — Broaden failure coverage

- Add duplicate webhook and persistence failure scenarios.
- Add asynchronous assertions, cleanup ownership/reporting, and additional adapters only as supported end-to-end.

**Exit:** multiple failure classes expose cross-service correctness defects with recorded evidence.

## GitHub workflow and checkpoints

- Track each milestone as a GitHub milestone; create focused issues for independently reviewable slices.
- Link every PR to an issue and name the acceptance criterion it closes.
- Keep scenario definitions and their expected verdicts in the same PR as the behavior they exercise.
- Use one PR per vertical boundary (demo, scenario schema, runner, assertion/report, UI, CLI/CI) rather than one PR per speculative package.
- Require a human-readable reproduction command and sample output for behavioral changes.
- Add CI gates as the selected stack is established; keep their results visible on the PR.

## Out of scope for the first milestone

- Production chaos injection or arbitrary user-provided executable scenario code.
- Kubernetes or broad infrastructure orchestration.
- Multiple language SDKs, databases, brokers, and dozens of adapters.
- Authentication, billing, collaboration, and advanced visual scenario editing.

## MVP completion criteria

- A new developer can start the isolated project using documented steps.
- Normal checkout succeeds.
- The seeded duplicate-charge defect is detected by an invariant, with relevant evidence.
- A completed run is inspectable without re-execution.
- The deterministic case produces a consistent verdict.
- Scenario configuration is validated before execution.
- Target allowlists, resource limits, cleanup, and secret redaction are enforced before expanding beyond the local demo.
