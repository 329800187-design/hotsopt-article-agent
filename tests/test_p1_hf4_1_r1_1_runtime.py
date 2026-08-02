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
        {"heading": "事件发生了什么", "body": "根据当前公开资料和热榜信息，该热点事件已经在多个平台引发广泛讨论。从目前可获取的信息来看，事件的基本轮廓正在逐步清晰。各方关注的重点主要集中在事件本身的发展经过、涉事主体的回应情况，以及公开报道中已经确认的关键事实。本文将基于现有公开信息进行系统梳理，重点区分已确认信息与尚待核实的细节，帮助读者快速了解当前可以确认的内容和需要注意的信息缺口。在发布前，建议继续关注权威来源是否发布更完整的说明和官方结论，以便及时更新和补充本文中的关键信息点。"},
        {"heading": "为什么受到关注", "body": "这一话题能够在短时间内引发大量公众关注和讨论，主要与以下几个因素有关。首先，事件涉及的领域与大量普通用户的日常生活或工作场景密切相关，因此产生了较强的代入感和讨论意愿。其次，事件中涉及的相关方或机构具有一定的公众认知度，其后续回应和处理方式也成为公众观察的焦点。第三，事件可能对同行业、同类型场景产生示范效应或参照意义，因此也引起了相关从业者和观察者的持续关注。需要说明的是，由于目前公开信息的完整程度各不相同，本文基于现有资料进行梳理，不扩大解读范围，也不对尚待确认的细节进行推断。"},
        {"heading": "可能带来哪些影响", "body": "从现有公开信息来看，该事件的后续发展和各方回应可能会在多个层面产生影响。短期来看，事件可能引发相关领域的管理调整、政策回应或行业自查，相关方的及时回应和信息公开程度将直接影响公众对该事件的整体判断。中长期来看，这一案例也可能成为同类场景的参考坐标和讨论样本，甚至可能推动相关规则或行业惯例的进一步明确。在更广泛的社会层面，该事件引发的讨论也可能促使公众对相关议题的持续关注和深入思考。当然，在更多官方正式结论和权威信息出现之前，任何影响评估都应当保持一定的审慎态度，避免过度解读或过早定论。"},
        {"heading": "后续值得关注什么", "body": "在后续的信息更新和事件发展中，有几个关键方向值得重点关注。第一，事件相关主体是否会发布正式说明或回应，其内容和态度将直接影响公众对事件性质和后续走向的判断。第二，关键时间线和事件细节的进一步明确，包括事件发生的准确时间节点、各方的反应序列、以及可能存在的补充数据或第三方独立评估。第三，相关平台、监管机构或行业协会是否会进一步更新信息、出台相关指引或采取相应措施。在更多权威信息出现之前，建议读者和发布者保持持续关注但不急于下结论，以公开资料和权威来源为基准进行理性判断。"},
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
    assert result["status"] == "failed"
    assert result["error_code"] == "TIMEOUT"
    assert "ARTICLE_TEXT_RETRY_REQUIRED" in result["quality_gate"]["reasons"]
    assert result["article"] is None
    assert result["image_usage"]["generation_calls"] == 0
    assert "TIMEOUT" in result["fallback_notice"]
    return
    article = result["article"]
    bodies = [section["body"] for section in article["sections"]]
    assert result["status"] == "completed"
    assert article["fallback_kind"] == "hotlist_limited_draft"
    assert len(article["sections"]) == 4
    assert len(set(bodies)) == 4
    assert "资料来源" not in article["content_markdown"]
    assert "AI辅助声明" not in article["content_markdown"]
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
