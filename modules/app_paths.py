from __future__ import annotations

import os
import shutil
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ENV = "HOTSPOT_DATA_ROOT"
INSTALL_MODE_ENV = "HOTSPOT_INSTALL_MODE"
INSTALLED_USER_DATA_ROOT = os.environ.get("LOCALAPPDATA", "") and Path(os.environ["LOCALAPPDATA"]) / "热点图文批量生产工作台"


def is_installed() -> bool:
    """Detect whether running from a bundled install (Programs dir) vs dev source."""
    if os.environ.get(INSTALL_MODE_ENV) == "1":
        return True
    project_str = str(PROJECT_ROOT).lower().replace("\\", "/")
    return "/programs/热点图文批量生产工作台" in project_str


def user_data_root() -> Path:
    """Return the per-user data directory, independent of install location."""
    if is_installed():
        return INSTALLED_USER_DATA_ROOT
    return PROJECT_ROOT / "data"


def data_root() -> Path:
    configured = os.environ.get(DATA_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return user_data_root()


def config_dir() -> Path:
    if os.environ.get(DATA_ENV):
        return data_root() / "config"
    if is_installed():
        return INSTALLED_USER_DATA_ROOT / "config"
    return PROJECT_ROOT / "config"


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
    if os.environ.get(DATA_ENV):
        return data_root() / "exports"
    if is_installed():
        return INSTALLED_USER_DATA_ROOT / "exports"
    return PROJECT_ROOT / "export" / "user"


def logs_root() -> Path:
    if os.environ.get(DATA_ENV):
        return data_root() / "logs"
    if is_installed():
        return INSTALLED_USER_DATA_ROOT / "logs"
    return PROJECT_ROOT / "logs"


def runtime_root() -> Path:
    return data_root() / "runtime"


def license_root() -> Path:
    """License files live in user data, not install dir."""
    return INSTALLED_USER_DATA_ROOT / "license" if is_installed() else PROJECT_ROOT / "data" / "license"


def installed_legacy_config_dir() -> Path | None:
    """Old install-dir config path (pre-R1.2 persistence fix), for migration."""
    if not is_installed():
        return None
    legacy = Path(PROJECT_ROOT) / "config"
    return legacy if legacy.exists() else None


def ensure_user_data_dirs() -> None:
    for path in (config_dir(), database_path().parent, tasks_root(), research_root(), model_test_root(), cache_path().parent, exports_root(), logs_root(), runtime_root(), data_root() / "updates"):
        path.mkdir(parents=True, exist_ok=True)


def migrate_legacy_data() -> dict[str, object]:
    """Copy portable-mode data into the per-user directory without overwriting it."""
    target = data_root().resolve()
    legacy = (PROJECT_ROOT / "data").resolve()
    if not os.environ.get(DATA_ENV) or target == legacy:
        ensure_user_data_dirs()
        return {"migrated": False, "source": str(legacy), "target": str(target)}
    ensure_user_data_dirs()
    mappings = {
        PROJECT_ROOT / "config": config_dir(),
        legacy / "tasks": tasks_root(),
        legacy / "hotspot_agent.db": database_path(),
        legacy / "cache": cache_path().parent,
        PROJECT_ROOT / "export" / "user": exports_root(),
        PROJECT_ROOT / "logs": logs_root(),
    }
    copied: list[str] = []
    for source, destination in mappings.items():
        if not source.exists():
            continue
        if destination.exists():
            if not destination.is_dir() or any(destination.iterdir()):
                continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(source, destination)
        copied.append(str(destination))
    return {"migrated": bool(copied), "copied": copied, "source": str(legacy), "target": str(target)}
