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

try:
    from . import security
    from .schema import SUPPORTED_FAULTS
except ImportError:
    from faultline import security
    from faultline.schema import SUPPORTED_FAULTS


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
                message=str(security.redact_sensitive(run.get("error")) or f"Run {status}"),
            )
            error_el.text = json.dumps(security.redact_sensitive(run), indent=2)
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
            fail_el.text = json.dumps(security.redact_sensitive(failed_assertions or run), indent=2)

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


BENCHMARK_SCENARIOS = (
    "payment-response-lost",
    "duplicate-payment-webhook",
    "partial-persistence-failure",
)


def normalize_evaluation_assertions(assertions: list[dict]) -> list[tuple[str, str, str, str]]:
    normalized = []
    for a in assertions:
        normalized.append((
            str(a.get("name")),
            str(a.get("status")),
            str(a.get("expected")),
            str(a.get("observed")),
        ))
    return sorted(normalized, key=lambda x: x[0])


def run_benchmark(base: str, timeout: float, debug: bool, measure_determinism: bool = False) -> int:
    print("=" * 60)
    print("Faultline Integration Reliability Benchmark (Spec §17.4)")
    print("=" * 60)
    all_scenarios_map = {s.get("id"): s for s in api_json(f"{base}/api/scenarios")["scenarios"]}
    missing = [sc_id for sc_id in BENCHMARK_SCENARIOS if sc_id not in all_scenarios_map]
    if missing:
        print(f"Error: required benchmark scenario(s) missing from server: {', '.join(missing)}", file=sys.stderr)
        return 1
    scenarios_data = [all_scenarios_map[sc_id] for sc_id in BENCHMARK_SCENARIOS]
    results = []
    defects_caught = 0
    total_defects = len(scenarios_data)
    fixes_verified = 0
    fixed_verification_failures = 0
    determinism_matches = 0
    determinism_total = 0

    for sc in scenarios_data:
        sc_id = sc["id"]
        sc_def = sc.get("definition", {})
        fault_type = sc_def.get("fault", "unknown")
        expected_primary = sc_def.get("assertion") or (SUPPORTED_FAULTS.get(fault_type, {}).get("primary_assertion") if fault_type else None)

        if debug:
            print(f"[benchmark] Testing defect detection on {sc_id} (vulnerable)...", file=sys.stderr)
        sub_v = api_json(f"{base}/api/runs", {"scenario_id": sc_id, "implementation": "vulnerable"})
        res_v = poll_run(base, sub_v["run_id"], timeout, debug)

        assertions_v = res_v.get("assertions", []) if res_v else []
        failed_assertions = {
            assertion.get("name")
            for assertion in assertions_v
            if assertion.get("status") == "failed"
        }
        vuln_completed = bool(
            res_v is not None
            and res_v.get("status") == "completed"
        )
        vuln_detected = bool(
            expected_primary
            and vuln_completed
            and res_v.get("verdict") == "failed"
            and expected_primary in failed_assertions
        )

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
        if fixed_passed:
            fixes_verified += 1
        else:
            fixed_verification_failures += 1

        if measure_determinism and fixed_passed:
            if debug:
                print(f"[benchmark] Measuring determinism on {sc_id} (repeat run)...", file=sys.stderr)
            sub_rep = api_json(f"{base}/api/runs", {"scenario_id": sc_id, "implementation": "fixed"})
            res_rep = poll_run(base, sub_rep["run_id"], timeout, debug)
            determinism_total += 1
            if res_rep and res_rep.get("status") == "completed" and res_rep.get("verdict") == res_f.get("verdict"):
                inv1 = normalize_evaluation_assertions(res_f.get("assertions", []))
                inv2 = normalize_evaluation_assertions(res_rep.get("assertions", []))
                if inv1 == inv2:
                    determinism_matches += 1

        results.append({
            "scenario": sc_id,
            "fault": fault_type,
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
    detection_rate = (defects_caught / total_defects * 100) if total_defects else 0.0
    fixed_pass_rate = (fixes_verified / total_defects * 100) if total_defects else 0.0
    fixed_fail_rate = (fixed_verification_failures / total_defects * 100) if total_defects else 0.0
    scenarios_fully_verified = sum(1 for r in results if r["defect_caught"] and r["fix_verified"])

    print(f"Defect Detection Rate:   {detection_rate:.1f}% ({defects_caught}/{total_defects} caught)")
    print(f"Fixed Verification Pass Rate:    {fixed_pass_rate:.1f}% ({fixes_verified}/{total_defects} passed)")
    print(f"Fixed Verification Failure Rate: {fixed_fail_rate:.1f}% ({fixed_verification_failures}/{total_defects})")
    if measure_determinism and determinism_total > 0:
        det_rate = (determinism_matches / determinism_total) * 100
        print(f"Evaluator Determinism:   {det_rate:.1f}% ({determinism_matches}/{determinism_total} repeated runs matched)")
    else:
        print("Evaluator Determinism:   NOT MEASURED (use --measure-determinism)")
    print(f"Scenarios Verified:      {scenarios_fully_verified}/{total_defects} consistent outcomes")
    print("=" * 60)

    all_defects_detected = (defects_caught == total_defects) and (total_defects > 0)
    all_fixes_verified = (fixed_verification_failures == 0) and (total_defects > 0)
    all_scenarios_verified = (scenarios_fully_verified == total_defects) and (total_defects > 0)
    if all_defects_detected and all_fixes_verified and all_scenarios_verified:
        print("Benchmark PASSED: All seeded integration defects detected and verified.")
        return 0
    else:
        print("Benchmark FAILED: Some defects were missed or unexpected failures occurred.")
        return 1



def format_terminal_summary_table(run: dict) -> str:
    lines = []
    lines.append("=" * 64)
    lines.append("FAULTLINE EXECUTION SUMMARY")
    lines.append("=" * 64)
    lines.append(f"  Run ID:         {run.get('id', 'unknown')}")
    scenario_info = run.get("scenario") or run.get("scenario_id") or "unknown"
    if isinstance(scenario_info, dict):
        scenario_name = scenario_info.get("name", "unknown")
    else:
        scenario_name = str(scenario_info)
    lines.append(f"  Scenario:       {scenario_name}")
    lines.append(f"  Implementation: {run.get('implementation', 'default')}")
    target_id = run.get("target_id") or (run.get("scenario", {}).get("target_id") if isinstance(run.get("scenario"), dict) else None) or "checkout-demo"
    lines.append(f"  Target App:     {target_id}")
    status = str(run.get("status", "unknown")).upper()
    verdict = str(run.get("verdict", "pending")).upper()
    lines.append(f"  Status:         {status} - Verdict: {verdict}")
    lines.append(f"  Duration:       {calculate_duration(run):.2f}s")
    lines.append("-" * 64)
    lines.append("INVARIANT ASSERTIONS:")
    assertions = run.get("assertions", [])
    if not assertions:
        lines.append("  (No invariant assertions recorded)")
    else:
        for a in assertions:
            st = a.get("status", "pending")
            mark = "OK [PASS]" if st == "passed" else "FAIL [FAIL]"
            name = a.get("name", "assertion")
            expected = a.get("expected", "")
            observed = a.get("observed", "")
            lines.append(f"  {mark} {name:<26} (Observed: {observed} | Expected: {expected})")
    
    observer_results = run.get("summary", {}).get("observer_results") if isinstance(run.get("summary"), dict) else None
    if observer_results:
        lines.append("-" * 64)
        lines.append("INDEPENDENT EXTERNAL OBSERVATION:")
        db_obs = observer_results.get("database")
        if db_obs:
            lines.append(f"  SQLite DB:      {db_obs.get('sqlite_path', 'N/A')}")
            recs = db_obs.get("records", {})
            for tbl, cnt in recs.items():
                lines.append(f"    - Table '{tbl}': {cnt} row(s)")
        http_obs = observer_results.get("http_state")
        if http_obs:
            lines.append(f"  HTTP State:     {json.dumps(http_obs)}")
    lines.append("=" * 64)
    return "\n".join(lines)


def run_doctor(base: str, debug: bool = False) -> int:
    print("=" * 64)
    print("Faultline Doctor - System & Environment Diagnostics")
    print("=" * 64)
    all_ok = True

    # 1. Python runtime
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 10):
        print(f"  [OK] Python Runtime:          v{py_ver} (>= 3.10 required)")
    else:
        print(f"  [FAIL] Python Runtime:          v{py_ver} (< 3.10 not supported)")
        all_ok = False

    # 2. Standard library zero-pip dependencies
    req_mods = ["sqlite3", "socket", "http.server", "urllib.request", "threading", "json", "uuid", "hashlib"]
    missing = []
    for m in req_mods:
        try:
            __import__(m)
        except ImportError:
            missing.append(m)
    if not missing:
        print("  [OK] Standard Library:        Zero pip dependencies requirement met")
    else:
        print(f"  [FAIL] Standard Library:        Missing core modules: {missing}")
        all_ok = False

    # 3. SQLite database store
    try:
        from . import store
        store.initialize()
        targets = store.list_targets()
        scenarios = store.list_scenarios()
        print(f"  [OK] SQLite Database Store:   Healthy ({len(scenarios)} scenarios, {len(targets)} targets registered)")
    except Exception as exc:
        print(f"  [FAIL] SQLite Database Store:   Failed ({exc})")
        all_ok = False

    # 4. TCP Loopback Socket binding and communication
    try:
        import socket
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.connect(("127.0.0.1", port))
        conn, _ = srv.accept()
        client.sendall(b"FAULTLINE_PING")
        data = conn.recv(64)
        conn.close()
        client.close()
        srv.close()
        if data == b"FAULTLINE_PING":
            print(f"  [OK] Network Sockets:         TCP loopback socket test passed (bound port {port})")
        else:
            print("  [FAIL] Network Sockets:         Loopback payload mismatch")
            all_ok = False
    except Exception as exc:
        print(f"  [FAIL] Network Sockets:         Loopback socket test failed ({exc})")
        all_ok = False

    # 5. Faultline Control Plane HTTP Daemon
    try:
        req = Request(f"{base}/api/metrics")
        with urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                print(f"  [OK] Faultline Control Plane: Online at {base}")
            else:
                print(f"  [!] Faultline Control Plane: Returned status {resp.status} at {base}")
    except Exception:
        print(f"  [!] Faultline Control Plane: Offline (no daemon at {base}; in-process CLI ready)")

    # 6. Registered Targets Health
    try:
        from .targets import target_registry
        all_targets = target_registry.list()
        if all_targets:
            healthy_count = 0
            for t in all_targets:
                h = target_registry.check_health(t.id, timeout=1.0)
                if h.get("status") == "healthy":
                    healthy_count += 1
            print(f"  [OK] Target Applications:     {len(all_targets)} registered ({healthy_count} healthy)")
        else:
            print("  [OK] Target Applications:     0 registered (ready for registration)")
    except Exception as exc:
        print(f"  [!] Target Applications:     Check skipped ({exc})")

    print("=" * 64)
    if all_ok:
        print("Doctor assessment: ALL CORE SYSTEMS HEALTHY.")
        return 0
    else:
        print("Doctor assessment: SYSTEM ISSUES DETECTED. Review errors above.")
        return 1


def run_target_cli(base: str, subcommand: str | None, extra_args: list[str], args: argparse.Namespace) -> int:
    sub = (subcommand or "list").lower()
    from .targets import target_registry, TargetConfig

    if sub == "list":
        try:
            targets_data = api_json(f"{base}/api/targets").get("targets", [])
        except Exception:
            targets_data = [t.to_dict() for t in target_registry.list()]

        print("=" * 72)
        print("FAULTLINE REGISTERED TARGET APPLICATIONS")
        print("=" * 72)
        if not targets_data:
            print("  No targets registered yet. Use 'faultline target add <id> <name> --url <base_url>'.")
        else:
            print(f"  {'TARGET ID':<18} {'MODE':<9} {'PROXY':<8} {'BASE URL':<26} {'NAME'}")
            print(f"  {'-'*16} {'-'*7} {'-'*6} {'-'*24} {'-'*15}")
            for t in targets_data:
                proxy_str = f":{t['proxy_port']}" if t.get("proxy_port") else "-"
                print(f"  {t['id']:<18} {t.get('mode', 'connect'):<9} {proxy_str:<8} {t.get('base_url', ''):<26} {t.get('name', '')}")
        print("=" * 72)
        return 0

    elif sub == "add":
        if not extra_args or len(extra_args) < 1:
            print("Usage: faultline target add <target-id> [<display-name>] --url <base-url> [--mode connect|managed]", file=sys.stderr)
            return 2
        target_id = extra_args[0]
        name = extra_args[1] if len(extra_args) > 1 else target_id
        url = args.url
        if not url:
            print("Error: --url <base_url> is required for 'target add'", file=sys.stderr)
            return 2
        payload = {
            "id": target_id,
            "name": name,
            "mode": args.mode,
            "base_url": url,
            "health_url": args.health or "/health",
            "proxy_port": args.proxy_port,
            "sqlite_path": args.db,
            "start_command": args.start_cmd,
            "stop_command": args.stop_cmd,
            "working_dir": args.cwd,
        }
        try:
            api_json(f"{base}/api/targets", payload)
        except Exception:
            target_registry.register(TargetConfig.from_dict(payload))
        print(f"Target '{target_id}' ({name}) registered successfully at {url}.")
        return 0

    elif sub == "test":
        target_id = extra_args[0] if extra_args else None
        if target_id:
            targets_to_test = [target_id]
        else:
            try:
                targets_to_test = [t["id"] for t in api_json(f"{base}/api/targets").get("targets", [])]
            except Exception:
                targets_to_test = [t.id for t in target_registry.list()]

        if not targets_to_test:
            print("No targets registered to test.")
            return 0

        all_healthy = True
        print("=" * 64)
        print("TARGET APPLICATION HEALTH CHECK")
        print("=" * 64)
        for tid in targets_to_test:
            try:
                res = api_json(f"{base}/api/targets/{tid}/health")
            except Exception:
                res = target_registry.check_health(tid)

            status = res.get("status", "unreachable")
            if status == "healthy":
                code = res.get("status_code", 200)
                dur = res.get("latency_ms", 0)
                print(f"  [OK HEALTHY]     {tid:<18} (HTTP {code} in {dur:.1f}ms)")
            else:
                err = res.get("error", "Connection failed")
                print(f"  [FAIL UNREACHABLE] {tid:<18} ({err})")
                all_healthy = False
        print("=" * 64)
        return 0 if all_healthy else 1

    elif sub == "start":
        if not extra_args:
            print("Usage: faultline target start <target-id>", file=sys.stderr)
            return 2
        target_id = extra_args[0]
        try:
            res = api_json(f"{base}/api/targets/{target_id}/start", {})
        except Exception:
            res = target_registry.start_managed(target_id)
        print(f"Target process '{target_id}' started (PID: {res.get('pid')}).")
        return 0

    elif sub == "stop":
        if not extra_args:
            print("Usage: faultline target stop <target-id>", file=sys.stderr)
            return 2
        target_id = extra_args[0]
        try:
            res = api_json(f"{base}/api/targets/{target_id}/stop", {})
        except Exception:
            res = {"stopped": target_registry.stop_managed(target_id)}
        print(f"Target process '{target_id}' stopped.")
        return 0

    elif sub == "delete":
        if not extra_args:
            print("Usage: faultline target delete <target-id>", file=sys.stderr)
            return 2
        target_id = extra_args[0]
        try:
            req = Request(f"{base}/api/targets/{target_id}", method="DELETE")
            with urlopen(req, timeout=5) as r:
                pass
        except Exception:
            target_registry.delete(target_id)
        print(f"Target '{target_id}' deleted.")
        return 0

    else:
        print(f"Unknown target subcommand '{sub}'. Choose from: list, add, test, start, stop, delete.", file=sys.stderr)
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Faultline CLI for integration failure testing and benchmarks")
    parser.add_argument("command", choices=["run", "report", "benchmark", "templates", "metrics", "prune", "target", "doctor"], help="command to execute")
    parser.add_argument("subcommand", nargs="?", default=None, help="subcommand for target or positional scenario for run")
    parser.add_argument("extra_args", nargs="*", default=[], help="extra arguments for subcommands")
    parser.add_argument("--scenario", help="scenario ID to run")
    parser.add_argument("--suite", help="suite ID to run all member scenarios")
    parser.add_argument("--target", help="target application ID")
    parser.add_argument("--url", help="base URL for target application")
    parser.add_argument("--mode", choices=["connect", "managed"], default="connect", help="target integration mode")
    parser.add_argument("--health", default="/health", help="health check endpoint path")
    parser.add_argument("--proxy-port", type=int, help="network proxy fault injection port")
    parser.add_argument("--db", help="SQLite database path for independent observation")
    parser.add_argument("--start-cmd", help="start command for managed target process")
    parser.add_argument("--stop-cmd", help="stop command for managed target process")
    parser.add_argument("--cwd", help="working directory for managed target process")
    parser.add_argument("--implementation", choices=["vulnerable", "fixed"], default="vulnerable")
    parser.add_argument("--report-format", choices=["json", "junit", "markdown", "table"], default="json")
    parser.add_argument("--table", action="store_true", help="format output as human-readable table")
    parser.add_argument("--local", action="store_true", help="execute run in-process without control plane daemon")
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
        if args.command == "doctor":
            return run_doctor(base, args.debug)

        if args.command == "target":
            return run_target_cli(base, args.subcommand, args.extra_args, args)

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

        # Command is 'run'
        scenario_arg = args.scenario or args.subcommand
        if not scenario_arg and not args.suite:
            parser.error("one of --scenario or --suite is required")
        if (args.scenario or (args.subcommand and not args.scenario)) and args.suite:
            parser.error("cannot specify both --scenario and --suite")

        if args.suite:
            suite_info = api_json(f"{base}/api/suites/{args.suite}")["suite"]
            scenario_ids = suite_info.get("scenarios", [])
            suite_name = suite_info.get("name", args.suite)
            scenario_definitions = {}
        else:
            scenario_definitions = {}
            if scenario_arg and (scenario_arg.endswith(".json") or Path(scenario_arg).is_file()):
                spec_path = Path(scenario_arg)
                if not spec_path.is_file():
                    print(f"Error: scenario file '{scenario_arg}' not found.", file=sys.stderr)
                    return 2
                def_obj = json.loads(spec_path.read_text(encoding="utf-8"))
                sc_id = def_obj.get("name") or spec_path.stem
                if args.target:
                    def_obj["target_id"] = args.target
                scenario_ids = [sc_id]
                scenario_definitions[sc_id] = def_obj
                suite_name = sc_id
            else:
                scenario_ids = [scenario_arg]
                suite_name = scenario_arg

        runs = []
        for sc_id in scenario_ids:
            payload = {
                "scenario_id": sc_id,
                "implementation": args.implementation,
            }
            if sc_id in scenario_definitions:
                payload["definition"] = scenario_definitions[sc_id]
            if args.target:
                payload["target_id"] = args.target

            if args.debug:
                print(f"[debug] Submitting run: scenario={sc_id} implementation={args.implementation} to {base}/api/runs", file=sys.stderr)

            if args.local:
                if args.debug:
                    print(f"[debug] Executing scenario '{sc_id}' in-process (--local)...", file=sys.stderr)
                from . import store, engine
                store.initialize()
                if sc_id in scenario_definitions:
                    definition = scenario_definitions[sc_id]
                else:
                    sc_record = store.get_scenario(sc_id)
                    if not sc_record:
                        print(f"Error: scenario '{sc_id}' not found.", file=sys.stderr)
                        return 2
                    definition = sc_record["definition"]
                if definition.get("fault") in {"payment_response_lost_after_commit", "duplicate_payment_webhook", "persistence_failure_after_payment"}:
                    try:
                        from . import adapters
                        adapters.start_local_services()
                    except Exception:
                        pass

                from uuid import uuid4
                run_id = str(uuid4())
                scenario_ver = sc_record.get("version", 1) if 'sc_record' in locals() and sc_record else 1
                store.create_run(run_id, sc_id, scenario_ver, args.implementation)
                store.record_event(run_id, "RUN_QUEUED", {"scenario_id": sc_id})
                engine.submit(run_id, definition, args.implementation)
                deadline = time.monotonic() + args.timeout
                while time.monotonic() < deadline:
                    r_obj = store.get_run(run_id)
                    if r_obj and r_obj["status"] in {"completed", "failed", "cancelled", "timed_out"}:
                        runs.append(r_obj)
                        break
                    time.sleep(0.1)
                else:
                    print(f"Timed out waiting for in-process run {run_id}", file=sys.stderr)
                    return 3
            else:
                submitted = api_json(base + "/api/runs", payload)
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
        elif args.report_format == "table" or args.table:
            if len(runs) == 1:
                report_text = format_terminal_summary_table(runs[0])
            else:
                report_text = "\n\n".join(format_terminal_summary_table(r) for r in runs)
        else:
            if not args.suite and len(runs) == 1:
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


if __name__ == '__main__':
    raise SystemExit(main())
