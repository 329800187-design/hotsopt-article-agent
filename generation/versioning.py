from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterable

from modules.security import sanitize_json
from modules.models import utc_now


MANAGED_FILES = (
    "article.json",
    "article.md",
    "prompts/article_prompt.txt",
    "prompts/cover_prompt.txt",
    "images/cover.png",
    "images/assets.json",
)


class VersionCommitError(RuntimeError):
    def __init__(self, message: str, *, commit_path: Path | None = None):
        super().__init__(message)
        self.commit_path = commit_path


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _files_present(root: Path, files: Iterable[str]) -> list[str]:
    return [relative for relative in files if (root / relative).is_file()]


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".commit.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(sanitize_json(value), ensure_ascii=False, indent=2), encoding="utf-8")
        with temporary.open("r+", encoding="utf-8") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_intended_state(attempts_root: Path, state: dict[str, Any]) -> Path:
    path = attempts_root / "intended_state.json"
    _write_json(path, state)
    return path


def load_intended_state(attempts_root: Path) -> dict[str, Any] | None:
    path = attempts_root / "intended_state.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def update_commit_record(attempts_root: Path, record: dict[str, Any], status: str) -> dict[str, Any]:
    updated = dict(record)
    updated["status"] = status
    updated["updated_at"] = utc_now()
    _write_json(attempts_root / "commit.json", updated)
    return updated


def _next_version_id(versions_root: Path) -> str:
    numbers = []
    for path in versions_root.glob("version-*"):
        try:
            numbers.append(int(path.name.split("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return f"version-{max(numbers, default=0) + 1:04d}"


def _copy_candidate(source_root: Path, candidate_root: Path, files: Iterable[str]) -> list[str]:
    copied: list[str] = []
    for relative in files:
        source = source_root / relative
        if source.is_file():
            _copy_file(source, candidate_root / relative)
            copied.append(relative)
    return copied


def commit_candidate(
    root: Path,
    candidate_root: Path,
    *,
    files: Iterable[str] = MANAGED_FILES,
    files_to_delete: Iterable[str] = (),
    version_id: str | None = None,
    defer_finalize: bool = False,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Commit a candidate with rollback metadata and crash recovery evidence.

    Only the listed files are replaced. The task snapshot is intentionally not
    part of this file transaction; callers update it after this function
    returns successfully.
    """
    root = root.resolve()
    candidate_root = candidate_root.resolve()
    if not candidate_root.is_dir():
        raise VersionCommitError("candidate directory does not exist")
    selected_files = tuple(dict.fromkeys(files))
    delete_files = tuple(dict.fromkeys(str(relative).replace("\\", "/") for relative in files_to_delete))
    overlap = set(selected_files) & set(delete_files)
    if overlap:
        raise VersionCommitError(f"file cannot be replaced and deleted: {sorted(overlap)!r}")
    attempts_root = root / ".attempts" / f"commit-{utc_now().replace(':', '').replace('+00:00', 'Z')}-{uuid.uuid4().hex[:8]}"
    rollback_root = attempts_root / "rollback"
    candidate_copy = attempts_root / "candidate"
    attempts_root.mkdir(parents=True, exist_ok=True)
    copied_files = _copy_candidate(candidate_root, candidate_copy, selected_files)
    existing_files = _files_present(root, tuple(dict.fromkeys((*selected_files, *delete_files))))
    rollback_files = _copy_candidate(root, rollback_root, existing_files)
    versions_root = root / ".versions"
    versions_root.mkdir(parents=True, exist_ok=True)
    selected_version = version_id or _next_version_id(versions_root)
    version_root = versions_root / selected_version
    if version_root.exists():
        shutil.rmtree(version_root)
    _copy_candidate(candidate_copy, version_root, copied_files)
    commit_path = attempts_root / "commit.json"
    record: dict[str, Any] = {
        "version_id": selected_version,
        "previous_version_id": None,
        "status": "prepared",
        "files": copied_files,
        "files_to_replace": [relative for relative in copied_files if relative in existing_files],
        "files_to_create": [relative for relative in copied_files if relative not in existing_files],
        "files_to_delete": [relative for relative in delete_files if relative in existing_files],
        "candidate_hashes": {relative: _sha256(candidate_copy / relative) for relative in copied_files},
        "rollback_hashes": {relative: _sha256(rollback_root / relative) for relative in rollback_files},
        "commit_started_at": "",
        "commit_completed_at": "",
        "attempt_root": str(attempts_root.name),
    }
    if metadata:
        record.update(sanitize_json(metadata))
    _write_json(commit_path, record)
    record["status"] = "committing_files"
    record["commit_started_at"] = utc_now()
    _write_json(commit_path, record)
    try:
        for relative in copied_files:
            source = candidate_copy / relative
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_file(source, target)
        for relative in record["files_to_delete"]:
            (root / relative).unlink(missing_ok=False)
        record["status"] = "files_committed"
        record["files_committed_at"] = utc_now()
        _write_json(commit_path, record)
        if not defer_finalize:
            finalize_candidate(root, attempts_root, record)
            # Preserve the legacy direct-call contract; production orchestration
            # uses defer_finalize and records the stronger completed state.
            record["status"] = "committed"
            _write_json(commit_path, record)
            _write_json(version_root / "version.json", record)
        return record
    except Exception as exc:
        try:
            rollback_candidate(root, attempts_root, record)
        except Exception as rollback_error:
            raise VersionCommitError(f"version commit failed and rollback failed: {rollback_error}", commit_path=commit_path) from exc
        raise VersionCommitError(f"version commit failed: {exc}", commit_path=commit_path) from exc


def finalize_candidate(root: Path, attempts_root: Path, record: dict[str, Any] | None = None) -> dict[str, Any]:
    """Mark a file commit complete only after task and SQLite state are durable."""
    root = root.resolve()
    record = record or json.loads((attempts_root / "commit.json").read_text(encoding="utf-8"))
    candidate_root = attempts_root / "candidate"
    candidate_hashes = record.get("candidate_hashes") or {}
    if not candidate_hashes or any(_sha256(candidate_root / relative) != expected for relative, expected in candidate_hashes.items()):
        raise VersionCommitError("candidate files are incomplete", commit_path=attempts_root / "commit.json")
    if any(_sha256(root / relative) != expected for relative, expected in candidate_hashes.items()):
        raise VersionCommitError("formal files do not match candidate", commit_path=attempts_root / "commit.json")
    if any((root / relative).exists() for relative in record.get("files_to_delete") or []):
        raise VersionCommitError("stale files remain after commit", commit_path=attempts_root / "commit.json")
    record["status"] = "completed"
    record["commit_completed_at"] = utc_now()
    _write_json(attempts_root / "commit.json", record)
    _write_json(root / ".versions" / str(record["version_id"]) / "version.json", record)
    return record


def formal_files_match(root: Path, record: dict[str, Any]) -> bool:
    hashes = record.get("candidate_hashes") or {}
    if not hashes:
        return False
    if any(_sha256(root / relative) != expected for relative, expected in hashes.items()):
        return False
    return not any((root / relative).exists() for relative in record.get("files_to_delete") or [])


def rollback_candidate(root: Path, attempts_root: Path, record: dict[str, Any] | None = None) -> None:
    record = record or json.loads((attempts_root / "commit.json").read_text(encoding="utf-8"))
    files = list(record.get("files") or [])
    replace_files = set(record.get("files_to_replace") or files)
    create_files = set(record.get("files_to_create") or [])
    delete_files = set(record.get("files_to_delete") or [])
    rollback_root = attempts_root / "rollback"
    for relative in sorted(replace_files | delete_files):
        target = root / relative
        backup = rollback_root / relative
        if backup.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
        else:
            target.unlink(missing_ok=True)
    for relative in sorted(create_files):
        (root / relative).unlink(missing_ok=True)
    commit_path = attempts_root / "commit.json"
    record["status"] = "rollback_required"
    _write_json(commit_path, record)
    record["status"] = "rolled_back"
    record["rollback_completed_at"] = utc_now()
    _write_json(commit_path, record)


def snapshot_current(root: Path, *, label: str = "snapshot", files: Iterable[str] = MANAGED_FILES) -> dict[str, Any] | None:
    present = _files_present(root, files)
    if not present:
        return None
    versions_root = root / ".versions"
    versions_root.mkdir(parents=True, exist_ok=True)
    version_id = _next_version_id(versions_root)
    version_root = versions_root / version_id
    _copy_candidate(root, version_root, present)
    manifest = {"version_id": version_id, "label": label, "status": "committed", "files": present, "created_at": utc_now(), "hashes": {relative: _sha256(version_root / relative) for relative in present}}
    _write_json(version_root / "version.json", manifest)
    return manifest


def recover_version_commits(root: Path) -> list[dict[str, Any]]:
    """Resolve interrupted multi-file commits without touching task.json."""
    recovered: list[dict[str, Any]] = []
    attempts_root = root / ".attempts"
    if not attempts_root.is_dir():
        return recovered
    for commit_path in attempts_root.glob("commit-*/commit.json"):
        try:
            record = json.loads(commit_path.read_text(encoding="utf-8"))
            status = str(record.get("status") or "")
            if status == "completed":
                if formal_files_match(root, record):
                    if load_intended_state(commit_path.parent) is not None:
                        recovered.append({"attempt": commit_path.parent.name, "status": "completed", "needs_state_recovery": True, "task_id": record.get("task_id")})
                else:
                    rollback_candidate(root, commit_path.parent, record)
                    recovered.append({"attempt": commit_path.parent.name, "status": "rolled_back", "recovery_error": "completed commit files do not match"})
                continue
            if status not in {"prepared", "committing", "committing_files", "files_committed", "committing_state"}:
                continue
            candidate_root = commit_path.parent / "candidate"
            hashes = record.get("candidate_hashes") or {}
            complete = bool(hashes) and all(_sha256(candidate_root / relative) == expected for relative, expected in hashes.items())
            deleted = record.get("files_to_delete") or []
            formal_complete = complete and all(_sha256(root / relative) == expected for relative, expected in hashes.items()) and all(not (root / relative).exists() for relative in deleted)
            intended_state = load_intended_state(commit_path.parent)
            if formal_complete and status in {"files_committed", "committing_state"} and intended_state is not None:
                recovered.append({"attempt": commit_path.parent.name, "status": status, "needs_state_recovery": True, "task_id": record.get("task_id")})
            elif formal_complete and status in {"files_committed", "committing_state"}:
                record["status"] = "completed"
                record["commit_completed_at"] = utc_now()
                _write_json(commit_path, record)
                _write_json(root / ".versions" / str(record["version_id"]) / "version.json", record)
                recovered.append({"attempt": commit_path.parent.name, "status": "completed"})
            else:
                rollback_candidate(root, commit_path.parent, record)
                recovered.append({"attempt": commit_path.parent.name, "status": "rolled_back"})
        except Exception as exc:
            recovered.append({"attempt": commit_path.parent.name, "status": "failed", "error": str(exc)})
    return recovered


def reconcile_version_commit(root: Path, attempts_root: Path, record: dict[str, Any], store: Any) -> dict[str, Any]:
    from modules.generation_store import load_generation_task, save_generation_task

    intended = load_intended_state(attempts_root)
    final_state = intended.get("final_state") if intended else None
    previous_state = intended.get("previous_state") if intended else None
    task_id = str((intended or {}).get("task_id") or record.get("task_id") or "")

    def fail_recovery(reason: str) -> dict[str, Any]:
        try:
            rollback_candidate(root, attempts_root, record)
        finally:
            if task_id and isinstance(previous_state, dict):
                restored = dict(previous_state)
                restored.update({
                    "status": "partial_success",
                    "stage": "committing_version",
                    "failed_step": "committing_version",
                    "error_code": "VERSION_STATE_COMMIT_FAILED",
                    "safe_error_message": "新版本提交失败，当前展示上一版本",
                    "fallback_notice": "新版本提交失败，当前展示上一版本",
                    "retryable": True,
                    "rewrite_requested": False,
                    "previous_result": previous_state.get("previous_result"),
                    "state_version": int((load_generation_task(task_id) or {}).get("state_version") or 0) + 1,
                    "updated_at": utc_now(),
                })
                save_generation_task(restored, expected_version=None, allow_terminal_recovery=True)
                getattr(store, "force_task_status", store.update_task_status)(task_id, "partial_success")
            elif task_id:
                getattr(store, "force_task_status", store.update_task_status)(task_id, "partial_success")
        return {"status": "rolled_back", "task_id": task_id, "recovery_error": reason}

    if not task_id or not isinstance(final_state, dict) or not isinstance(previous_state, dict):
        return fail_recovery("intended_state.json is invalid")
    if str(final_state.get("task_id") or task_id) != task_id or str(final_state.get("version_id") or "") != str(record.get("version_id") or ""):
        return fail_recovery("intended state version does not match commit")
    if not formal_files_match(root, record):
        return fail_recovery("formal files do not match candidate")

    try:
        current = load_generation_task(task_id) or {}
        desired = dict(final_state)
        desired["state_version"] = max(int(current.get("state_version") or 0) + 1, int(desired.get("state_version") or 0))
        desired["updated_at"] = utc_now()
        save_generation_task(desired, expected_version=None, allow_terminal_recovery=True)
        getattr(store, "force_task_status", store.update_task_status)(task_id, "completed")
        saved = load_generation_task(task_id) or {}
        database_task = store.get_task(task_id)
        if saved.get("status") != "completed" or str(saved.get("version_id") or "") != str(record.get("version_id") or "") or not database_task or database_task.get("status") != "completed":
            return fail_recovery("task and SQLite state could not be reconciled")
        try:
            finalized = finalize_candidate(root, attempts_root, record)
        except Exception:
            if formal_files_match(root, record):
                return {"status": "committing_state", "task_id": task_id, "pending_finalize": True}
            return fail_recovery("commit completion could not be written")
        return {"status": finalized.get("status", "completed"), "task_id": task_id}
    except Exception as exc:
        return fail_recovery(str(exc))


def list_versions(root: Path) -> list[dict[str, Any]]:
    versions: list[dict[str, Any]] = []
    for path in sorted((root / ".versions").glob("version-*/version.json"), reverse=True):
        try:
            versions.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return versions
