from __future__ import annotations

import os
import shutil
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ENV = "HOTSPOT_DATA_ROOT"


def data_root() -> Path:
    configured = os.environ.get(DATA_ENV)
    return Path(configured).expanduser().resolve() if configured else PROJECT_ROOT / "data"


def config_dir() -> Path:
    return PROJECT_ROOT / "config" if not os.environ.get(DATA_ENV) else data_root() / "config"


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
    return PROJECT_ROOT / "export" / "user" if not os.environ.get(DATA_ENV) else data_root() / "exports"


def logs_root() -> Path:
    return PROJECT_ROOT / "logs" if not os.environ.get(DATA_ENV) else data_root() / "logs"


def runtime_root() -> Path:
    return data_root() / "runtime"


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
