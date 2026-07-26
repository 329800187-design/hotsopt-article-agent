from __future__ import annotations

import base64
import binascii
import json
import math
import re
from datetime import datetime
from typing import Any


SCHEMA_VERSION = 1
PRODUCT_ID = "hotspot-article-agent"
SIGNATURE_ALGORITHM = "Ed25519"
DEVICE_CODE_RE = re.compile(r"^[A-Z2-7]{4}(-[A-Z2-7]{4}){4}$")


class LicenseValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _reject_non_finite(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        raise LicenseValidationError("INVALID_LICENSE", "license contains a non-finite number")
    if isinstance(value, dict):
        return {key: _reject_non_finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_reject_non_finite(item) for item in value]
    return value


def canonical_payload(license_data: dict[str, Any]) -> bytes:
    payload = {key: value for key, value in license_data.items() if key != "signature"}
    _reject_non_finite(payload)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise LicenseValidationError("INVALID_LICENSE", f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise LicenseValidationError("INVALID_LICENSE", f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise LicenseValidationError("INVALID_LICENSE", f"{field} must include a timezone")
    return parsed


def validate_license_structure(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LicenseValidationError("INVALID_LICENSE", "license must be an object")
    data = dict(value)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise LicenseValidationError("UNKNOWN_SCHEMA_VERSION", "license schema version is unsupported")
    required = ("license_id", "product", "edition", "customer_name", "device_code", "issued_at", "not_before", "expires_at", "features", "signature_algorithm", "signature")
    if any(field not in data for field in required):
        raise LicenseValidationError("INVALID_LICENSE", "license is missing required fields")
    if data.get("product") != PRODUCT_ID:
        raise LicenseValidationError("PRODUCT_MISMATCH", "license product does not match")
    if not isinstance(data.get("customer_name"), str) or not data["customer_name"].strip():
        raise LicenseValidationError("INVALID_LICENSE", "customer name is required")
    if not isinstance(data.get("license_id"), str) or not data["license_id"].strip():
        raise LicenseValidationError("INVALID_LICENSE", "license id is required")
    if not isinstance(data.get("device_code"), str) or not DEVICE_CODE_RE.fullmatch(data["device_code"]):
        raise LicenseValidationError("INVALID_LICENSE", "device code is invalid")
    if data.get("signature_algorithm") != SIGNATURE_ALGORITHM:
        raise LicenseValidationError("INVALID_LICENSE", "signature algorithm is invalid")
    if not isinstance(data.get("features"), list) or not data["features"] or not all(isinstance(item, str) and item for item in data["features"]):
        raise LicenseValidationError("INVALID_LICENSE", "features are invalid")
    not_before = _parse_time(data.get("not_before"), "not_before")
    expires_at = _parse_time(data.get("expires_at"), "expires_at")
    _parse_time(data.get("issued_at"), "issued_at")
    if expires_at <= not_before:
        raise LicenseValidationError("INVALID_LICENSE", "expires_at must be after not_before")
    signature = data.get("signature")
    if not isinstance(signature, str) or not signature:
        raise LicenseValidationError("INVALID_LICENSE", "signature is missing")
    try:
        base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    except (ValueError, binascii.Error) as exc:
        raise LicenseValidationError("INVALID_LICENSE", "signature encoding is invalid") from exc
    return data
