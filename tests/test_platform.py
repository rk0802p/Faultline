import http.client
import json
import tempfile
import threading
import time
from typing import Any
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4
from pathlib import Path

from http.server import ThreadingHTTPServer

from faultline import adapters, engine, security, server, store
from faultline.adapters import provider, start_local_services
from faultline.server import ControlPlaneHandler


ROOT = Path(__file__).resolve().parents[1]


class FaultlinePlatformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory(dir=ROOT)
        cls.old_data_dir, cls.old_db_path = store.DATA_DIR, store.DB_PATH
        cls.old_checkout_port, cls.old_payment_port = adapters.CHECKOUT_PORT, adapters.PAYMENT_PORT
        store.DATA_DIR = Path(cls.temp_dir.name)
        store.DB_PATH = store.DATA_DIR / "test.sqlite3"
        store.initialize()
        cls.local_services = start_local_services(checkout_port=0, payment_port=0)
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
        provider.refunds.clear()
        provider.idempotency.clear()
        provider.faulted_runs.clear()
        adapters.checkout_service.fulfillments.clear()
        adapters.checkout_service.processed_events.clear()
        store.DATA_DIR, store.DB_PATH = cls.old_data_dir, cls.old_db_path
        adapters.CHECKOUT_PORT, adapters.PAYMENT_PORT = cls.old_checkout_port, cls.old_payment_port
        cls.temp_dir.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None, headers: dict | None = None) -> tuple[int, Any]:
        connection = http.client.HTTPConnection("127.0.0.1", self.api_port, timeout=15)
        body = json.dumps(payload) if payload is not None else None
        req_headers = {"Content-Type": "application/json"} if body else {}
        if headers:
            req_headers.update(headers)
        connection.request(method, path, body, req_headers)
        response = connection.getresponse()
        raw = response.read()
        content_type = response.getheader("Content-Type", "")
        if "json" in content_type:
            result = json.loads(raw) if raw else {}
        else:
            result = raw.decode("utf-8") if raw else ""
        connection.close()
        return response.status, result

    def wait_for_run(self, run_id: str) -> dict:
        for _ in range(60):
            status, result = self.request("GET", f"/api/runs/{run_id}")
            self.assertEqual(status, 200)
            if result["run"]["status"] in {"completed", "failed", "cancelled", "timed_out"}:
                return result["run"]
            time.sleep(0.05)
        self.fail("run did not finish within the expected local execution window")

    def launch_and_wait(self, implementation: str, scenario_id: str = "payment-response-lost") -> dict:
        status, payload = self.request(
            "POST", "/api/runs",
            {"scenario_id": scenario_id, "implementation": implementation},
        )
        self.assertEqual(status, 202)
        return self.wait_for_run(payload["run_id"])

    def test_dashboard_and_project_scenario_api_are_available(self) -> None:
        status, projects = self.request("GET", "/api/projects")
        self.assertEqual(status, 200)
        self.assertEqual(projects["projects"][0]["id"], "faultline-demo")
        status, scenarios = self.request("GET", "/api/scenarios")
        self.assertEqual(status, 200)
        scenario_ids = [s["id"] for s in scenarios["scenarios"]]
        self.assertIn("payment-response-lost", scenario_ids)
        self.assertIn("duplicate-payment-webhook", scenario_ids)
        self.assertIn("partial-persistence-failure", scenario_ids)
        status, single_sc = self.request("GET", "/api/scenarios/payment-response-lost")
        self.assertEqual(status, 200)
        self.assertEqual(single_sc["scenario"]["id"], "payment-response-lost")
        status, not_found_sc = self.request("GET", "/api/scenarios/nonexistent-scenario-xyz")
        self.assertEqual(status, 404)
        status, faults_data = self.request("GET", "/api/scenarios/supported-faults")
        self.assertEqual(status, 200)
        self.assertIn("payment_response_lost_after_commit", faults_data["faults"])
        self.assertIn("workflow_steps", faults_data)
        connection = http.client.HTTPConnection("127.0.0.1", self.api_port, timeout=4)
        connection.request("GET", "/api.js")
        api_js_resp = connection.getresponse()
        self.assertEqual(api_js_resp.status, 200)
        self.assertIn("text/javascript", api_js_resp.headers.get("Content-Type", ""))
        api_js_content = api_js_resp.read().decode("utf-8")
        self.assertIn("getScenarios", api_js_content)
        self.assertIn("getSupportedFaults", api_js_content)
        connection.request("GET", "/")
        response = connection.getresponse()
        html = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertIn("Integration failure lab", html)
        self.assertIn("Visual scenario designer", html)
        self.assertIn("data-field=\"timeout_seconds\"", html)
        self.assertIn("Execution timeline & replay", html)
        self.assertIn("id=\"replay-controls\"", html)
        self.assertIn("data-scenario=\"partial-persistence-failure\"", html)
        self.assertIn("Critical Integrations Suite", html)
        self.assertIn("id=\"run-diff-card\"", html)
        self.assertIn("id=\"coverage\"", html)
        self.assertIn("id=\"annotation-form\"", html)
        self.assertIn("id=\"task-form\"", html)

    def test_partial_persistence_failure_vulnerable_leaves_uncompensated_charge(self) -> None:
        run = self.launch_and_wait("vulnerable", "partial-persistence-failure")

        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["verdict"], "failed")
        self.assertEqual(run["summary"]["charge_count"], 1)
        self.assertEqual(run["summary"]["refund_count"], 0)
        self.assertEqual(run["summary"]["compensated"], False)
        self.assertEqual(len(run["assertions"]), 4)
        self.assertEqual(
            next(item["status"] for item in run["assertions"] if item["name"] == "charge_compensated_on_failure"),
            "failed",
        )
        self.assertEqual(
            next(item["status"] for item in run["assertions"] if item["name"] == "no_orphaned_charge"),
            "failed",
        )
        self.assertEqual(
            next(item["status"] for item in run["assertions"] if item["name"] == "fault_activation_confirmed"),
            "passed",
        )
        event_types = [event["type"] for event in run["events"]]
        self.assertIn("FAULT_PERSISTENCE_FAILED", event_types)

    def test_partial_persistence_failure_fixed_compensates_charge_with_refund(self) -> None:
        run = self.launch_and_wait("fixed", "partial-persistence-failure")

        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["verdict"], "passed")
        self.assertEqual(run["summary"]["charge_count"], 1)
        self.assertEqual(run["summary"]["refund_count"], 1)
        self.assertEqual(run["summary"]["compensated"], True)
        self.assertTrue(all(item["status"] == "passed" for item in run["assertions"]))
        event_types = [event["type"] for event in run["events"]]
        self.assertIn("COMPENSATION_COMPLETED", event_types)
        self.assertIn("PROVIDER_REFUND_COMMITTED", event_types)

    def test_duplicate_webhook_vulnerable_creates_multiple_fulfillments_and_fails_invariants(self) -> None:
        run = self.launch_and_wait("vulnerable", "duplicate-payment-webhook")

        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["verdict"], "failed")
        self.assertEqual(run["summary"]["fulfillment_count"], 2)
        self.assertEqual(len(run["assertions"]), 4)
        self.assertEqual(
            next(item["status"] for item in run["assertions"] if item["name"] == "idempotent_webhook_processing"),
            "failed",
        )
        self.assertEqual(
            next(item["status"] for item in run["assertions"] if item["name"] == "fault_activation_confirmed"),
            "passed",
        )
        event_types = [event["type"] for event in run["events"]]
        self.assertIn("FAULT_DUPLICATE_WEBHOOK_DELIVERED", event_types)
        self.assertIn("ORDER_FULFILLMENT_COMMITTED", event_types)

    def test_duplicate_webhook_fixed_deduplicates_and_passes_invariants(self) -> None:
        run = self.launch_and_wait("fixed", "duplicate-payment-webhook")

        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["verdict"], "passed")
        self.assertEqual(run["summary"]["fulfillment_count"], 1)
        self.assertIn("WEBHOOK_DUPLICATE_IGNORED", [event["type"] for event in run["events"]])
        self.assertTrue(all(item["status"] == "passed" for item in run["assertions"]))

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
        scenarios = self.request("GET", "/api/scenarios")[1]["scenarios"]
        initial = next(item for item in scenarios if item["id"] == "payment-response-lost")
        edited = dict(initial["definition"], amount_cents=3100)
        status, result = self.request("POST", f"/api/scenarios/{initial['id']}/versions", {"definition": edited})
        self.assertEqual((status, result["version"]), (201, 2))
        versions = self.request("GET", f"/api/scenarios/{initial['id']}/versions")[1]["versions"]
        self.assertEqual([item["version"] for item in versions], [2, 1])
        self.assertEqual(versions[1]["definition"]["amount_cents"], initial["definition"]["amount_cents"])
        run = self.launch_and_wait("fixed")
        self.assertEqual(run["scenario_version"], 2)
        self.assertEqual(run["scenario"]["amount_cents"], 3100)
        status, bundle = self.request("GET", f"/api/runs/{run['id']}/reproduce")
        self.assertEqual(status, 200)
        self.assertEqual(bundle["format"], "faultline-repro-v1")
        event_types = [event["type"] for event in bundle["run"]["events"]]
        self.assertIn("CHECKOUT_HTTP_REQUEST", event_types)
        self.assertIn("PAYMENT_HTTP_REQUEST", event_types)
        self.assertIn("PAYMENT_HTTP_RESPONSE", event_types)

    def test_invalid_scenario_is_rejected_with_validation_feedback(self) -> None:
        status, result = self.request("POST", "/api/scenarios/validate", {"definition": {"name": "broken"}})
        self.assertEqual(status, 400)
        self.assertIn("unsupported scenario_version", result["error"])
        scenarios = self.request("GET", "/api/scenarios")[1]["scenarios"]
        definition = next(s for s in scenarios if s["id"] == "payment-response-lost")["definition"]
        definition["timeout_seconds"] = 61
        status, result = self.request("POST", "/api/scenarios/validate", {"definition": definition})
        self.assertEqual(status, 400)
        self.assertIn("timeout_seconds", result["error"])

    def test_new_scenario_can_be_created_from_a_valid_visual_editor_definition(self) -> None:
        scenarios = self.request("GET", "/api/scenarios")[1]["scenarios"]
        definition = dict(next(s for s in scenarios if s["id"] == "payment-response-lost")["definition"])
        definition.update({"name": "custom-payment-check", "order_id": "order-custom", "timeout_seconds": 12})
        status, created = self.request("POST", "/api/scenarios", {"definition": definition})
        self.assertEqual(status, 201)
        self.assertEqual(created["scenario_id"], "custom-payment-check")
        versions = self.request("GET", "/api/scenarios/custom-payment-check/versions")[1]["versions"]
        self.assertEqual(versions[0]["version"], 1)
        run = self.launch_and_wait("fixed", "custom-payment-check")
        self.assertEqual(run["scenario"]["order_id"], "order-custom")
        self.assertEqual(run["scenario_version"], 1)

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

    def test_suites_api_list_get_and_create(self) -> None:
        status, data = self.request("GET", "/api/suites")
        self.assertEqual(status, 200)
        suite_ids = [s["id"] for s in data["suites"]]
        self.assertIn("critical-integrations", suite_ids)
        crit = next(s for s in data["suites"] if s["id"] == "critical-integrations")
        self.assertEqual(crit["scenarios"], [
            "payment-response-lost",
            "duplicate-payment-webhook",
            "partial-persistence-failure",
        ])

        status, single = self.request("GET", "/api/suites/critical-integrations")
        self.assertEqual(status, 200)
        self.assertEqual(single["suite"]["id"], "critical-integrations")
        self.assertEqual(len(single["suite"]["scenarios"]), 3)

        status, missing = self.request("GET", "/api/suites/non-existent-suite")
        self.assertEqual(status, 404)

        # Create custom suite
        new_suite_payload = {
            "id": "custom-checkout-suite",
            "name": "Custom Checkout Suite",
            "description": "A customized regression suite",
            "scenarios": ["duplicate-payment-webhook", "partial-persistence-failure"],
        }
        status, created = self.request("POST", "/api/suites", new_suite_payload)
        self.assertEqual(status, 201)
        self.assertEqual(created["suite_id"], "custom-checkout-suite")

        status, fetched = self.request("GET", "/api/suites/custom-checkout-suite")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["suite"]["scenarios"], [
            "duplicate-payment-webhook",
            "partial-persistence-failure",
        ])

    def test_run_diff_endpoint_compares_runs_and_identifies_divergence(self) -> None:
        vuln_run = self.launch_and_wait("vulnerable", "payment-response-lost")
        fixed_run = self.launch_and_wait("fixed", "payment-response-lost")

        status, diff_data = self.request("GET", f"/api/runs/{vuln_run['id']}/diff/{fixed_run['id']}")
        self.assertEqual(status, 200)
        diff = diff_data["diff"]

        self.assertFalse(diff["verdict_match"])
        self.assertEqual(diff["run_a"]["verdict"], "failed")
        self.assertEqual(diff["run_b"]["verdict"], "passed")

        dup_charge_inv = next(item for item in diff["invariants"] if item["name"] == "no_duplicate_charge")
        self.assertTrue(dup_charge_inv["diverged"])
        self.assertEqual(dup_charge_inv["run_a_status"], "failed")
        self.assertEqual(dup_charge_inv["run_b_status"], "passed")

        self.assertIsNotNone(diff["first_event_divergence"])
        self.assertGreater(diff["event_counts"]["run_a"], 0)
        self.assertGreater(diff["event_counts"]["run_b"], 0)

        # Non-existent run diff returns 404
        status, _ = self.request("GET", f"/api/runs/{vuln_run['id']}/diff/non-existent-id")
        self.assertEqual(status, 404)

    def test_failure_coverage_matrix_api(self) -> None:
        status, data = self.request("GET", "/api/coverage")
        self.assertEqual(status, 200)
        self.assertIn("workflow_steps", data)
        self.assertIn("fault_classes", data)
        self.assertIn("matrix", data)
        self.assertIn("summary", data)

        step_ids = [s["id"] for s in data["workflow_steps"]]
        self.assertIn("initiate_checkout", step_ids)
        self.assertIn("process_payment", step_ids)
        self.assertIn("order_persistence", step_ids)

        summary = data["summary"]
        self.assertGreater(summary["total_applicable"], 0)
        self.assertGreaterEqual(summary["coverage_percentage"], 0)

        # Verify latest-run policy: a failing run after a passing run transitions cell to defect_exposed
        sc_id = "duplicate-payment-webhook"
        pass_run = self.launch_and_wait("fixed", sc_id)
        self.assertEqual(pass_run["verdict"], "passed")
        status, data_pass = self.request("GET", "/api/coverage")
        self.assertEqual(status, 200)
        matching_cells = [c for r in data_pass["matrix"] if r["step"]["id"] == "webhook_delivery" for c in r["cells"] if c["fault_id"] == "duplicate_payment_webhook"]
        self.assertTrue(matching_cells)
        self.assertEqual(matching_cells[0]["status"], "passed")

        fail_run = self.launch_and_wait("vulnerable", sc_id)
        self.assertEqual(fail_run["verdict"], "failed")
        status, data_fail = self.request("GET", "/api/coverage")
        self.assertEqual(status, 200)
        matching_cells_after = [c for r in data_fail["matrix"] if r["step"]["id"] == "webhook_delivery" for c in r["cells"] if c["fault_id"] == "duplicate_payment_webhook"]
        self.assertTrue(matching_cells_after)
        self.assertEqual(matching_cells_after[0]["status"], "defect_exposed")

    def test_run_annotations_lifecycle(self) -> None:
        run = self.launch_and_wait("vulnerable", "payment-response-lost")
        run_id = run["id"]

        # Initially empty
        status, initial = self.request("GET", f"/api/runs/{run_id}/annotations")
        self.assertEqual(status, 200)
        self.assertEqual(len(initial["annotations"]), 0)

        # Add annotation
        note_payload = {
            "author": "Alice",
            "comment": "Observed connection close immediately after charge commit.",
            "sequence_number": 3,
        }
        status, created = self.request("POST", f"/api/runs/{run_id}/annotations", note_payload)
        self.assertEqual(status, 201)
        ann = created["annotation"]
        self.assertEqual(ann["author"], "Alice")
        self.assertEqual(ann["comment"], note_payload["comment"])
        self.assertEqual(ann["sequence_number"], 3)

        # Retrieve and verify
        status, fetched = self.request("GET", f"/api/runs/{run_id}/annotations")
        self.assertEqual(status, 200)
        self.assertEqual(len(fetched["annotations"]), 1)
        self.assertEqual(fetched["annotations"][0]["id"], ann["id"])

    def test_defect_action_tasks_lifecycle(self) -> None:
        run = self.launch_and_wait("vulnerable", "duplicate-payment-webhook")
        run_id = run["id"]

        # Create task linked to run
        task_payload = {
            "title": "Implement webhook idempotency key cache",
            "description": "Fulfillments duplicate when network delivers webhook twice.",
            "priority": "critical",
            "assignee": "Bob",
        }
        status, created = self.request("POST", f"/api/runs/{run_id}/tasks", task_payload)
        self.assertEqual(status, 201)
        task = created["task"]
        self.assertEqual(task["title"], task_payload["title"])
        self.assertEqual(task["status"], "open")
        self.assertEqual(task["priority"], "critical")
        self.assertEqual(task["assignee"], "Bob")

        task_id = task["id"]

        # Verify task is returned under run tasks
        status, run_tasks = self.request("GET", f"/api/runs/{run_id}/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(len(run_tasks["tasks"]), 1)
        self.assertEqual(run_tasks["tasks"][0]["id"], task_id)

        # Verify task is returned under project tasks
        status, proj_tasks = self.request("GET", "/api/projects/faultline-demo/tasks")
        self.assertEqual(status, 200)
        self.assertTrue(any(t["id"] == task_id for t in proj_tasks["tasks"]))

        # Update task status to resolved via PATCH
        status, updated = self.request("PATCH", f"/api/tasks/{task_id}", {"status": "resolved"})
        self.assertEqual(status, 200)
        self.assertEqual(updated["task"]["status"], "resolved")
        self.assertIsNotNone(updated["task"]["resolved_at"])

    def test_incident_to_scenario_importer(self) -> None:
        incident_payload = {
            "title": "Checkout double charge incident 2026-10",
            "description": "Customer reported 2 charges after network dropped during checkout retry.",
            "fault": "payment_response_lost_after_commit",
            "order_id": "order-inc-99",
            "amount_cents": 4900,
            "retries": 1,
        }
        status, result = self.request("POST", "/api/scenarios/import-incident", incident_payload)
        self.assertEqual(status, 201)
        self.assertIn("scenario_id", result)
        self.assertEqual(result["version"], 1)

        # Verify imported scenario is now available in scenario list
        status, scenarios_data = self.request("GET", "/api/scenarios")
        self.assertEqual(status, 200)
        scenario_ids = [s["id"] for s in scenarios_data["scenarios"]]
        self.assertIn(result["scenario_id"], scenario_ids)

    def test_retrospective_report_endpoint(self) -> None:
        run = self.launch_and_wait("fixed", "payment-response-lost")
        run_id = run["id"]

        status, report_data = self.request("GET", f"/api/runs/{run_id}/report")
        self.assertEqual(status, 200)
        rep = report_data["report"]
        self.assertEqual(rep["run_id"], run_id)
        self.assertEqual(rep["verdict"], "passed")
        self.assertTrue(rep["fault_analysis"]["activated"])
        self.assertTrue(rep["recovery_analysis"]["recovered"])
        self.assertIn("resilient", rep["recommendation"].lower())

        # Markdown format
        connection = http.client.HTTPConnection("127.0.0.1", self.api_port, timeout=4)
        connection.request("GET", f"/api/runs/{run_id}/report?format=markdown")
        response = connection.getresponse()
        md = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertIn("# Faultline Retrospective Report", md)
        self.assertIn("Executive Summary", md)
        self.assertIn("Invariant Evaluation", md)
        self.assertIn("Cleanup & Fixture Teardown", md)

    def test_security_redaction_and_loopback_allowlist(self) -> None:
        from faultline import security

        # Test secret redaction on sensitive payload
        sensitive_data = {
            "password": "supersecretpassword123",
            "auth_header": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xyz",
            "api_key": "sk_live_99887766554433221100",
            "card_number": "4111 2222 3333 4444",
            "order_id": "order-safe-123",
            "nested": {
                "token": "tok_12345",
                "message": "User with card 5555444433332222 checked out with key sk_live_1122334455667788",
            },
        }
        redacted = security.redact_sensitive(sensitive_data)
        self.assertEqual(redacted["password"], "[REDACTED]")
        self.assertIn("Bearer [REDACTED]", redacted["auth_header"])
        self.assertEqual(redacted["api_key"], "[REDACTED]")
        self.assertEqual(redacted["card_number"], "[REDACTED]")
        self.assertEqual(redacted["order_id"], "order-safe-123")
        self.assertEqual(redacted["nested"]["token"], "[REDACTED]")
        self.assertIn("[REDACTED_PAN]", redacted["nested"]["message"])
        self.assertIn("[REDACTED_KEY]", redacted["nested"]["message"])

        # Test loopback security allowlist
        self.assertTrue(security.is_loopback_allowed("http://127.0.0.1:8766/v1/checkout"))
        self.assertTrue(security.is_loopback_allowed("http://localhost:8000/api/runs"))
        self.assertTrue(security.is_loopback_allowed("127.0.0.1:8767"))
        self.assertFalse(security.is_loopback_allowed("0.0.0.0"))
        self.assertFalse(security.is_loopback_allowed("http://0.0.0.0:8000"))
        self.assertFalse(security.is_loopback_allowed("https://api.external-payment.com/v1/charges"))
        self.assertFalse(security.is_loopback_allowed("http://192.168.1.50:8000"))

        with self.assertRaises(PermissionError):
            security.assert_loopback_safe("https://evil.internal.corp/steal")

    def test_cleanup_tracking_and_failure_separation(self) -> None:
        run = self.launch_and_wait("fixed", "payment-response-lost")
        run_id = run["id"]

        status, run_data = self.request("GET", f"/api/runs/{run_id}")
        self.assertEqual(status, 200)
        cleanup = run_data["run"]["cleanup"]
        self.assertEqual(cleanup["status"], "succeeded")
        self.assertIsInstance(cleanup["owned_resources"], list)
        self.assertTrue(len(cleanup["owned_resources"]) > 0)
        self.assertIsNone(cleanup.get("error"))

    def test_scenario_templates_api_and_instantiation(self) -> None:
        # 1. List templates
        status, data = self.request("GET", "/api/templates")
        self.assertEqual(status, 200)
        templates = data["templates"]
        self.assertEqual(len(templates), 3)
        tpl_ids = [t["id"] for t in templates]
        self.assertEqual(sorted(tpl_ids), [
            "duplicate-payment-webhook-tpl",
            "partial-persistence-failure-tpl",
            "payment-response-lost-tpl",
        ])

        # 2. Get specific template
        status, tpl_data = self.request("GET", "/api/templates/payment-response-lost-tpl")
        self.assertEqual(status, 200)
        self.assertEqual(tpl_data["template"]["category"], "network_failure")
        self.assertIn("parameters_schema", tpl_data["template"])

        # 3. Instantiate template with valid parameters
        inst_payload = {
            "scenario_id": f"inst-sc-{uuid4().hex[:6]}",
            "name": "Customized Payment Loss",
            "parameters": {"amount_cents": 9999, "retries": 2},
        }
        status, inst_result = self.request("POST", "/api/templates/payment-response-lost-tpl/instantiate", inst_payload)
        self.assertEqual(status, 201)
        self.assertEqual(inst_result["instance"]["scenario_id"], inst_payload["scenario_id"])
        self.assertEqual(inst_result["instance"]["definition"]["amount_cents"], 9999)

        # 4. Reject invalid parameters
        # Unknown parameter rejected
        status, bad_res1 = self.request("POST", "/api/templates/payment-response-lost-tpl/instantiate", {
            "scenario_id": f"inst-sc-bad1-{uuid4().hex[:4]}",
            "parameters": {"unexpected_key": "val"},
        })
        self.assertEqual(status, 400)

        # Invalid parameter type rejected
        status, bad_res2 = self.request("POST", "/api/templates/payment-response-lost-tpl/instantiate", {
            "scenario_id": f"inst-sc-bad2-{uuid4().hex[:4]}",
            "parameters": {"amount_cents": "not_an_integer"},
        })
        self.assertEqual(status, 400)

        # Out-of-range parameter rejected
        status, bad_res3 = self.request("POST", "/api/templates/payment-response-lost-tpl/instantiate", {
            "scenario_id": f"inst-sc-bad3-{uuid4().hex[:4]}",
            "parameters": {"retries": 99},
        })
        self.assertEqual(status, 400)

        # Verify valid scenario exists and is runnable
        status, scenarios_data = self.request("GET", "/api/scenarios")
        self.assertEqual(status, 200)
        sc_ids = [s["id"] for s in scenarios_data["scenarios"]]
        self.assertIn(inst_payload["scenario_id"], sc_ids)

        # 5. Multi-project template instantiation
        store.create_project("custom-proj", "Custom Project", "Testing multi-project support")
        custom_sc_id = f"inst-custom-{uuid4().hex[:6]}"
        inst_custom_payload = {
            "scenario_id": custom_sc_id,
            "project_id": "custom-proj",
            "parameters": {"amount_cents": 5000},
        }
        status, custom_res = self.request("POST", "/api/templates/payment-response-lost-tpl/instantiate", inst_custom_payload)
        self.assertEqual(status, 201)
        self.assertEqual(custom_res["instance"]["project_id"], "custom-proj")
        custom_scen = store.get_scenario(custom_sc_id)
        self.assertIsNotNone(custom_scen)
        self.assertEqual(custom_scen["project_id"], "custom-proj")

    def test_openapi_workflow_import_endpoint(self) -> None:
        openapi_spec = {
            "openapi": "3.0.0",
            "info": {"title": "Checkout E-Commerce API", "version": "2.4.0"},
            "paths": {
                "/v1/checkout": {
                    "post": {
                        "summary": "Initiate checkout order",
                        "operationId": "createCheckout",
                        "parameters": [{"name": "Idempotency-Key", "in": "header"}],
                    }
                },
                "/v1/charges": {
                    "post": {
                        "summary": "Process payment charge",
                        "operationId": "createCharge",
                    }
                },
                "/v1/webhooks/payment": {
                    "post": {
                        "summary": "Payment webhook notification callback",
                        "operationId": "handlePaymentWebhook",
                    }
                },
            },
        }
        status, result = self.request("POST", "/api/workflows/import-openapi", {"spec": openapi_spec})
        self.assertEqual(status, 200)
        wf = result["workflow"]
        self.assertEqual(wf["title"], "Checkout E-Commerce API")
        self.assertEqual(wf["endpoints_count"], 3)
        self.assertEqual(len(wf["workflow_nodes"]), 3)
        self.assertEqual(len(wf["workflow_edges"]), 2)
        self.assertGreaterEqual(len(wf["recommended_scenarios"]), 2)
        rec_names = [r["name"] for r in wf["recommended_scenarios"]]
        self.assertTrue(any("Lost Response" in n for n in rec_names))
        self.assertTrue(any("Duplicate Webhook" in n for n in rec_names))

    def test_project_services_registry_and_health_checks(self) -> None:
        status, data = self.request("GET", "/api/projects/faultline-demo/services")
        self.assertEqual(status, 200)
        services = data["services"]
        self.assertGreaterEqual(len(services), 2)
        service_ids = [s["id"] for s in services]
        self.assertIn("checkout-service", service_ids)
        self.assertIn("payment-provider", service_ids)
        for s in services:
            self.assertEqual(s["status"], "healthy")
            self.assertIsNotNone(s["latency_ms"])

    def test_audit_events_lifecycle_and_retrieval(self) -> None:
        # 1. Trigger an action that records an audit event (e.g. submit run)
        run = self.launch_and_wait("fixed", "payment-response-lost")
        run_id = run["id"]

        # 2. Retrieve audit log
        status, data = self.request("GET", "/api/projects/faultline-demo/audit-events")
        self.assertEqual(status, 200)
        events = data["audit_events"]
        self.assertIsInstance(events, list)
        self.assertGreaterEqual(len(events), 1)
        actions = [e["action"] for e in events]
        self.assertIn("RUN_SUBMITTED", actions)

    def test_state_snapshots_and_point_in_time_reconstruction(self) -> None:
        run = self.launch_and_wait("fixed", "payment-response-lost")
        run_id = run["id"]

        # Verify snapshots endpoint
        status, snap_data = self.request("GET", f"/api/runs/{run_id}/snapshots")
        self.assertEqual(status, 200)
        snapshots = snap_data["snapshots"]
        self.assertGreaterEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["snapshot"]["verdict"], "pending")
        self.assertEqual(snapshots[-1]["snapshot"]["verdict"], "passed")

        # Verify point-in-time state reconstruction
        status, state_data = self.request("GET", f"/api/runs/{run_id}/state?sequence=10")
        self.assertEqual(status, 200)
        state = state_data["state"]
        self.assertEqual(state["run_id"], run_id)
        self.assertIn("orders", state)
        self.assertIn("charges", state)

    def test_all_supported_fault_assertion_combinations(self) -> None:
        """Verify that every supported fault/assertion combination validates cleanly, and mismatches fail."""
        valid_defs = [
            {
                "scenario_version": 1,
                "name": "test-valid-1",
                "order_id": "ord-1",
                "amount_cents": 1000,
                "fault": "payment_response_lost_after_commit",
                "assertion": "no_duplicate_charge",
                "retries": 1,
            },
            {
                "scenario_version": 1,
                "name": "test-valid-2",
                "order_id": "ord-2",
                "amount_cents": 2000,
                "fault": "duplicate_payment_webhook",
                "assertion": "idempotent_webhook_processing",
                "webhook_deliveries": 2,
                "delay_ms": 100,
            },
            {
                "scenario_version": 1,
                "name": "test-valid-3",
                "order_id": "ord-3",
                "amount_cents": 3000,
                "fault": "persistence_failure_after_payment",
                "assertion": "charge_compensated_on_failure",
                "retries": 0,
            },
        ]
        for d in valid_defs:
            server.validate_scenario(d)

        # Mismatched assertion raises ValueError
        mismatched = dict(valid_defs[0])
        mismatched["assertion"] = "idempotent_webhook_processing"
        with self.assertRaises(ValueError):
            server.validate_scenario(mismatched)

        # Unsupported fault raises ValueError
        bad_fault = dict(valid_defs[0])
        bad_fault["fault"] = "nonexistent_fault"
        with self.assertRaises(ValueError):
            server.validate_scenario(bad_fault)

    def test_invalid_template_parameters_leaves_no_database_record(self) -> None:
        """Verify that template instantiation failure leaves no partial scenario record in database."""
        sc_id = f"inst-bad-atomic-{uuid4().hex[:6]}"
        status, res = self.request("POST", "/api/templates/payment-response-lost-tpl/instantiate", {
            "scenario_id": sc_id,
            "parameters": {"retries": 999},
        })
        self.assertEqual(status, 400)
        self.assertIsNone(store.get_scenario(sc_id))
        with store.database() as db:
            row = db.execute("SELECT id FROM scenarios WHERE id=?", (sc_id,)).fetchone()
            self.assertIsNone(row)
            ver_row = db.execute("SELECT scenario_id FROM scenario_versions WHERE scenario_id=?", (sc_id,)).fetchone()
            self.assertIsNone(ver_row)

    def test_generate_scenarios_from_coverage_gaps(self) -> None:
        status, result = self.request("POST", "/api/scenarios/generate-from-gaps", {"project_id": "faultline-demo"})
        self.assertEqual(status, 201)
        self.assertIn("generated", result)
        self.assertIsInstance(result["generated"], list)
        for gen in result["generated"]:
            sc_def = gen["definition"]
            # Every generated scenario must validate cleanly under canonical validate_scenario
            server.validate_scenario(sc_def)
            self.assertIn(sc_def["fault"], server.SUPPORTED_FAULTS)
            expected_assertion = server.SUPPORTED_FAULTS[sc_def["fault"]]["primary_assertion"]
            self.assertEqual(sc_def["assertion"], expected_assertion)

        # Repeated generation must not create duplicates
        status2, result2 = self.request("POST", "/api/scenarios/generate-from-gaps", {"project_id": "faultline-demo"})
        self.assertEqual(status2, 201)
        self.assertEqual(result2["generated"], [])

    def test_opentelemetry_traces_and_telemetry_endpoint(self) -> None:
        run = self.launch_and_wait("fixed", "payment-response-lost")
        run_id = run["id"]

        status, telemetry = self.request("GET", f"/api/runs/{run_id}/telemetry")
        self.assertEqual(status, 200)
        self.assertIn("traceId", telemetry)
        self.assertEqual(len(telemetry["traceId"]), 32)
        self.assertGreaterEqual(telemetry["spanCount"], 4)
        span_names = [s["name"] for s in telemetry["spans"]]
        self.assertTrue(any("faultline.scenario" in n for n in span_names))
        self.assertTrue(any("fault.interceptor" in n for n in span_names))
        self.assertTrue(any("evaluator.invariants" in n for n in span_names))
        self.assertTrue(any("environment.cleanup" in n for n in span_names))

        # Standard OTLP payload check
        self.assertIn("resourceSpans", telemetry)
        resource_attrs = telemetry["resourceSpans"][0]["resource"]["attributes"]
        attr_keys = [a["key"] for a in resource_attrs]
        self.assertIn("service.name", attr_keys)
        self.assertIn("run.id", attr_keys)

        # Alias /traces endpoint
        status_traces, traces = self.request("GET", f"/api/runs/{run_id}/traces")
        self.assertEqual(status_traces, 200)
        self.assertEqual(traces["traceId"], telemetry["traceId"])

    def test_checkpoint_branching_creates_linked_run(self) -> None:
        # Run vulnerable first (which produces a failed run)
        parent_run = self.launch_and_wait("vulnerable", "payment-response-lost")
        parent_id = parent_run["id"]
        self.assertEqual(parent_run["verdict"], "failed")

        # 1. Invalid implementation rejected with 400
        status, bad_impl = self.request("POST", f"/api/runs/{parent_id}/branch", {
            "overrides": {"implementation": "invalid_mode"}
        })
        self.assertEqual(status, 400)
        self.assertIn("invalid implementation", bad_impl.get("error", "").lower())

        # 2. Invalid scenario override rejected with 400
        status, bad_override = self.request("POST", f"/api/runs/{parent_id}/branch", {
            "overrides": {"scenario_overrides": {"retries": 99}}
        })
        self.assertEqual(status, 400)

        # 3. Non-existent snapshot sequence rejected with 404 (no silent fallback)
        status, bad_seq = self.request("POST", f"/api/runs/{parent_id}/branch", {
            "snapshot_sequence": 9999,
            "overrides": {"implementation": "fixed"},
        })
        self.assertEqual(status, 404)
        self.assertIn("not found", bad_seq.get("error", "").lower())

        # 4. Valid branch from checkpoint snapshot with 'fixed' override and scenario override
        branch_payload = {
            "snapshot_sequence": 1,
            "overrides": {
                "implementation": "fixed",
                "scenario_overrides": {"retries": 2},
            },
        }
        status, branch_data = self.request("POST", f"/api/runs/{parent_id}/branch", branch_payload)
        self.assertEqual(status, 201)
        branched = branch_data["run"]
        self.assertEqual(branched["parent_run_id"], parent_id)
        self.assertEqual(branched["forked_from_snapshot_id"], 1)
        self.assertEqual(branched["implementation"], "fixed")
        self.assertEqual(branched["scenario"]["retries"], 2)

        # 5. Reproduction bundle captures branch metadata and effective overrides
        status, repro_data = self.request("GET", f"/api/runs/{branched['id']}/reproduce")
        self.assertEqual(status, 200)
        bundle = repro_data
        self.assertIn("branch", bundle)
        self.assertEqual(bundle["branch"]["parent_run_id"], parent_id)
        self.assertEqual(bundle["branch"]["forked_from_snapshot_id"], 1)
        self.assertEqual(bundle["branch"]["effective_scenario"]["retries"], 2)

        # 6. Await completion of branched run
        final_branched = self.wait_for_run(branched["id"])
        self.assertEqual(final_branched["verdict"], "passed")
        self.assertEqual(final_branched["scenario"]["retries"], 2)

        # 7. Verify parent run remains unchanged
        status, parent_check = self.request("GET", f"/api/runs/{parent_id}")
        self.assertEqual(status, 200)
        self.assertEqual(parent_check["run"]["verdict"], "failed")

    def test_suite_runs_lifecycle_and_history(self) -> None:
        # 1. Execute suite synchronously
        suite_id = "critical-integrations"
        status, suite_res = self.request("POST", f"/api/suites/{suite_id}/runs", {
            "implementation": "fixed",
            "synchronous": True,
        })
        self.assertEqual(status, 200)
        run_summary = suite_res["suite_run"]
        self.assertEqual(run_summary["verdict"], "passed")
        self.assertEqual(run_summary["total_scenarios"], 3)
        self.assertEqual(run_summary["passed_scenarios"], 3)
        self.assertEqual(run_summary["failed_scenarios"], 0)

        # 2. Query suite results history
        status, results_data = self.request("GET", f"/api/suites/{suite_id}/results")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(results_data["results"]), 1)
        latest = results_data["results"][0]
        self.assertEqual(latest["verdict"], "passed")
        self.assertEqual(latest["pass_rate"], 100.0)

        # 3. Add scenario to suite (using an isolated suite)
        custom_suite_id = f"custom-suite-{uuid4().hex[:6]}"
        self.request("POST", "/api/suites", {
            "id": custom_suite_id,
            "name": "Custom Isolated Suite",
            "scenarios": ["payment-response-lost"],
        })
        status, added = self.request("POST", f"/api/suites/{custom_suite_id}/scenarios", {
            "scenario_id": "duplicate-payment-webhook",
        })
        self.assertEqual(status, 201)
        self.assertEqual(added["suite_id"], custom_suite_id)

    def test_scenario_version_cloning_and_baseline_diff(self) -> None:
        scenario_id = "payment-response-lost"

        # 1. Clone scenario version
        status, cloned = self.request("POST", f"/api/scenarios/{scenario_id}/versions/1/clone", {
            "target_scenario_id": f"clone-{uuid4().hex[:6]}",
            "name_suffix": " (Cloned Test)",
        })
        self.assertEqual(status, 201)
        self.assertIn("scenario_id", cloned)
        self.assertEqual(cloned["version"], 1)

        # 2. Run a scenario and designate as baseline
        run = self.launch_and_wait("fixed", scenario_id)
        run_id = run["id"]
        status, base_res = self.request("POST", f"/api/runs/{run_id}/baseline", {})
        self.assertEqual(status, 200)
        self.assertTrue(base_res["is_baseline"])

        # 3. Query baseline diff
        status, diff_data = self.request("GET", f"/api/scenarios/{scenario_id}/baseline-diff")
        self.assertEqual(status, 200)
        self.assertTrue(diff_data["has_baseline"])
        self.assertEqual(diff_data["baseline_run_id"], run_id)
        self.assertIn("regression_status", diff_data)

    def test_operational_metrics_endpoint(self) -> None:
        # JSON metrics endpoint
        status, metrics = self.request("GET", "/api/metrics")
        self.assertEqual(status, 200)
        self.assertIn("uptime_seconds", metrics)
        self.assertGreaterEqual(metrics["uptime_seconds"], 0)
        self.assertEqual(metrics["worker_health"]["status"], "healthy")
        self.assertIn("queue_depth", metrics["worker_health"])
        self.assertIn("runs", metrics)
        self.assertIn("completed", metrics["runs"])
        self.assertIn("total", metrics["runs"])
        self.assertIn("assertions", metrics)
        self.assertIn("pass_rate_percent", metrics["assertions"])
        self.assertIn("suites", metrics)
        self.assertIn("inventory", metrics)
        self.assertGreaterEqual(metrics["inventory"]["scenarios"], 1)

        # Prometheus format via query parameter
        status, prom_text = self.request("GET", "/api/metrics?format=prometheus")
        self.assertEqual(status, 200)
        self.assertIsInstance(prom_text, str)
        self.assertIn("faultline_runs_total", prom_text)
        self.assertIn("faultline_assertions_total", prom_text)
        self.assertIn("faultline_queue_depth", prom_text)
        self.assertIn("faultline_uptime_seconds", prom_text)

        # Prometheus format via Accept header
        status, prom_header_text = self.request("GET", "/api/metrics", headers={"Accept": "text/plain"})
        self.assertEqual(status, 200)
        self.assertIsInstance(prom_header_text, str)
        self.assertIn("faultline_runs_total", prom_header_text)

    def test_artifact_retention_and_pruning_policy(self) -> None:
        # 1. Run two scenarios
        run_ephemeral = self.launch_and_wait("fixed")
        run_baseline = self.launch_and_wait("fixed")
        eph_id = run_ephemeral["id"]
        base_id = run_baseline["id"]

        # 2. Designate second run as baseline
        status, _ = self.request("POST", f"/api/runs/{base_id}/baseline", {})
        self.assertEqual(status, 200)

        # 3. Dry run retention prune (max_age_days=0, keep_baselines=True)
        status, dry_res = self.request("POST", "/api/projects/faultline-demo/prune", {
            "max_age_days": 0,
            "keep_baselines": True,
            "dry_run": True,
        })
        self.assertEqual(status, 200)
        self.assertTrue(dry_res["dry_run"])
        self.assertIn(eph_id, dry_res["pruned_run_ids"])
        self.assertNotIn(base_id, dry_res["pruned_run_ids"])
        self.assertGreaterEqual(dry_res["skipped_baselines"], 1)

        # Verify run is NOT deleted during dry run
        status, eph_check = self.request("GET", f"/api/runs/{eph_id}")
        self.assertEqual(status, 200)

        # 4. Actual execution of retention prune
        status, prune_res = self.request("POST", "/api/projects/faultline-demo/prune", {
            "max_age_days": 0,
            "keep_baselines": True,
            "dry_run": False,
        })
        self.assertEqual(status, 200)
        self.assertFalse(prune_res["dry_run"])
        self.assertIn(eph_id, prune_res["pruned_run_ids"])
        self.assertNotIn(base_id, prune_res["pruned_run_ids"])

        # Ephemeral run is pruned
        status, _ = self.request("GET", f"/api/runs/{eph_id}")
        self.assertEqual(status, 404)

        # Baseline run is preserved
        status, base_check = self.request("GET", f"/api/runs/{base_id}")
        self.assertEqual(status, 200)

        # 5. Check PROJECT_PRUNED audit event recorded
        status, audit = self.request("GET", "/api/projects/faultline-demo/audit-events")
        self.assertEqual(status, 200)
        pruned_events = [e for e in audit["audit_events"] if e["action"] == "PROJECT_PRUNED"]
        self.assertGreaterEqual(len(pruned_events), 1)

        # 6. Non-existent project returns 404
        status, err = self.request("POST", "/api/projects/non-existent-proj/prune", {"max_age_days": 30})
        self.assertEqual(status, 404)

    def test_all_scenario_templates_validate_and_instantiate(self) -> None:
        """Verify that every built-in template satisfies validate_scenario and can be instantiated."""
        status, data = self.request("GET", "/api/templates")
        self.assertEqual(status, 200)
        templates = data["templates"]
        self.assertEqual(len(templates), 3)

        for tpl in templates:
            t_id = tpl["id"]
            d_def = tpl["default_definition"]
            # 1. Default definition must validate cleanly
            server.validate_scenario(d_def)

            # 2. Instantiate template into a distinct scenario
            new_sc_id = f"test-inst-{t_id[:12]}"
            status, inst_data = self.request("POST", f"/api/templates/{t_id}/instantiate", {
                "scenario_id": new_sc_id,
            })
            self.assertEqual(status, 201)
            inst_def = inst_data["instance"]["definition"]
            # 3. Instantiated definition must validate cleanly
            server.validate_scenario(inst_def)

    def test_state_reconstruction_input_validation(self) -> None:
        """Verify that malformed or negative sequence parameters return 400 Bad Request."""
        run = self.launch_and_wait("fixed", "payment-response-lost")
        run_id = run["id"]

        # Valid sequence returns 200
        status, data = self.request("GET", f"/api/runs/{run_id}/state?sequence=1")
        self.assertEqual(status, 200)
        self.assertIn("state", data)

        # Malformed non-integer sequence returns 400
        status, data = self.request("GET", f"/api/runs/{run_id}/state?sequence=not-a-number")
        self.assertEqual(status, 400)
        self.assertIn("error", data)

        # Negative sequence returns 400
        status, data = self.request("GET", f"/api/runs/{run_id}/state?sequence=-5")
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_state_reconstruction_boundary_clamping(self) -> None:
        """Verify that requesting sequences beyond latest recorded event clamps to max sequence."""
        run = self.launch_and_wait("fixed", "payment-response-lost")
        run_id = run["id"]

        status, data = self.request("GET", f"/api/runs/{run_id}/state?sequence=99999")
        self.assertEqual(status, 200)
        state = data["state"]
        self.assertLess(state["sequence"], 99999)
        self.assertEqual(state["sequence"], state["max_sequence"])
        self.assertEqual(state["requested_sequence"], 99999)

    def test_request_body_validation_and_size_limits(self) -> None:
        """Verify uniform 16KB limit, negative length rejection, and JSON parsing on POST & PATCH."""
        import http.client

        # 1. Negative Content-Length on POST rejected with 400
        conn = http.client.HTTPConnection("127.0.0.1", self.api_port, timeout=5)
        conn.putrequest("POST", "/api/runs")
        conn.putheader("Content-Length", "-1")
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        conn.close()

        # 2. Negative Content-Length on PATCH rejected with 400
        conn = http.client.HTTPConnection("127.0.0.1", self.api_port, timeout=5)
        conn.putrequest("PATCH", "/api/tasks/tsk_dummy")
        conn.putheader("Content-Length", "-1")
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        conn.close()

        # 3. Oversized POST (>16KB) rejected with 413
        huge_payload = {"padding": "x" * 20000}
        status, _ = self.request("POST", "/api/runs", huge_payload)
        self.assertEqual(status, 413)

        # 4. Oversized PATCH (>16KB) rejected with 413
        status, _ = self.request("PATCH", "/api/tasks/tsk_dummy", huge_payload)
        self.assertEqual(status, 413)

        # 5. Malformed JSON on POST rejected with 400
        conn = http.client.HTTPConnection("127.0.0.1", self.api_port, timeout=5)
        conn.request("POST", "/api/runs", body=b"{malformed json", headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        conn.close()

    def test_redaction_at_persistence_boundaries(self) -> None:
        """Verify that annotations, tasks, and cleanup errors redact sensitive keys and patterns."""
        run = self.launch_and_wait("fixed", "payment-response-lost")
        run_id = run["id"]

        # 1. Annotations redact secret keys
        ann = store.add_annotation(run_id, "auditor", "Authorization: Bearer my-top-secret-token")
        self.assertNotIn("my-top-secret-token", ann["comment"])
        self.assertIn("Bearer [REDACTED]", ann["comment"])

        # 2. Tasks redact passwords and card numbers
        tsk = store.create_task(
            "faultline-demo",
            run_id=run_id,
            title="Fix password supersecretpass123 in order service",
            description="Card 4111 2222 3333 4444 observed in unmasked log",
        )
        self.assertNotIn("supersecretpass123", tsk["title"])
        self.assertNotIn("4111 2222 3333 4444", tsk["description"])

        # 3. Cleanup error redaction
        store.record_cleanup(
            run_id,
            "failed",
            ["res1"],
            error="DB error with password=mysecretpass and card 4111 2222 3333 4444",
        )
        db_run = store.get_run(run_id)
        cleanup_data = db_run.get("cleanup", {})
        self.assertNotIn("mysecretpass", str(cleanup_data))
        self.assertNotIn("4111 2222 3333 4444", str(cleanup_data))

    def test_security_loopback_and_url_boundary_validation(self) -> None:
        """Verify robust URL parsing, scheme checking, credential rejection, and wildcard rejection."""
        # 1. 0.0.0.0 must be rejected
        self.assertFalse(security.is_loopback_allowed("0.0.0.0"))
        self.assertFalse(security.is_loopback_allowed("http://0.0.0.0:8000"))
        self.assertFalse(security.is_loopback_allowed("0.0.0.0:8766"))

        # 2. Valid loopbacks
        self.assertTrue(security.is_loopback_allowed("127.0.0.1"))
        self.assertTrue(security.is_loopback_allowed("127.0.0.1:8000"))
        self.assertTrue(security.is_loopback_allowed("http://127.0.0.1:8000"))
        self.assertTrue(security.is_loopback_allowed("localhost"))
        self.assertTrue(security.is_loopback_allowed("localhost:8766"))
        self.assertTrue(security.is_loopback_allowed("http://localhost:8766"))
        self.assertTrue(security.is_loopback_allowed("::1"))
        self.assertTrue(security.is_loopback_allowed("[::1]:8000"))
        self.assertTrue(security.is_loopback_allowed("http://[::1]:8000"))

        # 3. Invalid schemes must be rejected
        self.assertFalse(security.is_loopback_allowed("ftp://127.0.0.1:21"))
        self.assertFalse(security.is_loopback_allowed("file:///etc/passwd"))
        self.assertFalse(security.is_loopback_allowed("gopher://127.0.0.1"))

        # 4. Embedded credentials in URLs must be rejected
        self.assertFalse(security.is_loopback_allowed("http://admin:secret@127.0.0.1:8000"))
        self.assertFalse(security.is_loopback_allowed("http://user@localhost:8766"))

        # 5. External domains must be rejected
        self.assertFalse(security.is_loopback_allowed("http://evil.com"))
        self.assertFalse(security.is_loopback_allowed("http://169.254.169.254"))
        self.assertFalse(security.is_loopback_allowed("http://8.8.8.8"))

        # 6. Port boundary and malformed port tests
        self.assertFalse(security.is_loopback_allowed("http://localhost:99999"))
        self.assertFalse(security.is_loopback_allowed("localhost:99999"))
        self.assertFalse(security.is_loopback_allowed("http://127.0.0.1:0"))
        self.assertFalse(security.is_loopback_allowed("http://127.0.0.1:65536"))
        self.assertFalse(security.is_loopback_allowed("[::1]:99999"))
        self.assertFalse(security.is_loopback_allowed("http://[::1]:99999"))

        # 7. Container service port allowlists
        with patch.object(security, "CONTAINER_ALLOWED_HOSTS", {"checkout-service", "payment-provider", "toxiproxy", "redis"}):
            self.assertTrue(security.is_loopback_allowed("http://checkout-service:8766/v1/checkout"))
            self.assertFalse(security.is_loopback_allowed("http://checkout-service:9000/v1/checkout"))
            self.assertTrue(security.is_loopback_allowed("payment-provider:8767"))
            self.assertFalse(security.is_loopback_allowed("payment-provider:8080"))
            self.assertTrue(security.is_loopback_allowed("http://toxiproxy:8474"))
            self.assertFalse(security.is_loopback_allowed("http://toxiproxy:22"))
            self.assertTrue(security.is_loopback_allowed("redis:6379"))
            self.assertFalse(security.is_loopback_allowed("redis:80"))

            # Explicit port argument enforcement
            self.assertTrue(security.is_loopback_allowed("checkout-service", port=8766))
            self.assertFalse(security.is_loopback_allowed("checkout-service", port=9000))
            self.assertFalse(security.is_loopback_allowed("checkout-service"))
            security.assert_loopback_safe("checkout-service", port=8766)
            with self.assertRaises(PermissionError):
                security.assert_loopback_safe("checkout-service", port=9000)
            with self.assertRaises(PermissionError):
                security.assert_loopback_safe("checkout-service")

        # 8. assert_loopback_safe raises PermissionError
        with self.assertRaises(PermissionError):
            security.assert_loopback_safe("http://evil.com")
        with self.assertRaises(PermissionError):
            security.assert_loopback_safe("0.0.0.0")

    def test_request_transfer_encoding_chunked_rejected_with_501(self) -> None:
        """Verify that requests with unsupported Transfer-Encoding are rejected with HTTP 501."""
        conn = http.client.HTTPConnection("127.0.0.1", self.api_port)
        conn.putrequest("POST", "/api/runs")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.putheader("Content-Type", "application/json")
        conn.endheaders()
        body = b"0\r\n\r\n"
        conn.send(body)
        resp = conn.getresponse()
        self.assertEqual(resp.status, 501)
        resp_data = json.loads(resp.read().decode("utf-8"))
        self.assertIn("unsupported Transfer-Encoding", resp_data.get("error", ""))
        conn.close()

    def test_security_redaction_nested_structures(self) -> None:
        """Verify recursive sensitive data redaction on nested dicts, lists, and strings."""
        payload = {
            "order_id": "order-123",
            "password": "super-secret-pass",
            "token": "tok_live_123456",
            "nested": {
                "authorization": "Bearer secret-token-xyz.123",
                "api_key": "sk_test_51234567890abcdef",
                "card_number": "4111 2222 3333 4444",
                "safe_list": ["item1", "Bearer my-jwt-token-val"],
            },
        }
        sanitized = security.redact_sensitive(payload)
        self.assertEqual(sanitized["order_id"], "order-123")
        self.assertEqual(sanitized["password"], "[REDACTED]")
        self.assertEqual(sanitized["token"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["authorization"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["api_key"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["card_number"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["safe_list"][0], "item1")
        self.assertIn("[REDACTED]", sanitized["nested"]["safe_list"][1])

    def test_template_expected_invariants_match_actual_run_assertions(self) -> None:
        """Verify that every template's expected_invariants exactly matches the assertions emitted by a completed run."""
        status, tpl_data = self.request("GET", "/api/templates")
        self.assertEqual(status, 200)
        templates = tpl_data["templates"]
        self.assertGreaterEqual(len(templates), 3)

        for tpl in templates:
            t_id = tpl["id"]
            inst_sc_id = f"tpl-inv-check-{uuid4().hex[:6]}"
            inst_status, _ = self.request("POST", f"/api/templates/{t_id}/instantiate", {
                "scenario_id": inst_sc_id,
            })
            self.assertEqual(inst_status, 201)
            # Run the instantiated scenario in fixed mode
            run = self.launch_and_wait("fixed", inst_sc_id)
            self.assertEqual(run["verdict"], "passed", f"Scenario {inst_sc_id} from {t_id} failed in fixed mode")
            emitted_names = [a["name"] for a in run["assertions"]]
            self.assertEqual(
                tpl["expected_invariants"],
                emitted_names,
                f"Template {t_id} expected_invariants do not match emitted run assertions",
            )

    def test_unknown_fault_fails_closed_in_engine(self) -> None:
        """Verify engine fails closed on unknown fault type without falling back to payment execution."""
        from faultline import engine
        from faultline.adapters import provider, checkout_service

        run_id = f"test-unknown-fault-{uuid4().hex[:6]}"
        bad_scenario = {
            "name": "unsupported-fault-run",
            "scenario_version": 1,
            "fault": "unsupported_unknown_fault",
            "assertion": "some_assertion",
        }
        store.create_run(run_id, "payment-response-lost", 1, "vulnerable")

        # Execute run directly
        engine.execute_run(run_id, bad_scenario, "vulnerable")

        run = store.get_run(run_id)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["verdict"], "error")
        self.assertIn("unsupported fault type 'unsupported_unknown_fault'", run.get("error", ""))

        # Verify no payment side-effects occurred
        self.assertEqual(provider.ledger(run_id), [])
        self.assertEqual(checkout_service.ledger(run_id), [])

        # Verify events
        event_types = [e["type"] for e in run.get("events", [])]
        self.assertIn("RUN_EXECUTION_FAILED", event_types)
        self.assertNotIn("ASSERTIONS_EVALUATED", event_types)

    def test_generic_non_payment_scenario_execution(self) -> None:
        """Verify non-payment scenario (e.g. http_timeout with workflow steps) validates and executes cleanly."""
        from faultline import engine

        generic_def = {
            "scenario_version": 1,
            "name": "generic-service-timeout",
            "description": "Simulate network timeout on generic downstream HTTP service",
            "fault": "http_timeout",
            "assertion": "all_steps_succeeded_or_handled",
            "timeout_seconds": 10,
            "timeout_ms": 1500,
            "workflow": [
                {"id": "auth_token", "name": "Fetch Auth Token", "type": "http.post"},
                {"id": "call_service", "name": "Call Downstream Service", "type": "http.get"},
                {"id": "save_record", "name": "Persist Response Record", "type": "db.write"},
            ],
            "target_step": "call_service",
        }

        # 1. Validates cleanly without payment fields (order_id, amount_cents)
        server.validate_scenario(generic_def)

        # 2. Register scenario via API
        sc_id = f"gen-scen-{uuid4().hex[:6]}"
        status, res = self.request("POST", "/api/scenarios", {
            "definition": {**generic_def, "name": sc_id},
        })
        self.assertEqual(status, 201)

        # 3. Execute in vulnerable mode -> fails assertion due to simulated unhandled timeout
        vuln_run = self.launch_and_wait("vulnerable", sc_id)
        self.assertEqual(vuln_run["verdict"], "failed")
        vuln_events = [e["type"] for e in vuln_run.get("events", [])]
        self.assertIn("FAULT_INJECTED", vuln_events)
        self.assertIn("HTTP_REQUEST_SENT", vuln_events)
        self.assertIn("HTTP_TIMEOUT_OBSERVED", vuln_events)
        self.assertIn("STEP_FAILED", vuln_events)

        # 4. Execute in fixed mode -> recovers and passes
        fixed_run = self.launch_and_wait("fixed", sc_id)
        self.assertEqual(fixed_run["verdict"], "passed")
        fixed_events = [e["type"] for e in fixed_run.get("events", [])]
        self.assertIn("FAULT_INJECTED", fixed_events)
        self.assertIn("HTTP_REQUEST_SENT", fixed_events)
        self.assertIn("HTTP_TIMEOUT_OBSERVED", fixed_events)
        self.assertIn("STEP_RETRY", fixed_events)
        self.assertIn("HTTP_RESPONSE_RECEIVED", fixed_events)
        self.assertIn("STEP_COMPLETED", fixed_events)

        # Invariants
        assertions = {a["name"]: a["status"] for a in fixed_run["assertions"]}
        self.assertEqual(assertions.get("fault_activation_confirmed"), "passed")
        self.assertEqual(assertions.get("all_steps_succeeded_or_handled"), "passed")

    def test_registry_rejects_duplicate_registration(self) -> None:
        """Verify fault and step registries reject duplicate handler registrations."""
        from faultline.engine import fault_registry, step_registry

        # Duplicate fault registration raises ValueError
        with self.assertRaises(ValueError) as ctx:
            fault_registry.register("http_timeout", lambda *a: None, ["test"])
        self.assertIn("already registered", str(ctx.exception))

        # Duplicate step registration raises ValueError
        with self.assertRaises(ValueError) as ctx:
            step_registry.register("http.request", lambda *a: None)
        self.assertIn("already registered", str(ctx.exception))

    def test_schema_validates_step_types_and_target_step(self) -> None:
        """Verify schema rejects unknown step types and incompatible target steps for faults."""
        base_def = {
            "scenario_version": 1,
            "name": "test-step-val",
            "fault": "http_timeout",
            "assertion": "all_steps_succeeded_or_handled",
            "timeout_seconds": 10,
        }

        # 1. Unknown step type is rejected
        bad_type_def = dict(base_def, workflow=[
            {"id": "s1", "name": "Bad Step", "type": "unsupported.custom.type"},
        ])
        with self.assertRaises(ValueError) as ctx:
            server.validate_scenario(bad_type_def)
        self.assertIn("unsupported workflow step type", str(ctx.exception))

        # 2. Incompatible target_step for http_timeout (e.g. db.write instead of HTTP)
        incompatible_def = dict(base_def, workflow=[
            {"id": "db_step", "name": "Write DB", "type": "db.write"},
        ], target_step="db_step")
        with self.assertRaises(ValueError) as ctx:
            server.validate_scenario(incompatible_def)
        self.assertIn("is incompatible with fault 'http_timeout'", str(ctx.exception))

        # 3. Non-existent target_step is rejected
        nonexistent_def = dict(base_def, workflow=[
            {"id": "s1", "name": "Step 1", "type": "http.get"},
        ], target_step="s_does_not_exist")
        with self.assertRaises(ValueError) as ctx:
            server.validate_scenario(nonexistent_def)
        self.assertIn("unsupported target_step", str(ctx.exception))

    def test_prohibited_ports_security_enforcement(self) -> None:
        """Verify that loopback security check rejects dangerous/privileged service ports."""
        from faultline import security

        # Prohibited ports (SSH, Telnet, SMTP, DNS, SMB, etc.) must be rejected
        for port in [21, 22, 23, 25, 53, 110, 135, 139, 445]:
            self.assertFalse(security.is_loopback_allowed("127.0.0.1", port=port), f"Port {port} should be prohibited")
            self.assertFalse(security.is_loopback_allowed("localhost", port=port), f"Port {port} should be prohibited")

        # Approved application ports should be permitted
        self.assertTrue(security.is_loopback_allowed("127.0.0.1", port=8000))
        self.assertTrue(security.is_loopback_allowed("127.0.0.1", port=8080))
        self.assertTrue(security.is_loopback_allowed("localhost", port=3000))

    def test_https_strict_tls_verification(self) -> None:
        """Verify HTTPS connections enforce strict TLS context with CERT_REQUIRED and hostname checking."""
        import ssl
        from faultline.engine import _resolve_http_endpoint, _create_http_connection
        from http.client import HTTPSConnection

        scheme, host, port, path = _resolve_http_endpoint({"url": "https://127.0.0.1:8443/v1/health"})
        self.assertEqual(scheme, "https")
        self.assertEqual(host, "127.0.0.1")
        self.assertEqual(port, 8443)
        self.assertEqual(path, "/v1/health")

        conn = _create_http_connection("https", "127.0.0.1", 8443, 2.0)
        self.assertIsInstance(conn, HTTPSConnection)
        # Verify strict TLS certificate validation is enabled
        self.assertEqual(conn._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(conn._context.check_hostname)

    def test_redirect_loopback_validation(self) -> None:
        """Verify redirect validation allows loopback targets and rejects external/prohibited ports."""
        from faultline import security

        # Valid relative redirect
        target = security.validate_redirect("http://127.0.0.1:8766/v1/checkout", "/v1/receipt")
        self.assertEqual(target, "http://127.0.0.1:8766/v1/receipt")

        # Open redirect to external domain must be rejected
        with self.assertRaises(PermissionError):
            security.validate_redirect("http://127.0.0.1:8766/v1/checkout", "http://external-malicious.com/leak")

        # Redirect to prohibited port (e.g. SSH port 22) must be rejected
        with self.assertRaises(PermissionError):
            security.validate_redirect("http://127.0.0.1:8766/v1/checkout", "http://127.0.0.1:22/admin")

    def test_db_step_explicitly_labeled_as_simulation(self) -> None:
        """Verify database workflow steps are explicitly marked as simulations in events and side-effects."""
        sc_id = f"sim-db-{uuid4().hex[:6]}"
        db_scenario = {
            "scenario_version": 1,
            "name": sc_id,
            "description": "Simulated DB step execution",
            "fault": "http_timeout",
            "assertion": "all_steps_succeeded_or_handled",
            "timeout_seconds": 10,
            "workflow": [
                {"id": "db_step", "name": "Persist Order Record", "type": "db.write", "table": "orders"},
                {"id": "call_svc", "name": "Ping Service", "type": "http.get"},
            ],
            "target_step": "call_svc",
        }
        status, res = self.request("POST", "/api/scenarios", {"definition": db_scenario})
        self.assertEqual(status, 201)

        run = self.launch_and_wait("fixed", sc_id)
        self.assertEqual(run["verdict"], "passed")
        event_types = [e["type"] for e in run.get("events", [])]
        self.assertIn("DB_OPERATION_SIMULATED", event_types)
        db_events = [e for e in run.get("events", []) if e["type"] == "DB_OPERATION_SIMULATED"]
        self.assertTrue(all(e.get("payload", {}).get("simulated") is True for e in db_events))


if __name__ == "__main__":
    unittest.main()
