from __future__ import annotations

import json
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


def _paragraph(seed: str = "\u516c\u5f00\u8d44\u6599") -> str:
    parts = [
        f"{seed}整理显示，当前话题需要先确认来源、时间和主要当事方，避免把热榜标题直接写成结论。",
        f"围绕{seed}，正文应说明读者可以核对哪些公开材料，并把已经确认的信息和推测性分析分开。",
        f"从背景解释看，相关讨论之所以扩散，是因为它连接了普通读者熟悉的生活场景和公共服务期待。",
        f"从传播风险看，标题里的悬念容易被二次加工，因此文章需要交代信息边界，而不是重复情绪判断。",
        f"从核验路径看，读者可以继续查看权威发布、媒体跟进和当事方说明，比较不同来源是否指向同一事实。",
        f"从影响分析看，事件后续可能推动流程说明更透明，但具体责任和结果必须等待进一步公开资料。",
        f"从读者价值看，文章应提供可操作的判断方法，例如保存车次时间、物品特征和沟通记录。",
        f"从同类案例看，遗失物寻找通常依赖具体线索、现场条件和后续联系渠道，不能凭空保证结果。",
        f"因此，{seed}相关写作要用事实卡组织材料，用动态小标题展开，不用固定模板凑满篇幅。",
    ]
    return "".join(parts)


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


def _quality_good_markdown_response() -> str:
    base = """# 非 JSON 可读长文

这是一段独立导语，说明当前热点需要基于公开资料进行整理，同时把已确认事实、传播风险和后续核验路径分开处理。

## 先确认资料边界
官方渠道已经公开发布说明，事件仍在发展中，正文不能把尚未确认的内容写成结论。读者首先需要知道哪些信息来自来源，哪些只是传播中的概括。

这部分写作的价值在于建立核验顺序：先看发布时间和发布主体，再比较不同来源是否指向同一事实，最后再判断是否需要补充背景。编辑在处理这类材料时，还要把来源名称、发布时间、原文链接和关键事实卡分开保存，避免后续写作时把网页栏目、推荐阅读或评论区文字误当成正文材料。

如果模型返回的是普通 Markdown，而不是约定 JSON，系统仍应优先判断它是否形成了可读文章。只要标题、导语、小标题和自然段完整，且不存在重复段落、模板污染和明显事实越界，就不应因为格式不标准直接判定任务失败。

## 为什么会被讨论
话题进入视野，往往不是因为单一事实本身，而是因为它和普通读者的生活经验、服务期待或行业规则产生了连接。文章应解释这种连接，而不是重复热榜标题。

对传播者来说，最需要避免的是把情绪性表述当作事实推进。较稳妥的写法，是把读者关心的问题列出来，再逐项说明目前能确认到什么。比如先交代事件何以被看见，再解释相关主体为什么需要回应，最后说明普通读者可以怎样等待后续更新。

这种写法和固定模板不同，它不是把同一事件经过塞进多个栏目，而是让每个小节承担不同任务。一个小节负责事实边界，一个小节负责传播原因，一个小节负责核验路径，最后再落到写作边界和读者行动。

## 后续如何核验
后续核验可以沿着三条路径推进：查看权威发布是否更新，观察媒体是否补充采访或文件，比较当事方说明和第三方报道是否存在冲突。

如果出现新的数字、处罚、伤亡或责任结论，应回到来源本身确认，而不是依据二次转述直接加入正文。这样能减少误读，也能提升文章可信度。对于尚未确认的信息，正文可以写清“目前未见进一步来源”，但不能反复用这句话凑字，更不能把推测包装成已经发生的结果。

核验过程还应记录失败路径，例如哪些候选链接被判定为不相关，哪些网页只提供预览文本，哪些内容包含动态模板字段。把这些信息留在研究包里，可以帮助用户重新生成正文时继续使用已有资料，而不是从零开始搜索。

## 写作上需要避开什么
这类文章不适合用固定五段模板填充篇幅，也不适合把网页预览提示、推荐阅读和客户端引导混进正文。每个小节都应承担不同信息功能。

最后的落点应回到读者行动：保留来源链接，标注信息边界，等待正式更新。这样即使资料还在变化，文章也能提供清晰判断路径。导出 Word 时，只应写入标题、导语、正文、图片和受限来源列表，不应该写入 raw_source_text、research_bundle、provider_response 或任何调试字段。

如果质量门发现正文仍然重复、污染或残句，系统可以尝试一次针对性重写；重写后仍失败，就应阻断图片和正式 Word。这样的失败不是坏结果，而是把不可交付内容挡在客户文件之外。"""
    extra = """

## 交付前还要看什么
交付前的最后检查，应把正文和来源分开看。正文要确认每个自然段承担不同功能，来源区只保留平台、标题和链接，不能把整页抓取内容放进文档。

同时还要检查跨文章差异。如果同一批次里多篇文章只是替换标题、沿用相同段落顺序和表达句式，就算单篇没有明显错字，也不应作为最终客户交付。"""
    extra += "\n\n这条规则可以帮助测试确认：格式容错不等于质量放行，只有内容本身达标且结构完整、来源清楚、表达不重复时，非 JSON 响应才应进入完成态。"
    return base + extra


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
    assert article["recommended_status"] in {"completed", "warning"}
    assert article.get("response_format_warning") is not True


def test_FENCED_JSON_ARTICLE_PASS(monkeypatch: pytest.MonkeyPatch):
    payload = _article_payload("\u56f4\u680f JSON")
    response = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    monkeypatch.setattr(article_generator.OpenAITextProvider, "generate", lambda _self, prompt, temperature=0.8, max_tokens=3000: response)
    article = article_generator.generate_article(_topic(), plan_angles(1)[0], "\u70ed\u70b9\u8d44\u8baf", "\u5ba2\u89c2\u901a\u4fd7", 1200, _profile(), research_bundle=_bundle(_topic()))
    assert article["title"] == "\u56f4\u680f JSON"
    assert article["recommended_status"] in {"completed", "warning"}


def test_JSON_WITH_PROSE_PASS(monkeypatch: pytest.MonkeyPatch):
    payload = _article_payload("\u5e26\u89e3\u91ca JSON")
    response = "\u4e0b\u9762\u662f\u6587\u7ae0 JSON\uff1a\n" + json.dumps(payload, ensure_ascii=False) + "\n\u4ee5\u4e0a\u5185\u5bb9\u4f9b\u53c2\u8003\u3002"
    monkeypatch.setattr(article_generator.OpenAITextProvider, "generate", lambda _self, prompt, temperature=0.8, max_tokens=3000: response)
    article = article_generator.generate_article(_topic(), plan_angles(1)[0], "\u70ed\u70b9\u8d44\u8baf", "\u5ba2\u89c2\u901a\u4fd7", 1200, _profile(), research_bundle=_bundle(_topic()))
    assert article["title"] == "\u5e26\u89e3\u91ca JSON"
    assert article["recommended_status"] in {"completed", "warning"}


def test_MARKDOWN_RESPONSE_FALLBACK_PASS(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(article_generator.OpenAITextProvider, "generate", lambda _self, prompt, temperature=0.8, max_tokens=3000: _markdown_response())
    article = article_generator.generate_article(_topic(), plan_angles(1)[0], "\u70ed\u70b9\u8d44\u8baf", "\u5ba2\u89c2\u901a\u4fd7", 1200, _profile(), research_bundle=_bundle(_topic()))
    assert article["response_format_warning"] is False
    assert article["fallback_kind"] == ""
    assert article["used_local_fallback"] is False
    assert article["response_parser_mode"] == "markdown"
    assert "## " in article["content_markdown"]


def test_PLAIN_TEXT_RESPONSE_FALLBACK_PASS(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(article_generator.OpenAITextProvider, "generate", lambda _self, prompt, temperature=0.8, max_tokens=3000: _plain_text_response())
    article = article_generator.generate_article(_topic(), plan_angles(1)[0], "\u70ed\u70b9\u8d44\u8baf", "\u5ba2\u89c2\u901a\u4fd7", 1200, _profile(), research_bundle=_bundle(_topic()))
    assert article["response_format_warning"] is False
    assert article["fallback_kind"] == ""
    assert article["used_local_fallback"] is False
    assert article["title"] == "\u666e\u901a\u6587\u672c\u6807\u9898"


def test_INVALID_JSON_NOT_TASK_FAILURE_PASS(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    response = "\u6a21\u578b\u8bf4\u660e\uff1a\u8fd9\u6b21\u672a\u6309 JSON \u8fd4\u56de\uff0c\u4f46\u6b63\u6587\u53ef\u8bfb\u3002\n\n" + _quality_good_markdown_response()
    result = _run_single_task(monkeypatch, tmp_path, response)
    assert result["status"] == "completed"
    assert result["quality_gate"]["status"] == "warning"
    assert result["fallback_notice"] == ""
    assert result["article"]["fallback_kind"] == ""
    assert result["article"]["used_local_fallback"] is False


def test_EMPTY_RESPONSE_LOCAL_DRAFT_PASS(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    result = _run_single_task(monkeypatch, tmp_path, "   ")
    assert result["status"] == "failed"
    assert result["error_code"] == "ARTICLE_PARSE_ERROR"
    assert "ARTICLE_TEXT_RETRY_REQUIRED" in result["quality_gate"]["reasons"]
    assert result["quality_gate"]["status"] == "failed"
    assert result["article"] is None
    assert "ARTICLE_PARSE_ERROR" in result["fallback_notice"]
