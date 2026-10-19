"""Background scenario execution for the local checkout environment."""

import json
import threading
from http.client import HTTPConnection
from typing import Any

from . import store
from .adapters import provider


def execute_run(run_id: str, scenario: dict[str, Any], implementation: str) -> None:
    store.update_run(run_id, status="running", verdict="pending")
    store.record_event(run_id, "RUN_STARTED", {"implementation": implementation})
    try:
        # The target is fixed to the loopback checkout service; scenario input
        # cannot redirect the worker to an arbitrary host.
        body = json.dumps({
            "run_id": run_id,
            "order_id": scenario["order_id"],
            "amount_cents": scenario["amount_cents"],
            "fault": scenario["fault"],
            "retries": scenario["retries"],
            "implementation": implementation,
        })
        connection = HTTPConnection("127.0.0.1", 8766, timeout=8)
        connection.request("POST", "/v1/checkout", body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        checkout_result = json.loads(response.read())
        connection.close()
        if response.status != 200:
            raise RuntimeError(checkout_result.get("error", f"checkout returned HTTP {response.status}"))

        provider_charges = provider.ledger(run_id)
        events = store.get_run(run_id)["events"]
        event_types = [event["type"] for event in events]
        assertions = [
            {
                "name": "fault_activation_confirmed",
                "status": "passed" if "FAULT_RESPONSE_DROPPED" in event_types else "failed",
                "expected": "provider response dropped after commit",
                "observed": "confirmed" if "FAULT_RESPONSE_DROPPED" in event_types else "not observed",
                "evidence": ["FAULT_RESPONSE_DROPPED"],
            },
            {
                "name": "no_duplicate_charge",
                "status": "passed" if len(provider_charges) <= 1 else "failed",
                "expected": "at most one successful charge per order",
                "observed": f"{len(provider_charges)} successful charge(s)",
                "evidence": [f"charge:{item['id']}" for item in provider_charges],
            },
            {
                "name": "payment_matches_ledger",
                "status": "passed" if checkout_result["status"] != "paid" or len(provider_charges) >= 1 else "failed",
                "expected": "paid order has provider charge evidence",
                "observed": f"order {checkout_result['status']}; {len(provider_charges)} charge(s)",
                "evidence": ["CHECKOUT_REACHED_TERMINAL_STATE", "PROVIDER_CHARGE_COMMITTED"],
            },
            {
                "name": "checkout_terminal_state",
                "status": "passed" if checkout_result["status"] in {"paid", "payment_unknown"} else "failed",
                "expected": "checkout reaches an allowed terminal state",
                "observed": checkout_result["status"],
                "evidence": ["CHECKOUT_REACHED_TERMINAL_STATE"],
            },
        ]
        store.record_event(run_id, "ASSERTIONS_EVALUATED", {"count": len(assertions)})
        store.replace_assertions(run_id, assertions)
        verdict = "passed" if all(item["status"] == "passed" for item in assertions) else "failed"
        summary = {
            "order_id": checkout_result["order_id"],
            "checkout_status": checkout_result["status"],
            "attempts": checkout_result["attempts"],
            "charge_count": len(provider_charges),
            "charges": provider_charges,
            "failed_assertions": [item["name"] for item in assertions if item["status"] == "failed"],
        }
        store.record_event(run_id, "TEST_ENVIRONMENT_CLEANED", {"targets": ["checkout", "payment-provider"]})
        provider.cleanup(run_id)
        store.record_event(run_id, "RUN_COMPLETED", {"verdict": verdict})
        store.update_run(run_id, status="completed", verdict=verdict, summary=summary, finished=True)
    except Exception as exc:
        store.record_event(run_id, "RUN_EXECUTION_FAILED", {"error": str(exc)[:500]})
        store.update_run(run_id, status="failed", verdict="error", error=str(exc)[:1000], finished=True)


def submit(run_id: str, scenario: dict[str, Any], implementation: str) -> None:
    threading.Thread(
        target=execute_run,
        args=(run_id, scenario, implementation),
        daemon=True,
        name=f"faultline-run-{run_id[:8]}",
    ).start()
