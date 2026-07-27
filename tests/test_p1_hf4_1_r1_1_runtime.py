from __future__ import annotations

from pathlib import Path
import sys

from docx import Document

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import generation.single_task as single_task
import modules.generation_store as generation_store
from export.docx_exporter import export_article
from modules.database import SQLiteStore
from modules.models import HotTopic
from providers.text_provider import ProviderError


def _topic(topic_id: str = "r1-1-topic", *, empty: bool = False) -> HotTopic:
    return HotTopic(
        id=topic_id,
        title="" if empty else "某热点进入热榜讨论",
        summary="" if empty else "热榜摘要显示，该话题正在引发用户关注，更多权威信息仍待确认。",
        source="test-hotlist",
        source_name="" if empty else "测试热榜",
        source_url="" if empty else "https://example.com/hot",
        hot_value="" if empty else "热度 1000",
    )


def _task(store: SQLiteStore, topic: HotTopic) -> dict:
    store.save_topics([topic])
    return store.create_task(
        f"R1.1 {topic.id}",
        "multi_topic",
        [topic.to_dict()],
        1,
        generation_options={"article_type": "热点资讯", "style": "客观通俗", "image_plan_mode": "none", "word_count": 800},
    )


def _zero_bundle(topic: HotTopic) -> dict:
    return {
        "topic_id": topic.id,
        "topic_title": topic.title,
        "research_status": "not_collected",
        "accepted_source_count": 0,
        "official_or_reliable_source_count": 0,
        "candidate_link_count": 0,
        "rejected_source_count": 0,
        "sources": [],
        "usable_facts": [],
        "research_fact_cards": [],
    }


def _one_source_bundle(topic: HotTopic) -> dict:
    return {
        "topic_id": topic.id,
        "topic_title": topic.title,
        "research_status": "sufficient",
        "accepted_source_count": 1,
        "official_or_reliable_source_count": 1,
        "usable_fact_count": 1,
        "sources": [
            {
                "source_id": "s1",
                "source_name": "测试机构",
                "title": "测试事件说明",
                "published_at": "2026-07-27",
                "url": "https://example.com/source",
                "summary": "测试机构发布事件说明。",
                "content": "测试机构发布事件说明，确认该热点已有可用公开资料。",
                "fetch_success": True,
                "accepted_for_research": True,
                "source_level": "official",
                "domain": "example.com",
            }
        ],
        "usable_facts": [
            {
                "fact_id": "f1",
                "canonical_fact": "测试机构发布事件说明，确认该热点已有可用公开资料。",
                "source_ids": ["s1"],
                "supporting_source_ids": ["s1"],
                "verification_type": "official_single_source",
            }
        ],
        "research_fact_cards": [],
    }


def _article(topic: HotTopic) -> dict:
    sections = [
        {"heading": "事件发生了什么", "body": "根据当前资料，该热点已经进入公开讨论。文章围绕已确认信息进行整理，并保留后续核对空间。", "image_brief": "新闻事件概览，无文字"},
        {"heading": "为什么受到关注", "body": "这一话题受到关注，主要是因为公众希望了解事件进展、相关主体回应以及后续可能出现的新信息。", "image_brief": "公众讨论新闻，无文字"},
        {"heading": "可能带来哪些影响", "body": "从现有资料看，事件可能影响相关讨论节奏，也提醒发布者在事实尚未充分前保持谨慎表达。", "image_brief": "新闻分析场景，无文字"},
        {"heading": "后续值得关注什么", "body": "后续需要关注权威来源是否发布完整说明，以及人物、时间、数字等关键信息能否进一步确认。", "image_brief": "公告更新场景，无文字"},
    ]
    source = "[1] 测试机构：《测试事件说明》，2026-07-27\n原文链接：https://example.com/source"
    markdown = "\n\n".join(
        [f"# {topic.title}的新观察", "导语：这是一段重新生成的导语，用于说明事件信息、资料范围和后续核对方向。"]
        + [f"## {item['heading']}\n{item['body']}" for item in sections]
        + [f"## 资料来源\n{source}", "AI辅助声明：本文根据公开资料和AI辅助生成，发布前请核对人物、时间、数字和来源。"]
    )
    return {
        "title": f"{topic.title}的新观察",
        "intro": "导语：这是一段重新生成的导语，用于说明事件信息、资料范围和后续核对方向。",
        "summary": topic.summary,
        "sections": sections,
        "content_markdown": markdown,
        "source_list": [source],
        "source_statement": source,
        "ai_statement": "AI辅助声明：本文根据公开资料和AI辅助生成，发布前请核对人物、时间、数字和来源。",
        "fact_basis": [],
        "recommended_status": "completed",
        "text_generation_calls": 1,
        "text_generation_limit": 1,
    }


def _run(tmp_path: Path, monkeypatch, topic: HotTopic, collect, generate):
    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    monkeypatch.setattr(single_task.ResearchService, "collect", collect)
    monkeypatch.setattr(single_task, "generate_article", generate)
    monkeypatch.setattr(single_task, "analyze_source_overlap", lambda article, bundle: {"status": "passed", "violations": []})
    store = SQLiteStore(tmp_path / f"{topic.id}.sqlite")
    task = _task(store, topic)
    return single_task.run_single_task(
        task,
        {"api_key": "text-key", "model": "test-text", "timeout_seconds": 30},
        {"api_key": "image-key", "model": "test-image"},
        settings={"network": {}, "image_plan_mode": "none"},
        store=store,
    )


def test_ZERO_ACCEPTED_SOURCE_WITH_HOTLIST_METADATA_GENERATES_ARTICLE_AND_WORD(tmp_path: Path, monkeypatch):
    topic = _topic("r1-1-zero")
    result = _run(
        tmp_path,
        monkeypatch,
        topic,
        lambda self, topic, references=None, supplemental_text="": _zero_bundle(topic),
        lambda topic, *args, **kwargs: _article(topic),
    )
    assert result["status"] == "completed"
    assert result["research_bundle"]["research_status"] == "hotlist_limited"
    assert result["quality_gate"]["status"] == "warning"
    assert result["error_code"] != "RESEARCH_NOT_COLLECTED"
    output = export_article(result["article"], tmp_path / "zero-source.docx")
    assert Document(output).paragraphs[0].text == result["article"]["title"]


def test_ZERO_ACCEPTED_SOURCE_TIMEOUT_USES_LOCAL_COMPLETE_DRAFT(tmp_path: Path, monkeypatch):
    topic = _topic("r1-1-timeout")
    result = _run(
        tmp_path,
        monkeypatch,
        topic,
        lambda self, topic, references=None, supplemental_text="": _zero_bundle(topic),
        lambda *args, **kwargs: (_ for _ in ()).throw(ProviderError("TIMEOUT", "timeout")),
    )
    article = result["article"]
    bodies = [section["body"] for section in article["sections"]]
    assert result["status"] == "completed"
    assert article["fallback_kind"] == "hotlist_limited_draft"
    assert len(article["sections"]) == 4
    assert len(set(bodies)) == 4
    assert "资料来源" in article["content_markdown"]
    assert export_article(article, tmp_path / "timeout-fallback.docx").is_file()


def test_ZERO_ACCEPTED_SOURCE_WITHOUT_ANY_HOTLIST_METADATA_STILL_BLOCKS(tmp_path: Path, monkeypatch):
    topic = _topic("r1-1-empty", empty=True)
    result = _run(
        tmp_path,
        monkeypatch,
        topic,
        lambda self, topic, references=None, supplemental_text="": _zero_bundle(topic),
        lambda topic, *args, **kwargs: _article(topic),
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "RESEARCH_NOT_COLLECTED"


def test_ACCEPTED_SOURCE_PATH_IS_NOT_MARKED_HOTLIST_LIMITED(tmp_path: Path, monkeypatch):
    topic = _topic("r1-1-source")
    result = _run(
        tmp_path,
        monkeypatch,
        topic,
        lambda self, topic, references=None, supplemental_text="": _one_source_bundle(topic),
        lambda topic, *args, **kwargs: _article(topic),
    )
    assert result["status"] == "completed"
    assert result["research_bundle"]["accepted_source_count"] == 1
    assert result["research_bundle"]["research_status"] == "sufficient"
    assert not result["research_bundle"].get("hotlist_metadata_available")
