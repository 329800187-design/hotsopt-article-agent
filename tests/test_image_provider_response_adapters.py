from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from providers.contracts import ImageGenerationRequest
from providers.text_provider import ProviderError
from providers.image_provider import OpenAIImageProvider, build_image_request_payload, normalize_endpoint_url


def _png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (96, 96), (32, 96, 160)).save(buffer, format="PNG")
    return buffer.getvalue()


class _Response:
    def __init__(self, status_code: int = 200, payload: dict[str, Any] | None = None, content: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self.headers = headers or {"content-type": "application/json"}

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, post_payload: dict[str, Any], get_content: bytes | None = None) -> None:
        self.post_payload = post_payload
        self.get_content = get_content or b""
        self.posted: list[dict[str, Any]] = []
        self.fetched: list[str] = []

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, url: str, headers: dict[str, str], json: dict[str, Any]) -> _Response:
        self.posted.append({"url": url, "headers": headers, "json": json})
        return _Response(payload=self.post_payload)

    def get(self, url: str) -> _Response:
        self.fetched.append(url)
        return _Response(content=self.get_content, headers={"content-type": "image/png"})


def _provider(monkeypatch: pytest.MonkeyPatch, post_payload: dict[str, Any], get_content: bytes | None = None) -> OpenAIImageProvider:
    client = _Client(post_payload, get_content)
    monkeypatch.setattr("providers.image_provider.create_http_client", lambda settings: client)
    provider = OpenAIImageProvider({"api_key": "test-key", "base_url": "https://image.example/v1", "endpoint": "/images/generations", "model": "image-model", "size": "1024x1024"})
    provider._client = client  # type: ignore[attr-defined]
    return provider


def test_openai_b64_json_response_is_written(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = {"data": [{"b64_json": base64.b64encode(_png_bytes()).decode("ascii")}]} 
    provider = _provider(monkeypatch, payload)
    output = provider.generate_image(ImageGenerationRequest("prompt", tmp_path / "out.png"))
    assert output.is_file()
    assert provider.last_response_type == "base64"


def test_raw_base64_image_response_is_written(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = {"data": [{"image_base64": base64.b64encode(_png_bytes()).decode("ascii")}]} 
    provider = _provider(monkeypatch, payload)
    output = provider.generate_image(ImageGenerationRequest("prompt", tmp_path / "out.png"))
    assert output.is_file()
    assert provider.last_response_type == "base64"


def test_data_uri_image_response_is_written(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    encoded = base64.b64encode(_png_bytes()).decode("ascii")
    provider = _provider(monkeypatch, {"data": [{"url": f"data:image/png;base64,{encoded}"}]})
    output = provider.generate_image(ImageGenerationRequest("prompt", tmp_path / "out.png"))
    assert output.is_file()
    assert provider.last_response_type == "data_uri"


def test_url_response_fetches_and_validates_image(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider = _provider(monkeypatch, {"data": [{"url": "https://cdn.example/image.png"}]}, get_content=_png_bytes())
    output = provider.generate_image(ImageGenerationRequest("prompt", tmp_path / "out.png"))
    assert output.is_file()
    assert provider.last_response_type == "url"
    assert provider._client.fetched == ["https://cdn.example/image.png"]  # type: ignore[attr-defined]


def test_dashscope_native_image_field_is_supported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    encoded = base64.b64encode(_png_bytes()).decode("ascii")
    payload = {"output": {"choices": [{"message": {"content": [{"image": f"data:image/png;base64,{encoded}"}]}}]}}
    client = _Client(payload)
    monkeypatch.setattr("providers.image_provider.create_http_client", lambda settings: client)
    provider = OpenAIImageProvider({"api_key": "test-key", "api_format": "dashscope_native", "base_url": "https://dashscope.example/api", "endpoint": "/services/aigc/multimodal-generation/generation", "model": "qwen-image", "size": "1024*1024"})
    output = provider.generate_image(ImageGenerationRequest("prompt", tmp_path / "out.png"))
    assert output.is_file()
    assert provider.last_response_type == "data_uri"


def test_html_or_json_bytes_are_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider = _provider(monkeypatch, {"data": [{"url": "https://cdn.example/error.png"}]}, get_content=b"{\"error\":\"not image\"}")
    with pytest.raises(ProviderError) as excinfo:
        provider.generate_image(ImageGenerationRequest("prompt", tmp_path / "out.png"))
    assert excinfo.value.code == "INVALID_RESPONSE"


def test_endpoint_normalization_does_not_duplicate_version_path() -> None:
    assert normalize_endpoint_url("https://image.example/v1", "/v1/images/generations") == "https://image.example/v1/images/generations"
    assert normalize_endpoint_url("https://image.example/api/v3", "/images/generations") == "https://image.example/api/v3/images/generations"


def test_request_adapter_controls_native_dashscope_payload() -> None:
    payload = build_image_request_payload(
        {
            "request_adapter": "dashscope_multimodal_generation",
            "model": "qwen-image",
            "size": "1024x1024",
        },
        "正文配图",
    )
    assert payload["input"]["messages"][0]["content"] == [{"text": "正文配图"}]
    assert payload["parameters"]["size"] == "1024*1024"
