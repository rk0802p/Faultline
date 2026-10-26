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
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("booking_service")


class BookingDatabase:
    """In-memory or SQLite-backed business state for the booking application."""

    def __init__(self, sqlite_path: str | None = None) -> None:
        self.sqlite_path = sqlite_path
        self.bookings: dict[str, dict] = {}
        self.claimed_seats: set[str] = set()
        self.attempts: dict[str, int] = {}
        if sqlite_path:
            self.init_sqlite(sqlite_path)

    def init_sqlite(self, path: str) -> None:
        self.sqlite_path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=5.0)
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bookings (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT,
                    seat_number TEXT,
                    price_cents INTEGER,
                    status TEXT,
                    idempotency_key TEXT,
                    attempts INTEGER,
                    created_at TEXT
                )
            """)
        conn.close()

    def record_booking(self, booking: dict) -> None:
        self.bookings[booking["id"]] = booking
        self.claimed_seats.add(booking["seat_number"])
        if self.sqlite_path:
            try:
                Path(self.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(self.sqlite_path, timeout=5.0)
                with conn:
                    conn.execute("""
                        INSERT OR REPLACE INTO bookings (id, customer_id, seat_number, price_cents, status, idempotency_key, attempts, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    """, (
                        booking["id"],
                        booking.get("customer_id", ""),
                        booking.get("seat_number", ""),
                        booking.get("price_cents", 0),
                        booking.get("status", "confirmed"),
                        booking.get("idempotency_key", ""),
                        booking.get("attempts", 1),
                    ))
                conn.close()
            except Exception as exc:
                logger.debug("SQLite record error: %s", exc)

    def find_by_idempotency_key(self, idem_key: str) -> dict | None:
        if not idem_key:
            return None
        if self.sqlite_path:
            conn = sqlite3.connect(self.sqlite_path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM bookings WHERE idempotency_key = ?", (idem_key,))
            row = cursor.fetchone()
            conn.close()
            if row:
                return dict(row)
        for b in self.bookings.values():
            if b.get("idempotency_key") == idem_key:
                return b
        return None

    def reset(self) -> None:
        self.bookings.clear()
        self.claimed_seats.clear()
        self.attempts.clear()
        if self.sqlite_path and Path(self.sqlite_path).exists():
            conn = sqlite3.connect(self.sqlite_path, timeout=5.0)
            with conn:
                conn.execute("DELETE FROM bookings")
            conn.close()


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
            if not booking and db.sqlite_path:
                conn = sqlite3.connect(db.sqlite_path, timeout=5.0)
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                c.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
                row = c.fetchone()
                conn.close()
                if row:
                    booking = dict(row)
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
        idempotency_key = self.headers.get("X-Idempotency-Key") or payload.get("idempotency_key")
        run_id = self.headers.get("X-Faultline-Run-Id") or payload.get("run_id")

        # Check for fault injection headers passed by Faultline
        fault_header = self.headers.get("X-Faultline-Fault")
        delay_header = self.headers.get("X-Faultline-Delay")
        implementation = self.headers.get("X-Implementation", payload.get("implementation", "vulnerable"))

        db.attempts[booking_id] = db.attempts.get(booking_id, 0) + 1
        current_attempt = db.attempts[booking_id]

        # Resilient (fixed) implementation deduplicates idempotent retries
        if implementation == "fixed" and idempotency_key:
            existing = db.find_by_idempotency_key(idempotency_key)
            if existing:
                self.respond(200, {
                    "status": "confirmed",
                    "booking": existing,
                    "deduplicated": True,
                })
                return

        if fault_header == "http_timeout" or payload.get("simulate_timeout"):
            delay = float(delay_header) if delay_header else 0.5
            time.sleep(delay)

            # In vulnerable mode, unhandled timeout fails the downstream call
            if implementation == "vulnerable":
                fail_record = {
                    "id": booking_id,
                    "customer_id": customer_id,
                    "seat_number": seat_number,
                    "price_cents": price_cents,
                    "status": "failed",
                    "error": "downstream inventory service timed out",
                    "attempts": current_attempt,
                }
                db.record_booking(fail_record)
                self.respond(504, {
                    "error": "Gateway Timeout: Downstream inventory service did not respond",
                    "booking_id": booking_id,
                    "attempts": current_attempt,
                })
                return
            else:
                # In fixed mode, if this was the first attempt under fault, retry succeeds and confirms
                conf_record = {
                    "id": booking_id,
                    "customer_id": customer_id,
                    "seat_number": seat_number,
                    "price_cents": price_cents,
                    "status": "confirmed",
                    "idempotency_key": idempotency_key or "",
                    "attempts": current_attempt,
                    "recovery": "recovered_with_retry",
                }
                db.record_booking(conf_record)
                self.respond(200, {
                    "status": "confirmed",
                    "booking": conf_record,
                })
                return

        # Normal successful booking path
        booking_record = {
            "id": booking_id,
            "customer_id": customer_id,
            "seat_number": seat_number,
            "price_cents": price_cents,
            "status": "confirmed",
            "idempotency_key": idempotency_key or "",
            "attempts": current_attempt,
        }
        db.record_booking(booking_record)

        # Notify commit checkpoint if coordinator is active
        if run_id:
            try:
                from faultline.injectors import checkpoint_coordinator
                checkpoint_coordinator.record_checkpoint(run_id, "booking_committed", {"booking_id": booking_id})
            except Exception:
                pass

        self.respond(201, {
            "status": "confirmed",
            "booking": booking_record,
        })

    def log_message(self, format: str, *args: object) -> None:
        # Suppress noisy request logs in test output unless debug enabled
        pass


def run_service(host: str = "127.0.0.1", port: int = 8990, sqlite_db: str | None = None) -> ThreadingHTTPServer:
    global db
    db = BookingDatabase(sqlite_path=sqlite_db)
    server = ThreadingHTTPServer((host, port), BookingServiceHandler)
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Independent Booking Service for Faultline Phase 01/02 testing")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8990)
    parser.add_argument("--db", default=None, help="Optional SQLite database file path for persistence")
    args = parser.parse_args()

    server = run_service(args.host, args.port, args.db)
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
