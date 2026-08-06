from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from modules.database import SQLiteStore
from modules.generation_store import load_generation_task
from modules.models import HotTopic
from providers.contracts import ArticleGenerationRequest
from providers.image_provider import OpenAIImageProvider, ProviderError, inspect_image
from providers.text_provider import OpenAITextProvider, _headers


def article_value() -> dict:
    def filler(seed: int, length: int = 260) -> str:
        return "".join(chr(0x4E00 + ((seed * 3001 + index * (37 + seed * 2)) % 20000)) for index in range(length))

    intro = "\u8fd9\u662f\u4e00\u6bb5\u7528\u4e8e\u6d4b\u8bd5\u7684\u6587\u7ae0\u5bfc\u8bed\uff0c\u8bf4\u660e\u4e8b\u4ef6\u80cc\u666f\u3001\u5199\u4f5c\u89d2\u5ea6\u548c\u8bfb\u8005\u9700\u8981\u5173\u6ce8\u7684\u6838\u5fc3\u4fe1\u606f\u3002"
    sections = [
        {"heading": "\u4e8b\u5b9e\u68b3\u7406", "body": "\u4e8b\u5b9e\u68b3\u7406\u4fdd\u7559\u6765\u6e90\u3001\u65f6\u95f4\u3001\u4e3b\u4f53\u548c\u516c\u5f00\u4fe1\u606f\u8fb9\u754c\uff0c\u907f\u514d\u628a\u7247\u6bb5\u5185\u5bb9\u5f53\u6210\u5b8c\u6574\u7ed3\u8bba\u3002" + filler(1), "image_brief": "\u65b0\u95fb\u73b0\u573a"},
        {"heading": "\u6838\u9a8c\u8def\u5f84", "body": "\u6838\u9a8c\u8def\u5f84\u8981\u5bf9\u7167\u539f\u59cb\u94fe\u63a5\u3001\u53d1\u5e03\u4e3b\u4f53\u548c\u4e0d\u540c\u6765\u6e90\uff0c\u5e2e\u52a9\u8bfb\u8005\u786e\u8ba4\u53ef\u4fe1\u606f\u3002" + filler(2), "image_brief": "\u8d44\u6599\u6838\u9a8c"},
        {"heading": "\u5f71\u54cd\u5206\u6790", "body": "\u5f71\u54cd\u5206\u6790\u5173\u6ce8\u666e\u901a\u8bfb\u8005\u3001\u76f8\u5173\u673a\u6784\u548c\u540e\u7eed\u6d41\u7a0b\uff0c\u4f46\u4e0d\u628a\u89c2\u70b9\u5199\u6210\u786c\u4e8b\u5b9e\u3002" + filler(3), "image_brief": "\u4eba\u7269\u601d\u8003"},
        {"heading": "\u540e\u7eed\u5173\u6ce8", "body": "\u540e\u7eed\u5173\u6ce8\u6743\u5a01\u6765\u6e90\u662f\u5426\u8865\u5145\u8bf4\u660e\uff0c\u5e76\u63d0\u9192\u8bfb\u8005\u5728\u4fe1\u606f\u6838\u5b9e\u540e\u518d\u5f62\u6210\u5224\u65ad\u3002" + filler(4), "image_brief": "\u516c\u5f00\u4fe1\u606f"},
    ]
    markdown = "# \u771f\u5b9e\u6a21\u578b\u6587\u7ae0\u6807\u9898\n\n" + intro + "\n\n" + "\n\n".join(f"## {s['heading']}\n{s['body']}" for s in sections)
    return {"title": "\u771f\u5b9e\u6a21\u578b\u6587\u7ae0\u6807\u9898", "intro": intro, "sections": sections, "content_markdown": markdown, "tags": ["\u70ed\u70b9"], "fact_basis": [], "closing_quote": "\u4fdd\u6301\u6838\u5b9e", "keywords": ["\u70ed\u70b9"], "source_statement": "\u6765\u6e90\u58f0\u660e", "demo_mode": False}


def create_phase2a_task(tmp_path: Path) -> tuple[SQLiteStore, dict]:
    store = SQLiteStore(tmp_path / "db.sqlite")
    topic = HotTopic(id="phase2a-topic", title="阶段二测试热点", summary="测试摘要", source="test", source_name="测试源", source_url="https://example.com/topic")
    store.save_topics([topic])
    return store, store.create_task(
        "2A 单篇任务",
        "multi_topic",
        [topic.to_dict()],
        1,
        generation_options={"image_plan_mode": "standard", "image_generation_requested": True},
    )


def profiles() -> tuple[dict, dict]:
    return ({"name": "text", "api_key": "text-test-key", "base_url": "https://example.invalid/v1", "endpoint": "/chat/completions", "model": "text-model", "auth_type": "bearer"}, {"name": "image", "api_key": "image-test-key", "base_url": "https://example.invalid/v1", "endpoint": "/images/generations", "model": "image-model", "auth_type": "bearer", "size": "1536x1024"})


def install_image_provider(monkeypatch, should_fail=False):
    import generation.single_task as single_task

    class FakeImageProvider:
        last_response_type = "base64"

        def __init__(self, *args, **kwargs):
            self.should_fail = should_fail

        def generate(self, prompt, output_path):
            if self.should_fail:
                raise ProviderError("NETWORK_ERROR", "proxy http://user:password@proxy.example")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (512, 512), (20, 80, 160)).save(output_path, format="PNG")
            return output_path

    monkeypatch.setattr(single_task, "OpenAIImageProvider", FakeImageProvider)
    return FakeImageProvider


def test_single_task_persists_article_cover_prompts_and_relative_paths(tmp_path, monkeypatch):
    import generation.single_task as single_task
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    monkeypatch.setattr(single_task, "generate_article", lambda *args, **kwargs: article_value())
    install_image_provider(monkeypatch)
    store, task = create_phase2a_task(tmp_path)
    text_profile, image_profile = profiles()
    result = single_task.run_single_task(task, text_profile, image_profile, settings={"network": {}}, store=store)
    task_dir = tmp_path / "tasks" / task["task_id"]
    assert result["status"] == "completed"
    assert result["progress"] == 100
    assert (task_dir / "task.json").exists()
    assert (task_dir / "article.json").exists()
    assert (task_dir / "article.md").exists()
    assert (task_dir / "prompts" / "article_prompt.txt").exists()
    assert (task_dir / "prompts" / "cover_prompt.txt").exists()
    assert (task_dir / "images" / "cover.png").exists()
    assert result["paths"]["cover"] == "images/cover.png"
    assert all(not str(value).startswith(str(tmp_path)) for value in result["paths"].values())
    saved = load_generation_task(task["task_id"])
    assert saved and saved["status"] == "completed"
    assert "api_key" not in json.dumps(saved, ensure_ascii=False).lower()
    assert inspect_image(task_dir / "images" / "cover.png")["width"] == 1536


def test_cover_failure_preserves_article_and_retries_cover_only(tmp_path, monkeypatch):
    import generation.single_task as single_task
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    monkeypatch.setattr(single_task, "generate_article", lambda *args, **kwargs: article_value())
    install_image_provider(monkeypatch, should_fail=True)
    store, task = create_phase2a_task(tmp_path)
    text_profile, image_profile = profiles()
    failed = single_task.run_single_task(task, text_profile, image_profile, settings={"network": {}}, store=store)
    assert failed["status"] == "partial_success"
    assert failed["failed_step"] == "generating_cover"
    assert failed["article"]
    install_image_provider(monkeypatch, should_fail=False)
    completed = single_task.run_single_task(task, text_profile, image_profile, settings={"network": {}}, store=store, retry_step="retry-cover")
    assert completed["status"] == "completed"
    assert completed["retry_count"] == 1


def test_article_failure_retries_article_then_cover(tmp_path, monkeypatch):
    import generation.single_task as single_task
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    monkeypatch.setattr(single_task, "generate_article", lambda *args, **kwargs: (_ for _ in ()).throw(ProviderError("MODEL_OUTPUT_INVALID", "sections invalid")))
    install_image_provider(monkeypatch)
    store, task = create_phase2a_task(tmp_path)
    text_profile, image_profile = profiles()
    failed = single_task.run_single_task(task, text_profile, image_profile, settings={"network": {}}, store=store)
    assert failed["status"] == "failed"
    assert failed["failed_step"] == "generating_article"
    monkeypatch.setattr(single_task, "generate_article", lambda *args, **kwargs: article_value())
    completed = single_task.run_single_task(task, text_profile, image_profile, settings={"network": {}}, store=store, retry_step="retry-article")
    assert completed["status"] == "completed"
    assert completed["retry_count"] == 1


def test_image_validation_rejects_html(tmp_path):
    path = tmp_path / "bad.png"
    path.write_text("<html>error</html>", encoding="utf-8")
    with pytest.raises(ProviderError, match="INVALID_RESPONSE"):
        inspect_image(path)


def test_auth_modes_and_text_connection_result(monkeypatch):
    assert _headers({"api_key": "key", "auth_type": "bearer"})["Authorization"] == "Bearer key"
    assert _headers({"api_key": "key", "auth_type": "x-api-key"})["X-API-Key"] == "key"
    assert _headers({"api_key": "key", "auth_type": "custom_header", "auth_header": "X-Custom"})["X-Custom"] == "key"
    assert "Authorization" not in _headers({"api_key": "key", "auth_type": "none"})

    class FakeResponse:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": '{"ok":true}'}}]}
        def raise_for_status(self):
            return None

    class FakeClient:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def post(self, *args, **kwargs):
            return FakeResponse()

    import providers.text_provider as text_provider
    monkeypatch.setattr(text_provider, "create_http_client", lambda settings: FakeClient())
    result = OpenAITextProvider({"api_key": "key", "base_url": "https://example.invalid", "model": "test", "auth_type": "bearer"}).test_connection()
    assert result.success is True
    assert result.supports_json is True
    assert result.http_status == 200


def test_image_connection_result_supports_base64(monkeypatch, tmp_path):
    buffer = io.BytesIO()
    Image.new("RGB", (128, 128), (10, 20, 30)).save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

    class FakeResponse:
        status_code = 200
        def json(self):
            return {"data": [{"b64_json": encoded}]}
        def raise_for_status(self):
            return None

    class FakeClient:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def post(self, *args, **kwargs):
            return FakeResponse()

    import providers.image_provider as image_provider
    monkeypatch.setattr(image_provider, "create_http_client", lambda settings: FakeClient())
    result = OpenAIImageProvider({"api_key": "key", "base_url": "https://example.invalid", "model": "test", "auth_type": "bearer"}).test_connection(tmp_path / "test.png")
    assert result.success is True
    assert result.image_response_type == "base64"
    assert result.details["sha256"]


def test_api_task_run_endpoint_returns_queued_state(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import api

    store, task = create_phase2a_task(tmp_path)
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "load_settings", lambda: {
        "text_profile": {"model": "text-model", "base_url": "https://example.invalid/v1", "endpoint": "/chat/completions"},
        "image_profile": {},
        "network": {},
        "verified_text_model": "text-model",
        "verified_text_base_url": "https://example.invalid/v1",
        "verified_text_endpoint": "/chat/completions",
    })
    class FakeExecutor:
        def is_running(self, task_id):
            return False

        def submit(self, task_id, function):
            return object()

    monkeypatch.setattr(api, "executor", FakeExecutor())
    response = TestClient(api.app).post(f"/api/tasks/{task['task_id']}/run")
    assert response.status_code == 202
    assert response.json()["data"]["status"] == "queued"


def test_phase2a_rejects_multi_topic_task(tmp_path):
    import generation.single_task as single_task

    store = SQLiteStore(tmp_path / "db.sqlite")
    topics = [HotTopic(id=f"topic-{index}", title=f"话题{index}") for index in range(2)]
    store.save_topics(topics)
    task = store.create_task("invalid", "multi_topic", [item.to_dict() for item in topics], 2)
    with pytest.raises(ProviderError, match="PHASE2A_SINGLE_ONLY"):
        single_task.run_single_task(task, {}, {}, store=store)
