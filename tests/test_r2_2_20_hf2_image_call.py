from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
from PIL import Image

from providers.image_provider import OpenAIImageProvider, build_image_request_payload


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (96, 96), (40, 120, 80)).save(output, format="PNG")
    return output.getvalue()


class _Response:
    status_code = 200
    headers = {"x-request-id": "req-hf2-test"}
    text = ""

    def json(self):
        return {"data": [{"b64_json": base64.b64encode(_png()).decode("ascii")}]}

    def raise_for_status(self):
        return None


class _Client:
    def __init__(self):
        self.payloads = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def post(self, _url, *, headers, json):
        self.payloads.append((headers, json))
        return _Response()


def test_hf2_openai_compatible_payload_is_minimal_and_preserves_model(monkeypatch, tmp_path: Path):
    client = _Client()
    import providers.image_provider as module
    monkeypatch.setattr(module, "create_http_client", lambda _settings: client)
    profile = {"api_key": "secret", "base_url": "https://proxy.example/v1", "endpoint": "/images/generations", "model": "gpt-image-2"}
    assert set(build_image_request_payload(profile, "一只白猫")) == {"model", "prompt"}
    result = OpenAIImageProvider(profile).test_connection(tmp_path / "one.png")
    assert result.success is True
    assert result.http_status == 200
    assert client.payloads[0][1]["model"] == "gpt-image-2"
    assert set(client.payloads[0][1]) == {"model", "prompt"}
    assert (tmp_path / "one.png").is_file()


def test_hf2_three_calls_return_three_local_images(monkeypatch, tmp_path: Path):
    client = _Client()
    import providers.image_provider as module
    monkeypatch.setattr(module, "create_http_client", lambda _settings: client)
    profile = {"api_key": "secret", "base_url": "https://proxy.example/v1", "endpoint": "/images/generations", "model": "gpt-image-2"}
    provider = OpenAIImageProvider(profile)
    for index in range(3):
        provider.generate(f"第 {index + 1} 张配图", tmp_path / f"{index}.png")
    assert len(client.payloads) == 3
    assert all(path.is_file() for path in tmp_path.glob("*.png"))


def test_hf2_empty_prompt_is_rejected_before_http(monkeypatch, tmp_path: Path):
    client = _Client()
    import providers.image_provider as module
    monkeypatch.setattr(module, "create_http_client", lambda _settings: client)
    profile = {"api_key": "secret", "base_url": "https://proxy.example/v1", "model": "gpt-image-2"}
    with pytest.raises(Exception, match="图片提示词为空"):
        OpenAIImageProvider(profile).generate("", tmp_path / "none.png")
    assert client.payloads == []
