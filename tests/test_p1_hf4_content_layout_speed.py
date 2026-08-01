from __future__ import annotations

from pathlib import Path
import sys

import pytest
from docx import Document

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import generation.single_task as single_task
import generation.source_overlap as source_overlap
import modules.generation_store as generation_store
from export.docx_exporter import export_article
from export.layout_pipeline import check_article_product
from generation.article_generator import _init_generation_stats, _prompt, _register_text_generation_call
from generation.source_overlap import analyze_source_overlap
from modules.database import SQLiteStore
from modules.models import HotTopic
from providers.text_provider import ProviderError


CASE_ROOT = ROOT / ".hf4_manual"


@pytest.fixture
def tmp_path() -> Path:
    return CASE_ROOT


def _topic() -> HotTopic:
    return HotTopic(
        id="hf4-topic",
        title="HF4 排版测速专题",
        summary="围绕公开资料整理生成、排版门禁与导出流程的专项测试。",
        source="test",
        source_name="测试来源",
        source_url="https://example.com/topic",
    )


def _bundle() -> dict:
    return {
        "topic_id": "hf4-topic",
        "topic_title": "HF4 排版测速专题",
        "research_status": "sufficient",
        "accepted_source_count": 2,
        "official_or_reliable_source_count": 1,
        "usable_fact_count": 3,
        "timeline": ["2026-07-26", "2026-07-26T10:00:00"],
        "key_people": ["项目负责人"],
        "key_organizations": ["测试机构"],
        "research_fact_cards": [
            {
                "fact_id": "f1",
                "subject": "测试机构",
                "action": "发布",
                "object": "修复说明",
                "time": "2026-07-26",
                "location": "北京",
                "number": "1篇",
                "source_name": "测试机构",
                "source_url": "https://example.com/source-1",
                "reliability": "official",
                "fact": "测试机构于2026年7月26日发布修复说明，明确本轮检查聚焦排版和导出链路。",
            },
            {
                "fact_id": "f2",
                "subject": "项目团队",
                "action": "补充",
                "object": "流程说明",
                "time": "当日",
                "location": "上海",
                "number": "",
                "source_name": "行业媒体",
                "source_url": "https://example.com/source-2",
                "reliability": "source_page",
                "fact": "项目团队补充说明，要求正文可以直接排版导出，不再自动触发二次改写。",
            },
        ],
        "background_fact_cards": [
            {
                "fact_id": "b1",
                "subject": "专项复核",
                "action": "关注",
                "object": "生成时效",
                "time": "本轮",
                "location": "线上",
                "number": "",
                "source_name": "行业媒体",
                "source_url": "https://example.com/source-2",
                "reliability": "source_page",
                "fact": "专项复核关注三分钟内可获得正文或基础稿，并保证资料来源格式正常。",
            }
        ],
        "sources": [
            {
                "source_id": "s1",
                "source_name": "测试机构",
                "title": "HF4.1 排版测速公告",
                "published_at": "2026-07-26",
                "url": "https://example.com/source-1",
                "summary": "SOURCE_SUMMARY_SHOULD_NOT_BE_IN_PROMPT",
                "content": "测试机构于2026年7月26日发布公告，强调文章要能直接导出并通过排版检查。",
                "fetch_success": True,
                "accepted_for_research": True,
                "source_level": "official",
                "domain": "example.com",
            },
            {
                "source_id": "s2",
                "source_name": "行业媒体",
                "title": "HF4.1 复核观察",
                "published_at": "2026-07-26",
                "url": "https://example.com/source-2",
                "summary": "ANOTHER_SUMMARY_SENTINEL",
                "content": "行业媒体提到，本轮重点是取消自动二次调用并稳定输出可导出的中文文章。",
                "fetch_success": True,
                "accepted_for_research": True,
                "source_level": "source_page",
                "domain": "news.example.com",
            },
        ],
    }


def _article(title: str, body: str, *, calls: int = 1, reason: str = "") -> dict:
    markdown = (
        f"# {title}\n\n"
        "这是一段导语，用于说明文章主题和公开资料范围。\n\n"
        f"## 事件概览\n{body}\n\n"
        f"## 影响分析\n{body}\n\n"
        f"## 后续关注\n{body}"
    )
    return {
        "title": title,
        "intro": "这是一段导语，用于说明文章主题和公开资料范围。",
        "summary": "专项测试摘要",
        "sections": [
            {"heading": "事件概览", "body": body, "image_brief": "scene a"},
            {"heading": "影响分析", "body": body, "image_brief": "scene b"},
            {"heading": "后续关注", "body": body, "image_brief": "scene c"},
        ],
        "content_markdown": markdown,
        "source_list": ["[1] 测试机构：《HF4.1 排版测速公告》，2026-07-26\n原文链接：https://example.com/source-1"],
        "fact_basis": [],
        "ai_statement": "AI辅助声明：本文基于公开资料整理生成，发布前请再次核对关键信息。",
        "recommended_status": "completed",
        "text_generation_calls": calls,
        "text_generation_limit": 1,
        "text_generation_second_call_reason": reason,
    }


def test_ARTICLE_PROMPT_USES_FACT_CARDS_PASS():
    prompt = _prompt(
        _topic(),
        {"name": "公共价值", "instruction": "补充公众理解价值", "structure": ["概览", "影响"], "must_avoid": []},
        "解读",
        "客观",
        1200,
        None,
        _bundle(),
    )
    assert "SOURCE_SUMMARY_SHOULD_NOT_BE_IN_PROMPT" not in prompt
    assert "ANOTHER_SUMMARY_SENTINEL" not in prompt
    assert "关键事实卡" in prompt
    assert ("1200" in prompt and "1400" in prompt) or ("1500" in prompt and "1700" in prompt) or ("1600" in prompt and "1800" in prompt), f"prompt missing target range: ...{prompt[-200:]}"


def test_SOURCE_OVERLAP_LOCAL_CHECK_PASS(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(source_overlap, "LONG_COPY_CHARS", 10)
    body = "公开资料整理显示文章需要直接排版导出并保持中文来源格式，自动二次改写已经取消，人工复核仍然必要。"
    bundle = {
        "sources": [
            {
                "source_id": "s-local",
                "source_name": "本地来源",
                "fetch_success": True,
                "accepted_for_research": True,
                "content": body,
            }
        ],
        "research_fact_cards": [],
        "key_people": [],
        "key_organizations": [],
    }
    report = analyze_source_overlap(_article("来源重合测试", body), bundle)
    assert report["status"] == "review_required"
    assert report["violations"]


def test_ONE_AUTOMATIC_REWRITE_CALL_PER_ARTICLE_PASS():
    stats = _init_generation_stats({})
    _register_text_generation_call(stats, "INITIAL_GENERATION")
    _register_text_generation_call(stats, "INVALID_OUTPUT_RECOVERY")
    _register_text_generation_call(stats, "CONTENT_TOO_SHORT_REWRITE")
    assert stats["text_generation_calls"] == 3
    with pytest.raises(ProviderError):
        _register_text_generation_call(stats, "source_overlap_rewrite")


def test_SOURCE_OVERLAP_NO_AUTO_REWRITE_PASS(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store = SQLiteStore(tmp_path / "hf4.sqlite")
    topic = _topic()
    store.save_topics([topic])
    task = store.create_task(
        "HF4 task",
        "multi_topic",
        [topic.to_dict()],
        1,
        generation_options={"article_type": "解读", "style": "客观", "image_plan_mode": "none", "word_count": 800},
    )

    calls = {"count": 0}
    copied_body = "测试机构于2026年7月26日发布公告，强调文章要能直接导出并通过排版检查。" * 12

    monkeypatch.setattr(single_task, "_auto_collect_research", lambda state, store, topic: _bundle())
    monkeypatch.setattr(single_task, "sanitize_article_hard_facts", lambda article, bundle: {"article": article, "removed_claims": []})
    monkeypatch.setattr(single_task, "quality_gate", lambda article, bundle: {"status": "passed", "passed": True, "warnings": []})
    monkeypatch.setattr(single_task, "ensure_article_layout", lambda article: {**article, "layout_status": "passed", "layout_check": {"passed": True}})
    monkeypatch.setattr(
        single_task,
        "analyze_source_overlap",
        lambda article, bundle: {
            "status": "review_required",
            "violations": ["long_copy"],
            "max_five_gram_overlap": 0.61,
            "max_paragraph_order_overlap": 0.67,
            "matched_source_id": "s1",
            "matched_source_name": "测试机构",
        },
    )

    def fake_generate_article(*args, **kwargs):
        calls["count"] += 1
        return _article("来源重合测试", copied_body, calls=1)

    monkeypatch.setattr(single_task, "generate_article", fake_generate_article)
    result = single_task.run_single_task(
        task,
        {"api_key": "text-key", "model": "hf4-text", "timeout_seconds": 70},
        {"api_key": "image-key", "model": "hf4-image"},
        settings={"network": {}, "image_plan_mode": "none"},
        store=store,
    )
    assert result["status"] == "completed"
    assert result["text_generation_calls"] == 1
    assert result["text_generation_second_call_reason"] == ""
    assert result["quality_gate"]["status"] == "warning"
    assert result["article"]["review_required"] is True
    assert "建议人工修改后发布" in str(result.get("fallback_notice") or "")
    assert calls["count"] == 1


def test_WORD_INCLUDES_SOURCE_AND_OMITS_AI_SECTIONS_PASS(tmp_path: Path):
    body = "这是一段用于 Word 导出的正文内容，能够稳定通过段落检查并保留来源格式。" * 30
    article = _article("Word 导出测试", body)
    output = export_article(article, tmp_path / "hf4.docx")
    document = Document(output)
    paragraphs = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    all_text = "\n".join(paragraphs)
    assert paragraphs[0] == "Word 导出测试"
    assert "资料来源" in paragraphs
    assert "原文链接：" in all_text
    assert "AI辅助声明" not in all_text


def test_THREE_SECTION_THREE_PARAGRAPH_LAYOUT_PASS():
    article = _article("排版通过测试", "每个小节保留一段自然段内容，确保三节三段也能通过排版门禁。")
    report = check_article_product(article)
    assert report["passed"] is True


def test_PROGRESS_UI_SHOWS_REMAINING_TIME_PASS():
    text = (ROOT / "ui" / "components.py").read_text(encoding="utf-8-sig")
    assert "预计剩余时间约 {_remaining_seconds(state)} 秒" in text
    assert "_stage_elapsed_seconds(state)" in text
    assert "_article_elapsed_seconds(state)" in text
