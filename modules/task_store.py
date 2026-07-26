from __future__ import annotations

import json
from pathlib import Path

from modules.app_paths import tasks_root
from typing import Any

from modules.security import sanitize_json


TASK_DIR = tasks_root()


def save_task(task: dict[str, Any], task_id: str | None = None) -> Path:
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    identifier = task_id or task.get("id") or "task"
    path = TASK_DIR / f"{identifier}.json"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(sanitize_json(task), handle, ensure_ascii=False, indent=2)
    return path


def load_task(task_id: str) -> dict[str, Any] | None:
    path = TASK_DIR / f"{task_id}.json"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def list_tasks() -> list[dict[str, Any]]:
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    tasks: list[dict[str, Any]] = []
    for path in sorted(TASK_DIR.glob("*.json"), reverse=True):
        try:
            with path.open("r", encoding="utf-8") as handle:
                tasks.append(json.load(handle))
        except (OSError, json.JSONDecodeError):
            continue
    return tasks
