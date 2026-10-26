"""Target application contract, configuration schema, registry, and execution supervisor.

Implements Phase 01: Generic Target Application Support.
Enables Faultline to connect to or manage arbitrary independently implemented applications.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from http.client import HTTPConnection, HTTPSConnection
from typing import Any
from urllib.parse import urlparse

from . import security, store

logger = logging.getLogger("faultline.targets")

TARGET_ID_REGEX = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
VALID_TARGET_MODES = {"connect", "managed"}


@dataclass
class TargetConfig:
    id: str
    name: str
    description: str = ""
    project_id: str = "faultline-demo"
    mode: str = "connect"  # "connect" or "managed"
    base_url: str = "http://127.0.0.1:8000"
    health_endpoint: str = "/health"
    health_timeout_seconds: float = 2.0
    services: list[dict[str, Any]] = field(default_factory=list)
    dependencies: list[dict[str, Any]] = field(default_factory=list)
    permitted_faults: list[str] = field(default_factory=lambda: ["http_timeout"])
    managed_command: list[str] | None = None
    managed_env: dict[str, str] | None = None
    observation_config: dict[str, Any] = field(default_factory=dict)
    cleanup_config: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TargetConfig:
        cleaned = dict(data)
        # Ensure optional fields with defaults are preserved
        return cls(
            id=str(cleaned["id"]),
            name=str(cleaned.get("name") or cleaned["id"]),
            description=str(cleaned.get("description", "")),
            project_id=str(cleaned.get("project_id", "faultline-demo")),
            mode=str(cleaned.get("mode", "connect")).lower(),
            base_url=str(cleaned.get("base_url", "http://127.0.0.1:8000")).rstrip("/"),
            health_endpoint=str(cleaned.get("health_endpoint", "/health")),
            health_timeout_seconds=float(cleaned.get("health_timeout_seconds", 2.0)),
            services=list(cleaned.get("services", [])),
            dependencies=list(cleaned.get("dependencies", [])),
            permitted_faults=list(cleaned.get("permitted_faults", ["http_timeout"])),
            managed_command=cleaned.get("managed_command"),
            managed_env=cleaned.get("managed_env"),
            observation_config=dict(cleaned.get("observation_config", {})),
            cleanup_config=dict(cleaned.get("cleanup_config", {})),
            metadata=dict(cleaned.get("metadata", {})),
        )


def validate_target_config(config: dict[str, Any] | TargetConfig) -> TargetConfig:
    """Validate a target application configuration against schema and security policies."""
    if isinstance(config, TargetConfig):
        cfg = config
    elif isinstance(config, dict):
        cfg = TargetConfig.from_dict(config)
    else:
        raise ValueError(f"Invalid target configuration type: {type(config).__name__}")

    if not cfg.id or not TARGET_ID_REGEX.match(cfg.id):
        raise ValueError(f"Target ID '{cfg.id}' is invalid. Must match regex: ^[a-z0-9][a-z0-9_-]{{1,63}}$")

    if not cfg.name or not cfg.name.strip():
        raise ValueError("Target name must not be empty")

    if cfg.mode not in VALID_TARGET_MODES:
        raise ValueError(f"Target mode '{cfg.mode}' is invalid. Must be one of: {sorted(VALID_TARGET_MODES)}")

    # Enforce URL safety and loopback allowlists
    security.assert_loopback_safe(cfg.base_url)

    if not cfg.health_endpoint.startswith("/"):
        raise ValueError(f"Target health_endpoint '{cfg.health_endpoint}' must start with '/'")

    if cfg.health_timeout_seconds <= 0 or cfg.health_timeout_seconds > 60:
        raise ValueError(f"Target health_timeout_seconds must be between 0 and 60, got {cfg.health_timeout_seconds}")

    for idx, svc in enumerate(cfg.services):
        if not isinstance(svc, dict) or "id" not in svc or "base_url" not in svc:
            raise ValueError(f"Service at index {idx} must be a dict with 'id' and 'base_url'")
        security.assert_loopback_safe(svc["base_url"])
        if "health_endpoint" in svc and not str(svc["health_endpoint"]).startswith("/"):
            raise ValueError(f"Service '{svc['id']}' health_endpoint must start with '/'")

    for idx, dep in enumerate(cfg.dependencies):
        if not isinstance(dep, dict) or "id" not in dep or "base_url" not in dep:
            raise ValueError(f"Dependency at index {idx} must be a dict with 'id' and 'base_url'")
        security.assert_loopback_safe(dep["base_url"])

    if cfg.mode == "managed":
        if not cfg.managed_command or not isinstance(cfg.managed_command, list) or len(cfg.managed_command) == 0:
            raise ValueError("Target in 'managed' mode requires a non-empty list of command arguments for 'managed_command'")

    return cfg


# Active managed subprocess registry
_managed_lock = threading.Lock()
_managed_processes: dict[str, subprocess.Popen] = {}


def check_service_health(base_url: str, health_endpoint: str = "/health", timeout: float = 2.0) -> dict[str, Any]:
    """Perform a single HTTP GET health check against an endpoint."""
    parsed = urlparse(base_url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if scheme == "https" else 80)
    path = health_endpoint if health_endpoint.startswith("/") else f"/{health_endpoint}"

    t0 = time.monotonic()
    conn_cls = HTTPSConnection if scheme == "https" else HTTPConnection
    conn = None
    try:
        conn = conn_cls(host, port, timeout=timeout)
        conn.request("GET", path, headers={"Accept": "application/json, text/plain"})
        resp = conn.getresponse()
        elapsed_ms = max(1, int((time.monotonic() - t0) * 1000))
        resp.read(4096)
        is_healthy = 200 <= resp.status < 300
        return {
            "status": "healthy" if is_healthy else "unhealthy",
            "status_code": resp.status,
            "latency_ms": elapsed_ms,
            "error": None if is_healthy else f"HTTP {resp.status}",
        }
    except Exception as exc:
        elapsed_ms = max(1, int((time.monotonic() - t0) * 1000))
        return {
            "status": "unreachable",
            "status_code": None,
            "latency_ms": elapsed_ms,
            "error": str(exc),
        }
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


class TargetRegistry:
    """Thread-safe registry managing target application configurations, health checks, and process lifecycles."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def register(self, config: TargetConfig | dict[str, Any], project_id: str = "faultline-demo") -> TargetConfig:
        validated = validate_target_config(config)
        validated.project_id = project_id
        store.create_target(validated.id, project_id, validated.to_dict())
        return validated

    def get(self, target_id: str) -> TargetConfig | None:
        raw = store.get_target(target_id)
        if not raw:
            return None
        return TargetConfig.from_dict(raw["config"])

    def list(self, project_id: str = "faultline-demo") -> list[TargetConfig]:
        items = store.list_targets(project_id)
        return [TargetConfig.from_dict(item["config"]) for item in items]

    def delete(self, target_id: str) -> bool:
        self.stop_managed(target_id)
        return store.delete_target(target_id)

    def check_health(self, target_id: str, timeout: float | None = None) -> dict[str, Any]:
        target = self.get(target_id)
        if not target:
            raise ValueError(f"Target application '{target_id}' not found")

        eff_timeout = timeout or target.health_timeout_seconds
        main_health = check_service_health(target.base_url, target.health_endpoint, eff_timeout)

        services_health = []
        for svc in target.services:
            s_base = svc.get("base_url", target.base_url)
            s_ep = svc.get("health_endpoint", target.health_endpoint)
            h = check_service_health(s_base, s_ep, eff_timeout)
            services_health.append({
                "service_id": svc["id"],
                "name": svc.get("name", svc["id"]),
                "role": svc.get("role", "service"),
                **h,
            })

        # Overall target status determination
        if main_health["status"] == "healthy":
            if any(s["status"] != "healthy" for s in services_health):
                overall_status = "degraded"
            else:
                overall_status = "healthy"
        else:
            overall_status = main_health["status"]

        return {
            "target_id": target.id,
            "name": target.name,
            "mode": target.mode,
            "base_url": target.base_url,
            "status": overall_status,
            "main_service": main_health,
            "services": services_health,
            "checked_at": store.now(),
        }

    def start_managed(self, target_id: str, timeout: float = 10.0) -> dict[str, Any]:
        """Start a target application in managed mode and poll until healthy."""
        target = self.get(target_id)
        if not target:
            raise ValueError(f"Target '{target_id}' not found")
        if target.mode != "managed":
            raise ValueError(f"Target '{target_id}' is configured in '{target.mode}' mode, not 'managed' mode")
        if not target.managed_command:
            raise ValueError(f"Target '{target_id}' does not have a managed_command configured")

        with _managed_lock:
            existing = _managed_processes.get(target_id)
            if existing and existing.poll() is None:
                # Already running
                return {"status": "already_running", "pid": existing.pid}

            env = dict(os.environ)
            if target.managed_env:
                env.update(target.managed_env)

            proc = subprocess.Popen(
                target.managed_command,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            _managed_processes[target_id] = proc

        # Poll health endpoint
        t_deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < t_deadline:
            if proc.poll() is not None:
                out, err = proc.communicate(timeout=1.0)
                raise RuntimeError(
                    f"Managed target process exited prematurely with code {proc.returncode}.\nSTDOUT:\n{out}\nSTDERR:\n{err}"
                )
            h = check_service_health(target.base_url, target.health_endpoint, timeout=0.5)
            if h["status"] == "healthy":
                logger.info("Managed target '%s' successfully started with PID %d", target_id, proc.pid)
                return {"status": "started", "pid": proc.pid, "health": h}
            last_error = h.get("error")
            time.sleep(0.1)

        # Timeout reached without healthy status
        self.stop_managed(target_id)
        raise TimeoutError(f"Managed target '{target_id}' did not become healthy within {timeout}s: {last_error}")

    def stop_managed(self, target_id: str) -> bool:
        """Stop a running managed target process cleanly."""
        with _managed_lock:
            proc = _managed_processes.pop(target_id, None)
            if not proc:
                return False
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1.0)
                logger.info("Stopped managed target '%s' (PID %d)", target_id, proc.pid)
                return True
            return False

    def stop_all_managed(self) -> None:
        """Stop all running managed target processes."""
        with _managed_lock:
            for target_id, proc in list(_managed_processes.items()):
                if proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=2.0)
                    except Exception:
                        proc.kill()
            _managed_processes.clear()


target_registry = TargetRegistry()
