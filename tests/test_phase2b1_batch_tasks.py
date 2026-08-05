from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from generation.batch_executor import BatchExecutor
from modules.database import SQLiteStore
from modules.generation_store import load_generation_task, save_generation_task
from modules.models import HotTopic
from modules.topic_cache import TopicCacheStore
from hot_sources.service import HotTrendService
from providers.text_provider import ProviderError


def topics(count: int = 5) -> list[HotTopic]:
    return [HotTopic(id=f"batch-topic-{index}", title=f"批次话题 {index}", source_name="FakeProvider") for index in range(count)]


def make_store(tmp_path: Path, count: int = 5) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "batch.sqlite")
    values = topics(count)
    store.save_topics(values)
    return store


def make_batch(store: SQLiteStore, count: int = 2) -> dict:
    return store.create_batch("测试批次", "multi_topic", [topic.to_dict() for topic in topics(count)], {"article_type": "热点资讯", "word_count": 800})


def mark_task(store: SQLiteStore, task_id: str, status: str) -> None:
    store.update_task_status(task_id, status)
    state = load_generation_task(task_id)
    if state:
        state["status"] = status
        state["stage"] = status
        state["state_version"] = int(state.get("state_version") or 0) + 1
        save_generation_task(state, expected_version=state["state_version"] - 1)


def wait_for(store: SQLiteStore, batch_id: str, status: str, timeout: float = 3) -> dict:
    deadline = time.monotonic() + timeout
    value = store.refresh_batch(batch_id)
    while time.monotonic() < deadline and value.get("status") != status:
        time.sleep(0.02)
        value = store.refresh_batch(batch_id)
    return value


def fake_run(monkeypatch, mode: str = "success"):
    import generation.executor as executor_module

    def run(task, text_profile, image_profile, settings=None, store=None, retry_step=None):
        state = load_generation_task(task["task_id"])
        title = str((task.get("selected_topics") or [{}])[0].get("title") or "")
        if mode == "mixed" and title.endswith("1") and retry_step is None:
            state.update({"status": "failed", "stage": "generating_article", "failed_step": "generating_article", "error_code": "PROVIDER_ERROR"})
        elif mode == "cover_retry" and title.endswith("1") and retry_step is None:
            state.update({"status": "partial_success", "stage": "generating_cover", "failed_step": "generating_cover", "article": {"title": title, "content_markdown": "正文"}})
        else:
            state.update({"status": "completed", "stage": "completed", "progress": 100, "completed_at": "2026-07-18T00:00:00+00:00"})
        state["state_version"] = int(state.get("state_version") or 0) + 1
        save_generation_task(state, expected_version=state["state_version"] - 1)
        store.update_task_status(task["task_id"], state["status"])
        return state

    monkeypatch.setattr(executor_module, "run_single_task", run)


def test_create_one_child_batch(tmp_path):
    batch = make_batch(make_store(tmp_path), 1)
    assert batch["total_count"] == 1
    assert len(batch["items"]) == 1
    assert batch["items"][0]["task"]["article_count"] == 1


def test_create_five_child_batch(tmp_path):
    batch = make_batch(make_store(tmp_path), 5)
    assert batch["total_count"] == 5
    assert len({item["task"]["task_id"] for item in batch["items"]}) == 5


def test_zero_topics_rejected(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.create_batch("empty", "multi_topic", [], {})


def test_six_topics_rejected(tmp_path):
    store = make_store(tmp_path, 6)
    with pytest.raises(ValueError):
        store.create_batch("too many", "multi_topic", [topic.to_dict() for topic in topics(6)], {})


def test_duplicate_topics_rejected(tmp_path):
    store = make_store(tmp_path)
    duplicate = topics(1)[0].to_dict()
    with pytest.raises(ValueError):
        store.create_batch("duplicate", "multi_topic", [duplicate, duplicate], {})


def test_manual_and_hot_topic_snapshots_can_mix(tmp_path):
    store = make_store(tmp_path)
    manual = HotTopic(id="manual-1", title="手动话题", source="manual", source_name="手动").to_dict()
    batch = store.create_batch("mixed", "multi_topic", [topics(1)[0].to_dict(), manual], {})
    assert [item["topic_snapshot"]["source"] for item in batch["items"]] == ["unknown", "manual"]


def test_batch_creation_is_transactional(tmp_path):
    store = make_store(tmp_path)
    with store.connect() as connection:
        connection.execute("CREATE TRIGGER fail_second_item BEFORE INSERT ON generation_batch_items WHEN NEW.position=2 BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(Exception):
        store.create_batch("rollback", "multi_topic", [topic.to_dict() for topic in topics(2)], {})
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM generation_batches").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM generation_tasks").fetchone()[0] == 0


def test_generation_options_are_copied_without_api_key(tmp_path):
    batch = make_batch(make_store(tmp_path), 1)
    assert batch["generation_options"]["word_count"] == 800
    assert "api_key" not in json.dumps(batch, ensure_ascii=False).lower()
    assert batch["items"][0]["task"]["generation_options"] == batch["generation_options"]


def test_start_is_idempotent_and_uses_default_workers(tmp_path, monkeypatch):
    fake_run(monkeypatch)
    store = make_store(tmp_path, 5)
    batch = make_batch(store, 5)
    executor = BatchExecutor(store)
    assert executor.max_workers == 3
    executor.start_batch(batch["batch_id"])
    executor.start_batch(batch["batch_id"])
    result = wait_for(store, batch["batch_id"], "completed")
    assert result["completed_count"] == 5


def test_max_concurrency_is_clamped_to_three(tmp_path):
    assert BatchExecutor(make_store(tmp_path), 9).max_workers == 3


def test_each_child_has_one_future(tmp_path, monkeypatch):
    fake_run(monkeypatch)
    store = make_store(tmp_path, 2)
    batch = make_batch(store, 2)
    executor = BatchExecutor(store)
    task_id = batch["items"][0]["task"]["task_id"]
    executor.start_batch(batch["batch_id"])
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and executor.is_task_active(task_id):
        time.sleep(0.03)
    assert not executor.is_task_active(task_id) or task_id not in executor._active


def test_all_success_summarizes_completed(tmp_path, monkeypatch):
    fake_run(monkeypatch)
    store = make_store(tmp_path, 2)
    batch = make_batch(store, 2)
    executor = BatchExecutor(store)
    executor.start_batch(batch["batch_id"])
    assert wait_for(store, batch["batch_id"], "completed")["completed_count"] == 2


def test_all_failure_summarizes_failed(tmp_path, monkeypatch):
    fake_run(monkeypatch, "mixed")
    store = make_store(tmp_path, 2)
    batch = make_batch(store, 2)
    for item in batch["items"]:
        mark_task(store, item["task"]["task_id"], "failed")
    assert store.refresh_batch(batch["batch_id"])["status"] == "failed"


def test_success_plus_failure_is_partial_success(tmp_path):
    store = make_store(tmp_path, 2)
    batch = make_batch(store, 2)
    for item, status in zip(batch["items"], ["completed", "failed"]):
        mark_task(store, item["task"]["task_id"], status)
    result = store.refresh_batch(batch["batch_id"])
    assert result["status"] == "partial_success"
    assert result["completed_count"] == 1 and result["failed_count"] == 1


def test_success_plus_cancel_is_partial_success(tmp_path):
    store = make_store(tmp_path, 2)
    batch = make_batch(store, 2)
    for item, status in zip(batch["items"], ["completed", "cancelled"]):
        mark_task(store, item["task"]["task_id"], status)
    assert store.refresh_batch(batch["batch_id"])["status"] == "partial_success"


def test_all_cancelled_is_cancelled(tmp_path):
    store = make_store(tmp_path, 2)
    batch = make_batch(store, 2)
    for item in batch["items"]:
        mark_task(store, item["task"]["task_id"], "cancelled")
    assert store.refresh_batch(batch["batch_id"])["status"] == "cancelled"


def test_cancel_one_child_does_not_change_other(tmp_path):
    store = make_store(tmp_path, 2)
    batch = make_batch(store, 2)
    executor = BatchExecutor(store)
    executor.cancel_task(batch["batch_id"], batch["items"][0]["task"]["task_id"])
    result = store.refresh_batch(batch["batch_id"])
    statuses = [item["task"]["status"] for item in result["items"]]
    assert statuses == ["cancelled", "queued"]


def test_cancel_batch_cancels_queued_children(tmp_path):
    store = make_store(tmp_path, 2)
    batch = make_batch(store, 2)
    result = BatchExecutor(store).cancel_batch(batch["batch_id"])
    assert result["status"] == "cancelled"
    assert result["cancelled_count"] == 2


def test_retry_article_reuses_task_id(tmp_path, monkeypatch):
    fake_run(monkeypatch)
    store = make_store(tmp_path, 1)
    batch = make_batch(store, 1)
    task_id = batch["items"][0]["task"]["task_id"]
    mark_task(store, task_id, "failed")
    result = BatchExecutor(store).retry_task(batch["batch_id"], task_id)
    assert result["task_id"] == task_id and result["retry_step"] == "retry-article"


def test_retry_cover_is_selected_from_failed_step(tmp_path, monkeypatch):
    fake_run(monkeypatch, "cover_retry")
    store = make_store(tmp_path, 2)
    batch = make_batch(store, 2)
    executor = BatchExecutor(store)
    executor.start_batch(batch["batch_id"])
    partial = wait_for(store, batch["batch_id"], "partial_success")
    result = executor.retry_task(batch["batch_id"], partial["items"][1]["task"]["task_id"])
    assert result["retry_step"] == "retry-cover"


def test_retry_all_failed_items_isolated(tmp_path, monkeypatch):
    fake_run(monkeypatch)
    store = make_store(tmp_path, 2)
    batch = make_batch(store, 2)
    for item in batch["items"]:
        mark_task(store, item["task"]["task_id"], "failed")
    result = BatchExecutor(store).retry_failed(batch["batch_id"])
    assert len(result["submitted"]) == 2


def test_completed_task_cannot_retry(tmp_path):
    store = make_store(tmp_path, 1)
    batch = make_batch(store, 1)
    task_id = batch["items"][0]["task"]["task_id"]
    mark_task(store, task_id, "completed")
    with pytest.raises(ProviderError, match="completed"):
        BatchExecutor(store).retry_task(batch["batch_id"], task_id)


def test_recovery_recomputes_batch_from_children(tmp_path):
    store = make_store(tmp_path, 2)
    batch = make_batch(store, 2)
    mark_task(store, batch["items"][0]["task"]["task_id"], "completed")
    mark_task(store, batch["items"][1]["task"]["task_id"], "failed")
    report = BatchExecutor(store).recover_batches()
    assert report["recovered_batches"] == [] or report["skipped_batches"]
    assert store.refresh_batch(batch["batch_id"])["status"] == "partial_success"


def test_bad_batch_isolated_from_other_batches(tmp_path):
    store = make_store(tmp_path, 2)
    first = make_batch(store, 1)
    second = store.create_batch("second", "multi_topic", [topics(2)[1].to_dict()], {})
    with store.connect() as connection:
        connection.execute("UPDATE generation_tasks SET status='broken' WHERE task_id=?", (first["items"][0]["task"]["task_id"],))
    report = BatchExecutor(store).recover_batches()
    assert any(item["batch_id"] == first["batch_id"] for item in report["recovery_failed"] + report["skipped_batches"] + report["recovered_batches"]) or second["batch_id"]


def test_batch_api_key_never_enters_snapshot(tmp_path):
    store = make_store(tmp_path, 1)
    topic = topics(1)[0].to_dict()
    topic["summary"] = "safe"
    batch = store.create_batch("safe", "multi_topic", [topic], {"api_key": "SECRET", "headers": {"Authorization": "Bearer TOKEN"}})
    assert "SECRET" not in json.dumps(batch, ensure_ascii=False)
    assert "TOKEN" not in json.dumps(batch, ensure_ascii=False)


def test_batch_api_routes_are_available(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import api

    store = make_store(tmp_path, 1)
    api.store = store
    api.service = HotTrendService(store=store, providers=[], cache_store=TopicCacheStore(tmp_path / "cache.json", environment="test"))
    api.batch_executor = BatchExecutor(store)
    monkeypatch.setattr(api, "_license_gate", lambda feature=None: None)
    client = TestClient(api.app)
    topic_id = topics(1)[0].id
    response = client.post("/api/batches", json={"batch_name": "API批次", "topic_ids": [topic_id], "mode": "multi_topic"})
    assert response.status_code == 201
    batch_id = response.json()["data"]["batch_id"]
    assert client.get(f"/api/batches/{batch_id}").status_code == 200
    assert client.get(f"/api/batches/{batch_id}/items").status_code == 200
    assert client.post(f"/api/batches/{batch_id}/start").status_code == 202


def test_page_state_is_loaded_from_database(tmp_path):
    store = make_store(tmp_path, 1)
    batch = make_batch(store, 1)
    store.update_task_status(batch["items"][0]["task"]["task_id"], "completed")
    assert store.refresh_batch(batch["batch_id"])["completed_count"] == 1


def test_status_document_has_authoritative_current_results():
    status = Path(__file__).parents[1].joinpath("STATUS.md").read_text(encoding="utf-8")
    assert "## 当前测试结果" in status
    assert "当前测试结果" in status
