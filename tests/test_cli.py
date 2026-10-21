"""Offline tests for the CLI's verdict and transport exit codes."""

import contextlib
import io
import sys
import unittest
from unittest.mock import patch

from faultline import cli


class FaultlineCliTests(unittest.TestCase):
    def run_cli(self, responses: list[dict], *, timeout: float = 2) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        args = ["faultline.cli", "run", "--scenario", "sample", "--implementation", "fixed",
                "--base-url", "http://faultline.test", "--timeout", str(timeout)]
        with patch.object(sys, "argv", args), patch("faultline.cli.api_json", side_effect=responses), \
                patch("faultline.cli.time.sleep"), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_maps_each_terminal_run_result_to_exit_code(self) -> None:
        cases = [
            ({"status": "completed", "verdict": "passed"}, 0),
            ({"status": "completed", "verdict": "failed"}, 1),
            ({"status": "failed", "verdict": "error"}, 2),
            ({"status": "timed_out", "verdict": "error"}, 3),
            ({"status": "cancelled", "verdict": "cancelled"}, 4),
        ]
        for run, expected in cases:
            with self.subTest(status=run["status"], verdict=run["verdict"]):
                code, output, _ = self.run_cli([{"run_id": "run-1"}, {"run": run}])
                self.assertEqual(code, expected)
                self.assertIn('"status"', output)

    def test_cli_reports_poll_deadline_and_api_failures(self) -> None:
        code, _, error = self.run_cli([{"run_id": "run-1"}, {"run": {"status": "running"}}], timeout=0)
        self.assertEqual(code, 3)
        self.assertIn("Timed out waiting", error)

        from urllib.error import URLError
        with patch.object(sys, "argv", ["faultline.cli", "run", "--scenario", "sample"]), \
                patch("faultline.cli.api_json", side_effect=URLError("offline")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as error_output:
            code = cli.main()
        self.assertEqual(code, 3)
        self.assertIn("Faultline API error", error_output.getvalue())

    def test_cli_debug_flag_outputs_diagnostic_logs(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        args = ["faultline.cli", "run", "--scenario", "sample", "--implementation", "fixed",
                "--base-url", "http://faultline.test", "--debug"]
        responses = [{"run_id": "run-debug"}, {"run": {"status": "completed", "verdict": "passed"}}]
        with patch.object(sys, "argv", args), patch("faultline.cli.api_json", side_effect=responses), \
                patch("faultline.cli.time.sleep"), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main()
        self.assertEqual(code, 0)
        self.assertIn("[debug] Submitting run:", stderr.getvalue())
        self.assertIn("[debug] Poll #1: status=completed", stderr.getvalue())

    def test_cli_suite_execution_and_exit_codes(self) -> None:
        suite_meta = {
            "suite": {
                "id": "critical-integrations",
                "name": "Critical Integrations",
                "scenarios": ["scenario-1", "scenario-2"],
            }
        }
        passing_run_1 = {"scenario_id": "scenario-1", "status": "completed", "verdict": "passed"}
        passing_run_2 = {"scenario_id": "scenario-2", "status": "completed", "verdict": "passed"}
        failing_run_2 = {
            "scenario_id": "scenario-2",
            "status": "completed",
            "verdict": "failed",
            "assertions": [{"name": "no_duplicate_charge", "status": "failed"}],
        }

        # Case 1: All scenarios pass -> exit 0
        responses_pass = [
            suite_meta,
            {"run_id": "r1"},
            {"run": passing_run_1},
            {"run_id": "r2"},
            {"run": passing_run_2},
        ]
        stdout, stderr = io.StringIO(), io.StringIO()
        args = ["faultline.cli", "run", "--suite", "critical-integrations", "--base-url", "http://faultline.test"]
        with patch.object(sys, "argv", args), patch("faultline.cli.api_json", side_effect=responses_pass), \
                patch("faultline.cli.time.sleep"), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main()
        self.assertEqual(code, 0)
        self.assertIn('"passed": 2', stdout.getvalue())

        # Case 2: One scenario has failed verdict -> exit 1
        responses_fail = [
            suite_meta,
            {"run_id": "r1"},
            {"run": passing_run_1},
            {"run_id": "r2"},
            {"run": failing_run_2},
        ]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", args), patch("faultline.cli.api_json", side_effect=responses_fail), \
                patch("faultline.cli.time.sleep"), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main()
        self.assertEqual(code, 1)
        self.assertIn('"failed": 1', stdout.getvalue())

    def test_cli_junit_xml_generation_and_output_file(self) -> None:
        import tempfile
        from pathlib import Path
        import xml.etree.ElementTree as ET

        run_result = {
            "scenario_id": "payment-response-lost",
            "status": "completed",
            "verdict": "failed",
            "started_at": "2026-10-21T10:00:00",
            "finished_at": "2026-10-21T10:00:02",
            "assertions": [
                {"name": "no_duplicate_charge", "status": "failed"},
                {"name": "fault_activation_confirmed", "status": "passed"},
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "reports" / "junit.xml"
            args = [
                "faultline.cli", "run",
                "--scenario", "payment-response-lost",
                "--report-format", "junit",
                "--output", str(out_file),
                "--base-url", "http://faultline.test",
            ]
            responses = [{"run_id": "r-junit"}, {"run": run_result}]
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(sys, "argv", args), patch("faultline.cli.api_json", side_effect=responses), \
                    patch("faultline.cli.time.sleep"), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main()
            self.assertEqual(code, 1)
            self.assertTrue(out_file.exists())
            tree = ET.parse(out_file)
            root = tree.getroot()
            self.assertEqual(root.tag, "testsuites")
            self.assertEqual(root.attrib.get("tests"), "1")
            self.assertEqual(root.attrib.get("failures"), "1")
            tc = root.find(".//testcase")
            self.assertIsNotNone(tc)
            self.assertEqual(tc.attrib.get("name"), "payment-response-lost")
            self.assertEqual(tc.attrib.get("time"), "2.000")
            fail_el = tc.find("failure")
            self.assertIsNotNone(fail_el)
            self.assertIn("1 invariant(s) failed", fail_el.attrib.get("message"))

    def test_cli_requires_either_scenario_or_suite(self) -> None:
        # Neither flag provided
        with patch.object(sys, "argv", ["faultline.cli", "run"]), \
                patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 2)

        # Both flags provided
        with patch.object(sys, "argv", ["faultline.cli", "run", "--scenario", "s1", "--suite", "su1"]), \
                patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 2)

    def test_cli_report_command_outputs_report(self) -> None:
        report_data = {
            "report": {
                "run_id": "run-rep-1",
                "scenario_id": "payment-response-lost",
                "scenario_name": "Payment response lost",
                "implementation": "fixed",
                "verdict": "passed",
                "duration_seconds": 1.25,
                "fault_analysis": {"fault_type": "payment_response_lost_after_commit", "activated": True, "time_to_fault_ms": 200},
                "recovery_analysis": {"recovered": True, "time_to_recovery_ms": 400},
                "invariants": [{"name": "no_duplicate_charge", "status": "passed", "expected": "1 charge", "observed": "1 charge"}],
                "recommendation": "System demonstrated resilient behavior.",
            }
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        args = ["faultline.cli", "report", "--run-id", "run-rep-1", "--report-format", "json"]
        with patch.object(sys, "argv", args), patch("faultline.cli.api_json", return_value=report_data), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main()
        self.assertEqual(code, 0)
        self.assertIn("run-rep-1", stdout.getvalue())
        self.assertIn('"verdict": "passed"', stdout.getvalue())

        # Markdown format
        stdout_md = io.StringIO()
        args_md = ["faultline.cli", "report", "--run-id", "run-rep-1", "--report-format", "markdown"]
        with patch.object(sys, "argv", args_md), patch("faultline.cli.api_json", return_value=report_data), \
                contextlib.redirect_stdout(stdout_md), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main()
        self.assertEqual(code, 0)
        self.assertIn("# Faultline Retrospective Report", stdout_md.getvalue())
        self.assertIn("Recommendation", stdout_md.getvalue())

    def test_cli_benchmark_command_executes_suite_and_reports_metrics(self) -> None:
        scenarios_response = {
            "scenarios": [
                {"id": "sc-1", "definition": {"fault": "payment_response_lost_after_commit"}}
            ]
        }
        run_v_res = {"run": {"verdict": "failed"}}
        run_f_res = {"run": {"verdict": "passed"}}
        api_responses = [
            scenarios_response,
            {"run_id": "r-v"},
            {"run_id": "r-f"},
        ]
        poll_responses = [run_v_res["run"], run_f_res["run"]]

        stdout, stderr = io.StringIO(), io.StringIO()
        args = ["faultline.cli", "benchmark"]
        with patch.object(sys, "argv", args), \
                patch("faultline.cli.api_json", side_effect=api_responses), \
                patch("faultline.cli.poll_run", side_effect=poll_responses), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main()
        self.assertEqual(code, 0)
        self.assertIn("Faultline Integration Reliability Benchmark", stdout.getvalue())
        self.assertIn("Defect Detection Rate:   100.0%", stdout.getvalue())
        self.assertIn("Benchmark PASSED", stdout.getvalue())

    def test_cli_templates_command(self) -> None:
        templates_response = {
            "templates": [
                {
                    "id": "payment-response-lost-tpl",
                    "name": "Payment Response Lost",
                    "category": "network_failure",
                    "description": "Verifies retry uses idempotency key.",
                    "expected_invariants": ["no_duplicate_charge"],
                    "cleanup_strategy": "Purges provider ledger.",
                }
            ]
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        args = ["faultline.cli", "templates"]
        with patch.object(sys, "argv", args), \
                patch("faultline.cli.api_json", return_value=templates_response), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main()
        self.assertEqual(code, 0)
        self.assertIn("Faultline Reusable Scenario Templates", stdout.getvalue())
        self.assertIn("payment-response-lost-tpl", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
