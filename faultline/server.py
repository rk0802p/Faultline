"""Faultline control-plane API and local product web server."""

import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from . import engine, importer, security, store
from .adapters import start_local_services
from .engine import submit


WEB_ROOT = Path(__file__).resolve().parent / "web"
logger = logging.getLogger("faultline.control")


SUPPORTED_FAULTS = {
    "payment_response_lost_after_commit": {"primary_assertion": "no_duplicate_charge"},
    "duplicate_payment_webhook": {"primary_assertion": "idempotent_webhook_processing"},
    "persistence_failure_after_payment": {"primary_assertion": "charge_compensated_on_failure"},
}


def validate_scenario(definition: dict[str, Any]) -> None:
    if type(definition.get("scenario_version")) is not int or definition["scenario_version"] != 1:
        raise ValueError("unsupported scenario_version")
    fault = definition.get("fault")
    if fault not in SUPPORTED_FAULTS:
        raise ValueError("unsupported fault type")
    expected_assertion = SUPPORTED_FAULTS[fault]["primary_assertion"]
    if definition.get("assertion") != expected_assertion:
        raise ValueError("unsupported primary assertion")
    if not isinstance(definition.get("name"), str) or not definition["name"]:
        raise ValueError("scenario name is required")
    if not isinstance(definition.get("order_id"), str) or not definition["order_id"]:
        raise ValueError("order_id is required")
    if type(definition.get("amount_cents")) is not int or definition["amount_cents"] < 1:
        raise ValueError("amount_cents must be a positive integer")
    if fault in {"payment_response_lost_after_commit", "persistence_failure_after_payment"}:
        if type(definition.get("retries")) is not int or not 0 <= definition["retries"] <= 3:
            raise ValueError("retries must be between zero and three")
    elif fault == "duplicate_payment_webhook":
        deliveries = definition.get("webhook_deliveries", 2)
        if type(deliveries) is not int or not 2 <= deliveries <= 5:
            raise ValueError("webhook_deliveries must be between two and five")
        delay = definition.get("delay_ms", 100)
        if type(delay) is not int or not 0 <= delay <= 5000:
            raise ValueError("delay_ms must be between 0 and 5000")
    timeout = definition.get("timeout_seconds", 15)
    if type(timeout) is not int or not 2 <= timeout <= 60:
        raise ValueError("timeout_seconds must be between two and sixty")


class ControlPlaneHandler(BaseHTTPRequestHandler):
    server_version = "Faultline/0.1"

    def respond(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            self.respond(200, {"status": "ok", "service": "faultline-control-plane"})
        elif path == "/api/projects":
            self.respond(200, {"projects": store.list_projects()})
        elif path == "/api/metrics":
            parsed_query = parse_qs(urlparse(self.path).query)
            accept_header = self.headers.get("Accept", "")
            if parsed_query.get("format") == ["prometheus"] or "text/plain" in accept_header:
                raw = store.get_prometheus_metrics().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
            else:
                self.respond(200, store.get_system_metrics())
        elif re.fullmatch(r"/api/projects/[^/]+/suites", path):
            self.respond(200, {"suites": store.list_suites()})
        elif path == "/api/coverage":
            self.respond(200, store.get_coverage_matrix())
        elif re.fullmatch(r"/api/projects/[^/]+/tasks", path):
            project_id = path.split("/")[3]
            self.respond(200, {"tasks": store.list_tasks(project_id=project_id)})
        elif re.fullmatch(r"/api/projects/[^/]+/services", path):
            project_id = path.split("/")[3]
            self.respond(200, {"services": store.get_project_services(project_id)})
        elif re.fullmatch(r"/api/projects/[^/]+/audit-events", path):
            project_id = path.split("/")[3]
            self.respond(200, {"audit_events": store.list_audit_events(project_id)})
        elif path == "/api/suites":
            self.respond(200, {"suites": store.list_suites()})
        elif re.fullmatch(r"/api/suites/[^/]+/results", path):
            suite_id = path.split("/")[3]
            self.respond(200, {"results": store.list_suite_results(suite_id)})
        elif re.fullmatch(r"/api/suites/[^/]+", path):
            suite_id = path.split("/")[3]
            suite = store.get_suite(suite_id)
            self.respond(200, {"suite": suite}) if suite else self.respond(404, {"error": "suite not found"})
        elif path == "/api/templates":
            self.respond(200, {"templates": store.list_templates()})
        elif re.fullmatch(r"/api/templates/[^/]+", path):
            template_id = path.split("/")[3]
            template = store.get_template(template_id)
            self.respond(200, {"template": template}) if template else self.respond(404, {"error": "template not found"})
        elif path == "/api/scenarios":
            self.respond(200, {"scenarios": store.list_scenarios()})
        elif re.fullmatch(r"/api/scenarios/[^/]+/baseline-diff", path):
            scenario_id = path.split("/")[3]
            parsed_query = parse_qs(urlparse(self.path).query)
            target_run_id = parsed_query.get("target_run_id", [None])[0]
            self.respond(200, store.get_scenario_baseline_diff(scenario_id, target_run_id))
        elif re.fullmatch(r"/api/scenarios/[^/]+/versions", path):
            scenario_id = path.split("/")[3]
            versions = store.scenario_versions(scenario_id)
            self.respond(200, {"versions": versions}) if versions else self.respond(404, {"error": "scenario not found"})
        elif path == "/api/runs":
            self.respond(200, {"runs": store.list_runs()})
        elif path.startswith("/api/runs/"):
            parts = path.removeprefix("/api/runs/").split("/")
            run_id = parts[0]
            if len(parts) == 2 and parts[1] == "annotations":
                self.respond(200, {"annotations": store.list_annotations(run_id)})
            elif len(parts) == 2 and parts[1] == "tasks":
                self.respond(200, {"tasks": store.list_tasks(run_id=run_id)})
            elif len(parts) == 2 and parts[1] == "report":
                report = store.generate_run_report(run_id)
                if not report:
                    self.respond(404, {"error": "run not found"})
                elif "format=markdown" in self.path or "text/markdown" in self.headers.get("Accept", ""):
                    md = store.generate_markdown_report(report).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/markdown; charset=utf-8")
                    self.send_header("Content-Length", str(len(md)))
                    self.end_headers()
                    self.wfile.write(md)
                else:
                    self.respond(200, {"report": report})
            elif len(parts) == 3 and parts[1] == "diff":
                diff = store.diff_runs(run_id, parts[2])
                self.respond(200, {"diff": diff}) if diff else self.respond(404, {"error": "one or both runs not found"})
            elif len(parts) == 2 and parts[1] == "events":
                run = store.get_run(run_id)
                self.respond(200, {"events": run["events"] if run else []}) if run else self.respond(404, {"error": "run not found"})
            elif len(parts) == 2 and parts[1] == "assertions":
                run = store.get_run(run_id)
                self.respond(200, {"assertions": run["assertions"] if run else []}) if run else self.respond(404, {"error": "run not found"})
            elif len(parts) == 2 and parts[1] == "snapshots":
                self.respond(200, {"snapshots": store.get_snapshots(run_id)})
            elif len(parts) == 2 and parts[1] == "state":
                parsed_query = parse_qs(urlparse(self.path).query)
                seq = int(parsed_query.get("sequence", [0])[0])
                st = store.reconstruct_state_at(run_id, seq)
                self.respond(200, {"state": st}) if st else self.respond(404, {"error": "run not found"})
            elif len(parts) == 2 and parts[1] in {"telemetry", "traces"}:
                telemetry = store.get_run_telemetry(run_id)
                self.respond(200, telemetry) if telemetry else self.respond(404, {"error": "run not found"})
            elif len(parts) == 2 and parts[1] == "reproduce":
                bundle = store.get_reproduction_bundle(run_id)
                if bundle:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Disposition", f'attachment; filename="faultline-{run_id[:8]}-repro.json"')
                    raw = json.dumps(bundle, indent=2).encode()
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                else: self.respond(404, {"error": "run not found"})
            else:
                run = store.get_run(run_id)
                self.respond(200, {"run": run}) if run else self.respond(404, {"error": "run not found"})
        elif path == "/" or path == "/index.html":
            self.static_file("index.html", "text/html; charset=utf-8")
        elif path in {"/app.js", "/styles.css", "/report.css"}:
            content_type = "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8"
            self.static_file(path[1:], content_type)
        else:
            self.respond(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if (
            path not in {"/api/runs", "/api/scenarios", "/api/scenarios/validate", "/api/suites", "/api/scenarios/import-incident", "/api/workflows/import-openapi", "/api/scenarios/generate-from-gaps"}
            and not re.fullmatch(r"/api/scenarios/[^/]+/versions", path)
            and not re.fullmatch(r"/api/scenarios/[^/]+/versions/[^/]+/clone", path)
            and not re.fullmatch(r"/api/templates/[^/]+/instantiate", path)
            and not re.fullmatch(r"/api/runs/[^/]+/cancel", path)
            and not re.fullmatch(r"/api/runs/[^/]+/branch", path)
            and not re.fullmatch(r"/api/runs/[^/]+/baseline", path)
            and not re.fullmatch(r"/api/runs/[^/]+/annotations", path)
            and not re.fullmatch(r"/api/runs/[^/]+/tasks", path)
            and not re.fullmatch(r"/api/suites/[^/]+/runs", path)
            and not re.fullmatch(r"/api/suites/[^/]+/scenarios", path)
            and not re.fullmatch(r"/api/projects/[^/]+/prune", path)
            and not re.fullmatch(r"/api/projects/[^/]+/suites", path)
        ):
            self.respond(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 16_384:
                self.respond(413, {"error": "request too large"})
                return
            payload = json.loads(self.rfile.read(length)) if length > 0 else {}
            if not isinstance(payload, dict): raise ValueError("request body must be a JSON object")
            cancel_match = re.fullmatch(r"/api/runs/([^/]+)/cancel", path)
            if cancel_match:
                run_id = cancel_match.group(1)
                if engine.cancel(run_id):
                    store.record_audit_event("faultline-demo", "developer", "RUN_CANCEL_REQUESTED", "run", run_id, {})
                    self.respond(202, {"run_id": run_id, "status": "cancel_requested"})
                else:
                    run = store.get_run(run_id)
                    self.respond(409, {"error": "run is not cancellable", "status": run["status"] if run else "not_found"})
                return
            prune_match = re.fullmatch(r"/api/projects/([^/]+)/prune", path)
            if prune_match:
                project_id = prune_match.group(1)
                max_age_days = int(payload.get("max_age_days", 30))
                keep_baselines = bool(payload.get("keep_baselines", True))
                dry_run = bool(payload.get("dry_run", False))
                try:
                    res = store.prune_project_runs(project_id, max_age_days, keep_baselines, dry_run)
                    self.respond(200, res)
                except KeyError as exc:
                    self.respond(404, {"error": str(exc)})
                return
            proj_suite_match = re.fullmatch(r"/api/projects/([^/]+)/suites", path)
            if proj_suite_match:
                project_id = proj_suite_match.group(1)
                suite_id = payload.get("id") or re.sub(r"[^a-z0-9]+", "-", payload.get("name", "").lower()).strip("-")[:64]
                if not suite_id:
                    raise ValueError("suite id or name is required")
                name = payload.get("name") or suite_id
                description = payload.get("description", "")
                scenarios = payload.get("scenarios", [])
                if not isinstance(scenarios, list) or not scenarios:
                    raise ValueError("scenarios must be a non-empty list of scenario IDs")
                store.create_suite(suite_id, project_id, name, description, scenarios)
                store.record_audit_event(project_id, "developer", "SUITE_CREATED", "suite", suite_id, {"scenarios": scenarios})
                self.respond(201, {"suite_id": suite_id})
                return
            branch_match = re.fullmatch(r"/api/runs/([^/]+)/branch", path)
            if branch_match:
                run_id = branch_match.group(1)
                snapshot_seq = payload.get("snapshot_sequence")
                overrides = payload.get("overrides", {})
                try:
                    branched_run = engine.branch_run(run_id, snapshot_seq, overrides)
                    self.respond(201, {"run": branched_run})
                except ValueError as exc:
                    self.respond(404, {"error": str(exc)})
                return
            baseline_match = re.fullmatch(r"/api/runs/([^/]+)/baseline", path)
            if baseline_match:
                run_id = baseline_match.group(1)
                try:
                    res = store.set_baseline_run(run_id)
                    self.respond(200, res)
                except ValueError as exc:
                    self.respond(404, {"error": str(exc)})
                return
            suite_run_match = re.fullmatch(r"/api/suites/([^/]+)/runs", path)
            if suite_run_match:
                suite_id = suite_run_match.group(1)
                impl = payload.get("implementation", "fixed")
                sync = payload.get("synchronous", False)
                try:
                    res = engine.execute_suite_run(suite_id, implementation=impl, synchronous=sync)
                    self.respond(202 if not sync else 200, {"suite_run": res})
                except ValueError as exc:
                    self.respond(404, {"error": str(exc)})
                return
            suite_scen_match = re.fullmatch(r"/api/suites/([^/]+)/scenarios", path)
            if suite_scen_match:
                suite_id = suite_scen_match.group(1)
                scenario_id = payload.get("scenario_id")
                if not scenario_id:
                    raise ValueError("scenario_id is required")
                try:
                    res = store.add_scenario_to_suite(suite_id, scenario_id)
                    self.respond(201, res)
                except ValueError as exc:
                    self.respond(404, {"error": str(exc)})
                return
            clone_match = re.fullmatch(r"/api/scenarios/([^/]+)/versions/([^/]+)/clone", path)
            if clone_match:
                scenario_id = clone_match.group(1)
                version = int(clone_match.group(2))
                target_sc_id = payload.get("target_scenario_id")
                name_suffix = payload.get("name_suffix", " (Clone)")
                try:
                    res = store.clone_scenario_version(scenario_id, version, target_sc_id, name_suffix)
                    self.respond(201, res)
                except ValueError as exc:
                    self.respond(404, {"error": str(exc)})
                return
            ann_match = re.fullmatch(r"/api/runs/([^/]+)/annotations", path)
            if ann_match:
                run_id = ann_match.group(1)
                if not store.get_run(run_id):
                    self.respond(404, {"error": "run not found"})
                    return
                author = payload.get("author", "Developer")
                comment = payload.get("comment", "").strip()
                if not comment:
                    raise ValueError("comment is required")
                seq = payload.get("sequence_number")
                annotation = store.add_annotation(run_id, author, comment, seq)
                store.record_audit_event("faultline-demo", author, "ANNOTATION_ADDED", "run", run_id, {"comment": comment[:80]})
                self.respond(201, {"annotation": annotation})
                return
            task_match = re.fullmatch(r"/api/runs/([^/]+)/tasks", path)
            if task_match:
                run_id = task_match.group(1)
                if not store.get_run(run_id):
                    self.respond(404, {"error": "run not found"})
                    return
                title = payload.get("title", "").strip()
                if not title:
                    raise ValueError("task title is required")
                description = payload.get("description", "")
                priority = payload.get("priority", "medium")
                assignee = payload.get("assignee", "Developer")
                task = store.create_task("faultline-demo", run_id, title, description, priority, assignee)
                store.record_audit_event("faultline-demo", assignee, "TASK_CREATED", "task", task["id"], {"title": title, "priority": priority})
                self.respond(201, {"task": task})
                return
            if path == "/api/scenarios/generate-from-gaps":
                project_id = payload.get("project_id", "faultline-demo")
                generated = store.generate_scenarios_from_gaps(project_id)
                store.record_audit_event(project_id, "developer", "SCENARIOS_GENERATED_FROM_GAPS", "project", project_id, {"count": len(generated)})
                self.respond(201, {"generated": generated, "count": len(generated)})
                return
            if path == "/api/workflows/import-openapi":
                spec_data = payload.get("spec") or payload.get("spec_text") or payload
                workflow = importer.import_openapi_spec(spec_data)
                store.record_audit_event("faultline-demo", "developer", "OPENAPI_IMPORTED", "api", workflow["title"], {"endpoints": workflow["endpoints_count"]})
                self.respond(200, {"workflow": workflow})
                return
            template_match = re.fullmatch(r"/api/templates/([^/]+)/instantiate", path)
            if template_match:
                template_id = template_match.group(1)
                scenario_id = payload.get("scenario_id") or f"scenario-{uuid4().hex[:6]}"
                name = payload.get("name")
                parameters = payload.get("parameters")
                instance = store.instantiate_template(template_id, scenario_id, name, parameters)
                store.record_audit_event("faultline-demo", "developer", "TEMPLATE_INSTANTIATED", "scenario", scenario_id, {"template_id": template_id})
                self.respond(201, {"instance": instance})
                return
            if path == "/api/scenarios/import-incident":
                title = payload.get("title", "").strip()
                if not title:
                    raise ValueError("incident title is required")
                fault = payload.get("fault", "payment_response_lost_after_commit")
                if fault not in SUPPORTED_FAULTS:
                    raise ValueError("unsupported fault type")
                order_id = payload.get("order_id") or f"order-inc-{uuid4().hex[:6]}"
                amount_cents = int(payload.get("amount_cents", 2500))
                retries = int(payload.get("retries", 1))
                definition = {
                    "scenario_version": 1,
                    "name": title,
                    "description": payload.get("description", f"Imported incident: {title}"),
                    "order_id": order_id,
                    "amount_cents": amount_cents,
                    "fault": fault,
                    "retries": retries,
                    "assertion": SUPPORTED_FAULTS[fault]["primary_assertion"],
                }
                if fault == "duplicate_payment_webhook":
                    definition["webhook_deliveries"] = int(payload.get("webhook_deliveries", 2))
                    definition["delay_ms"] = int(payload.get("delay_ms", 100))
                validate_scenario(definition)
                scenario_id = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:64]
                if any(item["id"] == scenario_id for item in store.list_scenarios()):
                    scenario_id = f"{scenario_id}-{uuid4().hex[:4]}"
                definition["name"] = scenario_id
                store.create_scenario(scenario_id, "faultline-demo", definition)
                self.respond(201, {"scenario_id": scenario_id, "version": 1, "definition": definition})
                return
            if path == "/api/scenarios/validate":
                definition = payload.get("definition", payload)
                validate_scenario(definition)
                self.respond(200, {"valid": True, "errors": []})
                return
            if path == "/api/scenarios":
                definition = payload.get("definition")
                validate_scenario(definition)
                scenario_id = re.sub(r"[^a-z0-9]+", "-", definition["name"].lower()).strip("-")[:64]
                if any(item["id"] == scenario_id for item in store.list_scenarios()): raise ValueError("scenario name already exists")
                store.create_scenario(scenario_id, "faultline-demo", definition)
                store.record_audit_event("faultline-demo", "developer", "SCENARIO_CREATED", "scenario", scenario_id, {})
                self.respond(201, {"scenario_id": scenario_id, "version": 1})
                return
            if path == "/api/suites":
                suite_id = payload.get("id") or re.sub(r"[^a-z0-9]+", "-", payload.get("name", "").lower()).strip("-")[:64]
                if not suite_id:
                    raise ValueError("suite id or name is required")
                name = payload.get("name") or suite_id
                description = payload.get("description", "")
                scenarios = payload.get("scenarios", [])
                if not isinstance(scenarios, list) or not scenarios:
                    raise ValueError("scenarios must be a non-empty list of scenario IDs")
                store.create_suite(suite_id, payload.get("project_id", "faultline-demo"), name, description, scenarios)
                store.record_audit_event("faultline-demo", "developer", "SUITE_CREATED", "suite", suite_id, {"scenarios": scenarios})
                self.respond(201, {"suite_id": suite_id})
                return
            version_match = re.fullmatch(r"/api/scenarios/([^/]+)/versions", path)
            if version_match:
                scenario_id = version_match.group(1)
                if not any(item["id"] == scenario_id for item in store.list_scenarios()):
                    self.respond(404, {"error": "scenario not found"}); return
                definition = payload.get("definition")
                validate_scenario(definition)
                version = store.add_scenario_version(scenario_id, definition)
                store.record_audit_event("faultline-demo", "developer", "SCENARIO_VERSIONED", "scenario", scenario_id, {"version": version})
                self.respond(201, {"scenario_id": scenario_id, "version": version})
                return
            scenario_id = payload.get("scenario_id", "payment-response-lost")
            implementation = payload.get("implementation", "vulnerable")
            if implementation not in {"vulnerable", "fixed"}:
                raise ValueError("implementation must be vulnerable or fixed")
            scenario = next((item for item in store.list_scenarios() if item["id"] == scenario_id), None)
            if scenario is None:
                self.respond(404, {"error": "scenario not found"})
                return
            validate_scenario(scenario["definition"])
            run_id = str(uuid4())
            store.create_run(run_id, scenario_id, scenario["version"], implementation)
            store.record_event(run_id, "RUN_QUEUED", {"scenario_id": scenario_id})
            store.record_audit_event("faultline-demo", "developer", "RUN_SUBMITTED", "run", run_id, {"scenario_id": scenario_id, "implementation": implementation})
            submit(run_id, scenario["definition"], implementation)
            logger.info("submitted run_id=%s scenario=%s implementation=%s", run_id, scenario_id, implementation)
            self.respond(202, {"run_id": run_id, "status": "queued"})
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
            self.respond(400, {"error": str(exc)})

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        task_match = re.fullmatch(r"/api/tasks/([^/]+)", path)
        if not task_match:
            self.respond(404, {"error": "not found"})
            return
        task_id = task_match.group(1)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            updated = store.update_task(
                task_id,
                status=payload.get("status"),
                assignee=payload.get("assignee"),
                priority=payload.get("priority"),
            )
            if updated:
                store.record_audit_event("faultline-demo", updated.get("assignee", "developer"), "TASK_UPDATED", "task", task_id, payload)
                self.respond(200, {"task": updated})
            else:
                self.respond(404, {"error": "task not found"})
        except (ValueError, json.JSONDecodeError) as exc:
            self.respond(400, {"error": str(exc)})

    def static_file(self, filename: str, content_type: str) -> None:
        try:
            raw = (WEB_ROOT / filename).read_bytes()
        except OSError:
            self.respond(404, {"error": "web asset not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, message_format: str, *args: Any) -> None:
        print(f"[control-plane] {message_format % args}")


def main() -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="Start the local Faultline integration testing platform")
    parser.add_argument("--host", default="127.0.0.1", help="control-plane bind address (default: loopback)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--debug", action="store_true", help="enable detailed run and adapter logs")
    parser.add_argument("--no-embedded-services", action="store_true", help="disable embedded checkout and payment services for multi-container deployments")
    args = parser.parse_args()

    allow_external = os.environ.get("FAULTLINE_ALLOW_EXTERNAL") == "1"
    if args.host not in {"127.0.0.1", "localhost"} and not (allow_external and args.host == "0.0.0.0"):
        parser.error("the initial release is local-only; bind to loopback (or set FAULTLINE_ALLOW_EXTERNAL=1 for containerized setups)")
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    store.initialize()
    if not (args.no_embedded_services or os.environ.get("FAULTLINE_NO_EMBEDDED_SERVICES") == "1"):
        local_services = start_local_services()
    else:
        local_services = []
    server = ThreadingHTTPServer((args.host, args.port), ControlPlaneHandler)
    print(f"Faultline is ready at http://{args.host}:{args.port}")
    print("Local test targets: checkout 127.0.0.1:8766, payment provider 127.0.0.1:8767")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Faultline...")
    finally:
        server.shutdown()
        server.server_close()
        for local_service in local_services:
            local_service.shutdown()
            local_service.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
