from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from modules.security import redact_sensitive_text, sanitize_json
from providers.contracts import ArticleGenerationRequest, ModelTestResult
from providers.errors import user_facing_error_message
from providers.model_discovery import discover_models
from providers.text_provider import OpenAITextProvider, ProviderError


PROBE_PHRASE = "正文模型测试通过"
TEXT_CAPABILITY = "text_content"
INCOMPATIBLE_MODEL_ERRORS = {
    "MODEL_OUTPUT_REASONING_ONLY",
    "MODEL_NOT_FOUND",
    "MODEL_OUTPUT_EMPTY",
    "MODEL_OUTPUT_PARSE_FAILED",
    "INVALID_RESPONSE",
    "MODEL_CAPABILITY_PROBE_FAILED",
    "NO_USABLE_TEXT_MODEL",
}
EXCLUDED_MODEL_TOKENS = (
    "embedding",
    "embed",
    "rerank",
    "audio",
    "tts",
    "whisper",
    "image",
    "img",
    "dall",
    "seedream",
    "cogview",
    "flux",
    "stable-diffusion",
    "sdxl",
    "imagen",
)
REASONING_TOKENS = ("reasoner", "reasoning", "thinking", "r1", "v4-flash")
TEXT_PRIORITY_TOKENS = ("chat", "instruct", "general", "gpt", "qwen", "glm", "doubao", "deepseek")


def base_url_hash(base_url: str, endpoint: str = "") -> str:
    normalized = (str(base_url or "").strip().rstrip("/") + "|" + str(endpoint or "/chat/completions").strip()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _provider_id(profile: dict[str, Any]) -> str:
    return str(profile.get("provider_id") or profile.get("name") or "openai_compatible_text").strip()


def _endpoint(profile: dict[str, Any]) -> str:
    return "/" + str(profile.get("endpoint") or "/chat/completions").strip().lstrip("/")


def _usable_cached_model(settings: dict[str, Any], profile: dict[str, Any]) -> str:
    model = str(settings.get("resolved_text_model") or "").strip()
    if not model:
        return ""
    if str(settings.get("resolved_text_provider") or "") != _provider_id(profile):
        return ""
    if str(settings.get("resolved_text_base_url_hash") or "") != base_url_hash(str(profile.get("base_url") or ""), _endpoint(profile)):
        return ""
    if str(settings.get("resolved_text_capability_status") or "") != "verified":
        return ""
    return model


def _is_candidate_model(model_id: str) -> bool:
    lowered = str(model_id or "").strip().lower()
    return bool(lowered) and not any(token in lowered for token in EXCLUDED_MODEL_TOKENS)


def _candidate_rank(model_id: str, configured_model: str, cached_model: str) -> tuple[int, str]:
    lowered = model_id.lower()
    if cached_model and model_id == cached_model:
        return (0, lowered)
    if configured_model and model_id == configured_model:
        return (1, lowered)
    priority = 5
    if any(token in lowered for token in TEXT_PRIORITY_TOKENS):
        priority = 2
    if any(token in lowered for token in REASONING_TOKENS):
        priority = 8
    return (priority, lowered)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _fallback_candidates(profile: dict[str, Any]) -> list[str]:
    provider = _provider_id(profile).lower()
    values = [str(profile.get("model") or "").strip()]
    base = str(profile.get("base_url") or "").lower()
    if "deepseek" in provider or "deepseek" in base:
        values.extend(["deepseek-chat", "deepseek-reasoner"])
    values.extend(["gpt-4o-mini", "qwen-plus", "glm-4.5"])
    return [item for item in _dedupe(values) if _is_candidate_model(item)]


def _probe_candidate(profile: dict[str, Any], model_id: str, network_settings: dict[str, Any] | None) -> tuple[bool, dict[str, Any], str, str]:
    probe_profile = dict(profile)
    probe_profile["model"] = model_id
    probe_profile["timeout_seconds"] = min(45, max(10, int(float(probe_profile.get("timeout_seconds") or 30))))
    provider = OpenAITextProvider(probe_profile, network_settings=network_settings)
    try:
        content = provider.generate_article(
            ArticleGenerationRequest(
                "不要解释，不要分析，只回复：正文模型测试通过",
                temperature=0,
                max_tokens=16,
                response_format="none",
            )
        )
        diagnostic = dict(provider.last_diagnostic or {})
        if PROBE_PHRASE not in str(content or ""):
            raise ProviderError(
                "MODEL_CAPABILITY_PROBE_FAILED",
                "text model did not return the expected probe phrase",
                details=diagnostic,
            )
        if any(token in str(content).lower() for token in ("reasoning", "思考过程", "分析过程", "我需要", "we need")):
            raise ProviderError(
                "MODEL_CAPABILITY_PROBE_FAILED",
                "text model returned process text during probe",
                details=diagnostic,
            )
        return True, diagnostic, "", ""
    except ProviderError as exc:
        return False, dict(exc.details or provider.last_diagnostic or {}), str(exc.code), redact_sensitive_text(str(exc.detail))
    except Exception as exc:
        return False, dict(provider.last_diagnostic or {}), "MODEL_CONNECTION_FAILED", redact_sensitive_text(str(exc))


def resolve_usable_text_model(
    settings: dict[str, Any],
    profile: dict[str, Any],
    *,
    network_settings: dict[str, Any] | None = None,
    force_refresh: bool = False,
    source: str = "configured_probe",
) -> dict[str, Any]:
    working_profile = dict(profile)
    configured_model = str(working_profile.get("model") or "").strip()
    cached_model = _usable_cached_model(settings, working_profile)
    if cached_model and not force_refresh:
        working_profile["model"] = cached_model
        return {
            "success": True,
            "provider": _provider_id(working_profile),
            "resolved_model": cached_model,
            "base_url_hash": base_url_hash(str(working_profile.get("base_url") or ""), _endpoint(working_profile)),
            "capability": TEXT_CAPABILITY,
            "verified_at": str(settings.get("resolved_text_verified_at") or settings.get("verified_at") or ""),
            "probe_status": "cached",
            "response_parser_mode": str(settings.get("resolved_text_parser_mode") or ""),
            "model_resolution_source": "cached",
            "profile": working_profile,
            "discovery": {"success": None, "model_count": 0},
            "probes": [],
        }

    discovery = discover_models(working_profile, network_settings)
    discovered = list(discovery.get("text_models") or []) if discovery.get("success") else []
    fallback_candidates = [] if discovered else _fallback_candidates(working_profile)
    candidates = _dedupe([configured_model, cached_model, *discovered, *fallback_candidates])
    candidates = sorted([item for item in candidates if _is_candidate_model(item)], key=lambda item: _candidate_rank(item, configured_model, cached_model))
    candidates = candidates[:8]
    probes: list[dict[str, Any]] = []
    first_error_code = ""
    reasoning_only_count = 0
    model_not_found_count = 0
    for model_id in candidates:
        ok, diagnostic, code, message = _probe_candidate(working_profile, model_id, network_settings)
        probe = sanitize_json(
            {
                "model": model_id,
                "success": ok,
                "http_status": diagnostic.get("http_status"),
                "response_parser_mode": diagnostic.get("parser_mode"),
                "provider_error_code": code,
                "message": message[:200],
                "content_present": diagnostic.get("content_present"),
                "reasoning_content_present": diagnostic.get("reasoning_content_present"),
            }
        )
        probes.append(probe)
        if code == "MODEL_OUTPUT_REASONING_ONLY":
            reasoning_only_count += 1
        if code == "MODEL_NOT_FOUND":
            model_not_found_count += 1
        if code and not first_error_code:
            first_error_code = code
        if ok:
            resolved_profile = dict(working_profile)
            resolved_profile["model"] = model_id
            return {
                "success": True,
                "provider": _provider_id(resolved_profile),
                "resolved_model": model_id,
                "base_url_hash": base_url_hash(str(resolved_profile.get("base_url") or ""), _endpoint(resolved_profile)),
                "capability": TEXT_CAPABILITY,
                "verified_at": _now(),
                "probe_status": "verified",
                "response_parser_mode": str(diagnostic.get("parser_mode") or ""),
                "model_resolution_source": "models_discovery" if discovery.get("success") else source,
                "profile": resolved_profile,
                "discovery": sanitize_json(discovery),
                "probes": probes,
            }
    code = "MODEL_OUTPUT_REASONING_ONLY" if probes and reasoning_only_count == len(probes) else "NO_USABLE_TEXT_MODEL"
    if probes and model_not_found_count == len(probes):
        code = "MODEL_NOT_FOUND"
    if first_error_code in {
        "AUTHENTICATION_FAILED",
        "PERMISSION_DENIED",
        "RATE_LIMITED",
        "QUOTA_EXCEEDED",
        "INSUFFICIENT_BALANCE",
        "TIMEOUT",
        "TLS_ERROR",
        "DNS_ERROR",
        "NETWORK_ERROR",
        "PROXY_ERROR",
        "MODEL_CONNECTION_FAILED",
        "MODEL_HTTP_ERROR",
        "ENDPOINT_NOT_FOUND",
        "NO_AVAILABLE_CHANNEL",
    }:
        code = first_error_code
    if not candidates and not discovery.get("success"):
        code = "MODEL_DISCOVERY_FAILED"
    return {
        "success": False,
        "provider": _provider_id(working_profile),
        "resolved_model": "",
        "base_url_hash": base_url_hash(str(working_profile.get("base_url") or ""), _endpoint(working_profile)),
        "capability": TEXT_CAPABILITY,
        "verified_at": "",
        "probe_status": "failed",
        "response_parser_mode": "",
        "model_resolution_source": source,
        "profile": working_profile,
        "discovery": sanitize_json(discovery),
        "probes": probes,
        "error_code": code,
        "error_message": user_facing_error_message(code, "当前配置下没有找到可生成正式正文的模型。"),
        "first_error_code": first_error_code,
    }


def persist_resolved_text_model(settings: dict[str, Any], resolution: dict[str, Any]) -> dict[str, Any]:
    profile = dict(settings.get("text_profile") or {})
    resolved_profile = dict(resolution.get("profile") or {})
    for key, value in resolved_profile.items():
        if key != "configured_model":
            profile[key] = value
    resolved_model = str(resolution.get("resolved_model") or "")
    if resolved_model:
        profile["model"] = resolved_model
    settings["text_profile"] = profile
    settings["resolved_text_model"] = resolved_model
    settings["resolved_text_provider"] = str(resolution.get("provider") or "")
    settings["resolved_text_base_url_hash"] = str(resolution.get("base_url_hash") or "")
    settings["resolved_text_verified_at"] = str(resolution.get("verified_at") or _now())
    settings["resolved_text_capability_status"] = "verified"
    settings["resolved_text_parser_mode"] = str(resolution.get("response_parser_mode") or "")
    settings["verified_text_model"] = resolved_model
    settings["verified_text_base_url"] = str(profile.get("base_url") or "").strip().rstrip("/")
    settings["verified_text_endpoint"] = _endpoint(profile)
    settings["verified_at"] = settings["resolved_text_verified_at"]
    settings["last_text_model_test_at"] = settings["resolved_text_verified_at"]
    settings["_preserve_text_resolution_on_save"] = True
    return settings


def model_test_result_from_resolution(resolution: dict[str, Any]) -> ModelTestResult:
    success = bool(resolution.get("success"))
    details = sanitize_json(
        {
            "provider": resolution.get("provider"),
            "resolved_model": resolution.get("resolved_model"),
            "base_url_hash": resolution.get("base_url_hash"),
            "capability": resolution.get("capability"),
            "verified_at": resolution.get("verified_at"),
            "probe_status": resolution.get("probe_status"),
            "response_parser_mode": resolution.get("response_parser_mode"),
            "model_resolution_source": resolution.get("model_resolution_source"),
            "discovery": resolution.get("discovery"),
            "probes": resolution.get("probes"),
            "user_message": "文本模型连接成功，已自动匹配可生成正文的模型。" if success else resolution.get("error_message"),
        }
    )
    return ModelTestResult(
        success,
        str(resolution.get("provider") or "openai-compatible-text"),
        str(resolution.get("resolved_model") or ""),
        None,
        0,
        str(resolution.get("response_parser_mode") or "text"),
        success,
        error_code="" if success else str(resolution.get("error_code") or "NO_USABLE_TEXT_MODEL"),
        error_message="" if success else str(resolution.get("error_message") or "当前配置下没有找到可生成正式正文的模型。"),
        retryable=False,
        details=details,
    )
