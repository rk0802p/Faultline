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


if __name__ == "__main__":
    unittest.main()
