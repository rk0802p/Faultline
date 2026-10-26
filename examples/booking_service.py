"""Independent Target Application: Flight & Concert Seat Booking Service.

This service is an independent application used to validate that Faultline can test
arbitrary external services without hardcoding or domain assumptions in Faultline's core.

Exposes:
- GET  /v1/health          - Health check endpoint
- POST /v1/bookings        - Create a reservation (calls downstream inventory)
- GET  /v1/bookings/:id    - Inspect booking record and business state
- DELETE /v1/bookings      - Reset / clean test state
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

logger = logging.getLogger("booking_service")


class BookingDatabase:
    """In-memory business state for the booking application."""

    def __init__(self) -> None:
        self.bookings: dict[str, dict] = {}
        self.claimed_seats: set[str] = set()
        self.attempts: dict[str, int] = {}

    def reset(self) -> None:
        self.bookings.clear()
        self.claimed_seats.clear()
        self.attempts.clear()


db = BookingDatabase()


class BookingServiceHandler(BaseHTTPRequestHandler):
    server_version = "BookingService/1.0"

    def respond(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/v1/health":
            self.respond(200, {
                "status": "ok",
                "service": "booking-service",
                "bookings_count": len(db.bookings),
                "claimed_seats_count": len(db.claimed_seats),
            })
            return

        if path.startswith("/v1/bookings/"):
            booking_id = path.removeprefix("/v1/bookings/")
            booking = db.bookings.get(booking_id)
            if booking:
                self.respond(200, {"booking": booking})
            else:
                self.respond(404, {"error": "booking not found"})
            return

        self.respond(404, {"error": "endpoint not found"})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/v1/bookings":
            db.reset()
            self.respond(200, {"status": "cleared"})
            return
        self.respond(404, {"error": "endpoint not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path != "/v1/bookings":
            self.respond(404, {"error": "endpoint not found"})
            return

        # Read JSON body
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            self.respond(400, {"error": "invalid json"})
            return

        booking_id = payload.get("booking_id") or f"bk_{int(time.time() * 1000)}"
        seat_number = payload.get("seat_number", "12A")
        customer_id = payload.get("customer_id", "cust_101")
        price_cents = int(payload.get("price_cents", 15000))

        # Check for fault injection header passed by Faultline
        fault_header = self.headers.get("X-Faultline-Fault")
        delay_header = self.headers.get("X-Faultline-Delay")
        implementation = self.headers.get("X-Implementation", payload.get("implementation", "vulnerable"))

        db.attempts[booking_id] = db.attempts.get(booking_id, 0) + 1
        current_attempt = db.attempts[booking_id]

        if fault_header == "http_timeout" or payload.get("simulate_timeout"):
            delay = float(delay_header) if delay_header else 0.5
            time.sleep(delay)

            # In vulnerable mode, unhandled timeout fails the downstream call
            if implementation == "vulnerable":
                db.bookings[booking_id] = {
                    "id": booking_id,
                    "customer_id": customer_id,
                    "seat_number": seat_number,
                    "price_cents": price_cents,
                    "status": "failed",
                    "error": "downstream inventory service timed out",
                    "attempts": current_attempt,
                }
                self.respond(504, {
                    "error": "Gateway Timeout: Downstream inventory service did not respond",
                    "booking_id": booking_id,
                    "attempts": current_attempt,
                })
                return
            else:
                # In fixed mode, if this was the first attempt under fault, retry succeeds and confirms
                db.claimed_seats.add(seat_number)
                db.bookings[booking_id] = {
                    "id": booking_id,
                    "customer_id": customer_id,
                    "seat_number": seat_number,
                    "price_cents": price_cents,
                    "status": "confirmed",
                    "attempts": current_attempt,
                    "recovery": "recovered_with_retry",
                }
                self.respond(200, {
                    "status": "confirmed",
                    "booking": db.bookings[booking_id],
                })
                return

        # Normal successful booking path
        db.claimed_seats.add(seat_number)
        db.bookings[booking_id] = {
            "id": booking_id,
            "customer_id": customer_id,
            "seat_number": seat_number,
            "price_cents": price_cents,
            "status": "confirmed",
            "attempts": current_attempt,
        }
        self.respond(201, {
            "status": "confirmed",
            "booking": db.bookings[booking_id],
        })

    def log_message(self, format: str, *args: object) -> None:
        # Suppress noisy request logs in test output unless debug enabled
        pass


def run_service(host: str = "127.0.0.1", port: int = 8990) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), BookingServiceHandler)
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Independent Booking Service for Faultline Phase 01 testing")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8990)
    args = parser.parse_args()

    server = run_service(args.host, args.port)
    actual_port = server.server_address[1]
    print(f"Booking Service running at http://{args.host}:{actual_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Booking Service...")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
