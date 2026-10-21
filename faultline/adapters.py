"""Local sample checkout and payment-provider HTTP services.

The only fault adapter is deliberately constrained to loopback and a known test
provider. It commits a charge, then closes the connection before the response.
"""

import json
import logging
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPConnection, HTTPException
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from . import store


logger = logging.getLogger("faultline.adapter.http")
CHECKOUT_HOST = os.environ.get("CHECKOUT_HOST", "127.0.0.1")
CHECKOUT_PORT = int(os.environ.get("CHECKOUT_PORT", 8766))
PAYMENT_HOST = os.environ.get("PAYMENT_HOST", "127.0.0.1")
PAYMENT_PORT = int(os.environ.get("PAYMENT_PORT", 8767))


class ToxiproxyClient:
    """Helper client to inspect and configure Shopify Toxiproxy proxies and toxics (Spec §12)."""

    def __init__(self, base_url: str = "http://127.0.0.1:8474") -> None:
        self.base_url = os.environ.get("TOXIPROXY_URL", base_url).rstrip("/")

    def is_available(self) -> bool:
        try:
            req = Request(f"{self.base_url}/version", method="GET")
            with urlopen(req, timeout=1) as resp:
                return resp.status == 200
        except Exception:
            return False

    def list_proxies(self) -> dict[str, Any]:
        try:
            req = Request(f"{self.base_url}/proxies", method="GET")
            with urlopen(req, timeout=2) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.debug("Toxiproxy not reachable at %s: %s", self.base_url, exc)
            return {}

    def add_toxic(
        self,
        proxy_name: str,
        toxic_type: str,
        stream: str = "upstream",
        toxicity: float = 1.0,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "name": f"{toxic_type}_{int(time.time())}",
            "type": toxic_type,
            "stream": stream,
            "toxicity": toxicity,
            "attributes": attributes or {},
        }
        data = json.dumps(payload).encode("utf-8")
        req = Request(f"{self.base_url}/proxies/{proxy_name}/toxics", data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=2) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.warning("Failed to configure toxic on %s: %s", proxy_name, exc)
            return {"error": str(exc)}


toxiproxy = ToxiproxyClient()


class TestPaymentProvider:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.charges: dict[str, list[dict[str, Any]]] = {}
        self.refunds: dict[str, list[dict[str, Any]]] = {}
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

    def refund(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = payload["run_id"]
        charge_id = payload["charge_id"]
        with self.lock:
            refund_id = f"ref_{len(self.refunds.get(run_id, [])) + 1}"
            result = {
                "id": refund_id,
                "charge_id": charge_id,
                "amount_cents": payload.get("amount_cents", 0),
                "status": "refunded",
            }
            self.refunds.setdefault(run_id, []).append(result)
            logger.debug("refund committed run_id=%s refund_id=%s charge_id=%s", run_id, refund_id, charge_id)
            store.record_event(run_id, "PROVIDER_REFUND_COMMITTED", {
                "refund_id": refund_id,
                "charge_id": charge_id,
                "amount_cents": result["amount_cents"],
            })
            return result

    def ledger(self, run_id: str) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.charges.get(run_id, []))

    def refund_ledger(self, run_id: str) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.refunds.get(run_id, []))

    def cleanup(self, run_id: str) -> None:
        with self.lock:
            self.charges.pop(run_id, None)
            self.refunds.pop(run_id, None)
            self.faulted_runs.discard(run_id)
            for key in [key for key in self.idempotency if key[0] == run_id]:
                self.idempotency.pop(key, None)


provider = TestPaymentProvider()


class TestCheckoutService:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.fulfillments: dict[str, list[dict[str, Any]]] = {}
        self.processed_events: dict[tuple[str, str], dict[str, Any]] = {}

    def process_webhook(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        run_id = payload["run_id"]
        event_id = payload.get("event_id", "")
        order_id = payload["order_id"]
        fixed = payload.get("implementation") == "fixed"

        with self.lock:
            if fixed and (run_id, event_id) in self.processed_events:
                existing = self.processed_events[(run_id, event_id)]
                logger.debug("duplicate webhook ignored run_id=%s event_id=%s", run_id, event_id)
                store.record_event(run_id, "WEBHOOK_DUPLICATE_IGNORED", {
                    "event_id": event_id,
                    "order_id": order_id,
                    "fulfillment_id": existing["id"],
                })
                return {
                    "status": "already_processed",
                    "fulfillment_id": existing["id"],
                    "order_id": order_id,
                    "event_id": event_id,
                }, True

            fulfillment = {
                "id": f"ful_{len(self.fulfillments.get(run_id, [])) + 1}",
                "order_id": order_id,
                "event_id": event_id,
                "amount_cents": payload.get("amount_cents", 0),
                "status": "fulfilled",
            }
            self.fulfillments.setdefault(run_id, []).append(fulfillment)
            if fixed and event_id:
                self.processed_events[(run_id, event_id)] = fulfillment

            store.record_event(run_id, "ORDER_FULFILLMENT_COMMITTED", {
                "fulfillment_id": fulfillment["id"],
                "order_id": order_id,
                "event_id": event_id,
            })
            return {
                "status": "fulfilled",
                "fulfillment_id": fulfillment["id"],
                "order_id": order_id,
                "event_id": event_id,
            }, False

    def ledger(self, run_id: str) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.fulfillments.get(run_id, []))

    def cleanup(self, run_id: str) -> None:
        with self.lock:
            self.fulfillments.pop(run_id, None)
            for key in [k for k in self.processed_events if k[0] == run_id]:
                self.processed_events.pop(key, None)


checkout_service = TestCheckoutService()


def json_reply(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class ProviderHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path == "/v1/refunds":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
                if not payload.get("run_id") or not payload.get("charge_id"):
                    raise ValueError("run_id and charge_id are required")
                result = provider.refund(payload)
                json_reply(self, 200, result)
            except (ValueError, json.JSONDecodeError) as exc:
                json_reply(self, 400, {"error": str(exc)})
            return

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
        if parsed.path in {"/v1/health", "/health"}:
            json_reply(self, 200, {"status": "ok", "service": "payment-provider"})
            return
        if parsed.path == "/v1/refunds":
            run_id = parse_qs(parsed.query).get("run_id", [""])[0]
            json_reply(self, 200, {"refunds": provider.refund_ledger(run_id)})
            return
        if parsed.path != "/v1/ledger":
            json_reply(self, 404, {"error": "not found"})
            return
        run_id = parse_qs(parsed.query).get("run_id", [""])[0]
        json_reply(self, 200, {"charges": provider.ledger(run_id)})

    def log_message(self, format: str, *args: Any) -> None:
        return


class CheckoutHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path == "/v1/webhooks/payment":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
                run_id = payload.get("run_id")
                order_id = payload.get("order_id")
                if not run_id or not order_id:
                    raise ValueError("run_id and order_id are required")
                result, _ = checkout_service.process_webhook(payload)
                json_reply(self, 200, result)
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                json_reply(self, 400, {"error": str(exc)})
            except Exception as exc:
                logger.exception("webhook processing failed")
                json_reply(self, 500, {"error": str(exc)})
            return

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
                connection = HTTPConnection(PAYMENT_HOST, PAYMENT_PORT, timeout=min(3, remaining))
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

            if charge and payload.get("fault") == "persistence_failure_after_payment":
                store.record_event(run_id, "ORDER_PERSISTENCE_STARTED", {"order_id": order_id})
                store.record_event(run_id, "FAULT_PERSISTENCE_FAILED", {
                    "adapter": "local-db",
                    "stage": "order_persistence",
                    "order_id": order_id,
                })
                if fixed:
                    store.record_event(run_id, "COMPENSATION_INITIATED", {
                        "order_id": order_id,
                        "charge_id": charge["id"],
                    })
                    refund_conn = HTTPConnection(PAYMENT_HOST, PAYMENT_PORT, timeout=min(3, max(1, remaining)))
                    try:
                        refund_body = json.dumps({
                            "run_id": run_id,
                            "charge_id": charge["id"],
                            "amount_cents": amount_cents,
                        })
                        refund_conn.request("POST", "/v1/refunds", refund_body, {"Content-Type": "application/json"})
                        refund_resp = refund_conn.getresponse()
                        refund_result = json.loads(refund_resp.read())
                        store.record_event(run_id, "COMPENSATION_COMPLETED", {
                            "order_id": order_id,
                            "charge_id": charge["id"],
                            "refund_id": refund_result["id"],
                        })
                    finally:
                        refund_conn.close()
                    status = "cancelled_and_refunded"
                    store.record_event(run_id, "CHECKOUT_REACHED_TERMINAL_STATE", {
                        "order_id": order_id,
                        "status": status,
                        "attempts": attempts,
                    })
                    json_reply(self, 200, {
                        "order_id": order_id,
                        "status": status,
                        "attempts": attempts,
                        "charge": charge,
                        "compensated": True,
                    })
                    return
                else:
                    status = "persistence_failed"
                    store.record_event(run_id, "CHECKOUT_REACHED_TERMINAL_STATE", {
                        "order_id": order_id,
                        "status": status,
                        "attempts": attempts,
                    })
                    json_reply(self, 200, {
                        "order_id": order_id,
                        "status": status,
                        "attempts": attempts,
                        "charge": charge,
                        "compensated": False,
                    })
                    return

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

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/v1/health", "/health"}:
            json_reply(self, 200, {"status": "ok", "service": "checkout-service"})
            return
        if parsed.path == "/v1/fulfillments":
            run_id = parse_qs(parsed.query).get("run_id", [""])[0]
            json_reply(self, 200, {"fulfillments": checkout_service.ledger(run_id)})
            return
        json_reply(self, 404, {"error": "not found"})

    def log_message(self, format: str, *args: Any) -> None:
        return


def start_local_services(
    *,
    checkout_port: int = 8766,
    payment_port: int = 8767,
    checkout_host: str = "127.0.0.1",
    payment_host: str = "127.0.0.1",
) -> list[ThreadingHTTPServer]:
    global CHECKOUT_PORT, PAYMENT_PORT, CHECKOUT_HOST, PAYMENT_HOST
    servers: list[ThreadingHTTPServer] = []
    for host, port, handler in ((payment_host, payment_port, ProviderHandler), (checkout_host, checkout_port, CheckoutHandler)):
        server = ThreadingHTTPServer((host, port), handler)
        servers.append(server)
    PAYMENT_HOST = servers[0].server_address[0]
    PAYMENT_PORT = servers[0].server_address[1]
    CHECKOUT_HOST = servers[1].server_address[0]
    CHECKOUT_PORT = servers[1].server_address[1]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True,
                         name=f"faultline-local-{server.server_address[1]}").start()
    return servers


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Start Faultline checkout or payment microservices")
    parser.add_argument("--service", choices=["all", "checkout", "payment"], default="all")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--checkout-port", type=int, default=8766)
    parser.add_argument("--payment-port", type=int, default=8767)
    args = parser.parse_args()

    servers: list[ThreadingHTTPServer] = []
    if args.service in {"all", "payment"}:
        p_server = ThreadingHTTPServer((args.host, args.payment_port), ProviderHandler)
        servers.append(p_server)
        print(f"Payment provider running on {args.host}:{args.payment_port}")
    if args.service in {"all", "checkout"}:
        c_server = ThreadingHTTPServer((args.host, args.checkout_port), CheckoutHandler)
        servers.append(c_server)
        print(f"Checkout service running on {args.host}:{args.checkout_port}")

    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        for s in servers:
            s.shutdown()
            s.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
