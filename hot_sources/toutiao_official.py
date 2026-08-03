from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import httpx

from hot_sources.base import HotProvider
from hot_sources.classifier import classify_topic
from modules.models import HotTopic
from modules.network import create_http_client, classify_network_error


class ToutiaoOfficialSource(HotProvider):
    provider_name = "toutiao_official"
    display_name = "今日头条官方热榜"

    # The public homepage calls this JSON endpoint with a query separator. The
    # old path without '?' now returns 404.
    def __init__(self, url: str = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc", timeout_seconds: int = 15, network_settings: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.url = url
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
    def _items(page: str) -> list[dict[str, Any]]:
        decoder = json.JSONDecoder()
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        position = 0
        while True:
            marker = page.find('"HotValue"', position)
            if marker < 0:
                break
            start = page.rfind("{", 0, marker)
            found: dict[str, Any] | None = None
            while start >= 0:
                try:
                    value, _ = decoder.raw_decode(page[start:])
                    if isinstance(value, dict) and (value.get("QueryWord") or value.get("Title")):
                        found = value
                        break
                except json.JSONDecodeError:
                    pass
                start = page.rfind("{", 0, start)
            if found:
                title = str(found.get("QueryWord") or found.get("Title") or "").strip()
                if title and title not in seen:
                    seen.add(title)
                    items.append(found)
            position = marker + len('"HotValue"')
        return items

    def normalize_item(self, item: dict[str, Any], index: int, captured_at: str) -> HotTopic | None:
        title = str(item.get("QueryWord") or item.get("Title") or item.get("title") or "").strip()
        if not title:
            return None
        cluster_id = str(item.get("ClusterIdStr") or item.get("ClusterId") or "")
        source_url = f"https://www.toutiao.com/trending/{cluster_id}/" if cluster_id else self.url
        identifier = hashlib.sha1(f"toutiao_official:{title}".encode("utf-8")).hexdigest()[:16]
        summary = str(item.get("Abstract") or item.get("Summary") or "")
        return HotTopic(id=identifier, title=title, hot_value=str(item.get("HotValue") or ""), rank=index, category=classify_topic(title, summary), summary=summary, source="toutiao_official", source_name=self.display_name, source_url=source_url, captured_at=captured_at, raw_data=item)

    def fetch_trends(self) -> list[HotTopic]:
        with create_http_client({**self.network_settings, "timeout_seconds": self.timeout_seconds}) as client:
            response = client.get(self.url, headers={"User-Agent": "Mozilla/5.0 hotspot-article-agent/0.1", "Accept": "text/html"})
            self.last_http_status = response.status_code
            self.last_content_type = str(response.headers.get("content-type") or "")
            response.raise_for_status()
            page = response.text
        captured_at = datetime.now(timezone.utc).isoformat()
        topics: list[HotTopic] = []
        raw_items = self._items(page)
        self.last_raw_item_count = len(raw_items)
        for index, item in enumerate(raw_items, start=1):
            topic = self.normalize_item(item, index, captured_at)
            if topic:
                topics.append(topic)
        if not topics:
            raise ValueError("今日头条官方页面没有识别到热榜条目")
        self.last_success_at = captured_at
        self.last_error = None
        return topics
