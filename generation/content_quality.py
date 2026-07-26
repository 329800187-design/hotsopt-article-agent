from __future__ import annotations

import re
from collections import Counter
from typing import Any

from modules.source_formatter import normalize_source_list


VAGUE_PATTERNS = ("目前无法确认", "现有信息没有说明", "尚不能判断", "需要等待后续信息", "不宜扩大解读")
TIME_RE = re.compile(r"(:20\d{2}年\s*\d{1,2}月\s*\d{1,2}日|\d{1,2}月\s*\d{1,2}日|今天|昨日|明日|本周|当日)")
NUMBER_RE = re.compile(r"\d+(:\.\d+)(:万|亿|人|元|%|公里|次|场|项)")
CONCRETE_CLAIM_RE = re.compile(
    r"(:\d+(:\.\d+)(:万|亿|亿元|万元|元|人|%|公里|次|场|项)|"
    r"20\d{2}年\s*\d{1,2}月\s*\d{1,2}日|\d{1,2}月\s*\d{1,2}日|"
    r"(:辞职|逮捕|关闭|发布|上涨|下跌|增加|减少|缺席|出席|被判|判处|处罚|罚款|死亡|去世|受伤|入院|损失|投入))"
)
EXPLICIT_NONFACT_RE = re.compile(r"(:\[\s*(:analysis|unknown|disputed)\s*\]|(:analysis|unknown|disputed)\s*[:：]|分析\s*[:：]|未知\s*[:：]|争议\s*[:：])", re.I)
CLAUSE_SPLIT_RE = re.compile(r"[，,；;、]+|并且|同时|此外|导致|造成|因此|且|并")
HARD_FACT_WARNING_RE = CONCRETE_CLAIM_RE
SOFT_ANALYSIS_RE = re.compile(r"(:可能|值得关注|从现有资料看|这意味着|趋势|影响|观点|分析|或许|有待观察|后续|折射|显示出|提醒)")
SOURCE_SECTION_TITLES = {"资料来源", "参考资料", "信息来源", "来源列表"}
SOURCE_REF_LINE_RE = re.compile(r"^\s*(:\[\s*\d+\s*\]|\d+\s*[.．、])\s*.+")
URL_RE = re.compile(r"https://|www\.", re.I)
PUBLISHED_AT_ONLY_RE = re.compile(r"^\s*(:发布时间|发布日期|来源时间)\s*[:：]\s*(:20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}日|\d{1,2}月\d{1,2}日)(:\s+\d{1,2}:\d{2}(::\d{2}))\s*$")
NEGATION_MARKERS = ("并不存在", "没有发生", "并未", "尚未", "未曾", "没有", "否认", "未", "不", "无")
POLARITY_ACTIONS = (
    "辞职", "逮捕", "关闭", "发布", "上涨", "下跌", "增加", "减少",
    "缺席", "出席", "被判", "判处", "处罚", "罚款", "死亡", "去世",
    "受伤", "入院", "损失", "投入",
)


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[。！？；\n]+", str(text or "")) if part.strip()]


def _fact_clauses(text: str) -> list[str]:
    """Split compound sentences before checking concrete claims."""
    clauses: list[str] = []
    for sentence in _sentences(text):
        clauses.extend(part.strip() for part in CLAUSE_SPLIT_RE.split(sentence) if part.strip())
    return clauses


def _normalize(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or "")).lower()


def _article_body_for_fact_scan(markdown: str) -> str:
    """Return article body only; source/reference sections are not factual claims."""
    body_lines: list[str] = []
    for raw_line in str(markdown or "").splitlines():
        line = raw_line.strip()
        heading = re.sub(r"^[#>\-\s]+", "", line).strip().rstrip("：:")
        if heading in SOURCE_SECTION_TITLES:
            break
        if SOURCE_REF_LINE_RE.match(line) or URL_RE.search(line) or PUBLISHED_AT_ONLY_RE.match(line):
            continue
        body_lines.append(raw_line)
    return "\n".join(body_lines)


def _tokens(value: str) -> set[str]:
    text = _normalize(value)
    if len(text) <= 2:
        return {text} if text else set()
    return {text[index:index + 2] for index in range(len(text) - 1)}


def _fact_supported(fact: str, source_text: str) -> bool:
    normalized_fact = _normalize(fact)
    normalized_source = _normalize(source_text)
    if not normalized_fact or not normalized_source:
        return False
    if normalized_fact in normalized_source:
        return True
    left, right = _tokens(fact), _tokens(source_text)
    return bool(left) and len(left & right) / len(left) >= 0.72


def _number_signature(text: str) -> set[str]:
    return set(re.findall(
        r"(:20\d{2}年\s*\d{1,2}月\s*\d{1,2}日|\d{1,2}月\s*\d{1,2}日|\d+(:\.\d+)(:亿元|万元|万|亿|元|人|%|公里|次|场|项))",
        str(text or ""),
    ))


def _action_polarities(text: str) -> dict[str, bool]:
    """Return whether each concrete action is negated in nearby text.

    The negation window intentionally looks only before the action phrase.  This
    prevents words such as “无期徒刑” from turning “被判无期徒刑” into a
    negative claim, while still catching “未被判无期徒刑”.
    """
    compact = re.sub(r"\s+", "", str(text or ""))
    polarities: dict[str, bool] = {}
    for action in POLARITY_ACTIONS:
        start = 0
        while True:
            index = compact.find(action, start)
            if index < 0:
                break
            window = compact[max(0, index - 12):index + len(action)]
            negated = any(marker in window for marker in NEGATION_MARKERS)
            if action not in polarities:
                polarities[action] = negated
            else:
                polarities[action] = polarities[action] and negated
            start = index + len(action)
    return polarities


def _polarity_conflicts(claim_clause: str, canonical_fact: str) -> bool:
    claim_polarities = _action_polarities(claim_clause)
    fact_polarities = _action_polarities(canonical_fact)
    for action in set(claim_polarities) & set(fact_polarities):
        if claim_polarities[action] != fact_polarities[action]:
            return True
    return False


def claim_supported_by_fact(claim_clause: str, canonical_fact: str) -> bool:
    """Check whether a concrete article clause is covered by a canonical fact.

    The shorter claim clause is the denominator, so a date-only or amount-only
    subclause can be supported by a longer canonical fact.  Numeric/date
    signatures in the claim must still be present in the canonical fact.  If
    the claim and fact describe the same concrete action with opposite polarity,
    reject before substring or fuzzy matching.
    """
    claim_norm = _normalize(claim_clause)
    fact_norm = _normalize(canonical_fact)
    if not claim_norm or not fact_norm:
        return False
    if _polarity_conflicts(claim_clause, canonical_fact):
        return False
    claim_numbers = _number_signature(claim_clause)
    fact_numbers = _number_signature(canonical_fact)
    if claim_numbers and not claim_numbers.issubset(fact_numbers):
        return False
    if claim_norm in fact_norm or fact_norm in claim_norm:
        return True
    claim_tokens, fact_tokens = _tokens(claim_clause), _tokens(canonical_fact)
    return bool(claim_tokens) and len(claim_tokens & fact_tokens) / len(claim_tokens) >= 0.72


def _canonical_fact_map(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    facts = list(bundle.get("usable_facts") or [])
    if not facts:
        facts = list(bundle.get("verified_facts") or []) + list(bundle.get("single_source_facts") or [])
    for item in facts:
        if not isinstance(item, dict):
            continue
        fact_id = str(item.get("fact_id") or item.get("canonical_fact_id") or "").strip()
        canonical = str(item.get("canonical_fact") or item.get("fact") or "").strip()
        if fact_id and canonical:
            result[fact_id] = item
    # R2.2.7 is positioned as a public-source assisted writing tool, not a
    # professional fact-ledger system.  If the simplified research flow has not
    # produced canonical facts, hard factual claims may still pass when the exact
    # claim is supported by accepted source text.  Fabricated numbers, penalties,
    # resignations, injuries, etc. still fail because they will not be found in
    # these source-backed support texts.
    for index, source in enumerate(bundle.get("sources") or [], start=1):
        if not isinstance(source, dict):
            continue
        if source.get("accepted_for_research") is False or source.get("fetch_success") is False:
            continue
        source_text = str(source.get("content") or source.get("text") or source.get("title") or "").strip()
        if not source_text:
            continue
        source_id = str(source.get("source_id") or f"source-{index}")
        result.setdefault(
            f"source:{source_id}",
            {
                "fact_id": f"source:{source_id}",
                "canonical_fact": source_text,
                "verification_type": "source_text",
                "supporting_source_ids": [source_id],
            },
        )
    return result


def _single_source_attributed(markdown: str, canonical: dict[str, Any], source_map: dict[str, dict[str, Any]]) -> bool:
    if str(canonical.get("verification_type") or "") != "single_source":
        return True
    text = str(markdown or "")
    if any(marker in text for marker in ("据", "根据", "公开资料显示", "目前公开资料显示", "从现有资料看")):
        return True
    for source_id in canonical.get("supporting_source_ids") or canonical.get("source_ids") or []:
        source = source_map.get(str(source_id))
        if not source:
            continue
        source_name = str(source.get("source_name") or source.get("publisher_id") or source.get("domain") or "").strip()
        if source_name and source_name in text:
            return True
    return False


def _unsupported_concrete_claims(markdown: str, fact_map: dict[str, dict[str, Any]]) -> list[str]:
    """Find concrete article statements that are absent from verified canonical facts."""
    unsupported: list[str] = []
    canonical_facts = [str(item.get("canonical_fact") or item.get("fact") or "") for item in fact_map.values()]
    body = _article_body_for_fact_scan(markdown)
    for clause in _fact_clauses(body):
        if not CONCRETE_CLAIM_RE.search(clause) or EXPLICIT_NONFACT_RE.search(clause):
            continue
        if SOURCE_REF_LINE_RE.match(clause) or URL_RE.search(clause) or PUBLISHED_AT_ONLY_RE.match(clause):
            continue
        if not any(claim_supported_by_fact(clause, fact) for fact in canonical_facts if fact.strip()):
            unsupported.append(clause[:240])
    return list(dict.fromkeys(unsupported))[:20]


def _soft_analysis_warning_clauses(markdown: str) -> list[str]:
    warnings: list[str] = []
    body = _article_body_for_fact_scan(markdown)
    for clause in _fact_clauses(body):
        if EXPLICIT_NONFACT_RE.search(clause):
            continue
        if CONCRETE_CLAIM_RE.search(clause):
            continue
        if SOFT_ANALYSIS_RE.search(clause):
            warnings.append(clause[:240])
    return list(dict.fromkeys(warnings))[:20]


def validate_fact_basis(article: dict[str, Any], research_bundle: dict[str, Any] | None) -> dict[str, Any]:
    """Validate model citations against the immutable, source-backed fact bundle."""
    bundle = research_bundle or {}
    source_map = {
        str(item.get("source_id")): item
        for item in bundle.get("sources") or []
        if isinstance(item, dict) and item.get("source_id") and item.get("fetch_success") and item.get("accepted_for_research") and not item.get("duplicate_of")
    }
    fact_map = _canonical_fact_map(bundle)
    facts = article.get("fact_basis")
    if not isinstance(facts, list) or not facts:
        return {"valid": False, "validated_count": 0, "total_count": 0, "source_coverage": 0.0, "cross_verified_count": 0, "invalid_reasons": ["文章没有返回可追溯的 fact_basis"]}
    unique_facts: list[Any] = []
    seen_fact_ids: set[str] = set()
    duplicate_fact_ids: set[str] = set()
    for item in facts:
        if isinstance(item, dict):
            item_id = str(item.get("fact_id") or item.get("canonical_fact_id") or "").strip()
            if item_id and item_id in seen_fact_ids:
                duplicate_fact_ids.add(item_id)
                continue
            if item_id:
                seen_fact_ids.add(item_id)
        unique_facts.append(item)
    validated = 0
    cross_verified = 0
    invalid: list[str] = []
    markdown = str(article.get("content_markdown") or "")
    for index, item in enumerate(unique_facts, start=1):
        prefix = f"第 {index} 条事实"
        if not isinstance(item, dict):
            invalid.append(f"{prefix}不是对象")
            continue
        fact_id = str(item.get("fact_id") or item.get("canonical_fact_id") or "").strip()
        canonical = fact_map.get(fact_id)
        if not fact_id or canonical is None:
            invalid.append(f"{prefix}的 fact_id 不在资料包可用事实中")
            continue
        submitted_fact = str(item.get("fact") or item.get("canonical_fact") or "").strip()
        canonical_text = str(canonical.get("canonical_fact") or canonical.get("fact") or "").strip()
        if _normalize(submitted_fact) != _normalize(canonical_text):
            invalid.append(f"{prefix}与 canonical_fact 不一致")
            continue
        ids = list(dict.fromkeys(str(value).strip() for value in item.get("source_ids") or item.get("supporting_source_ids") or [] if str(value).strip()))
        expected_ids = {str(value) for value in canonical.get("supporting_source_ids") or canonical.get("source_ids") or []}
        if not ids or any(source_id not in source_map for source_id in ids):
            invalid.append(f"{prefix}的 source_id 不存在或不是有效来源")
            continue
        if not set(ids).issubset(expected_ids):
            invalid.append(f"{prefix}引用了不支持该事实的来源")
            continue
        # Every cited source must pass independently. Never use any(source_supports_fact).
        unsupported = [source_id for source_id in ids if not _fact_supported(canonical_text, str(source_map[source_id].get("content") or source_map[source_id].get("summary") or ""))]
        if unsupported:
            invalid.append(f"{prefix}存在不支持该事实的引用来源：{', '.join(unsupported)}")
            continue
        if not _fact_supported(canonical_text, markdown):
            invalid.append(f"{prefix}没有在正文中实际使用")
            continue
        cited_publishers = {str(source_map[source_id].get("publisher_id") or source_map[source_id].get("domain") or source_id) for source_id in ids}
        is_official = str(canonical.get("verification_type") or "") == "official_single_source" or any(str(source_map[source_id].get("source_level") or "") == "official" or str(source_map[source_id].get("domain") or "").lower().endswith((".gov.cn", ".gov", ".edu.cn")) for source_id in ids)
        if not _single_source_attributed(markdown, canonical, source_map):
            invalid.append(f"{prefix}为单一来源信息，正文缺少“据XX报道/公开资料显示”等来源归属")
            continue
        validated += 1
        if not is_official and len(cited_publishers) >= 2:
            cross_verified += 1
    if duplicate_fact_ids:
        invalid.append(f"存在重复事实引用：{', '.join(sorted(duplicate_fact_ids))}")
    concrete_claims = _unsupported_concrete_claims(markdown, fact_map)
    if concrete_claims:
        invalid.append("正文存在未经 verified_facts 支持的具体陈述：" + "；".join(concrete_claims[:5]))
    total = len(unique_facts)
    return {
        "valid": not invalid,
        "validated_count": validated,
        "verified_fact_count": validated,
        "total_count": total,
        "source_coverage": round(validated / max(1, total), 4),
        "cross_verified_count": cross_verified,
        "duplicate_fact_ids": sorted(duplicate_fact_ids),
        "duplicate_fact_count": len(duplicate_fact_ids),
        "unsupported_concrete_claims": concrete_claims,
        "invalid_reasons": invalid[:20],
    }


def analyze_article(article: dict[str, Any], research_bundle: dict[str, Any] | None = None) -> dict[str, Any]:
    bundle = research_bundle or {}
    markdown = str(article.get("content_markdown") or "")
    sentences = _sentences(markdown)
    facts = article.get("fact_basis") if isinstance(article.get("fact_basis"), list) else []
    sources = bundle.get("sources") if isinstance(bundle.get("sources"), list) else []
    publisher_ids = {str(item.get("publisher_id") or item.get("domain") or "") for item in sources if isinstance(item, dict) and item.get("fetch_success") and item.get("accepted_for_research") and not item.get("duplicate_of")}
    publisher_ids.discard("")
    source_count = len(publisher_ids)
    time_count = len(TIME_RE.findall(markdown))
    entity_text = " ".join(str(item) for item in bundle.get("key_people") or []) + " " + " ".join(str(item) for item in bundle.get("key_organizations") or [])
    entity_count = len({item for item in re.split(r"[,，、；;\s]+", entity_text) if len(item) >= 2})
    number_count = len(NUMBER_RE.findall(markdown))
    counts = Counter(_normalize(sentence) for sentence in sentences)
    repeated = sum(count - 1 for count in counts.values() if count > 1)
    repetition_ratio = repeated / max(1, len(sentences))
    vague_count = sum(any(pattern in sentence for pattern in VAGUE_PATTERNS) for sentence in sentences)
    vague_ratio = vague_count / max(1, len(sentences))
    trace = validate_fact_basis(article, bundle) if facts else {"valid": False, "validated_count": 0, "verified_fact_count": 0, "total_count": 0, "source_coverage": 0.0, "cross_verified_count": 0, "duplicate_fact_ids": [], "duplicate_fact_count": 0, "unsupported_concrete_claims": _unsupported_concrete_claims(markdown, _canonical_fact_map(bundle)), "invalid_reasons": ["文章没有返回可追溯的 fact_basis"]}
    source_coverage = float(trace.get("source_coverage") or 0)
    target_word_count = int(article.get("word_count") or 0)
    actual_word_count = len(re.sub(r"\s+", "", markdown))
    length_ratio = actual_word_count / max(1, target_word_count) if target_word_count else 1.0
    score = min(100.0, round(
        min(30, int(trace.get("validated_count") or 0) * 6)
        + min(15, source_count * 5)
        + min(10, time_count * 5)
        + min(10, entity_count * 5)
        + min(10, number_count * 3)
        + source_coverage * 15
        + min(10, max(0.0, length_ratio) * 10)
        - repetition_ratio * 20
        - vague_ratio * 25,
        1,
    ))
    unique_basis_count = int(trace.get("total_count") or 0)
    return {"word_count": actual_word_count, "target_word_count": target_word_count, "length_ratio": round(length_ratio, 4), "verified_fact_count": int(trace.get("validated_count") or 0), "fact_basis_count": unique_basis_count, "invalid_fact_count": max(0, unique_basis_count - int(trace.get("validated_count") or 0)), "cross_verified_fact_count": int(trace.get("cross_verified_count") or 0), "source_count": source_count, "publisher_count": source_count, "time_count": time_count, "entity_count": entity_count, "number_count": number_count, "repetition_score": round(repetition_ratio, 4), "vague_sentence_ratio": round(vague_ratio, 4), "source_coverage": round(source_coverage, 4), "sentence_count": len(sentences), "information_sufficiency_score": float(score), "fact_trace": trace}


def _cleanup_claim_text(text: str, claims: list[str]) -> str:
    cleaned = str(text or "")
    for claim in claims:
        claim_text = str(claim or "").strip()
        if not claim_text:
            continue
        cleaned = cleaned.replace(claim_text + "。", "")
        cleaned = cleaned.replace(claim_text + "，", "")
        cleaned = cleaned.replace(claim_text + "；", "")
        cleaned = cleaned.replace(claim_text, "")
    cleaned = re.sub(r"[，,；;]{2,}", "，", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" ，；。\n")


def _rebuild_markdown(article: dict[str, Any]) -> str:
    parts: list[str] = []
    title = str(article.get("title") or "").strip()
    intro = str(article.get("intro") or "").strip()
    if title:
        parts.append(f"# {title}")
    if intro:
        parts.append(intro)
    for section in article.get("sections") or []:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or "").strip()
        body = str(section.get("body") or "").strip()
        if not body:
            continue
        parts.append(f"## {heading}\n{body}" if heading else body)
    source_list = normalize_source_list(article.get("source_list") or [])
    if source_list:
        parts.append("资料来源\n" + "\n\n".join(source_list))
    ai_statement = str(article.get("ai_statement") or "").strip()
    if ai_statement:
        parts.append(ai_statement)
    return "\n\n".join(part for part in parts if part).strip()


def sanitize_article_hard_facts(article: dict[str, Any], research_bundle: dict[str, Any] | None) -> dict[str, Any]:
    bundle = research_bundle or {}
    cleaned = dict(article)
    trace = validate_fact_basis(cleaned, bundle) if isinstance(cleaned.get("fact_basis"), list) and cleaned.get("fact_basis") else {
        "unsupported_concrete_claims": _unsupported_concrete_claims(str(cleaned.get("content_markdown") or ""), _canonical_fact_map(bundle)),
        "invalid_reasons": [],
    }
    claims = [
        str(item).strip()
        for item in trace.get("unsupported_concrete_claims") or []
        if str(item).strip() and HARD_FACT_WARNING_RE.search(str(item))
    ]
    if not claims:
        return {"article": cleaned, "removed_claims": [], "trace": trace}
    cleaned["intro"] = _cleanup_claim_text(str(cleaned.get("intro") or ""), claims) or "当前公开资料较少，建议发布前再次核对关键信息。"
    sections: list[dict[str, Any]] = []
    for section in cleaned.get("sections") or []:
        if not isinstance(section, dict):
            continue
        body = _cleanup_claim_text(str(section.get("body") or ""), claims)
        if body:
            sections.append({**section, "body": body})
    while len(sections) < 3:
        sections.append(
            {
                "heading": ["事件概览", "背景补充", "影响观察"][len(sections)],
                "body": "基于现有公开资料，相关细节仍需以后续权威信息为准。",
                "image_brief": "与该段信息相关的现实新闻场景，无文字",
            }
        )
    cleaned["sections"] = sections
    cleaned["content_markdown"] = _rebuild_markdown(cleaned)
    return {"article": cleaned, "removed_claims": claims, "trace": trace}


def quality_gate(article: dict[str, Any], research_bundle: dict[str, Any] | None, *, minimum_score: float = 35.0) -> dict[str, Any]:
    metrics = analyze_article(article, research_bundle)
    bundle = research_bundle or {}
    hard_reasons: list[str] = []
    warning_reasons: list[str] = []
    warning_clauses = _soft_analysis_warning_clauses(str(article.get("content_markdown") or ""))
    markdown = str(article.get("content_markdown") or "")
    accepted_source_count = int(bundle.get("accepted_source_count") or 0)
    trace = metrics.get("fact_trace") or {}

    if not markdown.strip():
        hard_reasons.append("正文内容为空")
    if accepted_source_count <= 0:
        hard_reasons.append("没有找到可用公开资料来源")

    if article.get("fact_basis") and not trace.get("valid", True):
        warning_reasons.extend(str(item) for item in (trace.get("invalid_reasons") or [])[:5])

    unsupported = [
        str(item).strip()
        for item in trace.get("unsupported_concrete_claims") or []
        if str(item).strip() and HARD_FACT_WARNING_RE.search(str(item))
    ]
    warning_reasons.extend(f"已本地删除缺少来源支撑的硬事实：{claim[:80]}" for claim in unsupported[:5])

    if metrics["entity_count"] < 1 and accepted_source_count > 0:
        warning_reasons.append("正文中的人物或机构信息较少，建议人工复核表达完整度")

    format_warning = str(article.get("format_warning") or "").strip()
    if format_warning:
        warning_reasons.append(format_warning)

    hard_reasons = list(dict.fromkeys(hard_reasons))
    warning_reasons = list(dict.fromkeys(warning_reasons))
    status = "failed" if hard_reasons else "warning" if warning_reasons else "passed"
    return {
        "passed": status != "failed",
        "status": status,
        "draft_status": "完成但建议核对" if status == "warning" else "",
        "hard_error_count": len(hard_reasons),
        "warning_count": len(warning_reasons),
        "hard_errors": hard_reasons,
        "warnings": warning_reasons,
        "warning_clauses": warning_clauses,
        "reasons": hard_reasons + warning_reasons,
        "metrics": metrics,
    }
