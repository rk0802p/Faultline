import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from http.server import ThreadingHTTPServer

from faultline import store
from faultline.adapters import provider, start_local_services
from faultline.server import ControlPlaneHandler


ROOT = Path(__file__).resolve().parents[1]


class FaultlinePlatformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory(dir=ROOT)
        cls.old_data_dir, cls.old_db_path = store.DATA_DIR, store.DB_PATH
        store.DATA_DIR = Path(cls.temp_dir.name)
        store.DB_PATH = store.DATA_DIR / "test.sqlite3"
        store.initialize()
        cls.local_services = start_local_services()
        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), ControlPlaneHandler)
        cls.api_thread = threading.Thread(target=cls.api.serve_forever, daemon=True)
        cls.api_thread.start()
        cls.api_port = cls.api.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.shutdown()
        cls.api.server_close()
        for service in cls.local_services:
            service.shutdown()
            service.server_close()
        provider.charges.clear()
        provider.idempotency.clear()
        provider.faulted_runs.clear()
        store.DATA_DIR, store.DB_PATH = cls.old_data_dir, cls.old_db_path
        cls.temp_dir.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", self.api_port, timeout=4)
        body = json.dumps(payload) if payload is not None else None
        headers = {"Content-Type": "application/json"} if body else {}
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        raw = response.read()
        result = json.loads(raw) if raw else {}
        connection.close()
        return response.status, result

    def launch_and_wait(self, implementation: str) -> dict:
        status, payload = self.request(
            "POST", "/api/runs",
            {"scenario_id": "payment-response-lost", "implementation": implementation},
        )
        self.assertEqual(status, 202)
        for _ in range(60):
            status, result = self.request("GET", f"/api/runs/{payload['run_id']}")
            self.assertEqual(status, 200)
            if result["run"]["status"] in {"completed", "failed"}:
                return result["run"]
            time.sleep(0.05)
        self.fail("run did not finish within the expected local execution window")

    def test_dashboard_and_project_scenario_api_are_available(self) -> None:
        status, projects = self.request("GET", "/api/projects")
        self.assertEqual(status, 200)
        self.assertEqual(projects["projects"][0]["id"], "faultline-demo")
        status, scenarios = self.request("GET", "/api/scenarios")
        self.assertEqual(status, 200)
        self.assertEqual(scenarios["scenarios"][0]["id"], "payment-response-lost")
        connection = http.client.HTTPConnection("127.0.0.1", self.api_port, timeout=4)
        connection.request("GET", "/")
        response = connection.getresponse()
        html = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertIn("Integration failure lab", html)

    def test_real_http_fault_detects_duplicate_charge_and_persists_evidence(self) -> None:
        run = self.launch_and_wait("vulnerable")

        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["verdict"], "failed")
        self.assertEqual(run["summary"]["charge_count"], 2)
        self.assertEqual(len(run["assertions"]), 4)
        self.assertEqual(
            next(item["status"] for item in run["assertions"] if item["name"] == "no_duplicate_charge"),
            "failed",
        )
        self.assertIn("FAULT_RESPONSE_DROPPED", [event["type"] for event in run["events"]])
        sequences = [event["sequence"] for event in run["events"]]
        self.assertEqual(sequences, list(range(1, len(sequences) + 1)))

    def test_fixed_checkout_reuses_charge_and_passes_invariants(self) -> None:
        run = self.launch_and_wait("fixed")

        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["verdict"], "passed")
        self.assertEqual(run["summary"]["charge_count"], 1)
        self.assertIn("PROVIDER_IDEMPOTENCY_REPLAYED", [event["type"] for event in run["events"]])
        self.assertTrue(all(item["status"] == "passed" for item in run["assertions"]))


if __name__ == "__main__":
    unittest.main()
