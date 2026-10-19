# Faultline

Faultline tests whether applications remain correct when their dependencies fail.

## First vertical slice

The first milestone is a local checkout demonstration of an ambiguous payment outcome:

1. The payment provider records a successful charge.
2. The response is lost, so checkout retries.
3. Faultline detects whether the retry created a duplicate charge.
4. The same scenario can be run against an idempotent implementation.

The first executable artifact is a small, dependency-free scenario runner. It reads a versioned JSON scenario, records an event history and charge ledger, and makes both the defect and its fix concrete before adding network adapters, storage, or a UI.

## Run the demonstration

Requires Python 3.10 or later. From the repository root:

```sh
python examples/checkout_demo.py
```

The script runs both vulnerable and fixed checkout behavior from `scenarios/payment-response-lost.json`. Use `--implementation fixed` for a passing run, or `--implementation vulnerable` to observe the seeded invariant failure. Assertion failures return exit code 1; execution/configuration failures return exit code 2. Pass `--json` to print full evidence, or `--output artifacts/run.json` to save a run bundle with the scenario definition, run ID, timestamps, event history, charge ledger, and assertion result.

Run the Phase 0 and 1 checks with:

```sh
python -m unittest discover -s tests -v
```

The demo and checks use only the Python standard library; there are no package installation steps.

## Scope and checkpoints

See [docs/MVP.md](docs/MVP.md) for the first milestone, acceptance criteria, and planned GitHub checkpoints.

## Safety

The initial demo is entirely local and makes no network calls. Faultline's first release is intended for isolated test environments, not production fault injection.
