from __future__ import annotations

import json
from pathlib import Path

from generation.content_quality import analyze_article, quality_gate
from generation.image_budget import estimate_image_calls, image_cost_preview, image_plan_for
from providers.errors import user_facing_error_message
from providers.image_provider import OpenAIImageProvider
from research.service import ResearchService, extract_page_content, load_research_bundle


ROOT = Path(__file__).resolve().parents[1]


def _profile(key: str = "key") -> dict[str, str]:
    return {"api_key": key, "base_url": "https://example.com/v1", "endpoint": "/images/generations", "model": "image-model", "auth_type": "bearer"}


def test_separate_text_image_keys_pass(tmp_path, monkeypatch):
    import modules.config_store as store

    settings_path = tmp_path / "config" / "settings.json"
    secrets: dict[str, str] = {}
    monkeypatch.setattr(store, "CONFIG_DIR", settings_path.parent)
    monkeypatch.setattr(store, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(store, "save_secret", lambda name, value: secrets.__setitem__(name, value) or f"dpapi:{name}")
    monkeypatch.setattr(store, "load_secret", lambda ref: secrets.get(ref.removeprefix("dpapi:"), ""))
    monkeypatch.setattr(store, "delete_secret", lambda ref: secrets.pop(ref.removeprefix("dpapi:"), None))
    store.save_settings({"text_profile": {"api_key": "text-key-123456"}, "image_profile": {"api_key": "image-key-654321"}})
    loaded = store.load_settings()
    assert loaded["text_profile"]["api_key"] == "text-key-123456"
    assert loaded["image_profile"]["api_key"] == "image-key-654321"
    assert secrets["text_profile_api_key"] != secrets["image_profile_api_key"]


def test_text_key_update_does_not_overwrite_image(tmp_path, monkeypatch):
    import modules.config_store as store

    settings_path = tmp_path / "config" / "settings.json"
    secrets: dict[str, str] = {}
    monkeypatch.setattr(store, "CONFIG_DIR", settings_path.parent)
    monkeypatch.setattr(store, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(store, "save_secret", lambda name, value: secrets.__setitem__(name, value) or f"dpapi:{name}")
    monkeypatch.setattr(store, "load_secret", lambda ref: secrets.get(ref.removeprefix("dpapi:"), ""))
    monkeypatch.setattr(store, "delete_secret", lambda ref: secrets.pop(ref.removeprefix("dpapi:"), None))
    store.save_settings({"text_profile": {"api_key": "text-old-123456"}, "image_profile": {"api_key": "image-old-654321"}})
    store.save_settings({"text_profile": {"api_key": "text-new-123456"}, "image_profile": {"api_key": "***"}})
    loaded = store.load_settings()
    assert loaded["text_profile"]["api_key"] == "text-new-123456"
    assert loaded["image_profile"]["api_key"] == "image-old-654321"


def test_separate_provider_configuration_pass():
    source = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")
    assert "rc132_text_provider" in source
    assert "rc132_image_provider" in source
    assert "单独保存文本模型" in source and "单独保存图片模型" in source
    assert "share_text_image_credentials" in source


def test_image_config_check_zero_generation_call_pass(monkeypatch):
    def fail_network(*args, **kwargs):
        raise AssertionError("configuration check must not call the image endpoint")

    monkeypatch.setattr("providers.image_provider.create_http_client", fail_network)
    result = OpenAIImageProvider(_profile()).check_configuration()
    assert result.success is True
    assert result.details["generation_calls"] == 0
    assert result.details["charged"] is False


def test_image_paid_test_requires_confirmation_pass():
    source = (ROOT / "api.py").read_text(encoding="utf-8")
    ui = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")
    assert "PAID_TEST_CONFIRMATION_REQUIRED" in source
    assert "confirm_paid_test" in source
    assert "可能产生费用" in ui


def test_image_test_call_counter_pass():
    provider = OpenAIImageProvider({"api_key": "", "auth_type": "bearer"})
    result = provider.test_connection(Path("unused.png"))
    assert result.details["generation_calls"] == 0
    assert result.details["charged"] is False


def test_error_messages_are_specific():
    assert "NO_AVAILABLE_CHANNEL" in user_facing_error_message("NO_AVAILABLE_CHANNEL")
    assert "没有分配" in user_facing_error_message("NO_AVAILABLE_CHANNEL")
    assert "MODEL_NOT_FOUND" in user_facing_error_message("MODEL_NOT_FOUND")
    assert "RATE_LIMITED" in user_facing_error_message("RATE_LIMITED")
    assert "INSUFFICIENT_BALANCE" in user_facing_error_message("INSUFFICIENT_BALANCE")
    assert "ENDPOINT_NOT_FOUND" in user_facing_error_message("ENDPOINT_NOT_FOUND")
    assert user_facing_error_message("UNKNOWN") == "网络连接异常"


def test_image_test_long_response_no_false_timeout_pass():
    source = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")
    assert '"/models/image/test": 210' in source
    assert '"timeout_override": 180' in source


def test_default_economy_image_mode_pass():
    plan = image_plan_for(800, "economy")
    assert plan["cover"] == 1
    assert plan["inline_count"] <= 1
    assert plan["retry_limit"] == 0


def test_image_call_budget_pass():
    preview = estimate_image_calls(1, 800, "economy")
    assert preview["max_possible_calls"] <= 2
    assert image_cost_preview(2, 800, "economy")["image_calls"] == 2


def test_no_unlimited_image_retry_pass():
    assert image_plan_for(800, "economy")["retry_limit"] == 0
    assert image_plan_for(1200, "standard")["retry_limit"] == 0


def test_source_page_content_extraction_pass():
    result = extract_page_content("<html><head><title>原文标题</title></head><body><article><p>第一段包含足够的事实内容和时间信息。</p><p>第二段包含人物和机构的具体回应。</p></article></body></html>", "https://example.com/news")
    assert result["title"] == "原文标题"
    assert result["fetch_success"] is True
    assert "事实内容" in result["content"]


def test_multi_source_research_pass(tmp_path, monkeypatch):
    import research.service as service
    monkeypatch.setattr(service, "research_root", lambda: tmp_path / "research")
    pages = {
        "https://one.example/news": {"source_name": "来源一", "title": "原文一", "url": "https://one.example/news", "content": "2026年7月1日，市政府召开会议，部长张三公开回应。事件影响涉及五万人。会议地点在市中心。", "summary": "市政府公布背景。", "fetch_success": True},
        "https://two.example/news": {"source_name": "来源二", "title": "原文二", "url": "https://two.example/news", "content": "2026年7月1日，市政府召开会议，部长张三公开回应。委员会发布第二份说明。相关金额为100万元。", "summary": "委员会补充说明。", "fetch_success": True},
    }
    bundle = ResearchService(fetcher=lambda url: pages[url]).collect(type("Topic", (), {"id": "topic-1", "title": "测试事件", "source_url": "https://one.example/news"})(), ["https://two.example/news"])
    assert len(bundle["sources"]) == 2
    assert len(bundle["verified_facts"]) >= 1
    assert all(item["verification_type"] in {"independent_publishers", "official_single_source"} for item in bundle["verified_facts"])
    assert bundle["research_status"] == "sufficient"
    assert load_research_bundle("topic-1")["topic_title"] == "测试事件"


def test_insufficient_information_block_pass():
    bundle = {"research_status": "insufficient", "sources": [], "verified_facts": []}
    gate = quality_gate({"content_markdown": "只有一个标题，没有正文。"}, bundle)
    assert gate["passed"] is False
    assert any("资料不足" in item or "来源" in item for item in gate["reasons"])


def test_quality_gate_metrics_and_source_coverage_pass():
    facts = [{"fact_id": f"f{i}", "canonical_fact_id": f"f{i}", "fact": f"事实{i}", "canonical_fact": f"事实{i}", "source_ids": ["source-1", "source-2"], "supporting_source_ids": ["source-1", "source-2"], "supporting_publisher_ids": ["one.example", "two.example"], "confidence": "cross_verified", "verification_type": "independent_publishers"} for i in range(6)]
    bundle = {"research_status": "sufficient", "sources": [{"source_id": "source-1", "publisher_id": "one.example", "domain": "one.example", "content": "事实0事实1事实2事实3事实4事实5", "fetch_success": True, "accepted_for_research": True}, {"source_id": "source-2", "publisher_id": "two.example", "domain": "two.example", "content": "事实0事实1事实2事实3事实4事实5", "fetch_success": True, "accepted_for_research": True}], "verified_facts": facts, "timeline": ["2026年7月1日"], "key_organizations": ["市政府"], "background": ["背景资料"]}
    article = {"content_markdown": "2026年7月1日市政府召开会议，公布了100万元项目。张三表示将继续推进。事实0事实1事实2事实3事实4事实5被两家来源交叉确认。" * 2, "fact_basis": facts}
    metrics = analyze_article(article, bundle)
    assert metrics["source_count"] == 2
    assert metrics["verified_fact_count"] == 6
    assert "source_coverage" in metrics


def test_article_gate_before_image_pass():
    source = (ROOT / "generation" / "single_task.py").read_text(encoding="utf-8")
    assert "quality_gate(article, bundle)" in source
    assert source.index("quality_gate(article, bundle)") < source.index("image_provider.generate")


def test_low_quality_article_zero_image_call_pass():
    source = (ROOT / "api.py").read_text(encoding="utf-8")
    assert "ensure_article_allows_paid_image_generation" in source
    assert "QUALITY_GATE_FAILED" in source


def test_research_bundle_persistence_pass(tmp_path, monkeypatch):
    import research.service as service
    monkeypatch.setattr(service, "research_root", lambda: tmp_path / "research")
    bundle = ResearchService(fetcher=lambda url: {"url": url, "title": "title", "content": "2026年7月1日，机构发布说明。", "summary": "背景", "fetch_success": True}).collect(type("Topic", (), {"id": "persist", "title": "persist", "source_url": "https://a.example"})(), ["https://b.example"])
    path = tmp_path / "research" / "persist" / "research_bundle.json"
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["topic_id"] == "persist"
