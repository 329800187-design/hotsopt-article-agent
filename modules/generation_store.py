from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from modules.security import sanitize_json
from modules.app_paths import PROJECT_ROOT, tasks_root


ROOT = PROJECT_ROOT
TASKS_ROOT = tasks_root()


class GenerationStateConflict(RuntimeError):
    pass


TERMINAL_STATES = {"completed", "failed", "partial_success", "cancelled"}


def generation_task_dir(task_id: str) -> Path:
    return TASKS_ROOT / str(task_id)


def generation_task_path(task_id: str) -> Path:
    return generation_task_dir(task_id) / "task.json"


def save_generation_task(task: dict[str, Any], expected_version: int | None = None, allow_terminal_recovery: bool = False) -> Path:
    task_id = str(task.get("task_id") or task.get("id") or "")
    if not task_id:
        raise ValueError("generation task id is required")
    path = generation_task_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_generation_task(task_id)
    if expected_version is not None:
        current_version = int((current or {}).get("state_version") or 0)
        if current_version != expected_version:
            raise GenerationStateConflict("generation task state version changed")
    if current and current.get("status") == "completed" and task.get("status") != "completed" and not allow_terminal_recovery and not task.get("rewrite_requested") and not task.get("inline_operation"):
        raise GenerationStateConflict("completed task cannot transition")
    if current and current.get("status") == "cancelled" and task.get("status") != "cancelled":
        raise GenerationStateConflict("cancelled task cannot transition")
    safe_task = sanitize_json(task)
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".task.", suffix=".tmp", delete=False)
    temporary_path = Path(handle.name)
    try:
        with handle as output:
            json.dump(safe_task, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        for attempt in range(5):
            try:
                temporary_path.replace(path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return path


def load_generation_task(task_id: str) -> dict[str, Any] | None:
    path = generation_task_path(task_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_generation_tasks() -> list[dict[str, Any]]:
    TASKS_ROOT.mkdir(parents=True, exist_ok=True)
    values: list[dict[str, Any]] = []
    for path in sorted(TASKS_ROOT.glob("*/task.json"), reverse=True):
        try:
            values.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return values
