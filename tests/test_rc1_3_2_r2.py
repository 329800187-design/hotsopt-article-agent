from __future__ import annotations

from generation.content_quality import analyze_article, quality_gate, validate_fact_basis
from research.service import ResearchService, registrable_domain


def _topic(title: str = "侯友宜缺席今天两场食安会议"):
    return type("Topic", (), {"id": "r2-topic", "title": title, "summary": "", "source_url": ""})()


def _page(url: str, title: str, content: str, *, level: str = "source_page") -> dict:
    from urllib.parse import urlparse

    domain = urlparse(url).netloc
    return {"url": url, "title": title, "content": content, "summary": content, "domain": domain, "source_name": domain, "source_level": level, "fetch_success": True}


def test_UNRELATED_SOURCE_REJECTED_PASS():
    pages = {
        "https://sports-a.example/a": _page("https://sports-a.example/a", "足球联赛决赛报道", "球队在体育赛事中获胜，球员赛后接受采访。"),
        "https://sports-b.example/b": _page("https://sports-b.example/b", "篮球赛事最新战报", "篮球比赛进入加时，赛事主办方公布比赛结果。"),
    }
    bundle = ResearchService(fetcher=lambda url: pages[url], discoverer=lambda _: list(pages)).collect(_topic("某公司发布新款手机"))
    assert all(not item["accepted_for_research"] for item in bundle["sources"])
    assert all(item.get("rejection_reason") for item in bundle["rejected_sources"])
    assert bundle["verified_facts"] == []


def test_UNRELATED_TWO_SOURCE_RESEARCH_INSUFFICIENT_PASS():
    pages = {
        "https://sports-a.example/a": _page("https://sports-a.example/a", "足球赛事报道", "足球赛事结束，球队公布了比赛结果和赛后安排。"),
        "https://sports-b.example/b": _page("https://sports-b.example/b", "篮球赛事报道", "篮球赛事结束，裁判公布比赛结果和赛后安排。"),
    }
    bundle = ResearchService(fetcher=lambda url: pages[url], discoverer=lambda _: list(pages)).collect(_topic("某公司发布新款手机"))
    assert bundle["research_status"] == "insufficient"
    assert bundle["information_sufficiency_score"] < 70


def test_TOPIC_ENTITY_MATCH_REQUIRED_PASS():
    pages = {"https://news.example/a": _page("https://news.example/a", "侯友宜缺席两场食安会议", "侯友宜今天缺席两场食品安全会议，相关机构说明后续安排。")}
    bundle = ResearchService(fetcher=lambda url: pages[url], discoverer=lambda _: list(pages)).collect(_topic())
    source = bundle["sources"][0]
    assert source["accepted_for_research"] is True
    assert source["matched_entities"]
    assert source["matched_topic_terms"]


def test_REJECTED_SOURCE_ZERO_FACT_CONTRIBUTION_PASS():
    pages = {"https://sports.example/a": _page("https://sports.example/a", "体育赛事报道", "体育赛事结束，球队公布赛后安排。")}
    bundle = ResearchService(fetcher=lambda url: pages[url], discoverer=lambda _: list(pages)).collect(_topic("某公司发布新款手机"))
    assert not bundle["verified_facts"]
    assert bundle["accepted_source_count"] == 0


def _fact_bundle(*, official: bool = False):
    sources = [
        {"source_id": "s1", "publisher_id": "media-a.example", "domain": "news.media-a.example", "fetch_success": True, "accepted_for_research": True, "source_level": "official" if official else "source_page", "content": "侯友宜缺席今天两场食安会议。"},
        {"source_id": "s2", "publisher_id": "media-b.example", "domain": "news.media-b.example", "fetch_success": True, "accepted_for_research": True, "source_level": "source_page", "content": "侯友宜缺席今天两场食安会议。"},
        {"source_id": "s3", "publisher_id": "sports.example", "domain": "sports.example", "fetch_success": True, "accepted_for_research": True, "source_level": "source_page", "content": "体育赛事报道与比赛结果。"},
    ]
    fact = {"fact_id": "f1", "canonical_fact_id": "f1", "fact": "侯友宜缺席今天两场食安会议。", "canonical_fact": "侯友宜缺席今天两场食安会议。", "supporting_source_ids": ["s1"] if official else ["s1", "s2"], "supporting_publisher_ids": ["media-a.example"] if official else ["media-a.example", "media-b.example"], "confidence": "official" if official else "cross_verified", "verification_type": "official_single_source" if official else "independent_publishers"}
    return {"research_status": "sufficient", "sources": sources, "verified_facts": [fact], "official_source_count": 1 if official else 0, "timeline": ["今天"], "key_organizations": ["食安会议"], "background": ["公开报道"]}


def _article(ids: list[str], fact_id: str = "f1", fact: str = "侯友宜缺席今天两场食安会议。"):
    return {"content_markdown": f"侯友宜缺席今天两场食安会议。相关信息如下。" * 2, "fact_basis": [{"fact_id": fact_id, "fact": fact, "source_ids": ids, "confidence": "confirmed"}]}


def test_ONE_SUPPORTING_ONE_UNRELATED_SOURCE_REJECTED_PASS():
    result = validate_fact_basis(_article(["s1", "s3"]), _fact_bundle())
    assert result["valid"] is False


def test_EVERY_CITED_SOURCE_MUST_SUPPORT_FACT_PASS():
    result = validate_fact_basis(_article(["s1", "s3"]), _fact_bundle())
    assert any("不支持" in reason or "引用" in reason for reason in result["invalid_reasons"])


def test_UNKNOWN_FACT_ID_REJECTED_PASS():
    result = validate_fact_basis(_article(["s1", "s2"], fact_id="not-in-bundle", fact="侯友宜缺席今天两场食安会议。"), _fact_bundle())
    assert result["valid"] is False


def test_MODEL_CANNOT_SELF_DECLARE_VERIFIED_FACT_PASS():
    result = validate_fact_basis(_article(["s1", "s2"], fact="模型临时创造的新事实。"), _fact_bundle())
    assert result["valid"] is False


def test_TWO_INDEPENDENT_SUPPORTING_SOURCES_PASS():
    result = validate_fact_basis(_article(["s1", "s2"]), _fact_bundle())
    assert result["valid"] is True
    assert result["cross_verified_count"] == 1


def test_OFFICIAL_SINGLE_SOURCE_FACT_PASS():
    result = validate_fact_basis(_article(["s1"]), _fact_bundle(official=True))
    assert result["valid"] is True


def test_SAME_PUBLISHER_SUBDOMAIN_NOT_INDEPENDENT_PASS():
    assert registrable_domain("news.example.com") == "example.com"
    assert registrable_domain("finance.example.com") == "example.com"


def test_SAME_MEDIA_MULTIPLE_PAGES_COUNT_ONCE_PASS():
    pages = {"https://news.example.com/a": _page("https://news.example.com/a", "手机发布消息", "公司发布新款手机，产品参数和上市安排已经公布。"), "https://finance.example.com/b": _page("https://finance.example.com/b", "手机上市安排", "公司发布新款手机，价格和上市安排已经公布。")}
    bundle = ResearchService(fetcher=lambda url: pages[url], discoverer=lambda _: list(pages)).collect(_topic("某公司发布新款手机"))
    assert bundle["independent_publisher_count"] == 1
    assert len([item for item in bundle["sources"] if item.get("accepted_for_research")]) == 2


def test_TWO_TRUE_PUBLISHERS_COUNT_TWO_PASS():
    pages = {"https://news.example.com/a": _page("https://news.example.com/a", "某公司发布新款手机", "某公司发布新款手机，产品参数和上市安排已经公布。"), "https://tech.other.com/b": _page("https://tech.other.com/b", "新款手机上市报道", "某公司发布新款手机，市场价格和上市安排已经公布。")}
    bundle = ResearchService(fetcher=lambda url: pages[url], discoverer=lambda _: list(pages)).collect(_topic("某公司发布新款手机"))
    assert bundle["independent_publisher_count"] == 2


def test_PRIMARY_SEARCH_FAILURE_FALLBACK_PASS():
    service = ResearchService(fetcher=lambda url: {})
    service.primary_discoverer = lambda topic, query=None: (_ for _ in ()).throw(RuntimeError("primary down"))
    service.fallback_discoverer = lambda topic, query=None: ["https://fallback.example/a"]
    urls, evidence = service.discover_with_fallback(_topic())
    assert urls == ["https://fallback.example/a"]
    assert evidence[0]["discoverer_name"].startswith("fallback_discoverer")
    assert evidence[0]["error_code"] == "PRIMARY_SEARCH_FAILED"


def test_SEARCH_CANDIDATE_RELEVANCE_FILTER_PASS():
    pages = {"https://good.example/a": _page("https://good.example/a", "某公司发布新款手机", "某公司发布新款手机，产品参数已经公布。"), "https://bad.example/b": _page("https://bad.example/b", "足球赛事报道", "足球赛事结束，球队公布赛后安排。")}
    bundle = ResearchService(fetcher=lambda url: pages[url], discoverer=lambda _: list(pages)).collect(_topic("某公司发布新款手机"))
    assert bundle["accepted_source_count"] == 1
    assert bundle["rejected_source_count"] == 1


def test_SEARCH_FAILURE_VISIBLE_TO_USER_PASS():
    service = ResearchService(fetcher=lambda url: {})
    service.primary_discoverer = lambda topic, query=None: (_ for _ in ()).throw(RuntimeError("primary down"))
    service.fallback_discoverer = lambda topic, query=None: (_ for _ in ()).throw(RuntimeError("fallback down"))
    service.discoverer = service.discover_urls
    bundle = service.collect(_topic())
    assert bundle["discovery"][0]["error_code"] == "PRIMARY_AND_FALLBACK_SEARCH_FAILED"


def _five_fact_bundle():
    sources = [{"source_id": "s1", "publisher_id": "a.example", "domain": "a.example", "fetch_success": True, "accepted_for_research": True, "content": "事实0 事实1 事实2 事实3 事实4 今天。"}, {"source_id": "s2", "publisher_id": "b.example", "domain": "b.example", "fetch_success": True, "accepted_for_research": True, "content": "事实0 事实1 事实2 事实3 事实4 今天。"}]
    facts = [{"fact_id": f"f{i}", "canonical_fact_id": f"f{i}", "fact": f"事实{i}", "canonical_fact": f"事实{i}", "supporting_source_ids": ["s1", "s2"], "supporting_publisher_ids": ["a.example", "b.example"], "confidence": "cross_verified", "verification_type": "independent_publishers"} for i in range(5)]
    return {"research_status": "sufficient", "sources": sources, "verified_facts": facts, "cross_verified_fact_count": 5, "official_source_count": 0, "timeline": ["今天"], "key_organizations": ["机构"], "background": ["背景"]}


def test_FACT_BASIS_COUNT_CANNOT_FAKE_SCORE_PASS():
    bundle = _five_fact_bundle()
    article = {"content_markdown": "今天。" + "事实0事实1事实2事实3事实4", "fact_basis": [{"fact_id": f"fake{i}", "fact": f"伪造事实{i}", "source_ids": ["s1", "s2"]} for i in range(20)]}
    gate = quality_gate(article, bundle)
    assert gate["passed"] is False
    assert gate["metrics"]["verified_fact_count"] == 0


def test_INVALID_CROSS_VERIFICATION_BLOCKS_IMAGES_PASS():
    bundle = _five_fact_bundle()
    article = {"content_markdown": "今天。事实0事实1事实2事实3事实4", "fact_basis": [{"fact_id": "f0", "fact": "事实0", "source_ids": ["s1", "s3"]}]}
    gate = quality_gate(article, bundle)
    assert gate["passed"] is False
    assert gate["metrics"]["invalid_fact_count"] == 1


def test_QUALITY_GATE_USES_CANONICAL_FACTS_PASS():
    bundle = _five_fact_bundle()
    article = {"content_markdown": "今天。事实0事实1事实2事实3事实4", "fact_basis": [{"fact_id": f"f{i}", "fact": f"事实{i}", "source_ids": ["s1", "s2"]} for i in range(5)]}
    metrics = analyze_article(article, bundle)
    assert metrics["verified_fact_count"] == 5
    assert metrics["fact_basis_count"] == 5
