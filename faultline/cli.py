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


def run_benchmark(base: str, timeout: float, debug: bool, measure_determinism: bool = False) -> int:
    print("=" * 60)
    print("Faultline Integration Reliability Benchmark (Spec §17.4)")
    print("=" * 60)
    scenarios_data = api_json(f"{base}/api/scenarios")["scenarios"]
    results = []
    defects_caught = 0
    total_defects = len(scenarios_data)
    false_positives = 0
    determinism_matches = 0
    determinism_total = 0

    for sc in scenarios_data:
        sc_id = sc["id"]
        if debug:
            print(f"[benchmark] Testing defect detection on {sc_id} (vulnerable)...", file=sys.stderr)
        sub_v = api_json(f"{base}/api/runs", {"scenario_id": sc_id, "implementation": "vulnerable"})
        res_v = poll_run(base, sub_v["run_id"], timeout, debug)
        vuln_detected = bool(res_v and res_v.get("status") == "completed" and res_v.get("verdict") == "failed")
        if vuln_detected:
            defects_caught += 1

        if debug:
            print(f"[benchmark] Testing fix validation on {sc_id} (fixed)...", file=sys.stderr)
        sub_f = api_json(f"{base}/api/runs", {"scenario_id": sc_id, "implementation": "fixed"})
        res_f = poll_run(base, sub_f["run_id"], timeout, debug)
        fixed_passed = bool(
            res_f is not None
            and res_f.get("status") == "completed"
            and res_f.get("verdict") == "passed"
        )
        if not fixed_passed:
            false_positives += 1

        if measure_determinism and fixed_passed:
            if debug:
                print(f"[benchmark] Measuring determinism on {sc_id} (repeat run)...", file=sys.stderr)
            sub_rep = api_json(f"{base}/api/runs", {"scenario_id": sc_id, "implementation": "fixed"})
            res_rep = poll_run(base, sub_rep["run_id"], timeout, debug)
            determinism_total += 1
            if res_rep and res_rep.get("status") == "completed" and res_rep.get("verdict") == res_f.get("verdict"):
                inv1 = [(a.get("name"), a.get("status")) for a in res_f.get("assertions", [])]
                inv2 = [(a.get("name"), a.get("status")) for a in res_rep.get("assertions", [])]
                if inv1 == inv2:
                    determinism_matches += 1

        results.append({
            "scenario": sc_id,
            "fault": sc.get("definition", {}).get("fault", "unknown"),
            "defect_caught": vuln_detected,
            "fix_verified": fixed_passed,
        })

    print(f"\nBenchmark Results across {total_defects} Integration Scenarios:")
    print("-" * 60)
    for r in results:
        v_mark = "PASS (defect caught)" if r["defect_caught"] else "FAIL (defect missed)"
        f_mark = "PASS (verified)" if r["fix_verified"] else "FAIL (unexpected error)"
        print(f"  * {r['scenario']}:")
        print(f"      Vulnerable run: {v_mark}")
        print(f"      Fixed run:      {f_mark}")

    print("-" * 60)
    detection_rate = (defects_caught / total_defects * 100) if total_defects else 0
    fp_rate = (false_positives / total_defects * 100) if total_defects else 0
    print(f"Defect Detection Rate:   {detection_rate:.1f}% ({defects_caught}/{total_defects} caught)")
    print(f"False Positive Rate:     {fp_rate:.1f}% ({false_positives}/{total_defects})")
    if measure_determinism and determinism_total > 0:
        det_rate = (determinism_matches / determinism_total) * 100
        print(f"Evaluator Determinism:   {det_rate:.1f}% ({determinism_matches}/{determinism_total} repeated runs matched)")
    else:
        print("Evaluator Determinism:   NOT MEASURED (use --measure-determinism)")
    print(f"Scenarios Verified:      {len(results)}/{total_defects} consistent outcomes")
    print("=" * 60)

    all_defects_detected = (defects_caught == total_defects) and (total_defects > 0)
    all_fixes_verified = all(r["fix_verified"] for r in results) and (len(results) > 0)
    if all_defects_detected and all_fixes_verified and false_positives == 0:
        print("Benchmark PASSED: All seeded integration defects detected and verified.")
        return 0
    else:
        print("Benchmark FAILED: Some defects were missed or unexpected failures occurred.")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Faultline CLI for integration failure testing and benchmarks")
    parser.add_argument("command", choices=["run", "report", "benchmark", "templates", "metrics", "prune"], help="command to execute (run, report, benchmark, templates, metrics, or prune)")
    parser.add_argument("--scenario", help="scenario ID to run")
    parser.add_argument("--suite", help="suite ID to run all member scenarios")
    parser.add_argument("--implementation", choices=["vulnerable", "fixed"], default="vulnerable")
    parser.add_argument("--report-format", choices=["json", "junit", "markdown"], default="json")
    parser.add_argument("--output", help="path to write the test report")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--debug", action="store_true", help="enable detailed debugging and request diagnostics")
    parser.add_argument("--run-id", help="specific run ID for report command (default: latest run)")
    parser.add_argument("--project", default="faultline-demo", help="project ID for project-scoped commands (prune)")
    parser.add_argument("--days", type=int, default=30, help="max age in days for retention pruning (default: 30)")
    parser.add_argument("--no-keep-baselines", action="store_true", help="allow pruning baseline runs during retention cleanup")
    parser.add_argument("--dry-run", action="store_true", help="preview retention cleanup without deleting runs")
    parser.add_argument("--measure-determinism", action="store_true", help="measure evaluator determinism by running repeated evaluations")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    try:
        if args.command == "metrics":
            metrics_data = api_json(f"{base}/api/metrics")
            if args.output:
                Path(args.output).write_text(json.dumps(metrics_data, indent=2), encoding="utf-8")
                print(f"Metrics written to {args.output}")
            else:
                runs = metrics_data.get("runs", {})
                assertions = metrics_data.get("assertions", {})
                worker = metrics_data.get("worker_health", {})
                print("=" * 60)
                print("Faultline Control Plane Operational Metrics (Spec Section 21.2)")
                print("=" * 60)
                print(f"  * System Uptime:       {metrics_data.get('uptime_seconds', 0)} seconds")
                print(f"  * Worker Health:       {str(worker.get('status', 'unknown')).upper()} (queue depth: {worker.get('queue_depth', 0)}, active: {worker.get('active_workers', 0)})")
                print(f"  * Total Runs:          {runs.get('total', 0)}")
                print(f"      - Completed:       {runs.get('completed', 0)}")
                print(f"      - Failed:          {runs.get('failed', 0)}")
                print(f"      - Timed Out:       {runs.get('timed_out', 0)}")
                print(f"      - Cancelled:       {runs.get('cancelled', 0)}")
                print(f"      - Queued/Running:  {runs.get('queued', 0)} / {runs.get('running', 0)}")
                print(f"  * Invariant Pass Rate: {assertions.get('pass_rate_percent', 0.0)}% ({assertions.get('passed', 0)}/{assertions.get('total', 0)} passed)")
                print("=" * 60)
            return 0

        if args.command == "prune":
            prune_payload = {
                "max_age_days": args.days,
                "keep_baselines": not args.no_keep_baselines,
                "dry_run": args.dry_run,
            }
            res = api_json(f"{base}/api/projects/{args.project}/prune", prune_payload)
            mode = "DRY RUN PREVIEW" if res.get("dry_run") else "EXECUTION COMPLETE"
            print("=" * 60)
            print(f"Faultline Retention Pruning [{mode}]")
            print("=" * 60)
            print(f"  * Project:          {res.get('project_id')}")
            print(f"  * Cutoff:           {res.get('cutoff')} (older than {res.get('max_age_days')} days)")
            print(f"  * Keep Baselines:   {res.get('keep_baselines')}")
            print(f"  * Runs Pruned:      {res.get('pruned_run_count')}")
            print(f"  * Baselines Kept:   {res.get('skipped_baselines')}")
            print("=" * 60)
            return 0

        if args.command == "templates":
            templates_data = api_json(f"{base}/api/templates")["templates"]
            print("=" * 60)
            print(f"Faultline Reusable Scenario Templates ({len(templates_data)} available)")
            print("=" * 60)
            for tpl in templates_data:
                print(f"  * {tpl['id']} [{tpl['category']}]:")
                print(f"      Name:        {tpl['name']}")
                print(f"      Description: {tpl['description']}")
                print(f"      Invariants:  {', '.join(tpl['expected_invariants'])}")
                print(f"      Cleanup:     {tpl['cleanup_strategy']}")
                print()
            return 0

        if args.command == "benchmark":
            return run_benchmark(base, args.timeout, args.debug, args.measure_determinism)

        if args.command == "report":
            run_id = args.run_id
            if not run_id:
                runs_list = api_json(f"{base}/api/runs")["runs"]
                if not runs_list:
                    print("No runs found in database.", file=sys.stderr)
                    return 1
                run_id = runs_list[0]["id"]
            fmt_query = "?format=markdown" if args.report_format == "markdown" else ""
            report_data = api_json(f"{base}/api/runs/{run_id}/report{fmt_query}")
            if args.report_format == "markdown" and isinstance(report_data, dict) and "report" in report_data:
                from . import store
                output_text = store.generate_markdown_report(report_data["report"])
            elif isinstance(report_data, str):
                output_text = report_data
            else:
                output_text = json.dumps(report_data, indent=2)

            if args.output:
                out_path = Path(args.output)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(output_text, encoding="utf-8")
                print(f"Report written to {args.output}")
            else:
                print(output_text)
            return 0

        if not args.scenario and not args.suite:
            parser.error("one of --scenario or --suite is required")
        if args.scenario and args.suite:
            parser.error("cannot specify both --scenario and --suite")

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
