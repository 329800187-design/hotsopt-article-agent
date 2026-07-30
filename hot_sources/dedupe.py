from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from modules.models import HotTopic


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower().strip()
    text = re.sub(r"^\s*(?:热搜|热点|榜单)?\s*#?\d+\s*[.、:：\-]?\s*", "", text)
    text = re.sub(r"\s*[【\[(]?\s*(?:爆|热|新|沸|荐)\s*[】\])]?\s*$", "", text)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", text)


def deduplicate_topics(topics: list[HotTopic], threshold: float = 0.9) -> list[HotTopic]:
    result: list[HotTopic] = []
    for topic in topics:
        normalized = _normalize(topic.title)
        if not normalized:
            continue
        if any(SequenceMatcher(None, normalized, _normalize(existing.title)).ratio() >= threshold for existing in result):
            continue
        result.append(topic)
    return result
