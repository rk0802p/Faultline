"""Checkout Resilience Integration Demo (Spec §8 & §20).

Demonstrates programmatic usage of the Faultline SDK:
1. Submitting a scenario against the vulnerable checkout implementation.
2. Observing invariant violations (duplicate charges detected).
3. Submitting against the fixed checkout implementation with idempotency keys.
4. Verifying invariant success (single charge committed).
5. Inspecting OpenTelemetry distributed traces and W3C traceparent headers.
6. Checkpoint branching to fork execution from a historical state snapshot.

Run with:
  python examples/checkout_demo.py
"""

import argparse
import sys
from pathlib import Path

# Add project root to path for direct script execution
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from faultline import FaultlineClient, FaultlineError, FaultlineInvariantError


def main() -> int:
    parser = argparse.ArgumentParser(description="Faultline integration resilience demo")
    parser.add_argument("--embedded", action="store_true", help="run with an embedded in-process test server")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Faultline server URL")
    args = parser.parse_args()

    print("=" * 68)
    print("  FAULTLINE INTEGRATION RESILIENCE DEMO — CHECKOUT & PAYMENT RECOVERY")
    print("=" * 68)

    client = FaultlineClient(base_url=args.base_url)

    # 1. Health check (fallback to embedded if requested or server unavailable)
    try:
        if args.embedded:
            raise FaultlineError("Forced embedded mode requested")
        health = client.health()
        print(f"[OK] Connected to Faultline Control Plane: {health.get('service')} (status: {health.get('status')})")
    except FaultlineError:
        print("[+] Starting isolated in-process demo server...")
        import tempfile
        import threading
        from http.server import ThreadingHTTPServer
        from faultline import store, adapters
        from faultline.adapters import start_local_services
        from faultline.server import ControlPlaneHandler

        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        store.DATA_DIR = Path(temp_dir.name)
        store.DB_PATH = store.DATA_DIR / "demo.sqlite3"
        store.initialize()
        start_local_services(checkout_port=0, payment_port=0)
        api_srv = ThreadingHTTPServer(("127.0.0.1", 0), ControlPlaneHandler)
        port = api_srv.server_address[1]
        threading.Thread(target=api_srv.serve_forever, daemon=True).start()
        client = FaultlineClient(base_url=f"http://127.0.0.1:{port}")
        health = client.health()
        print(f"[OK] Embedded server ready on port {port} (status: {health.get('status')})")

    scenario_id = "payment-response-lost"

    # 2. Run vulnerable implementation
    print(f"\n[1/4] Running scenario '{scenario_id}' with VULNERABLE implementation...")
    try:
        vuln_run = client.run_scenario(scenario_id, implementation="vulnerable", wait=True, assert_pass=True)
        print("    [!] Unexpected: Vulnerable run passed all invariants.")
    except FaultlineInvariantError as exc:
        vuln_run = exc.run
        print(f"    [OK] Defect Successfully Detected! (Verdict: {vuln_run.get('verdict')})")
        for assertion in exc.failed_assertions:
            print(f"        * Invariant Violation: {assertion.get('name')}")
            print(f"          Expected: {assertion.get('expected')}")
            print(f"          Observed: {assertion.get('observed')}")
            print(f"          Evidence: {assertion.get('evidence')}")

    # 3. Run fixed implementation
    print(f"\n[2/4] Running scenario '{scenario_id}' with FIXED implementation...")
    try:
        fixed_run = client.run_scenario(scenario_id, implementation="fixed", wait=True, assert_pass=True)
        print(f"    [OK] Resilience Verified! (Verdict: {fixed_run.get('verdict')})")
        for assertion in fixed_run.get("assertions", []):
            print(f"        * Invariant '{assertion.get('name')}': {assertion.get('status').upper()} (Observed: {assertion.get('observed')})")
    except FaultlineInvariantError as exc:
        print(f"    [!] Error: Fixed run failed assertions: {exc}")
        return 1

    # 4. OpenTelemetry Tracing & W3C Trace Context
    print(f"\n[3/4] Inspecting OpenTelemetry Distributed Tracing...")
    fixed_run_id = fixed_run.get("id")
    telemetry = client.get_telemetry(fixed_run_id)
    trace_id = telemetry.get("traceId") or telemetry.get("trace_id")
    spans = telemetry.get("spans", [])
    print(f"    [OK] Trace ID: {trace_id} ({len(spans)} spans recorded)")
    for span in spans:
        duration_ms = (span.get("end_time_unix_nano", 0) - span.get("start_time_unix_nano", 0)) / 1_000_000
        print(f"        * Span: {span.get('name'):<26} [{span.get('kind'):<6}] Duration: {duration_ms:.2f}ms (Status: {span.get('statusCode') or span.get('status_code')})")

    with client.trace_context(fixed_run_id) as headers:
        print(f"    [OK] Injected W3C Header: traceparent={headers.get('traceparent')}")

    # 5. Checkpoint Branching from Historical State
    print(f"\n[4/4] Exploratory Checkpoint Branching...")
    snapshots = client.get_snapshots(fixed_run_id)
    if snapshots:
        first_snap = snapshots[0]
        snap_seq = first_snap["sequence"]
        print(f"    [+] Branching from snapshot sequence #{snap_seq}...")
        branched = client.branch_run(fixed_run_id, snapshot_sequence=snap_seq, overrides={"notes": "Exploratory branch from demo script"})
        print(f"    [OK] Created Branched Run: {branched.get('id')}")
        print(f"        Parent Run ID:            {branched.get('parent_run_id')}")
        print(f"        Forked Snapshot Sequence: {branched.get('forked_from_snapshot_id')}")

    print("\n" + "=" * 68)
    print("  DEMO COMPLETE: All failure modes, invariant checks, and tracing verified.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
