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
    def filler(seed: int, length: int = 260) -> str:
        return "".join(chr(0x4E00 + ((seed * 3001 + index * (41 + seed * 2)) % 20000)) for index in range(length))

    intro = f"\u8fd9\u662f {version} \u7248\u672c\u7684\u6d4b\u8bd5\u5bfc\u8bed\uff0c\u7528\u6765\u786e\u8ba4\u5168\u6587\u91cd\u5199\u65f6\u6587\u7ae0\u3001\u5c01\u9762\u548c\u6b63\u6587\u56fe\u7247\u90fd\u80fd\u4fdd\u6301\u4e00\u81f4\u3002"
    sections = [
        {"heading": f"{version} \u4e8b\u5b9e\u68b3\u7406", "body": f"{version} \u4e8b\u5b9e\u68b3\u7406\u8bf4\u660e\u516c\u5f00\u8d44\u6599\u3001\u53d1\u5e03\u4e3b\u4f53\u548c\u6765\u6e90\u8fb9\u754c\u3002" + filler(11), "image_brief": f"{version} \u573a\u666f\u4e00"},
        {"heading": f"{version} \u80cc\u666f\u89e3\u91ca", "body": f"{version} \u80cc\u666f\u89e3\u91ca\u533a\u5206\u4e8b\u5b9e\u3001\u89c2\u70b9\u548c\u540e\u7eed\u9700\u8981\u6838\u9a8c\u7684\u4fe1\u606f\u3002" + filler(12), "image_brief": f"{version} \u573a\u666f\u4e8c"},
        {"heading": f"{version} \u4f20\u64ad\u98ce\u9669", "body": f"{version} \u4f20\u64ad\u98ce\u9669\u63d0\u9192\u8bfb\u8005\u4fdd\u7559\u5224\u65ad\uff0c\u5e76\u7b49\u5f85\u6743\u5a01\u6765\u6e90\u66f4\u65b0\u3002" + filler(13), "image_brief": f"{version} \u573a\u666f\u4e09"},
        {"heading": f"{version} \u5f71\u54cd\u5206\u6790", "body": f"{version} \u5f71\u54cd\u5206\u6790\u5173\u6ce8\u6d41\u7a0b\u3001\u7528\u6237\u548c\u540e\u7eed\u5904\u7f6e\u7684\u53ef\u80fd\u53d8\u5316\u3002" + filler(14), "image_brief": f"{version} \u573a\u666f\u56db"},
    ]
    return {"title": f"\u6587\u7ae0\u7248\u672c {version}", "intro": intro, "summary": intro, "content_markdown": "# \u6587\u7ae0\u7248\u672c " + version + "\n\n" + intro + "\n\n" + "\n\n".join(f"## {s['heading']}\n{s['body']}" for s in sections), "sections": sections, "images": []}


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
        if type(self).fail_section_two and type(self).calls >= 2:
            raise ProviderError("NETWORK_ERROR", "inline failure")
        _png(output_path, "red" if "版本 v2" in prompt else "blue")
        return output_path


def _task_store(tmp_path: Path, monkeypatch) -> tuple[SQLiteStore, dict, Path]:
    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store = SQLiteStore(tmp_path / "db.sqlite")
    topic = HotTopic(id="semantic-topic", title="语义一致性热点", summary="摘要", source="test", source_name="测试源", source_url="https://example.com/topic")
    store.save_topics([topic])
    task = store.create_task(
        "语义一致性任务",
        "multi_topic",
        [topic.to_dict()],
        1,
        generation_options={"image_style": "动漫新闻插画", "image_plan_mode": "standard", "image_generation_requested": True},
    )
    return store, task, generation_task_dir(task["task_id"])


def _state(task_id: str, article: dict, status: str = "completed") -> dict:
    return {
        "task_id": task_id,
        "status": status,
        "stage": "completed" if status == "completed" else "generating_inline_images",
        "progress": 100 if status == "completed" else 75,
        "state_version": 0,
        "generation_options": {"image_style": "动漫新闻插画", "image_plan_mode": "standard", "image_generation_requested": True},
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
    assert RewriteProvider.calls == 2
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
    assert result["new_version_status"] == "failed"
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
