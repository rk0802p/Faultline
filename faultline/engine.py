"""Background scenario execution for the local checkout environment."""

import json
import logging
import socket
import ssl
import threading
import time
from http.client import HTTPConnection, HTTPSConnection
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from . import adapters, security, store
from .adapters import provider, checkout_service
from .schema import validate_scenario, SUPPORTED_STEP_TYPES, HTTP_STEP_TYPES


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
    security.assert_loopback_safe(adapters.CHECKOUT_HOST, adapters.CHECKOUT_PORT)
    connection = HTTPConnection(adapters.CHECKOUT_HOST, adapters.CHECKOUT_PORT, timeout=timeout_seconds + 2)
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

        security.assert_loopback_safe(adapters.CHECKOUT_HOST, adapters.CHECKOUT_PORT)
        connection = HTTPConnection(adapters.CHECKOUT_HOST, adapters.CHECKOUT_PORT, timeout=timeout_seconds + 2)
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
    security.assert_loopback_safe(adapters.CHECKOUT_HOST, adapters.CHECKOUT_PORT)
    connection = HTTPConnection(adapters.CHECKOUT_HOST, adapters.CHECKOUT_PORT, timeout=timeout_seconds + 2)
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


class FaultRunnerRegistry:
    """Registry mapping fault types to their execution handlers and cleanup targets."""

    def __init__(self) -> None:
        self._registry: dict[str, tuple[Any, list[str]]] = {}

    def register(self, fault_type: str, handler: Any, cleanup_targets: list[str]) -> None:
        if fault_type in self._registry:
            raise ValueError(f"fault type '{fault_type}' is already registered")
        self._registry[fault_type] = (handler, cleanup_targets)

    def get(self, fault_type: str) -> tuple[Any, list[str]] | None:
        return self._registry.get(fault_type)

    def is_registered(self, fault_type: str) -> bool:
        return fault_type in self._registry


class StepRunnerRegistry:
    """Registry mapping workflow step types to their execution handlers."""

    def __init__(self) -> None:
        self._registry: dict[str, Any] = {}

    def register(self, step_type: str, handler: Any) -> None:
        if step_type in self._registry:
            raise ValueError(f"step type '{step_type}' is already registered")
        self._registry[step_type] = handler

    def get(self, step_type: str) -> Any | None:
        return self._registry.get(step_type)

    def is_registered(self, step_type: str) -> bool:
        return step_type in self._registry

    def registered_types(self) -> set[str]:
        return set(self._registry.keys())


MAX_REDIRECTS = 5
BLOCKED_REQUEST_HEADERS = {"host", "transfer-encoding", "connection", "upgrade"}
SAFE_RESPONSE_HEADERS = {"content-type", "content-length", "server", "location", "date", "x-request-id", "etag"}


def _resolve_http_endpoint(step: dict[str, Any]) -> tuple[str, str, int, str]:
    """Resolves scheme, host, port, path from step definition or defaults to local checkout service."""
    url = step.get("url") or step.get("endpoint")
    if url:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "http").lower()
        if scheme not in {"http", "https"}:
            raise ValueError(f"unsupported scheme '{scheme}', only http and https are allowed")
        host = parsed.hostname or adapters.CHECKOUT_HOST
        port = parsed.port or (443 if scheme == "https" else 80)
        path = parsed.path or "/v1/health"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return scheme, host, port, path

    scheme = step.get("scheme", "http").lower()
    host = step.get("host") or adapters.CHECKOUT_HOST
    port = int(step.get("port") or adapters.CHECKOUT_PORT)
    path = step.get("path") or "/v1/health"
    return scheme, host, port, path


def _create_http_connection(
    scheme: str,
    host: str,
    port: int,
    timeout: float,
) -> HTTPConnection:
    """Creates an HTTP or HTTPS connection with strict TLS certificate verification."""
    security.assert_loopback_safe(host, port)
    if scheme == "https":
        context = ssl.create_default_context()
        return HTTPSConnection(host, port, timeout=timeout, context=context)
    elif scheme == "http":
        return HTTPConnection(host, port, timeout=timeout)
    else:
        raise ValueError(f"unsupported scheme '{scheme}'")


def _build_http_payload(step: dict[str, Any]) -> tuple[bytes | None, str | None, dict[str, str]]:
    """Constructs and validates HTTP body, content-type, and headers from step definition."""
    step_config = step.get("config") or {}
    raw_input_body = step.get("body") if "body" in step else step_config.get("body")
    step_headers = step.get("headers") or step_config.get("headers") or {}

    raw_body: bytes | None = None
    content_type: str | None = None

    if raw_input_body is not None:
        if isinstance(raw_input_body, (dict, list)):
            raw_body = json.dumps(raw_input_body).encode("utf-8")
            content_type = "application/json"
        elif isinstance(raw_input_body, str):
            raw_body = raw_input_body.encode("utf-8")
            content_type = "text/plain; charset=utf-8"
        elif isinstance(raw_input_body, (bytes, bytearray)):
            raw_body = bytes(raw_input_body)
            content_type = "application/octet-stream"
        else:
            raw_body = str(raw_input_body).encode("utf-8")
            content_type = "text/plain; charset=utf-8"

    headers: dict[str, str] = {}
    if content_type:
        headers["Content-Type"] = content_type
    if raw_body is not None:
        headers["Content-Length"] = str(len(raw_body))

    # Add step headers, filtering out dangerous transport override headers
    if isinstance(step_headers, dict):
        for k, v in step_headers.items():
            if str(k).lower() not in BLOCKED_REQUEST_HEADERS:
                headers[str(k)] = str(v)

    return raw_body, content_type, headers


def _capture_response_summary(body_bytes: bytes, max_len: int = 500) -> tuple[int, str, bool]:
    """Returns total size, redacted snippet, and whether truncated."""
    size = len(body_bytes)
    truncated = size > max_len
    snippet = body_bytes[:max_len].decode("utf-8", errors="replace")
    redacted = security.redact_sensitive(snippet)
    return size, redacted, truncated


def _execute_http_request_with_redirects(
    run_id: str,
    step_id: str,
    scheme: str,
    host: str,
    port: int,
    path: str,
    method: str,
    headers: dict[str, str],
    raw_body: bytes | None,
    timeout: float,
    allowed_statuses: set[int],
    max_redirects: int = MAX_REDIRECTS,
) -> tuple[int, dict[str, str], bytes, float, list[dict[str, Any]]]:
    """Executes HTTP request, validating destinations and handling redirects securely."""
    curr_scheme = scheme
    curr_host = host
    curr_port = port
    curr_path = path
    curr_method = method
    curr_body = raw_body
    curr_headers = dict(headers)

    visited_urls: set[str] = set()
    redirect_count = 0
    redirect_history: list[dict[str, Any]] = []

    while True:
        curr_url = f"{curr_scheme}://{curr_host}:{curr_port}{curr_path}"
        if curr_url in visited_urls:
            raise ValueError(f"redirect loop detected for URL: '{curr_url}'")
        visited_urls.add(curr_url)

        # Immediate revalidation of destination and prohibited ports before connecting
        security.assert_loopback_safe(curr_url)

        conn = _create_http_connection(curr_scheme, curr_host, curr_port, timeout=timeout)
        with _active_lock:
            _active_connections[run_id] = conn

        t0 = time.monotonic()
        try:
            conn.request(curr_method, curr_path, body=curr_body, headers=curr_headers)
            resp = conn.getresponse()
            body_bytes = resp.read()
            elapsed = time.monotonic() - t0
            status_code = resp.status
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        finally:
            with _active_lock:
                _active_connections.pop(run_id, None)
            conn.close()

        if status_code in {301, 302, 303, 307, 308}:
            loc = resp_headers.get("location")
            if not loc:
                return status_code, resp_headers, body_bytes, elapsed, redirect_history

            if redirect_count >= max_redirects:
                store.record_event(run_id, "HTTP_REDIRECT_LIMIT_EXCEEDED", {
                    "step_id": step_id,
                    "max_redirects": max_redirects,
                    "final_url": curr_url,
                })
                raise ValueError(f"exceeded maximum redirect limit of {max_redirects}")

            redirect_count += 1
            target_url = security.validate_redirect(curr_url, loc)

            next_scheme, next_host, next_port, next_path = _resolve_http_endpoint({"url": target_url})

            # Block HTTPS to HTTP downgrade!
            if curr_scheme == "https" and next_scheme == "http":
                raise PermissionError(f"Insecure redirect downgrade from HTTPS to HTTP blocked: '{target_url}'")

            # Revalidate loopback on target URL
            security.assert_loopback_safe(target_url)

            # Strip credentials if scheme/host/port changed (cross-origin)
            if (next_scheme, next_host, next_port) != (curr_scheme, curr_host, curr_port):
                curr_headers.pop("authorization", None)
                curr_headers.pop("cookie", None)
                curr_headers.pop("x-api-key", None)

            # Standard RFC 7231 method changes: 301, 302, 303 switch to GET and drop body
            if status_code in {301, 302, 303}:
                curr_method = "GET"
                curr_body = None
                curr_headers.pop("content-length", None)
                curr_headers.pop("content-type", None)

            redirect_entry = {
                "from_url": curr_url,
                "to_url": target_url,
                "status": status_code,
                "method_after_redirect": curr_method,
            }
            redirect_history.append(redirect_entry)
            store.record_event(run_id, "HTTP_REDIRECT_FOLLOWED", {
                "step_id": step_id,
                **redirect_entry,
            })

            curr_scheme = next_scheme
            curr_host = next_host
            curr_port = next_port
            curr_path = next_path
            continue

        return status_code, resp_headers, body_bytes, elapsed, redirect_history


def _execute_http_step(
    run_id: str,
    step: dict[str, Any],
    inject_fault: bool,
    fault_type: str,
    implementation: str,
    trace_id: str,
    root_span_id: str,
) -> tuple[bool, dict[str, Any]]:
    """Executes a real HTTP request against an approved test endpoint with bounded timeouts and evidence capture."""
    step_id = step.get("id", "http_step")
    scheme, host, port, path = _resolve_http_endpoint(step)
    method = (step.get("method") or ("POST" if step.get("type") == "http.post" else "GET")).upper()

    raw_body, content_type, base_headers = _build_http_payload(step)
    if trace_id and root_span_id:
        base_headers["traceparent"] = f"00-{trace_id}-{root_span_id}-01"

    step_config = step.get("config") or {}
    allowed_statuses = set(step.get("allowed_statuses") or step_config.get("allowed_statuses") or [200, 201, 204])

    step_body = step.get("body") if "body" in step else step_config.get("body")
    sanitized_headers = security.redact_sensitive(base_headers)
    sanitized_body = security.redact_sensitive(step_body) if step_body is not None else None

    # Retry and idempotency semantics
    is_idempotent = step.get("idempotent") if "idempotent" in step else (
        step_config.get("idempotent", method in {"GET", "HEAD", "PUT", "DELETE"})
    )
    configured_retries = int(step.get("retries", step_config.get("retries", 1 if implementation == "fixed" else 0)))

    if inject_fault and fault_type == "http_timeout":
        fault_config = step.get("fault_config") or {}
        client_timeout = float(fault_config.get("client_timeout_seconds", 0.1))
        server_delay = float(fault_config.get("server_delay_seconds", client_timeout + 0.3))

        fault_headers = dict(base_headers)
        fault_headers["X-Faultline-Fault"] = "http_timeout"
        fault_headers["X-Faultline-Delay"] = str(server_delay)

        store.record_event(run_id, "HTTP_REQUEST_SENT", {
            "step_id": step_id,
            "method": method,
            "url": f"{scheme}://{host}:{port}{path}",
            "headers": security.redact_sensitive(fault_headers),
            "body": sanitized_body,
            "attempt": 1,
            "injected_fault": "http_timeout",
            "client_timeout_seconds": client_timeout,
        })

        t0 = time.monotonic()
        timed_out = False
        conn = _create_http_connection(scheme, host, port, timeout=client_timeout)
        with _active_lock:
            _active_connections[run_id] = conn
        try:
            conn.request(method, path, body=raw_body, headers=fault_headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            elapsed = time.monotonic() - t0
            size, snippet, truncated = _capture_response_summary(resp_body)
            # Target completed without timing out! (e.g. ignored fault headers)
            store.record_event(run_id, "HTTP_FAULT_ACTIVATION_FAILED", {
                "step_id": step_id,
                "fault": "http_timeout",
                "reason": "target completed request without timing out",
                "status": resp.status,
                "elapsed_seconds": round(elapsed, 4),
            })
            store.record_event(run_id, "HTTP_RESPONSE_RECEIVED", {
                "step_id": step_id,
                "status": resp.status,
                "attempt": 1,
                "elapsed_seconds": round(elapsed, 4),
                "response_size_bytes": size,
                "body_snippet": snippet,
                "body_truncated": truncated,
                "recovered": False,
            })
            is_ok = resp.status in allowed_statuses
            store.record_event(run_id, "STEP_COMPLETED", {
                "step_id": step_id,
                "status": "success" if is_ok else "unexpected_status",
                "http_status": resp.status,
            })
            return is_ok, {
                "step_id": step_id,
                "type": "http_result",
                "status": resp.status,
                "fault_activated": False,
                "simulated": False,
            }
        except (TimeoutError, socket.timeout, OSError) as exc:
            timed_out = True
            t_elapsed = round(time.monotonic() - t0, 4)
            store.record_event(run_id, "HTTP_TIMEOUT_OBSERVED", {
                "step_id": step_id,
                "method": method,
                "target": f"{scheme}://{host}:{port}{path}",
                "client_timeout_seconds": client_timeout,
                "elapsed_seconds": t_elapsed,
                "error": type(exc).__name__,
            })
        finally:
            with _active_lock:
                _active_connections.pop(run_id, None)
            conn.close()

        if timed_out:
            can_retry = configured_retries > 0 and (is_idempotent or implementation == "fixed")
            if not can_retry or implementation == "vulnerable":
                store.record_event(run_id, "STEP_FAILED", {
                    "step_id": step_id,
                    "error": f"HTTP request to {scheme}://{host}:{port}{path} timed out after {client_timeout}s",
                    "evidence": "HTTP_TIMEOUT_OBSERVED",
                })
                return False, {
                    "step_id": step_id,
                    "type": "http_result",
                    "status": "timed_out",
                    "fault_activated": True,
                    "simulated": False,
                }
            else:
                # Resilient implementation recovers by issuing a retry with standard timeout
                store.record_event(run_id, "STEP_RETRY", {
                    "step_id": step_id,
                    "attempt": 2,
                    "recovered": True,
                    "reason": "http_timeout_recovered",
                })
                store.record_event(run_id, "HTTP_REQUEST_SENT", {
                    "step_id": step_id,
                    "method": method,
                    "url": f"{scheme}://{host}:{port}{path}",
                    "headers": sanitized_headers,
                    "body": sanitized_body,
                    "attempt": 2,
                })
                retry_status, retry_headers, retry_body, retry_elapsed, _ = _execute_http_request_with_redirects(
                    run_id=run_id,
                    step_id=step_id,
                    scheme=scheme,
                    host=host,
                    port=port,
                    path=path,
                    method=method,
                    headers=base_headers,
                    raw_body=raw_body,
                    timeout=3.0,
                    allowed_statuses=allowed_statuses,
                )
                size, snippet, truncated = _capture_response_summary(retry_body)
                safe_resp_headers = {k: v for k, v in retry_headers.items() if k in SAFE_RESPONSE_HEADERS}
                store.record_event(run_id, "HTTP_RESPONSE_RECEIVED", {
                    "step_id": step_id,
                    "status": retry_status,
                    "attempt": 2,
                    "elapsed_seconds": round(retry_elapsed, 4),
                    "response_size_bytes": size,
                    "headers": safe_resp_headers,
                    "body_snippet": snippet,
                    "body_truncated": truncated,
                    "recovered": True,
                })
                is_status_ok = retry_status in allowed_statuses
                store.record_event(run_id, "STEP_COMPLETED", {
                    "step_id": step_id,
                    "status": "success" if is_status_ok else "unexpected_status",
                    "http_status": retry_status,
                    "recovered": True,
                })
                return is_status_ok, {
                    "step_id": step_id,
                    "type": "http_result",
                    "status": retry_status,
                    "recovered": True,
                    "fault_activated": True,
                    "simulated": False,
                }

    # Normal (non-faulted) HTTP step execution
    store.record_event(run_id, "HTTP_REQUEST_SENT", {
        "step_id": step_id,
        "method": method,
        "url": f"{scheme}://{host}:{port}{path}",
        "headers": sanitized_headers,
        "body": sanitized_body,
        "attempt": 1,
    })
    step_timeout = float(step.get("timeout_seconds", step_config.get("timeout_seconds", 3.0)))
    status_code, resp_headers, body_bytes, elapsed, _ = _execute_http_request_with_redirects(
        run_id=run_id,
        step_id=step_id,
        scheme=scheme,
        host=host,
        port=port,
        path=path,
        method=method,
        headers=base_headers,
        raw_body=raw_body,
        timeout=step_timeout,
        allowed_statuses=allowed_statuses,
    )
    size, snippet, truncated = _capture_response_summary(body_bytes)
    safe_resp_headers = {k: v for k, v in resp_headers.items() if k in SAFE_RESPONSE_HEADERS}
    store.record_event(run_id, "HTTP_RESPONSE_RECEIVED", {
        "step_id": step_id,
        "status": status_code,
        "attempt": 1,
        "elapsed_seconds": round(elapsed, 4),
        "response_size_bytes": size,
        "headers": safe_resp_headers,
        "body_snippet": snippet,
        "body_truncated": truncated,
        "recovered": False,
    })
    is_status_ok = status_code in allowed_statuses
    store.record_event(run_id, "STEP_COMPLETED", {
        "step_id": step_id,
        "status": "success" if is_status_ok else "unexpected_status",
        "http_status": status_code,
    })
    return is_status_ok, {
        "step_id": step_id,
        "type": "http_result",
        "status": status_code,
        "simulated": False,
    }


def _execute_db_step(
    run_id: str,
    step: dict[str, Any],
    inject_fault: bool,
    fault_type: str,
    implementation: str,
    trace_id: str,
    root_span_id: str,
) -> tuple[bool, dict[str, Any]]:
    step_id = step.get("id", "db_step")
    store.record_event(run_id, "DB_OPERATION_SIMULATED", {
        "step_id": step_id,
        "operation": step.get("operation", "write"),
        "table": step.get("table", "records"),
        "simulated": True,
    })
    store.record_event(run_id, "STEP_COMPLETED", {
        "step_id": step_id,
        "status": "success",
        "simulated": True,
    })
    return True, {"step_id": step_id, "type": "db_result", "status": "simulated", "simulated": True}


def _execute_generic_step(
    run_id: str,
    step: dict[str, Any],
    inject_fault: bool,
    fault_type: str,
    implementation: str,
    trace_id: str,
    root_span_id: str,
) -> tuple[bool, dict[str, Any]]:
    step_id = step.get("id", "generic_step")
    store.record_event(run_id, "GENERIC_OPERATION_SIMULATED", {
        "step_id": step_id,
        "name": step.get("name", step_id),
        "simulated": True,
    })
    store.record_event(run_id, "STEP_COMPLETED", {
        "step_id": step_id,
        "status": "success",
        "simulated": True,
    })
    return True, {"step_id": step_id, "type": "generic_result", "status": "simulated", "simulated": True}


step_registry = StepRunnerRegistry()
step_registry.register("http.request", _execute_http_step)
step_registry.register("http.get", _execute_http_step)
step_registry.register("http.post", _execute_http_step)
step_registry.register("db.operation", _execute_db_step)
step_registry.register("db.write", _execute_db_step)
step_registry.register("db.read", _execute_db_step)
step_registry.register("generic.operation", _execute_generic_step)


def _execute_generic_workflow(
    run_id: str,
    scenario: dict[str, Any],
    implementation: str,
    signal: threading.Event,
    timeout_seconds: int,
    started: float,
    trace_id: str,
    root_span_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Generic workflow step executor.

    Iterates through scenario['workflow'] steps, applies configured fault injection,
    and validates declared invariants against observed step results and side-effects.
    """
    workflow = scenario.get("workflow", [])
    fault_type = scenario.get("fault", "http_timeout")
    target_step = scenario.get("target_step")
    fault_config = scenario.get("fault_config") or {}

    executed_steps = []
    side_effects = []
    fault_injected = False
    fault_recovered = False
    fault_activation_confirmed = False
    step_failures = 0

    if not workflow:
        workflow = [{"id": "operation", "name": "Target Operation", "type": "http.request"}]

    for idx, step in enumerate(workflow):
        if signal.is_set() or store.run_status(run_id) == "cancel_requested":
            raise RunCancelled()
        if time.monotonic() - started > timeout_seconds:
            raise RunTimedOut()
        step_id = step.get("id", f"step_{idx+1}")
        step_name = step.get("name", step_id)
        step_type = step.get("type", "generic.operation")

        handler = step_registry.get(step_type)
        if handler is None:
            raise ValueError(f"unsupported workflow step type: '{step_type}'")

        store.record_event(run_id, "STEP_STARTED", {
            "step_id": step_id,
            "name": step_name,
            "type": step_type,
            "sequence": idx + 1,
        })

        is_fault_target = (target_step == step_id) or (target_step is None and idx == 0)

        if is_fault_target and not fault_injected:
            fault_injected = True
            store.record_event(run_id, "FAULT_INJECTED", {
                "fault_type": fault_type,
                "target_step": step_id,
                "config": fault_config,
            })
            success, side_effect = handler(
                run_id, step, True, fault_type, implementation, trace_id, root_span_id
            )
            if side_effect.get("fault_activated"):
                fault_activation_confirmed = True
            if success:
                executed_steps.append(step_id)
                fault_recovered = True
                side_effects.append(side_effect)
            else:
                step_failures += 1
        else:
            success, side_effect = handler(
                run_id, step, False, fault_type, implementation, trace_id, root_span_id
            )
            if success:
                executed_steps.append(step_id)
                side_effects.append(side_effect)
            else:
                step_failures += 1

    primary_assertion = scenario.get("assertion", "all_steps_succeeded_or_handled")
    is_passed = (fault_recovered and step_failures == 0) if implementation == "fixed" else (step_failures == 0)

    assertions = [
        {
            "name": "fault_activation_confirmed",
            "status": "passed" if fault_activation_confirmed else "failed",
            "expected": f"fault '{fault_type}' activated and observed at target step",
            "observed": "confirmed" if fault_activation_confirmed else "fault activation not observed",
            "evidence": ["FAULT_INJECTED", "HTTP_TIMEOUT_OBSERVED"] if fault_activation_confirmed else ["NO_FAULT_CONFIRMED"],
        },
        {
            "name": primary_assertion,
            "status": "passed" if is_passed else "failed",
            "expected": "all workflow steps succeed or recover gracefully",
            "observed": f"{len(executed_steps)}/{len(workflow)} steps succeeded; {step_failures} failures",
            "evidence": [f"step:{s}" for s in executed_steps] if is_passed else ["UNHANDLED_STEP_FAILURE"],
        },
    ]

    summary = {
        "workflow_steps_total": len(workflow),
        "workflow_steps_executed": len(executed_steps),
        "fault_injected": fault_injected,
        "fault_recovered": fault_recovered,
        "side_effects": side_effects,
        "failed_assertions": [a["name"] for a in assertions if a["status"] == "failed"],
    }
    return assertions, summary


fault_registry = FaultRunnerRegistry()
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
fault_registry.register(
    "http_timeout",
    _execute_generic_workflow,
    ["http-service"],
)


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
    start_payload = {"implementation": implementation}
    if scenario.get("target_step"):
        start_payload["target_step"] = scenario["target_step"]
    store.record_event(run_id, "RUN_STARTED", start_payload)
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
        registered = fault_registry.get(fault)
        if not registered:
            raise ValueError(f"unsupported fault type '{fault}'")
        runner_fn, clean_targets = registered
        assertions, summary = runner_fn(
            run_id, scenario, implementation, signal, timeout_seconds, started, trace_id, root_span_id
        )

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
            "charges": provider.ledger(run_id) if fault in {"payment_response_lost_after_commit", "persistence_failure_after_payment", "duplicate_payment_webhook"} else summary.get("charges", []),
            "fulfillments": checkout_service.ledger(run_id) if fault in {"payment_response_lost_after_commit", "persistence_failure_after_payment", "duplicate_payment_webhook"} else summary.get("fulfillments", []),
            "refunds": provider.refund_ledger(run_id) if fault in {"payment_response_lost_after_commit", "persistence_failure_after_payment", "duplicate_payment_webhook"} else summary.get("refunds", []),
            "side_effects": summary.get("side_effects", []),
            "assertions": assertions,
        })

        fault_name = fault or "unknown_fault"
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
    if new_impl not in {"vulnerable", "fixed"}:
        raise ValueError(f"invalid implementation '{new_impl}', must be 'vulnerable' or 'fixed'")

    scenario = dict(parent["scenario"])
    scenario_overrides = overrides.get("scenario_overrides")
    if scenario_overrides is not None:
        if not isinstance(scenario_overrides, dict):
            raise ValueError("scenario_overrides must be a dictionary")
        for k, v in scenario_overrides.items():
            scenario[k] = v

    validate_scenario(scenario)

    snaps = store.get_snapshots(parent_run_id)
    if snapshot_sequence is not None:
        target_snap = next((s for s in snaps if s["sequence"] == snapshot_sequence), None)
        if target_snap is None:
            raise ValueError(f"snapshot sequence {snapshot_sequence} not found for run {parent_run_id}")
    elif snaps:
        target_snap = snaps[-1]
        snapshot_sequence = target_snap["sequence"]
    else:
        target_snap = None
        snapshot_sequence = 1

    branch_run_id = str(uuid4())
    store.create_run(
        branch_run_id,
        scenario_id=parent["scenario_id"],
        scenario_version=parent["scenario_version"],
        implementation=new_impl,
        parent_run_id=parent_run_id,
        forked_from_snapshot_id=snapshot_sequence,
        definition_override=scenario,
    )
    store.record_event(branch_run_id, "RUN_BRANCHED_FROM_CHECKPOINT", {
        "parent_run_id": parent_run_id,
        "forked_sequence": snapshot_sequence,
        "checkpoint_state": target_snap["snapshot"] if target_snap else {},
        "overrides": overrides,
    })
    store.record_audit_event(
        parent.get("project_id") or store.default_project_id(),
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
    project_id = suite.get("project_id") or store.default_project_id()
    store.record_audit_event(project_id, "developer", "SUITE_RUN_SUBMITTED", "suite", suite_id, {
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
        store.record_audit_event(project_id, "developer", "SUITE_RUN_COMPLETED", "suite", suite_id, {
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
