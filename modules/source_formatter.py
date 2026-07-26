from __future__ import annotations

import ast
import json
import re
from typing import Any


_WHITESPACE_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"https://\S+", re.I)
_INDEX_PREFIX_RE = re.compile(r"^\s*\[\d+\]\s*")


def _clean_text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _looks_like_serialized_mapping(text: str) -> bool:
    text = text.strip()
    return bool(text) and text[0] in "[{" and text[-1] in "]}"


def _parse_source_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    text = _clean_text(value)
    if not _looks_like_serialized_mapping(text):
        return None
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(text)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _pick(mapping: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = _clean_text(mapping.get(key))
        if value:
            return value
    return default


def _normalize_date(value: str) -> str:
    text = _clean_text(value)
    return text or "发布日期未知"


def _normalize_url(value: str) -> str:
    text = _clean_text(value)
    return text or "链接未知"


def _format_source_mapping(mapping: dict[str, Any], index: int) -> str:
    publisher = _pick(
        mapping,
        "publisher",
        "source_name",
        "source",
        "organization",
        "publisher_id",
        "domain",
        default="发布机构未知",
    )
    title = _pick(mapping, "title", "headline", "name", default="未命名原文")
    published_at = _normalize_date(_pick(mapping, "published_at", "publish_date", "published", "date", "publishedAt"))
    url = _normalize_url(_pick(mapping, "url", "link", "source_url", "original_url"))
    return f"[{index}] {publisher}：《{title}》，{published_at}\n原文链接：{url}"


def _format_source_text(text: str, index: int) -> str:
    normalized = _clean_text(text)
    if not normalized:
        return ""
    mapping = _parse_source_mapping(normalized)
    if mapping:
        return _format_source_mapping(mapping, index)
    if "原文链接：" in normalized:
        lines = [line.strip() for line in normalized.splitlines() if line.strip()]
        if not lines:
            return ""
        first = _INDEX_PREFIX_RE.sub("", lines[0])
        remaining = [line for line in lines[1:] if line]
        if remaining:
            return f"[{index}] {first}\n" + "\n".join(remaining)
        return f"[{index}] {first}"
    url_match = _URL_RE.search(normalized)
    if url_match:
        url = url_match.group(0)
        prefix = normalized.replace(url, "").strip(" ：:;,，")
        return f"[{index}] 发布机构未知：《{prefix or '未命名原文'}》，发布日期未知\n原文链接：{url}"
    compact = _WHITESPACE_RE.sub(" ", normalized)
    return f"[{index}] 发布机构未知：《{compact}》，发布日期未知\n原文链接：链接未知"


def normalize_source_list(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    normalized: list[str] = []
    for item in items:
        if item is None:
            continue
        text = _format_source_mapping(item, len(normalized) + 1) if isinstance(item, dict) else _format_source_text(str(item), len(normalized) + 1)
        if text:
            normalized.append(text)
    return normalized
