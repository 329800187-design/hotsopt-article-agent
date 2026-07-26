from __future__ import annotations

import time
from typing import Any

import httpx

from modules.network import create_http_client, resolve_network_settings
from modules.security import redact_sensitive_text
from providers.errors import map_provider_exception, user_facing_error_message
from providers.text_provider import ProviderError, _headers


TEXT_EXCLUDE_TOKENS = ("embedding", "embed", "rerank", "audio", "tts", "whisper", "image-only", "vision-only")
IMAGE_TOKENS = ("image", "img", "dall", "seedream", "cogview", "flux", "stable-diffusion", "sdxl", "imagen")
UNSTABLE_TOKENS = ("preview", "beta", "experimental", "test")


def _models_url(base_url: str) -> str:
    base = str(base_url or "").rstrip("/")
    if not base:
        raise ProviderError("MODEL_NOT_CONFIGURED", "model base URL is missing")
    return f"{base}/models"


def _model_id(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get("id") or item.get("name") or item.get("model") or "").strip()
    return ""


def _looks_like_image(model_id: str, item: Any) -> bool:
    haystack = model_id.lower()
    if isinstance(item, dict):
        haystack += " " + " ".join(str(value).lower() for value in item.values() if isinstance(value, (str, int, float, bool)))
    return any(token in haystack for token in IMAGE_TOKENS)


def _looks_like_text(model_id: str, item: Any) -> bool:
    lowered = model_id.lower()
    if not lowered or any(token in lowered for token in TEXT_EXCLUDE_TOKENS):
        return False
    if _looks_like_image(model_id, item) and not any(token in lowered for token in ("gpt-4o", "omni", "vl", "vision")):
        return False
    return True


def _sort_key(model_id: str) -> tuple[int, int, str]:
    lowered = model_id.lower()
    stable_penalty = 1 if any(token in lowered for token in UNSTABLE_TOKENS) else 0
    legacy_penalty = 1 if any(token in lowered for token in ("3.5", "turbo-instruct")) else 0
    return (stable_penalty, legacy_penalty, lowered)


def classify_models(raw_models: list[Any]) -> dict[str, Any]:
    text_models: list[str] = []
    image_models: list[str] = []
    other_models: list[str] = []
    for item in raw_models:
        model_id = _model_id(item)
        if not model_id:
            continue
        if _looks_like_image(model_id, item):
            image_models.append(model_id)
        if _looks_like_text(model_id, item):
            text_models.append(model_id)
        if model_id not in text_models and model_id not in image_models:
            other_models.append(model_id)
    text_models = sorted(dict.fromkeys(text_models), key=_sort_key)
    image_models = sorted(dict.fromkeys(image_models), key=_sort_key)
    other_models = sorted(dict.fromkeys(other_models))
    return {
        "text_models": text_models,
        "image_models": image_models,
        "other_models": other_models,
        "recommended_text_model": text_models[0] if text_models else "",
        "recommended_image_model": image_models[0] if image_models else "",
    }


def discover_models(profile: dict[str, Any], network_settings: dict[str, Any] | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    base_url = str(profile.get("base_url") or "").strip()
    url = _models_url(base_url)
    response: httpx.Response | None = None
    try:
        timeout = float(profile.get("timeout_seconds") or 15)
        with create_http_client(resolve_network_settings(network_settings, {**profile, "timeout_seconds": timeout})) as client:
            response = client.get(url, headers=_headers(profile))
        if response.status_code in {404, 405}:
            raise ProviderError("MODEL_LIST_UNSUPPORTED", "provider does not support GET /models")
        if response.status_code == 401:
            raise ProviderError("AUTHENTICATION_FAILED", "provider authentication failed")
        if response.status_code == 403:
            raise ProviderError("PERMISSION_DENIED", "provider permission denied")
        response.raise_for_status()
        payload = response.json()
        raw_models = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(raw_models, list):
            raise ProviderError("INVALID_RESPONSE", "model list response is invalid")
        classified = classify_models(raw_models)
        return {
            "success": True,
            "discoverer": "GET /models",
            "url": url,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "model_count": len(raw_models),
            **classified,
        }
    except Exception as exc:
        mapped = map_provider_exception(exc, response)
        code = str(getattr(mapped, "code", "NETWORK_ERROR"))
        if code == "MODEL_NOT_FOUND" and response is not None and response.status_code in {404, 405}:
            code = "MODEL_LIST_UNSUPPORTED"
        detail = redact_sensitive_text(str(getattr(mapped, "detail", mapped)))
        return {
            "success": False,
            "discoverer": "GET /models",
            "url": url if base_url else "",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "error_code": code,
            "message": user_facing_error_message(code, detail),
            "detail": detail,
            "model_count": 0,
            "text_models": [],
            "image_models": [],
            "other_models": [],
            "recommended_text_model": "",
            "recommended_image_model": "",
        }
