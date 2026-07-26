from __future__ import annotations

import json
from pathlib import Path

from generation.content_quality import claim_supported_by_fact, quality_gate


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "r2251_real_evidence_min.json"


def _source_bundle() -> dict:
    facts = [
        {"fact_id": "f1", "canonical_fact": "2026年7月21日，外交部边海司负责人提出严正交涉。", "supporting_source_ids": ["s1"], "verification_type": "single_source"},
        {"fact_id": "f2", "canonical_fact": "仁爱礁是中国南沙群岛的一部分。", "supporting_source_ids": ["s1"], "verification_type": "single_source"},
        {"fact_id": "f3", "canonical_fact": "中国海警在仁爱礁附近海域开展执法活动。", "supporting_source_ids": ["s1"], "verification_type": "single_source"},
    ]
    source_content = "".join(item["canonical_fact"] for item in facts)
    return {
        "research_status": "sufficient",
        "accepted_source_count": 1,
        "usable_fact_count": 3,
        "official_or_reliable_source_count": 1,
        "key_organizations": ["外交部", "中国海警"],
        "sources": [
            {
                "source_id": "s1",
                "source_name": "外交部",
                "publisher_id": "mfa.gov.cn",
                "domain": "mfa.gov.cn",
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


def _article_with_source_list(extra: str = "") -> dict:
    body = (
        "据外交部公开信息，2026年7月21日，外交部边海司负责人提出严正交涉。"
        "据外交部公开信息，仁爱礁是中国南沙群岛的一部分。"
        "据外交部公开信息，中国海警在仁爱礁附近海域开展执法活动。"
        f"{extra}\n\n"
        "资料来源\n"
        "[1] 外交部，发布时间：2026年7月21日，https://www.mfa.gov.cn/\n"
        "[2] 腾讯新闻，发布时间：2026年7月21日，https://news.qq.com/\n"
        "[3] 央视新闻，发布时间：2026年7月21日，https://www.cctv.com/\n"
    )
    return {
        "content_markdown": body,
        "word_count": 0,
        "fact_basis": [
            {"fact_id": "f1", "fact": "2026年7月21日，外交部边海司负责人提出严正交涉。", "source_ids": ["s1"]},
            {"fact_id": "f2", "fact": "仁爱礁是中国南沙群岛的一部分。", "source_ids": ["s1"]},
            {"fact_id": "f3", "fact": "中国海警在仁爱礁附近海域开展执法活动。", "source_ids": ["s1"]},
        ],
    }


def test_SOURCE_LIST_NUMBER_NOT_CONCRETE_CLAIM_PASS():
    result = quality_gate(_article_with_source_list(), _source_bundle())
    assert result["status"] == "passed", result["reasons"]
    assert result["metrics"]["fact_trace"]["unsupported_concrete_claims"] == []


def test_SOURCE_SECTION_EXCLUDED_FROM_FACT_SCAN_PASS():
    article = _article_with_source_list("\n\n参考资料\n1. 来源名称 2026年7月21日 https://example.com\n2. 来源名称 300亿元 URL")
    result = quality_gate(article, _source_bundle())
    assert result["status"] == "passed", result["reasons"]


def test_DATE_SUBCLAUSE_SUPPORTED_BY_FULL_FACT_PASS():
    fact = "2026年7月21日，外交部边海司负责人就菲律宾在仁爱礁蓄意挑衅提出严正交涉。"
    assert claim_supported_by_fact("2026年7月21日", fact)


def test_SHORT_CLAUSE_MATCH_DIRECTION_PASS():
    fact = "2026年7月21日，外交部边海司负责人就菲律宾在仁爱礁蓄意挑衅提出严正交涉。"
    assert claim_supported_by_fact("外交部边海司负责人提出严正交涉", fact)
    assert not claim_supported_by_fact("2026年7月22日", fact)
    assert not claim_supported_by_fact("投入300亿元", "投入30亿元用于相关工作。")


def test_UNSUPPORTED_CONCRETE_CLAIM_BLOCK_PASS():
    result = quality_gate(_article_with_source_list("另有5000人入院，投入300亿元。"), _source_bundle())
    assert result["status"] == "failed"
    unsupported = result["metrics"]["fact_trace"]["unsupported_concrete_claims"]
    assert any("5000人入院" in item for item in unsupported)
    assert any("投入300亿元" in item for item in unsupported)


def _load_real_evidence() -> dict:
    return {"research_bundle": json.loads(FIXTURE.read_text(encoding="utf-8"))}


def _real_article(extra: str = "") -> tuple[dict, dict]:
    evidence = _load_real_evidence()
    bundle = evidence.get("research_bundle") or {}
    facts = list(bundle.get("verified_facts") or [])[:3]
    assert len(facts) >= 3
    bundle["usable_fact_count"] = max(3, int(bundle.get("usable_fact_count") or len(facts)))
    bundle["official_or_reliable_source_count"] = max(1, int(bundle.get("official_or_reliable_source_count") or bundle.get("official_source_count") or 0))
    content = "\n".join(str(item.get("canonical_fact") or item.get("fact") or "") for item in facts)
    content += f"\n{extra}\n\n资料来源\n[1] 外交部……\n[2] 腾讯新闻……\n[3] 央视新闻……\n"
    article = {
        "content_markdown": content,
        "word_count": 0,
        "fact_basis": [
            {
                "fact_id": item["fact_id"],
                "fact": item.get("canonical_fact") or item.get("fact"),
                "source_ids": item.get("supporting_source_ids") or item.get("source_ids") or [],
            }
            for item in facts
        ],
    }
    return article, bundle


def test_REAL_EVIDENCE_THREE_FACT_ARTICLE_PASS():
    article, bundle = _real_article()
    result = quality_gate(article, bundle)
    assert result["status"] == "passed", result["reasons"]


def test_REAL_EVIDENCE_NUMBERED_SOURCE_LIST_PASS():
    article, bundle = _real_article()
    result = quality_gate(article, bundle)
    assert result["metrics"]["fact_trace"]["unsupported_concrete_claims"] == []


def test_REAL_EVIDENCE_FABRICATED_EXTENSION_BLOCK_PASS():
    article, bundle = _real_article("另有5000人入院并投入300亿元。")
    result = quality_gate(article, bundle)
    assert result["status"] == "failed"
    unsupported = result["metrics"]["fact_trace"]["unsupported_concrete_claims"]
    assert any("5000人入院" in item for item in unsupported)
    assert any("投入300亿元" in item for item in unsupported)
