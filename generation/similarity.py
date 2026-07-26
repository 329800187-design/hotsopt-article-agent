from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any


SIMILARITY_THRESHOLDS = {
    "title_similarity": 0.75,
    "opening_similarity": 0.65,
    "structure_similarity": 0.75,
    "body_similarity": 0.72,
    "overall_similarity": 0.72,
}


def normalize(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text or "", flags=re.UNICODE).lower()


def similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize(left), normalize(right)).ratio()


def _opening(article: dict[str, Any]) -> str:
    content = str(article.get("content_markdown") or article.get("intro") or "")
    return content[:150]


def _structure(article: dict[str, Any]) -> str:
    sections = article.get("sections") or []
    return "|".join(str(section.get("heading") or "") for section in sections if isinstance(section, dict))


def _body(article: dict[str, Any]) -> str:
    sections = article.get("sections") or []
    if sections:
        return "\n".join(str(section.get("body") or "") for section in sections if isinstance(section, dict))
    return str(article.get("content_markdown") or "")


def compare_articles(left: dict[str, Any], right: dict[str, Any], thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    limits = dict(SIMILARITY_THRESHOLDS)
    limits.update(thresholds or {})
    values = {
        "title_similarity": similarity(str(left.get("title") or ""), str(right.get("title") or "")),
        "opening_similarity": similarity(_opening(left), _opening(right)),
        "structure_similarity": similarity(_structure(left), _structure(right)),
        "body_similarity": similarity(_body(left), _body(right)),
    }
    values["overall_similarity"] = round((values["title_similarity"] + values["opening_similarity"] + values["structure_similarity"] + values["body_similarity"]) / 4, 6)
    violations = [name for name, value in values.items() if value > limits.get(name, 1.0)]
    return {**{key: round(value, 6) for key, value in values.items()}, "status": "rewrite_required" if violations else "passed", "violations": violations}


def compare_batch_articles(articles: list[dict[str, Any]], thresholds: dict[str, float] | None = None) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for index, left in enumerate(articles):
        for other_index in range(index + 1, len(articles)):
            result = compare_articles(left, articles[other_index], thresholds)
            if result["status"] != "passed":
                pairs.append({"left_index": index, "right_index": other_index, **result})
    return pairs


def compare_batch_report(articles: list[dict[str, Any]], thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    """Return every pair comparison, including pairs that passed."""
    pairs: list[dict[str, Any]] = []
    for index, left in enumerate(articles):
        for other_index in range(index + 1, len(articles)):
            result = compare_articles(left, articles[other_index], thresholds)
            pairs.append({"left_index": index, "right_index": other_index, **result})
    return {
        "total_pairs_checked": len(pairs),
        "pairs": pairs,
        "violating_pairs": [pair for pair in pairs if pair.get("status") != "passed"],
    }


def duplicate_pairs(texts: list[str], threshold: float = 0.65) -> list[tuple[int, int, float]]:
    pairs: list[tuple[int, int, float]] = []
    for index, left in enumerate(texts):
        for other_index in range(index + 1, len(texts)):
            score = similarity(left, texts[other_index])
            if score >= threshold:
                pairs.append((index, other_index, score))
    return pairs
