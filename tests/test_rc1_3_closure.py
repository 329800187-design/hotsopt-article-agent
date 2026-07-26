from __future__ import annotations

import json
from pathlib import Path

import pytest

import modules.generation_store as generation_store
from generation.editor import get_article, save_article_draft
from generation.recovery import recover_interrupted_tasks
from generation.versioning import commit_candidate, update_commit_record, write_intended_state
from modules.database import SQLiteStore
from modules.generation_store import generation_task_dir, load_generation_task, save_generation_task
from modules.models import HotTopic


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _fixture(tmp_path: Path, monkeypatch) -> tuple[SQLiteStore, dict, Path]:
    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store = SQLiteStore(tmp_path / "db.sqlite")
    topic = HotTopic(id="rc13-topic", title="RC1.3 测试热点", summary="测试摘要", source="test", source_name="测试源", source_url="https://example.com/topic")
    store.save_topics([topic], record_observation=False)
    task = store.create_task("RC1.3 测试任务", "multi_topic", [topic.to_dict()], 1)
    state = {"task_id": task["task_id"], "status": "running", "stage": "committing_version", "state_version": 0, "version_id": "version-0001", "version_commit": {"version_id": "version-0001"}, "article": {"title": "旧版本", "sections": []}, "cover": {"status": "completed"}, "inline_images": [], "inline_image_summary": {"status": "completed"}, "quality_evidence": {"old": True}, "attempt_history": [{"attempt": 1, "status": "completed"}], "completed_at": "2026-07-19T00:00:00+00:00", "article_revision": 0, "article_edit_status": "saved", "paths": {"article_json": "article.json"}, "progress": 90}
    save_generation_task(state)
    store.update_task_status(task["task_id"], "running")
    root = generation_task_dir(task["task_id"])
    _write(root / "article.json", json.dumps(state["article"], ensure_ascii=False))
    return store, task, root


def _prepared_new_commit(store: SQLiteStore, task: dict, root: Path) -> tuple[dict, dict]:
    candidate = root.parent / "candidate"
    _write(candidate / "article.json", json.dumps({"title": "新版本"}, ensure_ascii=False))
    record = commit_candidate(root, candidate, files=["article.json"], defer_finalize=True, metadata={"task_id": task["task_id"]})
    previous = load_generation_task(task["task_id"])
    final = dict(previous or {})
    final.update({"status": "completed", "stage": "completed", "progress": 100, "version_id": record["version_id"], "version_commit": {"version_id": record["version_id"], "status": "committing_state"}, "completed_at": "2026-07-19T00:01:00+00:00"})
    write_intended_state(root / ".attempts" / record["attempt_root"], {"task_id": task["task_id"], "version_id": record["version_id"], "final_state": final, "previous_state": previous})
    update_commit_record(root / ".attempts" / record["attempt_root"], record, "committing_state")
    return record, final


def test_files_committed_recovery_reconciles_task_and_sqlite(tmp_path, monkeypatch):
    store, task, root = _fixture(tmp_path, monkeypatch)
    record, _ = _prepared_new_commit(store, task, root)
    report = recover_interrupted_tasks(store=store)
    state = load_generation_task(task["task_id"])
    assert any(item.get("status") == "completed" for item in report["recovered"] + report["skipped"])
    assert state["status"] == "completed"
    assert state["version_id"] == record["version_id"]
    assert store.get_task(task["task_id"])["status"] == "completed"
    assert json.loads((root / "article.json").read_text(encoding="utf-8"))["title"] == "新版本"


def test_corrupt_intended_state_rolls_back_all_formal_files(tmp_path, monkeypatch):
    store, task, root = _fixture(tmp_path, monkeypatch)
    candidate = root.parent / "candidate-corrupt"
    _write(candidate / "article.json", json.dumps({"title": "不应发布"}, ensure_ascii=False))
    record = commit_candidate(root, candidate, files=["article.json"], defer_finalize=True, metadata={"task_id": task["task_id"]})
    attempts = root / ".attempts" / record["attempt_root"]
    write_intended_state(attempts, {"task_id": task["task_id"]})
    update_commit_record(attempts, record, "committing_state")
    recover_interrupted_tasks(store=store)
    assert json.loads((root / "article.json").read_text(encoding="utf-8"))["title"] == "旧版本"
    assert load_generation_task(task["task_id"])["error_code"] == "VERSION_STATE_COMMIT_FAILED"
    assert store.get_task(task["task_id"])["status"] == "partial_success"


def test_recovery_is_idempotent_after_state_reconciliation(tmp_path, monkeypatch):
    store, task, root = _fixture(tmp_path, monkeypatch)
    _prepared_new_commit(store, task, root)
    first = recover_interrupted_tasks(store=store)
    second = recover_interrupted_tasks(store=store)
    assert first["recovery_failed"] == []
    assert second["recovery_failed"] == []
    assert load_generation_task(task["task_id"])["status"] == "completed"


def test_previous_result_contract_contains_full_version_metadata():
    from generation.single_task import _capture_previous_result

    state = {key: {"key": key} for key in ("article", "cover", "inline_images", "inline_image_summary", "version_id", "version_commit", "quality_evidence", "attempt_history", "completed_at", "article_revision", "article_edit_status", "paths", "status", "stage", "progress")}
    result = _capture_previous_result(state)
    assert set(state).issubset(result)


def test_editor_uses_persisted_editing_article_for_follow_up_changes(tmp_path, monkeypatch):
    store, task, root = _fixture(tmp_path, monkeypatch)
    state = load_generation_task(task["task_id"])
    state.update({"status": "completed", "stage": "completed", "article": {"title": "原稿", "intro": "导语", "summary": "导语", "sections": [{"heading": "第一段", "body": "正文"}], "content_markdown": "# 原稿"}, "editing_article": {"title": "原稿", "intro": "导语", "summary": "导语", "sections": [{"heading": "第一段", "body": "正文"}], "content_markdown": "# 原稿"}})
    save_generation_task(state, allow_terminal_recovery=True)
    store.update_task_status(task["task_id"], "completed")
    save_article_draft(task["task_id"], {"sections": [{"heading": "第一段", "body": "正文"}, {"heading": "新增", "body": "新增正文"}]}, store)
    data = get_article(task["task_id"])
    assert [item["heading"] for item in data["editing_article"]["sections"]] == ["第一段", "新增"]
    assert [item["heading"] for item in data["draft"]["sections"]] == ["第一段", "新增"]


def test_rc13_scripts_and_audit_contracts_exist():
    script = Path("scripts/rc1_3_windows_portable_smoke.ps1").read_text(encoding="utf-8-sig")
    package = Path("scripts/package_rc1.py").read_text(encoding="utf-8")
    assert "ProgramDir" in script and "LOCALAPPDATA" in script
    assert "PORTABLE_LOCALAPPDATA_PASS" in script
    assert "rc1-3" in package


def test_rc13_ui_uses_unified_editing_source():
    source = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    assert 'editing_key = f"editing_article_{task_id}"' in source
    assert "st.session_state[editing_key]" in source
    assert "st.dataframe" in source
