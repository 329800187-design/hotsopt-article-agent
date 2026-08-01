from __future__ import annotations

import json
import threading
from contextlib import nullcontext
from pathlib import Path

import pytest
from PIL import Image

from generation.recovery import recover_interrupted_tasks
from generation.single_task import prepare_generation_state, run_single_task
from modules.database import SQLiteStore
from modules.generation_store import generation_task_dir, load_generation_task, save_generation_task
from modules.models import HotTopic
from providers.text_provider import ProviderError


def make_task(tmp_path: Path, options: dict | None = None) -> tuple[SQLiteStore, dict]:
    store = SQLiteStore(tmp_path / "db.sqlite")
    topic = HotTopic(id="recovery-topic", title="恢复测试热点", summary="摘要", source="test", source_name="测试", source_url="https://example.com/topic")
    store.save_topics([topic])
    return store, store.create_task("恢复任务", "multi_topic", [topic.to_dict()], 1, generation_options=options or {})


def running_state(store: SQLiteStore, task: dict, stage: str) -> dict:
    state = prepare_generation_state(task, {}, {}, store=store)
    state.update({"status": "running", "stage": stage, "progress": 50})
    save_generation_task(state)
    store.update_task_status(task["task_id"], "running")
    return state


def write_valid_article(task_id: str) -> None:
    path = generation_task_dir(task_id)
    path.mkdir(parents=True, exist_ok=True)
    (path / "article.json").write_text(json.dumps({"title": "已保存文章", "summary": "摘要", "content_markdown": "正文"}, ensure_ascii=False), encoding="utf-8")


def test_retry_waiting_cancel_is_finally_persisted_cancelled(tmp_path, monkeypatch):
    import generation.executor as executor_module
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store, task = make_task(tmp_path)
    prepare_generation_state(task, {}, {}, store=store)
    executor = executor_module.GenerationExecutor(max_workers=1)
    calls: list[int] = []
    waiting = threading.Event()

    def fake_run(*args, **kwargs):
        calls.append(1)
        return {"task_id": task["task_id"], "status": "failed", "error_code": "NETWORK_ERROR", "failed_step": "generating_article"}

    def wait_for_cancel(task_id: str, seconds: int) -> bool:
        waiting.set()
        while not executor_module.is_cancel_requested(task_id):
            waiting.wait(0.01)
        return False

    monkeypatch.setattr(executor_module, "run_single_task", fake_run)
    monkeypatch.setattr(executor_module.GenerationExecutor, "_sleep_or_cancel", staticmethod(wait_for_cancel))
    future = executor.submit(task["task_id"], lambda: executor.execute_with_retry(task, {}, {}, {"max_auto_retries": 2}, store))
    assert waiting.wait(1)
    requested = executor.cancel(task["task_id"], store)
    result = future.result(timeout=2)
    persisted = load_generation_task(task["task_id"])
    assert requested["cancellation_requested"] is True
    assert result["status"] == "cancelled"
    assert persisted["status"] == "cancelled"
    assert persisted["next_retry_at"] is None
    assert store.get_task(task["task_id"])["status"] == "cancelled"
    assert len(calls) == 1
    assert not executor.is_running(task["task_id"])
    executor.pool.shutdown(wait=True)


def test_recovery_marks_article_stage_interrupted_and_retryable(tmp_path, monkeypatch):
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store, task = make_task(tmp_path)
    running_state(store, task, "generating_article")
    recovered = recover_interrupted_tasks(store)
    state = recovered[0]
    assert state["status"] == "failed"
    assert state["stage"] == "interrupted"
    assert state["error_code"] == "TASK_INTERRUPTED"
    assert state["failed_step"] == "generating_article"
    assert state["retryable"] is True
    assert store.get_task(task["task_id"])["status"] == "failed"


def test_recovery_marks_saved_article_partial_and_cover_retry(tmp_path, monkeypatch):
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store, task = make_task(tmp_path)
    running_state(store, task, "generating_cover")
    write_valid_article(task["task_id"])
    recovered = recover_interrupted_tasks(store)
    assert recovered[0]["status"] == "partial_success"
    assert recovered[0]["failed_step"] == "generating_cover"
    assert recovered[0]["next_retry_at"] is None


def test_recovery_cancellation_and_completed_are_immutable(tmp_path, monkeypatch):
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store, cancelled_task = make_task(tmp_path)
    cancelled = running_state(store, cancelled_task, "generating_article")
    cancelled["cancellation_requested"] = True
    save_generation_task(cancelled)
    _, completed_task = make_task(tmp_path / "completed")
    completed = prepare_generation_state(completed_task, {}, {}, store=SQLiteStore(tmp_path / "completed" / "db.sqlite"))
    completed["status"] = "completed"
    save_generation_task(completed)
    recovered = recover_interrupted_tasks(store)
    assert any(item["task_id"] == cancelled_task["task_id"] and item["status"] == "cancelled" for item in recovered["recovered"])
    assert load_generation_task(completed_task["task_id"])["status"] == "completed"


def test_stale_running_api_task_is_recovered_before_run(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import api
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store, task = make_task(tmp_path)
    running_state(store, task, "generating_article")
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "load_settings", lambda: {
        "text_profile": {"model": "text-model", "base_url": "https://example.invalid/v1", "endpoint": "/chat/completions"},
        "image_profile": {},
        "network": {},
        "verified_text_model": "text-model",
        "verified_text_base_url": "https://example.invalid/v1",
        "verified_text_endpoint": "/chat/completions",
    })

    class IdleExecutor:
        def is_running(self, task_id):
            return False

        def task_lock(self, task_id):
            return nullcontext()

        def submit(self, task_id, function):
            return object()

    monkeypatch.setattr(api, "executor", IdleExecutor())
    response = TestClient(api.app).post(f"/api/tasks/{task['task_id']}/run")
    assert response.status_code == 202
    assert response.json()["data"]["status"] == "queued"
    assert load_generation_task(task["task_id"])["status"] == "queued"


def test_api_persists_generation_options_and_rejects_invalid_values(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import api
    from hot_sources.service import HotTrendService
    from modules.config_store import load_settings

    store, task = make_task(tmp_path)
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "service", HotTrendService(load_settings(), store=store))
    payload = {
        "task_name": "参数任务",
        "mode": "multi_topic",
        "topic_ids": ["recovery-topic"],
        "article_count": 1,
        "generation_options": {"article_type": "社会民生", "style": "专业分析", "image_style": "二维国漫新闻插画", "word_count": 1200},
    }
    client = TestClient(api.app)
    response = client.post("/api/tasks", json=payload)
    assert response.status_code == 201
    created = response.json()["data"]
    assert created["generation_options"] == payload["generation_options"]
    assert store.get_task(created["task_id"])["generation_options"] == payload["generation_options"]
    invalid = dict(payload)
    invalid["generation_options"] = {**payload["generation_options"], "word_count": 999}
    invalid_response = client.post("/api/tasks", json=invalid)
    assert invalid_response.status_code == 201
    assert invalid_response.json()["data"]["generation_options"]["word_count"] == 1200


def test_background_generation_uses_persisted_options(tmp_path, monkeypatch):
    import generation.single_task as single_task
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    options = {"article_type": "社会民生", "style": "专业分析", "image_style": "二维国漫新闻插画", "word_count": 1200, "image_plan_mode": "standard", "image_generation_requested": True}
    store, task = make_task(tmp_path, options)
    def filler(seed: int, length: int = 320) -> str:
        return "".join(chr(0x4E00 + ((seed * 3001 + index * (61 + seed * 2)) % 20000)) for index in range(length))

    sections = [
        {"heading": "\u4e8b\u5b9e\u68b3\u7406", "body": "\u4e8b\u5b9e\u68b3\u7406\u4ea4\u4ee3\u5df2\u786e\u8ba4\u4fe1\u606f\u548c\u516c\u5f00\u6765\u6e90\u8fb9\u754c\u3002" + filler(51), "image_brief": "\u73b0\u573a"},
        {"heading": "\u5f71\u54cd\u5206\u6790", "body": "\u5f71\u54cd\u5206\u6790\u8bf4\u660e\u8bfb\u8005\u3001\u673a\u6784\u548c\u540e\u7eed\u6d41\u7a0b\u53ef\u80fd\u53d7\u5230\u7684\u53d8\u5316\u3002" + filler(52), "image_brief": "\u73b0\u573a"},
        {"heading": "\u6838\u9a8c\u8def\u5f84", "body": "\u6838\u9a8c\u8def\u5f84\u63d0\u9192\u7ee7\u7eed\u67e5\u770b\u53d1\u5e03\u65f6\u95f4\u3001\u4e3b\u4f53\u548c\u6743\u5a01\u56de\u5e94\u3002" + filler(53), "image_brief": "\u73b0\u573a"},
    ]
    monkeypatch.setattr(single_task, "generate_article", lambda *args, **kwargs: {"title": "标题", "intro": "这是一段结构完整的测试导语，用来确认持久化选项会进入后台生成。", "sections": sections, "content_markdown": "# 标题\n\n" + "\n\n".join(f"## {s['heading']}\n{s['body']}" for s in sections), "demo_mode": False})

    class FakeImageProvider:
        last_response_type = "base64"

        def __init__(self, *args, **kwargs):
            pass

        def generate(self, prompt, output_path):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (128, 128), (20, 80, 160)).save(output_path, format="PNG")

    monkeypatch.setattr(single_task, "OpenAIImageProvider", FakeImageProvider)
    result = run_single_task(task, {}, {}, settings={"phase2a_article_type": "热点资讯", "phase2a_style": "客观通俗", "phase2a_word_count": 800}, store=store)
    assert result["status"] == "completed"
    assert result["article"]["article_type"] == "社会民生"
    assert result["article"]["style"] == "专业分析"
    assert result["article"]["word_count"] == 1200
    assert "二维国漫新闻插画" in result["cover"]["prompt"]


def test_manual_retry_reads_original_persisted_options(tmp_path, monkeypatch):
    import generation.executor as executor_module
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    options = {"article_type": "社会民生", "style": "专业分析", "image_style": "国风 3D 新闻插画", "word_count": 1500}
    store, task = make_task(tmp_path, options)
    prepare_generation_state(task, {}, {}, store=store)
    executor = executor_module.GenerationExecutor(max_workers=1)
    seen: list[dict] = []

    def fake_run(task_value, *args, **kwargs):
        seen.append(dict(task_value["generation_options"]))
        if len(seen) == 1:
            return {"task_id": task["task_id"], "status": "failed", "error_code": "NETWORK_ERROR", "failed_step": "generating_article"}
        return {"task_id": task["task_id"], "status": "completed"}

    monkeypatch.setattr(executor_module, "run_single_task", fake_run)
    monkeypatch.setattr(executor_module.GenerationExecutor, "_sleep_or_cancel", staticmethod(lambda task_id, seconds: True))
    result = executor.execute_with_retry(task, {}, {}, {"max_auto_retries": 1}, store)
    assert result["status"] == "completed"
    assert seen == [options, options]
    executor.pool.shutdown(wait=True)


def test_task_snapshot_does_not_persist_api_key(tmp_path, monkeypatch):
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store, task = make_task(tmp_path, {"article_type": "热点资讯", "api_key": "TOP_SECRET"})
    state = prepare_generation_state(task, {"api_key": "TEXT_SECRET"}, {"api_key": "IMAGE_SECRET"}, store=store)
    text = (generation_task_dir(task["task_id"]) / "task.json").read_text(encoding="utf-8")
    assert "TOP_SECRET" not in text
    assert "TEXT_SECRET" not in text
    assert "IMAGE_SECRET" not in text
    assert "api_key" not in json.dumps(state.get("model_info"), ensure_ascii=False)


def test_recovery_records_reason_and_time(tmp_path, monkeypatch):
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store, task = make_task(tmp_path)
    running_state(store, task, "retry_waiting")
    state = load_generation_task(task["task_id"])
    state["failed_step"] = "generating_article"
    state["next_retry_at"] = "2099-01-01T00:00:00+00:00"
    save_generation_task(state)
    recovered = recover_interrupted_tasks(store)[0]
    assert recovered["status"] == "failed"
    assert recovered["next_retry_at"] is None
    assert recovered["recovery_time"]
    assert "no active executor future" in recovered["recovery_reason"]


def test_ui_history_uses_result_api_and_no_expired_copy():
    text = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    assert "查看全文" in text
    assert "阶段一只创建待生成任务" not in text
    assert "1 张封面 + 1～2 张正文配图" not in text
    assert "差异检查暂未完成" in text
