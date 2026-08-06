from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class ModelTestResult:
    success: bool
    provider: str
    model: str
    http_status: int | None = None
    elapsed_ms: int = 0
    response_format: str = "unknown"
    supports_json: bool = False
    image_response_type: str = ""
    error_code: str = ""
    error_message: str = ""
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "provider": self.provider,
            "model": self.model,
            "http_status": self.http_status,
            "elapsed_ms": self.elapsed_ms,
            "response_format": self.response_format,
            "supports_json": self.supports_json,
            "image_response_type": self.image_response_type,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "retryable": self.retryable,
            "details": self.details,
        }


@dataclass
class ArticleGenerationRequest:
    prompt: str
    temperature: float = 0.75
    max_tokens: int = 3000
    response_format: str = "json_object"


@dataclass
class ImageGenerationRequest:
    prompt: str
    output_path: Path


class TextModelProvider(Protocol):
    def test_connection(self) -> ModelTestResult: ...

    def generate_article(self, request: ArticleGenerationRequest) -> str: ...


class ImageModelProvider(Protocol):
    def test_connection(self, output_path: Path) -> ModelTestResult: ...

    def generate_image(self, request: ImageGenerationRequest) -> Path: ...
