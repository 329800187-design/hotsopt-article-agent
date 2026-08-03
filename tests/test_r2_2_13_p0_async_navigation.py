from __future__ import annotations

import time
from pathlib import Path

from generation.batch_executor import BatchExecutor
from modules.database import SQLiteStore
from modules.models import HotTopic


def _batch(tmp_path: Path) -> tuple[SQLiteStore, dict]:
    store = SQLiteStore(tmp_path / "db.sqlite")
    topic = HotTopic(
        id="p0-topic",
        title="P0 异步任务话题",
        summary="公开摘要",
        source_url="https://example.com/topic",
    )
    store.save_topics([topic])
    return store, store.create_batch(
        "P0 异步批次",
        "multi_topic",
        [topic.to_dict()],
        {"article_count": 1},
        1,
    )


def test_start_batch_async_returns_before_background_work(tmp_path, monkeypatch):
    store, batch = _batch(tmp_path)
    executor = BatchExecutor(store)

    def slow_worker(batch_id: str):
        time.sleep(0.25)
        return store.refresh_batch(batch_id) or {}

    monkeypatch.setattr(executor, "_start_batch_worker", slow_worker)
    started = time.perf_counter()
    result = executor.start_batch_async(batch["batch_id"])
    elapsed = time.perf_counter() - started

    assert elapsed < 0.15
    assert result["batch_id"] == batch["batch_id"]
    assert store.get_batch(batch["batch_id"]) is not None


def test_start_batch_async_can_skip_refresh_for_interactive_create(tmp_path, monkeypatch):
    store, batch = _batch(tmp_path)
    executor = BatchExecutor(store)
    refresh_calls = []

    monkeypatch.setattr(executor, "_start_batch_worker", lambda batch_id: {})
    original_refresh = store.refresh_batch
    monkeypatch.setattr(store, "refresh_batch", lambda batch_id: refresh_calls.append(batch_id) or original_refresh(batch_id))

    result = executor.start_batch_async(batch["batch_id"], refresh=False)

    assert result["batch_id"] == batch["batch_id"]
    assert refresh_calls == []


def test_start_page_submits_one_async_start_call():
    source = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    assert '"/batches/{batch[\'batch_id\']}/start"' not in source
    assert 'st.session_state["rc1_content_detail_task_id"]' in source
    assert "轻量摘要" in source
