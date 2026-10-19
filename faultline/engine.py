"""Background scenario execution for the local checkout environment."""

import json
import logging
import threading
import time
from http.client import HTTPConnection
from typing import Any

from . import store
from .adapters import provider


logger = logging.getLogger("faultline.runner")
_active_lock = threading.Lock()
_active_runs: dict[str, threading.Event] = {}
_active_connections: dict[str, HTTPConnection] = {}


class RunCancelled(Exception):
    pass


class RunTimedOut(Exception):
    pass


def cancel(run_id: str) -> bool:
    accepted = store.request_cancel(run_id)
    if accepted:
        store.record_event(run_id, "RUN_CANCELLATION_REQUESTED", {})
        with _active_lock:
            signal = _active_runs.get(run_id)
        if signal:
            signal.set()
        with _active_lock:
            connection = _active_connections.get(run_id)
        if connection:
            connection.close()
    return accepted


def execute_run(run_id: str, scenario: dict[str, Any], implementation: str) -> None:
    logger.debug("run starting run_id=%s scenario=%s implementation=%s", run_id, scenario["name"], implementation)
    signal = threading.Event()
    with _active_lock:
        _active_runs[run_id] = signal
    if store.run_status(run_id) == "cancel_requested":
        store.transition_run(run_id, "cancelled", verdict="cancelled", finished=True)
        store.record_event(run_id, "RUN_CANCELLED", {"phase": "before_start"})
        with _active_lock:
            _active_runs.pop(run_id, None)
        return
    if not store.transition_run(run_id, "running"):
        with _active_lock:
            _active_runs.pop(run_id, None)
        return
    store.record_event(run_id, "RUN_STARTED", {"implementation": implementation})
    timeout_seconds = scenario.get("timeout_seconds", 15)
    started = time.monotonic()
    connection: HTTPConnection | None = None
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
            "timeout_seconds": timeout_seconds,
        })
        store.record_event(run_id, "CHECKOUT_HTTP_REQUEST", {"method": "POST", "path": "/v1/checkout", "body": json.loads(body)})
        if signal.is_set():
            raise RunCancelled()
        connection = HTTPConnection("127.0.0.1", 8766, timeout=timeout_seconds + 2)
        with _active_lock:
            _active_connections[run_id] = connection
        if signal.is_set():
            raise RunCancelled()
        connection.request("POST", "/v1/checkout", body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        checkout_result = json.loads(response.read())
        store.record_event(run_id, "CHECKOUT_HTTP_RESPONSE", {"status": response.status, "body": checkout_result})
        connection.close()
        if signal.is_set() or store.run_status(run_id) == "cancel_requested":
            raise RunCancelled()
        if time.monotonic() - started > timeout_seconds:
            raise RunTimedOut()
        if response.status == 408:
            raise RunTimedOut(checkout_result.get("error", "scenario execution timed out"))
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
        logger.debug(
            "assertions evaluated run_id=%s verdict=%s charges=%d failed=%s",
            run_id, verdict, len(provider_charges),
            [item["name"] for item in assertions if item["status"] == "failed"],
        )
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
        if store.transition_run(run_id, "completed", verdict=verdict, summary=summary, finished=True):
            store.record_event(run_id, "RUN_COMPLETED", {"verdict": verdict})
        else:
            if store.run_status(run_id) == "cancel_requested":
                store.record_event(run_id, "RUN_CANCELLED", {"phase": "completion_race"})
                store.transition_run(run_id, "cancelled", verdict="cancelled", summary=summary, finished=True)
        logger.info("run completed run_id=%s verdict=%s", run_id, verdict)
    except RunCancelled:
        provider.cleanup(run_id)
        store.record_event(run_id, "RUN_CANCELLED", {"phase": "execution"})
        store.transition_run(run_id, "cancelled", verdict="cancelled", finished=True)
    except (RunTimedOut, TimeoutError, OSError) as exc:
        if signal.is_set() or store.run_status(run_id) == "cancel_requested":
            provider.cleanup(run_id)
            store.record_event(run_id, "RUN_CANCELLED", {"phase": "execution"})
            store.transition_run(run_id, "cancelled", verdict="cancelled", finished=True)
            return
        provider.cleanup(run_id)
        store.record_event(run_id, "RUN_TIMED_OUT", {"timeout_seconds": timeout_seconds})
        store.transition_run(run_id, "timed_out", verdict="error", error=str(exc) or "run exceeded its time limit", finished=True)
    except Exception as exc:
        logger.exception("run execution failed run_id=%s", run_id)
        store.record_event(run_id, "RUN_EXECUTION_FAILED", {"error": str(exc)[:500]})
        store.transition_run(run_id, "failed", verdict="error", error=str(exc)[:1000], finished=True)
    finally:
        if connection:
            connection.close()
        with _active_lock:
            _active_runs.pop(run_id, None)
            _active_connections.pop(run_id, None)


def submit(run_id: str, scenario: dict[str, Any], implementation: str) -> None:
    threading.Thread(
        target=execute_run,
        args=(run_id, scenario, implementation),
        daemon=True,
        name=f"faultline-run-{run_id[:8]}",
    ).start()
