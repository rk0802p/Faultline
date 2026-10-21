"""Faultline programmatic Python SDK and client helpers (Spec §11.6 & §20).

Provides a high-level, standard-library-only client interface to orchestrate
scenarios, verify invariants, trace operations with W3C context, inspect state
snapshots, and perform exploratory checkpoint branching.
"""

import json
import time
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class FaultlineError(Exception):
    """Base exception for Faultline SDK errors."""


class FaultlineAPIError(FaultlineError):
    """Exception raised when the Faultline control-plane API returns an error status."""

    def __init__(self, status_code: int, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.payload = payload or {}


class FaultlineTimeoutError(FaultlineError):
    """Raised when a run execution exceeds the client wait deadline."""


class FaultlineInvariantError(FaultlineError):
    """Raised when one or more domain invariants fail during scenario execution."""

    def __init__(self, run: dict[str, Any], failed_assertions: list[dict[str, Any]]) -> None:
        names = ", ".join(a.get("name", "unknown") for a in failed_assertions)
        super().__init__(f"Run {run.get('id')} failed invariant assertion(s): {names}")
        self.run = run
        self.failed_assertions = failed_assertions


class FaultlineClient:
    """Official programmatic client for the Faultline integration testing platform."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000", timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req_headers = {"Content-Type": "application/json"} if data else {}
        if headers:
            req_headers.update(headers)
        request = Request(url, data=data, headers=req_headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                content_type = response.getheader("Content-Type", "")
                raw = response.read()
                if "application/json" in content_type:
                    return json.loads(raw.decode("utf-8")) if raw else {}
                return raw.decode("utf-8")
        except HTTPError as exc:
            raw_err = exc.read().decode("utf-8")
            err_data = {}
            try:
                err_data = json.loads(raw_err)
            except Exception:
                err_data = {"error": raw_err or str(exc)}
            raise FaultlineAPIError(exc.code, err_data.get("error", str(exc)), err_data) from exc
        except URLError as exc:
            raise FaultlineError(f"Failed to connect to Faultline at {self.base_url}: {exc.reason}") from exc

    def health(self) -> dict[str, Any]:
        """Check control plane service health."""
        return self._request("GET", "/api/health")

    def metrics(self, format: str = "json") -> dict[str, Any] | str:
        """Fetch live operational metrics (supports 'json' or 'prometheus')."""
        if format.lower() == "prometheus":
            return self._request("GET", "/api/metrics?format=prometheus", headers={"Accept": "text/plain"})
        return self._request("GET", "/api/metrics")

    def list_scenarios(self) -> list[dict[str, Any]]:
        """List all available failure scenarios."""
        return self._request("GET", "/api/scenarios").get("scenarios", [])

    def get_scenario(self, scenario_id: str) -> dict[str, Any]:
        """Retrieve a specific scenario definition."""
        scenarios = self.list_scenarios()
        for sc in scenarios:
            if sc.get("id") == scenario_id:
                return sc
        raise FaultlineError(f"Scenario '{scenario_id}' not found")

    def list_runs(self) -> list[dict[str, Any]]:
        """List historical scenario runs."""
        return self._request("GET", "/api/runs").get("runs", [])

    def get_run(self, run_id: str) -> dict[str, Any]:
        """Get complete details, assertions, and summary for a run."""
        return self._request("GET", f"/api/runs/{run_id}").get("run", {})

    def submit_run(self, scenario_id: str, implementation: str = "fixed") -> str:
        """Submit a scenario run asynchronously and return the run ID."""
        res = self._request("POST", "/api/runs", {"scenario_id": scenario_id, "implementation": implementation})
        return res["run_id"]

    def wait_for_run(self, run_id: str, timeout: float = 30.0, poll_interval: float = 0.1) -> dict[str, Any]:
        """Poll until a run reaches a terminal state."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            run = self.get_run(run_id)
            if run.get("status") in {"completed", "failed", "timed_out", "cancelled"}:
                return run
            time.sleep(poll_interval)
        raise FaultlineTimeoutError(f"Run {run_id} did not finish within {timeout}s deadline")

    def run_scenario(
        self,
        scenario_id: str,
        implementation: str = "fixed",
        wait: bool = True,
        timeout: float = 30.0,
        assert_pass: bool = False,
    ) -> dict[str, Any]:
        """Execute a scenario, optionally waiting and asserting invariant pass."""
        run_id = self.submit_run(scenario_id, implementation)
        if not wait:
            return {"run_id": run_id, "status": "queued"}
        run = self.wait_for_run(run_id, timeout=timeout)
        if assert_pass:
            failed_assertions = [a for a in run.get("assertions", []) if a.get("status") != "passed"]
            if failed_assertions or run.get("verdict") != "passed":
                raise FaultlineInvariantError(run, failed_assertions)
        return run

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        """Request cancellation of an active or queued run."""
        return self._request("POST", f"/api/runs/{run_id}/cancel")

    def branch_run(
        self,
        run_id: str,
        snapshot_sequence: int | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Branch execution from a state snapshot, preserving parent immutability."""
        payload: dict[str, Any] = {}
        if snapshot_sequence is not None:
            payload["snapshot_sequence"] = snapshot_sequence
        if overrides:
            payload["overrides"] = overrides
        return self._request("POST", f"/api/runs/{run_id}/branch", payload).get("run", {})

    def set_baseline(self, run_id: str) -> dict[str, Any]:
        """Designate a run as regression baseline."""
        return self._request("POST", f"/api/runs/{run_id}/baseline")

    def get_baseline_diff(self, scenario_id: str, target_run_id: str | None = None) -> dict[str, Any]:
        """Compare a run against the designated scenario baseline."""
        path = f"/api/scenarios/{scenario_id}/baseline-diff"
        if target_run_id:
            path += f"?target_run_id={target_run_id}"
        return self._request("GET", path)

    def get_telemetry(self, run_id: str) -> dict[str, Any]:
        """Retrieve OpenTelemetry distributed trace spans for a run."""
        return self._request("GET", f"/api/runs/{run_id}/telemetry")

    def get_snapshots(self, run_id: str) -> list[dict[str, Any]]:
        """List state snapshots recorded during a run."""
        return self._request("GET", f"/api/runs/{run_id}/snapshots").get("snapshots", [])

    def get_state_at(self, run_id: str, sequence: int) -> dict[str, Any]:
        """Reconstruct state view model at a specific sequence number."""
        res = self._request("GET", f"/api/runs/{run_id}/state?sequence={sequence}")
        return res.get("state", res)

    def run_suite(
        self,
        suite_id: str = "critical-integrations",
        implementation: str = "fixed",
        synchronous: bool = True,
    ) -> dict[str, Any]:
        """Execute a regression test suite."""
        payload = {"implementation": implementation, "synchronous": synchronous}
        return self._request("POST", f"/api/suites/{suite_id}/runs", payload).get("suite_run", {})

    def list_suite_results(self, suite_id: str) -> list[dict[str, Any]]:
        """Get execution history for a test suite."""
        return self._request("GET", f"/api/suites/{suite_id}/results").get("results", [])

    def create_task(
        self,
        run_id: str,
        title: str,
        description: str = "",
        priority: str = "medium",
        assignee: str = "Developer",
    ) -> dict[str, Any]:
        """Create a defect remediation action task linked to a run."""
        payload = {
            "title": title,
            "description": description,
            "priority": priority,
            "assignee": assignee,
        }
        return self._request("POST", f"/api/runs/{run_id}/tasks", payload).get("task", {})

    def add_annotation(
        self,
        run_id: str,
        comment: str,
        author: str = "Developer",
        sequence_number: int | None = None,
    ) -> dict[str, Any]:
        """Add an engineering note or review annotation to a run."""
        payload: dict[str, Any] = {"comment": comment, "author": author}
        if sequence_number is not None:
            payload["sequence_number"] = sequence_number
        return self._request("POST", f"/api/runs/{run_id}/annotations", payload).get("annotation", {})

    def prune(
        self,
        project_id: str = "faultline-demo",
        max_age_days: int = 30,
        keep_baselines: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Execute artifact retention cleanup according to governance policies."""
        payload = {
            "max_age_days": max_age_days,
            "keep_baselines": keep_baselines,
            "dry_run": dry_run,
        }
        return self._request("POST", f"/api/projects/{project_id}/prune", payload)

    @staticmethod
    def w3c_traceparent(trace_id: str, span_id: str) -> str:
        """Format W3C Traceparent header string."""
        return f"00-{trace_id}-{span_id}-01"

    @contextmanager
    def trace_context(self, run_id: str) -> Iterator[dict[str, str]]:
        """Context manager yielding W3C traceparent headers linked to a run."""
        telemetry = self.get_telemetry(run_id)
        trace_id = telemetry.get("traceId") or telemetry.get("trace_id", "0" * 32)
        spans = telemetry.get("spans", [])
        span_id = (spans[0].get("spanId") or spans[0].get("span_id")) if spans else "0" * 16
        headers = {"traceparent": self.w3c_traceparent(trace_id, span_id)}
        yield headers
