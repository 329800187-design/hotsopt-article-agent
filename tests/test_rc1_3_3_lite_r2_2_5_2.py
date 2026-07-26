from __future__ import annotations

from pathlib import Path

from generation.content_quality import claim_supported_by_fact, quality_gate


ROOT = Path(__file__).resolve().parents[1]


def test_NEGATED_RESIGNATION_NOT_SUPPORTED_PASS():
    assert not claim_supported_by_fact("负责人辞职", "负责人未辞职")


def test_NEGATED_SENTENCE_NOT_SUPPORTED_PASS():
    assert not claim_supported_by_fact("被判无期徒刑", "未被判无期徒刑")


def test_NEGATED_INJURY_NOT_SUPPORTED_PASS():
    assert not claim_supported_by_fact("造成5人受伤", "没有造成5人受伤")


def test_NEGATED_PERCENT_CHANGE_NOT_SUPPORTED_PASS():
    assert not claim_supported_by_fact("上涨20%", "未上涨20%")


def test_SAME_POLARITY_FACT_SUPPORTED_PASS():
    assert claim_supported_by_fact("负责人未辞职", "公司公告称负责人未辞职")
    assert claim_supported_by_fact("造成5人受伤", "事故造成5人受伤")


def _opposite_bundle() -> dict:
    facts = [
        {
            "fact_id": "f1",
            "canonical_fact": "公司负责人未辞职。",
            "supporting_source_ids": ["s1"],
            "verification_type": "official_single_source",
        },
        {
            "fact_id": "f2",
            "canonical_fact": "公司于2026年7月21日发布公告。",
            "supporting_source_ids": ["s1"],
            "verification_type": "official_single_source",
        },
        {
            "fact_id": "f3",
            "canonical_fact": "公司正在正常经营。",
            "supporting_source_ids": ["s1"],
            "verification_type": "official_single_source",
        },
    ]
    source_content = "".join(item["canonical_fact"] for item in facts)
    return {
        "research_status": "sufficient",
        "accepted_source_count": 1,
        "usable_fact_count": 3,
        "official_or_reliable_source_count": 1,
        "key_organizations": ["公司"],
        "sources": [
            {
                "source_id": "s1",
                "source_name": "公司公告",
                "publisher_id": "company.example",
                "domain": "company.example",
                "source_level": "official",
                "fetch_success": True,
                "accepted_for_research": True,
                "content": source_content,
                "summary": source_content,
            }
        ],
        "usable_facts": facts,
        "verified_facts": facts,
        "single_source_facts": [],
    }


def _opposite_article(extra: str = "") -> dict:
    content = (
        "据公司公告，公司负责人未辞职。"
        "公司于2026年7月21日发布公告。"
        "公司正在正常经营。"
        f"{extra}"
    )
    return {
        "content_markdown": content,
        "word_count": 0,
        "fact_basis": [
            {"fact_id": "f1", "fact": "公司负责人未辞职。", "source_ids": ["s1"]},
            {"fact_id": "f2", "fact": "公司于2026年7月21日发布公告。", "source_ids": ["s1"]},
            {"fact_id": "f3", "fact": "公司正在正常经营。", "source_ids": ["s1"]},
        ],
    }


def test_OPPOSITE_FACT_ARTICLE_BLOCK_PASS():
    result = quality_gate(_opposite_article("随后，负责人辞职。"), _opposite_bundle())
    assert result["status"] == "failed"
    unsupported = result["metrics"]["fact_trace"]["unsupported_concrete_claims"]
    assert any("负责人辞职" in item for item in unsupported)
    assert '"image_usage": {"generation_calls": 0' in (ROOT / "generation" / "single_task.py").read_text(encoding="utf-8")


def test_OPPOSITE_FACT_ARTICLE_FABRICATED_NUMBER_STILL_BLOCK_PASS():
    result = quality_gate(_opposite_article("随后，公司投入300亿元并造成5人受伤。"), _opposite_bundle())
    assert result["status"] == "failed"
    unsupported = result["metrics"]["fact_trace"]["unsupported_concrete_claims"]
    assert any("投入300亿元" in item for item in unsupported)
    assert any("5人受伤" in item for item in unsupported)
