# Faultline

Faultline is an integration failure testing platform. It runs controlled dependency failures against a checkout workflow, records cross-service evidence, evaluates business invariants, and keeps a report that developers can inspect after the run.

## Run the application

Requires Python 3.10 or later. The initial product slice uses only the Python standard library and SQLite; it has no package installation step.

```sh
python -m faultline
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The application starts the control-plane API and two loopback-only test targets:

- **Checkout service** on `127.0.0.1:8766`
- **Payment provider** on `127.0.0.1:8767`

Use **Run vulnerable** to see the duplicate-charge defect, or **Run fixed** to verify that the same retry uses an idempotency key. Each run executes asynchronously, saves its events and assertion results in SQLite, and appears in the run history. Data is stored in `data/faultline.sqlite3`.

Use the visual scenario designer to create or version a scenario. Runs can be cancelled from the run inspector; each scenario has a bounded `timeout_seconds` setting from 2 to 60 seconds (15 by default). Assertion evidence links jump to the matching event in the timeline.

For detailed server, adapter, and assertion logs, start with `python -m faultline --debug`.

## What the first product slice does

- Serves a workspace dashboard and control-plane API.
- Validates the checked-in, versioned scenario before execution.
- Sends real HTTP requests from the sample checkout app to a local test provider.
- Commits a test charge, then closes the provider connection to simulate a lost response.
- Retries the checkout operation with or without an idempotency key.
- Evaluates four invariants and persists a report, event timeline, and provider ledger.
- Restricts target services to loopback; the scenario cannot supply arbitrary destinations.
- Allows scenario creation/versioning in the dashboard, with each run retaining its scenario version.
- Exposes request/response evidence, reproduction JSON downloads, and a CLI client that submits runs through the API.
- Provides a visual workflow designer, assertion-to-event links, bounded scenario timeouts, and cancellable run lifecycles.

The fault is local and deterministic. It makes no calls to external services and is not production chaos tooling.

## Checks

```sh
python -m unittest discover -s tests -v
python -m faultline.cli run --scenario payment-response-lost --implementation fixed
python -m faultline.cli run --scenario duplicate-payment-webhook --implementation fixed
python -m faultline.cli run --scenario partial-persistence-failure --implementation fixed
```

The CLI exits `0` for a passing run, `1` for an invariant failure, `2` for an execution failure, `3` for a timeout, and `4` for cancellation.

Run `python -m compileall -q faultline tests` and `node --check faultline/web/app.js` for Python and browser-script syntax checks. VS Code launch and task definitions are in `.vscode/`.

## Project plan

See [docs/MVP.md](docs/MVP.md) for product scope, phased acceptance criteria, and GitHub checkpoint guidance.
