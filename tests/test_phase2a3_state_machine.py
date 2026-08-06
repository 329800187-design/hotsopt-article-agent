from __future__ import annotations

import json
from pathlib import Path

import pytest

from generation.recovery import recover_interrupted_tasks
from generation.single_task import cancel_single_task, finalize_cancelled_task, prepare_generation_state, run_single_task
from modules.database import SQLiteStore
from modules.generation_store import generation_task_path, load_generation_task, save_generation_task
from modules.models import HotTopic
from providers.text_provider import ProviderError


def make_task(tmp_path: Path, name: str = "state-task") -> tuple[SQLiteStore, dict]:
    store = SQLiteStore(tmp_path / f"{name}.db")
    topic = HotTopic(id=f"topic-{name}", title="状态测试热点", summary="摘要", source="test", source_name="测试", source_url="https://example.com/topic")
    store.save_topics([topic])
    return store, store.create_task(name, "multi_topic", [topic.to_dict()], 1, generation_options={"article_type": "热点资讯", "style": "客观通俗", "image_style": "动漫化新闻插画", "word_count": 800})


def set_state(tmp_path: Path, store: SQLiteStore, task: dict, status: str, stage: str, failed_step: str | None = None, cancellation_requested: bool = False) -> dict:
    state = prepare_generation_state(task, {}, {}, store=store)
    state.update({"status": status, "stage": stage, "failed_step": failed_step, "cancellation_requested": cancellation_requested})
    save_generation_task(state)
    store.update_task_status(task["task_id"], status)
    return state


def write_article(task_id: str, valid: bool = True) -> None:
    path = generation_task_path(task_id).parent
    path.mkdir(parents=True, exist_ok=True)
    content = {"title": "旧文章", "content_markdown": "正文"} if valid else {"title": ""}
    (path / "article.json").write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")


def recovered_state(report: dict, task_id: str) -> dict:
    return next(item for item in report["recovered"] if item["task_id"] == task_id)


@pytest.mark.parametrize("status", ["failed", "partial_success"])
def test_failed_states_cancel_immediately(tmp_path, status):
    store, task = make_task(tmp_path, status)
    set_state(tmp_path, store, task, status, status, "generating_cover" if status == "partial_success" else "generating_article")
    result = cancel_single_task(task["task_id"], store)
    persisted = load_generation_task(task["task_id"])
    assert result["status"] == "cancelled"
    assert persisted["status"] == "cancelled"
    assert persisted["stage"] == "cancelled"
    assert persisted["cancellation_requested"] is True
    assert persisted["next_retry_at"] is None
    assert persisted["retryable"] is False
    assert store.get_task(task["task_id"])["status"] == "cancelled"


def test_running_cancel_is_request_then_final_cancel(tmp_path):
    store, task = make_task(tmp_path, "running")
    set_state(tmp_path, store, task, "running", "generating_article", "generating_article")
    requested = cancel_single_task(task["task_id"], store)
    assert requested["status"] == "running"
    assert requested["cancellation_requested"] is True
    final = finalize_cancelled_task(task["task_id"], store)
    assert final["status"] == "cancelled"


def test_completed_cancel_rejected_and_cancelled_is_idempotent(tmp_path):
    store, completed = make_task(tmp_path, "completed")
    set_state(tmp_path, store, completed, "completed", "completed")
    with pytest.raises(ProviderError) as caught:
        cancel_single_task(completed["task_id"], store)
    assert caught.value.code == "TASK_ALREADY_COMPLETED"

    store2, cancelled = make_task(tmp_path, "cancelled")
    set_state(tmp_path, store2, cancelled, "cancelled", "cancelled", cancellation_requested=True)
    result = cancel_single_task(cancelled["task_id"], store2)
    assert result["status"] == "cancelled"
    with pytest.raises(ProviderError) as caught_retry:
        run_single_task(cancelled, {}, {}, store=store2)
    assert caught_retry.value.code == "TASK_CANCELLED"


@pytest.mark.parametrize(
    "failed_step,has_article,expected_status,expected_step",
    [
        ("generating_article", False, "failed", "generating_article"),
        ("generating_article", True, "failed", "generating_article"),
        ("generating_cover", True, "partial_success", "generating_cover"),
        ("generating_cover", False, "failed", "generating_article"),
    ],
)
def test_retry_waiting_uses_failed_step_first(tmp_path, monkeypatch, failed_step, has_article, expected_status, expected_step):
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store, task = make_task(tmp_path, f"retry-{failed_step}-{has_article}")
    set_state(tmp_path, store, task, "running", "retry_waiting", failed_step)
    if has_article:
        write_article(task["task_id"])
    report = recover_interrupted_tasks(store)
    state = recovered_state(report, task["task_id"])
    assert state["status"] == expected_status
    assert state["failed_step"] == expected_step
    assert state["stage"] == "interrupted"


def test_recovery_isolates_invalid_task_and_continues(tmp_path, monkeypatch):
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    bad_store, bad = make_task(tmp_path, "bad")
    bad_state = prepare_generation_state(bad, {}, {}, store=bad_store)
    bad_state["status"] = "not-a-real-status"
    save_generation_task(bad_state)
    good_topic = HotTopic(id="topic-good", title="正常任务热点", summary="摘要", source="test", source_name="测试", source_url="https://example.com/good")
    bad_store.save_topics([good_topic])
    good = bad_store.create_task("good", "multi_topic", [good_topic.to_dict()], 1)
    set_state(tmp_path, bad_store, good, "running", "generating_article", "generating_article")
    report = recover_interrupted_tasks(bad_store)
    assert any(item["task_id"] == bad["task_id"] for item in report["recovery_failed"])
    assert any(item["task_id"] == good["task_id"] for item in report["recovered"])


def test_generation_options_insert_is_atomic_on_write_failure(tmp_path, monkeypatch):
    store, task = make_task(tmp_path, "atomic")
    original_write = store._write

    def fail_write(action):
        raise RuntimeError("injected insert failure")

    monkeypatch.setattr(store, "_write", fail_write)
    with pytest.raises(RuntimeError, match="injected"):
        store.create_task("will rollback", "multi_topic", task["selected_topics"], 1, generation_options={"word_count": 1200})
    monkeypatch.setattr(store, "_write", original_write)
    assert all(item["task_name"] != "will rollback" for item in store.list_tasks())


def test_generation_options_serialization_failure_rolls_back(tmp_path, monkeypatch):
    import modules.database as database

    store, task = make_task(tmp_path, "serialization")
    original_dumps = database.json.dumps

    def fail_dumps(value, *args, **kwargs):
        if isinstance(value, list) and value and isinstance(value[0], dict) and value[0].get("id"):
            raise TypeError("injected serialization failure")
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(database.json, "dumps", fail_dumps)
    with pytest.raises(TypeError, match="injected"):
        store.create_task("serialization rollback", "multi_topic", task["selected_topics"], 1, generation_options={"word_count": 1200})
    assert all(item["task_name"] != "serialization rollback" for item in store.list_tasks())


def test_generation_options_survive_restart(tmp_path):
    store, task = make_task(tmp_path, "restart-options")
    reopened = SQLiteStore(tmp_path / "restart-options.db")
    assert reopened.get_task(task["task_id"])["generation_options"] == task["generation_options"]


@pytest.mark.parametrize("json_status", ["completed", "cancelled", "failed"])
def test_json_snapshot_reconciles_sqlite_running(tmp_path, monkeypatch, json_status):
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store, task = make_task(tmp_path, f"reconcile-{json_status}")
    set_state(tmp_path, store, task, json_status, json_status, "generating_article", json_status == "cancelled")
    store.update_task_status(task["task_id"], "running")
    report = recover_interrupted_tasks(store)
    assert store.get_task(task["task_id"])["status"] == json_status
    assert load_generation_task(task["task_id"])["status"] == json_status
    assert report["recovery_failed"] == []


def test_corrupt_json_is_reported_without_overwrite(tmp_path, monkeypatch):
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store, task = make_task(tmp_path, "corrupt")
    prepare_generation_state(task, {}, {}, store=store)
    path = generation_task_path(task["task_id"])
    path.write_text("{broken", encoding="utf-8")
    report = recover_interrupted_tasks(store)
    assert any(item["task_id"] == task["task_id"] for item in report["recovery_failed"])
    assert path.read_text(encoding="utf-8") == "{broken"


def test_missing_queued_json_is_initialized(tmp_path, monkeypatch):
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store, task = make_task(tmp_path, "missing-queued")
    prepare_generation_state(task, {}, {}, store=store)
    generation_task_path(task["task_id"]).unlink()
    report = recover_interrupted_tasks(store)
    assert any(item["task_id"] == task["task_id"] for item in report["recovered"])
    assert load_generation_task(task["task_id"])["status"] == "queued"


def test_sqlite_failure_can_retry_reconciliation(tmp_path, monkeypatch):
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store, task = make_task(tmp_path, "db-failure")
    set_state(tmp_path, store, task, "failed", "interrupted", "generating_article")
    store.update_task_status(task["task_id"], "running")
    original_update = store.update_task_status
    calls = {"count": 0}

    def fail_once(task_id, status):
        if calls["count"] == 0:
            calls["count"] += 1
            raise RuntimeError("injected sqlite update failure")
        return original_update(task_id, status)

    monkeypatch.setattr(store, "update_task_status", fail_once)
    first = recover_interrupted_tasks(store)
    assert first["recovery_failed"]
    monkeypatch.setattr(store, "update_task_status", original_update)
    second = recover_interrupted_tasks(store)
    assert second["recovery_failed"] == []
    assert store.get_task(task["task_id"])["status"] == "failed"


def test_fastapi_lifespan_starts_and_health_is_available(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import api

    monkeypatch.setattr(api, "store", SQLiteStore(tmp_path / "api.db"))
    with TestClient(api.app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["success"] is True


def test_ui_maps_article_and_cover_retry_labels():
    text = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    assert "重新搜索资料并生成" in text
    assert "重新写文章" in text
    assert "/items/{task_id}/retry" in text
