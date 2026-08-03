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
            existing = next(existing for existing in result if SequenceMatcher(None, normalized, _normalize(existing.title)).ratio() >= threshold)
            existing_raw = dict(existing.raw_data or {})
            incoming_raw = dict(topic.raw_data or {})
            platforms = list(dict.fromkeys(list(existing_raw.get("aggregated_platforms") or []) + list(incoming_raw.get("aggregated_platforms") or []) + [str(incoming_raw.get("source_platform") or "").strip()]))
            platforms = [item for item in platforms if item]
            existing_raw.update({"aggregated_platforms": platforms, "source_count": len(platforms)})
            existing.raw_data = existing_raw
            continue
        result.append(topic)
    return result
