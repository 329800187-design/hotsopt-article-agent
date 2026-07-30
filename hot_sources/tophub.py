from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone
from typing import Any

from hot_sources.base import HotProvider
from hot_sources.classifier import classify_topic
from modules.models import HotTopic
from modules.network import classify_network_error, create_http_client


class TopHubToutiaoSource(HotProvider):
    provider_name = "tophub_toutiao"
    display_name = "今日热榜 TopHub"
    endpoint = "https://tophub.today/n/x9ozB4KoXb"
    ROW_RE = re.compile(
        r"<tr>\s*<td[^>]*>\s*(?P<rank>\d+)\.\s*</td>\s*"
        r"<td>\s*<a\s+href=\"(?P<url>[^\"]+)\"[^>]*>(?P<title>.*?)</a>\s*</td>\s*"
        r"<td[^>]*class=\"ws\"[^>]*>(?P<hot>.*?)</td>",
        re.S | re.I,
    )

    def __init__(
        self,
        timeout_seconds: int = 20,
        network_settings: dict[str, Any] | None = None,
        *,
        endpoint: str | None = None,
        provider_name: str | None = None,
        display_name: str | None = None,
    ) -> None:
        super().__init__()
        self.timeout_seconds = timeout_seconds
        self.network_settings = network_settings or {}
        self.endpoint = str(endpoint or type(self).endpoint)
        self.provider_name = str(provider_name or type(self).provider_name)
        self.display_name = str(display_name or type(self).display_name)

    def health_check(self) -> dict[str, Any]:
        try:
            topics = self.fetch_trends()
            return {"ok": True, "count": len(topics), "provider_name": self.provider_name}
        except Exception as exc:
            detail = classify_network_error(exc)
            return {"ok": False, "error": detail["message"], "error_type": detail["category"], "retryable": detail["retryable"], "provider_name": self.provider_name}

    def normalize_item(self, item: dict[str, Any], index: int, captured_at: str) -> HotTopic | None:
        title = re.sub(r"<.*?>", "", html.unescape(str(item.get("title") or ""))).strip()
        if not title:
            return None
        url = html.unescape(str(item.get("url") or self.endpoint)).strip()
        hot_value = re.sub(r"\s+", "", html.unescape(str(item.get("hot") or ""))).strip()
        rank = int(item.get("rank") or index)
        identifier = hashlib.sha1(f"{self.provider_name}:{title}".encode("utf-8")).hexdigest()[:16]
        return HotTopic(
            id=identifier,
            source=self.provider_name,
            source_name=self.display_name,
            title=title,
            category=classify_topic(title, "", ""),
            rank=rank,
            hot_value=hot_value,
            source_url=url,
            summary=f"{self.display_name}第 {rank} 位，热度 {hot_value}。",
            captured_at=captured_at,
            raw_data=item,
        )

    def fetch_trends(self) -> list[HotTopic]:
        with create_http_client({**self.network_settings, "timeout_seconds": self.timeout_seconds}) as client:
            response = client.get(self.endpoint, headers={"User-Agent": "Mozilla/5.0 hotspot-article-agent/0.1", "Accept": "text/html"})
            response.raise_for_status()
            text = response.text
        captured_at = datetime.now(timezone.utc).isoformat()
        topics: list[HotTopic] = []
        for index, match in enumerate(self.ROW_RE.finditer(text), start=1):
            topic = self.normalize_item(match.groupdict(), index, captured_at)
            if topic:
                topics.append(topic)
        if not topics:
            raise ValueError("TopHub 页面返回成功，但没有识别到热榜条目")
        self.last_success_at = captured_at
        self.last_error = None
        return topics
