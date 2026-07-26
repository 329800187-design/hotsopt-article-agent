from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient


APP_SOURCE = Path("ui/rc1_app.py").read_text(encoding="utf-8")
API_SOURCE = Path("api.py").read_text(encoding="utf-8")


def _mock_license(monkeypatch) -> None:
    import modules.license_service as license_service

    monkeypatch.setenv("HOTSPOT_ALLOW_UNAUTHENTICATED_TEST_API", "1")
    monkeypatch.setattr(license_service, "license_allows_generation", lambda feature=None: (True, {"valid": True}))


def _discover_with_settings(monkeypatch, settings: dict[str, Any], path: str, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    import api
    from providers import model_discovery

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = str(request.headers.get("Authorization") or "")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"id": "text-model-a"}, {"id": "text-model-b"}, {"id": "image-model-a"}, {"id": "image-model-b"}]})

    _mock_license(monkeypatch)
    monkeypatch.setattr(api, "load_settings", lambda: settings)
    monkeypatch.setattr(model_discovery, "create_http_client", lambda *_args, **_kwargs: httpx.Client(transport=httpx.MockTransport(handler)))
    body = TestClient(api.app).post(path, json=payload).json()
    return body, seen


def test_SAVED_TEXT_KEY_TEXT_DISCOVERY_PASS(monkeypatch):
    settings = {"text_profile": {"api_key": "TEXT-SAVED-KEY", "base_url": "https://text.example/v1"}, "image_profile": {"api_key": "IMAGE-SAVED-KEY"}, "network": {}}
    body, seen = _discover_with_settings(monkeypatch, settings, "/api/models/text/discover", {"profile_kind": "text", "profile": {"api_key": "", "base_url": "https://text.example/v1"}})
    assert body["success"] is True
    assert seen["authorization"] == "Bearer TEXT-SAVED-KEY"


def test_SAVED_IMAGE_KEY_IMAGE_DISCOVERY_PASS(monkeypatch):
    settings = {"text_profile": {"api_key": "TEXT-SAVED-KEY"}, "image_profile": {"api_key": "IMAGE-SAVED-KEY", "base_url": "https://image.example/v1"}, "network": {}}
    body, seen = _discover_with_settings(monkeypatch, settings, "/api/models/image/discover", {"profile_kind": "image", "profile": {"api_key": "", "base_url": "https://image.example/v1"}})
    assert body["success"] is True
    assert seen["authorization"] == "Bearer IMAGE-SAVED-KEY"


def test_IMAGE_DISCOVERY_NEVER_USES_TEXT_KEY_PASS(monkeypatch):
    settings = {"text_profile": {"api_key": "TEXT-SAVED-KEY"}, "image_profile": {"api_key": "IMAGE-SAVED-KEY", "base_url": "https://image.example/v1"}, "network": {}}
    _body, seen = _discover_with_settings(monkeypatch, settings, "/api/models/image/discover", {"profile_kind": "image", "profile": {"api_key": "", "base_url": "https://image.example/v1"}})
    assert seen["authorization"] != "Bearer TEXT-SAVED-KEY"
    assert seen["authorization"] == "Bearer IMAGE-SAVED-KEY"


def test_TEXT_DISCOVERY_NEVER_USES_IMAGE_KEY_PASS(monkeypatch):
    settings = {"text_profile": {"api_key": "TEXT-SAVED-KEY", "base_url": "https://text.example/v1"}, "image_profile": {"api_key": "IMAGE-SAVED-KEY"}, "network": {}}
    _body, seen = _discover_with_settings(monkeypatch, settings, "/api/models/text/discover", {"profile_kind": "text", "profile": {"api_key": "", "base_url": "https://text.example/v1"}})
    assert seen["authorization"] != "Bearer IMAGE-SAVED-KEY"
    assert seen["authorization"] == "Bearer TEXT-SAVED-KEY"


def test_CURRENT_IMAGE_KEY_OVERRIDES_SAVED_PASS(monkeypatch):
    settings = {"text_profile": {"api_key": "TEXT-SAVED-KEY"}, "image_profile": {"api_key": "IMAGE-SAVED-KEY", "base_url": "https://image.example/v1"}, "network": {}}
    _body, seen = _discover_with_settings(monkeypatch, settings, "/api/models/image/discover", {"profile_kind": "image", "profile": {"api_key": "IMAGE-NEW-KEY", "base_url": "https://image.example/v1"}})
    assert seen["authorization"] == "Bearer IMAGE-NEW-KEY"


def test_CURRENT_TEXT_KEY_OVERRIDES_SAVED_PASS(monkeypatch):
    settings = {"text_profile": {"api_key": "TEXT-SAVED-KEY", "base_url": "https://text.example/v1"}, "image_profile": {"api_key": "IMAGE-SAVED-KEY"}, "network": {}}
    _body, seen = _discover_with_settings(monkeypatch, settings, "/api/models/text/discover", {"profile_kind": "text", "profile": {"api_key": "TEXT-NEW-KEY", "base_url": "https://text.example/v1"}})
    assert seen["authorization"] == "Bearer TEXT-NEW-KEY"


def test_TEXT_SECOND_DROPDOWN_MODEL_SAVE_PASS():
    assert 'key="rc132_text_model_selected"' in APP_SOURCE
    assert 'values.update({"model": final_text_model' in APP_SOURCE
    assert 'return selected' in APP_SOURCE


def test_IMAGE_SECOND_DROPDOWN_MODEL_SAVE_PASS():
    assert 'key="rc132_image_model_selected"' in APP_SOURCE
    assert 'values.update({"model": final_image_model' in APP_SOURCE
    assert 'return selected' in APP_SOURCE


def test_TEXT_MODEL_PERSISTS_AFTER_REOPEN_PASS():
    assert "saved_text_model" in APP_SOURCE
    assert 'st.session_state.setdefault(selected_key, saved_model)' in APP_SOURCE
    assert 'st.session_state.setdefault(manual_key, saved_model)' in APP_SOURCE


def test_IMAGE_MODEL_PERSISTS_AFTER_REOPEN_PASS():
    assert "saved_image_model" in APP_SOURCE
    assert 'st.session_state.setdefault(selected_key, saved_model)' in APP_SOURCE
    assert 'st.session_state.setdefault(manual_key, saved_model)' in APP_SOURCE


def test_MANUAL_MODEL_MODE_PASS():
    assert "手动填写模型名称" in APP_SOURCE
    assert 'key="rc132_text_model_manual"' in APP_SOURCE
    assert 'key="rc132_image_model_manual"' in APP_SOURCE


def test_DROPDOWN_NOT_OVERRIDDEN_BY_MANUAL_PASS():
    assert 'if mode == "从检测结果中选择":' in APP_SOURCE
    assert 'return selected' in APP_SOURCE
    assert 'return str(st.session_state.get(f"rc132_{kind}_model_manual")' in APP_SOURCE
    assert 'text_model = st.text_input("手动填写文本模型名称"' not in APP_SOURCE
    assert 'image_model = st.text_input("手动填写图片模型名称"' not in APP_SOURCE


def test_DISCOVER_API_PROFILE_KIND_PASS():
    assert 'profile_kind: Literal["text", "image"]' in API_SOURCE
    assert '@app.post("/api/models/text/discover")' in API_SOURCE
    assert '@app.post("/api/models/image/discover")' in API_SOURCE
    assert 'profile_name = "image_profile" if profile_kind == "image" else "text_profile"' in API_SOURCE
