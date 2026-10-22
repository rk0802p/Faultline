"""Small SQLite persistence layer for projects, scenarios, runs, and evidence."""

import json
import hashlib
import os
import socket
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import security
from .schema import SUPPORTED_FAULTS, validate_scenario, validate_template_parameters


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "data" if (ROOT / "scenarios").is_dir() else Path.cwd() / "data"
DATA_DIR = Path(os.environ.get("FAULTLINE_DATA_DIR", DEFAULT_DATA_DIR))
DB_PATH = DATA_DIR / "faultline.sqlite3"
SYSTEM_START_TIME = time.time()


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def database() -> Any:
    connection = connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize() -> None:
    pkg_scenarios = Path(__file__).resolve().parent / "scenarios"
    root_scenarios = ROOT / "scenarios"
    scenario_files = sorted(pkg_scenarios.glob("*.json")) or sorted(root_scenarios.glob("*.json"))
    with database() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scenarios (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                name TEXT NOT NULL,
                definition_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scenario_versions (
                scenario_id TEXT NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
                version INTEGER NOT NULL,
                definition_json TEXT NOT NULL,
                checksum TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(scenario_id, version)
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL REFERENCES scenarios(id),
                scenario_version INTEGER NOT NULL DEFAULT 1,
                implementation TEXT NOT NULL,
                status TEXT NOT NULL,
                verdict TEXT NOT NULL DEFAULT 'pending',
                started_at TEXT NOT NULL,
                finished_at TEXT,
                summary_json TEXT NOT NULL DEFAULT '{}',
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                recorded_at TEXT NOT NULL,
                type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(run_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS assertions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                expected TEXT NOT NULL,
                observed TEXT NOT NULL,
                evidence_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS test_suites (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                name TEXT NOT NULL,
                description TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS suite_scenarios (
                suite_id TEXT NOT NULL REFERENCES test_suites(id) ON DELETE CASCADE,
                scenario_id TEXT NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                PRIMARY KEY(suite_id, scenario_id)
            );
            CREATE TABLE IF NOT EXISTS run_annotations (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                author TEXT NOT NULL,
                comment TEXT NOT NULL,
                sequence_number INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_tasks (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                priority TEXT NOT NULL DEFAULT 'medium',
                assignee TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS scenario_templates (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                description TEXT NOT NULL,
                parameters_schema_json TEXT NOT NULL,
                default_definition_json TEXT NOT NULL,
                expected_invariants_json TEXT NOT NULL,
                cleanup_strategy TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                scenario_version INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS suite_runs (
                id TEXT PRIMARY KEY,
                suite_id TEXT NOT NULL REFERENCES test_suites(id) ON DELETE CASCADE,
                implementation TEXT NOT NULL,
                status TEXT NOT NULL,
                verdict TEXT NOT NULL DEFAULT 'pending',
                total_scenarios INTEGER NOT NULL,
                passed_scenarios INTEGER NOT NULL DEFAULT 0,
                failed_scenarios INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                run_ids_json TEXT NOT NULL DEFAULT '[]',
                started_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS run_spans (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                trace_id TEXT NOT NULL,
                span_id TEXT NOT NULL,
                parent_span_id TEXT,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                start_time_unix_nano INTEGER NOT NULL,
                end_time_unix_nano INTEGER NOT NULL,
                status_code TEXT NOT NULL,
                status_message TEXT,
                attributes_json TEXT NOT NULL DEFAULT '{}',
                events_json TEXT NOT NULL DEFAULT '[]'
            );
            """
        )
        run_columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
        if "scenario_version" not in run_columns:
            db.execute("ALTER TABLE runs ADD COLUMN scenario_version INTEGER NOT NULL DEFAULT 1")
        if "cleanup_json" not in run_columns:
            db.execute("ALTER TABLE runs ADD COLUMN cleanup_json TEXT NOT NULL DEFAULT '{}'")
        if "parent_run_id" not in run_columns:
            db.execute("ALTER TABLE runs ADD COLUMN parent_run_id TEXT")
        if "forked_from_snapshot_id" not in run_columns:
            db.execute("ALTER TABLE runs ADD COLUMN forked_from_snapshot_id INTEGER")
        if "trace_id" not in run_columns:
            db.execute("ALTER TABLE runs ADD COLUMN trace_id TEXT")
        if "is_baseline" not in run_columns:
            db.execute("ALTER TABLE runs ADD COLUMN is_baseline INTEGER NOT NULL DEFAULT 0")
        if "definition_override_json" not in run_columns:
            db.execute("ALTER TABLE runs ADD COLUMN definition_override_json TEXT")
        db.execute(
            "INSERT OR IGNORE INTO projects VALUES (?, ?, ?, ?)",
            ("faultline-demo", "Checkout reliability lab", "Local, isolated checkout integration environment.", now()),
        )
        for scenario_file in scenario_files:
            definition = json.loads(scenario_file.read_text(encoding="utf-8"))
            scenario_id = definition["name"]
            existing_row = db.execute("SELECT definition_json FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
            if not existing_row:
                db.execute(
                    "INSERT INTO scenarios VALUES (?, ?, ?, ?, ?)",
                    (scenario_id, "faultline-demo", scenario_id, json.dumps(definition), now()),
                )
                _insert_scenario_version(db, scenario_id, 1, definition)
            else:
                existing_def = json.loads(existing_row["definition_json"])
                if existing_def != definition:
                    db.execute(
                        "UPDATE scenarios SET definition_json=?, name=? WHERE id=?",
                        (json.dumps(definition), scenario_id, scenario_id),
                    )
                    next_ver = (db.execute(
                        "SELECT MAX(version) AS max_v FROM scenario_versions WHERE scenario_id=?",
                        (scenario_id,)
                    ).fetchone()["max_v"] or 0) + 1
                    _insert_scenario_version(db, scenario_id, next_ver, definition)

        db.execute(
            "INSERT OR IGNORE INTO test_suites VALUES (?, ?, ?, ?, ?)",
            ("critical-integrations", "faultline-demo", "Critical Integrations Suite",
             "Core resilience suite covering payment loss, duplicate webhooks, and persistence failure.", now()),
        )
        default_suite_scenarios = [
            ("critical-integrations", "payment-response-lost", 1),
            ("critical-integrations", "duplicate-payment-webhook", 2),
            ("critical-integrations", "partial-persistence-failure", 3),
        ]
        for s_id, sc_id, seq in default_suite_scenarios:
            db.execute(
                "INSERT OR IGNORE INTO suite_scenarios VALUES (?, ?, ?)",
                (s_id, sc_id, seq),
            )

        templates = [
            (
                "payment-response-lost-tpl",
                "Payment Response Lost After Provider Commit",
                "network_failure",
                "Verifies checkout handles lost HTTP response after provider commits charge. Tests whether retry uses an idempotency key to prevent duplicate charges.",
                json.dumps({
                    "order_id": {"type": "string", "default": "order-tpl-001"},
                    "amount_cents": {"type": "integer", "min": 1, "default": 2500},
                    "retries": {"type": "integer", "min": 0, "max": 3, "default": 1},
                    "timeout_seconds": {"type": "integer", "min": 2, "max": 60, "default": 15},
                }),
                json.dumps({
                    "scenario_version": 1,
                    "name": "payment-response-lost",
                    "description": "Detect duplicate charges when the provider commits but checkout loses the response.",
                    "order_id": "order-tpl-001",
                    "amount_cents": 2500,
                    "fault": "payment_response_lost_after_commit",
                    "retries": 1,
                    "assertion": "no_duplicate_charge",
                }),
                json.dumps(["fault_activation_confirmed", "no_duplicate_charge", "payment_matches_ledger", "checkout_terminal_state"]),
                "Purges provider ledger and resets checkout idempotency cache.",
            ),
            (
                "duplicate-payment-webhook-tpl",
                "Duplicate Payment Webhook Delivery",
                "webhook_deduplication",
                "Verifies fulfillment consumer deduplicates redelivered payment succeeded webhooks without multiple inventory reservations or shipments.",
                json.dumps({
                    "order_id": {"type": "string", "default": "order-tpl-002"},
                    "amount_cents": {"type": "integer", "min": 1, "default": 4200},
                    "webhook_deliveries": {"type": "integer", "min": 2, "max": 5, "default": 2},
                    "delay_ms": {"type": "integer", "min": 0, "max": 5000, "default": 100},
                    "timeout_seconds": {"type": "integer", "min": 2, "max": 60, "default": 15},
                }),
                json.dumps({
                    "scenario_version": 1,
                    "name": "duplicate-payment-webhook",
                    "description": "Verify webhook receiver deduplicates redelivered payment succeeded webhooks.",
                    "order_id": "order-tpl-002",
                    "amount_cents": 4200,
                    "fault": "duplicate_payment_webhook",
                    "webhook_deliveries": 2,
                    "delay_ms": 100,
                    "assertion": "idempotent_webhook_processing",
                }),
                json.dumps(["fault_activation_confirmed", "idempotent_webhook_processing", "webhook_deliveries_acknowledged", "order_terminal_state"]),
                "Clears processed webhook idempotency ledger and resets fulfillment records.",
            ),
            (
                "partial-persistence-failure-tpl",
                "Partial Persistence Failure After Charge",
                "database_failure",
                "Verifies that when database persistence fails after provider commits payment, a compensating refund reconciles the transaction.",
                json.dumps({
                    "order_id": {"type": "string", "default": "order-tpl-003"},
                    "amount_cents": {"type": "integer", "min": 1, "default": 5500},
                    "retries": {"type": "integer", "min": 0, "max": 3, "default": 0},
                    "compensate": {"type": "boolean", "default": True},
                    "timeout_seconds": {"type": "integer", "min": 2, "max": 60, "default": 15},
                }),
                json.dumps({
                    "scenario_version": 1,
                    "name": "partial-persistence-failure",
                    "description": "Verify compensating refund reconciles payment when local database persistence fails after external commit.",
                    "order_id": "order-tpl-003",
                    "amount_cents": 5500,
                    "fault": "persistence_failure_after_payment",
                    "retries": 0,
                    "compensate": True,
                    "assertion": "charge_compensated_on_failure",
                }),
                json.dumps(["fault_activation_confirmed", "charge_compensated_on_failure", "no_orphaned_charge", "checkout_terminal_state"]),
                "Purges provider refunds ledger and resets temporary order rows.",
            ),
        ]
        for t_id, name, cat, desc, p_schema, d_def, exp_inv, cleanup_strat in templates:
            db.execute(
                """
                INSERT INTO scenario_templates (id, name, category, description, parameters_schema_json, default_definition_json, expected_invariants_json, cleanup_strategy, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    category = excluded.category,
                    description = excluded.description,
                    parameters_schema_json = excluded.parameters_schema_json,
                    default_definition_json = excluded.default_definition_json,
                    expected_invariants_json = excluded.expected_invariants_json,
                    cleanup_strategy = excluded.cleanup_strategy
                """,
                (t_id, name, cat, desc, p_schema, d_def, exp_inv, cleanup_strat, now()),
            )

        # Prune legacy or unverified templates so the database remains truthful
        db.execute(
            "DELETE FROM scenario_templates WHERE id NOT IN (?, ?, ?)",
            ("payment-response-lost-tpl", "duplicate-payment-webhook-tpl", "partial-persistence-failure-tpl"),
        )


def _insert_scenario_version(db: sqlite3.Connection, scenario_id: str, version: int, definition: dict[str, Any]) -> None:
    raw = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    db.execute("INSERT INTO scenario_versions VALUES (?, ?, ?, ?, ?)",
               (scenario_id, version, raw, hashlib.sha256(raw.encode()).hexdigest(), now()))


def list_projects() -> list[dict[str, Any]]:
    with database() as db:
        return [dict(row) for row in db.execute("SELECT * FROM projects ORDER BY name")]


def create_project(project_id: str, name: str, description: str = "") -> None:
    with database() as db:
        db.execute(
            "INSERT INTO projects (id, name, description, created_at) VALUES (?, ?, ?, ?)",
            (project_id, name, description, now()),
        )


def get_project(project_id: str) -> dict[str, Any] | None:
    with database() as db:
        row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return dict(row) if row else None


def default_project_id() -> str:
    with database() as db:
        row = db.execute("SELECT id FROM projects ORDER BY created_at LIMIT 1").fetchone()
        return row["id"] if row else "faultline-demo"


def list_scenarios() -> list[dict[str, Any]]:
    with database() as db:
        rows = db.execute("SELECT * FROM scenarios ORDER BY name").fetchall()
        return [
            {"id": row["id"], "project_id": row["project_id"], "name": row["name"],
             "definition": json.loads(row["definition_json"]), "created_at": row["created_at"],
             "version": _latest_version(db, row["id"])}
            for row in rows
        ]


def get_scenario(scenario_id: str) -> dict[str, Any] | None:
    return next((s for s in list_scenarios() if s["id"] == scenario_id), None)


def _latest_version(db: sqlite3.Connection, scenario_id: str) -> int:
    row = db.execute("SELECT MAX(version) FROM scenario_versions WHERE scenario_id=?", (scenario_id,)).fetchone()
    return row[0] or 1


def create_scenario(scenario_id: str, project_id: str, definition: dict[str, Any]) -> None:
    with database() as db:
        db.execute("INSERT INTO scenarios VALUES (?, ?, ?, ?, ?)",
                   (scenario_id, project_id, definition["name"], json.dumps(definition), now()))
        _insert_scenario_version(db, scenario_id, 1, definition)


def add_scenario_version(scenario_id: str, definition: dict[str, Any]) -> int:
    with database() as db:
        row = db.execute("SELECT MAX(version) FROM scenario_versions WHERE scenario_id=?", (scenario_id,)).fetchone()
        version = (row[0] or 0) + 1
        _insert_scenario_version(db, scenario_id, version, definition)
        db.execute("UPDATE scenarios SET name=?, definition_json=? WHERE id=?",
                   (definition["name"], json.dumps(definition), scenario_id))
        return version


def scenario_versions(scenario_id: str) -> list[dict[str, Any]]:
    with database() as db:
        return [{"version": row["version"], "definition": json.loads(row["definition_json"]),
                 "checksum": row["checksum"], "created_at": row["created_at"]}
                for row in db.execute("SELECT * FROM scenario_versions WHERE scenario_id=? ORDER BY version DESC", (scenario_id,))]


def create_run(
    run_id: str,
    scenario_id: str,
    scenario_version: int,
    implementation: str,
    parent_run_id: str | None = None,
    forked_from_snapshot_id: int | None = None,
    trace_id: str | None = None,
    definition_override: dict[str, Any] | None = None,
) -> None:
    if not trace_id:
        trace_id = uuid4().hex
    override_json = json.dumps(definition_override) if definition_override is not None else None
    with database() as db:
        db.execute(
            """INSERT INTO runs(
                id, scenario_id, scenario_version, implementation, status, started_at,
                parent_run_id, forked_from_snapshot_id, trace_id, is_baseline, definition_override_json
            ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, 0, ?)""",
            (run_id, scenario_id, scenario_version, implementation, now(), parent_run_id, forked_from_snapshot_id, trace_id, override_json),
        )


def transition_run(run_id: str, status: str, *, verdict: str = "pending", error: str | None = None,
                   summary: dict[str, Any] | None = None, finished: bool = False) -> bool:
    allowed = {
        "queued": {"running", "cancel_requested", "cancelled", "failed", "timed_out"},
        "running": {"cancel_requested", "cancelled", "completed", "failed", "timed_out"},
        "cancel_requested": {"cancelled", "timed_out", "failed"},
    }
    with database() as db:
        row = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None or status not in allowed.get(row["status"], set()):
            return False
        sanitized_error = security.redact_sensitive(error) if error else None
        sanitized_summary = json.dumps(security.redact_sensitive(summary)) if summary is not None else None
        db.execute("UPDATE runs SET status=?, verdict=?, summary_json=COALESCE(?, summary_json), error=?, finished_at=? WHERE id=?",
                   (status, verdict, sanitized_summary, sanitized_error,
                    now() if finished else None, run_id))
        return True


def run_status(run_id: str) -> str | None:
    with database() as db:
        row = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        return row["status"] if row else None


def request_cancel(run_id: str) -> bool:
    return transition_run(run_id, "cancel_requested")


def record_event(run_id: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    sanitized = security.redact_sensitive(payload or {})
    with database() as db:
        sequence = db.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        recorded_at = now()
        db.execute(
            "INSERT INTO run_events(run_id, sequence, recorded_at, type, payload_json) VALUES (?, ?, ?, ?, ?)",
            (run_id, sequence, recorded_at, event_type, json.dumps(sanitized)),
        )
    return {"sequence": sequence, "recorded_at": recorded_at, "type": event_type, "payload": sanitized}


def record_cleanup(
    run_id: str,
    status: str,
    owned_resources: list[str],
    error: str | None = None,
    duration_ms: int = 0,
) -> None:
    data = {
        "status": status,
        "cleaned_at": now(),
        "owned_resources": owned_resources,
        "error": error,
        "duration_ms": duration_ms,
    }
    clean_data = security.redact_sensitive(data)
    with database() as db:
        db.execute("UPDATE runs SET cleanup_json=? WHERE id=?", (json.dumps(clean_data), run_id))


def replace_assertions(run_id: str, assertions: list[dict[str, Any]]) -> None:
    with database() as db:
        db.execute("DELETE FROM assertions WHERE run_id=?", (run_id,))
        db.executemany(
            "INSERT INTO assertions(run_id, name, status, expected, observed, evidence_json) VALUES (?, ?, ?, ?, ?, ?)",
            [(run_id, item["name"], item["status"], str(item.get("expected", "")), str(item.get("observed", "")),
              json.dumps(security.redact_sensitive(item.get("evidence", {})))) for item in assertions],
        )


def get_run(run_id: str) -> dict[str, Any] | None:
    with database() as db:
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        scenario = db.execute("SELECT * FROM scenario_versions WHERE scenario_id=? AND version=?",
                              (row["scenario_id"], row["scenario_version"])).fetchone()
        if scenario is None:
            scenario = db.execute("SELECT definition_json FROM scenarios WHERE id=?", (row["scenario_id"],)).fetchone()
        events = db.execute(
            "SELECT sequence, recorded_at, type, payload_json FROM run_events WHERE run_id=? ORDER BY sequence",
            (run_id,),
        ).fetchall()
        assertions = db.execute(
            "SELECT name, status, expected, observed, evidence_json FROM assertions WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
        result = dict(row)
        result["summary"] = json.loads(result.pop("summary_json"))
        override_raw = result.pop("definition_override_json", None)
        if override_raw:
            try:
                result["scenario"] = json.loads(override_raw)
            except Exception:
                result["scenario"] = json.loads(scenario["definition_json"]) if scenario else {}
        else:
            result["scenario"] = json.loads(scenario["definition_json"]) if scenario else {}
        cleanup_raw = result.pop("cleanup_json", None)
        cleanup_data = {}
        if cleanup_raw:
            try:
                cleanup_data = json.loads(cleanup_raw)
            except Exception:
                pass
        result["cleanup"] = cleanup_data or {"status": "untracked", "owned_resources": []}
        result["parent_run_id"] = result.get("parent_run_id")
        result["forked_from_snapshot_id"] = result.get("forked_from_snapshot_id")
        result["trace_id"] = result.get("trace_id")
        result["is_baseline"] = bool(result.get("is_baseline", 0))
        result["events"] = [
            {"sequence": event["sequence"], "recorded_at": event["recorded_at"], "type": event["type"],
             "payload": json.loads(event["payload_json"])} for event in events
        ]
        result["assertions"] = [
            {"name": item["name"], "status": item["status"], "expected": item["expected"],
             "observed": item["observed"], "evidence": json.loads(item["evidence_json"])}
            for item in assertions
        ]
        return result


def list_templates() -> list[dict[str, Any]]:
    with database() as db:
        rows = db.execute("SELECT * FROM scenario_templates ORDER BY category, name").fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "category": r["category"],
                "description": r["description"],
                "parameters_schema": json.loads(r["parameters_schema_json"]),
                "default_definition": json.loads(r["default_definition_json"]),
                "expected_invariants": json.loads(r["expected_invariants_json"]),
                "cleanup_strategy": r["cleanup_strategy"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]


def get_template(template_id: str) -> dict[str, Any] | None:
    with database() as db:
        row = db.execute("SELECT * FROM scenario_templates WHERE id=?", (template_id,)).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "category": row["category"],
            "description": row["description"],
            "parameters_schema": json.loads(row["parameters_schema_json"]),
            "default_definition": json.loads(row["default_definition_json"]),
            "expected_invariants": json.loads(row["expected_invariants_json"]),
            "cleanup_strategy": row["cleanup_strategy"],
            "created_at": row["created_at"],
        }


def instantiate_template(
    template_id: str,
    scenario_id: str,
    name: str | None = None,
    parameters: dict[str, Any] | None = None,
    project_id: str = "faultline-demo",
) -> dict[str, Any]:
    tpl = get_template(template_id)
    if not tpl:
        raise ValueError(f"Template '{template_id}' not found")
    definition = dict(tpl["default_definition"])
    definition["name"] = scenario_id
    if name:
        definition["description"] = f"{tpl['description']} (Instance: {name})"
    if parameters is not None:
        validate_template_parameters(tpl["parameters_schema"], parameters)
        for k, v in parameters.items():
            definition[k] = v
    validate_scenario(definition)
    create_scenario(scenario_id, project_id, definition)
    return {
        "scenario_id": scenario_id,
        "template_id": template_id,
        "project_id": project_id,
        "definition": definition,
        "expected_invariants": tpl["expected_invariants"],
        "cleanup_strategy": tpl["cleanup_strategy"],
    }


def get_reproduction_bundle(run_id: str) -> dict[str, Any] | None:
    run = get_run(run_id)
    if run is None:
        return None
    bundle = {
        "format": "faultline-repro-v1",
        "run": run,
        "replay": {
            "command": f"python -m faultline.cli run --scenario {run['scenario_id']} --implementation {run['implementation']}",
            "note": "Replays the recorded scenario against the local deterministic test services.",
        },
    }
    if run.get("parent_run_id"):
        bundle["branch"] = {
            "parent_run_id": run["parent_run_id"],
            "forked_from_snapshot_id": run.get("forked_from_snapshot_id"),
            "effective_scenario": run.get("scenario"),
        }
    return security.redact_sensitive(bundle)


def list_runs(limit: int = 30) -> list[dict[str, Any]]:
    with database() as db:
        rows = db.execute(
            "SELECT r.*, s.name AS scenario_name FROM runs r JOIN scenarios s ON s.id=r.scenario_id "
            "ORDER BY r.started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for row in rows:
            cleanup_data = {}
            if "cleanup_json" in row.keys() and row["cleanup_json"]:
                try:
                    cleanup_data = json.loads(row["cleanup_json"])
                except Exception:
                    pass
            result.append({
                "id": row["id"],
                "scenario_id": row["scenario_id"],
                "scenario_version": row["scenario_version"],
                "scenario": row["scenario_name"],
                "implementation": row["implementation"],
                "status": row["status"],
                "verdict": row["verdict"],
                "cleanup": cleanup_data or {"status": "untracked", "owned_resources": []},
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "summary": json.loads(row["summary_json"]),
                "error": row["error"],
                "parent_run_id": row["parent_run_id"] if "parent_run_id" in row.keys() else None,
                "forked_from_snapshot_id": row["forked_from_snapshot_id"] if "forked_from_snapshot_id" in row.keys() else None,
                "trace_id": row["trace_id"] if "trace_id" in row.keys() else None,
                "is_baseline": bool(row["is_baseline"]) if "is_baseline" in row.keys() else False,
            })
        return result


def list_suites() -> list[dict[str, Any]]:
    with database() as db:
        rows = db.execute("SELECT * FROM test_suites ORDER BY name").fetchall()
        result = []
        for row in rows:
            scenarios = [
                r[0] for r in db.execute(
                    "SELECT scenario_id FROM suite_scenarios WHERE suite_id=? ORDER BY sequence",
                    (row["id"],)
                ).fetchall()
            ]
            result.append({
                "id": row["id"],
                "project_id": row["project_id"],
                "name": row["name"],
                "description": row["description"],
                "created_at": row["created_at"],
                "scenarios": scenarios,
            })
        return result


def get_suite(suite_id: str) -> dict[str, Any] | None:
    with database() as db:
        row = db.execute("SELECT * FROM test_suites WHERE id=?", (suite_id,)).fetchone()
        if row is None:
            return None
        scenarios = [
            r[0] for r in db.execute(
                "SELECT scenario_id FROM suite_scenarios WHERE suite_id=? ORDER BY sequence",
                (suite_id,)
            ).fetchall()
        ]
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "name": row["name"],
            "description": row["description"],
            "created_at": row["created_at"],
            "scenarios": scenarios,
        }


def create_suite(suite_id: str, project_id: str, name: str, description: str, scenario_ids: list[str]) -> None:
    with database() as db:
        db.execute("INSERT INTO test_suites VALUES (?, ?, ?, ?, ?)",
                   (suite_id, project_id, name, description, now()))
        for seq, sc_id in enumerate(scenario_ids, 1):
            db.execute("INSERT INTO suite_scenarios VALUES (?, ?, ?)",
                       (suite_id, sc_id, seq))


def diff_runs(run_id_a: str, run_id_b: str) -> dict[str, Any] | None:
    run_a = get_run(run_id_a)
    run_b = get_run(run_id_b)
    if not run_a or not run_b:
        return None

    assertions_a = {item["name"]: item for item in run_a["assertions"]}
    assertions_b = {item["name"]: item for item in run_b["assertions"]}
    invariant_diff = []
    all_names = sorted(set(assertions_a.keys()) | set(assertions_b.keys()))
    for name in all_names:
        status_a = assertions_a.get(name, {}).get("status", "missing")
        status_b = assertions_b.get(name, {}).get("status", "missing")
        invariant_diff.append({
            "name": name,
            "run_a_status": status_a,
            "run_b_status": status_b,
            "diverged": status_a != status_b,
        })

    events_a = run_a["events"]
    events_b = run_b["events"]
    first_divergence = None
    min_len = min(len(events_a), len(events_b))
    for i in range(min_len):
        ev_a = events_a[i]
        ev_b = events_b[i]
        if ev_a["type"] != ev_b["type"] or ev_a.get("payload") != ev_b.get("payload"):
            first_divergence = {
                "sequence": i + 1,
                "run_a_event": ev_a["type"],
                "run_b_event": ev_b["type"],
                "run_a_payload": ev_a.get("payload"),
                "run_b_payload": ev_b.get("payload"),
            }
            break

    if not first_divergence and len(events_a) != len(events_b):
        first_divergence = {
            "sequence": min_len + 1,
            "run_a_event": events_a[min_len]["type"] if len(events_a) > min_len else "END_OF_STREAM",
            "run_b_event": events_b[min_len]["type"] if len(events_b) > min_len else "END_OF_STREAM",
            "note": "event stream lengths differ",
        }

    return {
        "run_a": {"id": run_a["id"], "scenario_id": run_a["scenario_id"], "implementation": run_a["implementation"], "verdict": run_a["verdict"]},
        "run_b": {"id": run_b["id"], "scenario_id": run_b["scenario_id"], "implementation": run_b["implementation"], "verdict": run_b["verdict"]},
        "verdict_match": run_a["verdict"] == run_b["verdict"],
        "invariants": invariant_diff,
        "first_event_divergence": first_divergence,
        "event_counts": {"run_a": len(events_a), "run_b": len(events_b)},
    }


def list_annotations(run_id: str) -> list[dict[str, Any]]:
    with database() as db:
        rows = db.execute(
            "SELECT * FROM run_annotations WHERE run_id=? ORDER BY created_at ASC", (run_id,)
        ).fetchall()
        return [
            {
                "id": row["id"],
                "run_id": row["run_id"],
                "author": row["author"],
                "comment": row["comment"],
                "sequence_number": row["sequence_number"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]


def add_annotation(run_id: str, author: str, comment: str, sequence_number: int | None = None) -> dict[str, Any]:
    annotation_id = f"ann_{uuid4().hex[:10]}"
    created_at = now()
    clean_comment = security.redact_sensitive(comment)
    clean_author = security.redact_sensitive(author)
    with database() as db:
        db.execute(
            "INSERT INTO run_annotations VALUES (?, ?, ?, ?, ?, ?)",
            (annotation_id, run_id, clean_author, clean_comment, sequence_number, created_at),
        )
    return {
        "id": annotation_id,
        "run_id": run_id,
        "author": clean_author,
        "comment": clean_comment,
        "sequence_number": sequence_number,
        "created_at": created_at,
    }


def list_tasks(project_id: str = "faultline-demo", run_id: str | None = None) -> list[dict[str, Any]]:
    with database() as db:
        if run_id:
            rows = db.execute(
                "SELECT * FROM run_tasks WHERE run_id=? ORDER BY created_at DESC", (run_id,)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM run_tasks WHERE project_id=? ORDER BY created_at DESC", (project_id,)
            ).fetchall()
        return [
            {
                "id": row["id"],
                "project_id": row["project_id"],
                "run_id": row["run_id"],
                "title": row["title"],
                "description": row["description"],
                "status": row["status"],
                "priority": row["priority"],
                "assignee": row["assignee"],
                "created_at": row["created_at"],
                "resolved_at": row["resolved_at"],
            }
            for row in rows
        ]


def create_task(
    project_id: str = "faultline-demo",
    run_id: str | None = None,
    title: str = "",
    description: str = "",
    priority: str = "medium",
    assignee: str = "unassigned",
) -> dict[str, Any]:
    task_id = f"tsk_{uuid4().hex[:10]}"
    created_at = now()
    clean_title = security.redact_sensitive(title)
    clean_desc = security.redact_sensitive(description) if description else ""
    with database() as db:
        db.execute(
            "INSERT INTO run_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, project_id, run_id, clean_title, clean_desc, "open", priority, assignee, created_at, None),
        )
    return {
        "id": task_id,
        "project_id": project_id,
        "run_id": run_id,
        "title": clean_title,
        "description": clean_desc,
        "status": "open",
        "priority": priority,
        "assignee": assignee,
        "created_at": created_at,
        "resolved_at": None,
    }


def update_task(
    task_id: str,
    status: str | None = None,
    assignee: str | None = None,
    priority: str | None = None,
) -> dict[str, Any] | None:
    with database() as db:
        row = db.execute("SELECT * FROM run_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return None
        new_status = status if status is not None else row["status"]
        new_assignee = assignee if assignee is not None else row["assignee"]
        new_priority = priority if priority is not None else row["priority"]
        resolved_at = now() if new_status == "resolved" and row["status"] != "resolved" else (None if new_status != "resolved" else row["resolved_at"])
        db.execute(
            "UPDATE run_tasks SET status=?, assignee=?, priority=?, resolved_at=? WHERE id=?",
            (new_status, new_assignee, new_priority, resolved_at, task_id),
        )
        return {
            "id": task_id,
            "project_id": row["project_id"],
            "run_id": row["run_id"],
            "title": row["title"],
            "description": row["description"],
            "status": new_status,
            "priority": new_priority,
            "assignee": new_assignee,
            "created_at": row["created_at"],
            "resolved_at": resolved_at,
        }


def get_coverage_matrix() -> dict[str, Any]:
    workflow_steps = [
        {"id": "initiate_checkout", "name": "Initiate checkout", "description": "Client creates order and requests checkout"},
        {"id": "process_payment", "name": "Process payment", "description": "Checkout submits charge to payment provider"},
        {"id": "order_persistence", "name": "Persist order", "description": "Checkout records completed order in database"},
        {"id": "webhook_delivery", "name": "Deliver webhook", "description": "Payment provider notifies checkout of payment events"},
        {"id": "reconciliation_refund", "name": "Reconciliation / Refund", "description": "Checkout compensates or reconciles aborted charges"},
    ]

    fault_classes = [
        {"id": "payment_response_lost_after_commit", "name": "Lost Response", "scenario": "payment-response-lost"},
        {"id": "duplicate_payment_webhook", "name": "Duplicate Webhook", "scenario": "duplicate-payment-webhook"},
        {"id": "persistence_failure_after_payment", "name": "Persistence Failure", "scenario": "partial-persistence-failure"},
    ]

    applicable_map = {
        ("initiate_checkout", "payment_response_lost_after_commit"): True,
        ("initiate_checkout", "duplicate_payment_webhook"): False,
        ("initiate_checkout", "persistence_failure_after_payment"): True,

        ("process_payment", "payment_response_lost_after_commit"): True,
        ("process_payment", "duplicate_payment_webhook"): False,
        ("process_payment", "persistence_failure_after_payment"): True,

        ("order_persistence", "payment_response_lost_after_commit"): True,
        ("order_persistence", "duplicate_payment_webhook"): True,
        ("order_persistence", "persistence_failure_after_payment"): True,

        ("webhook_delivery", "payment_response_lost_after_commit"): False,
        ("webhook_delivery", "duplicate_payment_webhook"): True,
        ("webhook_delivery", "persistence_failure_after_payment"): False,

        ("reconciliation_refund", "payment_response_lost_after_commit"): False,
        ("reconciliation_refund", "duplicate_payment_webhook"): False,
        ("reconciliation_refund", "persistence_failure_after_payment"): True,
    }

    default_steps = {
        "payment_response_lost_after_commit": "process_payment",
        "duplicate_payment_webhook": "webhook_delivery",
        "persistence_failure_after_payment": "order_persistence",
    }

    with database() as db:
        scenario_rows = db.execute("SELECT id, definition_json FROM scenarios").fetchall()
        cell_scenarios: dict[tuple[str, str], list[str]] = {}
        for r in scenario_rows:
            sc_id = r["id"]
            try:
                sc_def = json.loads(r["definition_json"])
            except Exception:
                continue
            f_id = sc_def.get("fault")
            s_id = sc_def.get("target_step") or (default_steps.get(f_id) if sc_id in {fc["scenario"] for fc in fault_classes} else None)
            if f_id and s_id:
                cell_scenarios.setdefault((s_id, f_id), []).append(sc_id)

        matrix = []
        total_applicable = 0
        tested_count = 0
        passed_count = 0
        failed_count = 0

        for step in workflow_steps:
            step_row = {"step": step, "cells": []}
            for fault in fault_classes:
                is_applicable = applicable_map.get((step["id"], fault["id"]), False)
                matching_scenarios = list(cell_scenarios.get((step["id"], fault["id"]), []))
                if not matching_scenarios and default_steps.get(fault["id"]) == step["id"]:
                    matching_scenarios = [fault["scenario"]]

                runs = []
                if matching_scenarios:
                    placeholders = ",".join("?" for _ in matching_scenarios)
                    runs = db.execute(
                        f"""SELECT id, verdict, status, implementation, started_at
                            FROM runs
                            WHERE scenario_id IN ({placeholders}) AND status IN ('completed', 'failed')
                            ORDER BY started_at DESC""",
                        matching_scenarios,
                    ).fetchall()

                cell_scenario_id = matching_scenarios[0] if matching_scenarios else fault["scenario"]
                runs_count = len(runs)
                latest_verdict = runs[0]["verdict"] if runs else None

                if is_applicable:
                    total_applicable += 1
                    if runs_count > 0:
                        tested_count += 1
                        if latest_verdict == "passed":
                            passed_count += 1
                            cell_status = "passed"
                        else:
                            failed_count += 1
                            cell_status = "defect_exposed"
                    else:
                        cell_status = "untested"
                else:
                    cell_status = "not_applicable"

                step_row["cells"].append({
                    "fault_id": fault["id"],
                    "fault_name": fault["name"],
                    "scenario_id": cell_scenario_id,
                    "target_step": step["id"],
                    "applicable": is_applicable,
                    "status": cell_status,
                    "runs_count": runs_count if is_applicable else 0,
                    "latest_verdict": latest_verdict if is_applicable else None,
                })
            matrix.append(step_row)

    coverage_pct = round((tested_count / total_applicable * 100) if total_applicable else 0, 1)

    return {
        "workflow_steps": workflow_steps,
        "fault_classes": fault_classes,
        "matrix": matrix,
        "summary": {
            "total_applicable": total_applicable,
            "tested_cells": tested_count,
            "passed_cells": passed_count,
            "defect_exposed_cells": failed_count,
            "coverage_percentage": coverage_pct,
        },
    }


def generate_run_report(run_id: str) -> dict[str, Any] | None:
    run = get_run(run_id)
    if not run:
        return None

    events = run["events"]
    assertions = run["assertions"]
    summary = run["summary"]
    annotations = list_annotations(run_id)
    tasks = list_tasks(run_id=run_id)

    duration_s = 0.0
    if run.get("started_at") and run.get("finished_at"):
        try:
            start_dt = datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))
            finish_dt = datetime.fromisoformat(run["finished_at"].replace("Z", "+00:00"))
            duration_s = max(0.0, (finish_dt - start_dt).total_seconds())
        except Exception:
            pass

    fault_event = next((e for e in events if "FAULT_" in e["type"] or e["type"] in {"DROPPED", "UNKNOWN"}), None)
    time_to_fault_ms = None
    time_to_recovery_ms = None
    if fault_event and run.get("started_at") and fault_event.get("recorded_at"):
        try:
            start_dt = datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))
            fault_dt = datetime.fromisoformat(fault_event["recorded_at"].replace("Z", "+00:00"))
            time_to_fault_ms = max(0, int((fault_dt - start_dt).total_seconds() * 1000))
            if run.get("finished_at"):
                finish_dt = datetime.fromisoformat(run["finished_at"].replace("Z", "+00:00"))
                time_to_recovery_ms = max(0, int((finish_dt - fault_dt).total_seconds() * 1000))
        except Exception:
            pass

    failed_assertions = [a for a in assertions if a["status"] != "passed"]
    failed_names = {a["name"] for a in failed_assertions}
    if not failed_assertions and run["verdict"] == "passed":
        recommendation = "System demonstrated resilient integration behavior. All evaluated invariants held under fault injection."
    elif "no_duplicate_charge" in failed_names:
        recommendation = "Deduplication failure: upstream checkout retried after dropped response without supplying an idempotency key."
    elif "idempotent_webhook_processing" in failed_names:
        recommendation = "Webhook redelivery failure: fulfillment service committed duplicate order actions without deduplication ledger."
    elif "charge_compensated_on_failure" in failed_names or "no_orphaned_charge" in failed_names:
        recommendation = "Persistence failure: checkout committed payment but failed order write without issuing a compensating refund."
    else:
        recommendation = "One or more business invariants failed during fault evaluation. Review assertion evidence below."

    report = {
        "run_id": run_id,
        "scenario_id": run["scenario_id"],
        "scenario_name": run["scenario"]["name"] if isinstance(run.get("scenario"), dict) else run.get("scenario", run["scenario_id"]),
        "implementation": run["implementation"],
        "status": run["status"],
        "verdict": run["verdict"],
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "duration_seconds": round(duration_s, 3),
        "fault_analysis": {
            "fault_type": run["scenario"].get("fault") if isinstance(run.get("scenario"), dict) else "unknown",
            "activated": fault_event is not None,
            "activation_event": fault_event["type"] if fault_event else None,
            "time_to_fault_ms": time_to_fault_ms,
        },
        "recovery_analysis": {
            "recovered": run["verdict"] == "passed",
            "time_to_recovery_ms": time_to_recovery_ms,
            "side_effects": summary,
        },
        "invariants": assertions,
        "cleanup": run.get("cleanup", {"status": "untracked", "owned_resources": []}),
        "annotations": annotations,
        "tasks": tasks,
        "recommendation": recommendation,
    }
    return report


def generate_markdown_report(report: dict[str, Any]) -> str:
    verdict_badge = "PASSED" if report["verdict"] == "passed" else ("FAILED" if report["verdict"] == "failed" else report["verdict"].upper())
    fault_status = "Observed" if report["fault_analysis"]["activated"] else "Not Activated"

    lines = [
        f"# Faultline Retrospective Report — {report['scenario_name']}",
        "",
        f"> **Run ID:** `{report['run_id']}`  ",
        f"> **Verdict:** **{verdict_badge}**  ",
        f"> **Implementation:** `{report['implementation']}`  ",
        f"> **Duration:** `{report['duration_seconds']}s`",
        "",
        "## Executive Summary",
        "",
        f"- **Scenario:** `{report['scenario_id']}`",
        f"- **Fault Class:** `{report['fault_analysis']['fault_type']}` ({fault_status})",
    ]

    if report["fault_analysis"]["time_to_fault_ms"] is not None:
        lines.append(f"- **Time to Fault Activation:** `{report['fault_analysis']['time_to_fault_ms']} ms`")
    if report["recovery_analysis"]["time_to_recovery_ms"] is not None:
        lines.append(f"- **Time to Mitigation / Terminal State:** `{report['recovery_analysis']['time_to_recovery_ms']} ms`")

    lines.extend([
        "",
        "### Recommendation",
        f"> {report['recommendation']}",
        "",
        "## Invariant Evaluation",
        "",
        "| Invariant | Status | Expected | Observed |",
        "| :--- | :---: | :--- | :--- |",
    ])

    for inv in report["invariants"]:
        icon = "Passed" if inv["status"] == "passed" else "Failed"
        lines.append(f"| `{inv['name']}` | {icon} | {inv['expected']} | {inv['observed']} |")

    cleanup = report.get("cleanup", {})
    cleanup_status = cleanup.get("status", "untracked").upper()
    owned_count = len(cleanup.get("owned_resources", []))
    lines.extend([
        "",
        "## Cleanup & Fixture Teardown (Spec §6.14)",
        f"- **Cleanup Verdict:** `{cleanup_status}`",
        f"- **Owned Resources Tracked:** `{owned_count}`",
        f"- **Teardown Error:** `{cleanup.get('error') or 'None'}`",
    ])

    if report.get("annotations"):
        lines.extend([
            "",
            "## Team Observations & Annotations",
            "",
        ])
        for a in report["annotations"]:
            seq_info = f" (at step #{a['sequence_number']})" if a.get("sequence_number") else ""
            lines.append(f"- **{a['author']}**{seq_info}: {a['comment']}")

    if report.get("tasks"):
        lines.extend([
            "",
            "## Follow-up Action Items",
            "",
        ])
        for t in report["tasks"]:
            box = "[x]" if t["status"] == "resolved" else "[ ]"
            lines.append(f"- {box} **[{t['priority'].upper()}]** {t['title']} (`{t['assignee']}`)")

    lines.extend([
        "",
        "---",
        f"*Generated by Faultline Integration Failure Lab · {now()}*",
    ])
    return "\n".join(lines)


def record_audit_event(
    project_id: str,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event_id = f"aud_{uuid4().hex[:12]}"
    created_at = now()
    clean_details = security.redact_sensitive(details or {})
    with database() as db:
        db.execute(
            "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, project_id, actor, action, target_type, target_id, json.dumps(clean_details), created_at),
        )
    return {
        "id": event_id,
        "project_id": project_id,
        "actor": actor,
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "details": clean_details,
        "created_at": created_at,
    }


def list_audit_events(project_id: str = "faultline-demo", limit: int = 50) -> list[dict[str, Any]]:
    with database() as db:
        rows = db.execute(
            "SELECT * FROM audit_events WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "project_id": r["project_id"],
                "actor": r["actor"],
                "action": r["action"],
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "details": json.loads(r["details_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]


def record_snapshot(run_id: str, sequence: int, scenario_version: int, snapshot: dict[str, Any]) -> None:
    created_at = now()
    clean_snapshot = security.redact_sensitive(snapshot)
    with database() as db:
        db.execute(
            "INSERT OR REPLACE INTO state_snapshots(run_id, sequence, scenario_version, snapshot_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, sequence, scenario_version, json.dumps(clean_snapshot), created_at),
        )


def get_snapshots(run_id: str) -> list[dict[str, Any]]:
    with database() as db:
        rows = db.execute(
            "SELECT sequence, scenario_version, snapshot_json, created_at FROM state_snapshots WHERE run_id=? ORDER BY sequence ASC",
            (run_id,),
        ).fetchall()
        return [
            {
                "sequence": r["sequence"],
                "scenario_version": r["scenario_version"],
                "snapshot": json.loads(r["snapshot_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]


def reconstruct_state_at(run_id: str, target_sequence: int) -> dict[str, Any] | None:
    if target_sequence < 0:
        raise ValueError("target_sequence must be non-negative")
    run = get_run(run_id)
    if not run:
        return None
    events = run.get("events", [])
    max_sequence = max((ev["sequence"] for ev in events), default=0)
    effective_sequence = min(target_sequence, max_sequence)
    state: dict[str, Any] = {
        "run_id": run_id,
        "sequence": effective_sequence,
        "requested_sequence": target_sequence,
        "max_sequence": max_sequence,
        "status": "initial",
        "orders": {},
        "charges": [],
        "fulfillments": [],
        "refunds": [],
        "invariants_evaluated": False,
        "fault_activated": False,
    }
    for ev in events:
        if ev["sequence"] > effective_sequence:
            break
        t = ev["type"]
        p = ev.get("payload", {})
        if t == "RUN_QUEUED":
            state["status"] = "queued"
        elif t == "CHECKOUT_STARTED":
            state["status"] = "running"
            state["orders"][p.get("order_id", "default")] = {"status": "started", "implementation": p.get("implementation")}
        elif t == "PROVIDER_CHARGE_COMMITTED":
            state["charges"].append(p)
        elif t == "PROVIDER_REFUND_COMMITTED":
            state["refunds"].append(p)
        elif t == "FULFILLMENT_COMMITTED":
            state["fulfillments"].append(p)
        elif "FAULT_" in t:
            state["fault_activated"] = True
            state["fault_event"] = t
        elif t == "CHECKOUT_REACHED_TERMINAL_STATE":
            state["orders"][p.get("order_id", "default")] = {"status": p.get("status"), "attempts": p.get("attempts")}
        elif t == "ASSERTIONS_EVALUATED":
            state["invariants_evaluated"] = True
        elif t in {"RUN_COMPLETED", "RUN_CANCELLED", "RUN_TIMED_OUT", "RUN_EXECUTION_FAILED"}:
            state["status"] = t.removeprefix("RUN_").lower()
    return state


def generate_scenarios_from_gaps(project_id: str = "faultline-demo") -> list[dict[str, Any]]:
    cov = get_coverage_matrix()
    matrix = cov["matrix"]
    generated = []
    existing_scenario_ids = {s["id"] for s in list_scenarios()}

    for row in matrix:
        step_id = row["step"]["id"]
        step_name = row["step"]["name"]
        for cell in row["cells"]:
            fault_id = cell["fault_id"]
            if cell["applicable"] and cell["status"] == "untested":
                if fault_id not in SUPPORTED_FAULTS:
                    continue
                sc_id = f"gap-{step_id}-{fault_id}".replace("_", "-")[:40]
                if sc_id in existing_scenario_ids:
                    continue

                primary_assertion = SUPPORTED_FAULTS[fault_id]["primary_assertion"]
                definition: dict[str, Any] = {
                    "scenario_version": 1,
                    "name": sc_id,
                    "target_step": step_id,
                    "description": f"Automated scenario generated to test {step_name} under {fault_id}.",
                    "order_id": f"order-gap-{uuid4().hex[:6]}",
                    "amount_cents": 3000,
                    "fault": fault_id,
                    "assertion": primary_assertion,
                    "timeout_seconds": 15,
                }
                if fault_id in {"payment_response_lost_after_commit", "persistence_failure_after_payment"}:
                    definition["retries"] = 1 if fault_id == "payment_response_lost_after_commit" else 0
                if fault_id == "duplicate_payment_webhook":
                    definition["webhook_deliveries"] = 2
                    definition["delay_ms"] = 100
                if fault_id == "persistence_failure_after_payment":
                    definition["compensate"] = True

                validate_scenario(definition)
                create_scenario(sc_id, project_id, definition)
                existing_scenario_ids.add(sc_id)
                generated.append({"scenario_id": sc_id, "step": step_id, "fault": fault_id, "definition": definition})
    return generated


def get_project_services(project_id: str = "faultline-demo") -> list[dict[str, Any]]:
    from . import adapters
    services = [
        {
            "id": "checkout-service",
            "name": "Checkout Application Service",
            "port": adapters.CHECKOUT_PORT,
            "base_url": f"http://127.0.0.1:{adapters.CHECKOUT_PORT}",
            "role": "application_under_test",
            "dependencies": ["payment-provider"],
        },
        {
            "id": "payment-provider",
            "name": "Test Payment Provider & Fault Adapter",
            "port": adapters.PAYMENT_PORT,
            "base_url": f"http://127.0.0.1:{adapters.PAYMENT_PORT}",
            "role": "simulated_dependency",
            "dependencies": [],
        },
    ]

    for svc in services:
        t0 = time.monotonic()
        try:
            with socket.create_connection(("127.0.0.1", svc["port"]), timeout=0.5):
                latency_ms = max(1, int((time.monotonic() - t0) * 1000))
                svc["status"] = "healthy"
                svc["latency_ms"] = latency_ms
        except Exception as exc:
            svc["status"] = "unreachable"
            svc["error"] = str(exc)
            svc["latency_ms"] = None
    return services


def record_span(
    run_id: str,
    trace_id: str,
    span_id: str,
    name: str,
    kind: str = "INTERNAL",
    parent_span_id: str | None = None,
    start_time_unix_nano: int = 0,
    end_time_unix_nano: int = 0,
    status_code: str = "OK",
    status_message: str | None = None,
    attributes: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    clean_attrs = security.redact_sensitive(attributes or {})
    span_pk = f"{run_id}:{span_id}"
    with database() as db:
        db.execute(
            """INSERT OR REPLACE INTO run_spans (
                id, run_id, trace_id, span_id, parent_span_id, name, kind,
                start_time_unix_nano, end_time_unix_nano, status_code, status_message,
                attributes_json, events_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                span_pk,
                run_id,
                trace_id,
                span_id,
                parent_span_id,
                name,
                kind,
                int(start_time_unix_nano),
                int(end_time_unix_nano),
                status_code,
                status_message,
                json.dumps(clean_attrs),
                json.dumps(events or []),
            ),
        )
    return {
        "id": span_id,
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "name": name,
        "kind": kind,
        "duration_ms": max(0, int((end_time_unix_nano - start_time_unix_nano) / 1_000_000)),
        "status": status_code,
        "attributes": clean_attrs,
    }


def get_run_telemetry(run_id: str) -> dict[str, Any] | None:
    with database() as db:
        run_row = db.execute("SELECT id, scenario_id, scenario_version, implementation, verdict, trace_id, started_at, finished_at FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run_row:
            return None
        trace_id = run_row["trace_id"] or uuid4().hex
        span_rows = db.execute("SELECT * FROM run_spans WHERE run_id=? ORDER BY start_time_unix_nano ASC", (run_id,)).fetchall()
        spans = []
        for r in span_rows:
            spans.append({
                "spanId": r["span_id"],
                "parentSpanId": r["parent_span_id"],
                "name": r["name"],
                "kind": r["kind"],
                "startTimeUnixNano": str(r["start_time_unix_nano"]),
                "endTimeUnixNano": str(r["end_time_unix_nano"]),
                "durationMs": max(0, int((r["end_time_unix_nano"] - r["start_time_unix_nano"]) / 1_000_000)),
                "statusCode": r["status_code"],
                "statusMessage": r["status_message"],
                "attributes": json.loads(r["attributes_json"]),
                "events": json.loads(r["events_json"]),
            })
        root_span = next((s for s in spans if not s["parentSpanId"]), None)

        otlp_spans = []
        for s in spans:
            otlp_attrs = []
            for k, v in s["attributes"].items():
                if isinstance(v, bool):
                    otlp_attrs.append({"key": k, "value": {"boolValue": v}})
                elif isinstance(v, int):
                    otlp_attrs.append({"key": k, "value": {"intValue": v}})
                elif isinstance(v, float):
                    otlp_attrs.append({"key": k, "value": {"doubleValue": v}})
                else:
                    otlp_attrs.append({"key": k, "value": {"stringValue": str(v)}})
            span_dict = {
                "traceId": trace_id,
                "spanId": s["spanId"],
                "name": s["name"],
                "kind": 1 if s["kind"] == "INTERNAL" else (2 if s["kind"] == "SERVER" else 3),
                "startTimeUnixNano": s["startTimeUnixNano"],
                "endTimeUnixNano": s["endTimeUnixNano"],
                "attributes": otlp_attrs,
                "status": {"code": 1 if s["statusCode"] == "OK" else 2, "message": s["statusMessage"] or ""},
            }
            if s["parentSpanId"]:
                span_dict["parentSpanId"] = s["parentSpanId"]
            otlp_spans.append(span_dict)

        return {
            "traceId": trace_id,
            "rootSpanId": root_span["spanId"] if root_span else (spans[0]["spanId"] if spans else None),
            "spanCount": len(spans),
            "runId": run_id,
            "scenarioId": run_row["scenario_id"],
            "verdict": run_row["verdict"],
            "spans": spans,
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "faultline-lab"}},
                            {"key": "service.version", "value": {"stringValue": "1.0.0"}},
                            {"key": "project.id", "value": {"stringValue": "faultline-demo"}},
                            {"key": "run.id", "value": {"stringValue": run_id}},
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "faultline.tracer", "version": "1.0.0"},
                            "spans": otlp_spans,
                        }
                    ]
                }
            ]
        }


def set_baseline_run(run_id: str) -> dict[str, Any]:
    with database() as db:
        row = db.execute("SELECT id, scenario_id FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ValueError(f"run {run_id} not found")
        scenario_id = row["scenario_id"]
        db.execute("UPDATE runs SET is_baseline=0 WHERE scenario_id=?", (scenario_id,))
        db.execute("UPDATE runs SET is_baseline=1 WHERE id=?", (run_id,))
    record_audit_event("faultline-demo", "developer", "SCENARIO_BASELINE_SET", "run", run_id, {"scenario_id": scenario_id})
    return {"run_id": run_id, "scenario_id": scenario_id, "is_baseline": True}


def get_scenario_baseline_diff(scenario_id: str, target_run_id: str | None = None) -> dict[str, Any]:
    with database() as db:
        baseline_row = db.execute("SELECT id FROM runs WHERE scenario_id=? AND is_baseline=1", (scenario_id,)).fetchone()
        if not baseline_row:
            baseline_row = db.execute("SELECT id FROM runs WHERE scenario_id=? AND verdict='passed' ORDER BY started_at ASC LIMIT 1", (scenario_id,)).fetchone()
        if not baseline_row:
            baseline_row = db.execute("SELECT id FROM runs WHERE scenario_id=? ORDER BY started_at ASC LIMIT 1", (scenario_id,)).fetchone()

        if not baseline_row:
            return {
                "has_baseline": False,
                "scenario_id": scenario_id,
                "message": "No baseline run exists for this scenario yet",
            }

        baseline_id = baseline_row["id"]
        if target_run_id:
            target_id = target_run_id
        else:
            target_row = db.execute("SELECT id FROM runs WHERE scenario_id=? AND id!=? ORDER BY started_at DESC LIMIT 1", (scenario_id, baseline_id)).fetchone()
            target_id = target_row["id"] if target_row else baseline_id

    diff_result = diff_runs(baseline_id, target_id)
    baseline_run = get_run(baseline_id)
    target_run = get_run(target_id)

    b_verdict = baseline_run["verdict"] if baseline_run else "unknown"
    t_verdict = target_run["verdict"] if target_run else "unknown"
    if b_verdict == "passed" and t_verdict == "passed":
        regression_status = "stable_pass"
    elif b_verdict == "failed" and t_verdict == "passed":
        regression_status = "regression_resolved"
    elif b_verdict == "passed" and t_verdict == "failed":
        regression_status = "regression_introduced"
    else:
        regression_status = "stable_failure"

    return {
        "has_baseline": True,
        "scenario_id": scenario_id,
        "baseline_run_id": baseline_id,
        "target_run_id": target_id,
        "regression_status": regression_status,
        "baseline_verdict": b_verdict,
        "target_verdict": t_verdict,
        "diff": diff_result,
    }


def clone_scenario_version(
    scenario_id: str,
    version: int,
    new_scenario_id: str | None = None,
    name_suffix: str = " (Clone)",
) -> dict[str, Any]:
    with database() as db:
        sc = db.execute("SELECT * FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
        if not sc:
            raise ValueError(f"scenario {scenario_id} not found")
        vrow = db.execute("SELECT * FROM scenario_versions WHERE scenario_id=? AND version=?", (scenario_id, version)).fetchone()
        if not vrow:
            raise ValueError(f"scenario {scenario_id} version {version} not found")

        definition = json.loads(vrow["definition_json"])
        project_id = sc["project_id"]

        if new_scenario_id:
            target_id = new_scenario_id
            target_name = definition["name"] + name_suffix
            definition["name"] = target_name
            db.execute(
                "INSERT INTO scenarios (id, project_id, name, definition_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (target_id, project_id, target_name, json.dumps(definition), now()),
            )
            _insert_scenario_version(db, target_id, 1, definition)
            result = {"scenario_id": target_id, "version": 1, "definition": definition}
            audit_args = (project_id, "developer", "SCENARIO_CLONED", "scenario", target_id, {
                "source_scenario_id": scenario_id,
                "source_version": version,
                "target_scenario_id": target_id,
            })
        else:
            next_version = db.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM scenario_versions WHERE scenario_id=?", (scenario_id,)
            ).fetchone()[0]
            _insert_scenario_version(db, scenario_id, next_version, definition)
            db.execute("UPDATE scenarios SET definition_json=? WHERE id=?", (json.dumps(definition), scenario_id))
            result = {"scenario_id": scenario_id, "version": next_version, "definition": definition}
            audit_args = (project_id, "developer", "SCENARIO_VERSION_CLONED", "scenario", scenario_id, {
                "source_version": version,
                "new_version": next_version,
            })

    record_audit_event(*audit_args)
    return result


def add_scenario_to_suite(suite_id: str, scenario_id: str) -> dict[str, Any]:
    with database() as db:
        suite = db.execute("SELECT id FROM test_suites WHERE id=?", (suite_id,)).fetchone()
        if not suite:
            raise ValueError(f"suite {suite_id} not found")
        scenario = db.execute("SELECT id FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
        if not scenario:
            raise ValueError(f"scenario {scenario_id} not found")
        next_seq = db.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM suite_scenarios WHERE suite_id=?", (suite_id,)
        ).fetchone()[0]
        db.execute(
            "INSERT OR REPLACE INTO suite_scenarios (suite_id, scenario_id, sequence) VALUES (?, ?, ?)",
            (suite_id, scenario_id, next_seq),
        )
    record_audit_event("faultline-demo", "developer", "SUITE_SCENARIO_ADDED", "suite", suite_id, {"scenario_id": scenario_id})
    return {"suite_id": suite_id, "scenario_id": scenario_id, "sequence": next_seq}


def record_suite_run_start(suite_id: str, implementation: str, total_scenarios: int) -> str:
    suite_run_id = str(uuid4())
    with database() as db:
        db.execute(
            """INSERT INTO suite_runs (
                id, suite_id, implementation, status, verdict, total_scenarios,
                passed_scenarios, failed_scenarios, duration_ms, run_ids_json, started_at
            ) VALUES (?, ?, ?, 'running', 'pending', ?, 0, 0, 0, '[]', ?)""",
            (suite_run_id, suite_id, implementation, total_scenarios, now()),
        )
    return suite_run_id


def finish_suite_run(
    suite_run_id: str,
    verdict: str,
    passed: int,
    failed: int,
    duration_ms: int,
    run_ids: list[str],
) -> None:
    with database() as db:
        db.execute(
            """UPDATE suite_runs SET
                status='completed', verdict=?, passed_scenarios=?, failed_scenarios=?,
                duration_ms=?, run_ids_json=?, finished_at=?
            WHERE id=?""",
            (verdict, passed, failed, duration_ms, json.dumps(run_ids), now(), suite_run_id),
        )


def list_suite_results(suite_id: str, limit: int = 20) -> list[dict[str, Any]]:
    with database() as db:
        rows = db.execute(
            "SELECT * FROM suite_runs WHERE suite_id=? ORDER BY started_at DESC LIMIT ?",
            (suite_id, limit),
        ).fetchall()
        result = []
        for r in rows:
            run_ids = json.loads(r["run_ids_json"]) if r["run_ids_json"] else []
            total = r["total_scenarios"]
            passed = r["passed_scenarios"]
            pass_rate = round((passed / total) * 100, 1) if total > 0 else 0.0
            result.append({
                "id": r["id"],
                "suite_id": r["suite_id"],
                "implementation": r["implementation"],
                "status": r["status"],
                "verdict": r["verdict"],
                "total_scenarios": total,
                "passed_scenarios": passed,
                "failed_scenarios": r["failed_scenarios"],
                "pass_rate": pass_rate,
                "duration_ms": r["duration_ms"],
                "run_ids": run_ids,
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
            })
        return result


def get_system_metrics() -> dict[str, Any]:
    with database() as db:
        status_rows = db.execute(
            "SELECT status, count(*) as count FROM runs GROUP BY status"
        ).fetchall()
        run_counts = {row["status"]: row["count"] for row in status_rows}
        total_runs = sum(run_counts.values())

        runs_metric = {
            "queued": run_counts.get("queued", 0),
            "running": run_counts.get("running", 0),
            "completed": run_counts.get("completed", 0),
            "failed": run_counts.get("failed", 0),
            "timed_out": run_counts.get("timed_out", 0),
            "cancelled": run_counts.get("cancelled", 0),
            "total": total_runs,
        }

        assertion_rows = db.execute(
            "SELECT status, count(*) as count FROM assertions GROUP BY status"
        ).fetchall()
        assertion_counts = {row["status"]: row["count"] for row in assertion_rows}
        total_assertions = sum(assertion_counts.values())
        passed_assertions = assertion_counts.get("passed", 0) + assertion_counts.get("pass", 0)
        failed_assertions = assertion_counts.get("failed", 0) + assertion_counts.get("fail", 0)
        pass_rate = round((passed_assertions / total_assertions) * 100, 2) if total_assertions > 0 else 100.0
        assertions_metric = {
            "total": total_assertions,
            "passed": passed_assertions,
            "failed": failed_assertions,
            "pass_rate_percent": pass_rate,
        }

        total_suites = db.execute("SELECT count(*) as count FROM test_suites").fetchone()["count"]
        suite_rows = db.execute(
            "SELECT status, count(*) as count FROM suite_runs GROUP BY status"
        ).fetchall()
        suite_counts = {row["status"]: row["count"] for row in suite_rows}
        total_suite_runs = sum(suite_counts.values())
        suites_metric = {
            "total_suites": total_suites,
            "total_runs": total_suite_runs,
            "status_counts": suite_counts,
        }

        total_scenarios = db.execute("SELECT count(*) as count FROM scenarios").fetchone()["count"]
        total_versions = db.execute("SELECT count(*) as count FROM scenario_versions").fetchone()["count"]
        total_spans = db.execute("SELECT count(*) as count FROM run_spans").fetchone()["count"]
        total_snapshots = db.execute("SELECT count(*) as count FROM state_snapshots").fetchone()["count"]

        queue_depth = run_counts.get("queued", 0)
        active_workers = 1 if run_counts.get("running", 0) > 0 else 0
        uptime = round(time.time() - SYSTEM_START_TIME, 2)

        redis_url = os.environ.get("REDIS_URL")
        toxiproxy_url = os.environ.get("TOXIPROXY_URL")

        return {
            "uptime_seconds": uptime,
            "worker_health": {
                "status": "healthy",
                "queue_depth": queue_depth,
                "active_workers": active_workers,
                "concurrency_limit": 5,
            },
            "infrastructure": {
                "execution_mode": "in_process_threads",
                "redis_configured": bool(redis_url),
                "toxiproxy_configured": bool(toxiproxy_url),
            },
            "runs": runs_metric,
            "assertions": assertions_metric,
            "suites": suites_metric,
            "inventory": {
                "scenarios": total_scenarios,
                "scenario_versions": total_versions,
                "telemetry_spans": total_spans,
                "state_snapshots": total_snapshots,
            },
        }


def get_prometheus_metrics() -> str:
    metrics = get_system_metrics()
    runs = metrics["runs"]
    assertions = metrics["assertions"]
    lines = [
        "# HELP faultline_runs_total Total runs partitioned by lifecycle status",
        "# TYPE faultline_runs_total gauge",
    ]
    for status, count in runs.items():
        if status != "total":
            lines.append(f'faultline_runs_total{{status="{status}"}} {count}')
    lines.extend([
        "# HELP faultline_assertions_total Invariant assertions evaluated",
        "# TYPE faultline_assertions_total counter",
        f'faultline_assertions_total{{status="passed"}} {assertions["passed"]}',
        f'faultline_assertions_total{{status="failed"}} {assertions["failed"]}',
        "# HELP faultline_queue_depth Current runner queue depth",
        "# TYPE faultline_queue_depth gauge",
        f'faultline_queue_depth {metrics["worker_health"]["queue_depth"]}',
        "# HELP faultline_uptime_seconds Faultline control-plane uptime in seconds",
        "# TYPE faultline_uptime_seconds counter",
        f'faultline_uptime_seconds {metrics["uptime_seconds"]}',
        "",
    ])
    return "\n".join(lines)


def prune_project_runs(
    project_id: str,
    max_age_days: int = 30,
    keep_baselines: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat().replace("+00:00", "Z")

    with database() as db:
        project = db.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise KeyError(f"Project '{project_id}' not found")

        query = """
            SELECT r.id, r.is_baseline, r.status, r.started_at
            FROM runs r
            JOIN scenarios s ON r.scenario_id = s.id
            WHERE s.project_id = ?
              AND r.started_at <= ?
              AND r.status IN ('completed', 'failed', 'timed_out', 'cancelled')
        """
        rows = db.execute(query, (project_id, cutoff)).fetchall()

        pruned_ids = []
        skipped_baselines = 0
        for r in rows:
            if keep_baselines and r["is_baseline"]:
                skipped_baselines += 1
                continue
            pruned_ids.append(r["id"])

        if not dry_run and pruned_ids:
            placeholders = ",".join("?" for _ in pruned_ids)
            db.execute(f"DELETE FROM runs WHERE id IN ({placeholders})", pruned_ids)

    if not dry_run and pruned_ids:
        record_audit_event(
            project_id=project_id,
            action="PROJECT_PRUNED",
            actor="system",
            target_type="project",
            target_id=project_id,
            details={
                "max_age_days": max_age_days,
                "cutoff": cutoff,
                "keep_baselines": keep_baselines,
                "pruned_run_count": len(pruned_ids),
                "pruned_run_ids": pruned_ids,
                "skipped_baselines": skipped_baselines,
            },
        )

    return {
        "project_id": project_id,
        "max_age_days": max_age_days,
        "cutoff": cutoff,
        "keep_baselines": keep_baselines,
        "dry_run": dry_run,
        "pruned_run_count": len(pruned_ids),
        "pruned_run_ids": pruned_ids,
        "skipped_baselines": skipped_baselines,
    }



