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
        raise LicenseValidationError("INVALID_LICENSE", "许可证包含无效数字")
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
        raise LicenseValidationError("INVALID_LICENSE", f"{field} 无效")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise LicenseValidationError("INVALID_LICENSE", f"{field} 无效") from exc
    if parsed.tzinfo is None:
        raise LicenseValidationError("INVALID_LICENSE", f"{field} 必须包含时区")
    return parsed


def validate_license_structure(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LicenseValidationError("INVALID_LICENSE", "许可证格式无效")
    data = dict(value)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise LicenseValidationError("UNKNOWN_SCHEMA_VERSION", "许可证版本不支持")
    required = ("license_id", "product", "edition", "customer_name", "device_code", "issued_at", "not_before", "expires_at", "features", "signature_algorithm", "signature")
    if any(field not in data for field in required):
        raise LicenseValidationError("INVALID_LICENSE", "许可证缺少必要字段")
    if data.get("product") != PRODUCT_ID:
        raise LicenseValidationError("PRODUCT_MISMATCH", "许可证产品不匹配")
    if not isinstance(data.get("customer_name"), str) or not data["customer_name"].strip():
        raise LicenseValidationError("INVALID_LICENSE", "客户名称不能为空")
    if not isinstance(data.get("license_id"), str) or not data["license_id"].strip():
        raise LicenseValidationError("INVALID_LICENSE", "许可证编号不能为空")
    if not isinstance(data.get("device_code"), str) or not DEVICE_CODE_RE.fullmatch(data["device_code"]):
        raise LicenseValidationError("INVALID_LICENSE", "设备申请码无效")
    if data.get("signature_algorithm") != SIGNATURE_ALGORITHM:
        raise LicenseValidationError("INVALID_LICENSE", "签名算法无效")
    if not isinstance(data.get("features"), list) or not data["features"] or not all(isinstance(item, str) and item for item in data["features"]):
        raise LicenseValidationError("INVALID_LICENSE", "授权功能无效")
    not_before = _parse_time(data.get("not_before"), "生效时间")
    expires_at = _parse_time(data.get("expires_at"), "到期时间")
    _parse_time(data.get("issued_at"), "签发时间")
    if expires_at <= not_before:
        raise LicenseValidationError("INVALID_LICENSE", "到期时间必须晚于生效时间")
    signature = data.get("signature")
    if not isinstance(signature, str) or not signature:
        raise LicenseValidationError("INVALID_LICENSE", "缺少签名")
    try:
        base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    except (ValueError, binascii.Error) as exc:
        raise LicenseValidationError("INVALID_LICENSE", "签名编码无效") from exc
    return data
