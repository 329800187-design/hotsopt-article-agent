from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class HotTopic:
    id: str
    title: str
    hot_value: str | None = None
    hot_score: float | None = None
    rank: int | None = None
    category: str = "综合热点"
    summary: str = ""
    source: str = "unknown"
    source_name: str = "未知来源"
    source_url: str = ""
    captured_at: str = field(default_factory=utc_now)
    provider_status: str = "online"
    is_cached: bool = False
    raw_data: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @property
    def url(self) -> str:
        return self.source_url

    @property
    def collected_at(self) -> str:
        return self.captured_at

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        raw = value.get("raw_data") if isinstance(value.get("raw_data"), dict) else {}
        value["source_platform"] = str(raw.get("source_platform") or value.get("source_name") or "其他来源")
        value["acquisition_channel"] = str(raw.get("acquisition_channel") or "其他渠道")
        value["aggregated_platforms"] = list(raw.get("aggregated_platforms") or [value["source_platform"]])
        value["source_count"] = int(raw.get("source_count") or len(value["aggregated_platforms"]))
        value["platform_rank"] = int(raw.get("platform_rank") or value.get("rank") or 0)
        value["url"] = self.source_url
        value["collected_at"] = self.captured_at
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "HotTopic":
        data = dict(value)
        if not data.get("id"):
            raise ValueError("TOPIC_SNAPSHOT_MISSING_ID")
        data["source_url"] = data.get("source_url", data.pop("url", ""))
        data["captured_at"] = data.get("captured_at", data.pop("collected_at", utc_now()))
        data.setdefault("source_name", data.get("source", "未知来源"))
        data.setdefault("provider_status", "online")
        data["is_cached"] = bool(data.get("is_cached", False))
        data.setdefault("raw_data", {})
        data.setdefault("created_at", data["captured_at"])
        data.setdefault("updated_at", data["captured_at"])
        data.setdefault("category", "综合热点")
        data.pop("url", None)
        data.pop("collected_at", None)
        allowed = {field_name for field_name in cls.__dataclass_fields__}
        return cls(**{key: item for key, item in data.items() if key in allowed})


@dataclass
class ImageAsset:
    role: str
    paragraph_ref: str | None
    prompt: str
    path: str = ""
    status: str = "pending"
    error: str | None = None
    image_id: str = ""
    order: int = 0
    section_title: str = ""
    insert_after_paragraph: int | None = None
    purpose: str = ""
    error_code: str = ""
    attempt_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    fallback_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ArticleTask:
    id: str
    topic: str
    angle: str
    article_type: str
    style: str
    structure: str
    word_count: int
    title: str = ""
    intro: str = ""
    sections: list[dict[str, Any]] = field(default_factory=list)
    content_markdown: str = ""
    tags: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    images: list[ImageAsset] = field(default_factory=list)
    docx_path: str = ""
    status: str = "pending"
    error: str | None = None
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["images"] = [image.to_dict() for image in self.images]
        return value


def article_from_dict(value: dict[str, Any]) -> ArticleTask:
    images = [ImageAsset(**item) for item in value.get("images", [])]
    data = dict(value)
    data["images"] = images
    return ArticleTask(**data)
