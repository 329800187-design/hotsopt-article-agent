from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from hot_sources.classifier import CATEGORIES
from modules.models import HotTopic


class HotProvider(ABC):
    provider_name = "unknown"
    display_name = "未知来源"

    def __init__(self) -> None:
        self.last_success_at: str | None = None
        self.last_error: str | None = None

    @property
    def name(self) -> str:
        return self.display_name

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def fetch_trends(self) -> list[HotTopic]:
        raise NotImplementedError

    @abstractmethod
    def normalize_item(self, item: dict[str, Any], index: int, captured_at: str) -> HotTopic | None:
        raise NotImplementedError

    def supports_category(self, category: str) -> bool:
        return category in CATEGORIES

    def fetch(self) -> list[HotTopic]:
        return self.fetch_trends()


HotSource = HotProvider
