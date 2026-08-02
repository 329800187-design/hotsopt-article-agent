from __future__ import annotations

import re
from typing import Any


META_CONTENT_PATTERNS = {
    "ARTICLE_META_CONTENT_LEAK": [
        r"^\s*(?:\u5199\u4f5c\u601d\u8def|\u521b\u4f5c\u601d\u8def|\u5185\u5bb9\u89c4\u5212|\u5199\u4f5c\u6846\u67b6|\u601d\u8003\u8fc7\u7a0b|\u5206\u6790\u8fc7\u7a0b|\u751f\u6210\u903b\u8f91)\s*[:：]",
        r"\u4e0b\u9762\u6211\u5c06",
        r"\u4f5c\u4e3a\s*AI",
        r"\u6839\u636e\u4ee5\u4e0a\u5206\u6790",
        r"\u4ee5\u4e0b\u662f\u6587\u7ae0\u7ed3\u6784",
        r"\u4ee5\u4e0b\u662f\u5199\u4f5c\u65b9\u6848",
        r"\u4e8b\u5b9e\u5361\s*[:：]",
        r"\u5185\u90e8\u6838\u9a8c",
        r"\u8d28\u91cf\u68c0\u6d4b\u7ed3\u679c",
        r"\u91cd\u5199\u5efa\u8bae",
        r"AI\s*\u8f85\u52a9\u58f0\u660e",
        r"AI\s*\u751f\u6210",
        r"\u672c\u6587\u7531\s*AI",
        r"\u6a21\u578b\u751f\u6210",
        r"\u5185\u5bb9\u7531\u6a21\u578b\u751f\u6210",
        r"\u8f6f\u4ef6\u751f\u6210\u8bf4\u660e",
    ],
    "SOURCE_METADATA_LEAK": [
        r"^\s*(?:\u8d44\u6599\u6765\u6e90|\u53c2\u8003\u8d44\u6599|\u53c2\u8003\u94fe\u63a5|\u6765\u6e90\u5e73\u53f0|\u65b0\u95fb\u6765\u6e90|\u539f\u6587\u94fe\u63a5|URL\s*\u5217\u8868)\s*$",
        r"^\s*\[\d+\].*(?:https?://|\u539f\u6587\u94fe\u63a5)",
        r"\u539f\u6587\u94fe\u63a5\s*[:：]\s*https?://",
    ],
}


def body_markdown_from_sections(article: dict[str, Any]) -> str:
    body = str(article.get("body_markdown") or "").strip()
    if body:
        return body
    parts: list[str] = []
    for section in article.get("sections") or []:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or "").strip()
        text = str(section.get("body") or "").strip()
        if text:
            parts.append(f"## {heading}\n{text}".strip() if heading else text)
    if parts:
        return "\n\n".join(parts).strip()
    return str(article.get("content_markdown") or "").strip()


def meta_content_hits(text: str) -> list[str]:
    content = str(text or "")
    hits: list[str] = []
    for code, patterns in META_CONTENT_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, content, flags=re.I | re.M):
                hits.append(code)
                break
    return list(dict.fromkeys(hits))


def customer_visible_article(article: dict[str, Any]) -> dict[str, Any]:
    body = body_markdown_from_sections(article)
    return {
        "title": str(article.get("title") or "").strip(),
        "lead": str(article.get("lead") or article.get("intro") or article.get("summary") or "").strip(),
        "body_markdown": body,
        "sections": article.get("sections") or [],
        "images": article.get("images") or [],
        "cover": article.get("cover"),
        "layout_check": article.get("layout_check"),
        "layout_status": article.get("layout_status"),
        "body_char_count": article.get("body_char_count"),
        "quality_gate": article.get("quality_gate"),
    }


def ensure_no_customer_meta_content(article: dict[str, Any]) -> None:
    visible = customer_visible_article(article)
    text = "\n".join(str(visible.get(key) or "") for key in ("title", "lead", "body_markdown"))
    hits = meta_content_hits(text)
    if hits:
        raise ValueError("ARTICLE_META_CONTENT_LEAK: " + ",".join(hits))
