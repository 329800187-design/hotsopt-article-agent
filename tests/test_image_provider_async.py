from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from providers.contracts import ImageGenerationRequest
from providers.image_provider import OpenAIImageProvider
from providers.text_provider import ProviderError


def _png_b64() -> str:
    buffer = BytesIO()
    Image.new("RGB", (96, 96), (20, 80, 140)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class _Response:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(str(self.status_code))


class _Client:
    def __init__(self, submit: dict[str, Any], polls: list[dict[str, Any]]) -> None:
        self.submit = submit
        self.polls = list(polls)
        self.posts = 0
        self.gets = 0

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, url: str, headers: dict[str, str], json: dict[str, Any]) -> _Response:
        self.posts += 1
        return _Response(self.submit)

    def get(self, url: str, headers: dict[str, str] | None = None) -> _Response:
        self.gets += 1
        return _Response(self.polls.pop(0))


def _provider(monkeypatch: pytest.MonkeyPatch, client: _Client) -> OpenAIImageProvider:
    monkeypatch.setattr("providers.image_provider.create_http_client", lambda settings: client)
    return OpenAIImageProvider(
        {
            "api_key": "test",
            "base_url": "https://image.example/v1",
            "endpoint": "/images/tasks",
            "poll_endpoint": "/images/tasks/{task_id}",
            "model": "image-model",
            "request_adapter": "openai_images_generations",
            "sync_or_async": "async",
            "poll_interval_seconds": 0,
            "max_poll_attempts": 4,
        }
    )


def test_async_task_is_submitted_once_polled_and_written(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _Client(
        {"request_id": "req-1", "status": "pending"},
        [{"request_id": "req-1", "status": "running"}, {"request_id": "req-1", "status": "completed", "data": [{"b64_json": _png_b64()}]}],
    )
    provider = _provider(monkeypatch, client)
    output = provider.generate_image(ImageGenerationRequest("prompt", tmp_path / "async.png"))
    assert output.is_file()
    assert client.posts == 1
    assert client.gets == 2


def test_async_task_supports_job_id_and_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _Client({"job_id": "job-1", "state": "queued"}, [{"job_id": "job-1", "state": "failed", "message": "quota"}])
    provider = _provider(monkeypatch, client)
    with pytest.raises(ProviderError) as excinfo:
        provider.generate_image(ImageGenerationRequest("prompt", tmp_path / "failed.png"))
    assert excinfo.value.code == "IMAGE_TASK_FAILED"
    assert client.posts == 1


def test_async_task_obeys_user_cancellation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _Client({"task_id": "task-1", "status": "pending"}, [])
    provider = _provider(monkeypatch, client)
    task = provider.submit_image_task(ImageGenerationRequest("prompt", tmp_path / "cancel.png"))
    with pytest.raises(ProviderError) as excinfo:
        provider.poll_image_task(task, cancel_check=lambda: True)
    assert excinfo.value.code == "TASK_CANCELLED"
    assert client.posts == 1
    assert client.gets == 0
