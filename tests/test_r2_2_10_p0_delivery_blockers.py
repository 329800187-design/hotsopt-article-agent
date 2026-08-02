from __future__ import annotations

import sys
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api
import generation.single_task as single_task
from export.customer_output import customer_visible_article, ensure_no_customer_meta_content
from export.docx_exporter import export_article
from generation.content_quality import quality_gate
from hot_sources.commercial_filter import classify_commercial_topic
from hot_sources.service import HotTrendService
from modules.database import SQLiteStore
from modules.models import HotTopic
from providers.text_provider import ProviderError


def _topic(topic_id: str, title: str, summary: str = "公开资料显示事件正在发展", hot_value: str = "100") -> HotTopic:
    return HotTopic(
        id=topic_id,
        title=title,
        summary=summary,
        hot_value=hot_value,
        rank=1,
        category="综合热点",
        source="test",
        source_name="测试源",
        source_url=f"https://example.com/{topic_id}",
    )


class _Provider:
    provider_name = "fake"
    display_name = "测试源"

    def __init__(self, topics: list[HotTopic]) -> None:
        self.topics = topics
        self.last_success_at = None
        self.last_error = None

    def fetch_trends(self) -> list[HotTopic]:
        return list(self.topics)


class _Cache(_Provider):
    provider_name = "cache"
    display_name = "缓存"


def _article() -> dict:
    section_body = "这是一段面向普通读者的正文，围绕事件背景、影响和后续观察展开。" * 18
    return {
        "title": "测试热点观察",
        "lead": "这是一段客户可见的导语。",
        "sections": [
            {"heading": "事件背景", "body": section_body},
            {"heading": "影响分析", "body": section_body},
            {"heading": "后续观察", "body": section_body},
        ],
        "body_markdown": "\n\n".join(
            [
                f"## 事件背景\n{section_body}",
                f"## 影响分析\n{section_body}",
                f"## 后续观察\n{section_body}",
            ]
        ),
        "content_markdown": "\n\n".join(
            [
                "# 测试热点观察",
                "这是一段客户可见的导语。",
                f"## 事件背景\n{section_body}",
                f"## 影响分析\n{section_body}",
                f"## 后续观察\n{section_body}",
            ]
        ),
        "body_char_count": 1200,
        "quality_gate": {"status": "passed", "passed": True, "hard_error_count": 0},
        "layout_status": "passed",
        "layout_check": {"passed": True},
        "source_list": ["资料来源：测试媒体", "原文链接：https://example.com/internal"],
        "source_statement": "资料来源：测试媒体",
        "keywords": ["内部关键词"],
    }


def test_commercial_hotspot_classifier_blocks_real_coupon_patterns() -> None:
    result = classify_commercial_topic(
        _topic(
            "ad-1",
            "加了新款！门店4元一个！沪上阿姨面包组合 原价¥31.98 券后¥13.98",
            hot_value="热销100件(近2小时)",
        )
    )
    assert result["is_blocked"] is True
    assert result["hotspot_class"] in {"ECOMMERCE_PRODUCT", "COMMERCIAL_PROMOTION"}
    assert result["commercial_score"] >= 3
    assert {"coupon_price", "original_price", "price"} & set(result["matched_signals"])


def test_hotspot_refresh_filters_commercial_without_forcing_200(tmp_path: Path) -> None:
    commercial = _topic("ad-2", "元气森林气泡水饮料280mL*12瓶 原价¥19.9 券后¥15.9", hot_value="热销200件")
    normal = _topic("news-1", "郑钦文无缘多伦多站正赛")
    store = SQLiteStore(tmp_path / "db.sqlite")
    result = HotTrendService(store=store, providers=[_Provider([commercial, normal])], cache_provider=_Cache([])).refresh()
    titles = [item.title for item in result["topics"]]
    assert titles == ["郑钦文无缘多伦多站正赛"]
    assert result["filter_stats"]["valid_hotspot_count"] == 1
    assert result["filter_stats"]["filtered_commercial_count"] == 1
    assert len(result["topics"]) < 200


def test_selecting_commercial_topic_is_blocked_before_task_creation(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "db.sqlite")
    commercial = _topic("ad-3", "【温碧霞代言】形象美深海藻眼膜60片 原价¥39.9 券后¥9.9")
    store.save_topics([commercial])
    service = HotTrendService(store=store, providers=[], cache_provider=_Cache([]))
    with pytest.raises(ValueError, match="TOPIC-COMMERCIAL-FILTERED"):
        service.create_task("blocked", "multi_topic", [commercial.id], 1, {})


def test_batch_api_blocks_direct_commercial_topic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "db.sqlite")
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "service", HotTrendService(store=store, providers=[], cache_provider=_Cache([])))
    response = TestClient(api.app).post(
        "/api/batches",
        json={
            "batch_name": "blocked",
            "topics": [_topic("ad-4", "【念村人】吸汁面藕圈 原价¥15.6 券后¥9.6").to_dict()],
            "mode": "multi_topic",
            "article_count": 1,
        },
    )
    body = response.json()
    assert response.status_code == 409
    assert body["error"]["code"] == "TOPIC-COMMERCIAL-FILTERED"
    assert body["error"]["detail"]["details"]["filter_reason"] in {"ECOMMERCE_PRODUCT", "COMMERCIAL_PROMOTION"}


def test_customer_visible_article_and_docx_do_not_export_sources(tmp_path: Path) -> None:
    article = _article()
    visible = customer_visible_article(article)
    assert "source_list" not in visible
    assert "source_statement" not in visible
    output = export_article(article, tmp_path / "article.docx")
    text = "\n".join(paragraph.text for paragraph in Document(output).paragraphs)
    assert "测试热点观察" in text
    assert "这是一段客户可见的导语" in text
    assert "资料来源" not in text
    assert "原文链接" not in text
    assert "https://example.com/internal" not in text
    assert "内部关键词" not in text


def test_customer_meta_content_blocks_export_and_quality_gate() -> None:
    article = _article()
    article["sections"][0]["body"] = "AI 辅助声明：本文由模型生成，资料来源见原文链接：https://example.com/internal"
    article["body_markdown"] = "## 事件背景\n" + article["sections"][0]["body"]
    article["content_markdown"] = article["body_markdown"]
    with pytest.raises(ValueError, match="ARTICLE_META_CONTENT_LEAK"):
        ensure_no_customer_meta_content(article)
    gate = quality_gate(article, {"accepted_source_count": 1, "sources": []})
    assert gate["status"] == "failed"
    assert "ARTICLE_META_CONTENT_LEAK" in gate["hard_errors"]


def test_model_generation_failure_exposes_exact_provider_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    topic = _topic("model-1", "真实热点生成失败复现")
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.save_topics([topic])
    task = store.create_task(
        "model failure",
        "multi_topic",
        [topic.to_dict()],
        1,
        generation_options={"article_type": "热点资讯", "style": "客观通俗", "image_plan_mode": "standard", "word_count": 1200},
    )
    monkeypatch.setattr(
        single_task.ResearchService,
        "collect",
        lambda self, topic, references=None, supplemental_text="": {"accepted_source_count": 1, "sources": [{"fetch_success": True, "accepted_for_research": True, "publisher_id": "x"}]},
    )
    monkeypatch.setattr(single_task, "generate_article", lambda *args, **kwargs: (_ for _ in ()).throw(ProviderError("MODEL_OUTPUT_REASONING_ONLY", "reasoning only")))
    result = single_task.run_single_task(
        task,
        {"api_key": "text-key", "model": "deepseek-v4-flash"},
        {"api_key": "image-key", "model": "image"},
        settings={"network": {}, "image_plan_mode": "standard"},
        store=store,
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "MODEL_OUTPUT_REASONING_ONLY"
    assert result["provider_error_code"] == "MODEL_OUTPUT_REASONING_ONLY"
    assert "ARTICLE_TEXT_RETRY_REQUIRED" in result["quality_gate"]["reasons"]
    assert result["image_usage"]["generation_calls"] == 0


def test_my_content_page_uses_lightweight_batch_snapshot_and_fallback() -> None:
    source = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")
    content = source[source.index("def _content"):source.index("def _settings_page")]
    assert '"/batches?limit=20&refresh=false"' in content
    assert 'timeout=6' in content
    failure_block = content[content.index('"CONTENT-LIST-001"'):content.index('batches = batch_payload.get("items", [])')]
    assert "return" not in failure_block
    assert '"/tasks?limit=20&unbatched=true"' in content
