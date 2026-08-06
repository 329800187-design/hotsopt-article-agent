from __future__ import annotations

import json
from pathlib import Path

from modules.database import SQLiteStore

ROOT = Path(__file__).resolve().parents[1]


def _topic(identifier: str) -> dict:
    return {"id": identifier, "title": f"话题 {identifier}", "summary": "摘要", "source_name": "测试源"}


def test_corrupt_task_json_fields_do_not_crash_content_listing(tmp_path):
    store = SQLiteStore(tmp_path / "hotspot.db")
    task = store.create_task("bad-json", "multi_topic", [_topic("t1")], 1)
    with store.connect() as connection:
        connection.execute(
            "UPDATE generation_tasks SET selected_topics=?, generation_options=?, angle_plan=? WHERE task_id=?",
            ("{bad", "[bad", "not-json", task["task_id"]),
        )
        connection.commit()

    listed = store.list_tasks(limit=20)

    assert listed[0]["task_id"] == task["task_id"]
    assert listed[0]["selected_topics"] == []
    assert listed[0]["generation_options"] == {}
    assert listed[0]["angle_plan"] == {}


def test_unbatched_task_listing_keeps_old_history_visible(tmp_path):
    store = SQLiteStore(tmp_path / "hotspot.db")
    standalone = store.create_task("old-single", "multi_topic", [_topic("single")], 1)
    batched = store.create_batch("batch", "multi_topic", [_topic("batch")], {"article_count": 1}, 1)

    items = store.list_tasks(limit=20, unbatched=True)

    assert [item["task_id"] for item in items] == [standalone["task_id"]]
    assert batched["items"][0]["task"]["task_id"] not in [item["task_id"] for item in items]


def test_batch_listing_is_bounded_and_single_pass(tmp_path):
    store = SQLiteStore(tmp_path / "hotspot.db")
    for index in range(25):
        store.create_batch(f"batch-{index}", "multi_topic", [_topic(f"t{index}")], {"article_count": 1}, 1)

    items = store.list_batches(limit=20)

    assert len(items) == 20
    assert items[0]["batch_name"] == "batch-24"


def test_api_batch_list_does_not_start_batches(monkeypatch):
    import api

    calls = {"start": 0, "refresh": 0}

    class FakeStore:
        def list_batches(self, limit=20, offset=0):
            assert limit == 20
            assert offset == 0
            return [{"batch_id": "b1", "status": "queued", "items": []}]

        def refresh_batch(self, batch_id: str):
            calls["refresh"] += 1
            return {"batch_id": batch_id, "status": "queued", "items": []}

    class FakeExecutor:
        store = FakeStore()

        def start_batch(self, batch_id: str):
            calls["start"] += 1
            raise AssertionError("GET /api/batches must not start generation")

    monkeypatch.setattr(api, "batch_executor", FakeExecutor())

    response = api.list_batches(limit=20, offset=0, refresh=True)
    payload = json.loads(response.body.decode("utf-8"))

    assert payload["success"] is True
    assert payload["data"]["count"] == 1
    assert calls == {"start": 0, "refresh": 1}


def test_content_page_uses_bounded_retryable_loading_paths():
    source = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")

    assert '"/batches?limit=20&refresh=false"' in source
    assert '"/tasks?limit=20&unbatched=true"' in source
    assert 'key="rc1_content_retry"' in source
    assert 'CONTENT-LIST-001' in source
