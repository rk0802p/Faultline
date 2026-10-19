"""Small SQLite persistence layer for projects, scenarios, runs, and evidence."""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
    scenario_path = ROOT / "scenarios" / "payment-response-lost.json"
    definition = json.loads(scenario_path.read_text(encoding="utf-8"))
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
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL REFERENCES scenarios(id),
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
            """
        )
        db.execute(
            "INSERT OR IGNORE INTO projects VALUES (?, ?, ?, ?)",
            ("faultline-demo", "Checkout reliability lab", "Local, isolated checkout integration environment.", now()),
        )
        db.execute(
            "INSERT OR IGNORE INTO scenarios VALUES (?, ?, ?, ?, ?)",
            (definition["name"], "faultline-demo", definition["name"], json.dumps(definition), now()),
        )


def list_projects() -> list[dict[str, Any]]:
    with database() as db:
        return [dict(row) for row in db.execute("SELECT * FROM projects ORDER BY name")]


def list_scenarios() -> list[dict[str, Any]]:
    with database() as db:
        rows = db.execute("SELECT * FROM scenarios ORDER BY name").fetchall()
        return [
            {"id": row["id"], "project_id": row["project_id"], "name": row["name"],
             "definition": json.loads(row["definition_json"]), "created_at": row["created_at"]}
            for row in rows
        ]


def create_run(run_id: str, scenario_id: str, implementation: str) -> None:
    with database() as db:
        db.execute(
            "INSERT INTO runs(id, scenario_id, implementation, status, started_at) VALUES (?, ?, ?, 'queued', ?)",
            (run_id, scenario_id, implementation, now()),
        )


def update_run(run_id: str, *, status: str, verdict: str, summary: dict[str, Any] | None = None,
               error: str | None = None, finished: bool = False) -> None:
    with database() as db:
        db.execute(
            "UPDATE runs SET status=?, verdict=?, summary_json=?, error=?, finished_at=? WHERE id=?",
            (status, verdict, json.dumps(summary or {}), error, now() if finished else None, run_id),
        )


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
        scenario = db.execute("SELECT * FROM scenarios WHERE id=?", (row["scenario_id"],)).fetchone()
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


def list_runs(limit: int = 30) -> list[dict[str, Any]]:
    with database() as db:
        rows = db.execute(
            "SELECT r.*, s.name AS scenario_name FROM runs r JOIN scenarios s ON s.id=r.scenario_id "
            "ORDER BY r.started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {"id": row["id"], "scenario_id": row["scenario_id"], "scenario": row["scenario_name"],
             "implementation": row["implementation"], "status": row["status"], "verdict": row["verdict"],
             "started_at": row["started_at"], "finished_at": row["finished_at"],
             "summary": json.loads(row["summary_json"]), "error": row["error"]}
            for row in rows
        ]
