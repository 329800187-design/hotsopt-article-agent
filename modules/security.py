from __future__ import annotations

import dataclasses
import json
import re
from typing import Any


SENSITIVE_KEY_NAMES = {
    "cookie",
    "set-cookie",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "proxy-authorization",
    "api_key",
    "api-key",
    "x-api-key",
    "secret",
    "client_secret",
    "password",
}
_SENSITIVE_KEY_PATTERN = re.compile(r"(?:cookie|set[-_]cookie|access[-_]token|refresh[-_]token|proxy[-_]authorization|authorization|api[-_]key|client[-_]secret|token|secret|password)", re.I)
_URL_CREDENTIAL_PATTERN = re.compile(r"(https?://)([^/@\s:]+):([^/@\s]+)@", re.I)
_AUTH_VALUE_PATTERN = re.compile(
    r"(\b(?:authorization|proxy-authorization|cookie|set-cookie|api[-_]key|token|access[-_]token|refresh[-_]token|secret|client[-_]secret|password)\b\s*[:=]\s*)(?:bearer\s+|basic\s+)?[^,;\s]+",
    re.I,
)
_BEARER_PATTERN = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I)
_API_KEY_VALUE_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")


def _normalized_key(key: Any) -> str:
    return str(key or "").strip().lower().replace("-", "_")


def is_sensitive_key(key: Any) -> bool:
    normalized = _normalized_key(key)
    return normalized in {name.replace("-", "_") for name in SENSITIVE_KEY_NAMES} or bool(_SENSITIVE_KEY_PATTERN.search(normalized))


def sanitize_sensitive_data(value: Any) -> Any:
    """Remove sensitive fields and redact sensitive text recursively."""
    if isinstance(value, dict):
        return {key: sanitize_sensitive_data(item) for key, item in value.items() if not is_sensitive_key(key)}
    if isinstance(value, list):
        return [sanitize_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_sensitive_data(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if dataclasses.is_dataclass(value):
        return sanitize_sensitive_data(dataclasses.asdict(value))
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return sanitize_sensitive_data(vars(value))
    return value


def redact_sensitive_text(value: Any) -> str:
    """Redact credentials and common secret-bearing text before logging or persistence."""
    text = str(value or "")
    text = _URL_CREDENTIAL_PATTERN.sub(r"\1***:***@", text)
    text = _BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    text = _API_KEY_VALUE_PATTERN.sub("[REDACTED]", text)
    text = _AUTH_VALUE_PATTERN.sub(r"\1[REDACTED]", text)
    return text


def sanitize_json(value: Any) -> Any:
    """Return a JSON-safe, recursively sanitized value for metadata snapshots."""
    sanitized = sanitize_sensitive_data(value)
    try:
        json.dumps(sanitized, ensure_ascii=False)
        return sanitized
    except (TypeError, ValueError):
        return redact_sensitive_text(sanitized)
