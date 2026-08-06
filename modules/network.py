from __future__ import annotations

import re
from typing import Any

import httpx
from modules.security import redact_sensitive_text


NETWORK_KEYS = {"mode", "timeout_seconds", "verify_ssl", "http_proxy", "https_proxy"}


def resolve_network_settings(defaults: dict[str, Any] | None = None, override: dict[str, Any] | None = None) -> dict[str, Any]:
    result = dict(defaults or {})
    profile = dict(override or {})
    nested = profile.get("network") if isinstance(profile.get("network"), dict) else {}
    result.update(nested)
    result.update({key: profile[key] for key in NETWORK_KEYS if key in profile})
    return result


def create_http_client(network_settings: dict[str, Any] | None = None) -> httpx.Client:
    settings = network_settings or {}
    mode = str(settings.get("mode") or "system").lower()
    timeout = float(settings.get("timeout_seconds") or 15)
    verify_ssl = bool(settings.get("verify_ssl", True))
    if mode == "direct":
        return httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False, verify=verify_ssl)
    if mode == "custom":
        proxy = str(settings.get("https_proxy") or settings.get("http_proxy") or "").strip() or None
        return httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False, verify=verify_ssl, proxy=proxy)
    return httpx.Client(timeout=timeout, follow_redirects=True, trust_env=True, verify=verify_ssl)


def sanitize_proxy_url(value: str) -> str:
    return re.sub(r"(https?://)([^/@:]+):([^/@]+)@", r"\1***:***@", str(value or ""))


def classify_network_error(error: Exception) -> dict[str, Any]:
    message = str(error)
    lowered = message.lower()
    if isinstance(error, httpx.TimeoutException):
        category = "timeout"
    elif "dns" in lowered or "getaddrinfo" in lowered or "name or service not known" in lowered:
        category = "dns"
    elif "ssl" in lowered or "certificate" in lowered or "tls" in lowered:
        category = "tls"
    elif "proxy" in lowered or "socks" in lowered:
        category = "proxy"
    elif isinstance(error, httpx.HTTPStatusError):
        category = "http_status"
    elif isinstance(error, (ValueError, TypeError, KeyError)):
        category = "data_format"
    else:
        category = "network"
    return {"category": category, "message": redact_sensitive_text(sanitize_proxy_url(message)), "retryable": category not in {"data_format"}}
