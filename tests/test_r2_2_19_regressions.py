from __future__ import annotations

import zipfile

from PIL import Image

from export.docx_exporter import export_article
from generation.workflow import (
    begin_image_generation,
    confirm_article,
    confirm_images,
    finish_image_generation,
    initialize_workflow,
    prepare_fusion,
)
from generation.batch_executor import BatchExecutor
from modules.database import SQLiteStore


def _workflow_state() -> dict:
    return {
        "task_id": "r219-workflow",
        "status": "completed",
        "article": {
            "title": "A title",
            "intro": "A lead",
            "sections": [
                {"heading": "One", "body": "First body"},
                {"heading": "Two", "body": "Second body"},
                {"heading": "Three", "body": "Third body"},
            ],
        },
        "cover": {"role": "cover", "path": "images/cover.png", "status": "completed"},
        "inline_images": [
            {"role": "inline", "image_id": "section-1", "paragraph_ref": "section-1", "path": "images/section-1.png", "status": "completed"},
        ],
    }


def test_fusion_rehydrates_article_images_from_persisted_slots():
    state = _workflow_state()
    initialize_workflow(state)
    confirm_article(state)
    begin_image_generation(state)
    finish_image_generation(state)
    confirm_images(state)
    prepare_fusion(state)

    assert state["workflow_state"] == "final_draft_pending_preview"
    assert {item["path"] for item in state["article"]["images"]} == {
        "images/cover.png",
        "images/section-1.png",
    }


def test_word_export_embeds_rehydrated_images(tmp_path):
    image_root = tmp_path / "images"
    image_root.mkdir()
    for name, color in (("cover.png", (80, 120, 160)), ("section-1.png", (160, 120, 80))):
        Image.new("RGB", (32, 32), color).save(image_root / name)

    state = _workflow_state()
    initialize_workflow(state)
    confirm_article(state)
    begin_image_generation(state)
    finish_image_generation(state)
    confirm_images(state)
    prepare_fusion(state)
    article = state["article"]
    article["layout_check"] = {"passed": True}
    article["layout_status"] = "passed"
    article["body_char_count"] = 100
    output = export_article(article, tmp_path / "article.docx", tmp_path)

    with zipfile.ZipFile(output) as archive:
        media = [name for name in archive.namelist() if name.startswith("word/media/")]
    assert len(media) == 2


def test_missing_batch_child_is_not_reported_as_completed(tmp_path):
    store = SQLiteStore(tmp_path / "batch.sqlite")
    topics = [
        {"id": "r219-topic-1", "title": "one", "source_name": "test"},
        {"id": "r219-topic-2", "title": "two", "source_name": "test"},
    ]
    batch = store.create_batch("r219", "multi_topic", topics, {})
    missing_task = batch["items"][1]["task"]["task_id"]
    with store.connect() as connection:
        connection.execute("DELETE FROM generation_tasks WHERE task_id=?", (missing_task,))

    refreshed = store.refresh_batch(batch["batch_id"])
    listed = store.list_batch_summaries(limit=20)

    assert refreshed["status"] == "partial_success"
    assert refreshed["failed_count"] == 1
    assert refreshed["missing_count"] == 1
    assert listed[0]["status"] == "partial_success"
    assert listed[0]["missing_count"] == 1
    assert listed[0]["final_ready"] == 0


def test_worker_failure_is_persisted_per_item_instead_of_staying_queued(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "batch.sqlite")
    topic = {"id": "r219-topic", "title": "one", "source_name": "test"}
    batch = store.create_batch(
        "r219-angle",
        "single_topic_multi_angle",
        [topic],
        {"article_count": 2},
        angles=[{"angle_id": "a", "angle_name": "A"}, {"angle_id": "b", "angle_name": "B"}],
    )
    executor = BatchExecutor(store)

    def fail_research(_batch):
        raise RuntimeError("research unavailable")

    monkeypatch.setattr(executor, "_ensure_shared_research", fail_research)
    result = executor.start_batch(batch["batch_id"])

    assert result["status"] == "failed"
    assert result["failed_count"] == 2
    assert all(item["task"]["status"] == "failed" for item in result["items"])
