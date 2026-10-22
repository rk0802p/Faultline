"""Background scenario execution for the local checkout environment."""

import errno
import http.client
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
from .evaluators import (
    AssertionEvaluatorRegistry,
    assertion_registry,
    evaluate_fault_activation_confirmed,
    evaluate_all_steps_succeeded_or_handled,
    evaluate_step_execution_verified,
)
from .schema import fault_schema_registry, validate_scenario, SUPPORTED_STEP_TYPES, HTTP_STEP_TYPES


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


class CleanupRegistry:
    """Registry of integration-specific cleanup hooks."""

    def __init__(self) -> None:
        self._hooks: dict[str, Any] = {}

    def register(self, name: str, hook: Any) -> None:
        self._hooks[name] = hook

    def cleanup(self, run_id: str, clean_targets: list[str] | None = None) -> list[str]:
        owned: list[str] = []
        for name, hook in self._hooks.items():
            if clean_targets is None or name in clean_targets or any(t.startswith(name) for t in clean_targets):
                try:
                    res = hook(run_id)
                    if isinstance(res, (list, tuple)):
                        owned.extend(res)
                    elif res:
                        owned.append(str(res))
                except Exception as exc:
                    logger.warning("cleanup hook '%s' failed for run_id=%s: %s", name, run_id, exc)
        return list(dict.fromkeys(owned))


cleanup_registry = CleanupRegistry()


def cleanup_all(run_id: str, clean_targets: list[str] | None = None) -> dict[str, Any]:
    try:
        t0 = time.monotonic()
    except (StopIteration, Exception):
        t0 = 0.0
    try:
        owned_resources = cleanup_registry.cleanup(run_id, clean_targets)
        if clean_targets:
            for t in clean_targets:
                if not any(r.startswith(f"target:{t}") for r in owned_resources):
                    owned_resources.append(f"target:{t}:{run_id}")
        try:
            duration_ms = max(1, int((time.monotonic() - t0) * 1000))
        except (StopIteration, Exception):
            duration_ms = 1
        store.record_cleanup(run_id, "succeeded", owned_resources=owned_resources, duration_ms=duration_ms)
        store.record_event(run_id, "TEST_ENVIRONMENT_CLEANED", {
            "status": "succeeded",
            "targets": clean_targets or list(cleanup_registry._hooks.keys()),
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
        store.record_cleanup(run_id, "failed", owned_resources=[], error=str(exc), duration_ms=duration_ms)
        store.record_event(run_id, "TEST_ENVIRONMENT_CLEANUP_FAILED", {
            "status": "failed",
            "owned_resources": [],
            "error": str(exc),
            "duration_ms": duration_ms,
        })
        return {"status": "failed", "owned_resources": [], "error": str(exc), "duration_ms": duration_ms}


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
MAX_RESPONSE_BYTES_DEFAULT = security.MAX_RESPONSE_BYTES_DEFAULT
MAX_RESPONSE_BYTES_CEILING = security.MAX_RESPONSE_BYTES_CEILING
validate_max_response_bytes = security.validate_max_response_bytes

BLOCKED_REQUEST_HEADERS = {"host", "transfer-encoding", "connection", "upgrade"}
SENSITIVE_REDIRECT_HEADERS = {
    "authorization",
    "cookie",
    "cookie2",
    "proxy-authorization",
    "x-api-key",
    "x-auth-token",
}
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


def _read_bounded_response(
    resp: http.client.HTTPResponse,
    max_bytes: int = MAX_RESPONSE_BYTES_DEFAULT,
) -> tuple[bytes, int, bool]:
    """Reads response body in bounded chunks up to max_bytes.

    Returns (captured_bytes, total_bytes_read, was_truncated).
    """
    chunks: list[bytes] = []
    total_read = 0
    was_truncated = False
    chunk_size = 65536  # 64 KB

    while True:
        to_read = min(chunk_size, max_bytes - total_read + 1)
        chunk = resp.read(to_read)
        if not chunk:
            break
        total_read += len(chunk)
        if total_read > max_bytes:
            overflow = total_read - max_bytes
            if len(chunk) > overflow:
                chunks.append(chunk[:-overflow])
            was_truncated = True
            break
        chunks.append(chunk)

    captured = b"".join(chunks)
    return captured, total_read, was_truncated


def _capture_response_summary(
    body_bytes: bytes,
    total_bytes_read: int | None = None,
    body_truncated: bool = False,
    max_len: int = 500,
) -> tuple[int, str, bool]:
    """Returns total size, redacted snippet, and whether truncated."""
    size = total_bytes_read if total_bytes_read is not None else len(body_bytes)
    truncated = body_truncated or (size > max_len)
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
    max_response_bytes: int = MAX_RESPONSE_BYTES_DEFAULT,
) -> tuple[int, dict[str, str], bytes, float, list[dict[str, Any]], bool, int]:
    """Executes HTTP request, validating destinations and handling redirects securely."""
    # Enforce positive bounded max_response_bytes before creating any connection
    max_response_bytes = validate_max_response_bytes(max_response_bytes)

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
            body_bytes, total_read, body_truncated = _read_bounded_response(resp, max_bytes=max_response_bytes)
            elapsed = time.monotonic() - t0
            status_code = resp.status
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        finally:
            with _active_lock:
                _active_connections.pop(run_id, None)
            conn.close()

        if body_truncated:
            store.record_event(run_id, "HTTP_RESPONSE_BODY_LIMIT_EXCEEDED", {
                "step_id": step_id,
                "url": curr_url,
                "max_response_bytes": max_response_bytes,
                "bytes_read": total_read,
            })

        if status_code in {301, 302, 303, 307, 308}:
            loc = resp_headers.get("location")
            if not loc:
                return status_code, resp_headers, body_bytes, elapsed, redirect_history, body_truncated, total_read

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

            # Case-insensitive credential stripping if origin (scheme/host/port) changed
            if (next_scheme, next_host, next_port) != (curr_scheme, curr_host, curr_port):
                curr_headers = {
                    k: v for k, v in curr_headers.items()
                    if k.lower() not in SENSITIVE_REDIRECT_HEADERS
                }

            # Standard RFC 7231 method changes: 301, 302, 303 switch to GET and drop body
            if status_code in {301, 302, 303}:
                curr_method = "GET"
                curr_body = None
                curr_headers = {
                    k: v for k, v in curr_headers.items()
                    if k.lower() not in {"content-length", "content-type"}
                }

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

        return status_code, resp_headers, body_bytes, elapsed, redirect_history, body_truncated, total_read


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

    # Reject invalid max_response_bytes configuration before opening connection
    raw_mrb = step.get("max_response_bytes") if "max_response_bytes" in step else step_config.get("max_response_bytes")
    try:
        max_response_bytes = validate_max_response_bytes(raw_mrb)
    except ValueError as exc:
        store.record_event(run_id, "STEP_FAILED", {
            "step_id": step_id,
            "error": f"Invalid max_response_bytes: {exc}",
            "evidence": "INVALID_CONFIG",
        })
        return False, {
            "step_id": step_id,
            "type": "http_result",
            "status": "config_error",
            "error": str(exc),
            "fault_activated": False,
            "simulated": False,
        }

    if inject_fault and fault_type == "http_timeout":
        fault_config = step.get("fault_config") or {}
        raw_ct = fault_config.get("client_timeout_seconds", 0.1)
        raw_sd = fault_config.get("server_delay_seconds")
        try:
            client_timeout = float(raw_ct)
            if client_timeout <= 0:
                raise ValueError(f"client_timeout_seconds must be positive (> 0), got {client_timeout}")
            server_delay = float(raw_sd) if raw_sd is not None else client_timeout + 0.3
            if server_delay < 0:
                raise ValueError(f"server_delay_seconds must be non-negative (>= 0), got {server_delay}")
        except (ValueError, TypeError) as exc:
            store.record_event(run_id, "STEP_FAILED", {
                "step_id": step_id,
                "error": f"Invalid fault configuration: {exc}",
                "evidence": "INVALID_FAULT_CONFIG",
            })
            return False, {
                "step_id": step_id,
                "type": "http_result",
                "status": "config_error",
                "error": str(exc),
                "fault_activated": False,
                "simulated": False,
            }

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
        conn = _create_http_connection(scheme, host, port, timeout=3.0)
        with _active_lock:
            _active_connections[run_id] = conn
        try:
            conn.connect()
            if conn.sock:
                conn.sock.settimeout(client_timeout)
            conn.request(method, path, body=raw_body, headers=fault_headers)
            resp = conn.getresponse()
            captured, total_read, was_truncated = _read_bounded_response(resp, max_bytes=max_response_bytes)
            elapsed = time.monotonic() - t0
            size, snippet, truncated = _capture_response_summary(captured, total_bytes_read=total_read, body_truncated=was_truncated)
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
                "headers": {k.lower(): v for k, v in resp.getheaders() if k.lower() in SAFE_RESPONSE_HEADERS},
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
        except ssl.SSLError as exc:
            t_elapsed = round(time.monotonic() - t0, 4)
            store.record_event(run_id, "HTTP_TLS_ERROR", {
                "step_id": step_id,
                "target": f"{scheme}://{host}:{port}{path}",
                "elapsed_seconds": t_elapsed,
                "error": type(exc).__name__,
                "details": str(exc),
            })
            store.record_event(run_id, "STEP_FAILED", {
                "step_id": step_id,
                "error": f"TLS verification error connecting to {scheme}://{host}:{port}{path}: {exc}",
                "evidence": "HTTP_TLS_ERROR",
            })
            return False, {
                "step_id": step_id,
                "type": "http_result",
                "status": "tls_error",
                "fault_activated": False,
                "simulated": False,
            }
        except ConnectionRefusedError as exc:
            t_elapsed = round(time.monotonic() - t0, 4)
            store.record_event(run_id, "HTTP_CONNECTION_REFUSED", {
                "step_id": step_id,
                "target": f"{scheme}://{host}:{port}{path}",
                "elapsed_seconds": t_elapsed,
                "error": type(exc).__name__,
                "details": str(exc),
            })
            store.record_event(run_id, "STEP_FAILED", {
                "step_id": step_id,
                "error": f"Connection refused connecting to {scheme}://{host}:{port}{path}",
                "evidence": "HTTP_CONNECTION_REFUSED",
            })
            return False, {
                "step_id": step_id,
                "type": "http_result",
                "status": "connection_refused",
                "fault_activated": False,
                "simulated": False,
            }
        except (ConnectionError, http.client.RemoteDisconnected) as exc:
            t_elapsed = round(time.monotonic() - t0, 4)
            store.record_event(run_id, "HTTP_CONNECTION_FAILED", {
                "step_id": step_id,
                "target": f"{scheme}://{host}:{port}{path}",
                "elapsed_seconds": t_elapsed,
                "error": type(exc).__name__,
                "details": str(exc),
            })
            store.record_event(run_id, "STEP_FAILED", {
                "step_id": step_id,
                "error": f"Connection error connecting to {scheme}://{host}:{port}{path}: {exc}",
                "evidence": "HTTP_CONNECTION_FAILED",
            })
            return False, {
                "step_id": step_id,
                "type": "http_result",
                "status": "connection_failed",
                "fault_activated": False,
                "simulated": False,
            }
        except (TimeoutError, socket.timeout) as exc:
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
        except OSError as exc:
            if getattr(exc, "errno", None) in (errno.ETIMEDOUT, 10060):
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
            else:
                t_elapsed = round(time.monotonic() - t0, 4)
                store.record_event(run_id, "HTTP_NETWORK_ERROR", {
                    "step_id": step_id,
                    "target": f"{scheme}://{host}:{port}{path}",
                    "elapsed_seconds": t_elapsed,
                    "error": type(exc).__name__,
                    "details": str(exc),
                })
                store.record_event(run_id, "STEP_FAILED", {
                    "step_id": step_id,
                    "error": f"Network error connecting to {scheme}://{host}:{port}{path}: {exc}",
                    "evidence": "HTTP_NETWORK_ERROR",
                })
                return False, {
                    "step_id": step_id,
                    "type": "http_result",
                    "status": "network_error",
                    "fault_activated": False,
                    "simulated": False,
                }
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
                try:
                    retry_status, retry_headers, retry_body, retry_elapsed, _, retry_truncated, retry_total_read = _execute_http_request_with_redirects(
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
                        max_response_bytes=max_response_bytes,
                    )
                except ssl.SSLError as exc:
                    store.record_event(run_id, "HTTP_TLS_ERROR", {
                        "step_id": step_id,
                        "target": f"{scheme}://{host}:{port}{path}",
                        "error": type(exc).__name__,
                        "details": str(exc),
                    })
                    store.record_event(run_id, "STEP_FAILED", {
                        "step_id": step_id,
                        "error": f"TLS verification error during retry to {scheme}://{host}:{port}{path}: {exc}",
                        "evidence": "HTTP_TLS_ERROR",
                    })
                    return False, {
                        "step_id": step_id,
                        "type": "http_result",
                        "status": "tls_error",
                        "fault_activated": True,
                        "simulated": False,
                    }
                except ConnectionRefusedError as exc:
                    store.record_event(run_id, "HTTP_CONNECTION_REFUSED", {
                        "step_id": step_id,
                        "target": f"{scheme}://{host}:{port}{path}",
                        "error": type(exc).__name__,
                    })
                    store.record_event(run_id, "STEP_FAILED", {
                        "step_id": step_id,
                        "error": f"Connection refused during retry to {scheme}://{host}:{port}{path}",
                        "evidence": "HTTP_CONNECTION_REFUSED",
                    })
                    return False, {
                        "step_id": step_id,
                        "type": "http_result",
                        "status": "connection_refused",
                        "fault_activated": True,
                        "simulated": False,
                    }
                except (ConnectionError, http.client.RemoteDisconnected) as exc:
                    store.record_event(run_id, "HTTP_CONNECTION_FAILED", {
                        "step_id": step_id,
                        "target": f"{scheme}://{host}:{port}{path}",
                        "error": type(exc).__name__,
                        "details": str(exc),
                    })
                    store.record_event(run_id, "STEP_FAILED", {
                        "step_id": step_id,
                        "error": f"Connection error during retry to {scheme}://{host}:{port}{path}: {exc}",
                        "evidence": "HTTP_CONNECTION_FAILED",
                    })
                    return False, {
                        "step_id": step_id,
                        "type": "http_result",
                        "status": "connection_failed",
                        "fault_activated": True,
                        "simulated": False,
                    }
                except (TimeoutError, socket.timeout) as exc:
                    store.record_event(run_id, "HTTP_TIMEOUT_OBSERVED", {
                        "step_id": step_id,
                        "target": f"{scheme}://{host}:{port}{path}",
                        "error": type(exc).__name__,
                    })
                    store.record_event(run_id, "STEP_FAILED", {
                        "step_id": step_id,
                        "error": f"Retry to {scheme}://{host}:{port}{path} timed out",
                        "evidence": "HTTP_TIMEOUT_OBSERVED",
                    })
                    return False, {
                        "step_id": step_id,
                        "type": "http_result",
                        "status": "timed_out",
                        "fault_activated": True,
                        "simulated": False,
                    }
                except Exception as exc:
                    store.record_event(run_id, "STEP_FAILED", {
                        "step_id": step_id,
                        "error": f"Retry request to {scheme}://{host}:{port}{path} failed: {exc}",
                        "evidence": "HTTP_RETRY_FAILED",
                    })
                    return False, {
                        "step_id": step_id,
                        "type": "http_result",
                        "status": "retry_failed",
                        "fault_activated": True,
                        "simulated": False,
                    }

                size, snippet, truncated = _capture_response_summary(retry_body, total_bytes_read=retry_total_read, body_truncated=retry_truncated)
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
    try:
        status_code, resp_headers, body_bytes, elapsed, _, body_truncated, total_read = _execute_http_request_with_redirects(
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
            max_response_bytes=max_response_bytes,
        )
    except ssl.SSLError as exc:
        store.record_event(run_id, "HTTP_TLS_ERROR", {
            "step_id": step_id,
            "target": f"{scheme}://{host}:{port}{path}",
            "error": type(exc).__name__,
            "details": str(exc),
        })
        store.record_event(run_id, "STEP_FAILED", {
            "step_id": step_id,
            "error": f"TLS verification error connecting to {scheme}://{host}:{port}{path}: {exc}",
            "evidence": "HTTP_TLS_ERROR",
        })
        return False, {
            "step_id": step_id,
            "type": "http_result",
            "status": "tls_error",
            "simulated": False,
        }
    except ConnectionRefusedError as exc:
        store.record_event(run_id, "HTTP_CONNECTION_REFUSED", {
            "step_id": step_id,
            "target": f"{scheme}://{host}:{port}{path}",
            "error": type(exc).__name__,
            "details": str(exc),
        })
        store.record_event(run_id, "STEP_FAILED", {
            "step_id": step_id,
            "error": f"Connection refused connecting to {scheme}://{host}:{port}{path}",
            "evidence": "HTTP_CONNECTION_REFUSED",
        })
        return False, {
            "step_id": step_id,
            "type": "http_result",
            "status": "connection_refused",
            "simulated": False,
        }
    except (ConnectionError, http.client.RemoteDisconnected) as exc:
        store.record_event(run_id, "HTTP_CONNECTION_FAILED", {
            "step_id": step_id,
            "target": f"{scheme}://{host}:{port}{path}",
            "error": type(exc).__name__,
            "details": str(exc),
        })
        store.record_event(run_id, "STEP_FAILED", {
            "step_id": step_id,
            "error": f"Connection error connecting to {scheme}://{host}:{port}{path}: {exc}",
            "evidence": "HTTP_CONNECTION_FAILED",
        })
        return False, {
            "step_id": step_id,
            "type": "http_result",
            "status": "connection_failed",
            "simulated": False,
        }
    except (TimeoutError, socket.timeout) as exc:
        store.record_event(run_id, "HTTP_TIMEOUT_OBSERVED", {
            "step_id": step_id,
            "target": f"{scheme}://{host}:{port}{path}",
            "error": type(exc).__name__,
        })
        store.record_event(run_id, "STEP_FAILED", {
            "step_id": step_id,
            "error": f"Request to {scheme}://{host}:{port}{path} timed out after {step_timeout}s",
            "evidence": "HTTP_TIMEOUT_OBSERVED",
        })
        return False, {
            "step_id": step_id,
            "type": "http_result",
            "status": "timed_out",
            "simulated": False,
        }
    except (ValueError, PermissionError) as exc:
        store.record_event(run_id, "STEP_FAILED", {
            "step_id": step_id,
            "error": str(exc),
            "evidence": "HTTP_SECURITY_VIOLATION",
        })
        return False, {
            "step_id": step_id,
            "type": "http_result",
            "status": "security_violation",
            "simulated": False,
        }
    except OSError as exc:
        store.record_event(run_id, "HTTP_NETWORK_ERROR", {
            "step_id": step_id,
            "target": f"{scheme}://{host}:{port}{path}",
            "error": type(exc).__name__,
            "details": str(exc),
        })
        store.record_event(run_id, "STEP_FAILED", {
            "step_id": step_id,
            "error": f"Network error connecting to {scheme}://{host}:{port}{path}: {exc}",
            "evidence": "HTTP_NETWORK_ERROR",
        })
        return False, {
            "step_id": step_id,
            "type": "http_result",
            "status": "network_error",
            "simulated": False,
        }

    size, snippet, truncated = _capture_response_summary(body_bytes, total_bytes_read=total_read, body_truncated=body_truncated)
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

        effective_step = dict(step)
        if "max_response_bytes" not in effective_step and "max_response_bytes" in scenario:
            effective_step["max_response_bytes"] = scenario["max_response_bytes"]

        if is_fault_target and not fault_injected:
            fault_injected = True
            # Explicit precedence: step-level fault_config specializes and overrides scenario-level fault_config
            resolved_fault_config = {
                **(scenario.get("fault_config") or {}),
                **(step.get("fault_config") or {}),
            }
            effective_step["fault_config"] = resolved_fault_config

            store.record_event(run_id, "FAULT_INJECTED", {
                "fault_type": fault_type,
                "target_step": step_id,
                "config": resolved_fault_config,
            })
            success, side_effect = handler(
                run_id, effective_step, True, fault_type, implementation, trace_id, root_span_id
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
                run_id, effective_step, False, fault_type, implementation, trace_id, root_span_id
            )
            if success:
                executed_steps.append(step_id)
                side_effects.append(side_effect)
            else:
                step_failures += 1

    primary_assertion = scenario.get("assertion", "all_steps_succeeded_or_handled")
    is_passed = (fault_recovered and step_failures == 0) if implementation == "fixed" else (step_failures == 0)

    context = {
        "fault_activation_confirmed": fault_activation_confirmed,
        "fault_type": fault_type,
        "is_passed": is_passed,
        "executed_steps": executed_steps,
        "workflow": workflow,
        "step_failures": step_failures,
    }
    assertions = [
        assertion_registry.evaluate("fault_activation_confirmed", context),
        assertion_registry.evaluate(primary_assertion, context),
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
    "http_timeout",
    _execute_generic_workflow,
    ["http-service"],
)

from .integrations import register_all_integrations

register_all_integrations(
    fault_registry=fault_registry,
    step_registry=step_registry,
    assertion_registry=assertion_registry,
    schema_registry=fault_schema_registry,
    cleanup_registry=cleanup_registry,
)

from .integrations.payment import (
    _execute_payment_response_lost,
    _execute_duplicate_webhook,
    _execute_persistence_failure,
)


def register_fault(fault_type: str, handler: Any, cleanup_targets: list[str]) -> None:
    fault_registry.register(fault_type, handler, cleanup_targets)


def register_step(step_type: str, handler: Any) -> None:
    step_registry.register(step_type, handler)
    if step_type not in SUPPORTED_STEP_TYPES:
        SUPPORTED_STEP_TYPES.add(step_type)


def register_assertion(name: str, evaluator: Any) -> None:
    assertion_registry.register(name, evaluator)


def register_cleanup_hook(name: str, hook: Any) -> None:
    cleanup_registry.register(name, hook)


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
            "charges": summary.get("charges", []),
            "fulfillments": summary.get("fulfillments", []),
            "refunds": summary.get("refunds", []),
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
