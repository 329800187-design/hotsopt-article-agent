from __future__ import annotations

import io
import httpx
from PIL import Image

from providers.image_provider import OpenAIImageProvider
from providers.errors import map_provider_exception


def test_html_404_image_endpoint_is_not_supported(monkeypatch, tmp_path):
    class FakeResponse:
        status_code = 404
        headers = {"content-type": "text/html; charset=utf-8"}

        def json(self):
            raise ValueError("not json")

        def raise_for_status(self):
            return None

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, *args, **kwargs):
            return FakeResponse()

    import providers.image_provider as image_provider

    monkeypatch.setattr(image_provider, "create_http_client", lambda settings: FakeClient())
    result = OpenAIImageProvider(
        {
            "api_key": "test-key",
            "base_url": "https://mimo.example/v1",
            "endpoint": "/images/generations",
            "model": "mimo-v2.5",
            "auth_type": "bearer",
        }
    ).test_connection(tmp_path / "test.png")
    assert result.success is False
    assert result.error_code == "IMAGE_GENERATION_NOT_SUPPORTED"
    assert result.retryable is False


def test_json_model_not_found_error_is_not_retryable():
    request = httpx.Request("POST", "https://example.invalid/v1/images/generations")
    response = httpx.Response(
        503,
        request=request,
        json={"error": {"code": "model_not_found", "message": "No available channel for model test"}},
    )
    mapped = map_provider_exception(
        httpx.HTTPStatusError("provider failure", request=request, response=response), response
    )
    assert mapped.code == "MODEL_NOT_FOUND"


def test_dashscope_native_image_response_is_downloaded(monkeypatch, tmp_path):
    buffer = io.BytesIO()
    Image.new("RGB", (128, 128), (30, 90, 160)).save(buffer, format="PNG")
    image_bytes = buffer.getvalue()

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"output": {"choices": [{"message": {"content": [{"image": "https://example.invalid/generated.png"}]}}]}}

        def raise_for_status(self):
            return None

    class FakeImageResponse:
        def __init__(self):
            self.content = image_bytes

        def raise_for_status(self):
            return None

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, *args, **kwargs):
            assert kwargs["json"]["input"]["messages"][0]["content"][0]["text"]
            return FakeResponse()

        def get(self, *args, **kwargs):
            return FakeImageResponse()

    import providers.image_provider as image_provider

    monkeypatch.setattr(image_provider, "create_http_client", lambda settings: FakeClient())
    profile = {
        "api_key": "test-key",
        "base_url": "https://example.invalid",
        "endpoint": "/api/v1/services/aigc/multimodal-generation/generation",
        "model": "qwen-image-2.0-pro",
        "auth_type": "bearer",
        "api_format": "dashscope_native",
        "size": "1024*1024",
    }
    result = OpenAIImageProvider(profile).test_connection(tmp_path / "native.png")
    assert result.success is True
    assert result.image_response_type == "url"
    assert result.details["width"] == 128
