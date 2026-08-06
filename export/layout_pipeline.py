"""Automatic article layout and user-facing product checks."""

from __future__ import annotations

import copy
import re
from typing import Any

from generation.image_budget import count_body_chinese_chars
from modules.source_formatter import normalize_source_list

TECHNICAL_KEYS = {
    "content_markdown",
    "fact_basis",
    "source_ids",
    "canonical_fact_id",
    "debug",
    "traceback",
    "prompt",
    "provider_response",
    "api_key",
    "model_info",
    "quality_gate",
    "research_bundle",
}
MARKDOWN_RE = re.compile(r"(^|\s)(#{1,6}|[*_`]{1,3}|```|[-+]\s)(?=\S)")
JSON_FIELD_RE = re.compile(r"[\"'](?:content_markdown|fact_basis|source_ids|api_key|model_info|quality_gate)[\"']\s*:")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])")
PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n+")


def clean_display_text(value: Any, *, preserve_breaks: bool = False) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"```(?:json|markdown|python)?", "", text, flags=re.I)
    text = re.sub(r"\{\s*[\"'](?:content_markdown|fact_basis|source_ids|api_key|model_info)[\"'].*?\}", "", text, flags=re.S)
    text = re.sub(r"(^|\n)\s*#{1,6}\s*", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    if preserve_breaks:
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
    return re.sub(r"\s+", " ", text).strip()


def clean_title_text(value: Any) -> str:
    text = clean_display_text(value)
    text = re.sub(r"^(?:标题|新标题|文章标题)\s*[:：]\s*", "", text).strip()
    return text.strip(" \"'“”‘’《》")


def clean_lead_text(value: Any, title: str = "") -> str:
    text = clean_display_text(value)
    text = re.sub(r"^(?:导语|摘要|引言)\s*[:：]\s*", "", text).strip()
    if _same_block(text, title):
        return ""
    return text.strip(" \"'“”‘’")


def _clean_lines(value: Any) -> list[str]:
    if isinstance(value, list):
        return [clean_display_text(item) for item in value if clean_display_text(item)]
    cleaned = clean_display_text(value)
    return [cleaned] if cleaned else []


def _split_dense_paragraphs(text: str, target: int = 180) -> str:
    normalized = clean_display_text(text, preserve_breaks=True)
    blocks = [block.strip() for block in PARAGRAPH_SPLIT_RE.split(normalized) if block.strip()]
    rebuilt: list[str] = []
    for block in blocks or ([normalized] if normalized else []):
        compact = re.sub(r"\s+", "", block)
        if len(compact) <= target:
            rebuilt.append(block)
            continue
        sentences = [item.strip() for item in SENTENCE_SPLIT_RE.split(block) if item.strip()]
        if not sentences:
            rebuilt.append(block)
            continue
        buffer = ""
        parts: list[str] = []
        for sentence in sentences:
            candidate = f"{buffer}{sentence}" if buffer else sentence
            if buffer and len(re.sub(r"\s+", "", candidate)) > target:
                parts.append(buffer.strip())
                buffer = sentence
            else:
                buffer = candidate
        if buffer.strip():
            parts.append(buffer.strip())
        rebuilt.extend(parts or [block])
    return "\n\n".join(item for item in rebuilt if item).strip()


def _paragraph_count(sections: list[dict[str, Any]]) -> int:
    total = 0
    for section in sections:
        if not isinstance(section, dict):
            continue
        body = str(section.get("body") or "").strip()
        total += len([block for block in PARAGRAPH_SPLIT_RE.split(body) if block.strip()])
    return total


def _longest_dense_block(sections: list[dict[str, Any]]) -> int:
    longest = 0
    for section in sections:
        if not isinstance(section, dict):
            continue
        for block in PARAGRAPH_SPLIT_RE.split(str(section.get("body") or "")):
            compact = re.sub(r"\s+", "", block)
            longest = max(longest, len(compact))
    return longest


def _body_chinese_char_count(article: dict[str, Any]) -> int:
    return count_body_chinese_chars(article)


def _rebuild_content_markdown(article: dict[str, Any]) -> str:
    parts: list[str] = []
    title = str(article.get("title") or "").strip()
    intro = str(article.get("lead") or article.get("intro") or article.get("subtitle") or "").strip()
    if title:
        parts.append(f"# {title}")
    if intro:
        parts.append(intro)
    body = _rebuild_body_markdown(article)
    if body:
        parts.append(body)
    return "\n\n".join(part for part in parts if part).strip()


def _rebuild_body_markdown(article: dict[str, Any]) -> str:
    parts: list[str] = []
    for section in article.get("sections") or []:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or "").strip()
        body = str(section.get("body") or "").strip()
        if not body:
            continue
        parts.append(f"## {heading}\n{body}".strip() if heading else body)
    return "\n\n".join(part for part in parts if part).strip()


def _normalized_text(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or ""))
    text = re.sub(r"^#{1,6}", "", text)
    text = re.sub(r"^(?:标题|新标题|文章标题|导语|摘要|引言)[:：]", "", text)
    return text.strip("：:。！？!?\"'“”‘’《》")


def _same_block(left: Any, right: Any) -> bool:
    a = _normalized_text(left)
    b = _normalized_text(right)
    return bool(a and b and a == b)


def prepare_article_layout(article: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(article or {})
    result["title"] = clean_title_text(result.get("title") or "未命名文章")
    result["subtitle"] = clean_lead_text(result.get("lead") or result.get("intro") or result.get("subtitle") or result.get("summary") or "", result["title"])
    result["intro"] = result["subtitle"]
    result["lead"] = result["subtitle"]
    sections: list[dict[str, Any]] = []
    for index, section in enumerate(result.get("sections") or [], start=1):
        if not isinstance(section, dict):
            continue
        heading = clean_display_text(section.get("heading") or f"正文 {index}")
        raw_body = _split_dense_paragraphs(section.get("body") or "")
        blocks = [block.strip() for block in PARAGRAPH_SPLIT_RE.split(raw_body) if block.strip()]
        filtered: list[str] = []
        for block in blocks:
            if _same_block(block, result["title"]) or _same_block(block, result["lead"]):
                continue
            cleaned_block = block
            if result["title"] and cleaned_block.startswith(result["title"]):
                cleaned_block = cleaned_block[len(result["title"]):].strip()
            if result["lead"] and cleaned_block.startswith(result["lead"]):
                cleaned_block = cleaned_block[len(result["lead"]):].strip()
            cleaned_block = cleaned_block.lstrip("，,。:：；;、 \t")
            if cleaned_block:
                filtered.append(cleaned_block)
        body = "\n\n".join(filtered).strip()
        if not body:
            continue
        item = dict(section)
        item.update(
            {
                "heading": heading,
                "body": body,
                "image_brief": clean_display_text(section.get("image_brief") or ""),
            }
        )
        sections.append(item)
    result["sections"] = sections
    result["keywords"] = _clean_lines(result.get("keywords") or result.get("tags") or [])
    result["source_statement"] = clean_display_text(result.get("source_statement") or "")
    result["source_list"] = [item for item in normalize_source_list(result.get("source_list") or []) if item]
    for image in result.get("images") or []:
        if isinstance(image, dict):
            image["caption"] = clean_display_text(image.get("caption") or image.get("purpose") or "")
    result["body_markdown"] = _rebuild_body_markdown(result)
    result["content_markdown"] = _rebuild_content_markdown(result)
    result["body_char_count"] = _body_chinese_char_count(result)
    result["layout_status"] = "passed"
    result["layout_check"] = check_article_product(result)
    if not result["layout_check"]["passed"]:
        result["layout_status"] = "failed"
    return result


def check_article_product(article: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    title = str(article.get("title") or "").strip()
    sections = article.get("sections") or []
    paragraph_count = _paragraph_count(sections)
    intro_count = 1 if str(article.get("intro") or article.get("subtitle") or "").strip() else 0
    total_paragraph_count = paragraph_count + intro_count
    longest_dense = _longest_dense_block(sections)

    if not title:
        reasons.append("缺少标题")
    if not isinstance(sections, list) or not sections:
        reasons.append("缺少正文分节")
    elif len(sections) < 3:
        reasons.append("正文小节少于 3 个")
    elif len(sections) > 5:
        reasons.append("正文小节超过 5 个")

    for field, value in (("title", article.get("title")), ("subtitle", article.get("subtitle"))):
        text = str(value or "")
        if MARKDOWN_RE.search(text) or JSON_FIELD_RE.search(text):
            reasons.append(f"{field} 含有技术格式残留")

    for section in sections:
        if not isinstance(section, dict) or not str(section.get("body") or "").strip():
            reasons.append("存在空正文段落")
            continue
        block = f"{section.get('heading') or ''} {section.get('body') or ''}"
        if JSON_FIELD_RE.search(block):
            reasons.append("正文含有 JSON 字段")

    forbidden = [key for key in TECHNICAL_KEYS if key in str(article.get("content_markdown") or "")]
    if forbidden:
        reasons.append("正文存在工程技术字段")
    if paragraph_count < 3:
        reasons.append("正文自然段少于 3 段")
    if longest_dense > 500:
        reasons.append("存在超过 500 字的长墙文本")

    checks = {
        "title": bool(title),
        "section_count": 3 <= len(sections) <= 5,
        "paragraph_count": total_paragraph_count >= 4,
        "no_wall_of_text": longest_dense <= 500,
        "markdown_clean": not forbidden,
    }
    return {
        "passed": not reasons,
        "status": "passed" if not reasons else "failed",
        "reasons": list(dict.fromkeys(reasons)),
        "checks": checks,
    }


def ensure_article_layout(article: dict[str, Any]) -> dict[str, Any]:
    result = prepare_article_layout(article)
    if result.get("layout_status") != "passed" or not (result.get("layout_check") or {}).get("passed"):
        raise ValueError("ARTICLE_LAYOUT_CHECK_FAILED")
    return result
