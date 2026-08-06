from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import modules.generation_store as generation_store
from generation.single_task import finalize_cancelled_task, prepare_generation_state
from modules.database import SQLiteStore, get_store
from modules.generation_store import generation_task_dir, load_generation_task, save_generation_task
from modules.models import utc_now
from generation.inline_images import sync_inline_image_files
from generation.versioning import reconcile_version_commit, recover_version_commits
from modules.security import redact_sensitive_text
from modules.task_locks import task_lock
from providers.image_provider import inspect_image


TERMINAL_STATES = {"completed", "cancelled"}
VALID_STATES = {"queued", "running", "failed", "partial_success", "completed", "cancelled"}


class RecoveryReport(dict[str, list[dict[str, Any]]]):
    def __getitem__(self, key: str | int):
        if isinstance(key, int):
            return super().__getitem__("recovered")[key]
        return super().__getitem__(key)


def _valid_article(task_id: str) -> bool:
    path = generation_task_dir(task_id) / "article.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and bool(str(value.get("title") or "").strip()) and bool(value.get("content_markdown") or value.get("sections"))


def _parse_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _record(report: dict[str, list[dict[str, Any]]], bucket: str, task_id: str, **values: Any) -> None:
    report[bucket].append({"task_id": task_id, **values})


def _safe_error(exc: Exception) -> str:
    return redact_sensitive_text(str(exc)) or exc.__class__.__name__


def _force_terminal_snapshot(state: dict[str, Any], status: str, store: SQLiteStore) -> dict[str, Any]:
    current_version = int(state.get("state_version") or 0)
    state["status"] = status
    state["stage"] = "completed" if status == "completed" else "cancelled"
    state["cancellation_requested"] = status == "cancelled"
    state["next_retry_at"] = None
    state["retryable"] = False
    state["updated_at"] = utc_now()
    state["state_version"] = current_version + 1
    save_generation_task(state, expected_version=current_version)
    store.update_task_status(state["task_id"], status)
    return state


def _reconcile(task: dict[str, Any], state: dict[str, Any], store: SQLiteStore) -> dict[str, Any]:
    sqlite_status = str(task.get("status") or "queued")
    json_status = str(state.get("status") or "queued")
    if sqlite_status in TERMINAL_STATES and json_status != sqlite_status:
        return _force_terminal_snapshot(state, sqlite_status, store)
    if json_status in TERMINAL_STATES and sqlite_status != json_status:
        store.update_task_status(state["task_id"], json_status)
        return state
    if json_status in {"failed", "partial_success"} and sqlite_status == "running":
        store.update_task_status(state["task_id"], json_status)
        return state
    if _parse_time(state.get("updated_at")) > _parse_time(task.get("updated_at")) and sqlite_status != json_status:
        store.update_task_status(state["task_id"], json_status)
    return state


def _recover_running(state: dict[str, Any], store: SQLiteStore) -> dict[str, Any]:
    failed_step = str(state.get("failed_step") or "")
    stage = str(state.get("stage") or "")
    has_article = _valid_article(state["task_id"])
    if failed_step == "generating_article":
        partial = False
    elif failed_step == "generating_cover" and has_article:
        partial = True
    elif (failed_step == "generating_inline_images" or stage == "generating_inline_images") and has_article:
        partial = True
    elif stage in {"article_saved", "generating_image_prompt", "generating_cover"} and has_article:
        partial = True
    else:
        partial = False
    state.update({
        "status": "partial_success" if partial else "failed",
        "stage": "interrupted",
        "error_code": "TASK_INTERRUPTED",
        "safe_error_message": "task was interrupted before completion",
        "failed_step": "generating_inline_images" if partial and (failed_step == "generating_inline_images" or stage == "generating_inline_images") else "generating_cover" if partial else "generating_article",
        "retryable": True,
        "next_retry_at": None,
        "recovery_time": utc_now(),
        "recovery_reason": "process restart detected no active executor future",
    })
    current_version = int(state.get("state_version") or 0)
    state["state_version"] = current_version + 1
    state["updated_at"] = utc_now()
    save_generation_task(state, expected_version=current_version)
    store.update_task_status(state["task_id"], state["status"])
    return state


def _reconcile_inline_images(state: dict[str, Any]) -> bool:
    """Turn interrupted per-image generating states into recoverable terminal states."""
    changed = False
    root = generation_task_dir(state["task_id"])
    for asset in state.get("inline_images") or []:
        if asset.get("status") != "generating":
            continue
        image_id = str(asset.get("image_id") or "")
        formal_path = root / "images" / f"{image_id}.png"
        try:
            metadata = inspect_image(formal_path)
        except Exception:
            asset.update({"status": "failed", "error_code": "TASK_INTERRUPTED", "error": "正文图片生成被中断", "retryable": True})
        else:
            asset.update({"status": "completed", "metadata": metadata, "fallback_available": False})
        changed = True
    if changed:
        assets = state.get("inline_images") or []
        completed = sum(item.get("status") == "completed" for item in assets)
        failed = sum(item.get("status") == "failed" for item in assets)
        pending = sum(item.get("status") in {"pending", "generating"} for item in assets)
        state["inline_image_summary"] = {"total": len(assets), "completed": completed, "failed": failed, "pending": pending, "status": "completed" if not failed and not pending else "partial_success" if completed else "failed"}
        state["updated_at"] = utc_now()
    return changed


def recover_interrupted_tasks(store: SQLiteStore | None = None, executor: Any = None) -> dict[str, list[dict[str, Any]]]:
    store = store or get_store()
    report: RecoveryReport = RecoveryReport(recovered=[], skipped=[], recovery_failed=[])
    try:
        sqlite_tasks = {str(task["task_id"]): task for task in store.list_tasks()}
    except Exception as exc:
        _record(report, "recovery_failed", "*", error=_safe_error(exc), reason="sqlite task listing failed")
        return report
    task_root = generation_store.TASKS_ROOT
    task_ids = set(sqlite_tasks)
    if task_root.exists():
        task_ids.update(path.name for path in task_root.iterdir() if path.is_dir())
    for task_id in sorted(task_ids):
        try:
            task = sqlite_tasks.get(task_id)
            if not task:
                _record(report, "skipped", task_id, reason="no matching SQLite task")
                continue
            path = generation_store.generation_task_path(task_id)
            task_root = generation_task_dir(task_id)
            version_recovery = recover_version_commits(task_root)
            if version_recovery:
                reconciled_versions: list[dict[str, Any]] = []
                for item in version_recovery:
                    if item.get("needs_state_recovery"):
                        attempt_root = task_root / ".attempts" / str(item.get("attempt") or "")
                        commit_path = attempt_root / "commit.json"
                        try:
                            record = json.loads(commit_path.read_text(encoding="utf-8"))
                            reconciled_versions.append({"attempt": item.get("attempt"), **reconcile_version_commit(task_root, attempt_root, record, store)})
                        except Exception as exc:
                            reconciled_versions.append({"attempt": item.get("attempt"), "status": "failed", "error": _safe_error(exc)})
                    elif item.get("status") == "rolled_back" and (task_root / ".attempts" / str(item.get("attempt") or "") / "intended_state.json").is_file():
                        attempt_root = task_root / ".attempts" / str(item.get("attempt") or "")
                        try:
                            record = json.loads((attempt_root / "commit.json").read_text(encoding="utf-8"))
                            reconciled_versions.append({"attempt": item.get("attempt"), **reconcile_version_commit(task_root, attempt_root, record, store)})
                        except Exception as exc:
                            reconciled_versions.append({"attempt": item.get("attempt"), "status": "failed", "error": _safe_error(exc)})
                    else:
                        reconciled_versions.append(item)
                version_recovery = reconciled_versions
            if not path.exists():
                if task.get("status") == "queued":
                    state = prepare_generation_state(task, {}, {}, store=store)
                    _record(report, "recovered", task_id, **{key: value for key, value in state.items() if key != "task_id"}, reason="queued task snapshot initialized")
                else:
                    _record(report, "recovery_failed", task_id, error="task snapshot is missing", reason="non-queued task cannot be initialized")
                continue
            try:
                state = load_generation_task(task_id)
            except Exception as exc:
                _record(report, "recovery_failed", task_id, error=_safe_error(exc), reason="task snapshot read failed")
                continue
            if not state:
                _record(report, "recovery_failed", task_id, error="task snapshot is invalid", reason="task snapshot was not overwritten")
                continue
            if version_recovery:
                state["version_recovery"] = version_recovery
                version_state_changed = False
                if any(item.get("status") == "rolled_back" and item.get("recovery_error") for item in version_recovery):
                    state.update({"status": "partial_success", "stage": "committing_version", "failed_step": "committing_version", "error_code": "VERSION_STATE_COMMIT_FAILED", "safe_error_message": "新版本提交失败，当前展示上一版本", "fallback_notice": "新版本提交失败，当前展示上一版本", "retryable": True})
                    store.update_task_status(task_id, "partial_success")
                    version_state_changed = True
                elif state.get("stage") in {"version_ready", "committing_version"}:
                    if any(item.get("status") in {"committed", "completed"} for item in version_recovery) and (state.get("inline_image_summary") or {}).get("status") == "completed" and _valid_article(task_id):
                        try:
                            state["article"] = json.loads((generation_task_dir(task_id) / "article.json").read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError):
                            pass
                        state.update({"status": "completed", "stage": "completed", "progress": 100, "failed_step": None, "error_code": "", "safe_error_message": "", "retryable": False, "completed_at": state.get("completed_at") or utc_now()})
                        store.update_task_status(task_id, "completed")
                        version_state_changed = True
                    elif any(item.get("status") == "rolled_back" for item in version_recovery):
                        if state.get("error_code") != "VERSION_STATE_COMMIT_FAILED":
                            state.update({"status": "partial_success", "stage": "committing_version", "failed_step": "committing_version", "error_code": "VERSION_COMMIT_FAILED", "safe_error_message": "新版本提交失败，当前展示上一版本", "fallback_notice": "新版本提交失败，当前展示上一版本", "retryable": True})
                        store.update_task_status(task_id, "partial_success")
                        version_state_changed = True
                if version_state_changed:
                    current_version = int(state.get("state_version") or 0)
                    state["state_version"] = current_version + 1
                    state["updated_at"] = utc_now()
                    save_generation_task(state, expected_version=current_version)
            with task_lock(task_id):
                state = _reconcile(task, state, store)
                inline_changed = _reconcile_inline_images(state)
                if state.get("status") not in VALID_STATES:
                    raise ValueError(f"invalid generation task status: {state.get('status')}")
                if inline_changed:
                    summary = state.get("inline_image_summary") or {}
                    if state.get("status") == "completed" and summary.get("status") != "completed":
                        state.update({"status": "partial_success", "stage": "generating_inline_images", "failed_step": "generating_inline_images", "error_code": "TASK_INTERRUPTED", "safe_error_message": "inline image generation was interrupted", "retryable": True, "inline_operation": False})
                        store.update_task_status(task_id, "partial_success")
                    elif state.get("status") == "running" and summary.get("status") == "completed":
                        state.update({"status": "completed", "stage": "completed", "progress": 100, "failed_step": None, "error_code": "", "safe_error_message": "", "retryable": False, "inline_operation": False, "completed_at": state.get("completed_at") or utc_now()})
                        store.update_task_status(task_id, "completed")
                    sync_inline_image_files(state)
                    current_version = int(state.get("state_version") or 0)
                    state["state_version"] = current_version + 1
                    save_generation_task(state, expected_version=current_version)
                if state.get("status") == "completed":
                    _record(report, "skipped", task_id, status="completed", reason="terminal task")
                    continue
                if state.get("status") == "cancelled":
                    _record(report, "skipped", task_id, status="cancelled", reason="terminal task")
                    continue
                if state.get("cancellation_requested"):
                    finalized = finalize_cancelled_task(task_id, store)
                    _record(report, "recovered", task_id, **{key: value for key, value in finalized.items() if key != "task_id"}, reason="cancellation request finalized")
                    continue
                if state.get("status") == "queued":
                    _record(report, "skipped", task_id, status="queued", reason="queued task can be run")
                    continue
                if executor is not None and executor.is_running(task_id):
                    _record(report, "skipped", task_id, status=state.get("status"), reason="active executor future")
                    continue
                if state.get("status") == "running":
                    recovered = _recover_running(state, store)
                    _record(report, "recovered", task_id, **{key: value for key, value in recovered.items() if key != "task_id"})
                else:
                    _record(report, "skipped", task_id, status=state.get("status"), reason="non-running task")
        except Exception as exc:
            _record(report, "recovery_failed", task_id, error=_safe_error(exc), reason="task recovery isolated failure")
    return report
