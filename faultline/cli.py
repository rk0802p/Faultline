"""Command-line client for the Faultline control-plane API."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


def api_json(url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def calculate_duration(run: dict) -> float:
    try:
        if run.get("started_at") and run.get("finished_at"):
            start = datetime.fromisoformat(run["started_at"])
            finish = datetime.fromisoformat(run["finished_at"])
            return max(0.0, (finish - start).total_seconds())
    except Exception:
        pass
    return 0.0


def generate_junit_xml(suite_name: str, runs: list[dict]) -> str:
    testsuites = ET.Element("testsuites", name="faultline")
    total_tests = len(runs)
    total_failures = 0
    total_errors = 0
    total_time = 0.0

    suite_el = ET.SubElement(testsuites, "testsuite", name=suite_name)

    for run in runs:
        duration = calculate_duration(run)
        total_time += duration
        scenario_name = (
            run.get("scenario_id")
            or (run.get("scenario", {}).get("name") if isinstance(run.get("scenario"), dict) else run.get("scenario"))
            or "unknown"
        )
        tc = ET.SubElement(
            suite_el,
            "testcase",
            classname=f"faultline.{suite_name}",
            name=str(scenario_name),
            time=f"{duration:.3f}",
        )
        verdict = run.get("verdict")
        status = run.get("status")
        if status in {"failed", "timed_out", "cancelled"}:
            total_errors += 1
            error_el = ET.SubElement(
                tc,
                "error",
                type=str(status),
                message=str(run.get("error") or f"Run {status}"),
            )
            error_el.text = json.dumps(run, indent=2)
        elif verdict != "passed":
            total_failures += 1
            failed_assertions = [a for a in run.get("assertions", []) if a.get("status") != "passed"]
            fail_msg = f"{len(failed_assertions)} invariant(s) failed" if failed_assertions else "Scenario verdict failed"
            fail_el = ET.SubElement(
                tc,
                "failure",
                type="AssertionFailure",
                message=fail_msg,
            )
            fail_el.text = json.dumps(failed_assertions or run, indent=2)

    suite_el.set("tests", str(total_tests))
    suite_el.set("failures", str(total_failures))
    suite_el.set("errors", str(total_errors))
    suite_el.set("time", f"{total_time:.3f}")

    testsuites.set("tests", str(total_tests))
    testsuites.set("failures", str(total_failures))
    testsuites.set("errors", str(total_errors))
    testsuites.set("time", f"{total_time:.3f}")

    ET.indent(testsuites, space="  ")
    return ET.tostring(testsuites, encoding="utf-8", xml_declaration=True).decode("utf-8")


def compute_exit_code(runs: list[dict]) -> int:
    statuses = {r.get("status") for r in runs}
    verdicts = {r.get("verdict") for r in runs}
    if "failed" in statuses:
        return 2
    if "timed_out" in statuses:
        return 3
    if "cancelled" in statuses:
        return 4
    if any(v != "passed" for v in verdicts):
        return 1
    return 0


def poll_run(base: str, run_id: str, timeout: float, debug: bool = False) -> dict | None:
    deadline = time.monotonic() + timeout
    poll_count = 0
    while time.monotonic() < deadline:
        poll_count += 1
        result = api_json(base + "/api/runs/" + run_id)["run"]
        if debug:
            print(f"[debug] Poll #{poll_count}: status={result['status']} verdict={result.get('verdict')}", file=sys.stderr)
        if result["status"] in {"completed", "failed", "cancelled", "timed_out"}:
            return result
        time.sleep(0.25)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a Faultline scenario or suite through the local control-plane API")
    parser.add_argument("run", choices=["run"])
    parser.add_argument("--scenario", help="scenario ID to run")
    parser.add_argument("--suite", help="suite ID to run all member scenarios")
    parser.add_argument("--implementation", choices=["vulnerable", "fixed"], default="vulnerable")
    parser.add_argument("--report-format", choices=["json", "junit"], default="json")
    parser.add_argument("--output", help="path to write the test report")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--debug", action="store_true", help="enable detailed debugging and request diagnostics")
    args = parser.parse_args()

    if not args.scenario and not args.suite:
        parser.error("one of --scenario or --suite is required")
    if args.scenario and args.suite:
        parser.error("cannot specify both --scenario and --suite")

    base = args.base_url.rstrip("/")
    try:
        if args.suite:
            suite_info = api_json(f"{base}/api/suites/{args.suite}")["suite"]
            scenario_ids = suite_info.get("scenarios", [])
            suite_name = suite_info.get("name", args.suite)
        else:
            scenario_ids = [args.scenario]
            suite_name = args.scenario

        runs = []
        for sc_id in scenario_ids:
            if args.debug:
                print(f"[debug] Submitting run: scenario={sc_id} implementation={args.implementation} to {base}/api/runs", file=sys.stderr)
            submitted = api_json(base + "/api/runs", {"scenario_id": sc_id, "implementation": args.implementation})
            run_id = submitted["run_id"]
            if args.debug:
                print(f"[debug] Run submitted successfully: run_id={run_id}", file=sys.stderr)

            result = poll_run(base, run_id, args.timeout, args.debug)
            if result is None:
                print(f"Timed out waiting for run {run_id}", file=sys.stderr)
                return 3
            runs.append(result)

        if args.report_format == "junit":
            report_text = generate_junit_xml(suite_name, runs)
        else:
            if args.scenario:
                report_text = json.dumps(runs[0], indent=2)
            else:
                report_text = json.dumps({
                    "suite": args.suite,
                    "name": suite_name,
                    "implementation": args.implementation,
                    "total": len(runs),
                    "passed": sum(1 for r in runs if r.get("verdict") == "passed"),
                    "failed": sum(1 for r in runs if r.get("verdict") != "passed"),
                    "runs": runs,
                }, indent=2)

        if args.output:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(report_text, encoding="utf-8")
        else:
            print(report_text)

        return compute_exit_code(runs)

    except (HTTPError, URLError, TimeoutError, KeyError, ValueError) as exc:
        print(f"Faultline API error: {exc}", file=sys.stderr)
        if args.debug:
            import traceback
            traceback.print_exc(file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
