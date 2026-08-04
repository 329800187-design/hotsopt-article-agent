from __future__ import annotations

import re
from typing import Any


IMAGE_PLANS: dict[str, dict[str, Any]] = {
    "none": {"label": "纯文字", "cover": 0, "inline_min": 0, "inline_max": 0, "retry_limit": 0},
    "economy": {"label": "经济型", "cover": 1, "inline_min": 0, "inline_max": 0, "retry_limit": 0},
    "low": {"label": "低成本型", "cover": 1, "inline_min": 1, "inline_max": 1, "retry_limit": 0},
    "standard": {"label": "标准型", "cover": 1, "inline_min": 1, "inline_max": 1, "retry_limit": 0},
    "three": {"label": "3张图", "cover": 1, "inline_min": 2, "inline_max": 2, "retry_limit": 0},
    "four": {"label": "4张图", "cover": 1, "inline_min": 3, "inline_max": 3, "retry_limit": 0},
    "five": {"label": "5张图", "cover": 1, "inline_min": 4, "inline_max": 4, "retry_limit": 0},
    "rich": {"label": "标准型", "cover": 1, "inline_min": 1, "inline_max": 1, "retry_limit": 0},
}


def normalize_image_plan(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    aliases = {
        "": "none",
        "text": "none",
        "plain": "none",
        "pure_text": "none",
        "none": "none",
        "economy": "economy",
        "economic": "economy",
        "low": "low",
        "standard": "standard",
        "rich": "standard",
        "three": "three",
        "3": "three",
        "four": "four",
        "4": "four",
        "five": "five",
        "5": "five",
    }
    return aliases.get(normalized, "none")


def image_plan_for(word_count: int, mode: str = "standard") -> dict[str, Any]:
    normalized = normalize_image_plan(mode)
    plan = dict(IMAGE_PLANS[normalized])
    words = int(word_count or 0)
    inline_count = int(plan.get("inline_min") or 0)
    plan.update({"mode": normalized, "word_count": words, "inline_count": inline_count, "max_calls": int(plan["cover"]) + inline_count})
    return plan


def calculate_image_budget(article_count: int, image_mode: str) -> int:
    count = max(0, int(article_count or 0))
    mode = normalize_image_plan(image_mode)
    per_article = {"none": 0, "economy": 1, "low": 2, "standard": 2, "three": 3, "four": 4, "five": 5}[mode]
    return count * per_article


def estimate_image_calls(article_count: int, word_count: int, mode: str = "standard") -> dict[str, int | str]:
    plan = image_plan_for(word_count, mode)
    count = max(0, int(article_count))
    attempts = calculate_image_budget(count, str(plan["mode"]))
    retries = count * int(plan["max_calls"]) * int(plan["retry_limit"])
    return {
        "mode": str(plan["mode"]),
        "articles": count,
        "cover_count": count * int(plan["cover"]),
        "inline_count": count * int(plan["inline_count"]),
        "image_calls": attempts,
        "max_retry_calls": retries,
        "max_possible_calls": attempts + retries,
    }


def image_cost_preview(article_count: int, word_count: int, mode: str, unit_price: float | None = None) -> dict[str, Any]:
    result = estimate_image_calls(article_count, word_count, mode)
    if unit_price is not None and float(unit_price) >= 0:
        result["estimated_cost"] = round(float(result["max_possible_calls"]) * float(unit_price), 6)
    else:
        result["estimated_cost"] = None
    result["cost_notice"] = (
        f"预计调用图片接口 {result['max_possible_calls']} 次，实际费用由模型服务商收取。"
        if result["estimated_cost"] is None
        else "估算金额仅供参考，实际费用由模型服务商收取。"
    )
    return result


_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")


def count_body_chinese_chars(article: dict[str, Any]) -> int:
    """Count visible Chinese body chars with one production-wide definition.

    Includes lead/intro plus section bodies. Excludes title, section headings,
    markdown markers, source lists, URLs, keywords, AI statements, and punctuation.
    """
    lead = str(article.get("lead") or article.get("intro") or "")
    section_bodies = "\n".join(
        str(section.get("body") or "")
        for section in article.get("sections") or []
        if isinstance(section, dict)
    )
    if lead or section_bodies.strip():
        return len(_CHINESE_RE.findall(f"{lead}\n{section_bodies}"))
    return len(_CHINESE_RE.findall(_legacy_body_markdown(article.get("content_markdown") or "")))


def _legacy_body_markdown(markdown: Any) -> str:
    """Recover body text for older saved articles that predate structured sections."""
    lines: list[str] = []
    in_body = False
    skip_section = False
    for raw_line in str(markdown or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r"^#\s+", line):
            continue
        h2 = re.match(r"^#{2,6}\s*(.+)$", line)
        if h2:
            heading = h2.group(1).strip()
            skip_section = bool(re.search(r"资料来源|参考资料|来源|AI|声明|关键词|标签", heading, re.I))
            in_body = not skip_section
            continue
        if skip_section or re.search(r"https?://|原文链接|AI辅助|生成声明|免责声明", line, re.I):
            continue
        if not in_body:
            in_body = True
        lines.append(re.sub(r"[*_`>\-\[\]()]+", "", line))
    return "\n".join(lines)


def recommended_word_count(target: Any) -> int:
    try:
        value = int(target or 0)
    except (TypeError, ValueError):
        value = 0
    if value >= 1600:
        return 1600
    if value >= 1500:
        return 1500
    return 1200
