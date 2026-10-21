# Faultline Checkout Integration Demo

This directory demonstrates how an application developer can programmatically integrate with Faultline to test resilience and distributed recovery workflows.

## What This Demo Demonstrates

1. **Vulnerable vs. Fixed Execution:**
   - Runs `payment-response-lost` against the unmitigated checkout implementation to show automated invariant failure detection (`no_duplicate_charge`).
   - Runs the same scenario against the idempotent checkout implementation to verify retry safety.
2. **OpenTelemetry Telemetry & W3C Trace Context:**
   - Extracts root and child spans (`http.client.checkout`, `fault.interceptor`, `invariant.evaluator`, `environment.cleanup`).
   - Inspects the W3C `traceparent` context header injected into outgoing requests.
3. **Exploratory Checkpoint Branching:**
   - Inspects recorded point-in-time state snapshots.
   - Branches a child run directly from a prior checkpoint without mutating the original run history.

## Running the Demo

1. Start the Faultline server daemon:
   ```sh
   python -m faultline
   ```

2. Run the demo script:
   ```sh
   python examples/checkout_demo.py
   ```
