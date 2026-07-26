from pathlib import Path

from generation.content_quality import quality_gate, validate_fact_basis
from generation.image_budget import image_plan_for
from research.service import ResearchService


def _topic(title: str = "侯友宜缺席今天两场食安会议"):
    return type("Topic", (), {"id": "r2-final-topic", "title": title, "summary": "", "source_url": ""})()


def _page(url: str, title: str, content: str) -> dict:
    from urllib.parse import urlparse

    domain = urlparse(url).netloc
    return {"url": url, "title": title, "content": content, "summary": content, "domain": domain, "source_name": domain, "source_level": "source_page", "fetch_success": True}


def _fact_bundle() -> dict:
    fact = {"fact_id": "f1", "canonical_fact_id": "f1", "fact": "侯友宜缺席今天两场食安会议。", "canonical_fact": "侯友宜缺席今天两场食安会议。", "supporting_source_ids": ["s1", "s2"], "supporting_publisher_ids": ["a.example", "b.example"], "confidence": "cross_verified", "verification_type": "independent_publishers"}
    sources = [
        {"source_id": "s1", "publisher_id": "a.example", "domain": "a.example", "fetch_success": True, "accepted_for_research": True, "content": fact["canonical_fact"]},
        {"source_id": "s2", "publisher_id": "b.example", "domain": "b.example", "fetch_success": True, "accepted_for_research": True, "content": fact["canonical_fact"]},
    ]
    return {"research_status": "sufficient", "sources": sources, "verified_facts": [fact], "official_source_count": 0, "timeline": ["今天"], "key_organizations": ["食安会议"], "background": ["公开报道"]}


def _article(fact_basis):
    return {"word_count": 800, "content_markdown": "侯友宜缺席今天两场食安会议。", "fact_basis": fact_basis}


def test_UNIQUE_FACT_ID_COUNT_PASS():
    fact_basis = [{"fact_id": "f1", "fact": "侯友宜缺席今天两场食安会议。", "source_ids": ["s1", "s2"]}] * 5
    trace = validate_fact_basis(_article(fact_basis), _fact_bundle())
    assert trace["validated_count"] == 1
    assert trace["verified_fact_count"] == 1


def test_DUPLICATE_FACT_ID_REJECT_PASS():
    fact_basis = [{"fact_id": "f1", "fact": "侯友宜缺席今天两场食安会议。", "source_ids": ["s1", "s2"]}] * 5
    gate = quality_gate(_article(fact_basis), _fact_bundle())
    assert gate["passed"] is False
    assert gate["metrics"]["fact_trace"]["duplicate_fact_ids"] == ["f1"]


def test_UNSUPPORTED_CONCRETE_CLAIM_BLOCK_PASS():
    article = _article([{"fact_id": "f1", "fact": "侯友宜缺席今天两场食安会议。", "source_ids": ["s1", "s2"]}])
    article["content_markdown"] += "5000人入院，投入300亿元，负责人辞职，被判处无期徒刑。"
    gate = quality_gate(article, _fact_bundle())
    assert gate["passed"] is False
    assert gate["metrics"]["fact_trace"]["unsupported_concrete_claims"]


def test_COMPOUND_CONCRETE_CLAIMS_SPLIT_AND_BLOCK_PASS():
    article = _article([{"fact_id": "f1", "fact": "侯友宜缺席今天两场食安会议。", "source_ids": ["s1", "s2"]}])
    article["content_markdown"] = "侯友宜缺席今天两场食安会议，并造成5000人入院、投入300亿元、负责人辞职且被判无期徒刑。"
    gate = quality_gate(article, _fact_bundle())
    unsupported = gate["metrics"]["fact_trace"]["unsupported_concrete_claims"]
    assert gate["passed"] is False
    assert all(item in unsupported for item in ["5000人入院", "投入300亿元", "负责人辞职", "被判无期徒刑"])


def test_FABRICATED_NUMBER_BLOCK_PASS():
    article = _article([{"fact_id": "f1", "fact": "侯友宜缺席今天两场食安会议。", "source_ids": ["s1", "s2"]}])
    article["content_markdown"] += "投入300亿元。"
    assert quality_gate(article, _fact_bundle())["passed"] is False


def test_FABRICATED_PENALTY_BLOCK_PASS():
    article = _article([{"fact_id": "f1", "fact": "侯友宜缺席今天两场食安会议。", "source_ids": ["s1", "s2"]}])
    article["content_markdown"] += "负责人辞职，被判处无期徒刑。"
    assert quality_gate(article, _fact_bundle())["passed"] is False


def test_CONTRADICTORY_ACTION_NOT_VERIFIED_PASS():
    pages = {
        "https://a.example/a": _page("https://a.example/a", "侯友宜缺席食安会议", "侯友宜缺席今天两场食安会议。"),
        "https://b.example/b": _page("https://b.example/b", "侯友宜出席食安会议", "侯友宜出席今天两场食安会议。"),
    }
    bundle = ResearchService(fetcher=lambda url: pages[url], discoverer=lambda _: list(pages)).collect(_topic())
    assert bundle["cross_verified_fact_count"] == 0
    assert not bundle["verified_facts"]


def test_NEGATION_CONFLICT_PASS():
    pages = {
        "https://a.example/a": _page("https://a.example/a", "现场有人受伤", "侯友宜缺席今天两场食安会议，现场有人受伤。"),
        "https://b.example/b": _page("https://b.example/b", "现场无人受伤", "侯友宜缺席今天两场食安会议，现场无人受伤。"),
    }
    bundle = ResearchService(fetcher=lambda url: pages[url], discoverer=lambda _: list(pages)).collect(_topic())
    assert bundle["cross_verified_fact_count"] == 0


def test_NUMERIC_CONFLICT_PASS():
    pages = {
        "https://a.example/a": _page("https://a.example/a", "事件损失100万元", "侯友宜缺席今天两场食安会议，损失100万元。"),
        "https://b.example/b": _page("https://b.example/b", "事件损失1000万元", "侯友宜缺席今天两场食安会议，损失1000万元。"),
    }
    bundle = ResearchService(fetcher=lambda url: pages[url], discoverer=lambda _: list(pages)).collect(_topic())
    assert bundle["cross_verified_fact_count"] == 0


def test_SEMANTIC_VERIFIED_FACT_DEDUP_PASS():
    pages = {
        "https://a.example/a": _page("https://a.example/a", "仁爱礁冲突视频", "视频显示事件始于菲方挑衅。"),
        "https://b.example/b": _page("https://b.example/b", "仁爱礁冲突现场", "视频更是显示事件始于菲方挑衅。"),
    }
    bundle = ResearchService(fetcher=lambda url: pages[url], discoverer=lambda _: list(pages)).collect(_topic("仁爱礁冲突"))
    assert len(bundle["verified_facts"]) == 1


def test_PAID_BATCH_IMAGE_CONFIRMATION_PASS():
    api_source = Path("api.py").read_text(encoding="utf-8")
    ui_source = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    phrase = "我确认本次会真实调用图片模型，并可能产生费用"
    assert phrase in api_source and phrase in ui_source
    assert "PAID_BATCH_IMAGE_CONFIRMATION_REQUIRED" in api_source


def test_UNCONFIRMED_BATCH_ZERO_IMAGE_CALL_PASS():
    api_source = Path("api.py").read_text(encoding="utf-8")
    ui_source = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    assert '"generation_calls": 0' in api_source
    assert "disabled=image_mode != \"none\" and not paid_batch_confirmed" in ui_source


def test_DEFAULT_TWO_IMAGES_PER_ARTICLE_PASS():
    plan = image_plan_for(1200, "standard")
    assert plan["cover"] == 1 and plan["inline_count"] == 1 and plan["max_calls"] == 2
    assert '"image_retry_limit": 0' in Path("ui/rc1_app.py").read_text(encoding="utf-8")
