"""Faultline control-plane API and local product web server."""

import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from . import store
from .adapters import start_local_services
from .engine import submit


WEB_ROOT = Path(__file__).resolve().parent / "web"
logger = logging.getLogger("faultline.control")


def validate_scenario(definition: dict[str, Any]) -> None:
    if definition.get("scenario_version") != 1:
        raise ValueError("unsupported scenario_version")
    if definition.get("fault") != "payment_response_lost_after_commit":
        raise ValueError("unsupported fault type")
    if definition.get("assertion") != "no_duplicate_charge":
        raise ValueError("unsupported primary assertion")
    if not isinstance(definition.get("name"), str) or not definition["name"]:
        raise ValueError("scenario name is required")
    if not isinstance(definition.get("order_id"), str) or not definition["order_id"]:
        raise ValueError("order_id is required")
    if type(definition.get("amount_cents")) is not int or definition["amount_cents"] < 1:
        raise ValueError("amount_cents must be a positive integer")
    if type(definition.get("retries")) is not int or not 0 <= definition["retries"] <= 3:
        raise ValueError("retries must be between zero and three")


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
        elif path == "/api/scenarios":
            self.respond(200, {"scenarios": store.list_scenarios()})
        elif re.fullmatch(r"/api/scenarios/[^/]+/versions", path):
            scenario_id = path.split("/")[3]
            versions = store.scenario_versions(scenario_id)
            self.respond(200, {"versions": versions}) if versions else self.respond(404, {"error": "scenario not found"})
        elif path == "/api/runs":
            self.respond(200, {"runs": store.list_runs()})
        elif path.startswith("/api/runs/"):
            parts = path.removeprefix("/api/runs/").split("/")
            run_id = parts[0]
            if len(parts) == 2 and parts[1] == "reproduce":
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
        if path not in {"/api/runs", "/api/scenarios", "/api/scenarios/validate"} and not re.fullmatch(r"/api/scenarios/[^/]+/versions", path):
            self.respond(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 16_384:
                self.respond(413, {"error": "request too large"})
                return
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict): raise ValueError("request body must be a JSON object")
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
                self.respond(201, {"scenario_id": scenario_id, "version": 1})
                return
            version_match = re.fullmatch(r"/api/scenarios/([^/]+)/versions", path)
            if version_match:
                scenario_id = version_match.group(1)
                if not any(item["id"] == scenario_id for item in store.list_scenarios()):
                    self.respond(404, {"error": "scenario not found"}); return
                definition = payload.get("definition")
                validate_scenario(definition)
                version = store.add_scenario_version(scenario_id, definition)
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
            submit(run_id, scenario["definition"], implementation)
            logger.info("submitted run_id=%s scenario=%s implementation=%s", run_id, scenario_id, implementation)
            self.respond(202, {"run_id": run_id, "status": "queued"})
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
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

    parser = argparse.ArgumentParser(description="Start the local Faultline integration testing platform")
    parser.add_argument("--host", default="127.0.0.1", help="control-plane bind address (default: loopback)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--debug", action="store_true", help="enable detailed run and adapter logs")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        parser.error("the initial release is local-only; bind to loopback")
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    store.initialize()
    local_services = start_local_services()
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
