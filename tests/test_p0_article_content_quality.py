from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from docx import Document

import generation.single_task as single_task
import modules.generation_store as generation_store
from export.docx_exporter import export_article
from generation.article_generator import _apply_quality_issue_rewrite
from generation.content_quality import intra_article_quality, quality_gate
from modules.database import SQLiteStore
from modules.models import HotTopic
from providers.text_provider import ProviderError
from research.service import clean_source_text


def _topic(topic_id: str = "p0-topic") -> HotTopic:
    return HotTopic(
        id=topic_id,
        title="老人高铁如厕掉落金戒指求助无果",
        summary="旅客称在D2857次列车如厕时金戒指滑落，随后咨询寻找流程。",
        source="manual",
        source_name="用户输入",
        source_url="https://example.com/topic",
    )


def _bundle(topic: HotTopic) -> dict:
    fact = "旅客称在D2857次列车如厕时金戒指滑落，随后向相关工作人员咨询寻找流程。"
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
                "source_name": "示例新闻",
                "title": "旅客反映高铁如厕时戒指滑落",
                "published_at": "2026-07-31",
                "url": "https://example.com/news",
                "content": fact,
                "summary": fact,
                "fetch_success": True,
                "accepted_for_research": True,
                "source_level": "source_page",
                "domain": "example.com",
            }
        ],
        "usable_facts": [{"fact_id": "f1", "canonical_fact": fact, "supporting_source_ids": ["s1"], "verification_type": "single_source"}],
        "verified_facts": [{"fact_id": "f1", "canonical_fact": fact, "supporting_source_ids": ["s1"], "verification_type": "single_source"}],
        "research_fact_cards": [{"fact_id": "f1", "fact": fact, "source_name": "示例新闻", "source_url": "https://example.com/news"}],
    }


def _article(title: str = "动态标题", extra: str = "") -> dict:
    lead = "这是一段独立导语，概括事件已经出现的公开信息和读者需要继续核对的关键边界。"
    sections = [
        {"heading": "车厢里的小物件为何难找", "body": "旅客描述金戒指在列车卫生间滑落，相关寻找需要先明确位置、时间和车次。这个事实只在这里展开一次，避免把同一经过拆成多段反复讲述。"},
        {"heading": "求助流程需要哪些信息", "body": "对普通乘客来说，车次、座位、使用卫生间的大致时段和物品特征，是后续沟通里最有用的信息。工作人员能否继续排查，也取决于这些线索是否足够具体。"},
        {"heading": "公开信息仍有哪些边界", "body": "目前能够写入正文的内容应限于旅客反映、列车车次和寻找诉求。至于责任划分、赔付结论或最终找回结果，在没有进一步来源前都不应补写。"},
    ]
    markdown = "\n\n".join([f"# {title}", lead] + [f"## {s['heading']}\n{s['body']}" for s in sections])
    if extra:
        markdown += "\n\n" + extra
    return {"title": title, "intro": lead, "lead": lead, "sections": sections, "content_markdown": markdown, "body_char_count": 520, "source_list": [{"publisher": "示例新闻", "title": "旅客反映高铁如厕时戒指滑落", "url": "https://example.com/news"}]}


def test_01_cleaner_deletes_handlebar_template_variables():
    result = clean_source_text("正文第一段说明事件经过。 {{item.reporter_name}} 正文第二段继续说明。")
    assert "{{" not in result["text"] and "reporter_name" not in result["text"]
    assert result["metrics"]["removed_noise_count"] >= 1


def test_02_cleaner_deletes_dynamic_data_and_item_fields():
    result = clean_source_text("dynamicData.sub_info = 1\nitem.tag 新闻标签\n旅客反映戒指滑落后咨询寻找流程。")
    assert "dynamicData" not in result["text"] and "item.tag" not in result["text"]


def test_03_cleaner_deletes_preview_unpublished_and_app_prompts():
    result = clean_source_text("未发布文章，仅支持15分钟预览\n打开紫牛新闻，阅读体验更佳\n旅客反映戒指滑落后咨询寻找流程。")
    assert "未发布" not in result["text"] and "阅读体验更佳" not in result["text"]


def test_04_duplicate_source_body_kept_once():
    paragraph = "旅客反映戒指滑落后咨询寻找流程。"
    result = clean_source_text(f"{paragraph}\n{paragraph}\n{paragraph}")
    assert result["text"].count("旅客反映戒指滑落后咨询寻找流程") == 1
    assert result["metrics"]["duplicate_block_count"] == 2


def test_05_exact_repeat_paragraphs_blocked():
    para = "旅客反映金戒指滑落后咨询寻找流程，这一段故意重复用于触发质量门。"
    article = _article(extra=f"{para}\n\n{para}")
    gate = quality_gate(article, _bundle(_topic()))
    assert gate["status"] == "failed"
    assert "ARTICLE_QUALITY_BLOCKED:REPEATED_PARAGRAPH" in gate["hard_errors"]


def test_06_high_similar_paragraphs_blocked():
    left = "旅客反映金戒指在列车卫生间滑落，随后咨询工作人员寻找流程，需要明确车次时间位置。"
    right = "旅客称金戒指在列车卫生间滑落，随后询问工作人员寻找流程，需要说明车次时间位置。"
    article = _article(extra=f"{left}\n\n{right}")
    report = intra_article_quality(article)
    assert report["passed"] is False
    assert "SIMILAR_PARAGRAPHS" in report["failures"]


def test_07_lead_summary_plus_body_expansion_is_not_false_positive():
    report = intra_article_quality(_article())
    assert report["passed"] is True


def _run_model_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str = "MODEL_OUTPUT_EMPTY") -> dict:
    topic = _topic(f"p0-{code.lower()}")
    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    monkeypatch.setattr(single_task.ResearchService, "collect", lambda self, topic, references=None, supplemental_text="": _bundle(topic))
    monkeypatch.setattr(single_task, "generate_article", lambda *args, **kwargs: (_ for _ in ()).throw(ProviderError(code, "model failed")))
    store = SQLiteStore(tmp_path / f"{topic.id}.sqlite")
    store.save_topics([topic])
    task = store.create_task("P0 model error", "multi_topic", [topic.to_dict()], 1, generation_options={"article_type": "热点资讯", "style": "客观", "image_plan_mode": "standard", "image_generation_requested": True, "word_count": 1200})
    return single_task.run_single_task(task, {"api_key": "text-key", "model": "text"}, {"api_key": "image-key", "model": "image"}, settings={"network": {}, "image_plan_mode": "standard"}, store=store)


def test_08_local_fallback_not_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    result = _run_model_error(tmp_path, monkeypatch)
    assert result["status"] == "failed"
    assert result["error_code"] == "MODEL_OUTPUT_EMPTY"
    assert "ARTICLE_TEXT_RETRY_REQUIRED" in result["quality_gate"]["reasons"]
    assert result["article"] is None


def test_09_local_fallback_generates_no_images(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    result = _run_model_error(tmp_path, monkeypatch)
    assert result["image_usage"]["generation_calls"] == 0
    assert result["inline_image_summary"]["status"] == "blocked"


def test_10_local_fallback_has_no_formal_word(tmp_path: Path):
    article = _article()
    article["used_local_fallback"] = True
    article["fallback_kind"] = "local_research_draft"
    with pytest.raises(ValueError, match="ARTICLE_NOT_READY"):
        export_article(article, tmp_path / "fallback.docx")


def test_11_reasoning_only_is_not_article_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    result = _run_model_error(tmp_path, monkeypatch, "MODEL_OUTPUT_REASONING_ONLY")
    assert result["status"] == "failed"
    assert result["article"] is None


def test_12_incomplete_sentence_fails():
    article = _article(extra="可能进入了 ，就没办法寻找了。")
    report = intra_article_quality(article)
    assert "INCOMPLETE_SENTENCE" in report["failures"]


def test_13_raw_source_fields_are_not_exported(tmp_path: Path):
    article = _article()
    article["raw_source_text"] = "RAW_SOURCE_SECRET_SHOULD_NOT_EXPORT"
    article["research_bundle"] = {"sources": [{"content": "RAW_SOURCE_SECRET_SHOULD_NOT_EXPORT"}]}
    path = export_article(article, tmp_path / "article.docx")
    text = "\n".join(paragraph.text for paragraph in Document(path).paragraphs)
    assert "RAW_SOURCE_SECRET_SHOULD_NOT_EXPORT" not in text
    assert "https://example.com/news" not in text


def test_14_insufficient_info_repeated_to_length_is_blocked():
    repeated = "公开资料有限，后续仍需关注。"
    article = _article(extra="\n\n".join([repeated * 4 for _ in range(8)]))
    report = intra_article_quality(article)
    assert report["passed"] is False
    assert "REPEATED_LONG_FRAGMENT" in report["failures"]


def test_15_targeted_rewrite_reruns_gate():
    class FakeProvider:
        last_diagnostic = {"http_status": 200, "parser_mode": "markdown"}

        def generate(self, prompt: str, temperature: float = 0.5, max_tokens: int = 3200) -> str:
            assert "质量问题" in prompt
            return _article("重写后的动态标题")["content_markdown"]

    stats = {"text_generation_calls": 1, "text_generation_limit": 3, "text_generation_call_reasons": ["INITIAL_GENERATION"]}
    rewritten, _ = _apply_quality_issue_rewrite(provider=FakeProvider(), topic=_topic(), angle={"name": "新闻资讯", "instruction": "事实优先"}, article_type="热点资讯", style="客观", requested_word_count=1200, article=_article(extra="可能进入了 ，就没办法寻找了。"), issue_list=["ARTICLE_QUALITY_BLOCKED:INCOMPLETE_SENTENCE"], required_headings=(), research_bundle=_bundle(_topic()), stats=stats)
    assert stats["text_generation_call_reasons"][-1] == "QUALITY_ISSUE_REWRITE"
    assert intra_article_quality(rewritten)["passed"] is True


def test_16_five_articles_pass_intra_quality_and_cross_article_difference():
    articles = [_article(f"动态标题 {index}", extra=f"第{index}篇补充不同观察角度，强调不同读者决策场景和核验路径。") for index in range(5)]
    reports = [intra_article_quality(article) for article in articles]
    assert all(report["passed"] for report in reports)
    bodies = [article["content_markdown"] for article in articles]
    assert len(set(bodies)) == 5


def test_17_phone_drop_must_not_be_rewritten_as_aircraft_crash():
    article = {
        "title": "高空坠机后手机完好",
        "intro": "一部手机从飞机上掉落后被找回，报道只涉及设备坠落。",
        "sections": [
            {"heading": "高空坠机后的手机状态", "body": "报道说手机从飞机上坠落后在农田中找回，并没有发生飞机失事。"},
            {"heading": "信息边界", "body": "公开资料只说明手机掉落、定位和找回过程，不能扩大成航空事故。"},
            {"heading": "读者判断", "body": "这只是设备意外坠落个案，不应被写成飞机事故或空难。"},
        ],
        "content_markdown": "# 高空坠机后手机完好\n\n一部手机从飞机上掉落后被找回，报道只涉及设备坠落。\n\n## 高空坠机后的手机状态\n报道说手机从飞机上坠落后在农田中找回，并没有发生飞机失事。\n\n## 信息边界\n公开资料只说明手机掉落、定位和找回过程，不能扩大成航空事故。\n\n## 读者判断\n这只是设备意外坠落个案，不应被写成飞机事故或空难。",
    }
    bundle = {
        "accepted_source_count": 1,
        "sources": [
            {
                "title": "一台苹果 iPhone 从 1.1 千米高空坠落后被找回",
                "content": "居民乘坐飞机时，其使用的一部没有保护壳的 iPhone 从 1.1 千米高的飞机上意外坠落，随后在油菜田中找回。",
                "accepted_for_research": True,
            }
        ],
    }
    report = intra_article_quality(article, bundle)
    assert "MISLEADING_AIRCRAFT_ACCIDENT_WORDING" in report["failures"]
    gate = quality_gate(article, bundle)
    assert "ARTICLE_QUALITY_BLOCKED:MISLEADING_AIRCRAFT_ACCIDENT_WORDING" in gate["hard_errors"]


def test_18_dangling_second_marker_is_blocked():
    article = _article(extra="二是在讨论类似新闻时，读者还需要区分个案事实和普遍规律。")
    report = intra_article_quality(article)
    assert "DANGLING_LIST_MARKER" in report["failures"]


def test_19_low_kilometer_source_must_not_be_rewritten_as_ten_thousand_meters():
    article = {
        "title": "万米高空坠落近乎无损",
        "intro": "一部 iPhone 从万米高空坠落后被找回。",
        "sections": [
            {"heading": "万米高空的意外", "body": "这段正文把 1.1 千米夸大成万米高空。"},
            {"heading": "信息边界", "body": "来源只说手机从 1.1 千米高度坠落，不能改写成一万米。"},
            {"heading": "读者判断", "body": "数字量级变化会改变读者对事件性质的理解。"},
        ],
        "content_markdown": "# 万米高空坠落近乎无损\n\n一部 iPhone 从万米高空坠落后被找回。\n\n## 万米高空的意外\n这段正文把 1.1 千米夸大成万米高空。\n\n## 信息边界\n来源只说手机从 1.1 千米高度坠落，不能改写成一万米。\n\n## 读者判断\n数字量级变化会改变读者对事件性质的理解。",
    }
    bundle = {
        "accepted_source_count": 1,
        "sources": [
            {
                "title": "iPhone 从 1.1 千米高空坠落后被找回",
                "content": "报道称一部 iPhone 从 1.1 千米高的飞机上意外坠落，随后被找回。",
                "accepted_for_research": True,
            }
        ],
    }
    report = intra_article_quality(article, bundle)
    assert "EXAGGERATED_ALTITUDE_WORDING" in report["failures"]


def test_20_unsourced_technical_details_are_blocked():
    article = {
        "title": "手机定位找回",
        "intro": "机主通过 Find My 定位找回手机。",
        "sections": [
            {"heading": "定位过程", "body": "文章自行补写该应用利用卫星信号定位。"},
            {"heading": "材料推断", "body": "文章又补写航空级铝合金和超瓷晶材料提升抗摔性能。"},
            {"heading": "信息边界", "body": "来源没有提供这些技术细节。"},
        ],
        "content_markdown": "# 手机定位找回\n\n机主通过 Find My 定位找回手机。\n\n## 定位过程\n文章自行补写该应用利用卫星信号定位。\n\n## 材料推断\n文章又补写航空级铝合金和超瓷晶材料提升抗摔性能。\n\n## 信息边界\n来源没有提供这些技术细节。",
    }
    bundle = {
        "accepted_source_count": 1,
        "sources": [{"title": "手机坠落后被找回", "content": "机主通过 Find My 应用定位并找回手机。", "accepted_for_research": True}],
    }
    report = intra_article_quality(article, bundle)
    assert "UNSUPPORTED_TECHNICAL_DETAIL" in report["failures"]
    assert set(report["unsupported_technical_detail_hits"]) >= {"卫星信号", "航空级铝合金", "超瓷晶"}
