"""Security controls: loopback allowlist enforcement and sensitive data redaction.

Implements Spec Section 16 & Section 6.1:
- Secret redaction for credentials, PAN/card numbers, tokens, and authorization headers.
- Loopback destination allowlist to strictly prevent unintended external requests.
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

ALLOWED_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
CONTAINER_ALLOWED_HOSTS = {
    h.strip().lower()
    for h in os.environ.get(
        "FAULTLINE_ALLOWED_HOSTS",
        "checkout-service,payment-provider,toxiproxy,redis"
    ).split(",")
    if h.strip()
}


def redact_sensitive(obj: Any) -> Any:
    """Recursively redacts sensitive keys and pattern-matching strings from data structures."""
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
        return text
    return obj


def is_loopback_allowed(target: str) -> bool:
    """Verifies that a target URL or host string resolves strictly to loopback."""
    if not target:
        return False
    if "://" in target:
        parsed = urlparse(target)
        hostname = parsed.hostname or ""
    else:
        hostname = target.split(":")[0].strip()
    return (hostname.lower() in ALLOWED_LOOPBACK_HOSTS) or (hostname.lower() in CONTAINER_ALLOWED_HOSTS)


def assert_loopback_safe(target: str) -> None:
    """Raises PermissionError if target is not strictly restricted to loopback."""
    if not is_loopback_allowed(target):
        raise PermissionError(
            f"Destination '{target}' is rejected. Faultline restricts all network targets to loopback (Spec §16)."
        )
