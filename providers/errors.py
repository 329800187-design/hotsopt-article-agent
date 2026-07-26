from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from modules.security import redact_sensitive_text


RETRYABLE_ERROR_CODES = {"RATE_LIMITED", "NETWORK_ERROR", "PROXY_ERROR", "TIMEOUT", "PROVIDER_INTERNAL_ERROR"}
NON_RETRYABLE_ERROR_CODES = {
    "MODEL_NOT_CONFIGURED",
    "AUTHENTICATION_FAILED",
    "PERMISSION_DENIED",
    "MODEL_NOT_FOUND",
    "IMAGE_GENERATION_NOT_SUPPORTED",
    "QUOTA_EXCEEDED",
    "INVALID_REQUEST",
    "INVALID_RESPONSE",
    "MODEL_OUTPUT_INVALID",
    "UNSUPPORTED_RESPONSE_FORMAT",
    "TASK_CANCELLED",
    "TEXT_MODEL_NOT_VERIFIED",
}


USER_FACING_ERROR_MESSAGES = {
    "AUTHENTICATION_FAILED": "API Key 无效，请检查是否复制完整。\n错误码：AUTHENTICATION_FAILED",
    "PERMISSION_DENIED": "API Key 有效，但没有当前服务或模型权限。\n错误码：PERMISSION_DENIED",
    "MODEL_NOT_FOUND": "当前文本模型不可用。\n可能是模型名称错误、服务商已下线该模型，或当前中转暂时没有可用通道。\n请进入“模型设置”重新获取或填写可用模型，测试成功后再重试。\n错误码：MODEL_NOT_FOUND",
    "NO_AVAILABLE_CHANNEL": "当前 Key 没有分配该模型通道。\n错误码：NO_AVAILABLE_CHANNEL",
    "IMAGE_GENERATION_NOT_SUPPORTED": "当前接口不支持图片生成。",
    "INVALID_REQUEST": "当前接口不支持本软件使用的请求格式。\n错误码：INVALID_REQUEST",
    "RATE_LIMITED": "请求过于频繁，请稍后重试。\n错误码：RATE_LIMITED",
    "QUOTA_EXCEEDED": "账户余额或调用额度不足。\n错误码：QUOTA_EXCEEDED",
    "INSUFFICIENT_BALANCE": "账户余额或调用额度不足。\n错误码：INSUFFICIENT_BALANCE",
    "TIMEOUT": "接口响应超时，请稍后重试。\n错误码：TIMEOUT",
    "DNS_ERROR": "无法解析当前接口域名。\n错误码：DNS_ERROR",
    "TLS_ERROR": "安全连接失败，请检查接口地址。\n错误码：TLS_ERROR",
    "PROXY_ERROR": "代理连接失败",
    "NETWORK_ERROR": "无法连接当前接口。\n错误码：NETWORK_ERROR",
    "PROVIDER_INTERNAL_ERROR": "模型服务暂时异常",
    "ENDPOINT_NOT_FOUND": "当前访问地址或接口路径不正确。\n错误码：ENDPOINT_NOT_FOUND",
    "MODEL_LIST_UNSUPPORTED": "当前接口不支持免费读取模型列表。\n可以继续进行兼容性检测，兼容性检测可能产生极少量文本费用。\n错误码：MODEL_LIST_UNSUPPORTED",
    "TEXT-LONG-TEST-TIMEOUT": "文章生成能力测试超时，当前模型或中转可能不适合长文生成。\n错误码：TEXT-LONG-TEST-TIMEOUT",
    "TEXT-LONG-TEST-FORMAT": "文章生成能力测试返回结构不符合要求，请更换模型或关闭不兼容的中转格式。\n错误码：TEXT-LONG-TEST-FORMAT",
    "TEXT-LONG-TEST-MODEL": "文章生成能力测试未找到当前文本模型。\n错误码：TEXT-LONG-TEST-MODEL",
    "TEXT-LONG-TEST-ENDPOINT": "文章生成能力测试接口路径或结构化输出格式不兼容。\n错误码：TEXT-LONG-TEST-ENDPOINT",
    "TEXT-LONG-TEST-AUTH": "文章生成能力测试鉴权失败，请检查文本 API Key。\n错误码：TEXT-LONG-TEST-AUTH",
    "FEATURE_NOT_AVAILABLE_IN_CURRENT_EDITION": "当前交付版本暂未开放该高级功能。\n错误码：FEATURE_NOT_AVAILABLE_IN_CURRENT_EDITION",
    "TEXT_MODEL_NOT_VERIFIED": "当前文本模型尚未测试。\n请先在“模型设置”中完成测试，再重新写文章。\n错误码：TEXT_MODEL_NOT_VERIFIED",
}


def user_facing_error_message(code: str, fallback: str = "") -> str:
    return USER_FACING_ERROR_MESSAGES.get(str(code), fallback or "网络连接异常")


def is_retryable_error(code: str) -> bool:
    return str(code) in RETRYABLE_ERROR_CODES


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def _quota_message(message: str) -> bool:
    lowered = message.lower()
    return any(token in lowered for token in ("quota", "insufficient_quota", "balance", "余额不足", "配额不足"))


def map_provider_exception(exc: Exception, response: httpx.Response | None = None) -> Exception:
    from providers.text_provider import ProviderError

    if isinstance(exc, ProviderError):
        return exc
    message = redact_sensitive_text(str(exc))
    if _quota_message(message):
        return ProviderError("QUOTA_EXCEEDED", "provider quota or balance is insufficient")
    if isinstance(exc, httpx.ProxyError):
        return ProviderError("PROXY_ERROR", message)
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError("TIMEOUT", "provider request timed out")
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        lowered = message.lower()
        if any(token in lowered for token in ("getaddrinfo", "dns", "name resolution", "name or service not known", "nodename nor servname")):
            return ProviderError("DNS_ERROR", "provider host cannot be resolved")
        if any(token in lowered for token in ("ssl", "tls", "certificate", "certifi")):
            return ProviderError("TLS_ERROR", "provider TLS handshake failed")
        return ProviderError("NETWORK_ERROR", message)
    if isinstance(exc, httpx.HTTPStatusError):
        status = response.status_code if response is not None else getattr(exc.response, "status_code", None)
        if response is not None:
            try:
                body = response.json()
                error = body.get("error", {}) if isinstance(body, dict) else {}
                provider_code = str(error.get("code") or "").lower() if isinstance(error, dict) else ""
                provider_message = str(error.get("message") or "").lower() if isinstance(error, dict) else ""
                if provider_code in {"model_not_found", "model-not-found"}:
                    return ProviderError("MODEL_NOT_FOUND", "provider model is not available")
                if "no available channel" in provider_message or "无可用通道" in provider_message:
                    return ProviderError("NO_AVAILABLE_CHANNEL", "provider has no available channel for this model")
                if provider_code in {"invalid_api_key", "authentication_error", "unauthorized"}:
                    return ProviderError("AUTHENTICATION_FAILED", "provider authentication failed")
            except (ValueError, TypeError, AttributeError):
                pass
        mapping = {
            400: "INVALID_REQUEST",
            401: "AUTHENTICATION_FAILED",
            402: "QUOTA_EXCEEDED",
            403: "PERMISSION_DENIED",
            404: "MODEL_NOT_FOUND",
            408: "TIMEOUT",
            409: "PROVIDER_CONFLICT",
            413: "REQUEST_TOO_LARGE",
            422: "INVALID_REQUEST",
            429: "RATE_LIMITED",
            500: "PROVIDER_INTERNAL_ERROR",
            502: "PROVIDER_INTERNAL_ERROR",
            503: "PROVIDER_INTERNAL_ERROR",
            504: "PROVIDER_INTERNAL_ERROR",
        }
        return ProviderError(mapping.get(status, "NETWORK_ERROR"), f"provider returned HTTP {status}")
    if isinstance(exc, (ValueError, TypeError, KeyError, IndexError, AttributeError, binascii.Error, json.JSONDecodeError)):
        return ProviderError("INVALID_RESPONSE", "provider response format is invalid")
    return ProviderError("PROVIDER_INTERNAL_ERROR", message)
