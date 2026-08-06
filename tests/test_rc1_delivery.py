from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from export.docx_exporter import export_article
from export.zip_exporter import export_article_bundle, safe_filename
from generation.editor import discard_article_draft, get_article, restore_article_version, save_article, save_article_draft
from generation.versioning import commit_candidate, recover_version_commits
from modules.database import SQLiteStore
from modules.generation_store import generation_task_dir, save_generation_task
from modules.models import HotTopic


def make_store(tmp_path: Path) -> tuple[SQLiteStore, dict, Path]:
    store = SQLiteStore(tmp_path / "rc1.db")
    store.init_schema()
    topic = HotTopic(id="rc1-topic", title="RC1 测试热点", summary="公开摘要", source_name="测试来源")
    store.save_topics([topic], record_observation=False)
    task = store.create_task("RC1 测试任务", "multi_topic", [topic.to_dict()], 1, generation_options={"article_type": "热点资讯"})
    root = generation_task_dir(task["task_id"])
    state = {"task_id": task["task_id"], "status": "completed", "stage": "completed", "state_version": 0, "article_revision": 0, "article_edit_status": "saved", "article": {"title": "原始标题", "intro": "原始导语", "summary": "原始导语", "sections": [{"heading": "原始小标题", "body": "原始正文"}], "content_markdown": "# 原始标题\n\n原始正文", "images": []}}
    save_generation_task(state)
    (root / "article.json").write_text(json.dumps(state["article"], ensure_ascii=False), encoding="utf-8")
    (root / "article.md").write_text(state["article"]["content_markdown"], encoding="utf-8")
    return store, task, root


def test_version_commit_records_manifest_and_replaces_candidate(tmp_path):
    root = tmp_path / "task"
    candidate = root / ".attempts" / "candidate"
    candidate.mkdir(parents=True)
    (root / "article.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "article.json").write_text("old", encoding="utf-8")
    (candidate / "article.json").write_text("new", encoding="utf-8")
    record = commit_candidate(root, candidate, files=["article.json"])
    assert record["status"] == "committed"
    assert (root / "article.json").read_text(encoding="utf-8") == "new"
    assert (root / ".attempts" / record["attempt_root"] / "commit.json").exists()
    assert (root / ".versions" / record["version_id"] / "article.json").read_text(encoding="utf-8") == "new"


def test_interrupted_version_commit_rolls_back(tmp_path):
    root = tmp_path / "task"
    attempt = root / ".attempts" / "commit-interrupted"
    (attempt / "candidate").mkdir(parents=True)
    (attempt / "rollback").mkdir(parents=True)
    (root / "article.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "article.json").write_text("mixed", encoding="utf-8")
    (attempt / "candidate" / "article.json").write_text("new", encoding="utf-8")
    (attempt / "rollback" / "article.json").write_text("old", encoding="utf-8")
    (attempt / "commit.json").write_text(json.dumps({"status": "committing", "files": ["article.json"], "candidate_hashes": {"article.json": "wrong"}}), encoding="utf-8")
    result = recover_version_commits(root)
    assert result[0]["status"] == "rolled_back"
    assert (root / "article.json").read_text(encoding="utf-8") == "old"


def test_article_draft_save_discard_and_persist(tmp_path):
    store, task, root = make_store(tmp_path)
    draft = save_article_draft(task["task_id"], {"title": "草稿标题", "sections": [{"heading": "草稿小标题", "body": "草稿正文"}]}, store)
    assert draft["edit_status"] == "draft_saved"
    assert json.loads((root / "article.draft.json").read_text(encoding="utf-8"))["title"] == "草稿标题"
    saved = save_article(task["task_id"], None, store)
    assert saved["article"]["title"] == "草稿标题"
    assert store.get_task(task["task_id"])["article_revision"] == 1
    assert get_article(task["task_id"])["article"]["title"] == "草稿标题"
    discarded = discard_article_draft(task["task_id"], store)
    assert discarded["edit_status"] == "discarded"


def test_restore_previous_article_version(tmp_path):
    store, task, root = make_store(tmp_path)
    first = save_article(task["task_id"], {"title": "第一版", "sections": [{"heading": "一", "body": "正文一"}]}, store)
    second = save_article(task["task_id"], {"title": "第二版", "sections": [{"heading": "二", "body": "正文二"}]}, store)
    versions = get_article(task["task_id"])["versions"]
    assert len(versions) >= 2
    restored = restore_article_version(task["task_id"], versions[-1]["version_id"], store)
    assert restored["article"]["title"] == "第一版"


def test_word_export_and_article_zip_are_user_facing(tmp_path):
    store, task, root = make_store(tmp_path)
    article = get_article(task["task_id"])["article"]
    docx = export_article(article, tmp_path / "result.docx", root)
    assert docx.exists()
    archive_path = export_article_bundle(article, root, tmp_path / "result.zip")
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        assert all("\\" not in name and not name.startswith("/") for name in names)
        assert any(name.endswith(".docx") for name in names)
        assert not any("task.json" in name or "api_key" in name for name in names)


@pytest.mark.parametrize("value,expected", [("正常中文标题", "正常中文标题"), ("a:b/cd*e", "a_b_cd_e"), ("a?b", "a_b"), ("...", "文章")])
def test_safe_export_filename(value, expected):
    assert safe_filename(value) == expected
