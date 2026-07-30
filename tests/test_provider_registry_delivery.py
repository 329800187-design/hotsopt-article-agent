from __future__ import annotations

from providers.registry import all_profiles, default_profile, profiles_for, ui_presets


REQUIRED_FIELDS = {
    "provider_id",
    "display_name",
    "capability",
    "base_url_template",
    "endpoint_strategy",
    "auth_strategy",
    "default_model",
    "models_discovery_strategy",
    "request_adapter",
    "response_adapter",
    "supports_sync",
    "supports_async_polling",
    "polling_strategy",
}


def test_registry_profiles_have_required_delivery_fields():
    assert all_profiles()
    for profile in all_profiles():
        payload = profile.to_dict()
        assert REQUIRED_FIELDS <= set(payload)
        assert payload["capability"] in {"text", "image"}
        assert payload["display_name"]
        assert payload["provider_id"]
        assert isinstance(payload["supports_sync"], bool)
        assert isinstance(payload["supports_async_polling"], bool)
        assert payload["polling_strategy"]


def test_registry_supports_required_text_providers():
    names = {profile.display_name for profile in profiles_for("text")}
    assert {"OpenAI 兼容", "智谱 GLM", "阿里云百炼", "火山引擎", "DeepSeek", "自定义"} <= names


def test_registry_supports_required_image_providers():
    names = {profile.display_name for profile in profiles_for("image")}
    assert {"OpenAI 兼容", "智谱 GLM", "阿里云百炼", "火山引擎", "自定义"} <= names


def test_ui_presets_are_generated_from_registry():
    presets = ui_presets()
    assert presets["OpenAI 兼容"]["text"]["base_url"] == default_profile("text").base_url_template
    assert presets["OpenAI 兼容"]["image"]["endpoint"] == default_profile("image").endpoint_strategy
    assert presets["自定义"]["text"] == {}
    assert presets["自定义"]["image"] == {}


def test_default_settings_use_registry_profiles():
    from modules.config_store import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["text_profile"]["provider_id"] == default_profile("text").provider_id
    assert DEFAULT_SETTINGS["image_profile"]["provider_id"] == default_profile("image").provider_id
