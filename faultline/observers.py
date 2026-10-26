"""Independent observation adapters and registries for verifiable business correctness.

Implements Phase 02: Real Fault Injection & Observation.
Verifies business correctness independently from the tested application's own HTTP responses:
- ObservationAdapter: Abstract interface for gathering ground-truth business state.
- DatabaseObserver: Directly queries SQLite or relational databases using parameterized SQL.
- HttpStateObserver: Out-of-band HTTP inspection against audit, admin, or query endpoints.
- MultiSourceObserver: Aggregates multiple observers into a unified state report.
- ObserverRegistry: Thread-safe registry managing named observers.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from abc import ABC, abstractmethod
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from . import security, store

logger = logging.getLogger("faultline.observers")


class ObservationAdapter(ABC):
    """Abstract interface for gathering ground-truth business state."""

    def __init__(self, name: str = "generic_observer") -> None:
        self.name = name

    @abstractmethod
    def observe(self, run_id: str, context: dict[str, Any]) -> dict[str, Any]:
        """Collect ground-truth facts and evidence from the target system.

        Returns a dictionary of observed facts and evidence items.
        """
        ...

    @abstractmethod
    def verify_assertions(
        self,
        run_id: str,
        assertion_configs: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Evaluate business invariant assertions against observed state.

        Returns a list of assertion outcome dicts:
        [{'name': str, 'status': 'passed'|'failed', 'expected': Any, 'observed': Any, 'evidence': list[str]}]
        """
        ...


class DatabaseObserver(ObservationAdapter):
    """Direct database state observer querying SQLite (or relational via DB-API 2.0).

    Inspects tables directly to verify state transitions, row counts, duplicate records,
    or ledger balances without trusting the application's HTTP response.
    """

    def __init__(
        self,
        db_path: str | Path,
        name: str = "database_observer",
        connection_factory: Callable[[], sqlite3.Connection] | None = None,
    ) -> None:
        super().__init__(name=name)
        self.db_path = Path(db_path) if isinstance(db_path, str) else db_path
        self._connection_factory = connection_factory

    def _get_connection(self) -> sqlite3.Connection:
        if self._connection_factory:
            return self._connection_factory()
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        """Executes a parameterized read-only SQL query and returns rows as dicts."""
        # Safety guard: prevent destructive statements in observation mode
        sql_stripped = sql.strip().upper()
        if any(sql_stripped.startswith(w) for w in ("DROP", "DELETE", "TRUNCATE", "ALTER")):
            raise ValueError(f"Destructive queries not permitted in DatabaseObserver: {sql[:30]}")

        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def observe(self, run_id: str, context: dict[str, Any]) -> dict[str, Any]:
        """Collects database snapshot based on queries declared in context or config."""
        queries = context.get("queries") or {}
        results: dict[str, Any] = {}

        for key, q_spec in queries.items():
            if isinstance(q_spec, str):
                sql = q_spec
                params: list[Any] = []
            elif isinstance(q_spec, dict):
                sql = q_spec.get("sql", "")
                raw_params = q_spec.get("params", [])
                # Interpolate context parameters if string template
                params = [
                    context.get(p.strip("{}"), p) if isinstance(p, str) and p.startswith("{") and p.endswith("}") else p
                    for p in raw_params
                ]
            else:
                continue

            try:
                rows = self.query(sql, params)
                results[key] = {
                    "count": len(rows),
                    "rows": security.redact_sensitive(rows),
                }
            except Exception as exc:
                results[key] = {
                    "error": str(exc),
                    "count": -1,
                    "rows": [],
                }

        try:
            store.record_event(run_id, "OBSERVATION_RECORDED", {
                "observer": self.name,
                "type": "database",
                "db_path": str(self.db_path),
                "results": results,
            })
        except Exception:
            pass
        return results

    def verify_assertions(
        self,
        run_id: str,
        assertion_configs: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []

        for cfg in assertion_configs:
            name = cfg.get("name", "unnamed_db_assertion")
            check_type = cfg.get("check", "exact_row_count")
            sql = cfg.get("query", "")
            raw_params = cfg.get("params", [])
            params = [
                context.get(p.strip("{}"), p) if isinstance(p, str) and p.startswith("{") and p.endswith("}") else p
                for p in raw_params
            ]

            try:
                rows = self.query(sql, params)
                passed = False
                observed_desc = ""
                expected_desc = ""
                evidence = [f"db_query:{self.name}:{name}"]

                if check_type == "exact_row_count":
                    expected_count = int(cfg.get("expected", 1))
                    actual_count = len(rows)
                    if rows and len(rows) == 1 and "count" in rows[0]:
                        actual_count = int(rows[0]["count"])
                    elif rows and len(rows) == 1 and "COUNT(*)" in rows[0]:
                        actual_count = int(rows[0]["COUNT(*)"])
                    passed = actual_count == expected_count
                    expected_desc = f"row count == {expected_count}"
                    observed_desc = f"row count == {actual_count}"

                elif check_type == "max_row_count":
                    max_count = int(cfg.get("expected", 1))
                    actual_count = len(rows)
                    passed = actual_count <= max_count
                    expected_desc = f"row count <= {max_count}"
                    observed_desc = f"row count == {actual_count}"

                elif check_type == "no_duplicate_records":
                    col = cfg.get("column", "id")
                    values = [r.get(col) for r in rows if col in r]
                    duplicates = len(values) - len(set(values))
                    passed = duplicates == 0
                    expected_desc = f"zero duplicate values for column '{col}'"
                    observed_desc = f"{duplicates} duplicates found among {len(values)} rows"

                elif check_type == "row_field_equals":
                    col = cfg.get("column", "status")
                    expected_val = cfg.get("expected")
                    if not rows:
                        passed = False
                        expected_desc = f"row[0]['{col}'] == {expected_val}"
                        observed_desc = "no rows returned"
                    else:
                        actual_val = rows[0].get(col)
                        passed = actual_val == expected_val
                        expected_desc = f"row[0]['{col}'] == {expected_val}"
                        observed_desc = f"row[0]['{col}'] == {actual_val}"

                else:
                    passed = False
                    expected_desc = f"supported check type"
                    observed_desc = f"unknown check type '{check_type}'"

                outcomes.append({
                    "name": name,
                    "status": "passed" if passed else "failed",
                    "expected": expected_desc,
                    "observed": observed_desc,
                    "evidence": evidence,
                })
            except Exception as exc:
                outcomes.append({
                    "name": name,
                    "status": "failed",
                    "expected": f"successful query execution",
                    "observed": f"query error: {exc}",
                    "evidence": [f"db_error:{self.name}"],
                })

        return outcomes


class HttpStateObserver(ObservationAdapter):
    """Out-of-band HTTP state observer.

    Queries inspection, health, or read-replica endpoints to verify system state
    independently from the tested workflow steps.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8990",
        name: str = "http_state_observer",
        default_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(name=name)
        self.base_url = base_url.rstrip("/")
        self.default_headers = default_headers or {}

    def _request(self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        url = path if path.startswith("http://") or path.startswith("https://") else f"{self.base_url}{path}"
        parsed = urlparse(url)
        scheme = parsed.scheme or "http"
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if scheme == "https" else 80)
        req_path = parsed.path or "/"
        if parsed.query:
            req_path = f"{req_path}?{parsed.query}"

        all_headers = dict(self.default_headers)
        if headers:
            all_headers.update(headers)

        conn_cls = HTTPSConnection if scheme == "https" else HTTPConnection
        conn = conn_cls(host, port, timeout=5.0)
        try:
            conn.request(method.upper(), req_path, body=body, headers=all_headers)
            resp = conn.getresponse()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            raw = resp.read()
            return resp.status, resp_headers, raw
        finally:
            conn.close()

    def observe(self, run_id: str, context: dict[str, Any]) -> dict[str, Any]:
        path = context.get("observe_path", "/v1/health")
        # Interpolate variables in path
        for k, v in context.items():
            if isinstance(v, (str, int)):
                path = path.replace(f"{{{k}}}", str(v))

        try:
            status, headers, body = self._request("GET", path)
            parsed_json = None
            if "application/json" in headers.get("content-type", ""):
                try:
                    parsed_json = json.loads(body.decode("utf-8"))
                except Exception:
                    pass

            result = {
                "status_code": status,
                "json": security.redact_sensitive(parsed_json) if parsed_json else None,
                "body_snippet": body[:256].decode("utf-8", errors="replace"),
            }
            try:
                store.record_event(run_id, "HTTP_OBSERVATION_RECORDED", {
                    "observer": self.name,
                    "path": path,
                    "status_code": status,
                })
            except Exception:
                pass
            return result
        except Exception as exc:
            return {"status_code": 0, "error": str(exc)}

    def verify_assertions(
        self,
        run_id: str,
        assertion_configs: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []

        for cfg in assertion_configs:
            name = cfg.get("name", "unnamed_http_assertion")
            path = cfg.get("path", "/v1/health")
            for k, v in context.items():
                if isinstance(v, (str, int)):
                    path = path.replace(f"{{{k}}}", str(v))

            check_type = cfg.get("check", "status_code_equals")
            try:
                status, headers, body = self._request("GET", path)
                parsed_json = {}
                try:
                    parsed_json = json.loads(body.decode("utf-8"))
                except Exception:
                    pass

                passed = False
                expected_desc = ""
                observed_desc = ""

                if check_type == "status_code_equals":
                    expected_status = int(cfg.get("expected", 200))
                    passed = status == expected_status
                    expected_desc = f"HTTP {expected_status}"
                    observed_desc = f"HTTP {status}"

                elif check_type == "json_field_equals":
                    field_path = cfg.get("field", "status")
                    expected_val = cfg.get("expected")
                    actual_val = self._extract_json_field(parsed_json, field_path)
                    passed = actual_val == expected_val
                    expected_desc = f"{field_path} == {expected_val}"
                    observed_desc = f"{field_path} == {actual_val}"

                else:
                    passed = False
                    expected_desc = "supported check type"
                    observed_desc = f"unknown check type '{check_type}'"

                outcomes.append({
                    "name": name,
                    "status": "passed" if passed else "failed",
                    "expected": expected_desc,
                    "observed": observed_desc,
                    "evidence": [f"http_observer:{self.name}:{name}"],
                })
            except Exception as exc:
                outcomes.append({
                    "name": name,
                    "status": "failed",
                    "expected": "successful HTTP observation",
                    "observed": f"error: {exc}",
                    "evidence": [f"http_error:{self.name}"],
                })

        return outcomes

    def _extract_json_field(self, obj: Any, path: str) -> Any:
        parts = path.split(".")
        curr = obj
        for p in parts:
            if isinstance(curr, dict):
                curr = curr.get(p)
            else:
                return None
        return curr


class MultiSourceObserver(ObservationAdapter):
    """Combines multiple independent observers into a unified verification report."""

    def __init__(self, observers: list[ObservationAdapter] | None = None, name: str = "multi_source_observer") -> None:
        super().__init__(name=name)
        self.observers: list[ObservationAdapter] = observers or []

    def add_observer(self, observer: ObservationAdapter) -> None:
        self.observers.append(observer)

    def observe(self, run_id: str, context: dict[str, Any]) -> dict[str, Any]:
        combined: dict[str, Any] = {}
        for obs in self.observers:
            try:
                combined[obs.name] = obs.observe(run_id, context)
            except Exception as exc:
                combined[obs.name] = {"error": str(exc)}
        return combined

    def verify_assertions(
        self,
        run_id: str,
        assertion_configs: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        all_outcomes: list[dict[str, Any]] = []
        for obs in self.observers:
            try:
                outcomes = obs.verify_assertions(run_id, assertion_configs, context)
                all_outcomes.extend(outcomes)
            except Exception as exc:
                logger.warning("Observer %s failed to verify assertions: %s", obs.name, exc)
        return all_outcomes


class ObserverRegistry:
    """Thread-safe registry managing independent observation adapters."""

    def __init__(self) -> None:
        self._observers: dict[str, ObservationAdapter] = {}
        self._lock = threading.Lock()

    def register(self, name: str, observer: ObservationAdapter) -> None:
        with self._lock:
            self._observers[name] = observer

    def get(self, name: str) -> ObservationAdapter | None:
        with self._lock:
            return self._observers.get(name)

    def remove(self, name: str) -> None:
        with self._lock:
            self._observers.pop(name, None)

    def all(self) -> dict[str, ObservationAdapter]:
        with self._lock:
            return dict(self._observers)

    def clear(self) -> None:
        with self._lock:
            self._observers.clear()

    def create_from_config(self, config: dict[str, Any]) -> ObservationAdapter:
        obs_type = config.get("type", "database")
        name = config.get("name", f"{obs_type}_observer")
        cfg = config.get("config", {})

        if obs_type == "database":
            db_path = cfg.get("db_path") or "data/faultline.sqlite3"
            return DatabaseObserver(db_path=db_path, name=name)
        elif obs_type == "http":
            base_url = cfg.get("base_url") or "http://127.0.0.1:8990"
            return HttpStateObserver(base_url=base_url, name=name)
        elif obs_type == "multi":
            sub_configs = cfg.get("observers", [])
            sub_observers = [self.create_from_config(sc) for sc in sub_configs]
            return MultiSourceObserver(observers=sub_observers, name=name)
        else:
            raise ValueError(f"Unsupported observer type: '{obs_type}'")


observer_registry = ObserverRegistry()
