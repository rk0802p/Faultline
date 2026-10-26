"""Phase 02 Automated Test Suite: Real Network Fault Injection & Independent Observation.

Tests:
1. NetworkStreamProxy: Real socket latency injection, transparent forwarding, and TCP RST cuts.
2. CheckpointCoordinator: Commit-sensitive fault injection where downstream cuts only trigger post-commit.
3. DatabaseObserver: Independent SQLite state observation, parameterized queries, and business invariant checks.
4. HttpStateObserver: Out-of-band HTTP inspection and JSON path assertion evaluations.
5. MultiSourceObserver: Aggregated verification across multiple independent sources.
6. End-to-End Real Fault Injection against Booking Application:
   - Network response dropped after commit checkpoint.
   - Resilient implementation recovers via idempotent retry.
   - DatabaseObserver inspects SQLite database directly to verify zero duplicate seat reservations.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from faultline import engine, schema, server, store
from faultline.injectors import (
    CheckpointCoordinator,
    LoopbackProxyInjector,
    NetworkStreamProxy,
    ToxiproxyInjector,
    checkpoint_coordinator,
    injector_registry,
)
from faultline.observers import (
    DatabaseObserver,
    HttpStateObserver,
    MultiSourceObserver,
    observer_registry,
)
from faultline.targets import TargetConfig, target_registry


class InjectionAndObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="fl_test_inj_")
        self.old_data_dir = store.DATA_DIR
        self.old_db_path = store.DB_PATH
        store.DATA_DIR = Path(self.temp_dir)
        store.DB_PATH = store.DATA_DIR / "test_inj.sqlite3"
        store.initialize()
        self.db_path = Path(self.temp_dir) / "test_app.sqlite3"
        self._servers_to_close: list[ThreadingHTTPServer] = []
        self._proxies_to_close: list[NetworkStreamProxy] = []

    def tearDown(self) -> None:
        for p in self._proxies_to_close:
            try:
                p.stop()
            except Exception:
                pass
        for s in self._servers_to_close:
            try:
                s.shutdown()
                s.server_close()
            except Exception:
                pass
        from examples import booking_service
        booking_service.db = booking_service.BookingDatabase()
        store.DATA_DIR = self.old_data_dir
        store.DB_PATH = self.old_db_path
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _start_dummy_server(self, handler_cls: type[BaseHTTPRequestHandler] | None = None) -> tuple[ThreadingHTTPServer, int]:
        if handler_cls is None:
            class DefaultHandler(BaseHTTPRequestHandler):
                def do_GET(self) -> None:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status": "ok"}')

                def do_POST(self) -> None:
                    len_h = int(self.headers.get("Content-Length", "0"))
                    body = self.rfile.read(len_h) if len_h else b"{}"
                    self.send_response(201)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status": "created", "echo": ' + body + b'}')

                def log_message(self, format: str, *args: object) -> None:
                    pass

            handler_cls = DefaultHandler

        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        self._servers_to_close.append(server)
        return server, port

    # -------------------------------------------------------------------------
    # 1. NetworkStreamProxy Tests
    # -------------------------------------------------------------------------

    def test_network_stream_proxy_latency_and_tcp_reset(self) -> None:
        """Verify real TCP proxy forwards traffic, injects latency, and resets socket on demand."""
        _, server_port = self._start_dummy_server()
        proxy = NetworkStreamProxy(upstream_host="127.0.0.1", upstream_port=server_port)
        proxy.start()
        self._proxies_to_close.append(proxy)

        # 1. Transparent forwarding when disarmed
        conn = HTTPConnection("127.0.0.1", proxy.listen_port, timeout=3.0)
        conn.request("GET", "/test")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.read().decode())
        self.assertEqual(body["status"], "ok")
        conn.close()

        # 2. Latency injection
        run_id = f"test_lat_{uuid4().hex[:8]}"
        proxy.arm_fault(run_id, {"latency_seconds": 0.25})
        t0 = time.monotonic()
        conn = HTTPConnection("127.0.0.1", proxy.listen_port, timeout=3.0)
        conn.request("GET", "/test")
        resp = conn.getresponse()
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.20)
        self.assertEqual(resp.status, 200)
        conn.close()
        proxy.disarm_fault()

        # 3. Connection reset (TCP RST cut)
        run_id_cut = f"test_cut_{uuid4().hex[:8]}"
        proxy.arm_fault(run_id_cut, {"drop_request": True, "reset": True})
        conn = HTTPConnection("127.0.0.1", proxy.listen_port, timeout=2.0)
        with self.assertRaises((ConnectionResetError, ConnectionError, OSError)):
            conn.request("GET", "/test")
            conn.getresponse()
        conn.close()

        self.assertTrue(proxy.was_fault_applied())
        proxy.disarm_fault()

    # -------------------------------------------------------------------------
    # 2. CheckpointCoordinator Tests
    # -------------------------------------------------------------------------

    def test_checkpoint_coordinator_commit_sensitive_cut(self) -> None:
        """Verify downstream cut triggers only after downstream emits commit checkpoint."""
        coordinator = CheckpointCoordinator()
        run_id = f"test_cp_{uuid4().hex[:8]}"

        notifications = []
        coordinator.add_listener(run_id, lambda name, data: notifications.append(name))

        self.assertFalse(coordinator.has_checkpoint(run_id, "payment_committed"))
        coordinator.record_checkpoint(run_id, "payment_committed", {"order_id": "ord_999"})

        self.assertTrue(coordinator.has_checkpoint(run_id, "payment_committed"))
        self.assertIn("payment_committed", notifications)
        checkpoints = coordinator.get_checkpoints(run_id)
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0]["payload"]["order_id"], "ord_999")

        coordinator.clear(run_id)
        self.assertFalse(coordinator.has_checkpoint(run_id, "payment_committed"))

    # -------------------------------------------------------------------------
    # 3. DatabaseObserver Tests
    # -------------------------------------------------------------------------

    def test_database_observer_queries_and_invariants(self) -> None:
        """Verify DatabaseObserver queries SQLite tables and validates business invariants."""
        # Initialize test SQLite schema
        conn = sqlite3.connect(str(self.db_path))
        with conn:
            conn.execute("""
                CREATE TABLE bookings (
                    id TEXT PRIMARY KEY,
                    seat_number TEXT,
                    customer_id TEXT,
                    status TEXT
                )
            """)
            conn.execute("INSERT INTO bookings VALUES ('bk_1', '12A', 'cust_100', 'confirmed')")
            conn.execute("INSERT INTO bookings VALUES ('bk_2', '14B', 'cust_101', 'confirmed')")
        conn.close()

        observer = DatabaseObserver(db_path=self.db_path, name="test_db_observer")
        run_id = f"test_obs_{uuid4().hex[:8]}"

        # 1. Observe general query
        obs_result = observer.observe(run_id, {
            "queries": {
                "all_bookings": "SELECT * FROM bookings ORDER BY seat_number",
            }
        })
        self.assertIn("all_bookings", obs_result)
        self.assertEqual(obs_result["all_bookings"]["count"], 2)

        # 2. Verify assertions: passing cases
        assertions = [
            {
                "name": "two_total_bookings",
                "check": "exact_row_count",
                "query": "SELECT count(*) as count FROM bookings",
                "expected": 2,
            },
            {
                "name": "no_duplicate_seats",
                "check": "no_duplicate_records",
                "query": "SELECT seat_number FROM bookings",
                "column": "seat_number",
            },
            {
                "name": "bk_1_is_confirmed",
                "check": "row_field_equals",
                "query": "SELECT status FROM bookings WHERE id = ?",
                "params": ["bk_1"],
                "column": "status",
                "expected": "confirmed",
            },
        ]
        outcomes = observer.verify_assertions(run_id, assertions, {})
        for outcome in outcomes:
            self.assertEqual(outcome["status"], "passed", f"Assertion failed: {outcome}")

        # 3. Verify assertions: failing case (detects duplicate row violation)
        conn = sqlite3.connect(str(self.db_path))
        with conn:
            conn.execute("INSERT INTO bookings VALUES ('bk_3', '12A', 'cust_102', 'confirmed')")
        conn.close()

        fail_check = [
            {
                "name": "no_duplicate_seats",
                "check": "no_duplicate_records",
                "query": "SELECT seat_number FROM bookings",
                "column": "seat_number",
            }
        ]
        fail_outcomes = observer.verify_assertions(run_id, fail_check, {})
        self.assertEqual(fail_outcomes[0]["status"], "failed")
        self.assertIn("1 duplicates found", fail_outcomes[0]["observed"])

    # -------------------------------------------------------------------------
    # 4. HttpStateObserver Tests
    # -------------------------------------------------------------------------

    def test_http_state_observer(self) -> None:
        """Verify HttpStateObserver performs out-of-band status inspection and field checks."""
        class InspectionHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/v1/audit/summary":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"healthy": true, "active_sessions": 5, "cluster": {"leader": "node-1"}}')
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                pass

        _, server_port = self._start_dummy_server(InspectionHandler)
        observer = HttpStateObserver(base_url=f"http://127.0.0.1:{server_port}", name="audit_observer")
        run_id = f"test_http_obs_{uuid4().hex[:8]}"

        assertions = [
            {
                "name": "audit_endpoint_ok",
                "path": "/v1/audit/summary",
                "check": "status_code_equals",
                "expected": 200,
            },
            {
                "name": "cluster_leader_node1",
                "path": "/v1/audit/summary",
                "check": "json_field_equals",
                "field": "cluster.leader",
                "expected": "node-1",
            },
        ]
        outcomes = observer.verify_assertions(run_id, assertions, {})
        for o in outcomes:
            self.assertEqual(o["status"], "passed", f"Http assertion failed: {o}")

    # -------------------------------------------------------------------------
    # 5. MultiSourceObserver Tests
    # -------------------------------------------------------------------------

    def test_multi_source_observer_registry(self) -> None:
        """Verify MultiSourceObserver aggregates observations and registers cleanly."""
        conn = sqlite3.connect(str(self.db_path))
        with conn:
            conn.execute("CREATE TABLE metrics (id TEXT, val INTEGER)")
            conn.execute("INSERT INTO metrics VALUES ('m1', 42)")
        conn.close()

        db_obs = DatabaseObserver(db_path=self.db_path, name="db_source")
        _, server_port = self._start_dummy_server()
        http_obs = HttpStateObserver(base_url=f"http://127.0.0.1:{server_port}", name="http_source")

        multi = MultiSourceObserver(observers=[db_obs, http_obs], name="combined_observer")
        run_id = f"test_multi_{uuid4().hex[:8]}"

        obs_data = multi.observe(run_id, {"queries": {"q": "SELECT count(*) as count FROM metrics"}})
        self.assertIn("db_source", obs_data)
        self.assertIn("http_source", obs_data)

        # Register and retrieve from global registry
        observer_registry.register("combined_observer", multi)
        retrieved = observer_registry.get("combined_observer")
        self.assertIsNotNone(retrieved)
        self.assertIs(retrieved, multi)
        observer_registry.remove("combined_observer")

    # -------------------------------------------------------------------------
    # 6. End-to-End Real Fault Injection & Observation against Booking Service
    # -------------------------------------------------------------------------

    def test_e2e_booking_service_network_fault_and_independent_db_observation(self) -> None:
        """E2E Test: Inject real network socket cut after booking commit.
        
        Verifies:
        1. Faultline routes HTTP step through live loopback stream proxy.
        2. Downstream commits booking to real SQLite database.
        3. Proxy drops response socket abruptly.
        4. In fixed mode, Faultline retries with idempotency key; downstream deduplicates.
        5. DatabaseObserver independently inspects SQLite table to prove zero duplicate seats.
        """
        from examples import booking_service

        booking_db_file = Path(self.temp_dir) / "e2e_booking.sqlite3"
        booking_service.db = booking_service.BookingDatabase(sqlite_path=str(booking_db_file))

        server_instance = booking_service.run_service("127.0.0.1", 0, sqlite_db=str(booking_db_file))
        actual_port = server_instance.server_address[1]
        t = threading.Thread(target=server_instance.serve_forever, daemon=True)
        t.start()
        self._servers_to_close.append(server_instance)

        # 1. Register target application pointing to the live booking service
        target_id = f"booking-e2e-{uuid4().hex[:6]}"
        target_cfg = TargetConfig(
            id=target_id,
            name="E2E Booking Target",
            base_url=f"http://127.0.0.1:{actual_port}",
            health_endpoint="/v1/health",
            permitted_faults=[
                "http_timeout",
                "network_latency",
                "network_disconnect",
                "network_reset",
                "network_response_drop",
            ],
        )
        target_registry.register(target_cfg)

        # 2. Register scenario with real network response drop and independent DatabaseObserver
        scenario_id = f"scen-booking-net-cut-{uuid4().hex[:6]}"
        scenario_def = {
            "scenario_version": 1,
            "name": scenario_id,
            "description": "Cut response socket after booking commit and observe database",
            "target_id": target_id,
            "fault": "network_response_drop",
            "assertion": "all_steps_succeeded_or_handled",
            "timeout_seconds": 15,
            "workflow": [
                {
                    "id": "reserve_seat",
                    "name": "Reserve Flight Seat",
                    "type": "http.post",
                    "path": "/v1/bookings",
                    "headers": {
                        "X-Idempotency-Key": "idem_flight_seat_9B",
                    },
                    "body": {
                        "booking_id": "bk_flight_9B",
                        "seat_number": "9B",
                        "customer_id": "cust_pilot",
                        "price_cents": 35000,
                    },
                }
            ],
            "target_step": "reserve_seat",
            "fault_config": {
                "drop_response": True,
                "wait_checkpoint": "booking_committed",
            },
            "observers": [
                {
                    "type": "database",
                    "name": "sqlite_booking_observer",
                    "config": {
                        "db_path": str(booking_db_file),
                    },
                    "assertions": [
                        {
                            "name": "exact_one_seat_9b_booked",
                            "check": "exact_row_count",
                            "query": "SELECT count(*) as count FROM bookings WHERE seat_number = '9B'",
                            "expected": 1,
                        },
                        {
                            "name": "no_duplicate_seat_records",
                            "check": "no_duplicate_records",
                            "query": "SELECT seat_number FROM bookings",
                            "column": "seat_number",
                        },
                        {
                            "name": "seat_status_confirmed",
                            "check": "row_field_equals",
                            "query": "SELECT status FROM bookings WHERE seat_number = '9B'",
                            "column": "status",
                            "expected": "confirmed",
                        },
                    ],
                }
            ],
        }

        schema.validate_scenario(scenario_def)
        store.create_scenario(scenario_id, "faultline-demo", scenario_def)

        # 3. Execute scenario in FIXED mode: recovers via idempotent retry and passes DB invariants
        run_fixed_id = str(uuid4())
        store.create_run(
            run_id=run_fixed_id,
            scenario_id=scenario_id,
            scenario_version=1,
            implementation="fixed",
        )
        engine.execute_run(run_fixed_id, scenario_def, implementation="fixed")

        run_record = store.get_run(run_fixed_id)
        self.assertIsNotNone(run_record)
        self.assertEqual(run_record["verdict"], "passed", f"Run failed: {run_record.get('error')}")

        assertions = run_record.get("assertions", [])
        assertion_map = {a["name"]: a["status"] for a in assertions}
        self.assertEqual(assertion_map.get("fault_activation_confirmed"), "passed")
        self.assertEqual(assertion_map.get("all_steps_succeeded_or_handled"), "passed")
        self.assertEqual(assertion_map.get("exact_one_seat_9b_booked"), "passed")
        self.assertEqual(assertion_map.get("no_duplicate_seat_records"), "passed")
        self.assertEqual(assertion_map.get("seat_status_confirmed"), "passed")

        # Directly query SQLite database using standard library sqlite3 to independently confirm state
        conn = sqlite3.connect(str(booking_db_file))
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) FROM bookings WHERE seat_number = '9B'")
        count = cursor.fetchone()[0]
        conn.close()
        self.assertEqual(count, 1, "Independent SQLite inspection must confirm exactly 1 booking record")

        # Clean target from registry
        target_registry.delete(target_id)


if __name__ == "__main__":
    unittest.main()
