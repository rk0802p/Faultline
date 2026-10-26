"""Phase 03 Automated Test Suite: Declarative Assertion Engine & Invariant Verification.

Tests:
1. Operator comparison engine across scalar, numeric, collection, and duplicate checks.
2. Database invariant evaluation with parameterized queries and column extractions.
3. HTTP response invariant evaluation with status codes, latency thresholds, and JSON paths.
4. Cross-system reconciliation comparing state across independent tables or databases.
5. Idempotency invariant evaluation verifying single-mutation guarantees.
6. End-to-end scenario execution with declarative assertions declared in scenario schema.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from faultline import assertions, engine, schema, store
from faultline.assertions import DeclarativeAssertionEngine, compare
from faultline.targets import TargetConfig, target_registry


class DeclarativeAssertionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="fl_test_assert_")
        self.old_data_dir = store.DATA_DIR
        self.old_db_path = store.DB_PATH
        store.DATA_DIR = Path(self.temp_dir)
        store.DB_PATH = store.DATA_DIR / "test_assert.sqlite3"
        store.initialize()

        self.db_path = Path(self.temp_dir) / "biz_data.sqlite3"
        self._servers_to_close: list[ThreadingHTTPServer] = []

    def tearDown(self) -> None:
        for s in self._servers_to_close:
            try:
                s.shutdown()
                s.server_close()
            except Exception:
                pass
        store.DATA_DIR = self.old_data_dir
        store.DB_PATH = self.old_db_path
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Comparison Operator Tests
    # -------------------------------------------------------------------------

    def test_comparison_operators(self) -> None:
        """Verify operator comparisons handle numbers, strings, lists, ranges, and duplicates."""
        # Equality / inequality
        p, _, _ = compare("equals", 42, 42)
        self.assertTrue(p)
        p, _, _ = compare("not_equals", "apple", "banana")
        self.assertTrue(p)

        # Ordering
        p, _, _ = compare("greater_than", 100, 50)
        self.assertTrue(p)
        p, _, _ = compare("less_than", 25, 50)
        self.assertTrue(p)
        p, _, _ = compare("gte", 50, 50)
        self.assertTrue(p)
        p, _, _ = compare("lte", 50, 50)
        self.assertTrue(p)

        # In / contains
        p, _, _ = compare("contains", ["admin", "editor"], "editor")
        self.assertTrue(p)
        p, _, _ = compare("not_in", ["active", "pending"], "deleted")
        self.assertTrue(p)

        # Between
        p, _, _ = compare("between", 50, [10, 100])
        self.assertTrue(p)
        p, _, _ = compare("between", 150, [10, 100])
        self.assertFalse(p)

        # Duplicate detection
        p, _, _ = compare("no_duplicates", ["a", "b", "c"], None)
        self.assertTrue(p)
        p, _, obs = compare("no_duplicates", ["a", "b", "a"], None)
        self.assertFalse(p)
        self.assertIn("1 duplicates found", obs)

        # Emptiness
        p, _, _ = compare("is_empty", [], None)
        self.assertTrue(p)
        p, _, _ = compare("is_not_empty", [1], None)
        self.assertTrue(p)

    # -------------------------------------------------------------------------
    # 2. Database Invariant Evaluator Tests
    # -------------------------------------------------------------------------

    def test_database_invariant_evaluator(self) -> None:
        """Verify DatabaseInvariantEvaluator executes parameterized SQL and evaluates assertions."""
        conn = sqlite3.connect(str(self.db_path))
        with conn:
            conn.execute("""
                CREATE TABLE orders (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT,
                    amount_cents INTEGER,
                    status TEXT
                )
            """)
            conn.execute("INSERT INTO orders VALUES ('ord_1', 'cust_A', 5000, 'CONFIRMED')")
            conn.execute("INSERT INTO orders VALUES ('ord_2', 'cust_B', 7500, 'CONFIRMED')")
        conn.close()

        evaluator = DeclarativeAssertionEngine()

        # Passing count assertion
        res = evaluator.evaluate_database_assertion({
            "name": "two_confirmed_orders",
            "db_path": str(self.db_path),
            "query": "SELECT count(*) FROM orders WHERE status = 'CONFIRMED'",
            "op": "equals",
            "expected": 2,
        }, {})
        self.assertEqual(res["status"], "passed")

        # Passing field assertion with parameter
        res_field = evaluator.evaluate_database_assertion({
            "name": "order_1_confirmed",
            "db_path": str(self.db_path),
            "query": "SELECT status FROM orders WHERE id = ?",
            "params": ["{order_id}"],
            "op": "equals",
            "expected": "CONFIRMED",
        }, {"order_id": "ord_1"})
        self.assertEqual(res_field["status"], "passed")

        # Failing assertion: expected 5 orders, got 2
        res_fail = evaluator.evaluate_database_assertion({
            "name": "five_orders_expected",
            "db_path": str(self.db_path),
            "query": "SELECT count(*) FROM orders",
            "op": "equals",
            "expected": 5,
        }, {})
        self.assertEqual(res_fail["status"], "failed")
        self.assertIn("2", res_fail["observed"])

    # -------------------------------------------------------------------------
    # 3. HTTP Response Invariant Evaluator Tests
    # -------------------------------------------------------------------------

    def test_http_response_invariant_evaluator(self) -> None:
        """Verify HttpResponseInvariantEvaluator checks status, latency, and nested JSON paths."""
        evaluator = DeclarativeAssertionEngine()

        mock_context = {
            "side_effects": [
                {
                    "step_id": "create_booking",
                    "status": 201,
                    "elapsed_seconds": 0.125,
                    "json_data": {
                        "booking": {
                            "id": "bk_999",
                            "status": "confirmed",
                            "seats": ["12A", "12B"],
                        },
                        "service": "booking-service",
                    },
                }
            ]
        }

        # Status code pass
        res_status = evaluator.evaluate_http_assertion({
            "name": "created_status",
            "step_id": "create_booking",
            "status_code": 201,
        }, mock_context)
        self.assertEqual(res_status["status"], "passed")

        # Latency bound pass
        res_lat = evaluator.evaluate_http_assertion({
            "name": "fast_latency",
            "step_id": "create_booking",
            "max_latency_seconds": 1.0,
        }, mock_context)
        self.assertEqual(res_lat["status"], "passed")

        # JSON path nested field pass
        res_json = evaluator.evaluate_http_assertion({
            "name": "confirmed_status_in_json",
            "step_id": "create_booking",
            "json_path": "booking.status",
            "op": "equals",
            "expected": "confirmed",
        }, mock_context)
        self.assertEqual(res_json["status"], "passed")

        # JSON path nested array pass
        res_arr = evaluator.evaluate_http_assertion({
            "name": "seat_12a_booked",
            "step_id": "create_booking",
            "json_path": "booking.seats",
            "op": "contains",
            "expected": "12A",
        }, mock_context)
        self.assertEqual(res_arr["status"], "passed")

        # Latency exceeded failure
        res_lat_fail = evaluator.evaluate_http_assertion({
            "name": "impossible_latency",
            "step_id": "create_booking",
            "max_latency_seconds": 0.05,
        }, mock_context)
        self.assertEqual(res_lat_fail["status"], "failed")

    # -------------------------------------------------------------------------
    # 4. Reconciliation Invariant Evaluator Tests
    # -------------------------------------------------------------------------

    def test_reconciliation_invariant_evaluator(self) -> None:
        """Verify cross-table balance and ledger reconciliation."""
        conn = sqlite3.connect(str(self.db_path))
        with conn:
            conn.execute("CREATE TABLE orders (id TEXT, status TEXT)")
            conn.execute("CREATE TABLE ledger (order_id TEXT, captured INTEGER)")
            conn.execute("INSERT INTO orders VALUES ('o1', 'PAID'), ('o2', 'PAID')")
            conn.execute("INSERT INTO ledger VALUES ('o1', 1), ('o2', 1)")
        conn.close()

        evaluator = DeclarativeAssertionEngine()

        # Balanced reconciliation
        res_bal = evaluator.evaluate_reconciliation_assertion({
            "name": "paid_orders_match_ledger_records",
            "db_path": str(self.db_path),
            "left_query": "SELECT count(*) FROM orders WHERE status = 'PAID'",
            "right_query": "SELECT count(*) FROM ledger WHERE captured = 1",
            "op": "equals",
        }, {})
        self.assertEqual(res_bal["status"], "passed")

        # Add uncaptured order -> discrepancy failure
        conn = sqlite3.connect(str(self.db_path))
        with conn:
            conn.execute("INSERT INTO orders VALUES ('o3', 'PAID')")
        conn.close()

        res_disc = evaluator.evaluate_reconciliation_assertion({
            "name": "paid_orders_match_ledger_records",
            "db_path": str(self.db_path),
            "left_query": "SELECT count(*) FROM orders WHERE status = 'PAID'",
            "right_query": "SELECT count(*) FROM ledger WHERE captured = 1",
            "op": "equals",
        }, {})
        self.assertEqual(res_disc["status"], "failed")
        self.assertIn("left=3, right=2", res_disc["observed"])

    # -------------------------------------------------------------------------
    # 5. Idempotency Invariant Evaluator Tests
    # -------------------------------------------------------------------------

    def test_idempotency_invariant_evaluator(self) -> None:
        """Verify IdempotencyInvariantEvaluator validates single-mutation guarantees."""
        conn = sqlite3.connect(str(self.db_path))
        with conn:
            conn.execute("CREATE TABLE idempotency_records (key TEXT, val TEXT)")
            conn.execute("INSERT INTO idempotency_records VALUES ('key_123', 'done')")
        conn.close()

        evaluator = DeclarativeAssertionEngine()

        # Passes when exactly 1 record exists
        res = evaluator.evaluate_idempotency_assertion({
            "name": "key_123_is_idempotent",
            "db_path": str(self.db_path),
            "table": "idempotency_records",
            "key_column": "key",
            "key_value": "key_123",
            "expected": 1,
        }, {})
        self.assertEqual(res["status"], "passed")

        # Fails when duplicate record is inserted
        conn = sqlite3.connect(str(self.db_path))
        with conn:
            conn.execute("INSERT INTO idempotency_records VALUES ('key_123', 'duplicate')")
        conn.close()

        res_dup = evaluator.evaluate_idempotency_assertion({
            "name": "key_123_is_idempotent",
            "db_path": str(self.db_path),
            "table": "idempotency_records",
            "key_column": "key",
            "key_value": "key_123",
            "expected": 1,
        }, {})
        self.assertEqual(res_dup["status"], "failed")
        self.assertIn("2 records found", res_dup["observed"])

    # -------------------------------------------------------------------------
    # 6. End-to-End Scenario with Declarative Assertions
    # -------------------------------------------------------------------------

    def test_e2e_scenario_with_declarative_assertions(self) -> None:
        """E2E Test: Run a scenario declaring declarative database and HTTP assertions."""
        class MockBookingHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')

            def do_POST(self) -> None:
                if self.headers.get("X-Faultline-Fault") == "http_timeout":
                    delay = float(self.headers.get("X-Faultline-Delay", "0.3"))
                    import time
                    time.sleep(delay)

                len_h = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(len_h).decode()) if len_h else {}
                booking_id = body.get("booking_id", "bk_test")

                # Insert booking into test sqlite database
                conn = sqlite3.connect(str(self.server.db_path))
                with conn:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS seat_reservations (
                            id TEXT PRIMARY KEY,
                            seat TEXT,
                            status TEXT
                        )
                    """)
                    conn.execute("INSERT OR REPLACE INTO seat_reservations VALUES (?, ?, ?)",
                                 (booking_id, body.get("seat", "5A"), "CONFIRMED"))
                conn.close()

                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "booking_id": booking_id,
                    "status": "CONFIRMED",
                    "seat": body.get("seat", "5A"),
                }).encode())

            def log_message(self, format: str, *args: object) -> None:
                pass

        server_instance = ThreadingHTTPServer(("127.0.0.1", 0), MockBookingHandler)
        server_instance.db_path = self.db_path
        port = server_instance.server_address[1]
        t = threading.Thread(target=server_instance.serve_forever, daemon=True)
        t.start()
        self._servers_to_close.append(server_instance)

        # 1. Target registration
        target_id = f"target-dec-{uuid4().hex[:6]}"
        target_cfg = TargetConfig(
            id=target_id,
            name="Declarative Target",
            base_url=f"http://127.0.0.1:{port}",
            permitted_faults=["http_timeout"],
        )
        target_registry.register(target_cfg)

        # 2. Scenario with declarative assertions
        sc_id = f"scen-dec-assert-{uuid4().hex[:6]}"
        scenario_def = {
            "scenario_version": 1,
            "name": sc_id,
            "target_id": target_id,
            "fault": "http_timeout",
            "assertion": "all_steps_succeeded_or_handled",
            "timeout_seconds": 10,
            "workflow": [
                {
                    "id": "book_seat_step",
                    "type": "http.post",
                    "path": "/v1/bookings",
                    "body": {"booking_id": "bk_dec_101", "seat": "5A"},
                }
            ],
            "target_step": "book_seat_step",
            "fault_config": {
                "client_timeout_seconds": 0.1,
                "server_delay_seconds": 0.3,
            },
            "assertions": [
                "all_steps_succeeded_or_handled",
                {
                    "name": "seat_5a_in_db",
                    "type": "database",
                    "db_path": str(self.db_path),
                    "query": "SELECT count(*) FROM seat_reservations WHERE seat = '5A' AND status = 'CONFIRMED'",
                    "op": "equals",
                    "expected": 1,
                },
                {
                    "name": "http_response_is_201",
                    "type": "http_response",
                    "step_id": "book_seat_step",
                    "status_code": 201,
                },
                {
                    "name": "http_response_seat_is_5a",
                    "type": "http_response",
                    "step_id": "book_seat_step",
                    "json_path": "seat",
                    "op": "equals",
                    "expected": "5A",
                },
            ],
        }

        schema.validate_scenario(scenario_def)
        store.create_scenario(sc_id, "faultline-demo", scenario_def)

        # 3. Execute scenario
        run_id = str(uuid4())
        store.create_run(run_id=run_id, scenario_id=sc_id, scenario_version=1, implementation="fixed")
        engine.execute_run(run_id, scenario_def, implementation="fixed")

        run_record = store.get_run(run_id)
        self.assertIsNotNone(run_record)
        self.assertEqual(run_record["verdict"], "passed", f"Run failed: {run_record.get('error')}")

        assertions_result = run_record.get("assertions", [])
        assert_map = {a["name"]: a["status"] for a in assertions_result}
        self.assertEqual(assert_map.get("all_steps_succeeded_or_handled"), "passed")
        self.assertEqual(assert_map.get("seat_5a_in_db"), "passed")
        self.assertEqual(assert_map.get("http_response_is_201"), "passed")
        self.assertEqual(assert_map.get("http_response_seat_is_5a"), "passed")

        target_registry.delete(target_id)


if __name__ == "__main__":
    unittest.main()
