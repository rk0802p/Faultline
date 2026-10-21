"""Small SQLite persistence layer for projects, scenarios, runs, and evidence."""

import json
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("FAULTLINE_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "faultline.sqlite3"


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
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
    scenario_files = sorted((ROOT / "scenarios").glob("*.json"))
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
            """
        )
        run_columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
        if "scenario_version" not in run_columns:
            db.execute("ALTER TABLE runs ADD COLUMN scenario_version INTEGER NOT NULL DEFAULT 1")
        db.execute(
            "INSERT OR IGNORE INTO projects VALUES (?, ?, ?, ?)",
            ("faultline-demo", "Checkout reliability lab", "Local, isolated checkout integration environment.", now()),
        )
        for scenario_file in scenario_files:
            definition = json.loads(scenario_file.read_text(encoding="utf-8"))
            scenario_id = definition["name"]
            db.execute(
                "INSERT OR IGNORE INTO scenarios VALUES (?, ?, ?, ?, ?)",
                (scenario_id, "faultline-demo", scenario_id, json.dumps(definition), now()),
            )
            existing = db.execute("SELECT 1 FROM scenario_versions WHERE scenario_id=?", (scenario_id,)).fetchone()
            if not existing:
                _insert_scenario_version(db, scenario_id, 1, definition)

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


def _insert_scenario_version(db: sqlite3.Connection, scenario_id: str, version: int, definition: dict[str, Any]) -> None:
    raw = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    db.execute("INSERT INTO scenario_versions VALUES (?, ?, ?, ?, ?)",
               (scenario_id, version, raw, hashlib.sha256(raw.encode()).hexdigest(), now()))


def list_projects() -> list[dict[str, Any]]:
    with database() as db:
        return [dict(row) for row in db.execute("SELECT * FROM projects ORDER BY name")]


def list_scenarios() -> list[dict[str, Any]]:
    with database() as db:
        rows = db.execute("SELECT * FROM scenarios ORDER BY name").fetchall()
        return [
            {"id": row["id"], "project_id": row["project_id"], "name": row["name"],
             "definition": json.loads(row["definition_json"]), "created_at": row["created_at"],
             "version": _latest_version(db, row["id"])}
            for row in rows
        ]


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


def create_run(run_id: str, scenario_id: str, scenario_version: int, implementation: str) -> None:
    with database() as db:
        db.execute(
            "INSERT INTO runs(id, scenario_id, scenario_version, implementation, status, started_at) VALUES (?, ?, ?, ?, 'queued', ?)",
            (run_id, scenario_id, scenario_version, implementation, now()),
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
        db.execute("UPDATE runs SET status=?, verdict=?, summary_json=COALESCE(?, summary_json), error=?, finished_at=? WHERE id=?",
                   (status, verdict, json.dumps(summary) if summary is not None else None, error,
                    now() if finished else None, run_id))
        return True


def run_status(run_id: str) -> str | None:
    with database() as db:
        row = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        return row["status"] if row else None


def request_cancel(run_id: str) -> bool:
    return transition_run(run_id, "cancel_requested")


def record_event(run_id: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    with database() as db:
        sequence = db.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        recorded_at = now()
        db.execute(
            "INSERT INTO run_events(run_id, sequence, recorded_at, type, payload_json) VALUES (?, ?, ?, ?, ?)",
            (run_id, sequence, recorded_at, event_type, json.dumps(payload or {})),
        )
    return {"sequence": sequence, "recorded_at": recorded_at, "type": event_type, "payload": payload or {}}


def replace_assertions(run_id: str, assertions: list[dict[str, Any]]) -> None:
    with database() as db:
        db.execute("DELETE FROM assertions WHERE run_id=?", (run_id,))
        db.executemany(
            "INSERT INTO assertions(run_id, name, status, expected, observed, evidence_json) VALUES (?, ?, ?, ?, ?, ?)",
            [(run_id, item["name"], item["status"], item["expected"], item["observed"],
              json.dumps(item["evidence"])) for item in assertions],
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
        result["scenario"] = json.loads(scenario["definition_json"])
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


def get_reproduction_bundle(run_id: str) -> dict[str, Any] | None:
    run = get_run(run_id)
    if run is None:
        return None
    return {"format": "faultline-repro-v1", "run": run,
            "replay": {"command": f"python -m faultline.cli run --scenario {run['scenario_id']} --implementation {run['implementation']}",
                       "note": "Replays the recorded scenario against the local deterministic test services."}}


def list_runs(limit: int = 30) -> list[dict[str, Any]]:
    with database() as db:
        rows = db.execute(
            "SELECT r.*, s.name AS scenario_name FROM runs r JOIN scenarios s ON s.id=r.scenario_id "
            "ORDER BY r.started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {"id": row["id"], "scenario_id": row["scenario_id"], "scenario_version": row["scenario_version"], "scenario": row["scenario_name"],
             "implementation": row["implementation"], "status": row["status"], "verdict": row["verdict"],
             "started_at": row["started_at"], "finished_at": row["finished_at"],
             "summary": json.loads(row["summary_json"]), "error": row["error"]}
            for row in rows
        ]


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
    with database() as db:
        db.execute(
            "INSERT INTO run_annotations VALUES (?, ?, ?, ?, ?, ?)",
            (annotation_id, run_id, author, comment, sequence_number, created_at),
        )
    return {
        "id": annotation_id,
        "run_id": run_id,
        "author": author,
        "comment": comment,
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
    with database() as db:
        db.execute(
            "INSERT INTO run_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, project_id, run_id, title, description, "open", priority, assignee, created_at, None),
        )
    return {
        "id": task_id,
        "project_id": project_id,
        "run_id": run_id,
        "title": title,
        "description": description,
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

    with database() as db:
        scenario_runs = {}
        for fc in fault_classes:
            sc_id = fc["scenario"]
            rows = db.execute(
                "SELECT verdict, status, implementation FROM runs WHERE scenario_id=? ORDER BY started_at DESC LIMIT 10",
                (sc_id,)
            ).fetchall()
            scenario_runs[sc_id] = {
                "total_runs": len(rows),
                "has_passed": any(r["verdict"] == "passed" for r in rows),
                "has_failed": any(r["verdict"] == "failed" for r in rows),
                "latest_verdict": rows[0]["verdict"] if rows else None,
            }

    matrix = []
    total_applicable = 0
    tested_count = 0
    passed_count = 0
    failed_count = 0

    for step in workflow_steps:
        step_row = {"step": step, "cells": []}
        for fault in fault_classes:
            is_applicable = applicable_map.get((step["id"], fault["id"]), False)
            if is_applicable:
                total_applicable += 1
                stats = scenario_runs.get(fault["scenario"], {})
                if stats.get("total_runs", 0) > 0:
                    tested_count += 1
                    if stats.get("has_passed"):
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
                "scenario_id": fault["scenario"],
                "applicable": is_applicable,
                "status": cell_status,
                "runs_count": scenario_runs.get(fault["scenario"], {}).get("total_runs", 0) if is_applicable else 0,
                "latest_verdict": scenario_runs.get(fault["scenario"], {}).get("latest_verdict") if is_applicable else None,
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


