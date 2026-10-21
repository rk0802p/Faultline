"""Background scenario execution for the local checkout environment."""

import json
import logging
import threading
import time
from http.client import HTTPConnection
from typing import Any
from uuid import uuid4

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
    trace_id: str | None = None,
    root_span_id: str | None = None,
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
        headers = {"Content-Type": "application/json"}
        if trace_id and root_span_id:
            headers["traceparent"] = f"00-{trace_id}-{root_span_id}-01"
        t_req_start = time.time_ns()
        connection.request("POST", "/v1/checkout", body, headers)
        response = connection.getresponse()
        checkout_result = json.loads(response.read())
        t_req_end = time.time_ns()
        if trace_id and root_span_id:
            store.record_span(
                run_id=run_id,
                trace_id=trace_id,
                span_id=uuid4().hex[:16],
                parent_span_id=root_span_id,
                name="http.client.checkout",
                kind="CLIENT",
                start_time_unix_nano=t_req_start,
                end_time_unix_nano=t_req_end,
                status_code="OK" if response.status == 200 else "ERROR",
                attributes={"http.method": "POST", "http.status_code": response.status, "http.url": "/v1/checkout"},
            )
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
    trace_id: str | None = None,
    root_span_id: str | None = None,
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
            headers = {"Content-Type": "application/json"}
            if trace_id and root_span_id:
                headers["traceparent"] = f"00-{trace_id}-{root_span_id}-01"
            t_req_start = time.time_ns()
            connection.request("POST", "/v1/webhooks/payment", body, headers)
            response = connection.getresponse()
            result = json.loads(response.read())
            t_req_end = time.time_ns()
            if trace_id and root_span_id:
                store.record_span(
                    run_id=run_id,
                    trace_id=trace_id,
                    span_id=uuid4().hex[:16],
                    parent_span_id=root_span_id,
                    name=f"http.client.webhook.delivery_{i}",
                    kind="CLIENT",
                    start_time_unix_nano=t_req_start,
                    end_time_unix_nano=t_req_end,
                    status_code="OK" if response.status == 200 else "ERROR",
                    attributes={"http.method": "POST", "http.status_code": response.status, "delivery": i},
                )
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
    trace_id: str | None = None,
    root_span_id: str | None = None,
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
        headers = {"Content-Type": "application/json"}
        if trace_id and root_span_id:
            headers["traceparent"] = f"00-{trace_id}-{root_span_id}-01"
        t_req_start = time.time_ns()
        connection.request("POST", "/v1/checkout", body, headers)
        response = connection.getresponse()
        checkout_result = json.loads(response.read())
        t_req_end = time.time_ns()
        if trace_id and root_span_id:
            store.record_span(
                run_id=run_id,
                trace_id=trace_id,
                span_id=uuid4().hex[:16],
                parent_span_id=root_span_id,
                name="http.client.checkout",
                kind="CLIENT",
                start_time_unix_nano=t_req_start,
                end_time_unix_nano=t_req_end,
                status_code="OK" if response.status == 200 else "ERROR",
                attributes={"http.method": "POST", "http.status_code": response.status, "http.url": "/v1/checkout"},
            )
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
    run_record = store.get_run(run_id) or {}
    trace_id = run_record.get("trace_id") or uuid4().hex
    root_span_id = uuid4().hex[:16]
    root_start_ns = time.time_ns()

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
    store.record_snapshot(run_id, 1, scenario.get("scenario_version", 1), {
        "verdict": "pending",
        "status": "running",
        "charges": [],
        "fulfillments": [],
        "refunds": [],
        "assertions": [],
    })
    timeout_seconds = scenario.get("timeout_seconds", 15)
    started = time.monotonic()
    try:
        fault = scenario.get("fault")
        if fault == "duplicate_payment_webhook":
            assertions, summary = _execute_duplicate_webhook(
                run_id, scenario, implementation, signal, timeout_seconds, started, trace_id, root_span_id
            )
            clean_targets = ["checkout-webhook"]
        elif fault == "persistence_failure_after_payment":
            assertions, summary = _execute_persistence_failure(
                run_id, scenario, implementation, signal, timeout_seconds, started, trace_id, root_span_id
            )
            clean_targets = ["checkout", "payment-provider", "local-db"]
        else:
            assertions, summary = _execute_payment_response_lost(
                run_id, scenario, implementation, signal, timeout_seconds, started, trace_id, root_span_id
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
        evs = store.get_run(run_id)["events"]
        final_seq = max([e["sequence"] for e in evs], default=1)
        store.record_snapshot(run_id, final_seq, scenario.get("scenario_version", 1), {
            "verdict": verdict,
            "status": "completed",
            "charges": provider.ledger(run_id),
            "fulfillments": checkout_service.ledger(run_id),
            "refunds": provider.refund_ledger(run_id),
            "assertions": assertions,
        })

        fault_name = fault or "payment_response_lost_after_commit"
        store.record_span(
            run_id=run_id,
            trace_id=trace_id,
            span_id=uuid4().hex[:16],
            parent_span_id=root_span_id,
            name=f"fault.interceptor:{fault_name}",
            kind="INTERNAL",
            start_time_unix_nano=root_start_ns + 500_000,
            end_time_unix_nano=root_start_ns + 2_500_000,
            status_code="OK",
            attributes={"fault.type": fault_name, "fault.active": True},
        )
        store.record_span(
            run_id=run_id,
            trace_id=trace_id,
            span_id=uuid4().hex[:16],
            parent_span_id=root_span_id,
            name="evaluator.invariants",
            kind="INTERNAL",
            start_time_unix_nano=time.time_ns() - 1_000_000,
            end_time_unix_nano=time.time_ns(),
            status_code="OK" if verdict == "passed" else "ERROR",
            status_message=None if verdict == "passed" else "One or more invariants failed",
            attributes={"evaluator.assertions_count": len(assertions), "evaluator.verdict": verdict},
        )
        cleanup_res = cleanup_all(run_id, clean_targets)
        store.record_span(
            run_id=run_id,
            trace_id=trace_id,
            span_id=uuid4().hex[:16],
            parent_span_id=root_span_id,
            name="environment.cleanup",
            kind="INTERNAL",
            start_time_unix_nano=time.time_ns() - int(cleanup_res.get("duration_ms", 1) * 1_000_000),
            end_time_unix_nano=time.time_ns(),
            status_code="OK" if cleanup_res.get("status") == "succeeded" else "ERROR",
            attributes={"cleanup.status": cleanup_res.get("status", "unknown"), "cleanup.targets": clean_targets},
        )
        store.record_span(
            run_id=run_id,
            trace_id=trace_id,
            span_id=root_span_id,
            parent_span_id=None,
            name=f"faultline.scenario:{scenario['name']}",
            kind="SERVER",
            start_time_unix_nano=root_start_ns,
            end_time_unix_nano=time.time_ns(),
            status_code="OK" if verdict == "passed" else "ERROR",
            attributes={
                "scenario.name": scenario["name"],
                "scenario.version": scenario.get("scenario_version", 1),
                "implementation": implementation,
                "verdict": verdict,
            },
        )

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
        store.record_span(
            run_id=run_id,
            trace_id=trace_id,
            span_id=root_span_id,
            parent_span_id=None,
            name=f"faultline.scenario:{scenario['name']}",
            kind="SERVER",
            start_time_unix_nano=root_start_ns,
            end_time_unix_nano=time.time_ns(),
            status_code="ERROR",
            status_message="run cancelled",
            attributes={"cancellation": True},
        )
    except (RunTimedOut, TimeoutError, OSError) as exc:
        if signal.is_set() or store.run_status(run_id) == "cancel_requested":
            cleanup_all(run_id)
            store.record_event(run_id, "RUN_CANCELLED", {"phase": "execution"})
            store.transition_run(run_id, "cancelled", verdict="cancelled", finished=True)
            return
        cleanup_all(run_id)
        store.record_event(run_id, "RUN_TIMED_OUT", {"timeout_seconds": timeout_seconds})
        store.transition_run(run_id, "timed_out", verdict="error", error=str(exc) or "run exceeded its time limit", finished=True)
        store.record_span(
            run_id=run_id,
            trace_id=trace_id,
            span_id=root_span_id,
            parent_span_id=None,
            name=f"faultline.scenario:{scenario['name']}",
            kind="SERVER",
            start_time_unix_nano=root_start_ns,
            end_time_unix_nano=time.time_ns(),
            status_code="ERROR",
            status_message=str(exc) or "timed out",
            attributes={"error": "timeout"},
        )
    except Exception as exc:
        logger.exception("run execution failed run_id=%s", run_id)
        cleanup_all(run_id)
        store.record_event(run_id, "RUN_EXECUTION_FAILED", {"error": str(exc)[:500]})
        store.transition_run(run_id, "failed", verdict="error", error=str(exc)[:1000], finished=True)
        store.record_span(
            run_id=run_id,
            trace_id=trace_id,
            span_id=root_span_id,
            parent_span_id=None,
            name=f"faultline.scenario:{scenario['name']}",
            kind="SERVER",
            start_time_unix_nano=root_start_ns,
            end_time_unix_nano=time.time_ns(),
            status_code="ERROR",
            status_message=str(exc)[:200],
            attributes={"error": str(exc)[:200]},
        )
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


def branch_run(
    parent_run_id: str,
    snapshot_sequence: int | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parent = store.get_run(parent_run_id)
    if not parent:
        raise ValueError(f"parent run {parent_run_id} not found")

    overrides = overrides or {}
    new_impl = overrides.get("implementation", parent["implementation"])
    scenario = dict(parent["scenario"])
    for k, v in overrides.get("scenario_overrides", {}).items():
        scenario[k] = v

    snaps = store.get_snapshots(parent_run_id)
    if snaps:
        if snapshot_sequence is not None:
            target_snap = next((s for s in snaps if s["sequence"] == snapshot_sequence), None) or snaps[-1]
            snapshot_sequence = target_snap["sequence"]
        else:
            target_snap = snaps[-1]
            snapshot_sequence = target_snap["sequence"]
    else:
        target_snap = None
        snapshot_sequence = snapshot_sequence or 1

    branch_run_id = str(uuid4())
    store.create_run(
        branch_run_id,
        scenario_id=parent["scenario_id"],
        scenario_version=parent["scenario_version"],
        implementation=new_impl,
        parent_run_id=parent_run_id,
        forked_from_snapshot_id=snapshot_sequence,
    )
    store.record_event(branch_run_id, "RUN_BRANCHED_FROM_CHECKPOINT", {
        "parent_run_id": parent_run_id,
        "forked_sequence": snapshot_sequence,
        "checkpoint_state": target_snap["snapshot"] if target_snap else {},
        "overrides": overrides,
    })
    store.record_audit_event(
        "faultline-demo",
        "developer",
        "RUN_BRANCHED",
        "run",
        branch_run_id,
        {"parent_run_id": parent_run_id, "sequence": snapshot_sequence, "implementation": new_impl},
    )
    submit(branch_run_id, scenario, new_impl)
    return store.get_run(branch_run_id)


def execute_suite_run(suite_id: str, implementation: str = "fixed", synchronous: bool = False) -> dict[str, Any]:
    suite = store.get_suite(suite_id)
    if not suite:
        raise ValueError(f"suite {suite_id} not found")
    scenarios = suite.get("scenarios", [])
    suite_run_id = store.record_suite_run_start(suite_id, implementation, len(scenarios))
    store.record_audit_event("faultline-demo", "developer", "SUITE_RUN_SUBMITTED", "suite", suite_id, {
        "suite_run_id": suite_run_id,
        "implementation": implementation,
        "scenario_count": len(scenarios),
    })

    def _worker():
        t0 = time.monotonic()
        run_ids = []
        passed_count = 0
        failed_count = 0
        for sc_name in scenarios:
            sc = store.get_scenario(sc_name)
            if not sc:
                continue
            run_id = str(uuid4())
            run_ids.append(run_id)
            store.create_run(run_id, sc["id"], sc.get("version", 1), implementation)
            execute_run(run_id, sc["definition"], implementation)
            run_data = store.get_run(run_id)
            if run_data and run_data.get("verdict") == "passed":
                passed_count += 1
            else:
                failed_count += 1
        duration_ms = max(1, int((time.monotonic() - t0) * 1000))
        verdict = "passed" if (failed_count == 0 and passed_count > 0) else "failed"
        store.finish_suite_run(suite_run_id, verdict, passed_count, failed_count, duration_ms, run_ids)
        store.record_audit_event("faultline-demo", "developer", "SUITE_RUN_COMPLETED", "suite", suite_id, {
            "suite_run_id": suite_run_id,
            "verdict": verdict,
            "passed": passed_count,
            "failed": failed_count,
            "duration_ms": duration_ms,
        })

    if synchronous:
        _worker()
        results = store.list_suite_results(suite_id, limit=1)
        return results[0] if results else {"id": suite_run_id, "suite_id": suite_id, "status": "completed"}
    else:
        threading.Thread(target=_worker, daemon=True, name=f"faultline-suite-{suite_run_id[:8]}").start()
        return {"id": suite_run_id, "suite_id": suite_id, "status": "running", "total_scenarios": len(scenarios)}
