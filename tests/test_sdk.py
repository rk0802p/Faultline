"""Unit and integration tests for the Faultline programmatic SDK (faultline.sdk)."""

import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from faultline import (
    FaultlineAPIError,
    FaultlineClient,
    FaultlineError,
    FaultlineInvariantError,
    FaultlineTimeoutError,
    adapters,
    store,
)
from faultline.adapters import start_local_services
from faultline.server import ControlPlaneHandler

ROOT = Path(__file__).resolve().parents[1]


class FaultlineSDKTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory(dir=ROOT)
        cls.old_data_dir = store.DATA_DIR
        cls.old_db_path = store.DB_PATH
        cls.old_checkout_port = adapters.CHECKOUT_PORT
        cls.old_payment_port = adapters.PAYMENT_PORT

        store.DATA_DIR = Path(cls.temp_dir.name)
        store.DB_PATH = store.DATA_DIR / "test.sqlite3"
        store.initialize()

        cls.local_servers = start_local_services(checkout_port=0, payment_port=0)
        cls.api_server = ThreadingHTTPServer(("127.0.0.1", 0), ControlPlaneHandler)
        cls.api_port = cls.api_server.server_address[1]
        cls.thread = threading.Thread(target=cls.api_server.serve_forever, daemon=True)
        cls.thread.start()

        cls.client = FaultlineClient(base_url=f"http://127.0.0.1:{cls.api_port}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api_server.shutdown()
        cls.api_server.server_close()
        for server in cls.local_servers:
            server.shutdown()
            server.server_close()
        store.DATA_DIR, store.DB_PATH = cls.old_data_dir, cls.old_db_path
        adapters.CHECKOUT_PORT, adapters.PAYMENT_PORT = cls.old_checkout_port, cls.old_payment_port
        cls.temp_dir.cleanup()

    def test_sdk_health_and_metrics(self) -> None:
        health = self.client.health()
        self.assertEqual(health.get("status"), "ok")
        self.assertEqual(health.get("service"), "faultline-control-plane")

        # JSON metrics
        metrics = self.client.metrics(format="json")
        self.assertIsInstance(metrics, dict)
        self.assertIn("uptime_seconds", metrics)
        self.assertIn("worker_health", metrics)
        self.assertEqual(metrics["worker_health"]["status"], "healthy")

        # Prometheus metrics
        prom = self.client.metrics(format="prometheus")
        self.assertIsInstance(prom, str)
        self.assertIn("faultline_runs_total", prom)

    def test_sdk_list_and_get_scenario(self) -> None:
        scenarios = self.client.list_scenarios()
        self.assertGreaterEqual(len(scenarios), 3)
        scenario_ids = [s["id"] for s in scenarios]
        self.assertIn("payment-response-lost", scenario_ids)

        sc = self.client.get_scenario("payment-response-lost")
        self.assertEqual(sc["id"], "payment-response-lost")

        with self.assertRaises(FaultlineError):
            self.client.get_scenario("non-existent-scenario-id")

    def test_sdk_run_scenario_vulnerable_and_fixed(self) -> None:
        # Fixed implementation passes invariants
        fixed_run = self.client.run_scenario("payment-response-lost", implementation="fixed", wait=True, assert_pass=True)
        self.assertEqual(fixed_run["verdict"], "passed")
        self.assertEqual(fixed_run["status"], "completed")

        # Vulnerable implementation raises FaultlineInvariantError
        with self.assertRaises(FaultlineInvariantError) as ctx:
            self.client.run_scenario("payment-response-lost", implementation="vulnerable", wait=True, assert_pass=True)
        self.assertGreaterEqual(len(ctx.exception.failed_assertions), 1)
        self.assertEqual(ctx.exception.run["verdict"], "failed")

    def test_sdk_telemetry_and_trace_context(self) -> None:
        run = self.client.run_scenario("payment-response-lost", implementation="fixed", wait=True)
        run_id = run["id"]

        telemetry = self.client.get_telemetry(run_id)
        self.assertTrue("traceId" in telemetry or "trace_id" in telemetry)
        self.assertGreaterEqual(len(telemetry["spans"]), 1)

        with self.client.trace_context(run_id) as headers:
            self.assertIn("traceparent", headers)
            self.assertTrue(headers["traceparent"].startswith("00-"))

    def test_sdk_state_snapshots_and_reconstruction(self) -> None:
        run = self.client.run_scenario("payment-response-lost", implementation="fixed", wait=True)
        run_id = run["id"]

        snapshots = self.client.get_snapshots(run_id)
        self.assertGreaterEqual(len(snapshots), 1)

        state = self.client.get_state_at(run_id, sequence=1)
        self.assertEqual(state.get("sequence"), 1)

    def test_sdk_checkpoint_branching(self) -> None:
        run = self.client.run_scenario("payment-response-lost", implementation="fixed", wait=True)
        run_id = run["id"]

        branched = self.client.branch_run(run_id, snapshot_sequence=1, overrides={"retries": 2})
        self.assertIsNotNone(branched.get("id"))
        self.assertEqual(branched.get("parent_run_id"), run_id)
        self.assertEqual(branched.get("forked_from_snapshot_id"), 1)

    def test_sdk_baseline_and_diff(self) -> None:
        run = self.client.run_scenario("payment-response-lost", implementation="fixed", wait=True)
        run_id = run["id"]

        base_res = self.client.set_baseline(run_id)
        self.assertTrue(base_res["is_baseline"])

        diff = self.client.get_baseline_diff("payment-response-lost")
        self.assertTrue(diff["has_baseline"])
        self.assertEqual(diff["baseline_run_id"], run_id)

    def test_sdk_suite_execution(self) -> None:
        suite_res = self.client.run_suite("critical-integrations", implementation="fixed", synchronous=True)
        self.assertEqual(suite_res["verdict"], "passed")
        self.assertEqual(suite_res["total_scenarios"], 3)

        results = self.client.list_suite_results("critical-integrations")
        self.assertGreaterEqual(len(results), 1)

    def test_sdk_tasks_and_annotations(self) -> None:
        run = self.client.run_scenario("payment-response-lost", implementation="vulnerable", wait=True)
        run_id = run["id"]

        task = self.client.create_task(run_id, title="Fix duplicate charge issue", priority="high")
        self.assertEqual(task["title"], "Fix duplicate charge issue")
        self.assertEqual(task["run_id"], run_id)

        ann = self.client.add_annotation(run_id, comment="Verified bug reproduced locally.", author="Tester")
        self.assertEqual(ann["comment"], "Verified bug reproduced locally.")
        self.assertEqual(ann["author"], "Tester")

    def test_sdk_retention_prune(self) -> None:
        res = self.client.prune(project_id="faultline-demo", max_age_days=30, dry_run=True)
        self.assertTrue(res["dry_run"])
        self.assertIn("pruned_run_count", res)

    def test_sdk_api_error_handling(self) -> None:
        with self.assertRaises(FaultlineAPIError) as ctx:
            self.client._request("GET", "/api/runs/non-existent-run-id-12345")
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
