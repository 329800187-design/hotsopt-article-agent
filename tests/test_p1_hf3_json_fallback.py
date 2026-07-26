from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

import generation.article_generator as article_generator
import generation.single_task as single_task
import modules.generation_store as generation_store
from generation.angle_planner import plan_angles
from modules.database import SQLiteStore
from modules.models import HotTopic


TOPIC_TITLE = "\u70ed\u70b9\u751f\u6210\u6d4b\u8bd5"
TOPIC_SUMMARY = "\u516c\u5f00\u8d44\u6599\u663e\u793a\u4e8b\u4ef6\u4ecd\u5728\u53d1\u5c55\uff0c\u9700\u8981\u57fa\u4e8e\u73b0\u6709\u6765\u6e90\u6574\u7406\u6587\u7ae0\u3002"
FORMAT_WARNING = "\u6587\u7ae0\u5df2\u751f\u6210\uff0c\u4f46\u6a21\u578b\u8fd4\u56de\u683c\u5f0f\u4e0d\u6807\u51c6\uff0c\u5df2\u81ea\u52a8\u8f6c\u6362\u4e3a\u53ef\u7f16\u8f91\u6587\u7ae0\u3002"
LOCAL_DRAFT_NOTICE = "\u5df2\u751f\u6210\u57fa\u7840\u7a3f\n\u5f53\u524d\u6a21\u578b\u8fd4\u56de\u5f02\u5e38\uff0c\u8f6f\u4ef6\u5df2\u6839\u636e\u516c\u5f00\u8d44\u6599\u751f\u6210\u53ef\u7f16\u8f91\u7248\u672c\u3002"


ROOT = Path(__file__).resolve().parents[1]
CASE_ROOT = ROOT / ".hf3_manual"


@pytest.fixture(autouse=True)
def signed_test_license():
    yield


@pytest.fixture
def tmp_path() -> Path:
    return CASE_ROOT


def _paragraph(seed: str = "\u516c\u5f00\u8d44\u6599") -> str:
    base = f"{seed}\u6574\u7406\u663e\u793a\uff0c\u5f53\u524d\u8bdd\u9898\u4ecd\u9700\u7ed3\u5408\u6743\u5a01\u6765\u6e90\u6301\u7eed\u6838\u5bf9\uff0c\u540c\u65f6\u5173\u6ce8\u80cc\u666f\u4fe1\u606f\u3001\u8bfb\u8005\u4ef7\u503c\u548c\u540e\u7eed\u7814\u7a76\u65b9\u5411\u3002"
    return base * 8


def _topic() -> HotTopic:
    return HotTopic(
        id="hf3-json-topic",
        title=TOPIC_TITLE,
        summary=TOPIC_SUMMARY,
        source="test",
        source_name="source",
        source_url="https://example.com/topic",
    )


def _profile() -> dict[str, str | int]:
    return {
        "api_key": "test-key",
        "base_url": "https://example.com/v1",
        "endpoint": "/chat/completions",
        "model": "hf3-text-model",
        "timeout_seconds": 180,
    }


def _article_payload(title: str = "\u6807\u51c6JSON\u6587\u7ae0") -> dict:
    body_a = _paragraph("\u4e8b\u4ef6\u6982\u89c8")
    body_b = _paragraph("\u80cc\u666f\u8865\u5145")
    body_c = _paragraph("\u5f71\u54cd\u89c2\u5bdf")
    return {
        "title": title,
        "intro": "\u8fd9\u662f\u4e00\u6bb5\u57fa\u4e8e\u516c\u5f00\u8d44\u6599\u6574\u7406\u7684\u5bfc\u8bed\u3002",
        "summary": "\u6807\u51c6 JSON \u6458\u8981",
        "sections": [
            {"heading": "\u4e8b\u4ef6\u6982\u89c8", "body": body_a, "image_brief": "scene a"},
            {"heading": "\u80cc\u666f\u4fe1\u606f", "body": body_b, "image_brief": "scene b"},
            {"heading": "\u5f71\u54cd\u4e0e\u89c2\u5bdf", "body": body_c, "image_brief": "scene c"},
        ],
        "content_markdown": f"# {title}\n\n\u8fd9\u662f\u4e00\u6bb5\u57fa\u4e8e\u516c\u5f00\u8d44\u6599\u6574\u7406\u7684\u5bfc\u8bed\u3002\n\n## \u4e8b\u4ef6\u6982\u89c8\n{body_a}\n\n## \u80cc\u666f\u4fe1\u606f\n{body_b}\n\n## \u5f71\u54cd\u4e0e\u89c2\u5bdf\n{body_c}",
        "fact_basis": [],
        "source_list": [],
        "ai_statement": "AI\u8f85\u52a9\u58f0\u660e\uff1a\u672c\u6587\u57fa\u4e8e\u516c\u5f00\u8d44\u6599\u6574\u7406\u751f\u6210\u3002",
    }


def _bundle(topic: HotTopic) -> dict:
    return {
        "topic_id": topic.id,
        "topic_title": topic.title,
        "research_status": "sufficient",
        "accepted_source_count": 2,
        "official_or_reliable_source_count": 1,
        "usable_fact_count": 3,
        "timeline": ["2026-07-26", "2026-07-26T10:00:00"],
        "background": ["\u76f8\u5173\u80cc\u666f\u4fe1\u606f\u6765\u81ea\u5df2\u516c\u5f00\u8d44\u6599\u6574\u7406\u3002"],
        "verified_facts": [
            {"fact_id": "f1", "canonical_fact": "\u5b98\u65b9\u6e20\u9053\u5df2\u516c\u5f00\u53d1\u5e03\u8bf4\u660e\u3002", "supporting_source_ids": ["s1", "s2"]},
            {"fact_id": "f2", "canonical_fact": "\u4e8b\u4ef6\u4ecd\u5728\u53d1\u5c55\u4e2d\uff0c\u5f85\u540e\u7eed\u66f4\u65b0\u3002", "supporting_source_ids": ["s1", "s2"]},
        ],
        "usable_facts": [
            {"fact_id": "f1", "canonical_fact": "\u5b98\u65b9\u6e20\u9053\u5df2\u516c\u5f00\u53d1\u5e03\u8bf4\u660e\u3002", "supporting_source_ids": ["s1", "s2"]},
            {"fact_id": "f2", "canonical_fact": "\u4e8b\u4ef6\u4ecd\u5728\u53d1\u5c55\u4e2d\uff0c\u5f85\u540e\u7eed\u66f4\u65b0\u3002", "supporting_source_ids": ["s1", "s2"]},
        ],
        "sources": [
            {
                "source_id": "s1",
                "source_name": "\u5b98\u65b9\u901a\u544a",
                "title": "\u516c\u5f00\u8bf4\u660e",
                "published_at": "2026-07-26",
                "url": "https://example.com/source-1",
                "summary": "\u5b98\u65b9\u516c\u5f00\u4fe1\u606f\u5df2\u7ecf\u53d1\u5e03\u3002",
                "content": "\u5b98\u65b9\u516c\u5f00\u4fe1\u606f\u5df2\u7ecf\u53d1\u5e03\uff0c\u4e8b\u4ef6\u4ecd\u5728\u53d1\u5c55\u4e2d\u3002",
                "fetch_success": True,
                "accepted_for_research": True,
                "source_level": "official",
                "publisher_id": "example.com",
                "domain": "example.com",
            },
            {
                "source_id": "s2",
                "source_name": "\u5a92\u4f53\u62a5\u9053",
                "title": "\u8fdb\u5c55\u8ddf\u8e2a",
                "published_at": "2026-07-26",
                "url": "https://news.example.com/source-2",
                "summary": "\u5a92\u4f53\u8ddf\u8e2a\u4e86\u8fd9\u6b21\u4e8b\u4ef6\u7684\u540e\u7eed\u5f71\u54cd\u3002",
                "content": "\u5a92\u4f53\u8ddf\u8e2a\u4e86\u8fd9\u6b21\u4e8b\u4ef6\u7684\u540e\u7eed\u5f71\u54cd\uff0c\u5e76\u6307\u51fa\u4e8b\u4ef6\u4ecd\u5728\u53d1\u5c55\u4e2d\u3002",
                "fetch_success": True,
                "accepted_for_research": True,
                "source_level": "source_page",
                "publisher_id": "news.example.com",
                "domain": "news.example.com",
            },
        ],
    }


def _markdown_response() -> str:
    body_a = _paragraph("\u5386\u53f2\u80cc\u666f")
    body_b = _paragraph("\u6280\u672f\u610f\u4e49")
    body_c = _paragraph("\u8bfb\u8005\u4ef7\u503c")
    return (
        "# Markdown \u957f\u6587\n\n"
        "\u8fd9\u662f\u57fa\u4e8e\u516c\u5f00\u8d44\u6599\u6574\u7406\u7684\u5bfc\u8bed\u3002\n\n"
        f"## \u5386\u53f2\u80cc\u666f\n{body_a}\n\n"
        f"## \u6280\u672f\u610f\u4e49\n{body_b}\n\n"
        f"## \u8bfb\u8005\u4ef7\u503c\n{body_c}"
    )


def _plain_text_response() -> str:
    return (
        "\u666e\u901a\u6587\u672c\u6807\u9898\n\n"
        + _paragraph("\u7b2c\u4e00\u6bb5") + "\n\n"
        + _paragraph("\u7b2c\u4e8c\u6bb5") + "\n\n"
        + _paragraph("\u7b2c\u4e09\u6bb5")
    )


def _make_store_and_task(tmp_path: Path) -> tuple[SQLiteStore, dict, HotTopic]:
    store = SQLiteStore(tmp_path / f"hf3-json-{time.time_ns()}.sqlite")
    topic = _topic()
    store.save_topics([topic])
    task = store.create_task(
        "HF3 JSON fallback",
        "multi_topic",
        [topic.to_dict()],
        1,
        generation_options={"article_type": "news", "style": "neutral", "image_plan_mode": "none", "word_count": 1200},
    )
    return store, task, topic


def _run_single_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, response_text: str) -> dict:
    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / f"tasks-{time.time_ns()}")
    store, task, topic = _make_store_and_task(tmp_path)
    bundle = _bundle(topic)

    class FakeResearchService:
        def collect(self, _topic, references=None, supplemental_text=""):
            return bundle

    def fake_generate(_self, prompt: str, temperature: float = 0.8, max_tokens: int = 3000) -> str:
        return response_text

    monkeypatch.setattr(single_task, "ResearchService", FakeResearchService)
    monkeypatch.setattr(single_task, "ensure_article_layout", lambda article: {**article, "layout_status": "passed", "layout_check": {"passed": True}})
    monkeypatch.setattr(article_generator.OpenAITextProvider, "generate", fake_generate)

    return single_task.run_single_task(task, _profile(), {"api_key": "image-key"}, settings={"network": {}, "image_plan_mode": "none"}, store=store)


def test_STANDARD_JSON_ARTICLE_PASS(monkeypatch: pytest.MonkeyPatch):
    payload = _article_payload("\u6807\u51c6 JSON")
    monkeypatch.setattr(article_generator.OpenAITextProvider, "generate", lambda _self, prompt, temperature=0.8, max_tokens=3000: json.dumps(payload, ensure_ascii=False))
    article = article_generator.generate_article(_topic(), plan_angles(1)[0], "\u70ed\u70b9\u8d44\u8baf", "\u5ba2\u89c2\u901a\u4fd7", 1200, _profile(), research_bundle=_bundle(_topic()))
    assert article["title"] == "\u6807\u51c6 JSON"
    assert article["recommended_status"] == "completed"
    assert article.get("response_format_warning") is not True


def test_FENCED_JSON_ARTICLE_PASS(monkeypatch: pytest.MonkeyPatch):
    payload = _article_payload("\u56f4\u680f JSON")
    response = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    monkeypatch.setattr(article_generator.OpenAITextProvider, "generate", lambda _self, prompt, temperature=0.8, max_tokens=3000: response)
    article = article_generator.generate_article(_topic(), plan_angles(1)[0], "\u70ed\u70b9\u8d44\u8baf", "\u5ba2\u89c2\u901a\u4fd7", 1200, _profile(), research_bundle=_bundle(_topic()))
    assert article["title"] == "\u56f4\u680f JSON"
    assert article["recommended_status"] == "completed"


def test_JSON_WITH_PROSE_PASS(monkeypatch: pytest.MonkeyPatch):
    payload = _article_payload("\u5e26\u89e3\u91ca JSON")
    response = "\u4e0b\u9762\u662f\u6587\u7ae0 JSON\uff1a\n" + json.dumps(payload, ensure_ascii=False) + "\n\u4ee5\u4e0a\u5185\u5bb9\u4f9b\u53c2\u8003\u3002"
    monkeypatch.setattr(article_generator.OpenAITextProvider, "generate", lambda _self, prompt, temperature=0.8, max_tokens=3000: response)
    article = article_generator.generate_article(_topic(), plan_angles(1)[0], "\u70ed\u70b9\u8d44\u8baf", "\u5ba2\u89c2\u901a\u4fd7", 1200, _profile(), research_bundle=_bundle(_topic()))
    assert article["title"] == "\u5e26\u89e3\u91ca JSON"
    assert article["recommended_status"] == "completed"


def test_MARKDOWN_RESPONSE_FALLBACK_PASS(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(article_generator.OpenAITextProvider, "generate", lambda _self, prompt, temperature=0.8, max_tokens=3000: _markdown_response())
    article = article_generator.generate_article(_topic(), plan_angles(1)[0], "\u70ed\u70b9\u8d44\u8baf", "\u5ba2\u89c2\u901a\u4fd7", 1200, _profile(), research_bundle=_bundle(_topic()))
    assert article["response_format_warning"] is True
    assert article["fallback_kind"] == "markdown_fallback"
    assert article["recommended_status"] == "review_required"
    assert "## " in article["content_markdown"]


def test_PLAIN_TEXT_RESPONSE_FALLBACK_PASS(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(article_generator.OpenAITextProvider, "generate", lambda _self, prompt, temperature=0.8, max_tokens=3000: _plain_text_response())
    article = article_generator.generate_article(_topic(), plan_angles(1)[0], "\u70ed\u70b9\u8d44\u8baf", "\u5ba2\u89c2\u901a\u4fd7", 1200, _profile(), research_bundle=_bundle(_topic()))
    assert article["response_format_warning"] is True
    assert article["fallback_kind"] == "plain_text_fallback"
    assert article["recommended_status"] == "review_required"
    assert article["title"] == "\u666e\u901a\u6587\u672c\u6807\u9898"


def test_INVALID_JSON_NOT_TASK_FAILURE_PASS(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    response = "\u6a21\u578b\u8bf4\u660e\uff1a\u8fd9\u6b21\u672a\u6309 JSON \u8fd4\u56de\uff0c\u4f46\u6b63\u6587\u53ef\u8bfb\u3002\n\n" + _plain_text_response()
    result = _run_single_task(monkeypatch, tmp_path, response)
    assert result["status"] == "completed"
    assert result["quality_gate"]["status"] == "warning"
    assert result["fallback_notice"] == FORMAT_WARNING
    assert result["article"]["fallback_kind"] == "plain_text_fallback"


def test_EMPTY_RESPONSE_LOCAL_DRAFT_PASS(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    result = _run_single_task(monkeypatch, tmp_path, "   ")
    assert result["status"] == "completed"
    assert result["quality_gate"]["status"] == "warning"
    assert result["fallback_notice"] == LOCAL_DRAFT_NOTICE
    assert result["article"]["fallback_kind"] == "local_research_draft"
    assert "\u8d44\u6599\u6765\u6e90" in result["article"]["content_markdown"]
