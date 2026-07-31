from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from modules.app_metadata import DATA_DIR_NAME


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ENV = "HOTSPOT_DATA_ROOT"
INSTALL_MODE_ENV = "HOTSPOT_INSTALL_MODE"
LAUNCH_MODE_ENV = "HOTSPOT_LAUNCH_MODE"
LEGACY_DATA_DIR_NAME = "热点图文工作台"
MIGRATION_REPORT_NAME = "identity-migration.json"


class IdentityMigrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _local_app_data() -> Path:
    configured = os.environ.get("LOCALAPPDATA")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "AppData" / "Local"


def official_windows_data_root() -> Path:
    return (_local_app_data() / DATA_DIR_NAME).resolve()


def is_windows() -> bool:
    return os.name == "nt"


def is_installed() -> bool:
    if os.environ.get(INSTALL_MODE_ENV) == "1":
        return True
    project_str = str(PROJECT_ROOT).lower().replace("\\", "/")
    return f"/programs/{DATA_DIR_NAME.lower()}" in project_str


def launch_mode() -> str:
    if os.environ.get(DATA_ENV) and os.environ.get(LAUNCH_MODE_ENV) == "explicit":
        return "explicit"
    configured = os.environ.get(LAUNCH_MODE_ENV)
    if configured in {"installed", "launcher", "source", "explicit"}:
        return configured
    if is_installed():
        return "installed"
    return "source"


def data_root() -> Path:
    configured = os.environ.get(DATA_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    if is_windows():
        return official_windows_data_root()
    return (PROJECT_ROOT / "data").resolve()


def config_dir() -> Path:
    return data_root() / "config"


def settings_path() -> Path:
    return config_dir() / "settings.json"


def database_path() -> Path:
    return data_root() / "data" / "hotspot_agent.db"


def tasks_root() -> Path:
    return data_root() / "data" / "tasks"


def research_root() -> Path:
    return data_root() / "data" / "research"


def model_test_root() -> Path:
    return data_root() / "data" / "model-tests"


def cache_path() -> Path:
    return data_root() / "cache" / "latest_topics.json"


def exports_root() -> Path:
    return data_root() / "exports"


def logs_root() -> Path:
    return data_root() / "logs"


def runtime_root() -> Path:
    return data_root() / "runtime"


def license_root() -> Path:
    return data_root() / "license"


def updates_root() -> Path:
    return data_root() / "updates"


def ensure_user_data_dirs() -> None:
    for path in (
        config_dir(),
        database_path().parent,
        tasks_root(),
        research_root(),
        model_test_root(),
        cache_path().parent,
        exports_root(),
        logs_root(),
        runtime_root(),
        license_root(),
        updates_root(),
    ):
        path.mkdir(parents=True, exist_ok=True)


def _identity(root: Path) -> tuple[str, str]:
    license_dir = root / "license"
    json_path = license_dir / "installation.json"
    backup_path = license_dir / "installation.dat"
    marker_path = license_dir / "installation.initialized"
    evidence = any(path.exists() for path in (json_path, backup_path, marker_path, license_dir / "active.license"))
    if not evidence:
        return "", "absent"
    if not (json_path.is_file() and backup_path.is_file() and marker_path.is_file()):
        return "", "incomplete"
    try:
        value = json.loads(json_path.read_text(encoding="utf-8"))
        installation_id = str(value.get("installation_id") or "").strip()
    except (OSError, json.JSONDecodeError, AttributeError):
        return "", "corrupt"
    return (installation_id, "complete") if installation_id else ("", "corrupt")


def legacy_data_roots() -> list[Path]:
    candidates = [
        (_local_app_data() / LEGACY_DATA_DIR_NAME).resolve(),
        (PROJECT_ROOT / "data").resolve(),
    ]
    target = data_root().resolve()
    result: list[Path] = []
    for candidate in candidates:
        if candidate != target and candidate not in result:
            result.append(candidate)
    return result


def _copy_if_present(source: Path, destination: Path) -> list[str]:
    mappings = (
        ("license", "license"),
        ("config/settings.json", "config/settings.json"),
        ("data/hotspot_agent.db", "data/hotspot_agent.db"),
        ("data/tasks", "data/tasks"),
        ("export/user", "exports"),
        ("exports", "exports"),
        ("cache", "cache"),
    )
    copied: list[str] = []
    for source_name, destination_name in mappings:
        source_path = source / source_name
        if source == (PROJECT_ROOT / "data").resolve() and source_name == "export/user":
            source_path = PROJECT_ROOT / "export" / "user"
        if not source_path.exists() and source == (PROJECT_ROOT / "data").resolve() and source_name == "config/settings.json":
            source_path = PROJECT_ROOT / "config" / "settings.json"
        if not source_path.exists():
            continue
        destination_path = destination / destination_name
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if source_path.is_dir():
            shutil.copytree(source_path, destination_path, dirs_exist_ok=True)
        else:
            shutil.copy2(source_path, destination_path)
        copied.append(destination_name)
    return copied


def _write_migration_report(target: Path, report: dict[str, object]) -> None:
    report_path = target / "logs" / MIGRATION_REPORT_NAME
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def migrate_legacy_data() -> dict[str, object]:
    target = data_root().resolve()
    target_id, target_status = _identity(target)
    base_report: dict[str, object] = {
        "source": "",
        "target": str(target),
        "status": "not_needed",
        "migrated": False,
        "files_copied": [],
        "conflict_code": "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if target_status == "complete":
        return base_report
    legacy = [(root, *_identity(root)) for root in legacy_data_roots()]
    evidence = [(root, identity, status) for root, identity, status in legacy if status != "absent"]
    if target_status in {"incomplete", "corrupt"}:
        base_report.update(status="conflict", conflict_code="INSTALLATION_ID_CONFLICT")
        _write_migration_report(target, base_report)
        return base_report
    if not evidence:
        return base_report
    invalid = [(root, status) for root, _, status in evidence if status != "complete"]
    if invalid:
        base_report.update(
            source=";".join(str(root) for root, _ in invalid),
            status="conflict",
            conflict_code="IDENTITY_MIGRATION_CONFLICT",
        )
        _write_migration_report(target, base_report)
        return base_report
    identities = {identity for _, identity, _ in evidence}
    if len(identities) != 1:
        base_report.update(
            source=";".join(str(root) for root, _, _ in evidence),
            status="conflict",
            conflict_code="IDENTITY_MIGRATION_CONFLICT",
        )
        _write_migration_report(target, base_report)
        return base_report
    source = evidence[0][0]
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}-migration-", dir=str(target.parent)))
    try:
        copied = _copy_if_present(source, stage)
        staged_id, staged_status = _identity(stage)
        if staged_status != "complete" or staged_id != evidence[0][1]:
            raise IdentityMigrationError("IDENTITY_MIGRATION_CONFLICT", "旧设备身份迁移验证失败。")
        backup = target.with_name(f"{target.name}.pre-migration-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")
        if target.exists():
            target.replace(backup)
        stage.replace(target)
        report = {
            **base_report,
            "source": str(source),
            "status": "migrated",
            "migrated": True,
            "files_copied": copied,
        }
        _write_migration_report(target, report)
        return report
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise
