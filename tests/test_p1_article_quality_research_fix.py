from __future__ import annotations

from docx import Document

from generation.article_generator import _complete_article_structure, _length_contract, _parse_markdown_article_response, _prompt
from generation.article_generator import generate_article as ag_generate_article
from generation.content_quality import _cleanup_claim_text, quality_gate
from generation.image_budget import count_body_chinese_chars, recommended_word_count
from modules.config_store import _migrate_word_count_settings
from export.docx_exporter import export_article
from export.layout_pipeline import prepare_article_layout
from modules.models import HotTopic
from providers.text_provider import OpenAITextProvider
from research.service import ResearchService
import ui.rc1_app as rc1_app


def _article_topic() -> HotTopic:
    return HotTopic(id="t1", title="原始话题标题", category="综合热点", summary="话题摘要")


def _article_angle() -> dict[str, str]:
    return {"name": "新闻资讯", "instruction": "事实优先"}


def _count_docx_text(path, text: str) -> int:
    doc = Document(path)
    return "\n".join(paragraph.text for paragraph in doc.paragraphs).count(text)


def test_bing_fallback_uses_valid_query_url(monkeypatch) -> None:
    captured: list[str] = []

    def fake_search(query: str, endpoint: str, name: str) -> list[str]:
        captured.append(endpoint.format(query=query))
        return []

    monkeypatch.setattr(ResearchService, "_search_html", staticmethod(fake_search))
    ResearchService().fallback_discoverer(HotTopic(id="t1", title="测试话题"))

    assert captured == ["https://www.bing.com/search?q=测试话题"]


def test_internal_structure_labels_are_not_exported_as_headings() -> None:
    topic = HotTopic(id="t1", title="测试热点", category="综合热点")
    article = {
        "title": "新标题",
        "intro": "这是一段足够长的导语，用来说明事件和文章角度。",
        "sections": [
            {"heading": "钩子开头", "body": "第一段正文，解释为什么读者会关注这个话题。"},
            {"heading": "30秒速览", "body": "第二段正文，快速交代已经发生的事实。"},
            {"heading": "单点深挖", "body": "第三段正文，围绕一个关键细节展开。"},
            {"heading": "观点判断", "body": "第四段正文，给出克制的分析判断。"},
            {"heading": "结尾互动", "body": "第五段正文，留下可以继续观察的问题。"},
        ],
    }

    completed = _complete_article_structure(article, topic, {"name": "新闻资讯"}, ("钩子开头", "30秒速览", "单点深挖", "观点判断", "结尾互动"))
    headings = [section["heading"] for section in completed["sections"]]

    assert "钩子开头" not in headings
    assert "30秒速览" not in headings
    assert "单点深挖" not in headings
    assert "为什么这个话题会被点开" in headings
    assert "先看清楚已经发生了什么" in headings


def test_cleanup_claim_text_removes_whole_sentence_without_punctuation_debris() -> None:
    text = "现有材料能够确认的是这条热榜信息及其传播位置，、信件内容和事件经过。后续仍需观察权威信息。"
    cleaned = _cleanup_claim_text(text, ["信件内容和事件经过"])

    assert "信件内容和事件经过" not in cleaned
    assert "，、" not in cleaned
    assert "、，" not in cleaned


def test_layout_pipeline_does_not_append_hardcoded_padding() -> None:
    article = {
        "title": "暑期文旅消费持续升温",
        "intro": "暑期出行需求上升，文旅市场出现新的消费变化。",
        "word_count": 1200,
        "sections": [
            {"heading": "事件发生了什么", "body": "多地文旅消费在暑期保持活跃，景区、酒店和交通场景都出现更高热度。"},
            {"heading": "为什么受到关注", "body": "这类话题关系到出行预算、服务供给和地方消费恢复，读者更关心价格、体验和承载能力。"},
            {"heading": "可能带来哪些影响", "body": "消费升温可能带动住宿、餐饮和周边服务，也会考验目的地管理和服务质量。"},
            {"heading": "后续值得关注什么", "body": "后续应关注价格波动、景区承载、服务投诉和错峰出行安排。"},
        ],
    }

    result = prepare_article_layout(article)
    markdown = result["content_markdown"]

    assert "铁路或属地部门" not in markdown
    assert "公共空间里的小摩擦" not in markdown
    assert "身份和情绪" not in markdown
    assert result["body_char_count"] < 1200


def test_length_contract_hotspot_1200_prompt_rules() -> None:
    topic = HotTopic(id="t1", title="智能驾驶提示灯会成为行业趋势吗", category="科技")
    prompt = _prompt(topic, _article_angle(), "热点资讯", "客观通俗", 1200, research_bundle={"research_status": "sufficient"})

    assert "每个二级标题下 1～2 个自然段" not in prompt
    assert "每段 90～180 个中文汉字" not in prompt
    assert "每个小节必须恰好 2 个自然段" in prompt
    assert "每段 145～165 个中文汉字" in prompt
    assert "不要 JSON" in prompt
    assert "不要代码围栏" in prompt
    assert len(prompt) <= 6000


def test_length_contract_hotspot_1500_and_1600_rules() -> None:
    assert _length_contract(1500)["paragraph_length"] == "每段 178～195 个中文汉字"
    assert _length_contract(1600)["paragraph_length"] == "每段 190～205 个中文汉字"


def test_length_contract_custom_topic_rules() -> None:
    assert _length_contract(1200, custom_topic=True)["paragraph_length"] == "每段 115～125 个中文汉字"
    assert _length_contract(1500, custom_topic=True)["paragraph_length"] == "每段 140～155 个中文汉字"
    assert _length_contract(1600, custom_topic=True)["paragraph_length"] == "每段 150～165 个中文汉字"


def test_prompt_tail_keeps_output_format_after_clipping() -> None:
    topic = HotTopic(id="t1", title="很长的话题" * 300, category="科技", summary="很长的摘要" * 300)
    bundle = {
        "research_status": "sufficient",
        "sources": [
            {
                "fetch_success": True,
                "accepted_for_research": True,
                "source_name": "测试来源",
                "title": "资料标题" * 200,
                "url": "https://example.com/article",
            }
        ],
    }
    prompt = _prompt(topic, _article_angle(), "热点资讯", "客观通俗", 1200, research_bundle=bundle)

    assert len(prompt) <= 6000
    assert "输出格式" in prompt
    assert "不要 JSON" in prompt
    assert "不要代码围栏" in prompt


def test_body_char_count_consistent_across_generator_quality_and_layout() -> None:
    article = {
        "title": "不计入标题",
        "lead": "这是一段导语，只统计中文汉字，不统计标点和标题。",
        "intro": "这是一段导语，只统计中文汉字，不统计标点和标题。",
        "word_count": 1200,
        "sections": [
            {"heading": "不计入小标题", "body": "第一段正文包含中文汉字，用来验证统一计数口径。" * 10},
            {"heading": "仍不计入小标题", "body": "第二段正文继续提供中文内容，不把链接来源关键词算进去。" * 10},
            {"heading": "来源不计入", "body": "第三段正文保持完整自然段，仅正文汉字参与质量门统计。" * 10},
            {"heading": "关键词不计入", "body": "第四段正文用于确认布局清洗之后仍然回写同一个字数。" * 10},
        ],
        "source_list": ["[1] 来源：https://example.com"],
        "keywords": ["关键词不计入"],
    }
    article["body_char_count"] = count_body_chinese_chars(article)
    expected = article["body_char_count"]

    gate = quality_gate(article, {"research_status": "custom_topic", "custom_topic": True, "sources": []})
    laid_out = prepare_article_layout(article)

    assert article["body_char_count"] == expected
    assert gate["metrics"]["word_count"] == expected
    assert laid_out["body_char_count"] == expected


def test_valid_model_markdown_is_not_fallback(monkeypatch) -> None:
    markdown = """# 人工智能产业发展观察

人工智能产业正在从概念热度走向更多真实应用，企业、开发者和普通用户都在重新评估它的价值边界。

## 事件发生了什么
多地围绕人工智能产业发布支持政策，企业也在加快把模型能力嵌入办公、制造、营销和客服等流程。

## 为什么受到关注
这一变化受到关注，是因为人工智能不再只是一项单点技术，而是开始影响岗位分工、产品体验和企业成本结构。

## 可能带来哪些影响
短期看，企业会优先选择能提升效率的工具；长期看，行业竞争会从模型参数转向数据、场景和交付能力。

## 后续值得关注什么
后续更值得关注的是应用落地质量、数据合规、人才培养和中小企业是否真正能用得起这些工具。
"""
    def fake_generate(self, *args, **kwargs):
        self.last_diagnostic = {"http_status": 200, "content_type": "text/markdown", "parser_mode": "text", "timeout_seconds": 150}
        return markdown

    monkeypatch.setattr(OpenAITextProvider, "generate", fake_generate)

    article = ag_generate_article(
        HotTopic(id="ai", title="人工智能产业发展", category="科技数码", summary="产业应用持续推进"),
        {"name": "新闻资讯", "instruction": "事实优先"},
        "热点资讯",
        "客观通俗",
        1000,
        {"api_key": "sk-test", "model": "test", "base_url": "https://api.example.com/v1", "endpoint": "/chat/completions", "timeout_seconds": 150},
        network_settings={},
        research_bundle={"research_status": "custom_topic", "custom_topic": True, "sources": []},
    )

    assert article["response_parser_mode"] == "markdown"
    assert article["response_format_warning"] is False
    assert article["fallback_kind"] == ""
    assert article["used_local_fallback"] is False


def test_markdown_title_lead_body_are_separated() -> None:
    markdown = """# 新生成标题

这是一段独立导语，说明事件背景和文章角度，不能在正文里再重复出现。

## 事件发生了什么
这里是真正的正文第一节，说明已经确认的信息。

## 为什么受到关注
这里解释关注原因和讨论背景。

## 可能带来哪些影响
这里分析可能影响和读者关心点。
"""
    article = _parse_markdown_article_response(markdown, _article_topic(), _article_angle())

    assert article["title"] == "新生成标题"
    assert article["lead"] == "这是一段独立导语，说明事件背景和文章角度，不能在正文里再重复出现。"
    assert article["intro"] == article["lead"]
    assert article["body_markdown"].startswith("## 事件发生了什么")
    assert "# 新生成标题" not in article["body_markdown"]
    assert article["lead"] not in article["body_markdown"]


def test_plain_title_and_title_prefix_are_cleaned() -> None:
    markdown = """标题：真正的新标题

导语：这是一段导语，用来说明文章切入点。

## 第一部分
正文第一段，提供事实信息。

## 第二部分
正文第二段，提供背景分析。

## 第三部分
正文第三段，提供后续观察。
"""
    article = _parse_markdown_article_response(markdown, _article_topic(), _article_angle())

    assert article["title"] == "真正的新标题"
    assert article["lead"] == "这是一段导语，用来说明文章切入点。"
    assert not article["title"].startswith("标题")


def test_code_fenced_markdown_and_repeated_title_are_cleaned() -> None:
    markdown = """```markdown
# 新标题

导语：这是一段用于测试的导语。

# 新标题

## 第一节
新标题

这是一段用于测试的导语。

正文保留，不能因为清理重复标题导语而误删。

## 第二节
第二节正文保留。

## 第三节
第三节正文保留。
```"""
    article = _parse_markdown_article_response(markdown, _article_topic(), _article_angle())

    assert article["title"] == "新标题"
    assert article["lead"] == "这是一段用于测试的导语。"
    assert article["body_markdown"].count("新标题") == 0
    assert article["body_markdown"].count(article["lead"]) == 0
    assert "正文保留" in article["body_markdown"]


def test_missing_h1_uses_topic_title_without_fallback() -> None:
    markdown = """这是一段导语，模型没有返回一级标题。

## 第一节
正文第一节。

## 第二节
正文第二节。

## 第三节
正文第三节。
"""
    article = _parse_markdown_article_response(markdown, _article_topic(), _article_angle())

    assert article["title"] == "原始话题标题"
    assert article["response_parser_mode"] == "markdown"
    assert article["fallback_kind"] == ""
    assert article["used_local_fallback"] is False


def test_missing_lead_is_review_not_hardcoded() -> None:
    markdown = """# 新标题

## 第一节
正文第一节。

## 第二节
正文第二节。

## 第三节
正文第三节。
"""
    article = _parse_markdown_article_response(markdown, _article_topic(), _article_angle())

    assert article["lead"] == ""
    assert article["intro"] == ""
    assert article["content_warning_code"] == "LEAD_MISSING"
    assert "围绕" not in article["content_markdown"].split("##", 1)[0]


def test_provider_json_and_article_markdown_modes_can_coexist(monkeypatch) -> None:
    markdown = """# 新标题

这是一段导语，证明正文内容是 Markdown。

## 第一节
正文第一节内容。

## 第二节
正文第二节内容。

## 第三节
正文第三节内容。
"""

    def fake_generate(self, *args, **kwargs):
        self.last_diagnostic = {"http_status": 200, "content_type": "application/json", "parser_mode": "json", "timeout_seconds": 150}
        return markdown

    monkeypatch.setattr(OpenAITextProvider, "generate", fake_generate)
    article = ag_generate_article(
        _article_topic(),
        _article_angle(),
        "热点资讯",
        "客观通俗",
        1200,
        {"api_key": "sk-test", "model": "test", "base_url": "https://api.example.com/v1", "endpoint": "/chat/completions", "timeout_seconds": 150},
        network_settings={},
        research_bundle={"research_status": "custom_topic", "custom_topic": True, "sources": []},
    )

    assert article["provider_parser_mode"] == "json"
    assert article["response_parser_mode"] == "markdown"
    assert article["fallback_kind"] == ""


def test_layout_ui_body_and_word_do_not_repeat_title_or_lead(tmp_path) -> None:
    article = prepare_article_layout(
        {
            "title": "# 标题：新标题",
            "lead": "导语：这是一段导语。",
            "sections": [
                {"heading": "第一节", "body": "新标题\n\n这是一段导语。\n\n正文第一节保留。"},
                {"heading": "第二节", "body": "正文第二节保留。"},
                {"heading": "第三节", "body": "正文第三节保留。"},
            ],
        }
    )
    path = tmp_path / "article.docx"
    export_article(article, path)

    assert article["title"] == "新标题"
    assert article["lead"] == "这是一段导语。"
    assert article["body_markdown"].count("新标题") == 0
    assert article["body_markdown"].count(article["lead"]) == 0
    assert _count_docx_text(path, "新标题") == 1
    assert _count_docx_text(path, "这是一段导语。") == 1


def test_legacy_1000_word_count_migrates_to_1200() -> None:
    settings, migrated = _migrate_word_count_settings({"phase2a_word_count": 1000, "text_profile": {"model": "kept"}})

    assert migrated is True
    assert settings["phase2a_word_count"] == 1200
    assert settings["text_profile"]["model"] == "kept"
    assert recommended_word_count(1000) == 1200
    assert recommended_word_count(800) == 1200
    assert recommended_word_count(1500) == 1500
    assert recommended_word_count(1600) == 1600


def test_unrelated_topic_does_not_receive_hardcoded_padding(monkeypatch) -> None:
    markdown = """# 人工智能产业发展观察

人工智能产业正在持续发展。

## 事件发生了什么
多个行业正在尝试把人工智能工具用于真实业务。

## 为什么受到关注
它关系到企业效率、应用成本和技术生态。

## 可能带来哪些影响
企业可能调整流程，开发者也会面对新的能力要求。

## 后续值得关注什么
后续需要观察应用质量、合规要求和商业模式。
"""
    def fake_generate(self, *args, **kwargs):
        self.last_diagnostic = {"http_status": 200, "content_type": "text/markdown", "parser_mode": "text", "timeout_seconds": 150}
        return markdown

    monkeypatch.setattr(OpenAITextProvider, "generate", fake_generate)

    article = ag_generate_article(
        HotTopic(id="ai", title="人工智能产业发展", category="科技数码", summary="产业应用持续推进"),
        {"name": "新闻资讯", "instruction": "事实优先"},
        "热点资讯",
        "客观通俗",
        1000,
        {"api_key": "sk-test", "model": "test", "base_url": "https://api.example.com/v1", "endpoint": "/chat/completions", "timeout_seconds": 150},
        network_settings={},
        research_bundle={"research_status": "custom_topic", "custom_topic": True, "sources": []},
    )
    text = article["content_markdown"]
    assert "公共交通" not in text
    assert "身份标签" not in text
    assert "公共空间冲突" not in text


def test_short_model_body_is_warning_not_local_padding(monkeypatch) -> None:
    markdown = """# 人工智能产业发展观察

人工智能产业正在持续发展。

## 事件发生了什么
多个行业正在尝试把人工智能工具用于真实业务。

## 为什么受到关注
它关系到企业效率、应用成本和技术生态。

## 可能带来哪些影响
企业可能调整流程，开发者也会面对新的能力要求。

## 后续值得关注什么
后续需要观察应用质量、合规要求和商业模式。
"""
    def fake_generate(self, *args, **kwargs):
        self.last_diagnostic = {"http_status": 200, "content_type": "text/markdown", "parser_mode": "text", "timeout_seconds": 150}
        return markdown

    monkeypatch.setattr(OpenAITextProvider, "generate", fake_generate)

    article = ag_generate_article(
        HotTopic(id="ai", title="人工智能产业发展", category="科技数码", summary="产业应用持续推进"),
        {"name": "新闻资讯", "instruction": "事实优先"},
        "热点资讯",
        "客观通俗",
        1000,
        {"api_key": "sk-test", "model": "test", "base_url": "https://api.example.com/v1", "endpoint": "/chat/completions", "timeout_seconds": 150},
        network_settings={},
        research_bundle={"research_status": "custom_topic", "custom_topic": True, "sources": []},
    )

    assert article["content_warning_code"] == "CONTENT_TOO_SHORT"
    assert article["recommended_status"] == "review_required"
    assert article["text_generation_calls"] == 1
    assert article["used_local_fallback"] is False
    assert "公共交通" not in article["content_markdown"]


class _FakeStreamlit:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def success(self, value: str) -> None:
        self.messages.append(("success", value))

    def warning(self, value: str) -> None:
        self.messages.append(("warning", value))

    def caption(self, value: str) -> None:
        self.messages.append(("caption", value))


def test_ui_success_for_model_markdown(monkeypatch) -> None:
    fake = _FakeStreamlit()
    monkeypatch.setattr(rc1_app, "st", fake)
    rc1_app._render_text_generation_status(
        {
            "text_generation_calls": 1,
            "used_local_fallback": False,
            "fallback_kind": "",
            "response_parser_mode": "markdown",
            "text_model_name": "gpt-5.6-luna",
        },
        "task-1",
        restricted=True,
    )
    assert ("success", "文本模型生成成功") in fake.messages
    assert all(message != "本篇未使用文本模型正式正文" for _, message in fake.messages)


def test_ui_local_fallback_shows_basic_framework(monkeypatch) -> None:
    fake = _FakeStreamlit()
    monkeypatch.setattr(rc1_app, "st", fake)
    rc1_app._render_text_generation_status(
        {
            "text_generation_calls": 0,
            "used_local_fallback": True,
            "fallback_kind": "local_research_draft",
            "provider_error_code": "TIMEOUT",
        },
        "task-1",
        restricted=True,
    )
    assert ("warning", "本篇未使用文本模型正式正文") in fake.messages
    assert ("caption", "当前展示可编辑基础框架") in fake.messages
