from __future__ import annotations

import json
from typing import Any

from providers import text_model_resolver as resolver
from providers.text_provider import ProviderError


def _profile(model: str = "deepseek-v4-flash") -> dict[str, Any]:
    return {
        "name": "deepseek",
        "api_key": "Key-A",
        "base_url": "https://api.deepseek.com",
        "endpoint": "/chat/completions",
        "model": model,
    }


def _settings(**overrides: Any) -> dict[str, Any]:
    value = {"text_profile": _profile(), "network": {}}
    value.update(overrides)
    return value


def test_RESOLVER_PROBES_DISCOVERED_TEXT_MODEL_AFTER_REASONING_ONLY(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(resolver, "discover_models", lambda *_args, **_kwargs: {"success": True, "text_models": ["deepseek-chat"], "image_models": [], "other_models": []})

    def fake_probe(_profile: dict[str, Any], model_id: str, _network: dict[str, Any] | None):
        calls.append(model_id)
        if model_id == "deepseek-v4-flash":
            return False, {"reasoning_content_present": True, "content_present": False}, "MODEL_OUTPUT_REASONING_ONLY", "thinking only"
        return True, {"parser_mode": "openai_chat", "content_present": True}, "", ""

    monkeypatch.setattr(resolver, "_probe_candidate", fake_probe)
    result = resolver.resolve_usable_text_model(_settings(), _profile(), network_settings={}, force_refresh=True)
    assert result["success"] is True
    assert result["resolved_model"] == "deepseek-chat"
    assert calls[:2] == ["deepseek-v4-flash", "deepseek-chat"]
    assert result["probes"][0]["provider_error_code"] == "MODEL_OUTPUT_REASONING_ONLY"


def test_RESOLVER_NEVER_TREATS_REASONING_ONLY_AS_SUCCESS(monkeypatch):
    monkeypatch.setattr(resolver, "discover_models", lambda *_args, **_kwargs: {"success": True, "text_models": ["reasoning-only"], "image_models": [], "other_models": []})
    monkeypatch.setattr(resolver, "_probe_candidate", lambda *_args, **_kwargs: (False, {"reasoning_content_present": True}, "MODEL_OUTPUT_REASONING_ONLY", "thinking only"))
    result = resolver.resolve_usable_text_model(_settings(), _profile("reasoning-only"), network_settings={}, force_refresh=True)
    assert result["success"] is False
    assert result["error_code"] == "MODEL_OUTPUT_REASONING_ONLY"
    assert result["resolved_model"] == ""


def test_RESOLVER_FILTERS_IMAGE_AND_EMBEDDING_MODELS(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        resolver,
        "discover_models",
        lambda *_args, **_kwargs: {"success": True, "text_models": ["gpt-image-1", "text-embedding-3-small", "qwen-plus"], "image_models": [], "other_models": []},
    )

    def fake_probe(_profile: dict[str, Any], model_id: str, _network: dict[str, Any] | None):
        seen.append(model_id)
        return True, {"parser_mode": "openai_chat"}, "", ""

    monkeypatch.setattr(resolver, "_probe_candidate", fake_probe)
    custom_profile = {**_profile(""), "name": "自定义", "base_url": "https://mock.local/v1"}
    result = resolver.resolve_usable_text_model(_settings(text_profile=custom_profile), custom_profile, network_settings={}, force_refresh=True)
    assert result["success"] is True
    assert result["resolved_model"] == "qwen-plus"
    assert "gpt-image-1" not in seen
    assert "text-embedding-3-small" not in seen


def test_RESOLVER_USES_VERIFIED_CACHE_WITHOUT_PROBING(monkeypatch):
    settings = _settings(
        resolved_text_model="usable-chat",
        resolved_text_provider="deepseek",
        resolved_text_base_url_hash=resolver.base_url_hash("https://api.deepseek.com", "/chat/completions"),
        resolved_text_capability_status="verified",
        resolved_text_verified_at="2026-08-02T00:00:00+00:00",
    )
    monkeypatch.setattr(resolver, "_probe_candidate", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("probe should not run")))
    result = resolver.resolve_usable_text_model(settings, _profile(), network_settings={}, force_refresh=False)
    assert result["success"] is True
    assert result["resolved_model"] == "usable-chat"
    assert result["probe_status"] == "cached"


def test_RESOLVER_INVALIDATES_CACHE_WHEN_BASE_URL_CHANGES(monkeypatch):
    settings = _settings(
        resolved_text_model="usable-chat",
        resolved_text_provider="deepseek",
        resolved_text_base_url_hash=resolver.base_url_hash("https://old.deepseek.com", "/chat/completions"),
        resolved_text_capability_status="verified",
    )
    monkeypatch.setattr(resolver, "discover_models", lambda *_args, **_kwargs: {"success": True, "text_models": ["deepseek-chat"], "image_models": [], "other_models": []})
    monkeypatch.setattr(resolver, "_probe_candidate", lambda *_args, **_kwargs: (True, {"parser_mode": "openai_chat"}, "", ""))
    result = resolver.resolve_usable_text_model(settings, _profile(), network_settings={}, force_refresh=False)
    assert result["success"] is True
    assert result["probe_status"] == "verified"


def test_RESOLVER_FALLS_BACK_TO_PROVIDER_CANDIDATES_WHEN_MODELS_LIST_UNAVAILABLE(monkeypatch):
    monkeypatch.setattr(resolver, "discover_models", lambda *_args, **_kwargs: {"success": False, "error_code": "MODEL_LIST_UNSUPPORTED"})
    monkeypatch.setattr(
        resolver,
        "_probe_candidate",
        lambda _profile, model_id, _network: (model_id == "deepseek-chat", {"parser_mode": "openai_chat"}, "" if model_id == "deepseek-chat" else "MODEL_NOT_FOUND", ""),
    )
    result = resolver.resolve_usable_text_model(_settings(), _profile("missing-model"), network_settings={}, force_refresh=True)
    assert result["success"] is True
    assert result["resolved_model"] == "deepseek-chat"


def test_RESOLVER_PRESERVES_AUTHENTICATION_FAILURE(monkeypatch):
    monkeypatch.setattr(resolver, "discover_models", lambda *_args, **_kwargs: {"success": False, "error_code": "AUTHENTICATION_FAILED"})
    monkeypatch.setattr(resolver, "_probe_candidate", lambda *_args, **_kwargs: (False, {}, "AUTHENTICATION_FAILED", "bad key"))
    result = resolver.resolve_usable_text_model(_settings(), _profile(), network_settings={}, force_refresh=True)
    assert result["success"] is False
    assert result["error_code"] == "AUTHENTICATION_FAILED"


def test_PERSIST_RESOLVED_MODEL_SAVES_CURRENT_FORM_PROFILE():
    settings = {"text_profile": {"api_key": "Old", "base_url": "https://old.local/v1", "endpoint": "/chat/completions", "model": "old"}}
    resolution = {
        "success": True,
        "provider": "自定义",
        "resolved_model": "usable-chat",
        "base_url_hash": resolver.base_url_hash("https://new.local/v1", "/chat/completions"),
        "verified_at": "2026-08-02T00:00:00+00:00",
        "response_parser_mode": "openai_chat",
        "profile": {"name": "自定义", "api_key": "New", "base_url": "https://new.local/v1", "endpoint": "/chat/completions", "model": "usable-chat"},
    }
    saved = resolver.persist_resolved_text_model(settings, resolution)
    assert saved["text_profile"]["api_key"] == "New"
    assert saved["text_profile"]["base_url"] == "https://new.local/v1"
    assert saved["text_profile"]["model"] == "usable-chat"
    assert saved["resolved_text_capability_status"] == "verified"


def test_CONFIG_SAVE_PRESERVES_FRESH_RESOLUTION_WHEN_NEW_KEY_IS_VERIFIED(monkeypatch, tmp_path):
    from modules import config_store

    current_path = tmp_path / "settings.json"
    current_path.write_text(json.dumps({"text_profile": {"base_url": "https://new.local/v1", "endpoint": "/chat/completions", "credential_ref": "dpapi:old", "has_api_key": True}}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config_store, "SETTINGS_PATH", current_path)
    monkeypatch.setattr(config_store, "save_secret", lambda name, secret: f"dpapi:{name}")
    persisted = config_store._settings_for_persistence(
        {
            "text_profile": {"api_key": "New-Key", "base_url": "https://new.local/v1", "endpoint": "/chat/completions", "model": "usable-chat"},
            "resolved_text_model": "usable-chat",
            "resolved_text_provider": "自定义",
            "resolved_text_base_url_hash": "hash",
            "resolved_text_capability_status": "verified",
            "_preserve_text_resolution_on_save": True,
        }
    )
    assert persisted["resolved_text_model"] == "usable-chat"
    assert persisted["resolved_text_capability_status"] == "verified"
    assert "_preserve_text_resolution_on_save" not in persisted


def test_CONFIG_SAVE_CLEARS_STALE_RESOLUTION_WHEN_KEY_CHANGES(monkeypatch, tmp_path):
    from modules import config_store

    current_path = tmp_path / "settings.json"
    current_path.write_text(json.dumps({"text_profile": {"base_url": "https://new.local/v1", "endpoint": "/chat/completions", "credential_ref": "dpapi:old", "has_api_key": True}}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config_store, "SETTINGS_PATH", current_path)
    monkeypatch.setattr(config_store, "save_secret", lambda name, secret: f"dpapi:{name}")
    persisted = config_store._settings_for_persistence(
        {
            "text_profile": {"api_key": "New-Key", "base_url": "https://new.local/v1", "endpoint": "/chat/completions", "model": "old-chat"},
            "resolved_text_model": "old-chat",
            "resolved_text_provider": "自定义",
            "resolved_text_base_url_hash": "hash",
            "resolved_text_capability_status": "verified",
        }
    )
    assert persisted["resolved_text_model"] is None
    assert persisted["resolved_text_capability_status"] == ""


def test_SINGLE_TASK_USES_RESOLVED_MODEL_FOR_FORMAL_GENERATION(monkeypatch, tmp_path):
    from generation import single_task
    from modules.database import SQLiteStore

    captured: dict[str, str] = {}
    store = SQLiteStore(tmp_path / "db.sqlite")
    topic = {
        "id": "t1",
        "title": "测试热点",
        "summary": "这是一个用于测试的热点摘要。",
        "source": "manual",
        "source_name": "manual",
        "source_url": "",
        "captured_at": "2026-08-02T00:00:00+00:00",
    }
    task = store.create_task("resolved", "multi_topic", [topic], 1, generation_options={"word_count": 800, "image_plan_mode": "none"})

    monkeypatch.setattr(single_task, "resolve_usable_text_model", lambda *_args, **_kwargs: {"success": True, "resolved_model": "usable-chat", "provider": "自定义", "base_url_hash": "hash", "capability": "text_content", "verified_at": "now", "probe_status": "cached", "response_parser_mode": "openai_chat", "model_resolution_source": "cached", "profile": {"api_key": "Key", "base_url": "https://mock.local/v1", "endpoint": "/chat/completions", "model": "usable-chat"}})
    monkeypatch.setattr(single_task, "save_settings", lambda _settings: None)
    monkeypatch.setattr(single_task, "_auto_collect_research", lambda *_args, **_kwargs: {"research_status": "completed", "accepted_source_count": 1, "sources": [{"title": "来源", "url": "https://example.com", "summary": "来源摘要"}]})
    monkeypatch.setattr(single_task, "quality_gate", lambda *_args, **_kwargs: {"status": "passed", "passed": True, "hard_errors": [], "warnings": [], "reasons": [], "metrics": {}})
    monkeypatch.setattr(single_task, "analyze_source_overlap", lambda *_args, **_kwargs: {"status": "passed"})
    monkeypatch.setattr(single_task, "ensure_article_layout", lambda article: article)

    def fake_generate_article(_topic, _angle, _article_type, _style, _word_count, profile, **_kwargs):
        captured["model"] = profile["model"]
        return {"title": "标题", "intro": "导语", "summary": "摘要", "sections": [{"heading": "一、背景", "body": "正文内容" * 120}], "content_markdown": "正文内容" * 120, "source_list": ["https://example.com"]}

    monkeypatch.setattr(single_task, "generate_article", fake_generate_article)
    result = single_task.run_single_task(task, _profile("deepseek-v4-flash"), {"auth_type": "none"}, settings={"network": {}, "text_profile": _profile("deepseek-v4-flash")}, store=store)
    assert result["status"] == "completed", result
    assert captured["model"] == "usable-chat"
    assert result["resolved_text_model"] == "usable-chat"


def test_SINGLE_TASK_RECOVERS_ON_REASONING_ONLY_BY_REFRESHING_MODEL(monkeypatch, tmp_path):
    from generation import single_task
    from modules.database import SQLiteStore

    calls: list[str] = []
    resolutions: list[dict[str, Any]] = [
        {"success": True, "resolved_model": "bad-reasoner", "provider": "自定义", "base_url_hash": "hash1", "capability": "text_content", "verified_at": "now", "probe_status": "cached", "response_parser_mode": "openai_chat", "model_resolution_source": "cached", "profile": {"api_key": "Key", "base_url": "https://mock.local/v1", "endpoint": "/chat/completions", "model": "bad-reasoner"}},
        {"success": True, "resolved_model": "usable-chat", "provider": "自定义", "base_url_hash": "hash2", "capability": "text_content", "verified_at": "now", "probe_status": "verified", "response_parser_mode": "openai_chat", "model_resolution_source": "automatic_recovery", "profile": {"api_key": "Key", "base_url": "https://mock.local/v1", "endpoint": "/chat/completions", "model": "usable-chat"}},
    ]
    store = SQLiteStore(tmp_path / "db.sqlite")
    task = store.create_task("recover", "multi_topic", [{"id": "t1", "title": "测试热点", "summary": "摘要", "source": "manual"}], 1, generation_options={"word_count": 800, "image_plan_mode": "none"})
    monkeypatch.setattr(single_task, "resolve_usable_text_model", lambda *_args, **_kwargs: resolutions.pop(0))
    monkeypatch.setattr(single_task, "save_settings", lambda _settings: None)
    monkeypatch.setattr(single_task, "_auto_collect_research", lambda *_args, **_kwargs: {"research_status": "completed", "accepted_source_count": 1, "sources": [{"title": "来源", "url": "https://example.com"}]})
    monkeypatch.setattr(single_task, "quality_gate", lambda *_args, **_kwargs: {"status": "passed", "passed": True, "hard_errors": [], "warnings": [], "reasons": [], "metrics": {}})
    monkeypatch.setattr(single_task, "analyze_source_overlap", lambda *_args, **_kwargs: {"status": "passed"})
    monkeypatch.setattr(single_task, "ensure_article_layout", lambda article: article)

    def fake_generate_article(_topic, _angle, _article_type, _style, _word_count, profile, **_kwargs):
        calls.append(profile["model"])
        if profile["model"] == "bad-reasoner":
            raise ProviderError("MODEL_OUTPUT_REASONING_ONLY", "thinking only", details={"reasoning_content_present": True})
        return {"title": "标题", "intro": "导语", "summary": "摘要", "sections": [{"heading": "一、背景", "body": "正文内容" * 120}], "content_markdown": "正文内容" * 120, "source_list": ["https://example.com"]}

    monkeypatch.setattr(single_task, "generate_article", fake_generate_article)
    text_profile = {"api_key": "Key", "base_url": "https://mock.local/v1", "endpoint": "/chat/completions", "model": "bad-reasoner"}
    result = single_task.run_single_task(task, text_profile, {"auth_type": "none"}, settings={"network": {}, "text_profile": text_profile}, store=store)
    assert result["status"] == "completed", result
    assert calls == ["bad-reasoner", "usable-chat"]
    assert result["text_model_recovery_result"] == "success"
