from __future__ import annotations

import json
from pathlib import Path

import pytest

import generation.editor as editor
import modules.generation_store as generation_store
from modules.database import SQLiteStore
from modules.generation_store import generation_task_dir, load_generation_task, save_generation_task
from modules.models import HotTopic
from generation.versioning import commit_candidate, finalize_candidate, recover_version_commits


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def test_deferred_version_commit_reaches_completed_only_after_finalize(tmp_path):
    root = tmp_path / "task"
    candidate = tmp_path / "candidate"
    _write(root / "article.json", "old")
    _write(candidate / "article.json", "new")
    record = commit_candidate(root, candidate, files=["article.json"], defer_finalize=True, metadata={"task_id": "task-1"})
    assert record["status"] == "files_committed"
    commit_path = root / ".attempts" / record["attempt_root"] / "commit.json"
    assert json.loads(commit_path.read_text(encoding="utf-8"))["status"] == "files_committed"
    finalize_candidate(root, commit_path.parent, record)
    assert json.loads(commit_path.read_text(encoding="utf-8"))["status"] == "completed"


def test_files_committed_recovery_is_idempotent(tmp_path):
    root = tmp_path / "task"
    candidate = tmp_path / "candidate"
    _write(root / "article.json", "old")
    _write(candidate / "article.json", "new")
    record = commit_candidate(root, candidate, files=["article.json"], defer_finalize=True)
    assert recover_version_commits(root)[0]["status"] == "completed"
    assert recover_version_commits(root) == []
    assert (root / ".versions" / record["version_id"] / "version.json").is_file()


def test_rc12_editor_uses_fragment_and_one_editing_source():
    source = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    assert 'getattr(st, "fragment"' in source
    assert "rc1_edit_changes_" in source
    assert "恢复模型原稿" in source
    assert "保存失败，上一版本仍然保留" in source


def test_rc12_commercial_ui_uses_brand_and_hides_toolbar():
    app = Path("ui/rc1_app.py").read_text(encoding="utf-8") + Path("ui/theme.py").read_text(encoding="utf-8")
    shortcut = Path("create_shortcut.ps1").read_text(encoding="utf-8-sig")
    assert '[data-testid="stToolbar"]' in app
    assert 'ui" / "assets" / "logo-light.svg' in app
    assert 'IconLocation = "$Icon,0"' in shortcut
    assert 'Join-Path $Root $ShortcutName' not in shortcut


def test_rc12_migration_copies_only_user_exports(tmp_path, monkeypatch):
    import modules.app_paths as paths

    project = tmp_path / "program"
    target = tmp_path / "user"
    _write(project / "config" / "settings.json", '{"safe": true}')
    _write(project / "data" / "hotspot_agent.db", "db")
    _write(project / "export" / "__init__.py", "source")
    _write(project / "export" / "user" / "article.zip", "user export")
    _write(project / "data" / "license" / "installation.json", '{"installation_id":"legacy-id"}')
    _write(project / "data" / "license" / "installation.dat", "test-placeholder")
    _write(project / "data" / "license" / "installation.initialized", "{}")
    monkeypatch.setattr(paths, "PROJECT_ROOT", project)
    monkeypatch.setenv(paths.DATA_ENV, str(target))
    result = paths.migrate_legacy_data()
    assert result["migrated"] is True
    assert (target / "exports" / "article.zip").is_file()
    assert not (target / "exports" / "__init__.py").exists()


def test_rc12_portable_smoke_and_package_names_are_present():
    script = Path("scripts/rc1_2_windows_portable_smoke.ps1").read_text(encoding="utf-8-sig")
    package = Path("scripts/package_rc1.py").read_text(encoding="utf-8")
    assert "runtime\\python.exe" in script
    assert "PYTHONHOME" in script and "PYTHONPATH" in script
    assert "PORTABLE_SMOKE_PASS" in script
    assert "RC1.2_UI设计说明" in package or "*UI设计说明.md" in package


def _editor_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store = SQLiteStore(tmp_path / "db.sqlite")
    topic = HotTopic(id="rc12-topic", title="RC1.2 话题", summary="摘要", source_name="来源")
    store.save_topics([topic], record_observation=False)
    task = store.create_task("RC1.2 编辑任务", "multi_topic", [topic.to_dict()], 1)
    state = {"task_id": task["task_id"], "status": "completed", "stage": "completed", "state_version": 0, "article_revision": 0, "article_edit_status": "saved", "article": {"title": "旧标题", "intro": "旧导语", "summary": "旧导语", "sections": [{"heading": "旧小标题", "body": "旧正文"}], "content_markdown": "# 旧标题\n\n旧正文", "images": []}}
    save_generation_task(state)
    root = generation_task_dir(task["task_id"])
    _write(root / "article.json", json.dumps(state["article"], ensure_ascii=False))
    _write(root / "article.md", state["article"]["content_markdown"])
    return store, task, root


def test_task_json_failure_restores_previous_article(tmp_path, monkeypatch):
    store, task, root = _editor_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(editor, "_save_state", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("task snapshot failure")))
    with pytest.raises(Exception):
        editor.save_article(task["task_id"], {"title": "新标题", "sections": [{"heading": "新小标题", "body": "新正文"}]}, store)
    assert json.loads((root / "article.json").read_text(encoding="utf-8"))["title"] == "旧标题"
    assert load_generation_task(task["task_id"])["article"]["title"] == "旧标题"


def test_sqlite_failure_after_task_write_restores_previous_article(tmp_path, monkeypatch):
    store, task, root = _editor_fixture(tmp_path, monkeypatch)
    original = store.update_task_edit_metadata
    calls = {"count": 0}

    def fail_once(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("sqlite state failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "update_task_edit_metadata", fail_once)
    with pytest.raises(Exception):
        editor.save_article(task["task_id"], {"title": "新标题", "sections": [{"heading": "新小标题", "body": "新正文"}]}, store)
    assert json.loads((root / "article.json").read_text(encoding="utf-8"))["title"] == "旧标题"
    assert load_generation_task(task["task_id"])["article"]["title"] == "旧标题"
