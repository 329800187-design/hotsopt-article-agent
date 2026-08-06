from __future__ import annotations

import uuid
import time
from pathlib import Path

from generation.batch_executor import BatchExecutor
from modules.database import SQLiteStore
from modules.generation_store import load_generation_task, save_generation_task


def test_partial_batch_is_visible_and_failed_item_retries_independently(tmp_path: Path, monkeypatch):
    import generation.executor as executor_module

    def fake_run(task, text_profile, image_profile, settings=None, store=None, retry_step=None):
        del text_profile, image_profile, settings
        state = load_generation_task(task["task_id"])
        attempt_id = uuid.uuid4().hex[:12]
        state["attempt"] = int(state.get("attempt") or 0) + 1
        state["attempt_id"] = attempt_id
        state.setdefault("attempt_history", []).append({"attempt": state["attempt"], "attempt_id": attempt_id})
        title = str((task.get("selected_topics") or [{}])[0].get("title") or "")
        status = "completed" if retry_step or title.endswith("ok") else "failed"
        state.update({"status": status, "stage": status, "progress": 100 if status == "completed" else 0})
        state["state_version"] = int(state.get("state_version") or 0) + 1
        save_generation_task(state, expected_version=state["state_version"] - 1)
        store.update_task_status(task["task_id"], status)
        return state

    monkeypatch.setattr(executor_module, "run_single_task", fake_run)
    store = SQLiteStore(tmp_path / "batch.sqlite")
    topics = [
        {"id": "r219-ok", "title": "ok", "source_name": "test"},
        {"id": "r219-fail", "title": "fail", "source_name": "test"},
    ]
    batch = store.create_batch("r219-e2e", "multi_topic", topics, {})
    executor = BatchExecutor(store)

    executor.start_batch(batch["batch_id"])
    deadline = time.monotonic() + 3
    result = store.refresh_batch(batch["batch_id"])
    while time.monotonic() < deadline and result["status"] == "running":
        time.sleep(0.02)
        result = store.refresh_batch(batch["batch_id"])
    assert result["status"] == "partial_success"
    assert result["completed_count"] == 1
    assert result["failed_count"] == 1
    listed = store.list_batch_summaries(limit=20)[0]
    assert {item["task"]["task_id"] for item in listed["items"]} == {
        item["task"]["task_id"] for item in batch["items"]
    }

    failed_task = next(item["task"]["task_id"] for item in result["items"] if item["task"]["status"] == "failed")
    old_state = load_generation_task(failed_task)
    retry = executor.retry_task(batch["batch_id"], failed_task)
    assert retry["status"] == "queued"
    refreshed = store.refresh_batch(batch["batch_id"])
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and refreshed["status"] == "running":
        time.sleep(0.02)
        refreshed = store.refresh_batch(batch["batch_id"])
    assert refreshed["status"] == "completed"
    assert load_generation_task(failed_task)["attempt_id"] != old_state["attempt_id"]
    assert load_generation_task(failed_task)["attempt_history"][-1]["attempt_id"] != old_state["attempt_history"][-1]["attempt_id"]
