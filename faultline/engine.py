"""Background scenario execution for the local checkout environment."""

import json
import logging
import threading
import time
from http.client import HTTPConnection
from typing import Any

from . import adapters, security, store
from .adapters import provider, checkout_service


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


def cleanup_all(run_id: str, clean_targets: list[str] | None = None) -> dict[str, Any]:
    try:
        t0 = time.monotonic()
    except (StopIteration, Exception):
        t0 = 0.0
    owned_resources = [
        f"provider:ledger:{run_id}",
        f"provider:idempotency:{run_id}",
        f"provider:refunds:{run_id}",
        f"checkout:orders:{run_id}",
        f"checkout:fulfillments:{run_id}",
    ]
    if clean_targets:
        owned_resources.extend([f"target:{t}:{run_id}" for t in clean_targets])
    try:
        provider.cleanup(run_id)
        checkout_service.cleanup(run_id)
        try:
            duration_ms = max(1, int((time.monotonic() - t0) * 1000))
        except (StopIteration, Exception):
            duration_ms = 1
        store.record_cleanup(run_id, "succeeded", owned_resources=owned_resources, duration_ms=duration_ms)
        store.record_event(run_id, "TEST_ENVIRONMENT_CLEANED", {
            "status": "succeeded",
            "targets": clean_targets or ["checkout", "payment-provider"],
            "owned_resources": owned_resources,
            "duration_ms": duration_ms,
        })
        return {"status": "succeeded", "owned_resources": owned_resources, "duration_ms": duration_ms}
    except Exception as exc:
        try:
            duration_ms = max(1, int((time.monotonic() - t0) * 1000))
        except (StopIteration, Exception):
            duration_ms = 1
        logger.exception("cleanup failed run_id=%s", run_id)
        store.record_cleanup(run_id, "failed", owned_resources=owned_resources, error=str(exc), duration_ms=duration_ms)
        store.record_event(run_id, "TEST_ENVIRONMENT_CLEANUP_FAILED", {
            "status": "failed",
            "owned_resources": owned_resources,
            "error": str(exc),
            "duration_ms": duration_ms,
        })
        return {"status": "failed", "owned_resources": owned_resources, "error": str(exc), "duration_ms": duration_ms}


def _execute_payment_response_lost(
    run_id: str,
    scenario: dict[str, Any],
    implementation: str,
    signal: threading.Event,
    timeout_seconds: int,
    started: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
    connection = HTTPConnection("127.0.0.1", adapters.CHECKOUT_PORT, timeout=timeout_seconds + 2)
    with _active_lock:
        _active_connections[run_id] = connection
    try:
        if signal.is_set():
            raise RunCancelled()
        connection.request("POST", "/v1/checkout", body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        checkout_result = json.loads(response.read())
        store.record_event(run_id, "CHECKOUT_HTTP_RESPONSE", {"status": response.status, "body": checkout_result})
    finally:
        connection.close()
        with _active_lock:
            _active_connections.pop(run_id, None)

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
    summary = {
        "order_id": checkout_result["order_id"],
        "checkout_status": checkout_result["status"],
        "attempts": checkout_result["attempts"],
        "charge_count": len(provider_charges),
        "charges": provider_charges,
        "failed_assertions": [item["name"] for item in assertions if item["status"] == "failed"],
    }
    return assertions, summary


def _execute_duplicate_webhook(
    run_id: str,
    scenario: dict[str, Any],
    implementation: str,
    signal: threading.Event,
    timeout_seconds: int,
    started: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    order_id = scenario["order_id"]
    event_id = scenario.get("event_id", f"evt_{order_id}")
    deliveries = scenario.get("webhook_deliveries", 2)
    delay_seconds = scenario.get("delay_ms", 100) / 1000.0
    amount_cents = scenario["amount_cents"]
    delivery_responses: list[dict[str, Any]] = []

    for i in range(1, deliveries + 1):
        if signal.is_set() or store.run_status(run_id) == "cancel_requested":
            raise RunCancelled()
        if time.monotonic() - started > timeout_seconds:
            raise RunTimedOut()

        store.record_event(run_id, "WEBHOOK_DELIVERY_STARTED", {"delivery": i, "event_id": event_id})
        if i > 1:
            store.record_event(run_id, "FAULT_DUPLICATE_WEBHOOK_DELIVERED", {"delivery": i, "event_id": event_id})

        body = json.dumps({
            "run_id": run_id,
            "event_id": event_id,
            "order_id": order_id,
            "amount_cents": amount_cents,
            "type": "payment.succeeded",
            "implementation": implementation,
        })
        store.record_event(run_id, "WEBHOOK_HTTP_REQUEST", {
            "method": "POST",
            "path": "/v1/webhooks/payment",
            "delivery": i,
            "body": json.loads(body),
        })

        connection = HTTPConnection("127.0.0.1", adapters.CHECKOUT_PORT, timeout=timeout_seconds + 2)
        with _active_lock:
            _active_connections[run_id] = connection
        try:
            if signal.is_set():
                raise RunCancelled()
            connection.request("POST", "/v1/webhooks/payment", body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            result = json.loads(response.read())
            store.record_event(run_id, "WEBHOOK_HTTP_RESPONSE", {
                "status": response.status,
                "delivery": i,
                "body": result,
            })
            if response.status != 200:
                raise RuntimeError(result.get("error", f"webhook returned HTTP {response.status}"))
            delivery_responses.append(result)
        finally:
            connection.close()
            with _active_lock:
                _active_connections.pop(run_id, None)

        if i < deliveries and delay_seconds > 0:
            if signal.wait(delay_seconds):
                raise RunCancelled()

    if signal.is_set() or store.run_status(run_id) == "cancel_requested":
        raise RunCancelled()

    fulfillments = checkout_service.ledger(run_id)
    events = store.get_run(run_id)["events"]
    event_types = [event["type"] for event in events]

    assertions = [
        {
            "name": "fault_activation_confirmed",
            "status": "passed" if "FAULT_DUPLICATE_WEBHOOK_DELIVERED" in event_types else "failed",
            "expected": "duplicate payment webhook delivered",
            "observed": "confirmed" if "FAULT_DUPLICATE_WEBHOOK_DELIVERED" in event_types else "not observed",
            "evidence": ["FAULT_DUPLICATE_WEBHOOK_DELIVERED"],
        },
        {
            "name": "idempotent_webhook_processing",
            "status": "passed" if len(fulfillments) == 1 else "failed",
            "expected": "at most one fulfillment per payment event",
            "observed": f"{len(fulfillments)} fulfillment(s)",
            "evidence": [f"fulfillment:{item['id']}" for item in fulfillments],
        },
        {
            "name": "webhook_deliveries_acknowledged",
            "status": "passed" if len(delivery_responses) == deliveries else "failed",
            "expected": f"all {deliveries} webhook deliveries acknowledged",
            "observed": f"{len(delivery_responses)} acknowledged",
            "evidence": ["WEBHOOK_HTTP_RESPONSE"],
        },
        {
            "name": "order_terminal_state",
            "status": "passed" if len(fulfillments) >= 1 else "failed",
            "expected": "order reaches fulfilled terminal state",
            "observed": "fulfilled" if len(fulfillments) >= 1 else "unfulfilled",
            "evidence": ["ORDER_FULFILLMENT_COMMITTED"],
        },
    ]

    summary = {
        "order_id": order_id,
        "event_id": event_id,
        "deliveries": deliveries,
        "fulfillment_count": len(fulfillments),
        "fulfillments": fulfillments,
        "charge_count": 0,
        "charges": [],
        "failed_assertions": [item["name"] for item in assertions if item["status"] == "failed"],
    }
    return assertions, summary


def _execute_persistence_failure(
    run_id: str,
    scenario: dict[str, Any],
    implementation: str,
    signal: threading.Event,
    timeout_seconds: int,
    started: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    body = json.dumps({
        "run_id": run_id,
        "order_id": scenario["order_id"],
        "amount_cents": scenario["amount_cents"],
        "fault": scenario["fault"],
        "retries": scenario.get("retries", 0),
        "implementation": implementation,
        "timeout_seconds": timeout_seconds,
    })
    store.record_event(run_id, "CHECKOUT_HTTP_REQUEST", {"method": "POST", "path": "/v1/checkout", "body": json.loads(body)})
    if signal.is_set():
        raise RunCancelled()
    connection = HTTPConnection("127.0.0.1", adapters.CHECKOUT_PORT, timeout=timeout_seconds + 2)
    with _active_lock:
        _active_connections[run_id] = connection
    try:
        if signal.is_set():
            raise RunCancelled()
        connection.request("POST", "/v1/checkout", body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        checkout_result = json.loads(response.read())
        store.record_event(run_id, "CHECKOUT_HTTP_RESPONSE", {"status": response.status, "body": checkout_result})
    finally:
        connection.close()
        with _active_lock:
            _active_connections.pop(run_id, None)

    if signal.is_set() or store.run_status(run_id) == "cancel_requested":
        raise RunCancelled()
    if time.monotonic() - started > timeout_seconds:
        raise RunTimedOut()
    if response.status == 408:
        raise RunTimedOut(checkout_result.get("error", "scenario execution timed out"))
    if response.status != 200:
        raise RuntimeError(checkout_result.get("error", f"checkout returned HTTP {response.status}"))

    provider_charges = provider.ledger(run_id)
    provider_refunds = provider.refund_ledger(run_id)
    events = store.get_run(run_id)["events"]
    event_types = [event["type"] for event in events]

    has_charge = len(provider_charges) > 0
    has_refund = len(provider_refunds) > 0 and any(r["charge_id"] == c["id"] for r in provider_refunds for c in provider_charges)
    compensated = checkout_result.get("compensated") is True or has_refund

    assertions = [
        {
            "name": "fault_activation_confirmed",
            "status": "passed" if "FAULT_PERSISTENCE_FAILED" in event_types else "failed",
            "expected": "checkout order persistence failed after charge",
            "observed": "confirmed" if "FAULT_PERSISTENCE_FAILED" in event_types else "not observed",
            "evidence": ["FAULT_PERSISTENCE_FAILED"],
        },
        {
            "name": "charge_compensated_on_failure",
            "status": "passed" if (has_charge and compensated) else "failed",
            "expected": "committed charge is refunded on persistence failure",
            "observed": f"{len(provider_refunds)} refund(s) for {len(provider_charges)} charge(s)",
            "evidence": [f"refund:{item['id']}" for item in provider_refunds] if provider_refunds else ["NO_COMPENSATION_ISSUED"],
        },
        {
            "name": "no_orphaned_charge",
            "status": "passed" if len(provider_charges) == len(provider_refunds) else "failed",
            "expected": "no uncompensated charges remaining",
            "observed": f"{len(provider_charges) - len(provider_refunds)} uncompensated charge(s)",
            "evidence": [f"charge:{item['id']}" for item in provider_charges],
        },
        {
            "name": "checkout_terminal_state",
            "status": "passed" if checkout_result.get("status") in {"cancelled_and_refunded", "reconciled"} else "failed",
            "expected": "checkout reaches safe reconciled terminal state",
            "observed": checkout_result.get("status", "unknown"),
            "evidence": ["CHECKOUT_REACHED_TERMINAL_STATE"],
        },
    ]

    summary = {
        "order_id": checkout_result["order_id"],
        "checkout_status": checkout_result["status"],
        "attempts": checkout_result["attempts"],
        "charge_count": len(provider_charges),
        "charges": provider_charges,
        "refund_count": len(provider_refunds),
        "refunds": provider_refunds,
        "compensated": checkout_result.get("compensated", False),
        "failed_assertions": [item["name"] for item in assertions if item["status"] == "failed"],
    }
    return assertions, summary


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
    try:
        fault = scenario.get("fault")
        if fault == "duplicate_payment_webhook":
            assertions, summary = _execute_duplicate_webhook(
                run_id, scenario, implementation, signal, timeout_seconds, started
            )
            clean_targets = ["checkout-webhook"]
        elif fault == "persistence_failure_after_payment":
            assertions, summary = _execute_persistence_failure(
                run_id, scenario, implementation, signal, timeout_seconds, started
            )
            clean_targets = ["checkout", "payment-provider", "local-db"]
        else:
            assertions, summary = _execute_payment_response_lost(
                run_id, scenario, implementation, signal, timeout_seconds, started
            )
            clean_targets = ["checkout", "payment-provider"]

        store.record_event(run_id, "ASSERTIONS_EVALUATED", {"count": len(assertions)})
        store.replace_assertions(run_id, assertions)
        verdict = "passed" if all(item["status"] == "passed" for item in assertions) else "failed"
        logger.debug(
            "assertions evaluated run_id=%s verdict=%s failed=%s",
            run_id, verdict,
            [item["name"] for item in assertions if item["status"] == "failed"],
        )
        cleanup_all(run_id, clean_targets)
        if store.transition_run(run_id, "completed", verdict=verdict, summary=summary, finished=True):
            store.record_event(run_id, "RUN_COMPLETED", {"verdict": verdict})
        else:
            if store.run_status(run_id) == "cancel_requested":
                store.record_event(run_id, "RUN_CANCELLED", {"phase": "completion_race"})
                store.transition_run(run_id, "cancelled", verdict="cancelled", summary=summary, finished=True)
        logger.info("run completed run_id=%s verdict=%s", run_id, verdict)
    except RunCancelled:
        cleanup_all(run_id)
        store.record_event(run_id, "RUN_CANCELLED", {"phase": "execution"})
        store.transition_run(run_id, "cancelled", verdict="cancelled", finished=True)
    except (RunTimedOut, TimeoutError, OSError) as exc:
        if signal.is_set() or store.run_status(run_id) == "cancel_requested":
            cleanup_all(run_id)
            store.record_event(run_id, "RUN_CANCELLED", {"phase": "execution"})
            store.transition_run(run_id, "cancelled", verdict="cancelled", finished=True)
            return
        cleanup_all(run_id)
        store.record_event(run_id, "RUN_TIMED_OUT", {"timeout_seconds": timeout_seconds})
        store.transition_run(run_id, "timed_out", verdict="error", error=str(exc) or "run exceeded its time limit", finished=True)
    except Exception as exc:
        logger.exception("run execution failed run_id=%s", run_id)
        cleanup_all(run_id)
        store.record_event(run_id, "RUN_EXECUTION_FAILED", {"error": str(exc)[:500]})
        store.transition_run(run_id, "failed", verdict="error", error=str(exc)[:1000], finished=True)
    finally:
        with _active_lock:
            conn = _active_connections.pop(run_id, None)
            if conn:
                conn.close()
            _active_runs.pop(run_id, None)


def submit(run_id: str, scenario: dict[str, Any], implementation: str) -> None:
    threading.Thread(
        target=execute_run,
        args=(run_id, scenario, implementation),
        daemon=True,
        name=f"faultline-run-{run_id[:8]}",
    ).start()
