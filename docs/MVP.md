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

### M0 — Project foundation

- Agree on the vertical slice and its acceptance criteria.
- Keep the starting demo dependency-free and local.
- Decide the production stack after inspecting contributor/runtime needs; do not split into services or packages prematurely.

**Exit:** a new contributor can understand the intended workflow and run the demo with a documented runtime.

### M1 — Reproducible checkout defect

- Provide vulnerable and idempotent checkout variants.
- Provide a deterministic payment ledger and response-loss fault point.
- Print an explicit invariant verdict and useful evidence (order ID, attempts, and charges).

**Exit:** vulnerable behavior produces two successful charges and fails the invariant; fixed behavior produces one and passes.

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
