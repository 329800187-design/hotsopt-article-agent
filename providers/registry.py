from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


Capability = Literal["text", "image"]


@dataclass(frozen=True)
class ProviderProfile:
    provider_id: str
    display_name: str
    capability: Capability
    base_url_template: str
    endpoint_strategy: str
    auth_strategy: str
    default_model: str
    models_discovery_strategy: str
    request_adapter: str
    response_adapter: str
    supports_sync: bool
    supports_async_polling: bool
    polling_strategy: str = "none"
    api_format: str = "openai_compatible"
    default_size: str = ""

    def to_runtime_profile(self) -> dict[str, Any]:
        profile = {
            "provider_id": self.provider_id,
            "name": self.display_name,
            "base_url": self.base_url_template,
            "endpoint": self.endpoint_strategy,
            "auth_type": self.auth_strategy,
            "model": self.default_model,
            "api_format": self.api_format,
            "request_adapter": self.request_adapter,
            "response_adapter": self.response_adapter,
            "sync_or_async": "async" if self.supports_async_polling and not self.supports_sync else "sync",
            "polling_strategy": self.polling_strategy,
        }
        if self.default_size:
            profile["size"] = self.default_size
        return profile

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_PROFILES: tuple[ProviderProfile, ...] = (
    ProviderProfile(
        provider_id="openai_compatible_text",
        display_name="OpenAI 兼容",
        capability="text",
        base_url_template="https://api.openai.com/v1",
        endpoint_strategy="/chat/completions",
        auth_strategy="bearer",
        default_model="gpt-4o-mini",
        models_discovery_strategy="openai_models",
        request_adapter="openai_chat_completions",
        response_adapter="openai_chat_or_markdown",
        supports_sync=True,
        supports_async_polling=False,
    ),
    ProviderProfile(
        provider_id="deepseek_text",
        display_name="DeepSeek",
        capability="text",
        base_url_template="https://api.deepseek.com/v1",
        endpoint_strategy="/chat/completions",
        auth_strategy="bearer",
        default_model="deepseek-chat",
        models_discovery_strategy="openai_models",
        request_adapter="openai_chat_completions",
        response_adapter="openai_chat_or_markdown",
        supports_sync=True,
        supports_async_polling=False,
    ),
    ProviderProfile(
        provider_id="zhipu_glm_text",
        display_name="智谱 GLM",
        capability="text",
        base_url_template="https://open.bigmodel.cn/api/paas/v4",
        endpoint_strategy="/chat/completions",
        auth_strategy="bearer",
        default_model="glm-4.5",
        models_discovery_strategy="openai_models",
        request_adapter="openai_chat_completions",
        response_adapter="openai_chat_or_markdown",
        supports_sync=True,
        supports_async_polling=False,
    ),
    ProviderProfile(
        provider_id="dashscope_text",
        display_name="阿里云百炼",
        capability="text",
        base_url_template="https://dashscope.aliyuncs.com/compatible-mode/v1",
        endpoint_strategy="/chat/completions",
        auth_strategy="bearer",
        default_model="qwen-plus",
        models_discovery_strategy="openai_models",
        request_adapter="openai_chat_completions",
        response_adapter="openai_chat_or_markdown",
        supports_sync=True,
        supports_async_polling=False,
    ),
    ProviderProfile(
        provider_id="volcengine_text",
        display_name="火山引擎",
        capability="text",
        base_url_template="https://ark.cn-beijing.volces.com/api/v3",
        endpoint_strategy="/chat/completions",
        auth_strategy="bearer",
        default_model="doubao-seed-1-6-250615",
        models_discovery_strategy="openai_models",
        request_adapter="openai_chat_completions",
        response_adapter="openai_chat_or_markdown",
        supports_sync=True,
        supports_async_polling=False,
    ),
    ProviderProfile(
        provider_id="custom_text_proxy",
        display_name="自定义",
        capability="text",
        base_url_template="",
        endpoint_strategy="/chat/completions",
        auth_strategy="bearer",
        default_model="",
        models_discovery_strategy="manual_or_openai_models",
        request_adapter="openai_chat_completions",
        response_adapter="openai_chat_or_markdown",
        supports_sync=True,
        supports_async_polling=False,
    ),
    ProviderProfile(
        provider_id="openai_compatible_image",
        display_name="OpenAI 兼容",
        capability="image",
        base_url_template="https://api.openai.com/v1",
        endpoint_strategy="/images/generations",
        auth_strategy="bearer",
        default_model="gpt-image-1",
        models_discovery_strategy="openai_models",
        request_adapter="openai_images_generations",
        response_adapter="image_url_b64_data_uri",
        supports_sync=True,
        supports_async_polling=False,
        default_size="1024x1024",
    ),
    ProviderProfile(
        provider_id="dashscope_image",
        display_name="阿里云百炼",
        capability="image",
        base_url_template="https://dashscope.aliyuncs.com",
        endpoint_strategy="/api/v1/services/aigc/multimodal-generation/generation",
        auth_strategy="bearer",
        default_model="qwen-image-2.0-pro",
        models_discovery_strategy="manual_or_openai_models",
        request_adapter="dashscope_multimodal_generation",
        response_adapter="dashscope_image_or_openai_image",
        supports_sync=True,
        supports_async_polling=False,
        api_format="dashscope_native",
        default_size="1024x1024",
    ),
    ProviderProfile(
        provider_id="volcengine_image",
        display_name="火山引擎",
        capability="image",
        base_url_template="https://ark.cn-beijing.volces.com/api/v3",
        endpoint_strategy="/images/generations",
        auth_strategy="bearer",
        default_model="doubao-seedream-4-0-250828",
        models_discovery_strategy="openai_models",
        request_adapter="openai_images_generations",
        response_adapter="image_url_b64_data_uri",
        supports_sync=True,
        supports_async_polling=False,
        default_size="1024x1024",
    ),
    ProviderProfile(
        provider_id="zhipu_image",
        display_name="智谱 GLM",
        capability="image",
        base_url_template="https://open.bigmodel.cn/api/paas/v4",
        endpoint_strategy="/images/generations",
        auth_strategy="bearer",
        default_model="cogview-4-250304",
        models_discovery_strategy="openai_models",
        request_adapter="openai_images_generations",
        response_adapter="image_url_b64_data_uri",
        supports_sync=True,
        supports_async_polling=False,
        default_size="1024x1024",
    ),
    ProviderProfile(
        provider_id="custom_image_proxy",
        display_name="自定义",
        capability="image",
        base_url_template="",
        endpoint_strategy="/images/generations",
        auth_strategy="bearer",
        default_model="",
        models_discovery_strategy="manual_or_openai_models",
        request_adapter="openai_images_generations",
        response_adapter="image_url_b64_data_uri",
        supports_sync=True,
        supports_async_polling=True,
        polling_strategy="generic_task_status",
        default_size="1024x1024",
    ),
)


def all_profiles() -> list[ProviderProfile]:
    return list(_PROFILES)


def profiles_for(capability: Capability) -> list[ProviderProfile]:
    return [profile for profile in _PROFILES if profile.capability == capability]


def profile_by_display_name(capability: Capability, display_name: str) -> ProviderProfile | None:
    for profile in profiles_for(capability):
        if profile.display_name == display_name:
            return profile
    return None


def default_profile(capability: Capability) -> ProviderProfile:
    for profile in profiles_for(capability):
        if profile.display_name == "OpenAI 兼容":
            return profile
    return profiles_for(capability)[0]


def ui_presets() -> dict[str, dict[str, dict[str, Any]]]:
    names = sorted({profile.display_name for profile in _PROFILES if profile.display_name != "自定义"})
    names.append("自定义")
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for name in names:
        result[name] = {"text": {}, "image": {}}
        if name == "自定义":
            continue
        for capability in ("text", "image"):
            profile = profile_by_display_name(capability, name)
            if profile:
                result[name][capability] = profile.to_runtime_profile()
    return result
