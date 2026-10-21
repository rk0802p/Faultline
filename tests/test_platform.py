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

from faultline import engine, store
from faultline.adapters import provider, start_local_services
from faultline import adapters
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
        connection = http.client.HTTPConnection("127.0.0.1", self.api_port, timeout=4)
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
        self.assertGreaterEqual(len(templates), 5)
        tpl_ids = [t["id"] for t in templates]
        self.assertIn("payment-response-lost-tpl", tpl_ids)
        self.assertIn("duplicate-payment-webhook-tpl", tpl_ids)
        self.assertIn("partial-persistence-failure-tpl", tpl_ids)

        # 2. Get specific template
        status, tpl_data = self.request("GET", "/api/templates/payment-response-lost-tpl")
        self.assertEqual(status, 200)
        self.assertEqual(tpl_data["template"]["category"], "network_failure")
        self.assertIn("parameters_schema", tpl_data["template"])

        # 3. Instantiate template
        inst_payload = {
            "scenario_id": f"inst-sc-{uuid4().hex[:6]}",
            "name": "Customized Payment Loss",
            "parameters": {"amount_cents": 9999, "retries": 2},
        }
        status, inst_result = self.request("POST", "/api/templates/payment-response-lost-tpl/instantiate", inst_payload)
        self.assertEqual(status, 201)
        self.assertEqual(inst_result["instance"]["scenario_id"], inst_payload["scenario_id"])
        self.assertEqual(inst_result["instance"]["definition"]["amount_cents"], 9999)

        # Verify scenario exists and is runnable
        status, scenarios_data = self.request("GET", "/api/scenarios")
        self.assertEqual(status, 200)
        sc_ids = [s["id"] for s in scenarios_data["scenarios"]]
        self.assertIn(inst_payload["scenario_id"], sc_ids)

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

    def test_generate_scenarios_from_coverage_gaps(self) -> None:
        status, result = self.request("POST", "/api/scenarios/generate-from-gaps", {"project_id": "faultline-demo"})
        self.assertEqual(status, 201)
        self.assertIn("generated", result)
        self.assertIsInstance(result["generated"], list)

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

        # Branch from checkpoint snapshot with 'fixed' override
        branch_payload = {
            "snapshot_sequence": 1,
            "overrides": {"implementation": "fixed"},
        }
        status, branch_data = self.request("POST", f"/api/runs/{parent_id}/branch", branch_payload)
        self.assertEqual(status, 201)
        branched = branch_data["run"]
        self.assertEqual(branched["parent_run_id"], parent_id)
        self.assertEqual(branched["forked_from_snapshot_id"], 1)
        self.assertEqual(branched["implementation"], "fixed")

        # Await completion of branched run
        final_branched = self.wait_for_run(branched["id"])
        self.assertEqual(final_branched["verdict"], "passed")

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


if __name__ == "__main__":
    unittest.main()
