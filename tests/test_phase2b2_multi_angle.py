from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from generation.angle_planner import available_angles, plan_angles
from generation.batch_executor import BatchExecutor
from generation.similarity import SIMILARITY_THRESHOLDS, compare_articles, compare_batch_articles
from modules.database import SQLiteStore
from modules.generation_store import load_generation_task, save_generation_task
from modules.models import HotTopic
from providers.text_provider import ProviderError


def make_store(tmp_path: Path, topic_count: int = 1) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.save_topics([HotTopic(id=f"angle-topic-{index}", title=f"角度热点 {index}", summary="公开摘要", source_url="https://example.com/topic") for index in range(topic_count)])
    return store


def make_angle_batch(store: SQLiteStore, count: int = 5, selected: list[str] | None = None) -> dict:
    topic = store.list_topics(limit=1)[0]
    angles = plan_angles(count, selected)
    return store.create_batch("五角度测试", "single_topic_multi_angle", [topic.to_dict()], {"article_count": count, "word_count": 800}, 2, angles)


def fake_completion(monkeypatch, duplicate: bool = False, always_duplicate: bool = False):
    import generation.executor as executor_module

    def run(task, text_profile, image_profile, settings=None, store=None, retry_step=None):
        state = load_generation_task(task["task_id"])
        angle = state.get("angle_id") or task.get("angle_id") or "news"
        rewrite_count = int(state.get("rewrite_count") or 0)
        same = always_duplicate or (duplicate and rewrite_count == 0)
        suffix = "same" if same else angle
        content_key = "same" if same else angle
        article = {
            "title": f"热点解读 {content_key}专题",
            "summary": f"{content_key}角度摘要，关注{content_key}带来的具体变化。",
            "content_markdown": f"# 热点解读 {content_key}专题\n\n开头聚焦{content_key}的独特问题。 " + (f"{content_key}专属事实与案例 " * 40),
            "sections": [
                {"heading": f"{content_key}核心观察", "body": f"{content_key}角度的专属分析与案例。 " + (f"{content_key}独有材料 " * 35), "image_brief": f"{content_key}场景"},
                {"heading": f"{content_key}行动建议", "body": f"面向读者的{content_key}建议。 " + (f"{content_key}差异化建议 " * 35), "image_brief": f"{content_key}视觉"},
            ],
            "angle_id": angle,
        }
        state.update({"status": "completed", "stage": "completed", "progress": 100, "article": article, "completed_at": "2026-07-18T00:00:00+00:00"})
        state["state_version"] = int(state.get("state_version") or 0) + 1
        save_generation_task(state, expected_version=state["state_version"] - 1)
        store.update_task_status(task["task_id"], "completed")
        return state

    monkeypatch.setattr(executor_module, "run_single_task", run)


def wait_status(store: SQLiteStore, batch_id: str, status: str, timeout: float = 5) -> dict:
    value = store.refresh_batch(batch_id)
    deadline = time.monotonic() + timeout
    stable_until = None
    while time.monotonic() < deadline:
        if value.get("status") == status:
            stable_until = stable_until or (time.monotonic() + 0.8)
            if time.monotonic() >= stable_until and all(item["task"].get("similarity_status") != "not_checked" for item in value.get("items", [])):
                break
        else:
            stable_until = None
        time.sleep(0.03)
        value = store.refresh_batch(batch_id)
    return value


def test_one_angle_batch_creates_one_child(tmp_path):
    batch = make_angle_batch(make_store(tmp_path), 1)
    assert batch["mode"] == "single_topic_multi_angle"
    assert batch["total_count"] == 1
    assert batch["items"][0]["angle_id"] == "news"


def test_five_angle_batch_creates_five_children(tmp_path):
    batch = make_angle_batch(make_store(tmp_path), 5)
    assert batch["total_count"] == 5
    assert [item["angle_position"] for item in batch["items"]] == [1, 2, 3, 4, 5]
    assert len({item["angle_id"] for item in batch["items"]}) == 5


def test_two_topics_rejected_in_multi_angle(tmp_path):
    store = make_store(tmp_path, 2)
    with pytest.raises(ValueError):
        store.create_batch("bad", "single_topic_multi_angle", [topic.to_dict() for topic in store.list_topics()], {"article_count": 2}, 2, plan_angles(2))


def test_zero_angles_rejected(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.create_batch("bad", "single_topic_multi_angle", [store.list_topics()[0].to_dict()], {"article_count": 0}, 2, [])


def test_six_angles_rejected(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.create_batch("bad", "single_topic_multi_angle", [store.list_topics()[0].to_dict()], {"article_count": 6}, 2, available_angles() + [{"angle_id": "six"}])


def test_duplicate_angles_rejected(tmp_path):
    store = make_store(tmp_path)
    angles = plan_angles(2, ["news", "commentary"])
    angles[1]["angle_id"] = "news"
    with pytest.raises(ValueError):
        store.create_batch("bad", "single_topic_multi_angle", [store.list_topics()[0].to_dict()], {"article_count": 2}, 2, angles)


def test_default_angle_distribution_is_stable():
    assert [item["angle_id"] for item in plan_angles(1)] == ["news"]
    assert [item["angle_id"] for item in plan_angles(2)] == ["news", "commentary"]
    assert [item["angle_id"] for item in plan_angles(3)] == ["news", "commentary", "social_observation"]
    assert [item["angle_id"] for item in plan_angles(5)] == ["news", "commentary", "social_observation", "emotional", "story"]


def test_each_angle_plan_is_persisted(tmp_path):
    store = make_store(tmp_path)
    batch = make_angle_batch(store, 3)
    reloaded = store.get_batch(batch["batch_id"])
    assert all(item["task"]["angle_plan"]["angle_id"] == item["angle_id"] for item in reloaded["items"])


def test_angle_snapshot_survives_restart(tmp_path):
    store = make_store(tmp_path)
    batch = make_angle_batch(store, 2)
    second_store = SQLiteStore(tmp_path / "db.sqlite")
    assert [item["angle_id"] for item in second_store.get_batch(batch["batch_id"])["items"]] == ["news", "commentary"]


def test_angle_options_are_copied_to_children(tmp_path):
    store = make_store(tmp_path)
    batch = make_angle_batch(store, 2)
    assert all(item["task"]["generation_options"]["angle_plan"]["angle_id"] == item["angle_id"] for item in batch["items"])


def test_single_task_angle_uses_saved_plan(tmp_path, monkeypatch):
    import generation.single_task as single_task
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    captured = {}
    monkeypatch.setattr(single_task, "generate_article", lambda topic, angle, *args, **kwargs: captured.setdefault("angle", angle) or {"title": "标题", "intro": "摘要", "sections": [{"heading": "a", "body": "正文", "image_brief": "图"}] * 3, "content_markdown": "# 标题"})
    store = make_store(tmp_path)
    batch = make_angle_batch(store, 1)
    task = batch["items"][0]["task"]
    single_task.run_single_task(task, {}, {"auth_type": "none"}, settings={"network": {}}, store=store)
    assert captured["angle"]["angle_id"] == "news"


def test_batch_executor_runs_five_children_with_fake_provider(tmp_path, monkeypatch):
    fake_completion(monkeypatch)
    store = make_store(tmp_path)
    batch = make_angle_batch(store, 5)
    executor = BatchExecutor(store)
    executor.start_batch(batch["batch_id"])
    result = wait_status(store, batch["batch_id"], "completed")
    assert result["completed_count"] == 5
    assert all(item["task"]["status"] == "completed" for item in result["items"])


def test_failed_angle_child_does_not_block_others(tmp_path, monkeypatch):
    import generation.executor as executor_module

    def run(task, text, image, settings=None, store=None, retry_step=None):
        state = load_generation_task(task["task_id"])
        if state.get("angle_id") == "commentary" and retry_step is None:
            state.update(status="failed", stage="generating_article", failed_step="generating_article", error_code="AUTHENTICATION_FAILED")
        else:
            state.update(status="completed", stage="completed", article={"title": state.get("angle_name"), "content_markdown": state.get("angle_name"), "sections": []})
        state["state_version"] = int(state.get("state_version") or 0) + 1
        save_generation_task(state, expected_version=state["state_version"] - 1)
        store.update_task_status(task["task_id"], state["status"])
        return state

    monkeypatch.setattr(executor_module, "run_single_task", run)
    store = make_store(tmp_path)
    batch = make_angle_batch(store, 3)
    executor = BatchExecutor(store)
    executor.start_batch(batch["batch_id"])
    result = wait_status(store, batch["batch_id"], "partial_success")
    assert result["completed_count"] == 2 and result["failed_count"] == 1


def test_similarity_detects_duplicate_title_and_body():
    left = {"title": "相同标题", "content_markdown": "相同开头" + "正文" * 100, "sections": [{"heading": "一", "body": "正文" * 100}]}
    right = dict(left)
    result = compare_articles(left, right)
    assert result["status"] == "rewrite_required"
    assert "title_similarity" in result["violations"]


def test_similarity_accepts_different_angles():
    left = {"title": "新闻事实梳理", "content_markdown": "事件发生后需要关注哪些事实", "sections": [{"heading": "时间线", "body": "事实信息"}]}
    right = {"title": "普通人的生活影响", "content_markdown": "从生活变化观察社会背景", "sections": [{"heading": "社会影响", "body": "群体观察"}]}
    assert compare_articles(left, right)["status"] == "passed"


def test_similarity_thresholds_are_centralized():
    assert SIMILARITY_THRESHOLDS["title_similarity"] == 0.75
    assert SIMILARITY_THRESHOLDS["opening_similarity"] == 0.65
    assert SIMILARITY_THRESHOLDS["body_similarity"] == 0.72


def test_batch_similarity_passes_distinct_articles(tmp_path, monkeypatch):
    fake_completion(monkeypatch)
    store = make_store(tmp_path)
    batch = make_angle_batch(store, 3)
    executor = BatchExecutor(store)
    executor.start_batch(batch["batch_id"])
    result = wait_status(store, batch["batch_id"], "completed")
    assert all(item["task"]["similarity_status"] == "passed" for item in result["items"])


def test_duplicate_articles_are_marked_for_manual_review_with_same_task_id(tmp_path, monkeypatch):
    fake_completion(monkeypatch, duplicate=True)
    store = make_store(tmp_path)
    batch = make_angle_batch(store, 2)
    original_ids = [item["task"]["task_id"] for item in batch["items"]]
    executor = BatchExecutor(store)
    executor.start_batch(batch["batch_id"])
    result = wait_status(store, batch["batch_id"], "completed")
    assert [item["task"]["task_id"] for item in result["items"]] == original_ids
    assert result["items"][0]["task"]["similarity_status"] == "review_required"
    assert result["items"][1]["task"]["similarity_status"] == "review_required"
    assert result["items"][1]["task"]["rewrite_count"] == 0


def test_similarity_duplicate_never_auto_rewrites(tmp_path, monkeypatch):
    fake_completion(monkeypatch, always_duplicate=True)
    store = make_store(tmp_path)
    batch = make_angle_batch(store, 2)
    executor = BatchExecutor(store)
    executor.start_batch(batch["batch_id"])
    result = wait_status(store, batch["batch_id"], "completed", timeout=8)
    assert result["items"][0]["task"]["rewrite_count"] == 0
    assert result["items"][1]["task"]["rewrite_count"] == 0
    assert result["items"][1]["task"]["similarity_status"] == "review_required"


def test_batch_api_rejects_two_topics_for_multi_angle(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import api
    from hot_sources.service import HotTrendService
    from modules.topic_cache import TopicCacheStore

    store = make_store(tmp_path, 2)
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "service", HotTrendService(store=store, providers=[], cache_store=TopicCacheStore(tmp_path / "cache.json", environment="test")))
    monkeypatch.setattr(api, "batch_executor", BatchExecutor(store))
    monkeypatch.setattr(api, "_license_gate", lambda feature=None: None)
    response = TestClient(api.app).post("/api/batches", json={"batch_name": "bad", "mode": "single_topic_multi_angle", "topic_ids": ["angle-topic-0", "angle-topic-1"], "article_count": 2})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "TOPIC-SELECT-LIMIT"


def test_batch_api_accepts_angle_ids_in_r227(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import api
    from hot_sources.service import HotTrendService
    from modules.topic_cache import TopicCacheStore

    store = make_store(tmp_path)
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "service", HotTrendService(store=store, providers=[], cache_store=TopicCacheStore(tmp_path / "cache.json", environment="test")))
    monkeypatch.setattr(api, "batch_executor", BatchExecutor(store))
    monkeypatch.setattr(api, "_license_gate", lambda feature=None: None)
    response = TestClient(api.app).post("/api/batches", json={"batch_name": "angles", "mode": "single_topic_multi_angle", "topic_ids": ["angle-topic-0"], "article_count": 3, "angles": ["news", "social_observation", "commentary"]})
    assert response.status_code == 201, response.text
    items = response.json()["data"]["items"]
    assert [item["task"]["angle_id"] for item in items] == ["news", "social_observation", "commentary"]


def test_api_angle_error_uses_unified_envelope(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import api
    from hot_sources.service import HotTrendService
    from modules.topic_cache import TopicCacheStore

    store = make_store(tmp_path)
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "service", HotTrendService(store=store, providers=[], cache_store=TopicCacheStore(tmp_path / "cache.json", environment="test")))
    monkeypatch.setattr(api, "batch_executor", BatchExecutor(store))
    monkeypatch.setattr(api, "_license_gate", lambda feature=None: None)
    response = TestClient(api.app).post("/api/batches", json={"batch_name": "bad", "mode": "single_topic_multi_angle", "topic_ids": ["angle-topic-0"], "article_count": 2, "angles": ["news", "news"]})
    body = response.json()
    assert response.status_code in {400, 422}
    assert set(body) == {"success", "data", "error", "request_id", "timestamp"}
    assert body["error"]["code"] in {"BATCH_CREATE_FAILED", "BATCH_OPERATION_FAILED", "VALIDATION_ERROR"}


def test_no_api_key_in_angle_snapshot(tmp_path):
    store = make_store(tmp_path)
    topic = store.list_topics()[0].to_dict()
    batch = store.create_batch("safe", "single_topic_multi_angle", [topic], {"article_count": 2, "api_key": "SECRET"}, 2, plan_angles(2))
    assert "SECRET" not in json.dumps(batch, ensure_ascii=False)
