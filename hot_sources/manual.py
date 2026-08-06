from __future__ import annotations

import hashlib

from hot_sources.base import HotSource
from hot_sources.classifier import classify_topic
from modules.models import HotTopic


class ManualHotSource(HotSource):
    provider_name = "manual"
    display_name = "手动输入"

    def __init__(self, title: str, summary: str = "", source_url: str = "") -> None:
        super().__init__()
        self.title = title.strip()
        self.summary = summary.strip()
        self.source_url = source_url.strip()

    def health_check(self) -> dict[str, object]:
        return {"ok": bool(self.title), "provider_name": self.provider_name}

    def normalize_item(self, item: dict[str, object], index: int, captured_at: str) -> HotTopic | None:
        return self.fetch_trends()[0] if self.title else None

    def fetch(self) -> list[HotTopic]:
        return self.fetch_trends()

    def fetch_trends(self) -> list[HotTopic]:
        if not self.title:
            return []
        identifier = hashlib.sha1(f"manual:{self.title}:{self.source_url}".encode("utf-8")).hexdigest()[:16]
        return [
            HotTopic(
                id=identifier,
                source=self.provider_name,
                source_name=self.display_name,
                title=self.title,
                category=classify_topic(self.title, self.summary),
                summary=self.summary,
                source_url=self.source_url,
                provider_status="manual",
            )
        ]
