# Faultline

Faultline tests whether applications remain correct when their dependencies fail.

## First vertical slice

The first milestone is a local checkout demonstration of an ambiguous payment outcome:

1. The payment provider records a successful charge.
2. The response is lost, so checkout retries.
3. Faultline detects whether the retry created a duplicate charge.
4. The same scenario can be run against an idempotent implementation.

This repository is at project kickoff. The initial executable artifact is a small, dependency-free simulation that makes the defect and its fix concrete before adding orchestration, adapters, storage, or a UI.

## Run the demonstration

Requires Python 3.10 or later. From the repository root:

```sh
python examples/checkout_demo.py
```

The script runs both vulnerable and fixed checkout behavior and prints the charge count and `no_duplicate_charge` verdict for each.

## Scope and checkpoints

See [docs/MVP.md](docs/MVP.md) for the first milestone, acceptance criteria, and planned GitHub checkpoints.

## Safety

The initial demo is entirely local and makes no network calls. Faultline's first release is intended for isolated test environments, not production fault injection.
