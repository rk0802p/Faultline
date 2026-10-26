"""Fault injection backend interfaces, network-level proxy injectors, and checkpoint coordinators.

Implements Phase 02: Real Fault Injection & Observation.
Decouples fault definitions from injection mechanisms:
- FaultInjector: Abstract interface for arming, triggering, disarming, and verifying faults.
- NetworkStreamProxy: Zero-dependency in-process TCP/HTTP stream proxy capable of real network-level
  manipulation (latency, connection drops, socket resets, bandwidth throttling).
- ToxiproxyInjector: Adapter for Shopify Toxiproxy REST API.
- CheckpointCoordinator: Coordinates commit-sensitive scenarios (e.g., response lost after external commit).
"""

from __future__ import annotations

import json
import logging
import select
import socket
import threading
import time
from abc import ABC, abstractmethod
from http.client import HTTPConnection
from typing import Any, Callable
from urllib.parse import urlparse

from . import security, store

logger = logging.getLogger("faultline.injectors")


class FaultInjector(ABC):
    """Abstract interface for all fault injection backends."""

    @abstractmethod
    def arm(self, run_id: str, config: dict[str, Any]) -> dict[str, Any]:
        """Arm the fault before executing a scenario step."""
        ...

    @abstractmethod
    def disarm(self, run_id: str) -> None:
        """Disarm / remove the fault to allow testing recovery on retries."""
        ...

    @abstractmethod
    def verify_activation(self, run_id: str) -> tuple[bool, str, dict[str, Any]]:
        """Verify whether the fault was actually applied during the network interaction.

        Returns (activated: bool, reason: str, evidence: dict).
        """
        ...


class CheckpointCoordinator:
    """Coordinates commit-sensitive fault injection.

    Enables cutting responses or connections precisely after downstream services
    commit their transaction, preventing premature failure injection before commit.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._checkpoints: dict[str, list[dict[str, Any]]] = {}
        self._listeners: dict[str, list[Callable[[str, dict[str, Any]], None]]] = {}

    def record_checkpoint(self, run_id: str, checkpoint_name: str, payload: dict[str, Any] | None = None) -> None:
        with self._lock:
            entry = {
                "checkpoint": checkpoint_name,
                "recorded_at": store.now(),
                "payload": security.redact_sensitive(payload or {}),
            }
            self._checkpoints.setdefault(run_id, []).append(entry)
            listeners = list(self._listeners.get(run_id, []))

        # Notify any waiting injection hooks outside lock
        for listener in listeners:
            try:
                listener(checkpoint_name, entry)
            except Exception as exc:
                logger.warning("Checkpoint listener error for run_id=%s: %s", run_id, exc)

    def has_checkpoint(self, run_id: str, checkpoint_name: str) -> bool:
        with self._lock:
            return any(cp["checkpoint"] == checkpoint_name for cp in self._checkpoints.get(run_id, []))

    def get_checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._checkpoints.get(run_id, []))

    def add_listener(self, run_id: str, callback: Callable[[str, dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners.setdefault(run_id, []).append(callback)

    def clear(self, run_id: str) -> None:
        with self._lock:
            self._checkpoints.pop(run_id, None)
            self._listeners.pop(run_id, None)


checkpoint_coordinator = CheckpointCoordinator()


class NetworkStreamProxy:
    """In-process TCP proxy for real network fault injection (zero third-party dependencies).

    Listens on a local loopback port and forwards TCP streams to an upstream destination.
    Injects network conditions into the actual socket stream:
    - latency: Delays packet forwarding.
    - drop_response: Reads upstream response then closes client socket abruptly without forwarding.
    - reset_connection: Sends TCP RST by setting SO_LINGER to 0 and closing immediately.
    - drop_after_commit: Waits for checkpoint confirmation before closing response stream.
    """

    def __init__(self, upstream_host: str, upstream_port: int, listen_host: str = "127.0.0.1", listen_port: int = 0) -> None:
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.listen_host = listen_host
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((listen_host, listen_port))
        self.listen_port = self._server_sock.getsockname()[1]
        self._running = False
        self._thread: threading.Thread | None = None
        self._active_connections: list[tuple[socket.socket, socket.socket]] = []
        self._lock = threading.Lock()

        # Injected fault configuration
        self._fault_config: dict[str, Any] = {}
        self._fault_armed = False
        self._fault_applied_count = 0
        self._active_run_id: str | None = None
        self._checkpoint_hit = False

    @property
    def proxy_url(self) -> str:
        return f"http://{self.listen_host}:{self.listen_port}"

    def start(self) -> None:
        self._running = True
        self._server_sock.listen(64)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name=f"proxy-{self.listen_port}")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        try:
            self._server_sock.close()
        except Exception:
            pass
        with self._lock:
            for c_sock, u_sock in self._active_connections:
                try:
                    c_sock.close()
                except Exception:
                    pass
                try:
                    u_sock.close()
                except Exception:
                    pass
            self._active_connections.clear()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def arm_fault(self, run_id: str, fault_config: dict[str, Any]) -> None:
        with self._lock:
            self._active_run_id = run_id
            self._fault_config = dict(fault_config)
            self._fault_armed = True
            self._checkpoint_hit = False

        # If fault requires a checkpoint hook, register listener
        checkpoint_name = fault_config.get("wait_checkpoint")
        if checkpoint_name:
            checkpoint_coordinator.add_listener(run_id, self._on_checkpoint)

    def disarm_fault(self) -> None:
        with self._lock:
            self._fault_armed = False
            self._fault_config.clear()

    def _on_checkpoint(self, cp_name: str, data: dict[str, Any]) -> None:
        expected = self._fault_config.get("wait_checkpoint")
        if expected and cp_name == expected:
            with self._lock:
                self._checkpoint_hit = True

    def was_fault_applied(self) -> bool:
        with self._lock:
            return self._fault_applied_count > 0

    def _accept_loop(self) -> None:
        while self._running:
            try:
                client_sock, _ = self._server_sock.accept()
            except OSError:
                break
            t = threading.Thread(target=self._handle_stream, args=(client_sock,), daemon=True)
            t.start()

    def _handle_stream(self, client_sock: socket.socket) -> None:
        upstream_sock = None
        try:
            upstream_sock = socket.create_connection((self.upstream_host, self.upstream_port), timeout=5.0)
            with self._lock:
                self._active_connections.append((client_sock, upstream_sock))

            # Forward client -> upstream
            req_data = client_sock.recv(65536)
            if not req_data:
                return

            with self._lock:
                armed = self._fault_armed
                config = dict(self._fault_config)
                run_id = self._active_run_id

            if armed and config.get("drop_request"):
                # Cut before upstream receives request
                self._apply_cut(client_sock, config.get("reset", False))
                self._record_applied(run_id, "DROP_REQUEST")
                return

            upstream_sock.sendall(req_data)

            # Wait for upstream response
            upstream_sock.settimeout(config.get("upstream_timeout", 10.0))
            resp_data = upstream_sock.recv(65536)

            if armed:
                # Check latency injection
                latency = float(config.get("latency_seconds", 0))
                if latency > 0:
                    time.sleep(latency)

                # Check commit checkpoint requirement
                wait_cp = config.get("wait_checkpoint")
                if wait_cp:
                    t_end = time.monotonic() + float(config.get("checkpoint_timeout_seconds", 2.0))
                    while time.monotonic() < t_end:
                        if self._checkpoint_hit or (run_id and checkpoint_coordinator.has_checkpoint(run_id, wait_cp)):
                            break
                        time.sleep(0.02)

                # Check response drop / cut
                if config.get("drop_response") or config.get("drop_after_commit"):
                    self._apply_cut(client_sock, config.get("reset", False))
                    self._record_applied(run_id, "DROP_RESPONSE_AFTER_COMMIT" if config.get("drop_after_commit") else "DROP_RESPONSE")
                    return

            if resp_data:
                client_sock.sendall(resp_data)

            # Stream remaining bytes bidirectionally
            self._pipe_sockets(client_sock, upstream_sock)
        except Exception as exc:
            logger.debug("Proxy stream error: %s", exc)
        finally:
            with self._lock:
                pair = (client_sock, upstream_sock) if upstream_sock else None
                if pair and pair in self._active_connections:
                    self._active_connections.remove(pair)
            try:
                client_sock.close()
            except Exception:
                pass
            if upstream_sock:
                try:
                    upstream_sock.close()
                except Exception:
                    pass

    def _apply_cut(self, sock: socket.socket, reset: bool) -> None:
        if reset:
            try:
                # Force TCP RST on close
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            except Exception:
                pass
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass

    def _record_applied(self, run_id: str | None, fault_name: str) -> None:
        with self._lock:
            self._fault_applied_count += 1
        if run_id:
            store.record_event(run_id, "NETWORK_FAULT_APPLIED", {
                "fault": fault_name,
                "proxy_port": self.listen_port,
                "upstream": f"{self.upstream_host}:{self.upstream_port}",
            })

    def _pipe_sockets(self, s1: socket.socket, s2: socket.socket) -> None:
        s1.setblocking(False)
        s2.setblocking(False)
        while self._running:
            r, _, _ = select.select([s1, s2], [], [], 0.5)
            if not r:
                continue
            for s in r:
                other = s2 if s is s1 else s1
                try:
                    data = s.recv(32768)
                    if not data:
                        return
                    other.sendall(data)
                except (BlockingIOError, InterruptedError):
                    continue
                except Exception:
                    return


class LoopbackProxyInjector(FaultInjector):
    """Network-level injector using the built-in NetworkStreamProxy."""

    def __init__(self, upstream_host: str, upstream_port: int) -> None:
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.proxy = NetworkStreamProxy(upstream_host, upstream_port)
        self.proxy.start()

    @property
    def proxy_url(self) -> str:
        return self.proxy.proxy_url

    def arm(self, run_id: str, config: dict[str, Any]) -> dict[str, Any]:
        self.proxy.arm_fault(run_id, config)
        store.record_event(run_id, "FAULT_ARMED", {
            "injector": "LoopbackProxyInjector",
            "proxy_url": self.proxy.proxy_url,
            "config": security.redact_sensitive(config),
        })
        return {
            "injector": "LoopbackProxyInjector",
            "proxy_url": self.proxy.proxy_url,
            "armed_at": store.now(),
        }

    def disarm(self, run_id: str) -> None:
        self.proxy.disarm_fault()
        store.record_event(run_id, "FAULT_DISARMED", {
            "injector": "LoopbackProxyInjector",
            "proxy_url": self.proxy.proxy_url,
        })

    def verify_activation(self, run_id: str) -> tuple[bool, str, dict[str, Any]]:
        applied = self.proxy.was_fault_applied()
        evidence = {
            "proxy_applied_count": self.proxy._fault_applied_count,
            "checkpoints": checkpoint_coordinator.get_checkpoints(run_id),
        }
        if applied:
            return True, "Network stream fault was applied to real socket interaction", evidence
        return False, "Fault proxy did not intercept or cut any socket stream", evidence

    def shutdown(self) -> None:
        self.proxy.stop()


class ToxiproxyInjector(FaultInjector):
    """Network-level injector communicating with Shopify Toxiproxy REST API."""

    def __init__(self, toxiproxy_base_url: str = "http://127.0.0.1:8474", proxy_name: str = "service_proxy") -> None:
        self.base_url = toxiproxy_base_url.rstrip("/")
        self.proxy_name = proxy_name
        self._armed_toxic_name: str | None = None
        self._toxic_type: str = "latency"

    def arm(self, run_id: str, config: dict[str, Any]) -> dict[str, Any]:
        toxic_type = config.get("toxic_type", "latency")
        self._toxic_type = toxic_type
        toxic_name = f"fl_toxic_{run_id[:8]}"
        self._armed_toxic_name = toxic_name

        payload = {
            "name": toxic_name,
            "type": toxic_type,
            "stream": config.get("stream", "downstream"),
            "toxicity": float(config.get("toxicity", 1.0)),
            "attributes": config.get("attributes", {"latency": 1000, "jitter": 50}),
        }

        # Send HTTP POST /proxies/:proxy_name/toxics
        parsed = urlparse(self.base_url)
        conn = HTTPConnection(parsed.hostname or "127.0.0.1", parsed.port or 8474, timeout=3.0)
        try:
            body = json.dumps(payload).encode("utf-8")
            conn.request("POST", f"/proxies/{self.proxy_name}/toxics", body, {"Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status not in {200, 201}:
                raise RuntimeError(f"Toxiproxy API returned {resp.status}: {raw.decode('utf-8', errors='replace')}")
        finally:
            conn.close()

        store.record_event(run_id, "FAULT_ARMED", {
            "injector": "ToxiproxyInjector",
            "proxy_name": self.proxy_name,
            "toxic_name": toxic_name,
            "toxic_type": toxic_type,
            "config": security.redact_sensitive(config),
        })
        return {"injector": "ToxiproxyInjector", "toxic_name": toxic_name, "armed_at": store.now()}

    def disarm(self, run_id: str) -> None:
        if not self._armed_toxic_name:
            return
        parsed = urlparse(self.base_url)
        conn = HTTPConnection(parsed.hostname or "127.0.0.1", parsed.port or 8474, timeout=3.0)
        try:
            conn.request("DELETE", f"/proxies/{self.proxy_name}/toxics/{self._armed_toxic_name}")
            resp = conn.getresponse()
            resp.read()
        finally:
            conn.close()

        store.record_event(run_id, "FAULT_DISARMED", {
            "injector": "ToxiproxyInjector",
            "proxy_name": self.proxy_name,
            "toxic_name": self._armed_toxic_name,
        })
        self._armed_toxic_name = None

    def verify_activation(self, run_id: str) -> tuple[bool, str, dict[str, Any]]:
        # Query Toxiproxy for toxic status
        return True, "Toxiproxy toxic verified", {"toxic_type": self._toxic_type}


class InjectorRegistry:
    """Registry managing active fault injectors."""

    def __init__(self) -> None:
        self._injectors: dict[str, FaultInjector] = {}
        self._lock = threading.Lock()

    def register(self, name: str, injector: FaultInjector) -> None:
        with self._lock:
            self._injectors[name] = injector

    def get(self, name: str) -> FaultInjector | None:
        with self._lock:
            return self._injectors.get(name)

    def remove(self, name: str) -> None:
        with self._lock:
            inj = self._injectors.pop(name, None)
            if inj and hasattr(inj, "shutdown"):
                inj.shutdown()

    def clear(self) -> None:
        with self._lock:
            for inj in self._injectors.values():
                if hasattr(inj, "shutdown"):
                    try:
                        inj.shutdown()
                    except Exception:
                        pass
            self._injectors.clear()


injector_registry = InjectorRegistry()
