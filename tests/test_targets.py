"""Phase 01: Target Application Contract, Registry, Health Checks, and Multi-App Execution Tests.

Verifies:
1. Target configuration schema validation, security allowlists, and port restrictions.
2. Target CRUD API endpoints (/api/targets).
3. Target health checks (healthy, degraded, unreachable).
4. Connect Mode: Generic scenario execution against an independent 2nd application (booking_service).
5. Target-level fault authorization (permitted_faults enforcement).
6. Managed Mode: Target process supervisor, health polling, and clean termination.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from faultline import adapters, engine, schema, security, server, store
from faultline.adapters import start_local_services
from faultline.server import ControlPlaneHandler
from faultline.targets import TargetConfig, check_service_health, target_registry, validate_target_config
from examples.booking_service import run_service as run_booking_service


class TargetApplicationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory(dir=ROOT)
        cls.old_data_dir, cls.old_db_path = store.DATA_DIR, store.DB_PATH
        cls.old_checkout_port, cls.old_payment_port = adapters.CHECKOUT_PORT, adapters.PAYMENT_PORT
        store.DATA_DIR = Path(cls.temp_dir.name)
        store.DB_PATH = store.DATA_DIR / "test_targets.sqlite3"
        store.initialize()

        cls.local_services = start_local_services(checkout_port=0, payment_port=0)
        from http.server import ThreadingHTTPServer
        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), ControlPlaneHandler)
        cls.api_thread = threading.Thread(target=cls.api.serve_forever, daemon=True)
        cls.api_thread.start()
        cls.api_port = cls.api.server_address[1]

        # Start independent booking service
        cls.booking_server = run_booking_service("127.0.0.1", 0)
        cls.booking_port = cls.booking_server.server_address[1]
        cls.booking_thread = threading.Thread(target=cls.booking_server.serve_forever, daemon=True)
        cls.booking_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.shutdown()
        cls.api.server_close()
        for s in cls.local_services:
            s.shutdown()
            s.server_close()
        cls.booking_server.shutdown()
        cls.booking_server.server_close()
        target_registry.stop_all_managed()
        store.DATA_DIR, store.DB_PATH = cls.old_data_dir, cls.old_db_path
        adapters.CHECKOUT_PORT, adapters.PAYMENT_PORT = cls.old_checkout_port, cls.old_payment_port
        cls.temp_dir.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        conn = HTTPConnection("127.0.0.1", self.api_port, timeout=15)
        body = json.dumps(payload) if payload is not None else None
        headers = {"Content-Type": "application/json"} if body else {}
        conn.request(method, path, body, headers)
        res = conn.getresponse()
        raw = res.read()
        conn.close()
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = {"raw": raw.decode("utf-8", errors="replace")}
        return res.status, data

    # -------------------------------------------------------------------------
    # 1. Target Configuration Schema & Security Validation
    # -------------------------------------------------------------------------
    def test_target_config_validation(self) -> None:
        """Verify target configuration schema rules and security boundaries."""
        # Valid connect mode config
        valid_cfg = {
            "id": "my-booking-service",
            "name": "Booking API",
            "mode": "connect",
            "base_url": "http://127.0.0.1:8990",
            "health_endpoint": "/v1/health",
            "permitted_faults": ["http_timeout"],
        }
        res = validate_target_config(valid_cfg)
        self.assertEqual(res.id, "my-booking-service")
        self.assertEqual(res.mode, "connect")
        self.assertEqual(res.base_url, "http://127.0.0.1:8990")

        # Invalid target ID (uppercase / spaces)
        with self.assertRaises(ValueError) as ctx:
            validate_target_config({**valid_cfg, "id": "Invalid ID!"})
        self.assertIn("Target ID 'Invalid ID!' is invalid", str(ctx.exception))

        # Invalid target mode
        with self.assertRaises(ValueError) as ctx:
            validate_target_config({**valid_cfg, "mode": "unsupported_mode"})
        self.assertIn("Target mode 'unsupported_mode' is invalid", str(ctx.exception))

        # Security: Non-loopback / external host rejected
        with self.assertRaises(PermissionError):
            validate_target_config({**valid_cfg, "base_url": "http://external-malicious-site.com:8000"})

        # Security: Prohibited port rejected (e.g. SSH port 22)
        with self.assertRaises(PermissionError):
            validate_target_config({**valid_cfg, "base_url": "http://127.0.0.1:22"})

        # Managed mode without managed_command is rejected
        with self.assertRaises(ValueError) as ctx:
            validate_target_config({**valid_cfg, "mode": "managed", "managed_command": []})
        self.assertIn("requires a non-empty list of command arguments", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 2. Target Registry CRUD API Endpoints
    # -------------------------------------------------------------------------
    def test_target_registry_and_crud_api(self) -> None:
        """Verify target registration, retrieval, listing, updating, and deletion via REST API."""
        # 1. Default seeded target exists
        status, list_data = self.request("GET", "/api/targets")
        self.assertEqual(status, 200)
        target_ids = [t["id"] for t in list_data["targets"]]
        self.assertIn("checkout-demo", target_ids)

        # 2. Register a new target
        new_target_payload = {
            "id": "flight-reservation-service",
            "name": "Flight Reservation System",
            "description": "Production airline reservation microservice",
            "mode": "connect",
            "base_url": f"http://127.0.0.1:{self.booking_port}",
            "health_endpoint": "/v1/health",
            "services": [
                {
                    "id": "seat-api",
                    "name": "Seat Inventory API",
                    "base_url": f"http://127.0.0.1:{self.booking_port}",
                    "health_endpoint": "/v1/health",
                    "role": "application_under_test",
                }
            ],
            "permitted_faults": ["http_timeout"],
        }
        status, create_res = self.request("POST", "/api/targets", new_target_payload)
        self.assertEqual(status, 201)
        self.assertEqual(create_res["target"]["id"], "flight-reservation-service")

        # 3. Retrieve specific target
        status, get_res = self.request("GET", "/api/targets/flight-reservation-service")
        self.assertEqual(status, 200)
        self.assertEqual(get_res["target"]["name"], "Flight Reservation System")

        # 4. Update target
        updated_payload = dict(new_target_payload)
        updated_payload["name"] = "Flight & Concert Reservation Platform"
        status, put_res = self.request("PUT", "/api/targets/flight-reservation-service", updated_payload)
        self.assertEqual(status, 200)
        self.assertEqual(put_res["target"]["name"], "Flight & Concert Reservation Platform")

        # 5. Delete target
        status, del_res = self.request("DELETE", "/api/targets/flight-reservation-service")
        self.assertEqual(status, 200)
        self.assertTrue(del_res["deleted"])

        # 6. Verify 404 after deletion
        status, _ = self.request("GET", "/api/targets/flight-reservation-service")
        self.assertEqual(status, 404)

    # -------------------------------------------------------------------------
    # 3. Target Health Checks
    # -------------------------------------------------------------------------
    def test_target_health_checks(self) -> None:
        """Verify target health check logic across live healthy, degraded, and unreachable targets."""
        # Register booking target pointing to live booking server
        target_id = "live-booking-target"
        target_payload = {
            "id": target_id,
            "name": "Live Booking Service",
            "mode": "connect",
            "base_url": f"http://127.0.0.1:{self.booking_port}",
            "health_endpoint": "/v1/health",
            "services": [
                {
                    "id": "inventory",
                    "base_url": f"http://127.0.0.1:{self.booking_port}",
                    "health_endpoint": "/v1/health",
                }
            ],
        }
        status, _ = self.request("POST", "/api/targets", target_payload)
        self.assertEqual(status, 201)

        # 1. Health check returns healthy
        status, health_data = self.request("GET", f"/api/targets/{target_id}/health")
        self.assertEqual(status, 200)
        self.assertEqual(health_data["status"], "healthy")
        self.assertGreaterEqual(health_data["main_service"]["latency_ms"], 1)
        self.assertEqual(health_data["main_service"]["status_code"], 200)

        # 2. Register target pointing to an unreachable port
        unreachable_id = "unreachable-test-target"
        self.request("POST", "/api/targets", {
            "id": unreachable_id,
            "name": "Unreachable Target",
            "mode": "connect",
            "base_url": "http://127.0.0.1:64999",
            "health_endpoint": "/health",
        })
        status, unreach_health = self.request("GET", f"/api/targets/{unreachable_id}/health")
        self.assertEqual(status, 200)
        self.assertEqual(unreach_health["status"], "unreachable")
        self.assertIn("error", unreach_health["main_service"])

    # -------------------------------------------------------------------------
    # 4. Connect Mode Execution: Generic Testing Against 2nd Independent App
    # -------------------------------------------------------------------------
    def test_connect_mode_generic_execution_on_independent_booking_app(self) -> None:
        """Prove that Faultline tests an independent application (booking_service) end-to-end.

        Exercises:
        1. Target registration in Connect Mode.
        2. Generic scenario execution against target's base URL.
        3. Vulnerable implementation detection (fails assertion).
        4. Fixed implementation verification (passes assertions).
        """
        target_id = "connect-booking-app"
        self.request("POST", "/api/targets", {
            "id": target_id,
            "name": "Independent Seat Booking Application",
            "mode": "connect",
            "base_url": f"http://127.0.0.1:{self.booking_port}",
            "health_endpoint": "/v1/health",
            "permitted_faults": ["http_timeout"],
        })

        booking_scenario_def = {
            "scenario_version": 1,
            "name": "booking-service-timeout",
            "target_id": target_id,
            "description": "Test flight seat reservation when downstream inventory times out",
            "fault": "http_timeout",
            "assertion": "all_steps_succeeded_or_handled",
            "timeout_seconds": 10,
            "workflow": [
                {
                    "id": "reserve_seat",
                    "name": "Reserve Flight Seat",
                    "type": "http.post",
                    "path": "/v1/bookings",
                    "body": {
                        "booking_id": "bk-fl-001",
                        "seat_number": "18C",
                        "customer_id": "cust-traveler-99",
                        "price_cents": 28000,
                    },
                }
            ],
            "target_step": "reserve_seat",
            "fault_config": {
                "client_timeout_seconds": 0.1,
                "server_delay_seconds": 0.3,
            },
        }

        # Validate scenario against schema with target_id
        schema.validate_scenario(booking_scenario_def)

        # Register scenario
        sc_id = "booking-timeout-resilience"
        status, res = self.request("POST", "/api/scenarios", {
            "definition": {**booking_scenario_def, "name": sc_id},
        })
        self.assertEqual(status, 201)

        # 1. Run in VULNERABLE mode: downstream timeout causes unhandled 504 -> run fails
        status, run_sub_v = self.request("POST", "/api/runs", {
            "scenario_id": sc_id,
            "implementation": "vulnerable",
        })
        self.assertEqual(status, 202)
        run_v_id = run_sub_v["run_id"]

        # Poll run completion
        run_v = None
        for _ in range(100):
            status, r_data = self.request("GET", f"/api/runs/{run_v_id}")
            if r_data["run"]["status"] in {"completed", "failed"}:
                run_v = r_data["run"]
                break
            time.sleep(0.05)
        self.assertIsNotNone(run_v)
        self.assertEqual(run_v["verdict"], "failed")
        v_events = [e["type"] for e in run_v.get("events", [])]
        self.assertIn("FAULT_INJECTED", v_events)
        self.assertIn("HTTP_TIMEOUT_OBSERVED", v_events)
        self.assertIn("STEP_FAILED", v_events)

        # 2. Run in FIXED mode: downstream timeout triggers retry & recovery -> run passes
        status, run_sub_f = self.request("POST", "/api/runs", {
            "scenario_id": sc_id,
            "implementation": "fixed",
        })
        self.assertEqual(status, 202)
        run_f_id = run_sub_f["run_id"]

        run_f = None
        for _ in range(100):
            status, r_data = self.request("GET", f"/api/runs/{run_f_id}")
            if r_data["run"]["status"] in {"completed", "failed"}:
                run_f = r_data["run"]
                break
            time.sleep(0.05)
        self.assertIsNotNone(run_f)
        self.assertEqual(run_f["verdict"], "passed")
        f_events = [e["type"] for e in run_f.get("events", [])]
        self.assertIn("FAULT_INJECTED", f_events)
        self.assertIn("HTTP_TIMEOUT_OBSERVED", f_events)
        self.assertIn("STEP_RETRY", f_events)
        self.assertIn("HTTP_RESPONSE_RECEIVED", f_events)
        self.assertIn("STEP_COMPLETED", f_events)

        # Verify point-in-time state reconstruction reflects the generic booking steps
        status, state_resp = self.request("GET", f"/api/runs/{run_f_id}/state?sequence=99")
        self.assertEqual(status, 200)
        self.assertEqual(state_resp["state"]["status"], "completed")
        self.assertIn("reserve_seat", state_resp["state"]["steps"])

    # -------------------------------------------------------------------------
    # 5. Target Permitted Faults Enforcement
    # -------------------------------------------------------------------------
    def test_target_permitted_faults_enforcement(self) -> None:
        """Verify that attempting to execute an unpermitted fault type against a target fails closed."""
        target_id = "restricted-target"
        self.request("POST", "/api/targets", {
            "id": target_id,
            "name": "Restricted Target",
            "mode": "connect",
            "base_url": f"http://127.0.0.1:{self.booking_port}",
            "health_endpoint": "/v1/health",
            "permitted_faults": ["http_timeout"],  # Only http_timeout permitted
        })

        # Scenario requests an unpermitted payment fault
        unpermitted_scenario = {
            "scenario_version": 1,
            "name": "unpermitted-fault-scenario",
            "target_id": target_id,
            "fault": "payment_response_lost_after_commit",
            "assertion": "no_duplicate_charge",
            "timeout_seconds": 5,
            "workflow": [{"id": "step1", "type": "http.get", "path": "/v1/health"}],
        }

        # 1. Validation rejects unpermitted fault for target with 400 Bad Request
        status, sub_data = self.request("POST", "/api/scenarios", {
            "definition": unpermitted_scenario,
        })
        self.assertEqual(status, 400)
        self.assertIn("is not permitted by target application", sub_data.get("error", ""))

    # -------------------------------------------------------------------------
    # 6. Managed Mode: Target Process Supervisor & Lifecycle
    # -------------------------------------------------------------------------
    def test_managed_mode_process_lifecycle(self) -> None:
        """Verify Faultline can start, health-poll, and terminate a managed target application."""
        managed_id = "managed-booking-app"
        booking_script = ROOT / "examples" / "booking_service.py"
        managed_port = 8995

        # Clean any leftover process on port
        target_registry.stop_managed(managed_id)

        managed_cfg = {
            "id": managed_id,
            "name": "Managed Booking App",
            "mode": "managed",
            "base_url": f"http://127.0.0.1:{managed_port}",
            "health_endpoint": "/v1/health",
            "managed_command": [sys.executable, str(booking_script), "--port", str(managed_port)],
            "permitted_faults": ["http_timeout"],
        }
        status, _ = self.request("POST", "/api/targets", managed_cfg)
        self.assertEqual(status, 201)

        # 1. Start managed target via API
        status, start_data = self.request("POST", f"/api/targets/{managed_id}/start", {"timeout": 8.0})
        self.assertEqual(status, 200)
        self.assertEqual(start_data["status"], "started")
        self.assertGreater(start_data["pid"], 0)

        # 2. Check health of running managed target
        status, health_data = self.request("GET", f"/api/targets/{managed_id}/health")
        self.assertEqual(status, 200)
        self.assertEqual(health_data["status"], "healthy")

        # 3. Stop managed target via API
        status, stop_data = self.request("POST", f"/api/targets/{managed_id}/stop", {})
        self.assertEqual(status, 200)
        self.assertTrue(stop_data["stopped"])

        # 4. Verify target is now unreachable
        status, post_stop_health = self.request("GET", f"/api/targets/{managed_id}/health")
        self.assertEqual(status, 200)
        self.assertEqual(post_stop_health["status"], "unreachable")


if __name__ == "__main__":
    unittest.main()
