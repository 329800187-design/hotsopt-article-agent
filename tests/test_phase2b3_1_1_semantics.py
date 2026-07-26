from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

import generation.inline_images as inline_images
import generation.single_task as single_task
import modules.generation_store as generation_store
from generation.recovery import recover_interrupted_tasks
from modules.database import SQLiteStore
from modules.generation_store import generation_task_dir, load_generation_task, save_generation_task
from modules.models import HotTopic
from providers.text_provider import ProviderError


def _article(version: str) -> dict:
    return {
        "title": f"文章版本 {version}",
        "summary": "公开摘要",
        "content_markdown": f"正文版本 {version}，保持事实和结构。",
        "sections": [
            {"heading": f"{version} 小标题一", "body": "第一段正文", "image_brief": f"{version} 场景一"},
            {"heading": f"{version} 小标题二", "body": "第二段正文", "image_brief": f"{version} 场景二"},
            {"heading": f"{version} 小标题三", "body": "第三段正文", "image_brief": f"{version} 场景三"},
        ],
        "images": [],
    }


def _png(path: Path, color: str = "blue") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (128, 128), color).save(path, format="PNG")


class RewriteProvider:
    calls = 0
    fail_section_two = False

    def __init__(self, *args, **kwargs):
        self.last_response_type = "base64"

    def generate(self, prompt: str, output_path: Path) -> Path:
        type(self).calls += 1
        if type(self).fail_section_two and "小标题二" in prompt:
            raise ProviderError("NETWORK_ERROR", "inline failure")
        _png(output_path, "red" if "版本 v2" in prompt else "blue")
        return output_path


def _task_store(tmp_path: Path, monkeypatch) -> tuple[SQLiteStore, dict, Path]:
    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store = SQLiteStore(tmp_path / "db.sqlite")
    topic = HotTopic(id="semantic-topic", title="语义一致性热点", summary="摘要", source="test", source_name="测试源", source_url="https://example.com/topic")
    store.save_topics([topic])
    task = store.create_task("语义一致性任务", "multi_topic", [topic.to_dict()], 1, generation_options={"image_style": "动漫新闻插画"})
    return store, task, generation_task_dir(task["task_id"])


def _state(task_id: str, article: dict, status: str = "completed") -> dict:
    return {
        "task_id": task_id,
        "status": status,
        "stage": "completed" if status == "completed" else "generating_inline_images",
        "progress": 100 if status == "completed" else 75,
        "state_version": 0,
        "generation_options": {"image_style": "动漫新闻插画"},
        "article": article,
        "cover": {"status": "completed", "path": "images/cover.png"},
        "inline_images": [],
        "inline_image_summary": {"total": 0, "completed": 0, "failed": 0, "pending": 0, "status": "pending"},
        "paths": {},
        "cancellation_requested": False,
        "inline_operation": False,
    }


def _complete_first_version(store: SQLiteStore, task: dict, root: Path, monkeypatch) -> dict:
    RewriteProvider.calls = 0
    RewriteProvider.fail_section_two = False
    monkeypatch.setattr(single_task, "generate_article", lambda *args, **kwargs: _article("v1"))
    monkeypatch.setattr(single_task, "OpenAIImageProvider", RewriteProvider)
    result = single_task.run_single_task(task, {}, {"auth_type": "none"}, settings={"network": {}}, store=store)
    assert result["status"] == "completed"
    return load_generation_task(task["task_id"])


def _mark_rewrite(state: dict, store: SQLiteStore) -> dict:
    previous = {
        "article": state.get("article"),
        "cover": state.get("cover"),
        "inline_images": state.get("inline_images"),
        "inline_image_summary": state.get("inline_image_summary"),
    }
    state.update({"status": "queued", "stage": "queued", "progress": 0, "rewrite_requested": True, "previous_result": previous, "state_version": int(state.get("state_version") or 0) + 1})
    save_generation_task(state, expected_version=state["state_version"] - 1)
    store.update_task_status(state["task_id"], "queued")
    return state


def test_full_rewrite_replans_and_calls_inline_provider(tmp_path, monkeypatch):
    store, task, root = _task_store(tmp_path, monkeypatch)
    old = _complete_first_version(store, task, root, monkeypatch)
    old_prompts = {item["image_id"]: item["prompt"] for item in old["inline_images"]}
    _mark_rewrite(old, store)
    RewriteProvider.calls = 0
    monkeypatch.setattr(single_task, "generate_article", lambda *args, **kwargs: _article("v2"))
    result = single_task.run_single_task(task, {}, {"auth_type": "none"}, settings={"network": {}}, store=store)
    assert result["status"] == "completed"
    assert RewriteProvider.calls == 4
    assert result["article"]["title"] == "文章版本 v2"
    assert all("v2" in item["section_title"] for item in result["inline_images"])
    assert all(old_prompts[item["image_id"]] != item["prompt"] for item in result["inline_images"])
    assert all(item["status"] == "completed" for item in result["inline_images"])


def test_failed_rewrite_keeps_old_version_and_does_not_promote_old_images(tmp_path, monkeypatch):
    store, task, root = _task_store(tmp_path, monkeypatch)
    old = _complete_first_version(store, task, root, monkeypatch)
    old_article = (root / "article.json").read_bytes()
    old_images = {item["image_id"]: (root / item["path"]).read_bytes() for item in old["inline_images"]}
    _mark_rewrite(old, store)
    RewriteProvider.fail_section_two = True
    monkeypatch.setattr(single_task, "generate_article", lambda *args, **kwargs: _article("v2"))
    result = single_task.run_single_task(task, {}, {"auth_type": "none"}, settings={"network": {}}, store=store)
    assert result["status"] == "partial_success"
    assert result["new_version_status"] == "partial_success"
    assert result["fallback_notice"]
    assert (root / "article.json").read_bytes() == old_article
    assert all((root / item["path"]).read_bytes() == old_images[item["image_id"]] for item in result["inline_images"])
    assert all("v1" in item["section_title"] for item in result["inline_images"])


def test_cancel_during_first_inline_request_does_not_start_following_images(tmp_path, monkeypatch):
    store, task, root = _task_store(tmp_path, monkeypatch)
    state = _state(task["task_id"], _article("cancel"))
    root.mkdir(parents=True, exist_ok=True)
    save_generation_task(state)
    store.update_task_status(task["task_id"], "completed")
    started = threading.Event()
    release = threading.Event()

    class SlowProvider:
        calls = 0
        last_response_type = "base64"

        def __init__(self, *args, **kwargs):
            pass

        def generate(self, prompt, output_path):
            type(self).calls += 1
            started.set()
            release.wait(2)
            _png(output_path)

    monkeypatch.setattr(inline_images, "OpenAIImageProvider", SlowProvider)
    result_holder: list[dict] = []
    worker = threading.Thread(target=lambda: result_holder.append(inline_images.run_inline_images(task["task_id"], {}, store=store)))
    worker.start()
    assert started.wait(1)
    start = time.monotonic()
    requested = __import__("generation.single_task", fromlist=["cancel_single_task"]).cancel_single_task(task["task_id"], store)
    elapsed = time.monotonic() - start
    release.set()
    worker.join(3)
    assert elapsed < 1
    assert requested["cancellation_requested"] is True
    assert not worker.is_alive()
    assert SlowProvider.calls == 1
    assert result_holder[0]["status"] == "cancelled"
    assert all(item["status"] != "generating" for item in result_holder[0]["inline_images"])


def test_retry_synchronizes_state_article_json_assets_and_sqlite(tmp_path, monkeypatch):
    store, task, root = _task_store(tmp_path, monkeypatch)
    state = _state(task["task_id"], _article("sync"))
    save_generation_task(state)
    store.update_task_status(task["task_id"], "completed")
    RewriteProvider.calls = 0
    RewriteProvider.fail_section_two = False
    monkeypatch.setattr(inline_images, "OpenAIImageProvider", RewriteProvider)
    result = inline_images.run_inline_images(task["task_id"], {}, store=store)
    saved = load_generation_task(task["task_id"])
    article = json.loads((root / "article.json").read_text(encoding="utf-8"))
    assets = json.loads((root / "images" / "assets.json").read_text(encoding="utf-8"))
    sqlite_status = store.get_task(task["task_id"])["status"]
    assert result["status"] == saved["status"] == sqlite_status == "completed"
    assert [item["image_id"] for item in saved["inline_images"]] == [item["image_id"] for item in article["images"] if item.get("role") == "inline"] == [item["image_id"] for item in assets["assets"]]
    for item in saved["inline_images"]:
        formal = root / item["path"]
        assert item["metadata"]["sha256"] == __import__("hashlib").sha256(formal.read_bytes()).hexdigest()


def test_regenerate_all_failure_sets_partial_then_retry_restores_completed(tmp_path, monkeypatch):
    store, task, root = _task_store(tmp_path, monkeypatch)
    state = _state(task["task_id"], _article("all"))
    save_generation_task(state)
    store.update_task_status(task["task_id"], "completed")
    RewriteProvider.calls = 0
    RewriteProvider.fail_section_two = True
    monkeypatch.setattr(inline_images, "OpenAIImageProvider", RewriteProvider)
    failed = inline_images.run_inline_images(task["task_id"], {}, store=store, regenerate_all=True)
    assert failed["status"] == "partial_success"
    assert failed["failed_step"] == "generating_inline_images"
    assert store.get_task(task["task_id"])["status"] == "partial_success"
    RewriteProvider.fail_section_two = False
    recovered = inline_images.run_inline_images(task["task_id"], {}, store=store)
    assert recovered["status"] == "completed"
    assert recovered["inline_image_summary"]["status"] == "completed"
    assert store.get_task(task["task_id"])["status"] == "completed"


def _recovery_case(tmp_path: Path, monkeypatch, assets: list[dict], status: str = "running") -> tuple[SQLiteStore, dict, Path]:
    store, task, root = _task_store(tmp_path, monkeypatch)
    state = _state(task["task_id"], _article("recover"), status)
    state["inline_images"] = assets
    state["inline_image_summary"] = {"total": len(assets), "completed": sum(a["status"] == "completed" for a in assets), "failed": 0, "pending": sum(a["status"] == "pending" for a in assets), "status": "partial_success"}
    save_generation_task(state)
    store.update_task_status(task["task_id"], status)
    return store, task, root


def _asset(image_id: str, status: str) -> dict:
    return {"image_id": image_id, "role": "inline", "order": int(image_id[-1]), "paragraph_ref": image_id, "section_title": image_id, "prompt": image_id, "status": status, "path": f"images/{image_id}.png", "file_path": f"images/{image_id}.png", "metadata": {}, "attempt_count": 1, "fallback_available": False}


def test_recovery_generating_without_formal_file_becomes_failed(tmp_path, monkeypatch):
    store, task, root = _recovery_case(tmp_path, monkeypatch, [_asset("section-1", "generating")])
    report = recover_interrupted_tasks(store=store)
    state = load_generation_task(task["task_id"])
    assert report["recovered"]
    assert state["inline_images"][0]["status"] == "failed"
    assert state["inline_images"][0]["error_code"] == "TASK_INTERRUPTED"
    assert state["status"] == store.get_task(task["task_id"])["status"] == "partial_success"


def test_recovery_generating_with_valid_formal_file_becomes_completed(tmp_path, monkeypatch):
    store, task, root = _recovery_case(tmp_path, monkeypatch, [_asset("section-1", "generating")])
    _png(root / "images" / "section-1.png")
    recover_interrupted_tasks(store=store)
    state = load_generation_task(task["task_id"])
    assert state["inline_images"][0]["status"] == "completed"
    assert state["status"] == store.get_task(task["task_id"])["status"] == "completed"


def test_recovery_mixed_assets_is_idempotent_and_preserves_completed(tmp_path, monkeypatch):
    assets = [_asset("section-1", "completed"), _asset("section-2", "generating"), _asset("section-3", "pending")]
    store, task, root = _recovery_case(tmp_path, monkeypatch, assets)
    _png(root / "images" / "section-1.png", "red")
    first = recover_interrupted_tasks(store=store)
    second = recover_interrupted_tasks(store=store)
    state = load_generation_task(task["task_id"])
    assert state["inline_images"][0]["status"] == "completed"
    assert state["inline_images"][1]["status"] == "failed"
    assert state["inline_images"][2]["status"] == "pending"
    assert state["status"] == "partial_success"
    assert second["recovery_failed"] == []
    assert not list(root.glob(".attempts/**/raw"))
