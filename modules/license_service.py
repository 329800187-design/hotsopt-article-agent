from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from modules.credential_store import load_secret, save_secret
from modules.device_identity import DeviceIdentityUnavailable, InstallationIdentityError, device_code, license_root
from modules.license_schema import LicenseValidationError, canonical_payload, validate_license_structure


logger = logging.getLogger(__name__)
PUBLIC_KEY_PATH = Path(__file__).resolve().parents[1] / "resources" / "license_public_key.pem"
ACTIVE_LICENSE_PATH = license_root() / "active.license"
STATE_PATH = license_root() / "license_state.json"
STATE_SECRET_NAME = "license_last_seen_utc"
CLOCK_ROLLBACK_TOLERANCE = timedelta(minutes=5)
NOT_BEFORE_CLOCK_SKEW = timedelta(minutes=5)
RECOVERY_MIN_INTERVAL = timedelta(seconds=5)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_public_key() -> Ed25519PublicKey:
    if not PUBLIC_KEY_PATH.is_file():
        raise LicenseValidationError("INVALID_LICENSE", "public key is unavailable")
    try:
        key = serialization.load_pem_public_key(PUBLIC_KEY_PATH.read_bytes())
    except Exception as exc:
        raise LicenseValidationError("INVALID_LICENSE", "public key is invalid") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise LicenseValidationError("INVALID_LICENSE", "public key algorithm is invalid")
    return key


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LicenseValidationError("LICENSE_FILE_CORRUPTED", "license file is corrupted") from exc


def _decode_signature(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def validate_license(value: object, expected_device: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    data = validate_license_structure(value)
    try:
        _read_public_key().verify(_decode_signature(str(data["signature"])), canonical_payload(data))
    except InvalidSignature as exc:
        raise LicenseValidationError("SIGNATURE_INVALID", "license signature verification failed") from exc
    try:
        expected_device = expected_device or device_code()
    except DeviceIdentityUnavailable as exc:
        raise LicenseValidationError("DEVICE_IDENTITY_UNAVAILABLE", "device identity is unavailable") from exc
    if data["device_code"] != expected_device:
        raise LicenseValidationError("DEVICE_MISMATCH", "license does not belong to this device")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    not_before = datetime.fromisoformat(data["not_before"]).astimezone(timezone.utc)
    expires_at = datetime.fromisoformat(data["expires_at"]).astimezone(timezone.utc)
    if current < not_before - NOT_BEFORE_CLOCK_SKEW:
        raise LicenseValidationError("NOT_YET_VALID", "license is not active yet")
    if current > expires_at:
        raise LicenseValidationError("LICENSE_EXPIRED", "license has expired")
    return data


def _state() -> dict[str, Any]:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    if value.get("clock_status") not in {"normal", "suspected", "recovery_pending"}:
        value["clock_status"] = "suspected" if value.get("clock_rollback_suspected") else "normal"
    return value


def _state_secret_path() -> Path:
    return license_root() / "license_state.dat"


def _load_last_seen() -> str:
    try:
        return load_secret("dpapi:" + STATE_SECRET_NAME, path=_state_secret_path())
    except Exception:
        return ""


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _max_time(*values: object) -> datetime | None:
    parsed = [item for item in (_parse_time(value) for value in values) if item is not None]
    return max(parsed) if parsed else None


def _rollback_reference(current: dict[str, Any], previous: str) -> datetime | None:
    return _max_time(
        current.get("rollback_reference_utc"),
        previous,
        current.get("last_seen_utc"),
        current.get("trusted_time_utc"),
    )


def _save_clock_state(now: datetime, allow_recovery: bool = False) -> dict[str, Any]:
    current = _state()
    previous = _load_last_seen()
    status = str(current.get("clock_status") or "normal")
    if status not in {"normal", "suspected", "recovery_pending"}:
        status = "normal"
    previous_time = _parse_time(previous)
    reference = _rollback_reference(current, previous)
    if status == "normal" and reference and now < reference - CLOCK_ROLLBACK_TOLERANCE:
        status = "suspected"
        current["rollback_detected_at"] = now.isoformat()
        current["rollback_reference_utc"] = reference.isoformat()
        current.pop("recovery_started_at", None)
        current.pop("recovery_last_check_utc", None)
        current["recovery_check_count"] = 0
    elif status in {"suspected", "recovery_pending"}:
        reference = reference or now
        current["rollback_reference_utc"] = reference.isoformat()
        if now < reference - CLOCK_ROLLBACK_TOLERANCE:
            status = "suspected"
            current.pop("recovery_started_at", None)
            current.pop("recovery_last_check_utc", None)
            current["recovery_check_count"] = 0
        elif allow_recovery and status == "suspected":
            status = "recovery_pending"
            current.setdefault("recovery_started_at", now.isoformat())
            current["recovery_last_check_utc"] = now.isoformat()
            current["recovery_check_count"] = 1
        elif allow_recovery and status == "recovery_pending":
            last_check = _parse_time(current.get("recovery_last_check_utc"))
            count = int(current.get("recovery_check_count") or 0)
            if last_check and now >= last_check + RECOVERY_MIN_INTERVAL:
                current["recovery_last_check_utc"] = now.isoformat()
                current["recovery_check_count"] = count + 1
    latest = _max_time(now, previous_time, reference) or now
    try:
        save_secret(STATE_SECRET_NAME, latest.isoformat(), path=_state_secret_path())
    except Exception:
        logger.warning("license clock state persistence failed: %s", "DPAPI_ERROR")
    current["clock_status"] = status
    if status == "normal":
        current.pop("clock_rollback_suspected", None)
        for key in ("rollback_detected_at", "rollback_reference_utc", "recovery_started_at", "recovery_last_check_utc", "recovery_check_count"):
            current.pop(key, None)
    else:
        current["clock_rollback_suspected"] = True
        current["recovery_check_count"] = int(current.get("recovery_check_count") or 0)
    current["trusted_time_utc"] = latest.isoformat()
    current.pop("last_seen_utc", None)
    current["last_seen_ref"] = "dpapi:" + STATE_SECRET_NAME
    _atomic_write(STATE_PATH, (json.dumps(current, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    return current


def _clock_rollback_suspected() -> bool:
    return _state().get("clock_status") in {"suspected", "recovery_pending"} or bool(_state().get("clock_rollback_suspected"))


def clock_status() -> dict[str, Any]:
    state = _state()
    status = str(state.get("clock_status") or ("suspected" if state.get("clock_rollback_suspected") else "normal"))
    return {
        "clock_status": status,
        "recovery_check_count": int(state.get("recovery_check_count") or 0),
        "recovery_ready": status == "recovery_pending" and int(state.get("recovery_check_count") or 0) >= 2,
        "recovery_pending": status == "recovery_pending",
        "recoverable": status == "recovery_pending" and int(state.get("recovery_check_count") or 0) >= 2,
        "rollback_reference_utc": state.get("rollback_reference_utc"),
        "allowed_tolerance_seconds": int(CLOCK_ROLLBACK_TOLERANCE.total_seconds()),
    }


def check_system_time(now: datetime | None = None) -> dict[str, Any]:
    _save_clock_state(now or datetime.now(timezone.utc), allow_recovery=True)
    return clock_status()


def recover_clock_rollback(now: datetime | None = None) -> dict[str, Any]:
    current = _state()
    status = str(current.get("clock_status") or ("suspected" if current.get("clock_rollback_suspected") else "normal"))
    count = int(current.get("recovery_check_count") or 0)
    if status != "recovery_pending" or count < 2:
        code = "CLOCK_ROLLBACK_SUSPECTED" if status == "suspected" else "CLOCK_RECOVERY_PENDING"
        return {"recovered": False, "code": code, **clock_status()}
    current_time = now or datetime.now(timezone.utc)
    reference = _parse_time(current.get("rollback_reference_utc"))
    if reference is None or current_time < reference - CLOCK_ROLLBACK_TOLERANCE:
        return {
            "recovered": False,
            "code": "CLOCK_ROLLBACK_SUSPECTED",
            "message": "系统时间尚未恢复到可信时间范围。",
            **clock_status(),
        }
    last_check = _parse_time(current.get("recovery_last_check_utc"))
    if last_check is None or current_time < last_check:
        return {
            "recovered": False,
            "code": "CLOCK_RECOVERY_PENDING",
            "message": "系统时间恢复检查尚未完成。",
            **clock_status(),
        }
    try:
        validate_license(_load_json(ACTIVE_LICENSE_PATH), now=current_time)
    except LicenseValidationError as exc:
        return {"recovered": False, "code": exc.code, "message": _friendly_error(exc.code), **clock_status()}
    current["clock_status"] = "normal"
    current.pop("clock_rollback_suspected", None)
    for key in ("rollback_detected_at", "rollback_reference_utc", "recovery_started_at", "recovery_last_check_utc", "recovery_check_count"):
        current.pop(key, None)
    latest = _max_time(current_time, reference, _load_last_seen()) or current_time
    current["trusted_time_utc"] = latest.isoformat()
    current["last_seen_ref"] = "dpapi:" + STATE_SECRET_NAME
    try:
        save_secret(STATE_SECRET_NAME, latest.isoformat(), path=_state_secret_path())
    except Exception:
        logger.warning("license clock state persistence failed: %s", "DPAPI_ERROR")
    _atomic_write(STATE_PATH, (json.dumps(current, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    return {"recovered": True, **clock_status()}


def check_license(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    try:
        device_code()
    except InstallationIdentityError as exc:
        return {"valid": False, "code": exc.code, "message": _friendly_error(exc.code)}
    except DeviceIdentityUnavailable:
        return {"valid": False, "code": "DEVICE_IDENTITY_UNAVAILABLE", "message": _friendly_error("DEVICE_IDENTITY_UNAVAILABLE")}
    try:
        _save_clock_state(now)
    except OSError:
        pass
    if not ACTIVE_LICENSE_PATH.is_file():
        return {"valid": False, "code": "LICENSE_REQUIRED", "message": "当前授权不可用，已有内容仍可查看和导出。导入有效许可证后即可继续生成。"}
    try:
        raw_data = _load_json(ACTIVE_LICENSE_PATH)
        data = validate_license(raw_data, now=now)
    except LicenseValidationError as exc:
        if exc.code == "NOT_YET_VALID":
            try:
                not_before = datetime.fromisoformat(str(raw_data["not_before"])).astimezone(timezone.utc)
                remaining = max(0, int((not_before - now).total_seconds()))
                return {
                    "valid": False,
                    "code": exc.code,
                    "message": _friendly_error(exc.code),
                    "not_before": not_before.isoformat(),
                    "not_before_remaining_seconds": remaining,
                }
            except (UnboundLocalError, KeyError, TypeError, ValueError):
                pass
        return {"valid": False, "code": exc.code, "message": _friendly_error(exc.code)}
    status = clock_status()
    if status["clock_status"] == "suspected":
        return {"valid": False, "code": "CLOCK_ROLLBACK_SUSPECTED", "message": "检测到系统时间异常，请校准电脑时间后重新检查。"}
    if status["clock_status"] == "recovery_pending":
        return {"valid": False, "code": "CLOCK_RECOVERY_PENDING", "message": "系统时间正在恢复检查，请连续检查两次后确认恢复授权。"}
    return {"valid": True, "code": "LICENSE_VALID", "message": "授权有效", "license": _public_summary(data)}


def import_license(path: Path) -> dict[str, Any]:
    try:
        candidate = _load_json(path)
    except LicenseValidationError:
        raise
    return _persist_validated_license(validate_license(candidate))


def import_license_text(payload: str | bytes) -> dict[str, Any]:
    """Import a license copied from chat/email, including wrapped Base64 text."""
    raw = payload.decode("utf-8-sig", errors="replace") if isinstance(payload, bytes) else str(payload)
    text = raw.strip()
    if not text:
        raise LicenseValidationError("LICENSE_EMPTY", "license text is empty")
    candidates = [text]
    if not text.startswith("{"):
        try:
            decoded = base64.urlsafe_b64decode("".join(text.split()) + "=" * (-len("".join(text.split())) % 4))
            candidates.append(decoded.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError, base64.binascii.Error):
            pass
    for candidate_text in candidates:
        try:
            candidate = json.loads(candidate_text)
        except (TypeError, json.JSONDecodeError):
            continue
        return _persist_validated_license(validate_license(candidate))
    raise LicenseValidationError("INVALID_LICENSE", "license text is not valid")


def _persist_validated_license(validated: dict[str, Any]) -> dict[str, Any]:
    ACTIVE_LICENSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if ACTIVE_LICENSE_PATH.exists():
        backup = ACTIVE_LICENSE_PATH.with_suffix(".license.bak")
        if backup.exists():
            backup = ACTIVE_LICENSE_PATH.with_suffix(".license.bak.1")
        shutil.copy2(ACTIVE_LICENSE_PATH, backup)
    _atomic_write(ACTIVE_LICENSE_PATH, (json.dumps(validated, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    result = check_license()
    if result.get("code") == "CLOCK_RECOVERY_PENDING" and clock_status().get("recovery_ready"):
        recover_clock_rollback()
        result = check_license()
    return result


def _public_summary(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "license_id": data.get("license_id"),
        "customer_name": data.get("customer_name"),
        "edition": data.get("edition"),
        "expires_at": data.get("expires_at"),
        "features": list(data.get("features") or []),
    }


def _friendly_error(code: str) -> str:
    return {
        "LICENSE_EMPTY": "请先粘贴激活码",
        "LICENSE_FILE_CORRUPTED": "软件授权文件异常，请重新激活",
        "INVALID_LICENSE": "激活码格式不正确",
        "SIGNATURE_INVALID": "激活码无效",
        "DEVICE_MISMATCH": "此激活码不属于当前设备",
        "NOT_YET_VALID": "当前授权尚未生效",
        "LICENSE_EXPIRED": "当前授权已经过期，请联系软件提供方续期",
        "CLOCK_ROLLBACK_SUSPECTED": "请开启 Windows 自动校时后重新检查",
        "PRODUCT_MISMATCH": "许可证产品不匹配",
        "UNKNOWN_SCHEMA_VERSION": "许可证版本不支持",
        "DEVICE_IDENTITY_UNAVAILABLE": "暂时无法读取设备信息，请以管理员身份重新打开软件或联系软件提供方。",
        "INSTALLATION_ID_MISSING": "本机安装标识已丢失，现有许可证可能无法继续使用。请联系软件提供方重新绑定，不要重复安装或清理授权目录。",
        "INSTALLATION_ID_CONFLICT": "本机安装标识不一致，无法安全判断设备绑定，请联系软件提供方。",
        "INSTALLATION_BACKUP_UNAVAILABLE": "本机安装标识备份无法写入，请以管理员身份重新打开软件。",
        "INSTALLATION_BACKUP_CORRUPTED": "本机安装标识备份已损坏，现有许可证可能无法继续使用。请联系软件提供方。",
        "INSTALLATION_ID_CONFIRMATION_REQUIRED": "本机安装标识不一致，请确认后才能重新建立设备绑定。",
    }.get(code, "激活失败，请联系售后，错误编号：LIC-XXX")


def license_error_message(code: str) -> str:
    return _friendly_error(code)


def license_allows_generation(feature: str | None = None, now: datetime | None = None) -> tuple[bool, dict[str, Any]]:
    status = check_license(now=now)
    if status.get("valid") and feature and feature not in set((status.get("license") or {}).get("features") or []):
        return False, {"valid": False, "code": "FEATURE_NOT_LICENSED", "message": "当前许可证未包含此功能"}
    return bool(status.get("valid")), status


def require_generation_license(feature: str | None = None, now: datetime | None = None) -> None:
    valid, status = license_allows_generation(feature, now=now)
    if not valid:
        from providers.errors import ProviderError

        raise ProviderError("LICENSE_REQUIRED", str(status.get("message") or "当前授权不可用"))
