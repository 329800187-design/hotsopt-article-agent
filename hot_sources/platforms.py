from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


PLATFORM_ALIASES = {
    "微博": "微博",
    "weibo": "微博",
    "百度": "百度热搜",
    "百度热搜": "百度热搜",
    "知乎": "知乎",
    "抖音": "抖音",
    "哔哩哔哩": "哔哩哔哩",
    "bilibili": "哔哩哔哩",
    "今日头条": "今日头条",
    "头条": "今日头条",
}


def _canonical(value: Any) -> str:
    text = str(value or "").strip()
    for key, platform in PLATFORM_ALIASES.items():
        if key.lower() in text.lower():
            return platform
    return ""


def normalize_topic_platform(topic: Any, provider: Any) -> Any:
    """Keep the original platform separate from the acquisition channel."""
    raw = dict(getattr(topic, "raw_data", {}) or {})
    provider_name = str(getattr(provider, "provider_name", "") or "")
    display_name = str(getattr(provider, "display_name", "") or "")
    platform = (
        _canonical(raw.get("source_platform"))
        or _canonical(raw.get("platform"))
        or _canonical(raw.get("board"))
        or _canonical(display_name)
        or _canonical(provider_name)
    )
    if not platform:
        host = urlparse(str(getattr(topic, "source_url", "") or "")).netloc.lower()
        platform = {"weibo.com": "微博", "zhihu.com": "知乎", "douyin.com": "抖音", "bilibili.com": "哔哩哔哩"}.get(host, "其他来源")
    acquisition = "今日热榜" if provider_name.startswith("tophub") else ("直接接口" if provider_name.endswith("official") or provider_name in {"toutiao", "newsnow_toutiao"} else display_name or "其他渠道")
    existing = raw.get("aggregated_platforms") if isinstance(raw.get("aggregated_platforms"), list) else []
    platforms = list(dict.fromkeys([_canonical(item) or str(item).strip() for item in existing if str(item).strip()] + [platform]))
    raw.update({
        "source_platform": platform,
        "acquisition_channel": acquisition,
        "aggregated_platforms": platforms,
        "source_count": len(platforms),
        "platform_rank": int(getattr(topic, "rank", 0) or 0),
    })
    topic.raw_data = raw
    topic.source_name = platform
    return topic
