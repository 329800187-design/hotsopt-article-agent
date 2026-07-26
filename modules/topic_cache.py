from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules.models import HotTopic, utc_now
from modules.security import sanitize_json
from modules.app_paths import cache_path


CACHE_PATH = cache_path()


class TopicCacheStore:
    def __init__(self, path: Path = CACHE_PATH, environment: str = "production") -> None:
        self.path = Path(path)
        self.environment = environment

    def save(self, topics: list[HotTopic], source_name: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = sanitize_json({"environment": self.environment, "saved_at": utc_now(), "source": source_name, "topics": [topic.to_dict() for topic in topics]})
        temporary_handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp", delete=False)
        temporary_path = Path(temporary_handle.name)
        try:
            with temporary_handle as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.replace(self.path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    def get_info(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("environment") != self.environment:
                return {}
            return value
        except (OSError, ValueError, TypeError):
            return {}

    def load(self) -> list[HotTopic]:
        value = self.get_info()
        topics = value.get("topics", [])
        return [HotTopic.from_dict(item) for item in topics if isinstance(item, dict)]

    def get_age_seconds(self) -> float | None:
        saved_at = self.get_info().get("saved_at")
        if not saved_at:
            return None
        try:
            saved = datetime.fromisoformat(str(saved_at).replace("Z", "+00:00")).astimezone(timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - saved).total_seconds())
        except (TypeError, ValueError):
            return None


def get_default_cache_store() -> TopicCacheStore:
    return TopicCacheStore()


def save_topics(topics: list[HotTopic], source_name: str = "未知") -> None:
    get_default_cache_store().save(topics, source_name)


def load_topics() -> list[HotTopic]:
    return get_default_cache_store().load()


def load_cache_info() -> dict[str, Any]:
    return get_default_cache_store().get_info()


def cache_age_seconds() -> float | None:
    return get_default_cache_store().get_age_seconds()
