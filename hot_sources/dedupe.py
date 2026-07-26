from __future__ import annotations

import re
from difflib import SequenceMatcher

from modules.models import HotTopic


def _normalize(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", (value or "").lower())


def deduplicate_topics(topics: list[HotTopic], threshold: float = 0.86) -> list[HotTopic]:
    result: list[HotTopic] = []
    for topic in topics:
        normalized = _normalize(topic.title)
        if not normalized:
            continue
        if any(SequenceMatcher(None, normalized, _normalize(existing.title)).ratio() >= threshold for existing in result):
            continue
        result.append(topic)
    return result
