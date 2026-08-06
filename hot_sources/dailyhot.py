from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import httpx

from hot_sources.base import HotProvider
from hot_sources.classifier import classify_topic
from modules.models import HotTopic
from modules.network import create_http_client, classify_network_error


class DailyHotSource(HotProvider):
    provider_name = "toutiao"
    display_name = "今日头条主源"

    def __init__(self, endpoint: str, timeout_seconds: int = 20, network_settings: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.endpoint = endpoint.rstrip("/")
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
        for key in ("data", "items", "list", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = DailyHotSource._items(value)
                if nested:
                    return nested
        return []

    def normalize_item(self, item: dict[str, Any], index: int, collected_at: str) -> HotTopic | None:
        title = str(item.get("title") or item.get("name") or item.get("word") or "").strip()
        if not title:
            return None
        url = str(item.get("url") or item.get("link") or item.get("mobileUrl") or "")
        identifier = hashlib.sha1(f"toutiao:{title}".encode("utf-8")).hexdigest()[:16]
        return HotTopic(id=identifier, source="toutiao", source_name=self.display_name, title=title, category=classify_topic(title, str(item.get("desc") or item.get("description") or ""), str(item.get("category") or "")), rank=int(item.get("index") or item.get("rank") or index), hot_value=str(item.get("hot") or item.get("hotValue") or item.get("heat") or ""), source_url=url, summary=str(item.get("desc") or item.get("description") or ""), captured_at=collected_at, raw_data=item)

    def fetch_trends(self) -> list[HotTopic]:
        with create_http_client({**self.network_settings, "timeout_seconds": self.timeout_seconds}) as client:
            response = client.get(self.endpoint, headers={"User-Agent": "Mozilla/5.0 hotspot-article-agent/0.1", "Accept": "application/json"})
            self.last_http_status = response.status_code
            self.last_content_type = str(response.headers.get("content-type") or "")
            response.raise_for_status()
            payload = response.json()
        raw_items = self._items(payload)
        self.last_raw_item_count = len(raw_items)
        topics: list[HotTopic] = []
        collected_at = datetime.now(timezone.utc).isoformat()
        for index, item in enumerate(raw_items, start=1):
            topic = self.normalize_item(item, index, collected_at)
            if topic:
                topics.append(topic)
        if not topics:
            raise ValueError("DailyHotApi 返回成功，但没有识别到热榜条目")
        self.last_success_at = collected_at
        self.last_error = None
        return topics
