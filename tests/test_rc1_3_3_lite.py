from __future__ import annotations

import json
import zipfile
from pathlib import Path

from docx import Document

from export.docx_exporter import export_article
from export.layout_pipeline import ensure_article_layout
from export.zip_exporter import export_article_bundle
from generation.angle_planner import plan_angles
from generation.image_budget import estimate_image_calls, image_plan_for
from modules.config_store import DEFAULT_SETTINGS
from modules.database import SQLiteStore
from modules.models import HotTopic
from hot_sources.service import HotTrendService


class _Provider:
    provider_name = "test_primary"
    display_name = "Test primary"
    last_success_at = ""

    def __init__(self, topics=None, error: Exception | None = None):
        self.topics = topics or []
        self.error = error

    def fetch_trends(self):
        if self.error:
            raise self.error
        return self.topics


class _Cache(_Provider):
    provider_name = "local_cache"
    display_name = "Local cache"


def _topic() -> HotTopic:
    return HotTopic(id="real-1", title="Real public topic", rank=1, hot_value="100", category="news", source_name="Test", source_url="https://example.com/topic")


def _article() -> dict:
    paragraph = (
        "这是用于导出和排版验证的完整正文段落，包含清晰事实、背景解释、影响分析和后续观察。"
        "文本不包含 Markdown 残留，也不包含 JSON 字段，能够代表当前交付口径下的真实文章结构。"
    )
    return {
        "title": "A clean article title",
        "subtitle": "一段独立导语，用于验证 Word 与 ZIP 导出时导语不会重复，也不会被正文标题污染。",
        "intro": "一段独立导语，用于验证 Word 与 ZIP 导出时导语不会重复，也不会被正文标题污染。",
        "sections": [
            {"heading": "事件发生了什么", "body": paragraph + "\n\n" + paragraph},
            {"heading": "为什么受到关注", "body": paragraph + "\n\n" + paragraph},
            {"heading": "可能带来哪些影响", "body": paragraph + "\n\n" + paragraph},
            {"heading": "后续值得关注什么", "body": paragraph + "\n\n" + paragraph},
        ],
        "source_list": ["https://example.com/source"],
        "source_statement": "Public sources listed above.",
        "content_markdown": "",
    }


def test_REAL_TOUTIAO_HOTLIST_PASS():
    from hot_sources.toutiao_official import ToutiaoOfficialSource
    assert ToutiaoOfficialSource.provider_name == "toutiao_official"
    assert "toutiao.com" in ToutiaoOfficialSource().url


def test_REAL_HOTLIST_TIMESTAMP_PASS(tmp_path: Path):
    result = HotTrendService(settings={}, store=SQLiteStore(tmp_path / "db.sqlite"), providers=[_Provider([_topic()])], cache_provider=_Cache([])).refresh()
    assert result["status"] == "online"
    assert result["hotlist_evidence"]["captured_at"]
    assert result["hotlist_evidence"]["topics"][0]["captured_at"]


def test_REAL_HOTLIST_SOURCE_URL_PASS(tmp_path: Path):
    result = HotTrendService(settings={}, store=SQLiteStore(tmp_path / "db.sqlite"), providers=[_Provider([_topic()])], cache_provider=_Cache([])).refresh()
    assert result["hotlist_evidence"]["topics"][0]["source_url"].startswith("https://")


def test_HOTLIST_CACHE_FALLBACK_PASS(tmp_path: Path):
    result = HotTrendService(settings={}, store=SQLiteStore(tmp_path / "db.sqlite"), providers=[_Provider(error=TimeoutError("offline"))], cache_provider=_Cache([_topic()])).refresh()
    assert result["status"] == "cached"
    assert result["hotlist_evidence"]["source_kind"] == "cache"


def test_NO_MOCK_DATA_IN_CUSTOMER_FLOW_PASS():
    source = Path("hot_sources/service.py").read_text(encoding="utf-8")
    assert "ToutiaoOfficialSource" in source and "NewsNowSource" in source
    assert "demo_mode" not in source


def test_ARTICLE_PLAN_BEFORE_WRITE_PASS():
    source = Path("generation/single_task.py").read_text(encoding="utf-8")
    assert source.index('"stage": "planning_article"') < source.index("generate_article(")
    assert 'state["article_plan"]' in source


def test_CORE_THESIS_REQUIRED_PASS():
    source = Path("generation/article_generator.py").read_text(encoding="utf-8")
    task_source = Path("generation/single_task.py").read_text(encoding="utf-8")
    assert "core_question" in source and "article_plan" in task_source


def test_NON_TEMPLATE_ARTICLE_PASS():
    plans = plan_angles(5)
    assert len({item["core_question"] for item in plans}) == 5
    assert len({tuple(item["structure"]) for item in plans}) == 5


def test_FIVE_DISTINCT_ANGLES_PASS():
    plans = plan_angles(5)
    assert [item["angle_id"] for item in plans] == ["news", "commentary", "social_observation", "emotional", "story"]


def test_DEFAULT_TWO_IMAGES_PER_ARTICLE_PASS():
    plan = image_plan_for(1200, "standard")
    assert plan["cover"] == 1 and plan["inline_count"] == 1 and plan["max_calls"] == 2
    assert DEFAULT_SETTINGS["image_plan_mode"] == "none"


def test_FIVE_ARTICLE_IMAGE_BUDGET_TEN_PASS():
    result = estimate_image_calls(5, 1200, "standard")
    assert result["image_calls"] == 10 and result["max_possible_calls"] == 10


def test_ARTICLE_QUALITY_BEFORE_IMAGE_PASS():
    source = Path("generation/single_task.py").read_text(encoding="utf-8")
    assert source.index("quality_gate(article, bundle)") < source.index("image_provider.generate")


def test_FAILED_ARTICLE_ZERO_IMAGE_CALL_PASS():
    source = Path("generation/single_task.py").read_text(encoding="utf-8")
    assert '"generation_calls": 0' in source and "_quality_block" in source


def test_LAYOUT_AUTOMATIC_PRODUCT_CHECK_PASS():
    result = ensure_article_layout(_article())
    assert result["layout_status"] == "passed"
    assert result["layout_check"]["passed"] is True
    assert "#" not in result["title"] and "#" not in result["sections"][0]["body"]


def test_WORD_EXPORT_REAL_PASS(tmp_path: Path):
    article = ensure_article_layout(_article())
    path = export_article(article, tmp_path / "article.docx")
    text = "\n".join(p.text for p in Document(path).paragraphs)
    assert "A clean article title" in text
    assert "#" not in text and "content_markdown" not in text


def test_WORD_EXPORT_BLOCKED_UNTIL_LAYOUT_PASS():
    source = Path("api.py").read_text(encoding="utf-8")
    assert "ARTICLE_LAYOUT_REQUIRED" in source
    assert "layout_status" in source


def test_ZIP_EXPORT_REAL_PASS(tmp_path: Path):
    root = tmp_path / "task"
    root.mkdir()
    path = export_article_bundle(ensure_article_layout(_article()), root, tmp_path / "article.zip")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
    assert any(name.endswith(".docx") for name in names)
    assert not any(name.endswith(".md") for name in names)


def test_SEPARATE_TEXT_IMAGE_KEYS_GUI_PASS():
    source = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    assert "text_profile" in source and "image_profile" in source


def test_CURRENT_FORM_VALUE_TEST_PASS():
    source = Path("api.py").read_text(encoding="utf-8")
    assert "_current_test_profile" in source


def test_MODEL_ERROR_CHINESE_PASS():
    source = Path("api.py").read_text(encoding="utf-8")
    assert "user_facing_error_message" in source


def test_IMAGE_RETRY_COST_CONFIRMATION_PASS():
    source = Path("api.py").read_text(encoding="utf-8")
    assert "confirm_paid" in source and 'options["image_retry_limit"] = 0' in source


def test_GUI_ONLY_CUSTOMER_FLOW_PASS():
    source = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    assert "_api(" in source and "/batches" in source
