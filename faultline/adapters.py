"""Local sample checkout and payment-provider HTTP services.

The only fault adapter is deliberately constrained to loopback and a known test
provider. It commits a charge, then closes the connection before the response.
"""

import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPConnection, HTTPException
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import store


logger = logging.getLogger("faultline.adapter.http")


class TestPaymentProvider:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.charges: dict[str, list[dict[str, Any]]] = {}
        self.idempotency: dict[tuple[str, str], dict[str, Any]] = {}
        self.faulted_runs: set[str] = set()

    def charge(self, payload: dict[str, Any], lose_response: bool) -> tuple[dict[str, Any], bool]:
        run_id = payload["run_id"]
        key = payload.get("idempotency_key")
        with self.lock:
            if key and (run_id, key) in self.idempotency:
                result = self.idempotency[(run_id, key)]
                logger.debug("idempotency replay run_id=%s charge_id=%s", run_id, result["id"])
                store.record_event(run_id, "PROVIDER_IDEMPOTENCY_REPLAYED", {"charge_id": result["id"]})
                return result, False
            result = {
                "id": f"ch_{len(self.charges.get(run_id, [])) + 1}",
                "order_id": payload["order_id"],
                "amount_cents": payload["amount_cents"],
                "status": "succeeded",
                "idempotency_key": key,
            }
            self.charges.setdefault(run_id, []).append(result)
            if key:
                self.idempotency[(run_id, key)] = result
            store.record_event(run_id, "PROVIDER_CHARGE_COMMITTED", {"charge_id": result["id"], "order_id": result["order_id"]})
            should_lose = lose_response and run_id not in self.faulted_runs
            if should_lose:
                self.faulted_runs.add(run_id)
                logger.debug("dropping provider response after commit run_id=%s charge_id=%s", run_id, result["id"])
                store.record_event(run_id, "FAULT_RESPONSE_DROPPED", {"adapter": "local-http", "after_commit": True})
            return result, should_lose

    def ledger(self, run_id: str) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.charges.get(run_id, []))

    def cleanup(self, run_id: str) -> None:
        with self.lock:
            self.charges.pop(run_id, None)
            self.faulted_runs.discard(run_id)
            for key in [key for key in self.idempotency if key[0] == run_id]:
                self.idempotency.pop(key, None)


provider = TestPaymentProvider()


def json_reply(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class ProviderHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/v1/charges":
            json_reply(self, 404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            if payload.get("run_id") is None or payload.get("order_id") is None:
                raise ValueError("run_id and order_id are required")
            result, drop = provider.charge(
                payload,
                self.headers.get("X-Faultline-Fault") == "payment_response_lost_after_commit",
            )
            if drop:
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
            json_reply(self, 200, result)
        except (ValueError, json.JSONDecodeError) as exc:
            json_reply(self, 400, {"error": str(exc)})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/v1/ledger":
            json_reply(self, 404, {"error": "not found"})
            return
        run_id = parse_qs(parsed.query).get("run_id", [""])[0]
        json_reply(self, 200, {"charges": provider.ledger(run_id)})

    def log_message(self, format: str, *args: Any) -> None:
        return


class CheckoutHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/v1/checkout":
            json_reply(self, 404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            run_id = payload["run_id"]
            order_id = payload["order_id"]
            fixed = payload["implementation"] == "fixed"
            amount_cents = payload["amount_cents"]
            retries = payload["retries"]
            deadline = time.monotonic() + payload.get("timeout_seconds", 15)
            store.record_event(run_id, "CHECKOUT_STARTED", {"order_id": order_id, "implementation": payload["implementation"]})
            charge = None
            attempts = 0
            for attempt in range(retries + 1):
                if store.run_status(run_id) == "cancel_requested":
                    store.record_event(run_id, "CHECKOUT_CANCEL_OBSERVED", {"attempt": attempt + 1})
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("scenario execution deadline exceeded")
                attempts += 1
                store.record_event(run_id, "PAYMENT_ATTEMPT_STARTED", {"attempt": attempt + 1})
                request_body = json.dumps({
                    "run_id": run_id,
                    "order_id": order_id,
                    "amount_cents": amount_cents,
                    "idempotency_key": f"checkout:{order_id}" if fixed else None,
                })
                store.record_event(run_id, "PAYMENT_HTTP_REQUEST", {"method": "POST", "path": "/v1/charges", "attempt": attempt + 1,
                                                                        "body": json.loads(request_body)})
                connection = HTTPConnection("127.0.0.1", 8767, timeout=min(3, remaining))
                try:
                    headers = {"Content-Type": "application/json"}
                    if payload["fault"]:
                        headers["X-Faultline-Fault"] = payload["fault"]
                    connection.request("POST", "/v1/charges", request_body, headers)
                    response = connection.getresponse()
                    charge = json.loads(response.read())
                    store.record_event(run_id, "PAYMENT_HTTP_RESPONSE", {"status": response.status, "attempt": attempt + 1,
                                                                            "body": charge})
                    if response.status != 200:
                        raise RuntimeError(f"payment provider returned HTTP {response.status}")
                    break
                except (OSError, HTTPException, TimeoutError) as exc:
                    store.record_event(run_id, "CHECKOUT_PAYMENT_OUTCOME_UNKNOWN", {"attempt": attempt + 1, "reason": type(exc).__name__})
                    if attempt < retries:
                        if deadline - time.monotonic() <= 0:
                            raise TimeoutError("scenario execution deadline exceeded")
                        store.record_event(run_id, "CHECKOUT_RETRY_SCHEDULED", {"attempt": attempt + 1})
                finally:
                    connection.close()
            status = "paid" if charge else "payment_unknown"
            store.record_event(run_id, "CHECKOUT_REACHED_TERMINAL_STATE", {"order_id": order_id, "status": status, "attempts": attempts})
            json_reply(self, 200, {"order_id": order_id, "status": status, "attempts": attempts, "charge": charge})
        except TimeoutError as exc:
            json_reply(self, 408, {"error": str(exc)})
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            json_reply(self, 400, {"error": str(exc)})
        except Exception as exc:
            logger.exception("checkout request failed run_id=%s", payload.get("run_id", "unknown") if "payload" in locals() else "unknown")
            json_reply(self, 500, {"error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        return


def start_local_services() -> list[ThreadingHTTPServer]:
    servers: list[ThreadingHTTPServer] = []
    for port, handler in ((8767, ProviderHandler), (8766, CheckoutHandler)):
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=server.serve_forever, daemon=True, name=f"faultline-local-{port}").start()
        servers.append(server)
    return servers
