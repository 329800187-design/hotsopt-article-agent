from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient


APP_SOURCE = Path("ui/rc1_app.py").read_text(encoding="utf-8")


def test_TEXT_MODEL_DETECT_NO_SESSION_STATE_ERROR_PASS():
    assert 'st.session_state["rc132_text_model"] = ' not in APP_SOURCE
    assert 'key="rc132_text_model_selected"' not in APP_SOURCE
    assert 'key="rc132_text_model_manual"' not in APP_SOURCE
    assert "rc132_pending_text_model" in APP_SOURCE


def test_IMAGE_MODEL_DETECT_NO_SESSION_STATE_ERROR_PASS():
    assert 'st.session_state["rc132_image_model"] = ' not in APP_SOURCE
    assert 'key="rc132_image_model_selected"' in APP_SOURCE
    assert 'key="rc132_image_model_manual"' in APP_SOURCE
    assert "rc132_pending_image_model" in APP_SOURCE


def test_TEXT_MODEL_SELECT_AFTER_DETECT_PASS():
    assert "测试文本接口" in APP_SOURCE
    assert "/models/text/test" in APP_SOURCE
    assert "已自动匹配正文模型" in APP_SOURCE
    assert "文本模型下拉列表" not in APP_SOURCE


def test_IMAGE_MODEL_SELECT_AFTER_DETECT_PASS():
    assert "rc132_image_model_options" in APP_SOURCE
    assert "图片模型下拉列表" in APP_SOURCE
    assert "测试图片模型" in APP_SOURCE


def test_RERUN_STATE_STABLE_PASS():
    assert "apply_pending_model" in APP_SOURCE
    assert "st.session_state.pop(pending_key" in APP_SOURCE
    assert "st.rerun()" in APP_SOURCE


def test_CURRENT_UNSAVED_TEXT_FORM_DETECT_PASS(monkeypatch):
    import api
    import modules.license_service as license_service
    from providers import model_discovery

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"data": [{"id": "new-text-model"}]})

    monkeypatch.setenv("HOTSPOT_ALLOW_UNAUTHENTICATED_TEST_API", "1")
    monkeypatch.setattr(license_service, "license_allows_generation", lambda feature=None: (True, {"valid": True}))
    monkeypatch.setattr(api, "load_settings", lambda: {"text_profile": {"api_key": "Key-A", "base_url": "https://old.local/v1"}, "network": {}})
    monkeypatch.setattr(model_discovery, "create_http_client", lambda *_args, **_kwargs: httpx.Client(transport=httpx.MockTransport(handler)))
    body = TestClient(api.app).post("/api/models/text/discover", json={"profile_kind": "text", "profile": {"api_key": "Key-B", "base_url": "https://new.local/v1", "endpoint": "/chat/completions"}}).json()
    assert body["success"] is True
    assert seen["url"] == "https://new.local/v1/models"
    assert seen["authorization"] == "Bearer Key-B"


def test_CURRENT_UNSAVED_IMAGE_FORM_DETECT_PASS(monkeypatch):
    import api
    import modules.license_service as license_service
    from providers import model_discovery

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"data": [{"id": "gpt-image-1"}]})

    monkeypatch.setenv("HOTSPOT_ALLOW_UNAUTHENTICATED_TEST_API", "1")
    monkeypatch.setattr(license_service, "license_allows_generation", lambda feature=None: (True, {"valid": True}))
    monkeypatch.setattr(api, "load_settings", lambda: {"image_profile": {"api_key": "Image-A", "base_url": "https://old-image.local/v1"}, "network": {}})
    monkeypatch.setattr(model_discovery, "create_http_client", lambda *_args, **_kwargs: httpx.Client(transport=httpx.MockTransport(handler)))
    body = TestClient(api.app).post("/api/models/image/discover", json={"profile_kind": "image", "profile": {"api_key": "Image-B", "base_url": "https://new-image.local/v1", "endpoint": "/images/generations"}}).json()
    assert body["success"] is True
    assert seen["url"] == "https://new-image.local/v1/models"
    assert seen["authorization"] == "Bearer Image-B"


def test_NO_STALE_KEY_DETECT_PASS(monkeypatch):
    test_CURRENT_UNSAVED_TEXT_FORM_DETECT_PASS(monkeypatch)


def test_OPENAI_COMPATIBLE_MODELS_PATH_NO_DOUBLE_V1_PASS(monkeypatch):
    from providers import model_discovery

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}]})

    monkeypatch.setattr(model_discovery, "create_http_client", lambda *_args, **_kwargs: httpx.Client(transport=httpx.MockTransport(handler)))
    result = model_discovery.discover_models({"api_key": "Key-B", "base_url": "https://api.ezlinkapi.top/v1"})
    assert result["success"] is True
    assert seen["path"] == "/v1/models"


def test_MODEL_ACTION_ERROR_LOCALIZED_PASS():
    assert "MODEL-LIST-401" in APP_SOURCE
    assert "MODEL-LIST-403" in APP_SOURCE
    assert "MODEL-LIST-404" in APP_SOURCE
    assert "MODEL-LIST-TIMEOUT" in APP_SOURCE


def test_NO_GUI_NAV_ERROR_FOR_MODEL_ACTION_PASS():
    model_page = APP_SOURCE.split("def _settings_page", 1)[1].split("def _license_candidates", 1)[0]
    assert "GUI-NAV-001" not in model_page
    assert "_model_list_error_message" in model_page


def test_MODEL_HTTP_ERROR_CHINESE_PASS():
    assert "API Key无效或没有访问权限" in APP_SOURCE
    assert "当前密钥无权访问模型列表" in APP_SOURCE
    assert "请求过于频繁" in APP_SOURCE


def test_SAVED_API_KEY_MASKED_PASS():
    assert 'type="password"' in APP_SOURCE
    assert "_mask_api_key" in APP_SOURCE
    assert "完整密钥不会回填显示" in APP_SOURCE


def test_API_KEY_NOT_RENDERED_AFTER_SAVE_PASS():
    assert 'value=""' in APP_SOURCE
    assert 'placeholder="已保存，留空则继续使用"' in APP_SOURCE
    assert "已保存图片 Key：" in APP_SOURCE and "已保存文本 Key：" in APP_SOURCE


def test_API_KEY_NOT_IN_LOG_PASS():
    security_source = Path("modules/security.py").read_text(encoding="utf-8")
    config_source = Path("modules/config_store.py").read_text(encoding="utf-8")
    assert "redact_sensitive_text" in security_source
    assert "api_key" in security_source
    assert "_log_credential_failure" in config_source
    assert "type(error).__name__" in config_source


def test_API_KEY_NOT_IN_ERROR_PASS():
    assert "Bearer <API_KEY>" not in APP_SOURCE
    assert "st.error(_model_list_error_message(_api_error_text(exc)))" in APP_SOURCE


def test_TEXT_IMAGE_KEYS_STILL_SEPARATE_PASS():
    assert "text_profile" in APP_SOURCE and "image_profile" in APP_SOURCE
    assert "rc132_text_key" in APP_SOURCE and "rc132_image_key" in APP_SOURCE
    assert "文本和图片使用同一个API Key" in APP_SOURCE
