from __future__ import annotations

import base64
import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from providers.contracts import ImageGenerationRequest
from providers.image_provider import OpenAIImageProvider
from providers.text_provider import ProviderError


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (96, 96), (120, 80, 40)).save(output, format="PNG")
    return output.getvalue()


class _Response:
    def __init__(self, status_code: int, body: dict, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {"content-type": "application/json"}
        self.text = str(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _SerialClient:
    def __init__(self, responses: list[_Response], hold_seconds: float = 0.03) -> None:
        self.responses = responses
        self.hold_seconds = hold_seconds
        self.index = 0
        self.active = 0
        self.max_active = 0
        self.intervals: list[tuple[float, float]] = []
        self._guard = threading.Lock()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def post(self, _url, *, headers, json):
        del headers, json
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            started = time.monotonic()
            response = self.responses[min(self.index, len(self.responses) - 1)]
            self.index += 1
        time.sleep(self.hold_seconds)
        finished = time.monotonic()
        with self._guard:
            self.intervals.append((started, finished))
            self.active -= 1
        return response


def _profile() -> dict[str, str]:
    return {
        "api_key": "test-key",
        "base_url": "https://proxy.example/v1",
        "endpoint": "/images/generations",
        "model": "gpt-image-2",
    }


def _success() -> _Response:
    return _Response(200, {"data": [{"b64_json": base64.b64encode(_png()).decode("ascii")}]})


def test_same_image_channel_is_serial_across_provider_instances(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    client = _SerialClient([_success(), _success()])
    monkeypatch.setattr("providers.image_provider.create_http_client", lambda _settings: client)
    profile = _profile()
    providers = [OpenAIImageProvider(profile), OpenAIImageProvider(profile)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(item.generate, f"图 {index}", tmp_path / f"{index}.png") for index, item in enumerate(providers)]
        assert all(future.result().is_file() for future in futures)
    assert client.max_active == 1
    assert len(client.intervals) == 2
    assert client.intervals[1][0] >= client.intervals[0][1] or client.intervals[0][0] >= client.intervals[1][1]


def test_http_429_retries_after_retry_after_and_saves_image(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    client = _SerialClient([_Response(429, {"error": {"code": "rate_limit", "message": "busy"}}, {"Retry-After": "0", "content-type": "application/json"}), _success()], hold_seconds=0)
    monkeypatch.setattr("providers.image_provider.create_http_client", lambda _settings: client)
    sleeps: list[float] = []
    monkeypatch.setattr("providers.image_provider.time.sleep", lambda seconds: sleeps.append(seconds))
    provider = OpenAIImageProvider(_profile())
    output = provider.generate_image(ImageGenerationRequest("一张图", tmp_path / "retry.png"))
    assert output.is_file()
    assert client.index == 2
    assert sleeps and sleeps[0] == 0
    assert provider.last_http_status == 200
    assert provider.last_attempt == 2


@pytest.mark.parametrize("status", [400, 401, 404, 422])
def test_non_rate_limit_status_is_not_retried(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int):
    client = _SerialClient([_Response(status, {"error": {"code": "rejected", "message": "invalid"}})], hold_seconds=0)
    monkeypatch.setattr("providers.image_provider.create_http_client", lambda _settings: client)
    provider = OpenAIImageProvider(_profile())
    with pytest.raises(ProviderError):
        provider.generate_image(ImageGenerationRequest("一张图", tmp_path / f"{status}.png"))
    assert client.index == 1


def test_diagnostic_queue_and_timestamps_are_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    client = _SerialClient([_success()], hold_seconds=0)
    monkeypatch.setattr("providers.image_provider.create_http_client", lambda _settings: client)
    provider = OpenAIImageProvider(_profile())
    result = provider.test_connection(tmp_path / "diagnostic.png")
    assert result.success
    for key in ("HTTP_STATUS", "ATTEMPT", "QUEUE_POSITION", "REQUEST_STARTED_AT", "REQUEST_FINISHED_AT", "RETRY_AFTER"):
        assert key in result.details
