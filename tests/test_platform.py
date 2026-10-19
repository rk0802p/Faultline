import http.client
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4
from pathlib import Path

from http.server import ThreadingHTTPServer

from faultline import engine, store
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
        self.assertIn("Visual scenario designer", html)
        self.assertIn("data-field=\"timeout_seconds\"", html)

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

    def test_scenario_versions_are_immutable_and_reproduction_bundle_is_available(self) -> None:
        initial = self.request("GET", "/api/scenarios")[1]["scenarios"][0]
        edited = dict(initial["definition"], amount_cents=3100)
        status, result = self.request("POST", f"/api/scenarios/{initial['id']}/versions", {"definition": edited})
        self.assertEqual((status, result["version"]), (201, 2))
        versions = self.request("GET", f"/api/scenarios/{initial['id']}/versions")[1]["versions"]
        self.assertEqual([item["version"] for item in versions], [2, 1])
        self.assertEqual(versions[1]["definition"]["amount_cents"], initial["definition"]["amount_cents"])
        run = self.launch_and_wait("fixed")
        status, bundle = self.request("GET", f"/api/runs/{run['id']}/reproduce")
        self.assertEqual(status, 200)
        self.assertEqual(bundle["format"], "faultline-repro-v1")
        self.assertIn("PAYMENT_HTTP_REQUEST", [event["type"] for event in bundle["run"]["events"]])

    def test_invalid_scenario_is_rejected_with_validation_feedback(self) -> None:
        status, result = self.request("POST", "/api/scenarios/validate", {"definition": {"name": "broken"}})
        self.assertEqual(status, 400)
        self.assertIn("unsupported scenario_version", result["error"])
        definition = self.request("GET", "/api/scenarios")[1]["scenarios"][0]["definition"]
        definition["timeout_seconds"] = 61
        status, result = self.request("POST", "/api/scenarios/validate", {"definition": definition})
        self.assertEqual(status, 400)
        self.assertIn("timeout_seconds", result["error"])

    def test_new_scenario_can_be_created_from_a_valid_visual_editor_definition(self) -> None:
        definition = dict(self.request("GET", "/api/scenarios")[1]["scenarios"][0]["definition"])
        definition.update({"name": "custom-payment-check", "order_id": "order-custom", "timeout_seconds": 12})
        status, created = self.request("POST", "/api/scenarios", {"definition": definition})
        self.assertEqual(status, 201)
        self.assertEqual(created["scenario_id"], "custom-payment-check")
        versions = self.request("GET", "/api/scenarios/custom-payment-check/versions")[1]["versions"]
        self.assertEqual(versions[0]["version"], 1)

    def test_queued_run_can_be_cancelled_and_reaches_terminal_state(self) -> None:
        with patch("faultline.server.submit"):
            status, submitted = self.request("POST", "/api/runs", {
                "scenario_id": "payment-response-lost", "implementation": "fixed",
            })
        self.assertEqual(status, 202)
        status, result = self.request("POST", f"/api/runs/{submitted['run_id']}/cancel", {})
        self.assertEqual(status, 202)
        self.assertEqual(result["status"], "cancel_requested")
        definition = self.request("GET", "/api/scenarios")[1]["scenarios"][0]["definition"]
        engine.execute_run(submitted["run_id"], definition, "fixed")
        run = self.request("GET", f"/api/runs/{submitted['run_id']}")[1]["run"]
        self.assertEqual(run["status"], "cancelled")
        self.assertIn("RUN_CANCELLATION_REQUESTED", [event["type"] for event in run["events"]])

    def test_worker_marks_run_timed_out_at_scenario_deadline(self) -> None:
        run_id = str(uuid4())
        store.create_run(run_id, "payment-response-lost", 1, "fixed")
        store.record_event(run_id, "RUN_QUEUED", {})
        response = Mock(status=200)
        response.read.return_value = b"{}"
        connection = Mock()
        connection.getresponse.return_value = response
        scenario = dict(self.request("GET", "/api/scenarios")[1]["scenarios"][0]["definition"], timeout_seconds=2)
        with patch("faultline.engine.HTTPConnection", return_value=connection), patch("faultline.engine.time.monotonic", side_effect=[1.0, 4.0]):
            engine.execute_run(run_id, scenario, "fixed")
        run = self.request("GET", f"/api/runs/{run_id}")[1]["run"]
        self.assertEqual(run["status"], "timed_out")
        self.assertIn("RUN_TIMED_OUT", [event["type"] for event in run["events"]])

    def test_running_run_cancellation_closes_worker_request(self) -> None:
        run_id = str(uuid4())
        store.create_run(run_id, "payment-response-lost", 1, "fixed")
        store.record_event(run_id, "RUN_QUEUED", {})
        request_started = threading.Event()
        connection_closed = threading.Event()
        connection = Mock()
        def wait_for_close(*_args: object, **_kwargs: object) -> object:
            request_started.set()
            if not connection_closed.wait(3):
                raise TimeoutError("test connection was not closed")
            raise ConnectionAbortedError("cancelled by test")
        connection.getresponse.side_effect = wait_for_close
        connection.close.side_effect = connection_closed.set
        scenario = self.request("GET", "/api/scenarios")[1]["scenarios"][0]["definition"]
        with patch("faultline.engine.HTTPConnection", return_value=connection):
            worker = threading.Thread(target=engine.execute_run, args=(run_id, scenario, "fixed"), daemon=True)
            worker.start()
            self.assertTrue(request_started.wait(2), "worker never reached the checkout request")
            status, result = self.request("POST", f"/api/runs/{run_id}/cancel", {})
            self.assertEqual(status, 202)
            worker.join(2)
        self.assertFalse(worker.is_alive())
        run = self.request("GET", f"/api/runs/{run_id}")[1]["run"]
        self.assertEqual(result["status"], "cancel_requested")
        self.assertEqual(run["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
