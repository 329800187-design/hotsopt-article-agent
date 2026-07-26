from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx
import pytest

from generation.executor import GenerationExecutor
from generation.image_prompt_generator import build_cover_prompt
from generation.single_task import prepare_generation_state
from modules.database import SQLiteStore
from modules.generation_store import generation_task_path, load_generation_task
from modules.models import HotTopic
from providers.errors import is_retryable_error, map_provider_exception
from providers.text_provider import ProviderError


def make_task(tmp_path: Path) -> tuple[SQLiteStore, dict]:
    store = SQLiteStore(tmp_path / "db.sqlite")
    topic = HotTopic(id="runtime-topic", title="运行状态测试", summary="摘要", source="test", source_name="测试", source_url="https://example.com/topic")
    store.save_topics([topic])
    return store, store.create_task("运行任务", "multi_topic", [topic.to_dict()], 1)


def test_provider_error_mapping_and_retry_policy():
    request = httpx.Request("POST", "https://example.com")
    for status, code in {
        400: "INVALID_REQUEST",
        401: "AUTHENTICATION_FAILED",
        403: "PERMISSION_DENIED",
        404: "MODEL_NOT_FOUND",
        408: "TIMEOUT",
        429: "RATE_LIMITED",
        500: "PROVIDER_INTERNAL_ERROR",
    }.items():
        response = httpx.Response(status, request=request)
        mapped = map_provider_exception(httpx.HTTPStatusError("failure", request=request, response=response), response)
        assert isinstance(mapped, ProviderError)
        assert mapped.code == code
        assert is_retryable_error(mapped.code) == (code in {"TIMEOUT", "RATE_LIMITED", "PROVIDER_INTERNAL_ERROR"})

    proxy = map_provider_exception(httpx.ProxyError("proxy failed", request=request))
    assert proxy.code == "PROXY_ERROR"
    assert is_retryable_error(proxy.code)


def test_duplicate_run_is_rejected_by_per_task_executor():
    executor = GenerationExecutor(max_workers=1)
    entered = threading.Event()
    release = threading.Event()

    def work():
        entered.set()
        release.wait(2)
        return {"status": "completed"}

    executor.submit("same-task", work)
    assert entered.wait(1)
    with pytest.raises(RuntimeError, match="TASK_ALREADY_RUNNING"):
        executor.submit("same-task", work)
    release.set()
    executor.pool.shutdown(wait=True)


def test_cancel_running_task_cannot_become_completed(tmp_path, monkeypatch):
    import modules.generation_store as generation_store
    import generation.executor as executor_module

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store, task = make_task(tmp_path)
    prepare_generation_state(task, {}, {}, store=store)
    executor = GenerationExecutor(max_workers=1)
    started = threading.Event()

    def fake_run(*args, **kwargs):
        started.set()
        while not executor_module.is_cancel_requested(task["task_id"]):
            time.sleep(0.01)
        return load_generation_task(task["task_id"]) or {"status": "cancelled"}

    monkeypatch.setattr(executor_module, "run_single_task", fake_run)
    future = executor.submit(task["task_id"], lambda: executor.execute_with_retry(task, {}, {}, {}, store))
    assert started.wait(1)
    cancelled = executor.cancel(task["task_id"], store)
    result = future.result(timeout=2)
    assert cancelled["status"] == "cancelled"
    assert result["status"] == "cancelled"
    assert load_generation_task(task["task_id"])["status"] == "cancelled"
    executor.pool.shutdown(wait=True)


def test_cancel_unknown_task_does_not_create_state_file(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    executor = GenerationExecutor(max_workers=1)
    with pytest.raises(ProviderError) as caught:
        executor.cancel("missing-task", store)
    assert caught.value.code == "TASK_NOT_FOUND"
    assert not generation_task_path("missing-task").exists()
    executor.pool.shutdown(wait=True)


def test_auto_retry_waits_then_retries_once(tmp_path, monkeypatch):
    import modules.generation_store as generation_store
    import generation.executor as executor_module

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store, task = make_task(tmp_path)
    prepare_generation_state(task, {}, {}, store=store)
    executor = GenerationExecutor(max_workers=1)
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(kwargs.get("retry_step"))
        if len(calls) == 1:
            return {"task_id": task["task_id"], "status": "failed", "error_code": "NETWORK_ERROR", "failed_step": "generating_article"}
        return {"task_id": task["task_id"], "status": "completed"}

    monkeypatch.setattr(executor_module, "run_single_task", fake_run)
    monkeypatch.setattr(executor_module.GenerationExecutor, "_sleep_or_cancel", staticmethod(lambda task_id, seconds: True))
    result = executor.execute_with_retry(task, {}, {}, {"max_auto_retries": 2}, store)
    assert result["status"] == "completed"
    assert calls == [None, "retry-article"]
    assert load_generation_task(task["task_id"])["stage"] == "retry_waiting"
    executor.pool.shutdown(wait=True)


def test_cover_prompt_uses_canonical_summary_with_intro_fallback():
    prompt = build_cover_prompt({"title": "标题", "summary": "规范摘要", "intro": "旧摘要", "sections": []}, "anime")
    assert "规范摘要" in prompt
    assert "旧摘要" not in prompt
    fallback = build_cover_prompt({"title": "标题", "intro": "旧摘要", "sections": []}, "anime")
    assert "旧摘要" in fallback


def test_api_duplicate_run_and_completed_retry_are_structured(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import api
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store, task = make_task(tmp_path)
    monkeypatch.setattr(api, "store", store)

    class BusyExecutor:
        def is_running(self, task_id):
            return True

    class IdleExecutor:
        def is_running(self, task_id):
            return False

        def submit(self, task_id, function):
            return object()

    monkeypatch.setattr(api, "executor", BusyExecutor())
    client = TestClient(api.app)
    busy = client.post(f"/api/tasks/{task['task_id']}/run")
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "TASK_ALREADY_RUNNING"

    monkeypatch.setattr(api, "executor", IdleExecutor())
    state = prepare_generation_state(task, {}, {}, store=store)
    state["status"] = "completed"
    state["stage"] = "completed"
    from modules.generation_store import save_generation_task
    save_generation_task(state)
    completed = client.post(f"/api/tasks/{task['task_id']}/retry-article")
    assert completed.status_code == 409
    assert completed.json()["error"]["code"] == "TASK_ALREADY_COMPLETED"


def test_api_cancel_unknown_task_has_no_side_effect(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import api

    store = SQLiteStore(tmp_path / "db.sqlite")
    monkeypatch.setattr(api, "store", store)
    response = TestClient(api.app).post("/api/tasks/unknown/cancel")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TASK_NOT_FOUND"
    assert not generation_task_path("unknown").exists()
