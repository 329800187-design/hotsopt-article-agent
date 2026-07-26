from __future__ import annotations

import re
from typing import Any


IMAGE_PLANS: dict[str, dict[str, Any]] = {
    "none": {"label": "纯文字", "cover": 0, "inline_min": 0, "inline_max": 0, "retry_limit": 0},
    "economy": {"label": "经济型", "cover": 1, "inline_min": 0, "inline_max": 0, "retry_limit": 0},
    "standard": {"label": "标准型", "cover": 1, "inline_min": 1, "inline_max": 1, "retry_limit": 0},
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
        "standard": "standard",
        "rich": "standard",
    }
    return aliases.get(normalized, "none")


def image_plan_for(word_count: int, mode: str = "standard") -> dict[str, Any]:
    normalized = normalize_image_plan(mode)
    plan = dict(IMAGE_PLANS[normalized])
    words = int(word_count or 0)
    inline_count = 0 if normalized != "standard" else 1
    plan.update({"mode": normalized, "word_count": words, "inline_count": inline_count, "max_calls": int(plan["cover"]) + inline_count})
    return plan


def calculate_image_budget(article_count: int, image_mode: str) -> int:
    count = max(0, int(article_count or 0))
    mode = normalize_image_plan(image_mode)
    per_article = {"none": 0, "economy": 1, "standard": 2}[mode]
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
    intro = str(article.get("intro") or "")
    section_bodies = "\n".join(str(section.get("body") or "") for section in article.get("sections") or [] if isinstance(section, dict))
    return len(_CHINESE_RE.findall(f"{intro}\n{section_bodies}"))


def recommended_word_count(target: Any) -> int:
    try:
        value = int(target or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return 800
    if value < 700:
        return 700
    if value > 1600:
        return 1600
    return value
