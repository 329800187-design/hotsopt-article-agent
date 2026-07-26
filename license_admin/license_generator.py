from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization

from license_admin.license_schema import canonical_payload, validate_license_structure
from license_admin.signing_identity import SigningIdentityError, load_signing_private_key


def private_key_path() -> Path:
    from license_admin.signing_identity import private_key_path as configured_path

    return configured_path()


def _load_private_key(path: Path | None = None):
    return load_signing_private_key(path)


def normalize_device_code(value: str) -> str:
    compact = re.sub(r"[^A-Z2-7]", "", str(value or "").upper())
    if len(compact) != 20:
        raise ValueError("device code must contain 20 Base32 characters")
    return "-".join(compact[index : index + 4] for index in range(0, 20, 4))


def create_license(*, customer_name: str, device_code: str, license_id: str, not_before: str, expires_at: str, edition: str = "standard", features: list[str] | None = None, private_key: Path | None = None) -> dict[str, Any]:
    if not customer_name.strip():
        raise ValueError("customer name is required")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "license_id": license_id.strip(),
        "product": "hotspot-article-agent",
        "edition": edition,
        "customer_name": customer_name.strip(),
        "device_code": normalize_device_code(device_code),
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "not_before": not_before,
        "expires_at": expires_at,
        "features": features or ["hot_topics", "custom_topic", "five_articles", "image_generation", "article_editing", "word_export", "zip_export"],
        "signature_algorithm": "Ed25519",
    }
    private = _load_private_key(private_key)
    signature = private.sign(canonical_payload(payload))
    payload["signature"] = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    validate_license_structure(payload)
    return payload


def write_license(value: dict[str, Any], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return output
