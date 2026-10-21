"""Security controls: loopback allowlist enforcement and best-effort sensitive data redaction.

Implements Spec Section 16 & Section 6.1:
- Best-effort secret redaction for credentials, PAN/card numbers, tokens, and authorization headers.
- Loopback destination allowlist to strictly prevent unintended external network requests.
"""

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


def is_loopback_allowed(target: str) -> bool:
    """Verifies that a target URL or host string resolves strictly to loopback or allowed container hosts."""
    if not target or not isinstance(target, str):
        return False
    target = target.strip()
    if not target:
        return False
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
    else:
        # Check if IPv6 literal like [::1]:8000 or [::1]
        if target.startswith("["):
            bracket_end = target.find("]")
            if bracket_end == -1:
                return False
            hostname = target[1:bracket_end].lower()
            remainder = target[bracket_end + 1:]
            if remainder and not remainder.startswith(":"):
                return False
        else:
            # IPv4 or hostname like 127.0.0.1:8000 or localhost
            # If target has multiple colons without brackets (e.g. raw ::1), handle it
            if target.count(":") > 1:
                hostname = target.lower()
            else:
                hostname = target.split(":")[0].strip().lower()

    if not hostname or hostname == "0.0.0.0":
        return False

    return (hostname in ALLOWED_LOOPBACK_HOSTS) or (hostname in CONTAINER_ALLOWED_HOSTS)


def assert_loopback_safe(target: str) -> None:
    """Raises PermissionError if target is not strictly restricted to loopback."""
    if not is_loopback_allowed(target):
        raise PermissionError(
            f"Destination '{target}' is rejected. Faultline restricts all network targets to loopback (Spec §16)."
        )
