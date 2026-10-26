"""Domain-independent declarative assertion engine and invariant evaluators (Spec §15, Phase 03).

Enables declarative invariant verification across databases, HTTP responses,
ledger reconciliation, and idempotency guarantees without custom Python code.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("faultline.assertions")


def _resolve_json_path(data: Any, path: str) -> Any:
    """Extract nested value from dict/list using dot notation (e.g. 'user.profile.id' or 'items.0.name')."""
    if not path:
        return data
    curr = data
    for part in path.split("."):
        if isinstance(curr, dict):
            curr = curr.get(part)
        elif isinstance(curr, list):
            try:
                idx = int(part)
                curr = curr[idx] if 0 <= idx < len(curr) else None
            except ValueError:
                return None
        else:
            return None
    return curr


def compare(op: str, actual: Any, expected: Any) -> tuple[bool, str, str]:
    """Applies comparison operator between actual and expected values.

    Returns (passed: bool, expected_desc: str, observed_desc: str).
    """
    op = (op or "equals").lower().strip()

    if op in ("equals", "eq", "=="):
        passed = actual == expected
        return passed, f"== {expected!r}", f"{actual!r}"

    if op in ("not_equals", "ne", "!="):
        passed = actual != expected
        return passed, f"!= {expected!r}", f"{actual!r}"

    if op in ("greater_than", "gt", ">"):
        try:
            passed = float(actual) > float(expected)
        except (ValueError, TypeError):
            passed = False
        return passed, f"> {expected}", f"{actual}"

    if op in ("greater_than_or_equal", "gte", ">="):
        try:
            passed = float(actual) >= float(expected)
        except (ValueError, TypeError):
            passed = False
        return passed, f">= {expected}", f"{actual}"

    if op in ("less_than", "lt", "<"):
        try:
            passed = float(actual) < float(expected)
        except (ValueError, TypeError):
            passed = False
        return passed, f"< {expected}", f"{actual}"

    if op in ("less_than_or_equal", "lte", "<="):
        try:
            passed = float(actual) <= float(expected)
        except (ValueError, TypeError):
            passed = False
        return passed, f"<= {expected}", f"{actual}"

    if op in ("in", "contains"):
        if isinstance(actual, (list, tuple, set, str)):
            passed = expected in actual
        elif isinstance(actual, dict):
            passed = expected in actual
        else:
            passed = False
        return passed, f"contains {expected!r}", f"{actual!r}"

    if op in ("not_in", "does_not_contain"):
        if isinstance(actual, (list, tuple, set, str)):
            passed = expected not in actual
        else:
            passed = True
        return passed, f"does not contain {expected!r}", f"{actual!r}"

    if op == "between":
        try:
            low, high = expected
            passed = float(low) <= float(actual) <= float(high)
            return passed, f"between {low} and {high}", f"{actual}"
        except Exception:
            return False, f"valid range {expected}", f"{actual}"

    if op == "no_duplicates":
        if isinstance(actual, (list, tuple)):
            dups = len(actual) - len(set(actual))
            passed = dups == 0
            return passed, "0 duplicates", f"{dups} duplicates found in {len(actual)} elements"
        return False, "iterable for duplicate check", f"{type(actual).__name__}"

    if op in ("is_empty", "empty"):
        passed = len(actual) == 0 if isinstance(actual, (list, tuple, dict, str)) else actual is None
        return passed, "empty collection or None", f"size {len(actual) if hasattr(actual, '__len__') else actual}"

    if op in ("is_not_empty", "not_empty"):
        passed = len(actual) > 0 if isinstance(actual, (list, tuple, dict, str)) else actual is not None
        return passed, "non-empty collection", f"size {len(actual) if hasattr(actual, '__len__') else actual}"

    return False, f"supported operator (got '{op}')", f"{actual!r}"


class DeclarativeAssertionEngine:
    """Evaluates declarative business assertions without custom code."""

    def evaluate_database_assertion(
        self,
        spec: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Evaluates database invariants using SQL query against target or test SQLite DB."""
        name = spec.get("name", "database_invariant")
        db_path = spec.get("db_path") or context.get("db_path")
        sql = spec.get("query", "")
        raw_params = spec.get("params", [])
        params = [
            context.get(p.strip("{}"), p) if isinstance(p, str) and p.startswith("{") and p.endswith("}") else p
            for p in raw_params
        ]
        op = spec.get("op", "equals")
        expected = spec.get("expected")

        if not db_path:
            return {
                "name": name,
                "status": "failed",
                "expected": f"valid db_path for database assertion '{name}'",
                "observed": "no db_path specified in assertion or run context",
                "evidence": ["CONFIG_ERROR"],
            }

        if not Path(db_path).exists():
            return {
                "name": name,
                "status": "failed",
                "expected": f"database file exists at {db_path}",
                "observed": f"file not found: {db_path}",
                "evidence": ["DB_NOT_FOUND"],
            }

        try:
            conn = sqlite3.connect(str(db_path), timeout=5.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            conn.close()

            column = spec.get("column")
            if column:
                actual_val = [r[column] for r in rows if column in r.keys()]
            elif op == "no_duplicates":
                col = spec.get("column") or (rows[0].keys()[0] if rows else "id")
                actual_val = [r[col] for r in rows if col in r.keys()]
            elif len(rows) == 1 and len(rows[0].keys()) == 1:
                actual_val = rows[0][0]
            elif spec.get("extract") == "count" or sql.strip().upper().startswith("SELECT COUNT"):
                actual_val = rows[0][0] if rows else 0
            elif spec.get("extract") == "rows":
                actual_val = [dict(r) for r in rows]
            else:
                actual_val = len(rows)

            passed, exp_desc, obs_desc = compare(op, actual_val, expected)
            return {
                "name": name,
                "status": "passed" if passed else "failed",
                "expected": f"{op} {expected}" if exp_desc.startswith("==") else exp_desc,
                "observed": obs_desc,
                "evidence": [f"db_assert:{name}"] if passed else [f"db_violation:{name}:{obs_desc}"],
            }
        except Exception as exc:
            return {
                "name": name,
                "status": "failed",
                "expected": "successful query execution",
                "observed": f"SQL error: {exc}",
                "evidence": [f"db_error:{name}"],
            }

    def evaluate_http_assertion(
        self,
        spec: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Evaluates assertions against workflow step HTTP responses and side effects."""
        name = spec.get("name", "http_invariant")
        step_id = spec.get("step_id")
        side_effects = context.get("side_effects", [])

        # Find target side effect
        target_se = None
        if step_id:
            for se in side_effects:
                if se.get("step_id") == step_id:
                    target_se = se
                    break
        elif side_effects:
            target_se = side_effects[-1]

        if not target_se:
            return {
                "name": name,
                "status": "failed",
                "expected": f"side effect for step '{step_id or 'last'}'",
                "observed": "no matching step side effect found in execution summary",
                "evidence": ["STEP_NOT_FOUND"],
            }

        # Check status code
        if "status_code" in spec:
            exp_status = spec["status_code"]
            actual_status = target_se.get("status")
            if isinstance(exp_status, list):
                passed = actual_status in exp_status
                exp_desc = f"status in {exp_status}"
            else:
                passed = actual_status == exp_status
                exp_desc = f"status == {exp_status}"
            obs_desc = f"status == {actual_status}"
            if not passed:
                return {
                    "name": name,
                    "status": "failed",
                    "expected": exp_desc,
                    "observed": obs_desc,
                    "evidence": [f"http_status_mismatch:{actual_status}"],
                }

        # Check latency bound
        if "max_latency_seconds" in spec:
            max_lat = float(spec["max_latency_seconds"])
            actual_lat = float(target_se.get("elapsed_seconds", 0))
            if actual_lat > max_lat:
                return {
                    "name": name,
                    "status": "failed",
                    "expected": f"latency <= {max_lat}s",
                    "observed": f"latency == {actual_lat:.3f}s",
                    "evidence": ["LATENCY_EXCEEDED"],
                }

        # Check JSON path if payload is present
        if "json_path" in spec:
            body_data = target_se.get("json_data") or target_se.get("body")
            if isinstance(body_data, str):
                try:
                    body_data = json.loads(body_data)
                except Exception:
                    pass
            actual_val = _resolve_json_path(body_data, spec["json_path"])
            op = spec.get("op", "equals")
            expected = spec.get("expected")
            passed, exp_desc, obs_desc = compare(op, actual_val, expected)
            return {
                "name": name,
                "status": "passed" if passed else "failed",
                "expected": f"{spec['json_path']} {exp_desc}",
                "observed": f"{spec['json_path']} == {obs_desc}",
                "evidence": [f"json_path:{spec['json_path']}"] if passed else ["JSON_PATH_ASSERTION_FAILED"],
            }

        return {
            "name": name,
            "status": "passed",
            "expected": "HTTP response invariant satisfied",
            "observed": "all response constraints satisfied",
            "evidence": ["HTTP_INVARIANT_SATISFIED"],
        }

    def evaluate_reconciliation_assertion(
        self,
        spec: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Cross-system reconciliation comparing two queries or state snapshots."""
        name = spec.get("name", "cross_system_reconciliation")
        db_path_left = spec.get("left_db_path") or spec.get("db_path") or context.get("db_path")
        sql_left = spec.get("left_query", "")
        db_path_right = spec.get("right_db_path") or spec.get("db_path") or context.get("db_path")
        sql_right = spec.get("right_query", "")
        op = spec.get("op", "equals")

        if not db_path_left or not db_path_right:
            return {
                "name": name,
                "status": "failed",
                "expected": "left and right database paths specified",
                "observed": f"left={db_path_left}, right={db_path_right}",
                "evidence": ["RECONCILIATION_CONFIG_ERROR"],
            }

        try:
            conn_l = sqlite3.connect(str(db_path_left), timeout=5.0)
            res_l = conn_l.cursor().execute(sql_left).fetchone()[0]
            conn_l.close()

            conn_r = sqlite3.connect(str(db_path_right), timeout=5.0)
            res_r = conn_r.cursor().execute(sql_right).fetchone()[0]
            conn_r.close()

            passed, exp_desc, obs_desc = compare(op, res_l, res_r)
            return {
                "name": name,
                "status": "passed" if passed else "failed",
                "expected": f"left ({res_l}) {op} right ({res_r})",
                "observed": f"left={res_l}, right={res_r}",
                "evidence": ["RECONCILIATION_BALANCED"] if passed else ["RECONCILIATION_DISCREPANCY"],
            }
        except Exception as exc:
            return {
                "name": name,
                "status": "failed",
                "expected": "successful reconciliation query execution",
                "observed": f"error: {exc}",
                "evidence": ["RECONCILIATION_ERROR"],
            }

    def evaluate_idempotency_assertion(
        self,
        spec: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Verifies that despite retries, exactly 1 permanent mutation occurred."""
        name = spec.get("name", "idempotency_verified")
        db_path = spec.get("db_path") or context.get("db_path")
        table = spec.get("table", "bookings")
        key_column = spec.get("key_column", "idempotency_key")
        key_value = spec.get("key_value") or context.get(key_column) or context.get("idempotency_key")

        if not db_path or not Path(db_path).exists():
            return {
                "name": name,
                "status": "failed",
                "expected": f"database file exists at {db_path}",
                "observed": "database path not found or not created",
                "evidence": ["IDEMPOTENCY_DB_NOT_FOUND"],
            }

        try:
            conn = sqlite3.connect(str(db_path), timeout=5.0)
            cursor = conn.cursor()
            if key_value:
                cursor.execute(f"SELECT count(*) FROM {table} WHERE {key_column} = ?", (key_value,))
            else:
                cursor.execute(f"SELECT count(*) FROM {table}")
            count = cursor.fetchone()[0]
            conn.close()

            expected_count = int(spec.get("expected", 1))
            passed = count == expected_count
            return {
                "name": name,
                "status": "passed" if passed else "failed",
                "expected": f"exactly {expected_count} record for {key_column}={key_value!r}",
                "observed": f"{count} records found in table '{table}'",
                "evidence": ["IDEMPOTENT_WRITE_CONFIRMED"] if passed else [f"DUPLICATE_MUTATIONS_FOUND:{count}"],
            }
        except Exception as exc:
            return {
                "name": name,
                "status": "failed",
                "expected": "successful idempotency verification query",
                "observed": f"error: {exc}",
                "evidence": ["IDEMPOTENCY_QUERY_ERROR"],
            }

    def evaluate(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """Dispatches declarative assertion specification to appropriate evaluator."""
        assert_type = spec.get("type", "database")
        if assert_type in ("database", "db"):
            return self.evaluate_database_assertion(spec, context)
        elif assert_type in ("http_response", "http", "response"):
            return self.evaluate_http_assertion(spec, context)
        elif assert_type in ("reconciliation", "reconcile"):
            return self.evaluate_reconciliation_assertion(spec, context)
        elif assert_type in ("idempotency", "deduplication"):
            return self.evaluate_idempotency_assertion(spec, context)
        else:
            return {
                "name": spec.get("name", "unsupported_assertion"),
                "status": "failed",
                "expected": "supported assertion type (database, http_response, reconciliation, idempotency)",
                "observed": f"unknown type '{assert_type}'",
                "evidence": ["UNSUPPORTED_ASSERTION_TYPE"],
            }


# Global declarative engine instance
declarative_engine = DeclarativeAssertionEngine()


def evaluate_declarative_assertion(spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Helper entry point for evaluating a declarative assertion."""
    return declarative_engine.evaluate(spec, context)
