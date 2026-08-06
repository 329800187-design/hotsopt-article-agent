from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import uuid
from pathlib import Path

from modules.app_paths import (
    data_root,
    launch_mode,
    license_root,
    migrate_legacy_data,
)
from modules.credential_store import load_secret, save_secret


PRODUCT_ID = "hotspot-article-agent"


ERROR_MESSAGES = {
    "MACHINE_GUID_UNAVAILABLE": "无法读取本机设备标识。",
    "DATA_ROOT_UNAVAILABLE": "无法访问应用数据目录。",
    "DATA_ROOT_NOT_WRITABLE": "应用数据目录不可写，请检查当前用户对该目录的权限。",
    "INSTALLATION_JSON_CORRUPTED": "设备身份主文件已损坏。",
    "INSTALLATION_BACKUP_UNAVAILABLE": "设备身份加密备份不可用。",
    "INSTALLATION_BACKUP_CORRUPTED": "设备身份加密备份已损坏或不属于当前 Windows 用户。",
    "INSTALLATION_ID_MISSING": "已发现设备身份痕迹，但关键身份文件缺失。",
    "INSTALLATION_ID_CONFLICT": "设备身份主文件与备份不一致。",
    "DPAPI_PROTECT_FAILED": "Windows DPAPI 无法保护设备身份。",
    "DPAPI_UNPROTECT_FAILED": "Windows DPAPI 无法读取设备身份备份。",
    "IDENTITY_MIGRATION_CONFLICT": "检测到多个不一致或不完整的旧设备身份，已停止自动迁移。",
    "IDENTITY_INITIALIZATION_FAILED": "设备身份初始化失败。",
}


class DeviceIdentityUnavailable(RuntimeError):
    pass


class InstallationIdentityError(DeviceIdentityUnavailable):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def installation_path() -> Path:
    return license_root() / "installation.json"


def installation_backup_path() -> Path:
    return license_root() / "installation.dat"


def installation_marker_path() -> Path:
    return license_root() / "installation.initialized"


def _machine_guid() -> str:
    if os.name != "nt":
        return "non-windows"
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            value = str(value).strip()
            if value:
                return value
    except (OSError, ImportError):
        pass
    try:
        value = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "(Get-CimInstance Win32_ComputerSystemProduct).UUID"],
            stderr=subprocess.DEVNULL,
            timeout=5,
            text=True,
        ).strip()
        if value and value.upper() not in {"UNKNOWN", "NONE", "NOT AVAILABLE"}:
            return value
    except (OSError, subprocess.SubprocessError):
        pass
    raise InstallationIdentityError("MACHINE_GUID_UNAVAILABLE", ERROR_MESSAGES["MACHINE_GUID_UNAVAILABLE"])


def _atomic_json_write(path: Path, value: dict[str, str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    except OSError as exc:
        raise InstallationIdentityError("DATA_ROOT_NOT_WRITABLE", ERROR_MESSAGES["DATA_ROOT_NOT_WRITABLE"]) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            Path(temporary).replace(path)
        except OSError as exc:
            raise InstallationIdentityError("DATA_ROOT_NOT_WRITABLE", ERROR_MESSAGES["DATA_ROOT_NOT_WRITABLE"]) from exc
    finally:
        Path(temporary).unlink(missing_ok=True)


def _installation_id_from_json(path: Path) -> tuple[str, str]:
    if not path.exists():
        return "", "missing"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", "corrupt"
    installation_id = str(value.get("installation_id") or "").strip() if isinstance(value, dict) else ""
    return (installation_id, "valid") if installation_id else ("", "corrupt")


def _installation_id_from_backup(path: Path) -> tuple[str, str, str]:
    if not path.exists():
        return "", "missing", ""
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", "corrupt", "INSTALLATION_BACKUP_CORRUPTED"
    try:
        value = load_secret("dpapi:installation_id", path=path).strip()
    except Exception:
        return "", "corrupt", "DPAPI_UNPROTECT_FAILED"
    return (value, "valid", "") if value else ("", "corrupt", "INSTALLATION_BACKUP_CORRUPTED")


def _save_installation_backup(installation_id: str) -> None:
    try:
        save_secret("installation_id", installation_id, path=installation_backup_path())
    except OSError as exc:
        raise InstallationIdentityError(
            "DATA_ROOT_NOT_WRITABLE",
            ERROR_MESSAGES["DATA_ROOT_NOT_WRITABLE"],
        ) from exc
    except Exception as exc:
        raise InstallationIdentityError(
            "DPAPI_PROTECT_FAILED",
            ERROR_MESSAGES["DPAPI_PROTECT_FAILED"],
        ) from exc


def _write_installation_pair(installation_id: str) -> None:
    _atomic_json_write(installation_path(), {"schema_version": "1", "installation_id": installation_id})
    _save_installation_backup(installation_id)
    _atomic_json_write(installation_marker_path(), {"schema_version": "1", "initialized": "true"})


def _device_code_for_installation(installation_id: str, machine_guid: str | None = None) -> str:
    material = "|".join((machine_guid or _machine_guid(), installation_id, PRODUCT_ID)).encode("utf-8")
    digest = hashlib.sha256(material).digest()
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=")[:20]
    return "-".join(encoded[index : index + 4] for index in range(0, 20, 4))


def _licensed_device_code() -> str:
    active = license_root() / "active.license"
    try:
        value = json.loads(active.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(value.get("device_code") or "").strip() if isinstance(value, dict) else ""


def rebuild_installation_id(confirm: bool = False) -> str:
    if not confirm:
        raise InstallationIdentityError(
            "INSTALLATION_ID_CONFIRMATION_REQUIRED",
            "本机安装标识不一致，请确认后才能重新建立设备绑定。",
        )
    installation_id = uuid.uuid4().hex
    _write_installation_pair(installation_id)
    return installation_id


def load_or_create_installation_id() -> str:
    try:
        migration = migrate_legacy_data()
    except Exception as exc:
        code = getattr(exc, "code", "IDENTITY_MIGRATION_CONFLICT")
        raise InstallationIdentityError(code, ERROR_MESSAGES.get(code, ERROR_MESSAGES["IDENTITY_MIGRATION_CONFLICT"])) from exc
    conflict = str(migration.get("conflict_code") or "")
    if conflict:
        raise InstallationIdentityError(conflict, ERROR_MESSAGES.get(conflict, ERROR_MESSAGES["IDENTITY_MIGRATION_CONFLICT"]))
    path = installation_path()
    backup = installation_backup_path()
    json_id, json_status = _installation_id_from_json(path)
    backup_id, backup_status, backup_error = _installation_id_from_backup(backup)
    if json_status == "valid" and backup_status == "valid" and json_id == backup_id:
        if not installation_marker_path().exists():
            _atomic_json_write(installation_marker_path(), {"schema_version": "1", "initialized": "true"})
        return json_id
    if json_status == "valid" and backup_status == "missing":
        _save_installation_backup(json_id)
        _atomic_json_write(installation_marker_path(), {"schema_version": "1", "initialized": "true"})
        return json_id
    if json_status in {"missing", "corrupt"} and backup_status == "valid":
        _atomic_json_write(path, {"schema_version": "1", "installation_id": backup_id})
        _atomic_json_write(installation_marker_path(), {"schema_version": "1", "initialized": "true"})
        return backup_id
    if json_status == "valid" and backup_status == "corrupt":
        licensed_code = _licensed_device_code()
        if not licensed_code or _device_code_for_installation(json_id) == licensed_code:
            _save_installation_backup(json_id)
            _atomic_json_write(installation_marker_path(), {"schema_version": "1", "initialized": "true"})
            return json_id
        code = backup_error or "INSTALLATION_ID_CONFLICT"
        raise InstallationIdentityError(code, ERROR_MESSAGES.get(code, ERROR_MESSAGES["INSTALLATION_ID_CONFLICT"]))
    if json_status in {"missing", "corrupt"} and backup_status == "corrupt":
        code = "INSTALLATION_JSON_CORRUPTED" if json_status == "corrupt" else (backup_error or "INSTALLATION_BACKUP_CORRUPTED")
        raise InstallationIdentityError(
            code,
            ERROR_MESSAGES[code],
        )
    if json_status == "valid" and backup_status == "valid" and json_id != backup_id:
        licensed_code = _licensed_device_code()
        if licensed_code:
            json_matches = _device_code_for_installation(json_id) == licensed_code
            backup_matches = _device_code_for_installation(backup_id) == licensed_code
            if json_matches != backup_matches:
                selected = json_id if json_matches else backup_id
                _write_installation_pair(selected)
                return selected
        raise InstallationIdentityError(
            "INSTALLATION_ID_CONFLICT",
            "本机安装标识不一致，无法安全判断设备绑定，请联系软件提供方。",
        )
    if json_status == "corrupt" and backup_status == "missing":
        raise InstallationIdentityError(
            "INSTALLATION_JSON_CORRUPTED",
            ERROR_MESSAGES["INSTALLATION_JSON_CORRUPTED"],
        )
    root = license_root()
    identity_evidence = (installation_marker_path(), installation_path(), installation_backup_path(), root / "active.license")
    if not root.exists() or not any(path.exists() for path in identity_evidence):
        installation_id = uuid.uuid4().hex
        _write_installation_pair(installation_id)
        return installation_id
    raise InstallationIdentityError(
        "INSTALLATION_ID_MISSING",
        "本机安装标识已丢失，现有许可证可能无法继续使用。请联系软件提供方重新绑定，不要重复安装或清理授权目录。",
    )


def device_code() -> str:
    return _device_code_for_installation(load_or_create_installation_id())


def device_status() -> dict[str, object]:
    path = installation_path()
    root = data_root()
    migration = {"status": "not_checked"}
    writable = False
    try:
        root.mkdir(parents=True, exist_ok=True)
        writable = os.access(root, os.W_OK)
        if not writable:
            raise InstallationIdentityError("DATA_ROOT_NOT_WRITABLE", ERROR_MESSAGES["DATA_ROOT_NOT_WRITABLE"])
        migration = migrate_legacy_data()
        installation_id = load_or_create_installation_id()
    except InstallationIdentityError as exc:
        return {
            "device_code": "",
            "installation_id_missing": exc.code == "INSTALLATION_ID_MISSING",
            "device_identity_unavailable": True,
            "installation_error": exc.code,
            "message": exc.message,
            "data_root": str(root),
            "license_root": str(license_root()),
            "launch_mode": launch_mode(),
            "writable": writable,
            "legacy_detected": bool(migration.get("source")),
            "migration_performed": migration.get("status") == "migrated",
        }
    except OSError:
        return {
            "device_code": "",
            "installation_id_missing": True,
            "device_identity_unavailable": True,
            "installation_error": "DATA_ROOT_UNAVAILABLE",
            "message": ERROR_MESSAGES["DATA_ROOT_UNAVAILABLE"],
            "data_root": str(root),
            "license_root": str(license_root()),
            "launch_mode": launch_mode(),
            "writable": False,
            "legacy_detected": False,
            "migration_performed": False,
        }
    try:
        code = device_code()
    except (DeviceIdentityUnavailable, InstallationIdentityError) as exc:
        code = getattr(exc, "code", "IDENTITY_INITIALIZATION_FAILED")
        return {
            "device_code": "",
            "installation_id_missing": not bool(installation_id),
            "device_identity_unavailable": True,
            "installation_error": code,
            "message": ERROR_MESSAGES.get(code, ERROR_MESSAGES["IDENTITY_INITIALIZATION_FAILED"]),
            "data_root": str(root),
            "license_root": str(license_root()),
            "launch_mode": launch_mode(),
            "writable": writable,
            "legacy_detected": bool(migration.get("source")),
            "migration_performed": migration.get("status") == "migrated",
        }
    return {
        "device_code": code,
        "installation_id_missing": not bool(installation_id),
        "device_identity_unavailable": False,
        "data_root": str(root),
        "license_root": str(license_root()),
        "installation_path": str(path),
        "launch_mode": launch_mode(),
        "writable": writable,
        "legacy_detected": bool(migration.get("source")),
        "migration_performed": migration.get("status") == "migrated",
    }
