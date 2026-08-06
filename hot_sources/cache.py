from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from hot_sources.base import HotProvider
from modules.models import HotTopic
from modules.topic_cache import TopicCacheStore, get_default_cache_store
from modules.network import classify_network_error


class LocalCacheProvider(HotProvider):
    provider_name = "local_cache"
    display_name = "本地缓存"

    def __init__(self, cache_store: TopicCacheStore | None = None) -> None:
        super().__init__()
        self.cache_store = cache_store or get_default_cache_store()

    def health_check(self) -> dict[str, Any]:
        try:
            topics = self.fetch_trends()
            age = self.cache_store.get_age_seconds()
            return {"ok": bool(topics), "count": len(topics), "age_seconds": age, "provider_name": self.provider_name}
        except Exception as exc:
            detail = classify_network_error(exc)
            return {"ok": False, "error": detail["message"], "error_type": detail["category"], "retryable": detail["retryable"], "provider_name": self.provider_name}

    def normalize_item(self, item: dict[str, Any], index: int, captured_at: str) -> HotTopic | None:
        return HotTopic.from_dict(item)

    def fetch_trends(self) -> list[HotTopic]:
        topics = self.cache_store.load()
        if not topics:
            raise RuntimeError("本地缓存不存在或为空")
        cache_info = self.cache_store.get_info()
        cached_at = str(cache_info.get("saved_at") or datetime.now(timezone.utc).isoformat())
        result: list[HotTopic] = []
        for topic in topics:
            topic.provider_status = "cached"
            topic.is_cached = True
            topic.captured_at = cached_at if not topic.captured_at else topic.captured_at
            result.append(topic)
        self.last_success_at = cached_at
        self.last_error = None
        return result
