import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from examples.checkout_demo import load_scenario, run_case


ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = ROOT / "scenarios" / "payment-response-lost.json"
RUNNER_PATH = ROOT / "examples" / "checkout_demo.py"


class CheckoutScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = load_scenario(SCENARIO_PATH)

    def test_seeded_bug_fails_with_two_recorded_charges(self) -> None:
        report = run_case(self.scenario, "vulnerable")

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["assertions"][0]["status"], "failed")
        self.assertEqual(len(report["charges"]), 2)
        self.assertEqual(report["attempts"], 2)
        self.assertEqual(
            [event["sequence"] for event in report["events"]],
            list(range(1, len(report["events"]) + 1)),
        )

    def test_idempotency_fix_passes_and_reuses_the_original_charge(self) -> None:
        report = run_case(self.scenario, "fixed")

        self.assertEqual(report["status"], "passed")
        self.assertEqual(len(report["charges"]), 1)
        self.assertIn(
            "idempotency_key_reused",
            [event["type"] for event in report["events"]],
        )

    def test_scenario_rejects_unsupported_retry_count(self) -> None:
        invalid_scenario = dict(self.scenario, retries=2)
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            scenario_path = Path(temp_dir) / "invalid.json"
            scenario_path.write_text(json.dumps(invalid_scenario), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "exactly one retry"):
                load_scenario(scenario_path)

    def test_cli_exit_code_tracks_assertion_result(self) -> None:
        fixed = subprocess.run(
            [sys.executable, str(RUNNER_PATH), "--implementation", "fixed"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        vulnerable = subprocess.run(
            [sys.executable, str(RUNNER_PATH), "--implementation", "vulnerable"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(fixed.returncode, 0, fixed.stdout + fixed.stderr)
        self.assertEqual(vulnerable.returncode, 1, vulnerable.stdout + vulnerable.stderr)


if __name__ == "__main__":
    unittest.main()
