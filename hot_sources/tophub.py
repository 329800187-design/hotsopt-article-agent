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
            self.last_http_status = response.status_code
            self.last_content_type = str(response.headers.get("content-type") or "")
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
        self.last_raw_item_count = len(topics)
        self.last_success_at = captured_at
        self.last_error = None
        return topics


class TopHubOverviewSource(HotProvider):
    """Read multiple independent boards embedded in TopHub's server-rendered overview."""

    provider_name = "tophub_overview"
    display_name = "今日热榜 多平台聚合"
    endpoint = "https://tophub.today/"
    CARD_SPLIT_RE = re.compile(r'<div class="cc-cd"[^>]*id="node-[^"]+">', re.I)
    BOARD_RE = re.compile(
        r'<a href="/n/(?P<board_id>[A-Za-z0-9]+)">.*?<div class="cc-cd-lb">.*?<span>\s*(?P<platform>.*?)\s*</span>.*?'
        r'<span class="cc-cd-sb-st">\s*(?P<board>.*?)\s*</span>',
        re.S | re.I,
    )
    ITEM_RE = re.compile(
        r'<a href="(?P<url>https?://[^"]+)"[^>]*>.*?'
        r'<span class="s[^"]*">\s*(?P<rank>\d+)\s*</span>\s*'
        r'<span class="t">(?P<title>.*?)</span>\s*'
        r'<span class="e">(?P<hot>.*?)</span>',
        re.S | re.I,
    )

    def __init__(
        self,
        timeout_seconds: int = 20,
        network_settings: dict[str, Any] | None = None,
        *,
        per_board_limit: int = 20,
        total_limit: int = 500,
    ) -> None:
        super().__init__()
        self.timeout_seconds = timeout_seconds
        self.network_settings = network_settings or {}
        self.per_board_limit = max(1, min(50, int(per_board_limit)))
        self.total_limit = max(1, min(1000, int(total_limit)))
        self.board_counts: dict[str, int] = {}

    @staticmethod
    def _clean(value: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"<.*?>", "", html.unescape(str(value or "")))).strip()

    def health_check(self) -> dict[str, Any]:
        try:
            topics = self.fetch_trends()
            return {"ok": True, "count": len(topics), "provider_name": self.provider_name}
        except Exception as exc:
            detail = classify_network_error(exc)
            return {"ok": False, "error": detail["message"], "error_type": detail["category"], "retryable": detail["retryable"], "provider_name": self.provider_name}

    def normalize_item(self, item: dict[str, Any], index: int, captured_at: str) -> HotTopic | None:
        title = self._clean(str(item.get("title") or ""))
        url = str(item.get("url") or "").strip()
        board_id = str(item.get("board_id") or "").strip()
        if not title or not url or not board_id:
            return None
        source_name = self._clean(str(item.get("source_name") or self.display_name))
        identifier = hashlib.sha1(f"tophub:{board_id}:{title}".encode("utf-8")).hexdigest()[:16]
        return HotTopic(
            id=identifier,
            source=f"tophub:{board_id}",
            source_name=source_name,
            title=title,
            category=classify_topic(title, "", ""),
            rank=int(item.get("rank") or index),
            hot_value=self._clean(str(item.get("hot") or "")),
            source_url=url,
            summary=f"{source_name}第 {int(item.get('rank') or index)} 位。",
            captured_at=captured_at,
            raw_data=item,
        )

    def fetch_trends(self) -> list[HotTopic]:
        with create_http_client({**self.network_settings, "timeout_seconds": self.timeout_seconds}) as client:
            response = client.get(self.endpoint, headers={"User-Agent": "Mozilla/5.0 hotspot-article-agent/0.1", "Accept": "text/html"})
            self.last_http_status = response.status_code
            self.last_content_type = str(response.headers.get("content-type") or "")
            response.raise_for_status()
            page = response.text
        captured_at = datetime.now(timezone.utc).isoformat()
        topics: list[HotTopic] = []
        self.board_counts = {}
        for chunk in self.CARD_SPLIT_RE.split(page)[1:]:
            board_match = self.BOARD_RE.search(chunk)
            if not board_match:
                continue
            board_id = board_match.group("board_id")
            platform = self._clean(board_match.group("platform"))
            board = self._clean(board_match.group("board"))
            source_name = " ".join(item for item in (platform, board) if item) or self.display_name
            count = 0
            for item in self.ITEM_RE.finditer(chunk):
                title = self._clean(item.group("title"))
                url = html.unescape(item.group("url")).strip()
                if not title or not url:
                    continue
                rank = int(item.group("rank"))
                hot_value = self._clean(item.group("hot"))
                identifier = hashlib.sha1(f"tophub:{board_id}:{title}".encode("utf-8")).hexdigest()[:16]
                topics.append(
                    HotTopic(
                        id=identifier,
                        source=f"tophub:{board_id}",
                        source_name=source_name,
                        title=title,
                        category=classify_topic(title, "", ""),
                        rank=rank,
                        hot_value=hot_value,
                        source_url=url,
                        summary=f"{source_name}第 {rank} 位" + (f"，热度 {hot_value}" if hot_value else "") + "。",
                        captured_at=captured_at,
                        raw_data={"board_id": board_id, "platform": platform, "board": board},
                    )
                )
                count += 1
                if count >= self.per_board_limit or len(topics) >= self.total_limit:
                    break
            if count:
                self.board_counts[board_id] = count
            if len(topics) >= self.total_limit:
                break
        if not topics:
            raise ValueError("TopHub 首页返回成功，但没有识别到多平台榜单条目")
        self.last_raw_item_count = len(topics)
        self.last_success_at = captured_at
        self.last_error = None
        return topics
