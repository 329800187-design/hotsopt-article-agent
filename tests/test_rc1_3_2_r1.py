from __future__ import annotations

from pathlib import Path

from generation.content_quality import quality_gate, validate_fact_basis
from modules.config_store import DEFAULT_SETTINGS
from research.service import ResearchService, extract_page_content


def test_jsonld_article_body_is_preferred_over_boilerplate():
    html = """<html><head><script type='application/ld+json'>{\"@type\":\"NewsArticle\",\"headline\":\"正式标题\",\"datePublished\":\"2026-07-22\",\"articleBody\":\"2026年7月22日，市政府发布了正式说明。\"}</script></head><body><nav>推荐阅读 登录</nav><article><p>页面正文备用内容足够长。</p></article><footer>版权声明</footer></body></html>"""
    result = extract_page_content(html, "https://news.example/item")
    assert result["content_source"] == "jsonld_articleBody"
    assert "市政府发布了正式说明" in result["content"]
    assert "推荐阅读" not in result["content"]


def test_research_discovers_sources_and_deduplicates_same_story(tmp_path, monkeypatch):
    import research.service as service

    monkeypatch.setattr(service, "research_root", lambda: tmp_path / "research")
    pages = {
        "https://one.example/a": {"url": "https://one.example/a", "domain": "one.example", "title": "同一报道", "content": "2026年7月22日，市政府发布说明。事件影响涉及五万人。会议地点在市中心。", "summary": "官方背景资料。", "fetch_success": True},
        "https://one.example/b": {"url": "https://one.example/b", "domain": "one.example", "title": "同一报道", "content": "2026年7月22日，市政府发布说明。事件影响涉及五万人。会议地点在市中心。", "summary": "官方背景资料。", "fetch_success": True},
        "https://two.example/c": {"url": "https://two.example/c", "domain": "two.example", "title": "后续说明", "content": "2026年7月22日，市政府发布说明。委员会公布相关金额为100万元。调查将继续进行。", "summary": "后续背景资料。", "fetch_success": True},
    }
    topic = type("Topic", (), {"id": "r1-topic", "title": "测试热点", "source_url": "https://one.example/a"})()
    bundle = ResearchService(fetcher=lambda url: pages[url], discoverer=lambda _topic: ["https://one.example/b", "https://two.example/c"]).collect(topic)
    valid = [item for item in bundle["sources"] if item.get("fetch_success")]
    assert len(valid) == 2
    assert bundle["unique_source_domains"] == ["one.example", "two.example"]
    assert bundle["cross_verified_fact_count"] >= 1
    assert bundle["research_status"] == "sufficient"


def test_quality_gate_rejects_untraceable_model_facts_and_short_article():
    bundle = {
        "research_status": "sufficient",
        "sources": [{"source_id": "s1", "domain": "one.example", "fetch_success": True, "content": "2026年7月22日，市政府发布正式说明。"}, {"source_id": "s2", "domain": "two.example", "fetch_success": True, "content": "2026年7月22日，市政府发布正式说明。"}],
        "key_organizations": ["市政府"],
        "timeline": ["2026年7月22日"],
        "background": ["背景"],
    }
    article = {"word_count": 800, "content_markdown": "2026年7月22日市政府发布说明。", "fact_basis": [{"fact": "模型自行编造的事实", "source_ids": ["missing-source"], "confidence": "confirmed"}]}
    trace = validate_fact_basis(article, bundle)
    gate = quality_gate(article, bundle)
    assert trace["valid"] is False
    assert gate["passed"] is False
    assert any("source_id" in reason or "事实" in reason or "正文长度" in reason for reason in gate["reasons"])


def test_rc132_default_is_text_first_and_image_route_exists():
    assert DEFAULT_SETTINGS["image_plan_mode"] == "none"
    import api

    assert "/api/tasks/{task_id}/images/generate" in {route.path for route in api.app.routes}


def test_source_rebuild_inputs_do_not_require_generated_executable():
    root = Path(__file__).resolve().parents[1]
    assert (root / "scripts" / "build_rc1_3_2.py").is_file()
    assert (root / "packaging" / "launcher_shell.csproj").is_file()
