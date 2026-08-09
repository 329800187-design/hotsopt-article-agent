from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import api
from hot_sources.service import HotTrendService
from modules.database import SQLiteStore
from modules.models import HotTopic
from modules.topic_cache import TopicCacheStore


ROOT = Path(__file__).resolve().parents[1]


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "hotspot.db")
    store.save_topics(
        [HotTopic(id="task-create-topic", title="测试热点", summary="公开资料摘要", source_name="测试源")],
        record_observation=False,
    )
    return store


def _client(monkeypatch, store: SQLiteStore) -> TestClient:
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(
        api,
        "service",
        HotTrendService(store=store, providers=[], cache_store=TopicCacheStore(store.db_path.parent / "cache.json", environment="test")),
    )
    monkeypatch.setattr(api.batch_executor, "store", store)
    monkeypatch.setattr(api, "_license_gate", lambda feature=None: None)
    monkeypatch.setattr(api.batch_executor, "start_batch_async", lambda batch_id, refresh=False: store.get_batch(batch_id))
    return TestClient(api.app)


def _payload(client_request_id: str = "task-create-regression-1") -> dict:
    return {
        "batch_name": "单篇任务创建回归",
        "mode": "multi_topic",
        "topic_ids": ["task-create-topic"],
        "article_count": 1,
        "concurrency": 1,
        "client_request_id": client_request_id,
        "generation_options": {
            "article_type": "热点资讯",
            "style": "客观通俗",
            "image_style": "动漫化新闻插画",
            "word_count": 1200,
            "image_plan_mode": "none",
            "image_call_budget_per_article": 0,
            "image_call_budget_per_batch": 0,
            "image_retry_limit": 0,
            "confirm_paid": False,
        },
    }


def test_submit_lock_is_boolean_on_first_render():
    source = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")
    assert "already_submitting = bool(st.session_state.get(submitting_key, False))" in source


def test_single_article_create_persists_without_image_batch_state(monkeypatch, tmp_path):
    store = _store(tmp_path)
    client = _client(monkeypatch, store)

    response = client.post("/api/batches", json=_payload())

    assert response.status_code == 201, response.text
    batch_id = response.json()["data"]["batch_id"]
    persisted = store.get_batch(batch_id)
    assert persisted and len(persisted["items"]) == 1
    assert persisted["generation_options"]["image_plan_mode"] == "none"
    assert persisted["items"][0]["task"]["generation_options"]["image_call_budget_per_batch"] == 0


def test_repeat_single_article_create_returns_same_batch_without_partial_task(monkeypatch, tmp_path):
    store = _store(tmp_path)
    client = _client(monkeypatch, store)
    payload = _payload("task-create-repeat-regression-1")

    first = client.post("/api/batches", json=payload)
    second = client.post("/api/batches", json=payload)

    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert second.json()["data"]["dedup"] is True
    assert second.json()["data"]["batch_id"] == first.json()["data"]["batch_id"]
    assert len(store.list_batches(limit=20)) == 1
    assert len(store.get_batch(first.json()["data"]["batch_id"])["items"]) == 1


def test_task_create_error_keeps_traceback_out_of_customer_message():
    source = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")
    assert 'st.error("任务创建失败，请重试。\\n错误编号：TASK-CREATE-001")' in source
    assert "format_exc()" in source


def test_content_cards_preview_completed_images_from_task_storage():
    source = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")
    assert "def _completed_image_paths" in source
    assert 'image.get("status") != "completed"' in source
    assert "st.image(completed_paths, width=220)" in source
