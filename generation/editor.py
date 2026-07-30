from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from generation.versioning import MANAGED_FILES, VersionCommitError, commit_candidate, finalize_candidate, list_versions, rollback_candidate, update_commit_record, write_intended_state
from modules.database import SQLiteStore, get_store
from modules.generation_store import generation_task_dir, load_generation_task, save_generation_task
from modules.models import utc_now
from modules.security import sanitize_json, sanitize_sensitive_data
from modules.task_locks import task_lock
from providers.text_provider import ProviderError


def _article_path(task_id: str) -> Path:
    return generation_task_dir(task_id) / "article.json"


def _draft_path(task_id: str) -> Path:
    return generation_task_dir(task_id) / "article.draft.json"


def _content_sha(article: dict[str, Any]) -> str:
    content = str(article.get("content_markdown") or "")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _read_article(task_id: str) -> dict[str, Any]:
    state = load_generation_task(task_id)
    if state and isinstance(state.get("article"), dict):
        return sanitize_sensitive_data(state["article"])
    path = _article_path(task_id)
    if not path.is_file():
        raise ProviderError("ARTICLE_NOT_AVAILABLE", "article result is missing")
    try:
        article = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderError("ARTICLE_NOT_AVAILABLE", "article result is invalid") from exc
    if not isinstance(article, dict):
        raise ProviderError("ARTICLE_NOT_AVAILABLE", "article result is invalid")
    return sanitize_sensitive_data(article)


def _read_editing_article(task_id: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    state = state or load_generation_task(task_id) or {}
    editing = state.get("editing_article")
    if isinstance(editing, dict):
        return sanitize_sensitive_data(editing)
    draft_path = _draft_path(task_id)
    if draft_path.is_file():
        try:
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            draft = None
        if isinstance(draft, dict):
            return sanitize_sensitive_data(draft)
    return _read_article(task_id)


def _markdown(article: dict[str, Any]) -> str:
    title = str(article.get("title") or "未命名文章").strip()
    intro = str(article.get("lead") or article.get("intro") or article.get("summary") or "").strip()
    chunks = [f"# {title}"]
    if intro:
        chunks.extend(["", intro])
    for section in article.get("sections") or []:
        heading = str(section.get("heading") or "").strip()
        body = str(section.get("body") or "").strip()
        if heading or body:
            chunks.extend(["", f"## {heading}", body])
    return "\n".join(chunks).strip() + "\n"


def _body_markdown(article: dict[str, Any]) -> str:
    chunks: list[str] = []
    for section in article.get("sections") or []:
        heading = str(section.get("heading") or "").strip()
        body = str(section.get("body") or "").strip()
        if heading or body:
            chunks.extend(["", f"## {heading}", body])
    return "\n".join(chunks).strip() + "\n"


def _normalise_article_changes(current: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    article = dict(current)
    if "title" in changes:
        title = str(changes.get("title") or "").strip()
        if not title:
            raise ValueError("文章标题不能为空")
        article["title"] = title
    if "intro" in changes:
        article["intro"] = str(changes.get("intro") or "").strip()
        article["lead"] = article["intro"]
        article["summary"] = article["intro"]
    if "summary" in changes and "intro" not in changes:
        article["summary"] = str(changes.get("summary") or "").strip()
        article["intro"] = article["summary"]
        article["lead"] = article["summary"]
    if "sections" in changes:
        sections = changes.get("sections")
        if not isinstance(sections, list):
            raise ValueError("正文小节格式不正确")
        clean_sections: list[dict[str, Any]] = []
        for index, raw in enumerate(sections, start=1):
            if not isinstance(raw, dict):
                raise ValueError("正文小节格式不正确")
            heading = str(raw.get("heading") or "").strip()
            body = str(raw.get("body") or "").strip()
            if not heading or not body:
                raise ValueError(f"第 {index} 个小节不能为空")
            section = dict(raw)
            section.update({"heading": heading, "body": body})
            clean_sections.append(section)
        if not clean_sections:
            raise ValueError("正文至少需要一个小节")
        article["sections"] = clean_sections
    article["content_markdown"] = _markdown(article)
    article["body_markdown"] = _body_markdown(article)
    article["updated_at"] = utc_now()
    return sanitize_sensitive_data(article)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(sanitize_json(value), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _save_state(state: dict[str, Any], store: SQLiteStore) -> dict[str, Any]:
    current = load_generation_task(state["task_id"]) or {}
    expected = int(current.get("state_version") or 0)
    state["state_version"] = expected + 1
    state["updated_at"] = utc_now()
    save_generation_task(state, expected_version=expected if current else None)
    store.update_task_edit_metadata(
        state["task_id"],
        int(state.get("article_revision") or 0),
        str(state.get("article_edit_status") or "saved"),
        _content_sha(state.get("article") or {}),
    )
    return state


def _restore_editor_state(state: dict[str, Any], store: SQLiteStore) -> None:
    save_generation_task(state, expected_version=None, allow_terminal_recovery=True)
    store.update_task_edit_metadata(state["task_id"], int(state.get("article_revision") or 0), str(state.get("article_edit_status") or "saved"), _content_sha(state.get("article") or {}))


def get_article(task_id: str) -> dict[str, Any]:
    state = load_generation_task(task_id)
    draft = None
    path = _draft_path(task_id)
    if path.is_file():
        try:
            draft = sanitize_sensitive_data(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            draft = None
    article = sanitize_sensitive_data((state or {}).get("editing_article") or (draft if isinstance(draft, dict) else None) or _read_article(task_id))
    return {"article": sanitize_sensitive_data((state or {}).get("article") or _read_article(task_id)), "editing_article": article, "draft": draft, "revision": int((state or {}).get("article_revision") or 0), "edit_status": (state or {}).get("article_edit_status") or "saved", "versions": list_versions(generation_task_dir(task_id))}


def save_article_draft(task_id: str, changes: dict[str, Any], store: SQLiteStore | None = None) -> dict[str, Any]:
    store = store or get_store()
    with task_lock(task_id):
        state = load_generation_task(task_id)
        if not state:
            raise ProviderError("TASK_NOT_FOUND", "task not found")
        current = _read_editing_article(task_id, state)
        draft = _normalise_article_changes(current, sanitize_sensitive_data(changes))
        _write_json(_draft_path(task_id), draft)
        state["article_edit_status"] = "draft_saved"
        state["editing_article"] = draft
        state["article_draft_path"] = "article.draft.json"
        state["draft_updated_at"] = utc_now()
        _save_state(state, store)
        return {"article": draft, "revision": int(state.get("article_revision") or 0), "edit_status": "draft_saved"}


def save_article(task_id: str, changes: dict[str, Any] | None = None, store: SQLiteStore | None = None) -> dict[str, Any]:
    store = store or get_store()
    with task_lock(task_id):
        root = generation_task_dir(task_id)
        (root / ".attempts").mkdir(parents=True, exist_ok=True)
        state = load_generation_task(task_id)
        if not state:
            raise ProviderError("TASK_NOT_FOUND", "task not found")
        previous_state = sanitize_sensitive_data(state)
        current = _read_editing_article(task_id, state)
        draft_path = _draft_path(task_id)
        if changes is None and draft_path.is_file():
            try:
                changes = json.loads(draft_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ProviderError("ARTICLE_EDIT_INVALID", "草稿无法读取") from exc
        article = _normalise_article_changes(current, sanitize_sensitive_data(changes or {}))
        candidate = Path(tempfile.mkdtemp(prefix="article-edit-", dir=root / ".attempts"))
        try:
            _write_json(candidate / "article.json", article)
            (candidate / "article.md").write_text(str(article["content_markdown"]), encoding="utf-8")
            files = ["article.json", "article.md"]
            record = commit_candidate(root, candidate, files=files, defer_finalize=True, metadata={"task_id": task_id})
        finally:
            shutil.rmtree(candidate, ignore_errors=True)
        state["article"] = article
        state["editing_article"] = article
        state["article_revision"] = int(state.get("article_revision") or 0) + 1
        state["article_edit_status"] = "saved"
        state["article_updated_at"] = utc_now()
        state["article_draft_path"] = "article.draft.json"
        state["version_id"] = record.get("version_id")
        state["version_commit"] = {key: value for key, value in record.items() if key not in {"candidate_hashes", "rollback_hashes"}}
        draft_path.unlink(missing_ok=True)
        attempts_root = root / ".attempts" / str(record["attempt_root"])
        final_state = sanitize_sensitive_data(state)
        write_intended_state(attempts_root, {"task_id": task_id, "version_id": record.get("version_id"), "final_state": final_state, "previous_state": previous_state})
        record = update_commit_record(attempts_root, record, "committing_state")
        final_state["version_commit"] = {key: value for key, value in record.items() if key not in {"candidate_hashes", "rollback_hashes"}}
        state_saved = False
        try:
            _save_state(final_state, store)
            state_saved = True
            finalize_candidate(root, attempts_root, record)
        except Exception as exc:
            if state_saved and record.get("status") == "committing_state" and all((root / relative).is_file() for relative in record.get("candidate_hashes") or {}):
                return {"article": article, "revision": final_state["article_revision"], "edit_status": "saved", "version_id": record.get("version_id")}
            try:
                rollback_candidate(root, attempts_root, record)
                _restore_editor_state(previous_state, store)
            except Exception as rollback_error:
                raise VersionCommitError(f"article save failed and rollback failed: {rollback_error}") from exc
            raise VersionCommitError("article save failed; previous version restored") from exc
        return {"article": article, "revision": final_state["article_revision"], "edit_status": "saved", "version_id": record.get("version_id")}


def discard_article_draft(task_id: str, store: SQLiteStore | None = None) -> dict[str, Any]:
    store = store or get_store()
    with task_lock(task_id):
        state = load_generation_task(task_id)
        if not state:
            raise ProviderError("TASK_NOT_FOUND", "task not found")
        _draft_path(task_id).unlink(missing_ok=True)
        state["editing_article"] = _read_article(task_id)
        state["article_edit_status"] = "discarded"
        _save_state(state, store)
        return {"article": _read_article(task_id), "revision": int(state.get("article_revision") or 0), "edit_status": "discarded"}


def restore_article_version(task_id: str, version_id: str, store: SQLiteStore | None = None) -> dict[str, Any]:
    store = store or get_store()
    with task_lock(task_id):
        root = generation_task_dir(task_id)
        (root / ".attempts").mkdir(parents=True, exist_ok=True)
        version_root = root / ".versions" / version_id
        if not version_root.is_dir():
            raise ProviderError("ARTICLE_VERSION_NOT_FOUND", "文章版本不存在")
        previous_state = sanitize_sensitive_data(load_generation_task(task_id) or {})
        candidate = Path(tempfile.mkdtemp(prefix="article-restore-", dir=root / ".attempts"))
        try:
            files = ["article.json", "article.md"]
            for relative in files:
                source = version_root / relative
                if source.is_file():
                    _copy = candidate / relative
                    _copy.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, _copy)
            record = commit_candidate(root, candidate, files=files, defer_finalize=True, metadata={"task_id": task_id})
        finally:
            shutil.rmtree(candidate, ignore_errors=True)
        state = load_generation_task(task_id)
        if not state:
            raise ProviderError("TASK_NOT_FOUND", "task not found")
        state["article"] = sanitize_sensitive_data(json.loads((version_root / "article.json").read_text(encoding="utf-8")))
        state["editing_article"] = state["article"]
        state["article_revision"] = int(state.get("article_revision") or 0) + 1
        state["article_edit_status"] = "restored"
        state["version_id"] = record.get("version_id")
        _draft_path(task_id).unlink(missing_ok=True)
        attempts_root = root / ".attempts" / str(record["attempt_root"])
        final_state = sanitize_sensitive_data(state)
        write_intended_state(attempts_root, {"task_id": task_id, "version_id": record.get("version_id"), "final_state": final_state, "previous_state": previous_state})
        record = update_commit_record(attempts_root, record, "committing_state")
        final_state["version_commit"] = {key: value for key, value in record.items() if key not in {"candidate_hashes", "rollback_hashes"}}
        state_saved = False
        try:
            _save_state(final_state, store)
            state_saved = True
            finalize_candidate(root, attempts_root, record)
        except Exception as exc:
            if state_saved and record.get("status") == "committing_state" and all((root / relative).is_file() for relative in record.get("candidate_hashes") or {}):
                return {"article": final_state["article"], "revision": final_state["article_revision"], "edit_status": "restored", "version_id": record.get("version_id")}
            try:
                rollback_candidate(root, attempts_root, record)
                _restore_editor_state(previous_state, store)
            except Exception as rollback_error:
                raise VersionCommitError(f"article restore failed and rollback failed: {rollback_error}") from exc
            raise VersionCommitError("article restore failed; previous version restored") from exc
        return {"article": final_state["article"], "revision": final_state["article_revision"], "edit_status": "restored", "version_id": record.get("version_id")}
