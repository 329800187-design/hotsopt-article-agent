from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from modules.network import create_http_client, resolve_network_settings
from modules.security import redact_sensitive_text
from providers.contracts import ArticleGenerationRequest, ModelTestResult
from providers.errors import is_retryable_error, map_provider_exception


logger = logging.getLogger(__name__)
DEFAULT_TEXT_ENDPOINT = "/chat/completions"
PREVIEW_LIMIT = 500


class ProviderError(RuntimeError):
    def __init__(
        self,
        code: str,
        detail: str | None = None,
        retry_after_seconds: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.detail = detail or code
        self.retry_after_seconds = retry_after_seconds
        self.details = details or {}
        super().__init__(f"{code}: {self.detail}")


def _extract_text_blocks(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            parts.extend(_extract_text_blocks(item))
        return parts
    if isinstance(value, dict):
        parts: list[str] = []
        for key in ("text", "output_text", "content", "message"):
            parts.extend(_extract_text_blocks(value.get(key)))
        if parts:
            return parts
    return []


def _extract_response_content(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        for candidate in (first.get("message"), first.get("delta"), first.get("text"), first.get("content")):
            text = "".join(_extract_text_blocks(candidate)).strip()
            if text:
                return text
    text = "".join(_extract_text_blocks(data.get("output_text"))).strip()
    if text:
        return text
    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            text = "".join(_extract_text_blocks(item)).strip()
            if text:
                return text
    text = "".join(_extract_text_blocks(data.get("content"))).strip()
    if text:
        return text
    return ""


def _headers(profile: dict[str, Any]) -> dict[str, str]:
    result = {str(key): str(value) for key, value in profile.get("headers", {}).items()}
    api_key = str(profile.get("api_key") or "")
    auth_type = str(profile.get("auth_type") or "bearer").lower()
    if api_key and auth_type == "bearer":
        result.setdefault("Authorization", f"Bearer {api_key}")
    elif api_key and auth_type == "x-api-key":
        result.setdefault("X-API-Key", api_key)
    elif api_key and auth_type == "custom_header":
        result.setdefault(str(profile.get("auth_header") or "X-API-Key"), api_key)
    result.setdefault("Content-Type", "application/json")
    return result


def _mask_url(url: str) -> str:
    try:
        split = urlsplit(str(url or ""))
        return urlunsplit((split.scheme, split.netloc, split.path, "", ""))
    except Exception:
        return redact_sensitive_text(str(url or ""))


def _preview_text(value: str) -> str:
    return redact_sensitive_text(str(value or "")[:PREVIEW_LIMIT])


def _response_preview(response: httpx.Response | None) -> str:
    if response is None:
        return ""
    try:
        return _preview_text(response.text)
    except Exception:
        return ""


def _content_type(response: httpx.Response | None) -> str:
    if response is None:
        return ""
    headers = getattr(response, "headers", {}) or {}
    return str(headers.get("Content-Type") or "").strip()


def _segments(path: str) -> list[str]:
    return [segment for segment in str(path or "").split("/") if segment]


def _base_contains_endpoint(path: str) -> bool:
    lowered = str(path or "").lower()
    return any(token in lowered for token in ("/chat/completions", "/responses", "/completions"))


def _build_request_url(base_url: str, endpoint: str) -> tuple[str, dict[str, Any]]:
    normalized_base = str(base_url or "").strip().rstrip("/")
    endpoint_text = str(endpoint or "").strip()
    if not normalized_base:
        raise ProviderError("MODEL_NOT_CONFIGURED", "text model base URL is missing")
    split = urlsplit(normalized_base)
    if not split.scheme or not split.netloc:
        raise ProviderError("ENDPOINT_NOT_FOUND", "text model base URL is invalid")
    if not endpoint_text:
        if _base_contains_endpoint(split.path):
            final_url = normalized_base
            return final_url, {
                "base_url": normalized_base,
                "endpoint": "",
                "normalized_endpoint": split.path or DEFAULT_TEXT_ENDPOINT,
                "final_url": _mask_url(final_url),
                "normalization": "base_url_contains_endpoint",
            }
        raise ProviderError("ENDPOINT_NOT_FOUND", "text model endpoint is empty")
    endpoint_path = "/" + endpoint_text.lstrip("/")
    base_segments = _segments(split.path)
    endpoint_segments = _segments(endpoint_path)
    overlap = 0
    max_overlap = min(len(base_segments), len(endpoint_segments))
    for size in range(max_overlap, 0, -1):
        if base_segments[-size:] == endpoint_segments[:size]:
            overlap = size
            break
    merged_segments = base_segments + endpoint_segments[overlap:]
    merged_path = "/" + "/".join(merged_segments) if merged_segments else endpoint_path
    final_url = urlunsplit((split.scheme, split.netloc, merged_path, "", ""))
    normalization = "unchanged"
    if overlap:
        normalization = "deduplicated_overlap"
    elif split.path and split.path != merged_path:
        normalization = "merged"
    return final_url, {
        "base_url": normalized_base,
        "endpoint": endpoint_text,
        "normalized_endpoint": endpoint_path,
        "final_url": _mask_url(final_url),
        "normalization": normalization,
    }


def _diagnostic_message(details: dict[str, Any]) -> str:
    return (
        "text provider request failed "
        f"url={details.get('final_url')} "
        f"model={details.get('model')} "
        f"status={details.get('http_status')} "
        f"error_type={details.get('error_type')} "
        f"elapsed_ms={details.get('elapsed_ms')} "
        f"content_type={details.get('content_type')} "
        f"preview={details.get('response_preview')}"
    )


def _strip_code_block(text: str) -> str:
    cleaned = str(text or "").strip()
    if not cleaned.startswith("```"):
        return cleaned
    lines = cleaned.splitlines()
    if not lines:
        return cleaned
    lines = lines[1:]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() == "```":
        lines.pop()
    return "\n".join(lines).strip()


def _first_json_object(text: str) -> str:
    start = -1
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(str(text or "")):
        if start < 0:
            if char == "{":
                start = index
                depth = 1
                in_string = False
                escaped = False
            continue
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


def _classify_http_502(response: httpx.Response, details: dict[str, Any]) -> ProviderError:
    preview = _response_preview(response)
    content_type = _content_type(response)
    lowered = preview.lower()
    code = "PROVIDER_INTERNAL_ERROR"
    detail = "provider returned HTTP 502"
    error_type = "provider_502"
    if any(token in lowered for token in ("model_not_found", "model not found", "unknown model", "no available channel")):
        code = "MODEL_NOT_FOUND"
        detail = "text model was not found by the provider"
        error_type = "model_invalid"
    elif any(token in lowered for token in ("gateway timeout", "upstream timeout", "upstream timed out", "timed out", "timeout")):
        code = "TIMEOUT"
        detail = "text model upstream timed out"
        error_type = "upstream_timeout"
    elif any(token in lowered for token in ("invalid_request_error", "invalid request", "unsupported response_format", "response_format", "messages")):
        code = "INVALID_REQUEST"
        detail = "text model request payload was rejected"
        error_type = "request_invalid"
    elif content_type and "json" not in content_type.lower() and ("<html" in lowered or "<!doctype" in lowered):
        code = "INVALID_RESPONSE"
        detail = "provider returned a non-JSON gateway response"
        error_type = "response_format_invalid"
    return ProviderError(
        code,
        detail,
        details={
            **details,
            "http_status": response.status_code,
            "content_type": content_type,
            "response_preview": preview,
            "error_type": error_type,
        },
    )


def _parse_sse_stream(body: str) -> str:
    """Parse SSE (Server-Sent Events) streaming response body into concatenated text."""
    parts: list[str] = []
    for line in str(body or "").splitlines():
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if line.lower() == "data: [done]":
            continue
        if line.startswith("data:"):
            data_str = line[5:].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                parts.append(data_str)
                continue
            if isinstance(chunk, dict):
                choices = chunk.get("choices")
                if isinstance(choices, list) and choices:
                    first = choices[0] if isinstance(choices[0], dict) else {}
                    for candidate in (first.get("delta"), first.get("message"), first):
                        text = "".join(_extract_text_blocks(candidate)).strip()
                        if text:
                            parts.append(text)
                            break
    return "".join(parts).strip()


def _decode_provider_response(response: httpx.Response) -> tuple[str, dict[str, Any]]:
    """Intelligently decode provider response into (content, diagnostic).

    Handles: standard JSON wrapper, SSE streaming, plain text / Markdown.
    """
    try:
        raw_body = str(getattr(response, "text", "") or "")
    except Exception:
        raw_body = ""
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("Content-Type") or "").strip()
    diagnostic: dict[str, Any] = {
        "http_status": response.status_code,
        "content_type": content_type,
        "parser_mode": "",
        "response_preview": _preview_text(raw_body),
    }

    # ── Try 1: Standard JSON (OpenAI chat completions wrapper) ──
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError):
        data = None

    if isinstance(data, dict):
        content = _extract_response_content(data)
        if content:
            diagnostic["parser_mode"] = "json"
            return content, diagnostic
        # Check for reasoning_content without content
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            msg = first.get("message")
            if isinstance(msg, dict):
                if msg.get("reasoning_content") and not msg.get("content"):
                    diagnostic["parser_mode"] = "json"
                    diagnostic["content_present"] = False
                    diagnostic["reasoning_content_present"] = True
                    # R1.2.1: Use reasoning_content as content for reasoning models (DeepSeek v4)
                    rc = str(msg.get("reasoning_content") or "").strip()
                    if rc:
                        diagnostic["fallback_reasoning_content"] = True
                        return (rc, diagnostic)
                    return ("", diagnostic)
        # Empty JSON but valid
        diagnostic["parser_mode"] = "json"
        return ("", diagnostic)

    # ── Try 2: SSE streaming ──
    if raw_body.strip().startswith("data:"):
        sse_content = _parse_sse_stream(raw_body)
        if sse_content:
            diagnostic["parser_mode"] = "sse"
            return sse_content, diagnostic

    # ── Try 3: Plain text / Markdown ──
    text = raw_body.strip()
    if text:
        diagnostic["parser_mode"] = "text"
        return text, diagnostic

    # ── Empty ──
    diagnostic["parser_mode"] = "empty"
    return ("", diagnostic)


class OpenAITextProvider:
    def __init__(self, profile: dict[str, Any], network_settings: dict[str, Any] | None = None) -> None:
        self.profile = profile
        self.network_settings = resolve_network_settings(network_settings, profile)
        self.last_http_status: int | None = None
        self.last_diagnostic: dict[str, Any] = {}

    def _request_text(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        response_format: str = "none",
        timeout_seconds: float | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Unified text request — all call paths MUST use this single HTTP + decode pipeline."""
        url, url_details = _build_request_url(
            str(self.profile.get("base_url") or ""),
            str(self.profile.get("endpoint") or DEFAULT_TEXT_ENDPOINT),
        )
        diagnostic: dict[str, Any] = {
            **url_details,
            "model": str(self.profile.get("model") or ""),
            "requested_response_format": response_format,
            "payload_has_response_format": bool(response_format and response_format != "none"),
            "stream": False,
            "max_tokens": max_tokens,
            "http_status": None,
            "content_type": "",
            "parser_mode": "",
            "response_preview": "",
            "elapsed_ms": 0,
            "error_type": "",
            "timeout_seconds": None,
        }
        payload: dict[str, Any] = {
            "model": self.profile.get("model"),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format and response_format != "none":
            payload["response_format"] = {"type": "json_object"}
            diagnostic["payload_has_response_format"] = True
        response: httpx.Response | None = None
        started = time.perf_counter()
        try:
            configured_timeout = float(
                timeout_seconds
                or self.profile.get("timeout_seconds")
                or self.network_settings.get("timeout_seconds")
                or 150
            )
            timeout = max(90.0, min(180.0, configured_timeout))
            diagnostic["timeout_seconds"] = timeout
            with create_http_client({**self.network_settings, "timeout_seconds": timeout}) as client:
                response = client.post(url, headers=_headers(self.profile), json=payload)
            self.last_http_status = response.status_code
            diagnostic.update(
                {
                    "http_status": response.status_code,
                    "content_type": _content_type(response),
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                }
            )
            if response.status_code == 401:
                raise ProviderError("AUTHENTICATION_FAILED", "text model authentication failed", details=dict(diagnostic))
            if response.status_code == 404:
                raise ProviderError("MODEL_NOT_FOUND", "text model endpoint or model was not found", details=dict(diagnostic))
            if response.status_code == 429:
                from providers.errors import parse_retry_after
                raise ProviderError(
                    "RATE_LIMITED",
                    "text model rate limited",
                    parse_retry_after(response.headers.get("Retry-After")),
                    details=dict(diagnostic),
                )
            if response.status_code in (502, 504):
                raise _classify_http_502(response, dict(diagnostic))
            response.raise_for_status()
            # ── unified decode ──
            content, decode_diag = _decode_provider_response(response)
            diagnostic.update(decode_diag)
            if not content:
                raise ProviderError("INVALID_RESPONSE", "text model response content is empty", details=dict(diagnostic))
            if content == "MODEL_OUTPUT_EMPTY":
                diagnostic["error_type"] = "model_output_empty"
                raise ProviderError("MODEL_OUTPUT_EMPTY", "text model returned reasoning_content but no content", details=dict(diagnostic))
            diagnostic["error_type"] = "success"
            return content, diagnostic
        except httpx.TimeoutException as exc:
            diagnostic.update({"elapsed_ms": int((time.perf_counter() - started) * 1000), "error_type": "timeout"})
            error = ProviderError("TIMEOUT", "text model response timed out", details=dict(diagnostic))
            logger.error(_diagnostic_message(error.details))
            raise error from exc
        except httpx.HTTPError as exc:
            mapped = map_provider_exception(exc, response)
            details = dict(getattr(mapped, "details", {}) or diagnostic)
            if details:
                logger.error(_diagnostic_message(details))
            raise mapped from exc
        except ProviderError as exc:
            details = dict(exc.details or diagnostic)
            if details:
                if "http_status" not in details:
                    details["http_status"] = self.last_http_status
                logger.error(_diagnostic_message(details))
            raise
        except Exception as exc:
            mapped = map_provider_exception(exc, response)
            details = dict(getattr(mapped, "details", {}) or diagnostic)
            if details:
                logger.error(_diagnostic_message(details))
            raise mapped from exc

    def _error_result(self, started: float, mapped: Exception, error_code: str | None = None, retryable: bool | None = None) -> ModelTestResult:
        code = error_code or str(getattr(mapped, "code", "PROVIDER_INTERNAL_ERROR"))
        detail = str(getattr(mapped, "detail", mapped))
        details = dict(getattr(mapped, "details", {}) or self.last_diagnostic)
        return ModelTestResult(
            False,
            "openai-compatible-text",
            str(self.profile.get("model") or ""),
            self.last_http_status,
            int((time.perf_counter() - started) * 1000),
            error_code=code,
            error_message=redact_sensitive_text(detail),
            retryable=is_retryable_error(code) if retryable is None else retryable,
            details=details,
        )

    def test_connection(self) -> ModelTestResult:
        started = time.perf_counter()
        try:
            content = self.generate_article(ArticleGenerationRequest('Return exactly JSON: {"ok":true}', temperature=0, max_tokens=30, response_format="json_object"))
            parsed = parse_json_response(content)
            return ModelTestResult(
                True,
                "openai-compatible-text",
                str(self.profile.get("model") or ""),
                self.last_http_status,
                int((time.perf_counter() - started) * 1000),
                "json",
                isinstance(parsed, dict),
                details=dict(self.last_diagnostic),
            )
        except Exception as exc:
            return self._error_result(started, map_provider_exception(exc))

    def basic_connection_test(self) -> ModelTestResult:
        started = time.perf_counter()
        try:
            content = self.generate_article(ArticleGenerationRequest("请只回复：连接成功", temperature=0, max_tokens=8, response_format="none"))
            details = dict(self.last_diagnostic)
            details["notice"] = "基础连接通过，不代表长文生成一定成功。"
            return ModelTestResult(
                True,
                "openai-compatible-text",
                str(self.profile.get("model") or ""),
                self.last_http_status,
                int((time.perf_counter() - started) * 1000),
                "text",
                bool(str(content).strip()),
                details=details,
            )
        except Exception as exc:
            return self._error_result(started, map_provider_exception(exc))

    def article_capability_test(self) -> ModelTestResult:
        started = time.perf_counter()
        prompt = """
请生成一段约300字的中文结构化热点文章测试内容，只返回 JSON。必须包含：
- title: 字符串
- intro: 字符串
- sections: 数组，至少3项，每项包含 heading、body、image_brief
- content_markdown: 完整中文正文
- fact_basis: 数组，至少1项，每项包含 fact_id、fact、source_ids
请使用虚构但明确标记为“模型能力测试”的安全内容，不要包含真实敏感个人信息。"""
        try:
            content = self.generate_article(
                ArticleGenerationRequest(
                    prompt,
                    temperature=0.2,
                    max_tokens=500,
                    response_format=str(self.profile.get("response_format") or "json_object"),
                )
            )
            parsed = parse_json_response(content)
            sections = parsed.get("sections")
            fact_basis = parsed.get("fact_basis")
            ok = (
                isinstance(parsed.get("content_markdown"), str)
                and bool(parsed.get("content_markdown").strip())
                and isinstance(sections, list)
                and len(sections) >= 3
                and all(isinstance(item, dict) and str(item.get("heading") or "").strip() and str(item.get("body") or "").strip() for item in sections[:3])
                and isinstance(fact_basis, list)
                and bool(fact_basis)
            )
            if not ok:
                raise ProviderError("MODEL_OUTPUT_INVALID", "article capability test JSON fields are incomplete")
            details = dict(self.last_diagnostic)
            details.update({"structure": "normal", "content_markdown": True, "sections": len(sections), "fact_basis": len(fact_basis)})
            return ModelTestResult(
                True,
                "openai-compatible-text",
                str(self.profile.get("model") or ""),
                self.last_http_status,
                int((time.perf_counter() - started) * 1000),
                "json",
                True,
                details=details,
            )
        except Exception as exc:
            mapped = map_provider_exception(exc)
            raw_code = str(getattr(mapped, "code", "PROVIDER_INTERNAL_ERROR"))
            code_map = {
                "TIMEOUT": "TEXT-LONG-TEST-TIMEOUT",
                "INVALID_RESPONSE": "TEXT-LONG-TEST-FORMAT",
                "MODEL_OUTPUT_INVALID": "TEXT-LONG-TEST-FORMAT",
                "MODEL_NOT_FOUND": "TEXT-LONG-TEST-MODEL",
                "ENDPOINT_NOT_FOUND": "TEXT-LONG-TEST-ENDPOINT",
                "INVALID_REQUEST": "TEXT-LONG-TEST-ENDPOINT",
                "AUTHENTICATION_FAILED": "TEXT-LONG-TEST-AUTH",
                "PERMISSION_DENIED": "TEXT-LONG-TEST-AUTH",
            }
            result = self._error_result(started, mapped, error_code=code_map.get(raw_code, raw_code), retryable=False)
            result.details["raw_error_code"] = raw_code
            return result

    def generate(self, prompt: str, temperature: float = 0.8, max_tokens: int = 3000) -> str:
        """Formal article generation — always uses response_format='none' for Markdown output."""
        return self.generate_article(ArticleGenerationRequest(prompt, temperature, max_tokens, "none"))

    def generate_article(self, request: ArticleGenerationRequest) -> str:
        api_key = str(self.profile.get("api_key") or "")
        if not api_key and str(self.profile.get("auth_type") or "bearer").lower() != "none":
            raise ProviderError("MODEL_NOT_CONFIGURED", "text model API key is missing")
        response_format = request.response_format or "none"
        messages: list[dict[str, str]] = [
            {"role": "system", "content": "You are a careful Chinese news writer. Separate facts from analysis."},
            {"role": "user", "content": request.prompt},
        ]
        content, diagnostic = self._request_text(
            messages=messages,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            response_format=response_format,
        )
        self.last_diagnostic = diagnostic
        return content


def parse_json_response(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    candidates = [raw, _strip_code_block(raw)]
    extracted = _first_json_object(candidates[-1] or raw)
    if extracted:
        candidates.append(extracted)
    seen: set[str] = set()
    for candidate in candidates:
        cleaned = str(candidate or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        try:
            value = json.loads(cleaned)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            raise ProviderError("INVALID_RESPONSE", "text model JSON root must be an object")
        return value
    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError("INVALID_RESPONSE", "text model response is not valid JSON") from exc
    raise ProviderError("INVALID_RESPONSE", "text model JSON root must be an object")
