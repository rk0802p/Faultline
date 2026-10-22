"""Checkout and payment reference integration pack.

Provides reference failure injection scenarios for distributed e-commerce transactions:
- Payment response loss after commit
- Duplicate payment webhook delivery
- Local database persistence failure after payment commit
"""

import json
import logging
import threading
import time
from http.client import HTTPConnection
from typing import Any
from uuid import uuid4

from .. import adapters, security, store
from ..adapters import checkout_service, provider


logger = logging.getLogger("faultline.integrations.payment")

PAYMENT_FAULTS: set[str] = {
    "payment_response_lost_after_commit",
    "duplicate_payment_webhook",
    "persistence_failure_after_payment",
}

PAYMENT_WORKFLOW_STEPS: set[str] = {
    "initiate_checkout",
    "process_payment",
    "order_persistence",
    "webhook_delivery",
    "reconciliation_refund",
}


def cleanup_payment(run_id: str) -> list[str]:
    """Cleans up checkout and payment provider test state for a run."""
    owned_resources = [
        f"provider:ledger:{run_id}",
        f"provider:idempotency:{run_id}",
        f"provider:refunds:{run_id}",
        f"checkout:orders:{run_id}",
        f"checkout:fulfillments:{run_id}",
    ]
    provider.cleanup(run_id)
    checkout_service.cleanup(run_id)
    return owned_resources


def validate_payment_scenario(definition: dict[str, Any]) -> None:
    """Validates payment-specific parameters for checkout integration scenarios."""
    fault = definition.get("fault")
    if fault not in PAYMENT_FAULTS:
        return

    if not isinstance(definition.get("order_id"), str) or not definition["order_id"].strip():
        raise ValueError("order_id is required")

    if type(definition.get("amount_cents")) is not int or definition["amount_cents"] < 1:
        raise ValueError("amount_cents must be a positive integer")

    if fault in {"payment_response_lost_after_commit", "persistence_failure_after_payment"}:
        retries = definition.get("retries")
        if type(retries) is not int or not 0 <= retries <= 3:
            raise ValueError("retries must be an integer between 0 and 3")
    elif fault == "duplicate_payment_webhook":
        deliveries = definition.get("webhook_deliveries", 2)
        if type(deliveries) is not int or not 2 <= deliveries <= 5:
            raise ValueError("webhook_deliveries must be an integer between 2 and 5")
        delay = definition.get("delay_ms", 100)
        if type(delay) is not int or not 0 <= delay <= 5000:
            raise ValueError("delay_ms must be an integer between 0 and 5000")


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
    from .. import engine
    from ..engine import RunCancelled, RunTimedOut, _active_connections, _active_lock

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
    security.assert_loopback_safe(adapters.CHECKOUT_HOST, adapters.CHECKOUT_PORT)
    connection = engine.HTTPConnection(adapters.CHECKOUT_HOST, adapters.CHECKOUT_PORT, timeout=timeout_seconds + 2)
    with _active_lock:
        _active_connections[run_id] = connection
    try:
        if signal.is_set():
            raise RunCancelled()
        headers = {"Content-Type": "application/json"}
        if trace_id and root_span_id:
            headers["traceparent"] = f"00-{trace_id}-{root_span_id}-01"
        t_req_start = engine.time.time_ns()
        connection.request("POST", "/v1/checkout", body, headers)
        response = connection.getresponse()
        checkout_result = json.loads(response.read())
        t_req_end = engine.time.time_ns()
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
    if engine.time.monotonic() - started > timeout_seconds:
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
    from .. import engine
    from ..engine import RunCancelled, RunTimedOut, _active_connections, _active_lock

    order_id = scenario["order_id"]
    event_id = scenario.get("event_id", f"evt_{order_id}")
    deliveries = scenario.get("webhook_deliveries", 2)
    delay_seconds = scenario.get("delay_ms", 100) / 1000.0
    amount_cents = scenario["amount_cents"]
    delivery_responses: list[dict[str, Any]] = []

    for i in range(1, deliveries + 1):
        if signal.is_set() or store.run_status(run_id) == "cancel_requested":
            raise RunCancelled()
        if engine.time.monotonic() - started > timeout_seconds:
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

        security.assert_loopback_safe(adapters.CHECKOUT_HOST, adapters.CHECKOUT_PORT)
        connection = engine.HTTPConnection(adapters.CHECKOUT_HOST, adapters.CHECKOUT_PORT, timeout=timeout_seconds + 2)
        with _active_lock:
            _active_connections[run_id] = connection
        try:
            if signal.is_set():
                raise RunCancelled()
            headers = {"Content-Type": "application/json"}
            if trace_id and root_span_id:
                headers["traceparent"] = f"00-{trace_id}-{root_span_id}-01"
            t_req_start = engine.time.time_ns()
            connection.request("POST", "/v1/webhooks/payment", body, headers)
            response = connection.getresponse()
            result = json.loads(response.read())
            t_req_end = engine.time.time_ns()
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
    from .. import engine
    from ..engine import RunCancelled, RunTimedOut, _active_connections, _active_lock

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
    security.assert_loopback_safe(adapters.CHECKOUT_HOST, adapters.CHECKOUT_PORT)
    connection = engine.HTTPConnection(adapters.CHECKOUT_HOST, adapters.CHECKOUT_PORT, timeout=timeout_seconds + 2)
    with _active_lock:
        _active_connections[run_id] = connection
    try:
        if signal.is_set():
            raise RunCancelled()
        headers = {"Content-Type": "application/json"}
        if trace_id and root_span_id:
            headers["traceparent"] = f"00-{trace_id}-{root_span_id}-01"
        t_req_start = engine.time.time_ns()
        connection.request("POST", "/v1/checkout", body, headers)
        response = connection.getresponse()
        checkout_result = json.loads(response.read())
        t_req_end = engine.time.time_ns()
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
    if engine.time.monotonic() - started > timeout_seconds:
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


def register_payment_integration(
    fault_registry: Any,
    step_registry: Any,
    assertion_registry: Any,
    schema_registry: Any,
    cleanup_registry: Any,
) -> None:
    """Registers checkout and payment failure scenarios, schemas, invariants, and cleanup hooks."""
    # 1. Register fault runners
    fault_registry.register(
        "payment_response_lost_after_commit",
        _execute_payment_response_lost,
        ["checkout", "payment-provider"],
    )
    fault_registry.register(
        "duplicate_payment_webhook",
        _execute_duplicate_webhook,
        ["checkout-webhook"],
    )
    fault_registry.register(
        "persistence_failure_after_payment",
        _execute_persistence_failure,
        ["checkout", "payment-provider", "local-db"],
    )

    # 2. Register schemas and validators
    schema_registry.register(
        fault_name="payment_response_lost_after_commit",
        primary_assertion="no_duplicate_charge",
        description="Provider commits charge, but response is dropped before checkout receives it.",
        category="payment",
        defaults={"retries": 1},
        parameter_schema={
            "order_id": {"type": "string", "label": "Order ID", "placeholder": "order-001"},
            "amount_cents": {"type": "integer", "label": "Charge Amount (cents)", "min": 1},
            "retries": {"type": "integer", "label": "Retry Attempts", "min": 0, "max": 3},
            "timeout_seconds": {"type": "integer", "label": "Run Timeout (seconds)", "min": 2, "max": 60},
        },
        validator=validate_payment_scenario,
        legacy_steps=PAYMENT_WORKFLOW_STEPS,
    )
    schema_registry.register(
        fault_name="duplicate_payment_webhook",
        primary_assertion="idempotent_webhook_processing",
        description="Provider sends multiple identical payment.succeeded webhooks for the same order.",
        category="payment",
        defaults={"webhook_deliveries": 2, "delay_ms": 100},
        parameter_schema={
            "order_id": {"type": "string", "label": "Order ID", "placeholder": "order-001"},
            "amount_cents": {"type": "integer", "label": "Charge Amount (cents)", "min": 1},
            "webhook_deliveries": {"type": "integer", "label": "Webhook Deliveries", "min": 2, "max": 5},
            "delay_ms": {"type": "integer", "label": "Delay Between Webhooks (ms)", "min": 0, "max": 5000},
            "timeout_seconds": {"type": "integer", "label": "Run Timeout (seconds)", "min": 2, "max": 60},
        },
        validator=validate_payment_scenario,
        legacy_steps=PAYMENT_WORKFLOW_STEPS,
    )
    schema_registry.register(
        fault_name="persistence_failure_after_payment",
        primary_assertion="charge_compensated_on_failure",
        description="Provider commits charge, but checkout fails to persist the order locally.",
        category="payment",
        defaults={"retries": 0},
        parameter_schema={
            "order_id": {"type": "string", "label": "Order ID", "placeholder": "order-001"},
            "amount_cents": {"type": "integer", "label": "Charge Amount (cents)", "min": 1},
            "retries": {"type": "integer", "label": "Retry Attempts", "min": 0, "max": 3},
            "timeout_seconds": {"type": "integer", "label": "Run Timeout (seconds)", "min": 2, "max": 60},
        },
        validator=validate_payment_scenario,
        legacy_steps=PAYMENT_WORKFLOW_STEPS,
    )

    # 3. Register cleanup hooks
    cleanup_registry.register("checkout", cleanup_payment)
    cleanup_registry.register("payment-provider", cleanup_payment)
    cleanup_registry.register("checkout-webhook", cleanup_payment)
    cleanup_registry.register("local-db", cleanup_payment)


register_integration = register_payment_integration
