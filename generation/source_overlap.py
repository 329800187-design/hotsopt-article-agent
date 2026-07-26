from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

LONG_COPY_CHARS = 30
FIVE_GRAM_THRESHOLD = 0.35
PARAGRAPH_ORDER_THRESHOLD = 0.5

DATE_NUMBER_RE = re.compile(
    r"(:20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日)"
    r"|(:\d{1,2}:\d{2}(::\d{2}))"
    r"|(:\d+(:\.\d+)[%％万亿千百十元公斤克毫米厘米米吨名例次篇项枚颗度])"
    r"|(:\d+(:\.\d+))"
)
QUOTE_RE = re.compile(r"[\"“”'‘’][^\"“”'‘’]{0,80}[\"“”'‘’]")
POLICY_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]{2,30}(:办法|条例|方案|意见|通知|公告|计划|指南|细则|规定)")
CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
SOURCE_HEADINGS = {
    "资料来源",
    "参考资料",
    "信息来源",
    "来源",
}


def _article_body(markdown: str) -> str:
    body_lines: list[str] = []
    for raw_line in str(markdown or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = raw_line.strip()
        if not line:
            body_lines.append("")
            continue
        heading = re.sub(r"^#{1,6}\s+", "", line).strip().rstrip("：:")
        if heading in SOURCE_HEADINGS:
            break
        if line.startswith("# "):
            continue
        body_lines.append(re.sub(r"^#{2,6}\s+", "", raw_line).strip())
    return "\n".join(body_lines).strip()


def _entity_terms(bundle: dict[str, Any] | None) -> set[str]:
    bundle = bundle or {}
    terms: set[str] = set()
    for value in list(bundle.get("key_people") or [])[:5] + list(bundle.get("key_organizations") or [])[:5]:
        item = str(value or "").strip()
        if len(item) >= 2:
            terms.add(item)
    for card in list(bundle.get("research_fact_cards") or [])[:15]:
        if not isinstance(card, dict):
            continue
        for key in ("subject", "object", "location"):
            item = str(card.get(key) or "").strip()
            if len(item) >= 2:
                terms.add(item)
    return terms


def _strip_exclusions(text: str, bundle: dict[str, Any] | None) -> str:
    cleaned = str(text or "")
    cleaned = QUOTE_RE.sub(" ", cleaned)
    cleaned = DATE_NUMBER_RE.sub(" ", cleaned)
    cleaned = POLICY_RE.sub(" ", cleaned)
    for term in sorted(_entity_terms(bundle), key=len, reverse=True):
        cleaned = cleaned.replace(term, " ")
    return cleaned


def _normalized_chinese(text: str, bundle: dict[str, Any] | None) -> str:
    stripped = _strip_exclusions(text, bundle)
    return "".join(CHINESE_RE.findall(stripped))


def _ngrams(text: str, size: int = 5) -> set[str]:
    if len(text) < size:
        return set()
    return {text[index : index + size] for index in range(len(text) - size + 1)}


def _paragraphs(text: str, bundle: dict[str, Any] | None) -> list[str]:
    blocks = [block.strip() for block in re.split(r"\n\s*\n+", str(text or "")) if block.strip()]
    paragraphs: list[str] = []
    for block in blocks:
        value = _normalized_chinese(block, bundle)
        if len(value) >= 20:
            paragraphs.append(value)
    return paragraphs


def analyze_source_overlap(article: dict[str, Any], research_bundle: dict[str, Any] | None) -> dict[str, Any]:
    bundle = research_bundle or {}
    body = _article_body(str(article.get("content_markdown") or ""))
    article_norm = _normalized_chinese(body, bundle)
    sources = [
        item
        for item in bundle.get("sources") or []
        if isinstance(item, dict) and item.get("fetch_success") and item.get("accepted_for_research") and not item.get("duplicate_of")
    ]
    if not article_norm or not sources:
        return {
            "status": "passed",
            "violations": [],
            "max_five_gram_overlap": 0.0,
            "max_paragraph_order_overlap": 0.0,
            "matched_source_id": "",
            "matched_source_name": "",
        }

    article_grams = _ngrams(article_norm, 5)
    article_paragraphs = _paragraphs(body, bundle)
    best = {
        "status": "passed",
        "violations": [],
        "max_five_gram_overlap": 0.0,
        "max_paragraph_order_overlap": 0.0,
        "matched_source_id": "",
        "matched_source_name": "",
    }

    for source in sources:
        source_text = str(source.get("content") or source.get("text") or source.get("summary") or source.get("title") or "")
        source_norm = _normalized_chinese(source_text, bundle)
        if not source_norm:
            continue

        long_copy = False
        if len(article_norm) >= LONG_COPY_CHARS:
            for index in range(len(article_norm) - LONG_COPY_CHARS + 1):
                segment = article_norm[index : index + LONG_COPY_CHARS]
                if segment and segment in source_norm:
                    long_copy = True
                    break

        source_grams = _ngrams(source_norm, 5)
        five_gram_ratio = (len(article_grams & source_grams) / len(article_grams)) if article_grams else 0.0
        source_paragraphs = _paragraphs(source_text, bundle)
        matched = 0
        source_index = 0
        for paragraph in article_paragraphs:
            while source_index < len(source_paragraphs):
                ratio = SequenceMatcher(None, paragraph, source_paragraphs[source_index]).ratio()
                source_index += 1
                if ratio >= 0.82:
                    matched += 1
                    break
        paragraph_ratio = (matched / len(article_paragraphs)) if article_paragraphs else 0.0

        violations: list[str] = []
        if long_copy:
            violations.append("long_copy")
        if five_gram_ratio > FIVE_GRAM_THRESHOLD:
            violations.append("five_gram_overlap")
        if paragraph_ratio > PARAGRAPH_ORDER_THRESHOLD:
            violations.append("paragraph_order")

        current_score = max(float(five_gram_ratio), float(paragraph_ratio), 1.0 if long_copy else 0.0)
        best_score = max(
            float(best.get("max_five_gram_overlap") or 0.0),
            float(best.get("max_paragraph_order_overlap") or 0.0),
            1.0 if "long_copy" in (best.get("violations") or []) else 0.0,
        )
        if current_score >= best_score:
            best = {
                "status": "review_required" if violations else "passed",
                "violations": violations,
                "max_five_gram_overlap": round(five_gram_ratio, 4),
                "max_paragraph_order_overlap": round(paragraph_ratio, 4),
                "matched_source_id": str(source.get("source_id") or ""),
                "matched_source_name": str(source.get("source_name") or source.get("publisher") or source.get("domain") or ""),
            }

    return best
