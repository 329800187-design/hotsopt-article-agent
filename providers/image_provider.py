from __future__ import annotations

import base64
import hashlib
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from PIL import Image

from modules.network import create_http_client, resolve_network_settings
from modules.security import redact_sensitive_text
from providers.contracts import ImageGenerationRequest, ModelTestResult
from providers.errors import is_retryable_error, map_provider_exception, user_facing_error_message
from providers.text_provider import ProviderError, _headers


MAX_IMAGE_BYTES = 20 * 1024 * 1024
DATA_URI_RE = re.compile(r"^data:(?P<mime>image/(?:png|jpeg|jpg|webp));base64,(?P<data>[A-Za-z0-9+/=\r\n]+)$", re.I)


def _first_string(value: Any, keys: list[str]) -> str:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return ""


def _image_item_from_response(data: Any, native_dashscope: bool = False) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    if native_dashscope:
        choices = data.get("output", {}).get("choices", []) if isinstance(data.get("output"), dict) else []
        content = choices[0].get("message", {}).get("content", []) if choices and isinstance(choices[0], dict) else []
        if isinstance(content, list):
            native_image = next((part.get("image") for part in content if isinstance(part, dict) and part.get("image")), None)
            if isinstance(native_image, str) and native_image.strip():
                return {"image": native_image.strip()}
    candidates: list[Any] = []
    if isinstance(data.get("data"), list):
        candidates.extend(data.get("data") or [])
    if isinstance(data.get("output"), dict):
        output = data.get("output") or {}
        for key in ("images", "results", "data"):
            if isinstance(output.get(key), list):
                candidates.extend(output.get(key) or [])
        candidates.append(output)
    if isinstance(data.get("images"), list):
        candidates.extend(data.get("images") or [])
    candidates.append(data)
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return {"image": candidate.strip()}
        if isinstance(candidate, dict):
            value = _first_string(candidate, ["b64_json", "base64", "image_base64", "image", "data", "url", "image_url"])
            if value:
                return dict(candidate)
    return {}


def _decode_image_string(value: str) -> tuple[str, bytes | str]:
    cleaned = str(value or "").strip()
    match = DATA_URI_RE.match(cleaned)
    if match:
        return "data_uri", base64.b64decode(match.group("data"), validate=True)
    parsed = urlparse(cleaned)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return "url", cleaned
    return "base64", base64.b64decode(cleaned, validate=True)


def normalize_image_size(size: Any, api_format: str | None = None) -> str:
    value = str(size or "").strip()
    if not value:
        value = "1024*1024" if str(api_format or "").lower() == "dashscope_native" else "1536x1024"
    if str(api_format or "").lower() == "dashscope_native":
        return value.replace("x", "*")
    return value.replace("*", "x")


def normalize_endpoint_url(base_url: Any, endpoint: Any) -> str:
    """Join provider URLs without duplicating a shared version path."""
    base = str(base_url or "").strip().rstrip("/")
    target = "/" + str(endpoint or "").strip().lstrip("/")
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProviderError("MODEL_NOT_CONFIGURED", "image model base URL is invalid")
    base_parts = [part for part in parsed.path.split("/") if part]
    endpoint_parts = [part for part in target.split("/") if part]
    overlap = 0
    for length in range(min(len(base_parts), len(endpoint_parts)), 0, -1):
        if base_parts[-length:] == endpoint_parts[:length]:
            overlap = length
            break
    path = "/" + "/".join(base_parts + endpoint_parts[overlap:])
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def build_image_request_payload(profile: dict[str, Any], prompt: str) -> dict[str, Any]:
    adapter = str(profile.get("request_adapter") or "").strip()
    if not adapter:
        adapter = "dashscope_multimodal_generation" if str(profile.get("api_format") or "").lower() == "dashscope_native" else "openai_images_generations"
    if adapter == "dashscope_multimodal_generation":
        return {
            "model": profile.get("model"),
            "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
            "parameters": {
                "watermark": False,
                "size": normalize_image_size(profile.get("size"), "dashscope_native"),
            },
        }
    if adapter == "openai_images_generations":
        return {
            "model": profile.get("model"),
            "prompt": prompt,
            "size": normalize_image_size(profile.get("size"), "openai_compatible"),
            "n": 1,
        }
    raise ProviderError("INVALID_REQUEST", f"unsupported image request adapter: {adapter}")


def inspect_image(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ProviderError("INVALID_RESPONSE", "image file was not created")
    size = path.stat().st_size
    if size <= 0 or size > MAX_IMAGE_BYTES:
        raise ProviderError("INVALID_RESPONSE", "image file size is invalid")
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            image_format = str(image.format or "").lower()
    except Exception as exc:
        raise ProviderError("INVALID_RESPONSE", "image file is not a valid raster image") from exc
    if width < 64 or height < 64 or image_format not in {"png", "jpeg", "webp"}:
        raise ProviderError("INVALID_RESPONSE", "image dimensions or format are unsupported")
    return {"mime_type": f"image/{'jpeg' if image_format == 'jpeg' else image_format}", "format": image_format, "width": width, "height": height, "bytes": size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


class OpenAIImageProvider:
    def __init__(self, profile: dict[str, Any], network_settings: dict[str, Any] | None = None) -> None:
        self.profile = profile
        self.network_settings = resolve_network_settings(network_settings, profile)
        self.last_http_status: int | None = None
        self.last_response_type = ""
        self.generation_calls = 0

    def check_configuration(self) -> ModelTestResult:
        """Validate image settings only; this method never calls the image endpoint."""
        started = time.perf_counter()
        base_url = str(self.profile.get("base_url") or "").strip()
        endpoint = str(self.profile.get("endpoint") or "").strip()
        model = str(self.profile.get("model") or "").strip()
        code = ""
        if not base_url or urlparse(base_url).scheme not in {"http", "https"} or not urlparse(base_url).netloc:
            code = "INVALID_REQUEST"
        elif not endpoint.startswith("/") or endpoint.rstrip("/") in {"", "/chat/completions"}:
            code = "INVALID_REQUEST"
        details = {"generation_calls": 0, "paid_test": False, "charged": False, "configuration_only": True, "key_present": bool(str(self.profile.get("api_key") or ""))}
        elapsed = int((time.perf_counter() - started) * 1000)
        if code:
            return ModelTestResult(False, "openai-compatible-image", model, elapsed_ms=elapsed, error_code=code, error_message=user_facing_error_message(code, "图片配置不完整"), details=details)
        return ModelTestResult(True, "openai-compatible-image", model, elapsed_ms=elapsed, details=details)

    def test_connection(self, output_path: Path) -> ModelTestResult:
        started = time.perf_counter()
        try:
            path = self.generate_image(ImageGenerationRequest("一只白色咖啡杯放在木桌上，纯净背景，不含文字。", output_path))
            metadata = inspect_image(path)
            return ModelTestResult(True, "openai-compatible-image", str(self.profile.get("model") or ""), self.last_http_status, int((time.perf_counter() - started) * 1000), image_response_type=self.last_response_type, details={**metadata, "generation_calls": self.generation_calls, "paid_test": True, "charged": self.generation_calls > 0})
        except Exception as exc:
            mapped = map_provider_exception(exc)
            code = str(getattr(mapped, "code", "PROVIDER_INTERNAL_ERROR"))
            detail = str(getattr(mapped, "detail", mapped))
            return ModelTestResult(False, "openai-compatible-image", str(self.profile.get("model") or ""), self.last_http_status, int((time.perf_counter() - started) * 1000), image_response_type=self.last_response_type, error_code=code, error_message=user_facing_error_message(code, redact_sensitive_text(detail)), retryable=is_retryable_error(code), details={"generation_calls": self.generation_calls, "paid_test": True, "charged": self.generation_calls > 0})

    def generate(self, prompt: str, output_path: Path) -> Path:
        return self.generate_image(ImageGenerationRequest(prompt, output_path))

    def generate_image(self, request: ImageGenerationRequest) -> Path:
        api_key = str(self.profile.get("api_key") or "")
        if not api_key and str(self.profile.get("auth_type") or "bearer").lower() != "none":
            raise ProviderError("MODEL_NOT_CONFIGURED", "image model API key is missing")
        base_url = str(self.profile.get("base_url") or "").rstrip("/")
        endpoint = str(self.profile.get("endpoint") or "/images/generations")
        if not base_url:
            raise ProviderError("MODEL_NOT_CONFIGURED", "image model base URL is missing")
        url = normalize_endpoint_url(base_url, endpoint)
        response_adapter = str(self.profile.get("response_adapter") or "")
        native_dashscope = response_adapter == "dashscope_image_or_openai_image" or str(self.profile.get("api_format") or "").lower() == "dashscope_native"
        payload = build_image_request_payload(self.profile, request.prompt)
        response: httpx.Response | None = None
        try:
            self.generation_calls += 1
            timeout = float(self.profile.get("timeout_seconds") or self.network_settings.get("timeout_seconds") or 180)
            with create_http_client({**self.network_settings, "timeout_seconds": timeout}) as client:
                response = client.post(url, headers=_headers(self.profile), json=payload)
            self.last_http_status = response.status_code
            if response.status_code == 401:
                raise ProviderError("AUTHENTICATION_FAILED", "image model authentication failed")
            if response.status_code == 404:
                content_type = str(response.headers.get("content-type") or "").lower()
                if "text/html" in content_type or "text/plain" in content_type:
                    raise ProviderError("IMAGE_GENERATION_NOT_SUPPORTED", "image generation endpoint is unavailable")
                raise ProviderError("MODEL_NOT_FOUND", "image model endpoint or model was not found")
            if response.status_code == 429:
                from providers.errors import parse_retry_after
                raise ProviderError("RATE_LIMITED", "image model rate limited", parse_retry_after(response.headers.get("Retry-After")))
            response.raise_for_status()
            data = response.json()
            item = _image_item_from_response(data, native_dashscope=native_dashscope)
            payload_value = _first_string(item, ["b64_json", "base64", "image_base64", "image", "data", "url", "image_url"])
            if not payload_value:
                raise ProviderError("UNSUPPORTED_RESPONSE_FORMAT", "image response has no URL or base64 payload")
            response_type, payload_data = _decode_image_string(payload_value)
            request.output_path.parent.mkdir(parents=True, exist_ok=True)
            if response_type == "url":
                self.last_response_type = "url"
                with create_http_client({**self.network_settings, "timeout_seconds": 60}) as client:
                    image = client.get(str(payload_data))
                    image.raise_for_status()
                    request.output_path.write_bytes(image.content)
            else:
                self.last_response_type = response_type
                request.output_path.write_bytes(payload_data if isinstance(payload_data, bytes) else bytes(payload_data))
            inspect_image(request.output_path)
            return request.output_path
        except httpx.TimeoutException as exc:
            raise ProviderError("TIMEOUT", "image model response timed out") from exc
        except httpx.HTTPError as exc:
            raise map_provider_exception(exc, response) from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise map_provider_exception(exc, response) from exc
