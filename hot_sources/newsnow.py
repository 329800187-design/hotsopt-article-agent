from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import httpx

from hot_sources.base import HotProvider
from hot_sources.classifier import classify_topic
from modules.models import HotTopic
from modules.network import create_http_client, classify_network_error


class NewsNowSource(HotProvider):
    provider_name = "newsnow_toutiao"
    display_name = "NewsNow 今日头条备用源"

    def __init__(self, base_url: str = "https://newsnow.busiyi.world", source_id: str = "toutiao", timeout_seconds: int = 12, network_settings: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.url = f"{base_url.rstrip('/')}/api/sid={source_id}&latest"
        self.timeout_seconds = timeout_seconds
        self.network_settings = network_settings or {}

    def health_check(self) -> dict[str, Any]:
        try:
            topics = self.fetch_trends()
            return {"ok": True, "count": len(topics), "provider_name": self.provider_name}
        except Exception as exc:
            detail = classify_network_error(exc)
            return {"ok": False, "error": detail["message"], "error_type": detail["category"], "retryable": detail["retryable"], "provider_name": self.provider_name}

    @staticmethod
    def _items(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("items", "data", "news", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = NewsNowSource._items(value)
                if nested:
                    return nested
        return []

    def normalize_item(self, item: dict[str, Any], index: int, captured_at: str) -> HotTopic | None:
        title = str(item.get("title") or item.get("name") or "").strip()
        if not title:
            return None
        url = str(item.get("url") or item.get("link") or "")
        identifier = hashlib.sha1(f"newsnow:{title}".encode("utf-8")).hexdigest()[:16]
        summary = str(item.get("desc") or item.get("description") or "")
        hot_value = str(item.get("hot") or item.get("hotValue") or item.get("heat") or item.get("score") or "")
        return HotTopic(id=identifier, source="newsnow:toutiao", source_name=self.display_name, title=title, category=classify_topic(title, summary), rank=index, hot_value=hot_value, source_url=url, summary=summary, captured_at=captured_at, raw_data=item)

    def fetch_trends(self) -> list[HotTopic]:
        with create_http_client({**self.network_settings, "timeout_seconds": self.timeout_seconds}) as client:
            response = client.get(self.url, headers={"User-Agent": "Mozilla/5.0 hotspot-article-agent/0.1", "Accept": "application/json"})
            self.last_http_status = response.status_code
            self.last_content_type = str(response.headers.get("content-type") or "")
            response.raise_for_status()
            payload = response.json()
        raw_items = self._items(payload)
        self.last_raw_item_count = len(raw_items)
        collected_at = datetime.now(timezone.utc).isoformat()
        topics: list[HotTopic] = []
        for index, item in enumerate(raw_items, start=1):
            topic = self.normalize_item(item, index, collected_at)
            if topic:
                topics.append(topic)
        if not topics:
            raise ValueError("NewsNow 返回成功，但没有识别到今日头条条目")
        self.last_success_at = collected_at
        self.last_error = None
        return topics
