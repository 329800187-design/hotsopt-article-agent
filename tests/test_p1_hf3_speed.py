from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api
import generation.batch_executor as batch_executor_module
import generation.single_task as single_task
import modules.generation_store as generation_store
from generation.angle_planner import plan_angles
from modules.database import SQLiteStore
from modules.generation_store import generation_task_dir, load_generation_task, save_generation_task
from modules.models import HotTopic
from providers.text_provider import ProviderError

CN_TOPIC_TITLE = "真实热点测试"
CN_TOPIC_SUMMARY = "公开资料显示事件仍在发展，需要基于现有来源整理文章。"
CN_TIMEOUT_LABEL = "本次超时上限"
CN_FALLBACK_NOTICE = "已生成基础稿"
CN_SOURCE_HEADING = "资料来源"


def _profiles() -> tuple[dict, dict]:
    return (
        {
            "name": "text",
            "api_key": "text-key",
            "base_url": "https://example.com/v1",
            "endpoint": "/chat/completions",
            "model": "hf3-text-model",
            "auth_type": "bearer",
            "timeout_seconds": 180,
        },
        {
            "name": "image",
            "api_key": "image-key",
            "base_url": "https://example.com/v1",
            "endpoint": "/images/generations",
            "model": "hf3-image-model",
            "auth_type": "bearer",
        },
    )


def _topic(topic_id: str = "hf3-topic") -> HotTopic:
    return HotTopic(
        id=topic_id,
        title=CN_TOPIC_TITLE,
        summary=CN_TOPIC_SUMMARY,
        source="test",
        source_name="source",
        source_url="https://example.com/topic",
    )


def _bundle(topic: HotTopic) -> dict:
    return {
        "topic_id": topic.id,
        "topic_title": topic.title,
        "research_status": "sufficient",
        "accepted_source_count": 2,
        "official_or_reliable_source_count": 1,
        "usable_fact_count": 3,
        "timeline": ["2026-07-26", "2026-07-26T10:00:00"],
        "background": ["background context from public sources"],
        "key_people": ["person-a"],
        "key_organizations": ["org-a"],
        "verified_facts": [
            {
                "fact_id": "f1",
                "canonical_fact": "official source published a public explanation for the event.",
                "supporting_source_ids": ["s1", "s2"],
                "verification_type": "independent_publishers",
            },
            {
                "fact_id": "f2",
                "canonical_fact": "multiple public sources said the event is still evolving.",
                "supporting_source_ids": ["s1", "s2"],
                "verification_type": "independent_publishers",
            },
        ],
        "usable_facts": [
            {
                "fact_id": "f1",
                "canonical_fact": "official source published a public explanation for the event.",
                "supporting_source_ids": ["s1", "s2"],
                "verification_type": "independent_publishers",
            },
            {
                "fact_id": "f2",
                "canonical_fact": "multiple public sources said the event is still evolving.",
                "supporting_source_ids": ["s1", "s2"],
                "verification_type": "independent_publishers",
            },
        ],
        "sources": [
            {
                "source_id": "s1",
                "source_name": "official",
                "title": "official notice",
                "published_at": "2026-07-26",
                "url": "https://example.com/source-1",
                "summary": "official source published a public explanation.",
                "content": "official source published a public explanation. multiple public sources said the event is still evolving.",
                "fetch_success": True,
                "accepted_for_research": True,
                "source_level": "official",
                "publisher_id": "example.com",
                "domain": "example.com",
            },
            {
                "source_id": "s2",
                "source_name": "media",
                "title": "media report",
                "published_at": "2026-07-26",
                "url": "https://news.example.com/source-2",
                "summary": "media report tracked the event impact.",
                "content": "media report tracked the event impact and said the event is still evolving.",
                "fetch_success": True,
                "accepted_for_research": True,
                "source_level": "source_page",
                "publisher_id": "news.example.com",
                "domain": "news.example.com",
            },
        ],
    }


def _long_article_markdown() -> str:
    section_body = "基于公开资料整理的核心信息仍在持续更新，同时需要关注背景信息、现实影响和后续变化。" * 14
    return (
        "# HF3 title\n\n"
        "导语部分基于公开资料整理。\n\n"
        "## section one\n" + section_body + "\n\n"
        "## section two\n" + section_body + "\n\n"
        "## section three\n" + section_body + "\n\n"
        + CN_SOURCE_HEADING + "\n[1] official：《official notice》，2026-07-26\n原文链接：https://example.com/source-1"
    )


def _article_payload() -> dict:
    return {
        "title": "HF3 title",
        "intro": "导语部分基于公开资料整理。",
        "summary": "HF3 summary",
        "sections": [
            {"heading": "section one", "body": "公开资料显示事件仍在持续更新。" * 10, "image_brief": "scene one"},
            {"heading": "section two", "body": "多个公开来源都在跟进事件影响。" * 10, "image_brief": "scene two"},
            {"heading": "section three", "body": "从现有资料看，读者更关心后续变化。" * 10, "image_brief": "scene three"},
        ],
        "content_markdown": _long_article_markdown(),
        "source_list": ["[1] official：《official notice》，2026-07-26\n原文链接：https://example.com/source-1"],
        "ai_statement": "AI辅助声明：本文基于公开资料整理生成。",
        "recommended_status": "completed",
        "text_generation_calls": 1,
        "text_generation_limit": 1,
        "text_generation_second_call_reason": "",
        "fact_basis": [],
    }


def _make_store_and_task(tmp_path: Path, *, article_count: int = 1) -> tuple[SQLiteStore, dict, HotTopic]:
    store = SQLiteStore(tmp_path / "hf3.sqlite")
    topic = _topic()
    store.save_topics([topic])
    task = store.create_task(
        "HF3 task",
        "multi_topic",
        [topic.to_dict()],
        article_count,
        generation_options={"article_type": "news", "style": "neutral", "image_plan_mode": "none", "word_count": 1200},
    )
    return store, task, topic


def _seed_completed_state(tmp_path: Path, *, gate_status: str) -> tuple[SQLiteStore, dict]:
    store, task, topic = _make_store_and_task(tmp_path)
    text_profile, image_profile = _profiles()
    state = single_task.prepare_generation_state(task, text_profile, image_profile, store=store)
    root = generation_task_dir(task["task_id"])
    root.mkdir(parents=True, exist_ok=True)
    article = _article_payload()
    article["layout_status"] = "passed"
    article["layout_check"] = {"passed": True}
    state.update(
        {
            "status": "completed",
            "stage": "completed",
            "progress": 100,
            "completed_at": "2026-07-26T12:00:00+08:00",
            "article": article,
            "quality_gate": {"status": gate_status, "passed": gate_status != "failed", "hard_error_count": 1 if gate_status == "failed" else 0},
            "research_bundle": _bundle(topic),
            "inline_images": [],
            "inline_image_summary": {"total": 0, "completed": 0, "failed": 0, "pending": 0, "status": "completed"},
        }
    )
    save_generation_task(state, expected_version=int(state.get("state_version") or 0), allow_terminal_recovery=True)
    store.update_task_status(task["task_id"], "completed")
    return store, task


def test_GENERATING_ARTICLE_STAGE_BEFORE_HTTP_PASS(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store, task, topic = _make_store_and_task(tmp_path)
    text_profile, image_profile = _profiles()
    bundle = _bundle(topic)
    observed: dict[str, object] = {}

    class FakeResearchService:
        def collect(self, _topic, references=None, supplemental_text=""):
            return bundle

    def fake_generate_article(*args, **kwargs):
        state = load_generation_task(task["task_id"])
        observed["stage"] = state.get("stage")
        observed["article_generation_started_at"] = state.get("article_generation_started_at")
        observed["timeout_seconds"] = ((state.get("model_info") or {}).get("text") or {}).get("timeout_seconds")
        return _article_payload()

    monkeypatch.setattr(single_task, "ResearchService", FakeResearchService)
    monkeypatch.setattr(single_task, "generate_article", fake_generate_article)
    monkeypatch.setattr(single_task, "ensure_article_layout", lambda article: {**article, "layout_status": "passed", "layout_check": {"passed": True}})

    result = single_task.run_single_task(task, text_profile, image_profile, settings={"network": {}, "image_plan_mode": "none"}, store=store)

    assert observed["stage"] == "generating_article"
    assert observed["article_generation_started_at"]
    assert observed["timeout_seconds"] >= 90
    assert result["status"] in {"completed", "warning", "partial_success", "failed"}


def test_SAME_TOPIC_RESEARCH_ONCE_PASS(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = SQLiteStore(tmp_path / "batch.sqlite")
    topic = _topic("shared-topic")
    store.save_topics([topic])
    batch = store.create_batch(
        "HF3 Batch",
        "single_topic_multi_angle",
        [topic.to_dict()],
        {"article_type": "news", "style": "neutral", "image_plan_mode": "none"},
        5,
        plan_angles(5),
    )
    executor = batch_executor_module.BatchExecutor(store=store, max_workers=3)
    calls = {"count": 0}
    shared_bundle = _bundle(topic)

    class FakeResearchService:
        def collect(self, _topic):
            calls["count"] += 1
            return shared_bundle

    monkeypatch.setattr(batch_executor_module, "ResearchService", FakeResearchService)

    updated = executor._ensure_shared_research(batch)

    assert calls["count"] == 1
    assert updated["generation_options"]["shared_research_bundle"]["accepted_source_count"] == 2
    for item in updated["items"]:
        assert item["task"]["generation_options"]["shared_research_bundle"]["accepted_source_count"] == 2


def test_MODEL_TIMEOUT_LOCAL_FALLBACK_PASS(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store, task, topic = _make_store_and_task(tmp_path)
    text_profile, image_profile = _profiles()
    bundle = _bundle(topic)

    class FakeResearchService:
        def collect(self, _topic, references=None, supplemental_text=""):
            return bundle

    def timeout_generate(*args, **kwargs):
        raise ProviderError("TIMEOUT", "model timeout")

    monkeypatch.setattr(single_task, "ResearchService", FakeResearchService)
    monkeypatch.setattr(single_task, "generate_article", timeout_generate)
    monkeypatch.setattr(single_task, "ensure_article_layout", lambda article: {**article, "layout_status": "passed", "layout_check": {"passed": True}})

    result = single_task.run_single_task(task, text_profile, image_profile, settings={"network": {}, "image_plan_mode": "none"}, store=store)

    assert result["status"] == "completed"
    assert CN_FALLBACK_NOTICE in str(result.get("fallback_notice") or "")
    assert int(result.get("text_generation_calls") or 0) <= 1
    assert "##" in str(result["article"].get("content_markdown") or "")


def test_IMAGE_DELAY_DOES_NOT_BLOCK_WORD_PASS(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store, task = _seed_completed_state(tmp_path, gate_status="failed")
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "_license_gate", lambda feature=None: None)
    monkeypatch.setattr(api, "load_settings", lambda: {"image_profile": {}, "network": {}})
    monkeypatch.setenv("HOTSPOT_LOCAL_API_TOKEN", "hf3-test-token")
    monkeypatch.setattr(api.executor, "is_running", lambda task_id: False)

    submitted = {}

    def fake_submit(task_id, fn):
        submitted["task_id"] = task_id
        future = Future()
        future.set_result(None)
        return future

    monkeypatch.setattr(api.executor, "submit", fake_submit)

    response = api.generate_selected_task_images(
        task["task_id"],
        api.ImageSelectionRequest(confirm_paid=True, include_cover=True, inline_count=0),
    )

    assert response.status_code == 400, response.body.decode("utf-8")
    assert "QUALITY_GATE_FAILED" in response.body.decode("utf-8")
    assert submitted == {}


def test_ANALYSIS_DOES_NOT_BLOCK_EXPORT_PASS(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store, task = _seed_completed_state(tmp_path, gate_status="warning")
    monkeypatch.setattr(api, "store", store)

    def fake_export_article(article: dict, path: Path, root: Path) -> None:
        path.write_bytes(b"hf3-docx")

    monkeypatch.setattr(api, "export_article", fake_export_article)

    response = api._article_export(task["task_id"], "word")

    assert str(response.path).endswith(".docx")
    assert Path(response.path).is_file()


def test_HF3_STATIC_GUARDS_PRESENT():
    ui_source = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")
    single_source = (ROOT / "generation" / "single_task.py").read_text(encoding="utf-8")
    batch_source = (ROOT / "generation" / "batch_executor.py").read_text(encoding="utf-8")
    research_source = (ROOT / "research" / "service.py").read_text(encoding="utf-8")
    article_source = (ROOT / "generation" / "article_generator.py").read_text(encoding="utf-8")
    api_source = (ROOT / "api.py").read_text(encoding="utf-8")
    components_source = (ROOT / "ui" / "components.py").read_text(encoding="utf-8")

    assert '"stage": "generating_article", "article_generation_started_at": utc_now(), "progress": 30' in single_source
    assert 'shared_research_bundle' in batch_source
    assert 'def _ensure_shared_research' in batch_source
    assert 'deadline = time.monotonic() + 60' in research_source
    assert 'urls = urls[:8]' in research_source
    assert 'query = str(getattr(topic, "title", "") or "").strip()' in research_source
    assert 'MAX_TEXT_GENERATION_CALLS = 3' in article_source
    assert 'token_budget = 3200' in article_source and 'token_budget = 2800' in article_source
    assert 'def _prompt_clip' in article_source and '6000' in article_source
    assert 'parse_json_response' in article_source and '_parse_markdown_article_response' in article_source
    assert '/topics/url-fetch' in ui_source
    assert '"reference_url": raw_input' in ui_source
    assert '"title": fetched_title[:300]' in ui_source
    assert '"/api/tasks/{task_id}/images/generate"' in api_source
    assert '\uFFFD' not in api_source
    assert CN_TIMEOUT_LABEL in components_source
