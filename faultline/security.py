"""Security controls: loopback allowlist enforcement and best-effort sensitive data redaction.

Implements Spec Section 16 & Section 6.1:
- Best-effort secret redaction for credentials, PAN/card numbers, tokens, and authorization headers.
- Loopback destination allowlist to strictly prevent unintended external network requests.
"""

import ipaddress
import os
import re
from typing import Any
from urllib.parse import urlparse


REDACTION_KEY_PATTERNS = {
    "password", "secret", "token", "authorization", "api_key", "apikey",
    "card_number", "pan", "cvv", "cvc", "credentials", "access_token",
    "refresh_token", "private_key",
}

PAN_PATTERN = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
BEARER_PATTERN = re.compile(r"Bearer\s+([A-Za-z0-9_\-\.]+)", re.IGNORECASE)
API_KEY_PATTERN = re.compile(r"\b(?:sk|pk)_(?:live|test)_[0-9a-zA-Z]{14,}\b")
PASSWORD_KV_PATTERN = re.compile(r"(?i)\b(password|secret|api_key|token)[\s:=]+([^\s,;\"'<>]+)")

# Strictly restricted loopback hosts (0.0.0.0 excluded as it is a wildcard bind address)
ALLOWED_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
CONTAINER_ALLOWED_HOSTS = {
    h.strip().lower()
    for h in os.environ.get(
        "FAULTLINE_ALLOWED_HOSTS",
        "checkout-service,payment-provider,toxiproxy,redis"
    ).split(",")
    if h.strip()
}

# Per-service port restrictions for configured internal containers
CONTAINER_SERVICE_PORTS: dict[str, set[int]] = {
    "checkout-service": {8766},
    "payment-provider": {8767},
    "toxiproxy": {8474, 8475, 8476, 8477, 8478, 8479, 8480},
    "redis": {6379},
}

# Dangerous / privileged ports prohibited from automated test targeting
PROHIBITED_PORTS: set[int] = {21, 22, 23, 25, 53, 110, 135, 137, 138, 139, 445}


def redact_sensitive(obj: Any) -> Any:
    """Best-effort recursive redaction for common sensitive keys and pattern-matching strings."""
    if isinstance(obj, dict):
        sanitized = {}
        for key, value in obj.items():
            key_str = str(key).lower()
            if any(term in key_str for term in REDACTION_KEY_PATTERNS):
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = redact_sensitive(value)
        return sanitized
    elif isinstance(obj, list):
        return [redact_sensitive(item) for item in obj]
    elif isinstance(obj, str):
        text = BEARER_PATTERN.sub("Bearer [REDACTED]", obj)
        text = API_KEY_PATTERN.sub("[REDACTED_KEY]", text)
        text = PAN_PATTERN.sub("[REDACTED_PAN]", text)
        text = PASSWORD_KV_PATTERN.sub(r"\1=[REDACTED]", text)
        return text
    return obj


def is_loopback_allowed(target: str, port: int | None = None) -> bool:
    """Verifies that a target URL or host string resolves strictly to loopback or allowed container hosts."""
    if not target or not isinstance(target, str):
        return False
    target = target.strip()
    if not target:
        return False

    parsed_port: int | None = None
    if "://" in target:
        try:
            parsed = urlparse(target)
        except Exception:
            return False
        # Only permit standard HTTP/HTTPS schemes
        if parsed.scheme.lower() not in {"http", "https"}:
            return False
        # Reject embedded credentials (e.g. http://user:pass@127.0.0.1)
        if parsed.username or parsed.password:
            return False
        hostname = (parsed.hostname or "").lower()
        try:
            if parsed.port is not None:
                parsed_port = parsed.port
            elif parsed.scheme.lower() == "http":
                parsed_port = 80
            elif parsed.scheme.lower() == "https":
                parsed_port = 443
        except (ValueError, TypeError):
            # Raised by urlparse when port is invalid or out of range (e.g. 99999)
            return False
    else:
        # Check if IPv6 literal like [::1]:8000 or [::1]
        if target.startswith("["):
            bracket_end = target.find("]")
            if bracket_end == -1:
                return False
            hostname = target[1:bracket_end].lower()
            remainder = target[bracket_end + 1:]
            if remainder:
                if not remainder.startswith(":"):
                    return False
                try:
                    parsed_port = int(remainder[1:])
                except ValueError:
                    return False
            else:
                parsed_port = None
        else:
            if target.count(":") == 1:
                parts = target.split(":")
                hostname = parts[0].strip().lower()
                try:
                    parsed_port = int(parts[1])
                except ValueError:
                    return False
            elif target.count(":") > 1:
                # Raw IPv6 literal without brackets
                hostname = target.strip().lower()
                parsed_port = None
            else:
                hostname = target.strip().lower()
                parsed_port = None

    # Determine effective port (explicit port parameter or parsed port)
    if parsed_port is not None and port is not None and parsed_port != port:
        return False
    effective_port = parsed_port if parsed_port is not None else port

    # Validate port bounds if a port was specified
    if effective_port is not None:
        try:
            effective_port = int(effective_port)
        except (ValueError, TypeError):
            return False
        if not (1 <= effective_port <= 65535):
            return False

    if not hostname or hostname == "0.0.0.0":
        return False

    # Enforce strict prohibited port restrictions for all loopback destinations
    if effective_port is not None and effective_port in PROHIBITED_PORTS:
        return False

    # Check IP literals with ipaddress module to enforce true loopback
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_loopback
    except ValueError:
        # Not an IP address literal, validate hostname
        if hostname == "localhost":
            return True
        if hostname in CONTAINER_ALLOWED_HOSTS:
            if hostname in CONTAINER_SERVICE_PORTS:
                if effective_port is None or effective_port not in CONTAINER_SERVICE_PORTS[hostname]:
                    return False
            return True
        return False


def assert_loopback_safe(target: str, port: int | None = None) -> None:
    """Raises PermissionError if target is not strictly restricted to loopback."""
    if not is_loopback_allowed(target, port=port):
        dest_repr = f"{target}:{port}" if port is not None and ":" not in target else target
        raise PermissionError(
            f"Destination '{dest_repr}' is rejected. Faultline restricts all network targets to loopback (Spec §16)."
        )


def validate_redirect(current_url: str, redirect_target: str) -> str:
    """Validates that an HTTP redirect stays strictly within approved loopback boundaries."""
    from urllib.parse import urljoin
    target_url = urljoin(current_url, redirect_target)
    assert_loopback_safe(target_url)
    return target_url
