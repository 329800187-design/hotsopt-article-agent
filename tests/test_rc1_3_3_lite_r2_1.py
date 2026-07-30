from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest


def _click_home_button(label: str) -> AppTest:
    at = AppTest.from_file("app.py", default_timeout=25)
    at.run()
    assert not at.exception
    button = next(item for item in at.button if item.label == label)
    button.click().run()
    assert not at.exception
    return at


def test_HOME_START_CREATION_NAVIGATION_PASS():
    at = _click_home_button("开始一次创作")
    assert "今日热点" in str(at)


def test_HOME_BROWSE_HOTSPOT_NAVIGATION_PASS():
    at = _click_home_button("浏览今日热点")
    assert "今日热点" in str(at)


def test_HOME_CUSTOM_TOPIC_NAVIGATION_PASS():
    at = _click_home_button("输入自己的话题")
    assert at.session_state["rc1_navigation"] == "◈ 选择话题"
    assert not any(item.label == "话题标题" for item in at.text_input)
    assert "今日热点" in str(at)


def test_NO_STREAMLIT_TRACEBACK_CUSTOMER_UI_PASS():
    source = Path("app.py").read_text(encoding="utf-8")
    assert "GUI-NAV-001" in source
    at = _click_home_button("浏览今日热点")
    rendered = str(at)
    assert "StreamlitAPIException" not in rendered
    assert "Traceback" not in rendered
    assert "session_state.rc1_navigation cannot be modified" not in rendered


def test_MODEL_NAME_OPTIONAL_FOR_BEGINNER_PASS():
    source = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    ordinary = source.split('st.markdown("### 文本接口")', 1)[1].split('with st.expander("高级设置"', 1)[0]
    assert "文本 API Key" in ordinary
    assert "文本 API 地址" in ordinary
    assert "文本模型名称" not in ordinary


def test_AUTO_MODEL_DISCOVERY_PASS(monkeypatch):
    from providers import model_discovery

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        assert request.headers["Authorization"] == "Bearer Key-B"
        return httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}, {"id": "text-embedding-3-small"}, {"id": "gpt-image-1"}]})

    monkeypatch.setattr(model_discovery, "create_http_client", lambda *_args, **_kwargs: httpx.Client(transport=httpx.MockTransport(handler), base_url="https://mock.local"))
    result = model_discovery.discover_models({"api_key": "Key-B", "base_url": "https://mock.local/v1"})
    assert result["success"] is True
    assert result["recommended_text_model"] == "gpt-4o-mini"
    assert result["recommended_image_model"] == "gpt-image-1"


def test_AUTO_TEXT_MODEL_SELECTION_PASS():
    from providers.model_discovery import classify_models

    result = classify_models([{"id": "text-embedding-3-small"}, {"id": "gpt-4o-mini"}])
    assert result["recommended_text_model"] == "gpt-4o-mini"


def test_AUTO_IMAGE_MODEL_SELECTION_PASS():
    from providers.model_discovery import classify_models

    result = classify_models([{"id": "gpt-4o-mini"}, {"id": "gpt-image-1"}])
    assert result["recommended_image_model"] == "gpt-image-1"


def test_NO_HARDCODED_MODEL_DEPENDENCY_PASS():
    source = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    ordinary_text = source.split('st.markdown("### 文本接口")', 1)[1].split('with st.expander("高级设置"', 1)[0]
    ordinary_image = source.split('st.markdown("### 图片接口")', 1)[1].split('with st.expander("高级设置"', 1)[0]
    assert "gpt-5.6-luna" not in ordinary_text
    assert "gpt-image-2" not in ordinary_image


def test_MODELS_ENDPOINT_UNSUPPORTED_FALLBACK_PASS(monkeypatch):
    from providers import model_discovery

    monkeypatch.setattr(model_discovery, "create_http_client", lambda *_args, **_kwargs: httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(404))))
    result = model_discovery.discover_models({"api_key": "key", "base_url": "https://mock.local/v1"})
    assert result["error_code"] == "MODEL_LIST_UNSUPPORTED"
    assert "不支持免费读取模型列表" in result["message"]


def test_SAME_GATEWAY_TEXT_IMAGE_AUTO_ASSIGN_PASS():
    source = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    assert "文本和图片使用同一个API Key" in source
    assert "recommended_text_model" in source and "recommended_image_model" in source


def test_CURRENT_TEXT_FORM_KEY_ACTUALLY_USED_PASS(monkeypatch):
    import api
    from providers import text_provider

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    monkeypatch.setattr(api, "load_settings", lambda: {"text_profile": {"api_key": "Key-A", "base_url": "https://mock.local/v1", "model": "old-model", "endpoint": "/chat/completions"}, "network": {}})
    monkeypatch.setattr(text_provider, "create_http_client", lambda *_args, **_kwargs: httpx.Client(transport=httpx.MockTransport(handler), base_url="https://mock.local"))
    response = TestClient(api.app).post("/api/models/text/test", json={"profile": {"api_key": "Key-B", "base_url": "https://mock.local/v1", "model": "gpt-4o-mini", "endpoint": "/chat/completions"}})
    assert response.json()["success"] is True
    assert seen["authorization"] == "Bearer Key-B"


def test_OLD_TEXT_KEY_NOT_REUSED_PASS(monkeypatch):
    import api
    from providers import text_provider

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("Authorization")
        return httpx.Response(401, json={"error": {"code": "invalid_api_key", "message": "bad key"}})

    monkeypatch.setattr(api, "load_settings", lambda: {"text_profile": {"api_key": "Key-A", "base_url": "https://mock.local/v1", "model": "old-model", "endpoint": "/chat/completions"}, "network": {}})
    monkeypatch.setattr(text_provider, "create_http_client", lambda *_args, **_kwargs: httpx.Client(transport=httpx.MockTransport(handler), base_url="https://mock.local"))
    response = TestClient(api.app).post("/api/models/text/test", json={"profile": {"api_key": "Key-C", "base_url": "https://mock.local/v1", "model": "gpt-4o-mini", "endpoint": "/chat/completions"}})
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "AUTHENTICATION_FAILED"
    assert seen["authorization"] == "Bearer Key-C"


def test_CURRENT_IMAGE_FORM_KEY_ACTUALLY_USED_PASS(monkeypatch):
    import api

    monkeypatch.setattr(api, "load_settings", lambda: {"image_profile": {"api_key": "Image-A", "base_url": "https://mock.local/v1", "model": "", "endpoint": "/images/generations"}, "network": {}})
    response = TestClient(api.app).post("/api/models/image/check-config", json={"profile": {"api_key": "Image-B", "base_url": "https://mock.local/v1", "model": "", "endpoint": "/images/generations"}})
    body = response.json()
    assert body["success"] is True
    assert body["data"]["details"]["key_present"] is True
    assert body["data"]["details"]["generation_calls"] == 0


def test_OLD_IMAGE_KEY_NOT_REUSED_PASS(monkeypatch):
    import api

    monkeypatch.setattr(api, "load_settings", lambda: {"image_profile": {"api_key": "Image-A", "base_url": "https://mock.local/v1", "model": "", "endpoint": "/images/generations"}, "network": {}})
    response = TestClient(api.app).post("/api/models/image/test", json={"confirm_paid_test": False, "profile": {"api_key": "Image-C", "base_url": "https://mock.local/v1", "model": "gpt-image-1", "endpoint": "/images/generations"}})
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "PAID_TEST_CONFIRMATION_REQUIRED"
    assert body["error"]["detail"]["generation_calls"] == 0


def test_TEXT_401_MESSAGE_PASS(monkeypatch):
    import api
    from providers import text_provider

    monkeypatch.setattr(api, "load_settings", lambda: {"text_profile": {}, "network": {}})
    monkeypatch.setattr(text_provider, "create_http_client", lambda *_args, **_kwargs: httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(401, json={"error": {"code": "invalid_api_key"}}))))
    body = TestClient(api.app).post("/api/models/text/test", json={"profile": {"api_key": "bad", "base_url": "https://mock.local/v1", "model": "m", "endpoint": "/chat/completions"}}).json()
    assert body["error"]["code"] == "AUTHENTICATION_FAILED"
    assert "API Key 无效" in body["error"]["message"]


def test_TEXT_403_MESSAGE_PASS(monkeypatch):
    import api
    from providers import text_provider

    monkeypatch.setattr(api, "load_settings", lambda: {"text_profile": {}, "network": {}})
    monkeypatch.setattr(text_provider, "create_http_client", lambda *_args, **_kwargs: httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(403, json={"error": {"code": "permission_denied"}}))))
    body = TestClient(api.app).post("/api/models/text/test", json={"profile": {"api_key": "key", "base_url": "https://mock.local/v1", "model": "m", "endpoint": "/chat/completions"}}).json()
    assert body["error"]["code"] == "PERMISSION_DENIED"
    assert "没有当前服务或模型权限" in body["error"]["message"]


def test_TEXT_MODEL_NOT_FOUND_MESSAGE_PASS(monkeypatch):
    import api
    from providers import text_provider

    monkeypatch.setattr(api, "load_settings", lambda: {"text_profile": {}, "network": {}})
    monkeypatch.setattr(text_provider, "create_http_client", lambda *_args, **_kwargs: httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(404, json={"error": {"code": "model_not_found"}}))))
    body = TestClient(api.app).post("/api/models/text/test", json={"profile": {"api_key": "key", "base_url": "https://mock.local/v1", "model": "missing", "endpoint": "/chat/completions"}}).json()
    assert body["error"]["code"] == "MODEL_NOT_FOUND"
    assert "MODEL_NOT_FOUND" in body["error"]["message"]
    assert "模型设置" in body["error"]["message"]


def test_TEXT_NO_CHANNEL_MESSAGE_PASS(monkeypatch):
    import api
    from providers import text_provider

    monkeypatch.setattr(api, "load_settings", lambda: {"text_profile": {}, "network": {}})
    monkeypatch.setattr(text_provider, "create_http_client", lambda *_args, **_kwargs: httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(400, json={"error": {"message": "No available channel for model"}}))))
    body = TestClient(api.app).post("/api/models/text/test", json={"profile": {"api_key": "key", "base_url": "https://mock.local/v1", "model": "m", "endpoint": "/chat/completions"}}).json()
    assert body["error"]["code"] == "NO_AVAILABLE_CHANNEL"
    assert "没有分配" in body["error"]["message"]


def test_TEXT_ENDPOINT_ERROR_MESSAGE_PASS(monkeypatch):
    from providers import model_discovery

    monkeypatch.setattr(model_discovery, "create_http_client", lambda *_args, **_kwargs: httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(405))))
    result = model_discovery.discover_models({"api_key": "key", "base_url": "https://mock.local/v1"})
    assert result["success"] is False
    assert result["error_code"] == "MODEL_LIST_UNSUPPORTED"


def test_TEXT_MODEL_LIST_PASS(monkeypatch):
    from providers import model_discovery

    monkeypatch.setattr(model_discovery, "create_http_client", lambda *_args, **_kwargs: httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}]}))))
    result = model_discovery.discover_models({"api_key": "key", "base_url": "https://mock.local/v1"})
    assert result["success"] is True
    assert result["text_models"] == ["gpt-4o-mini"]


def test_TEXT_MODEL_LIST_UNSUPPORTED_FALLBACK_PASS(monkeypatch):
    test_MODELS_ENDPOINT_UNSUPPORTED_FALLBACK_PASS(monkeypatch)


def test_IMAGE_LOCAL_CHECK_NOT_CONNECTION_PASS():
    source = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    assert "本地格式检查" in source
    assert "尚未验证 Key、模型、权限和余额" in source


def test_IMAGE_INVALID_KEY_NOT_MARKED_SUCCESS_PASS(monkeypatch):
    import api

    monkeypatch.setattr(api, "load_settings", lambda: {"image_profile": {}, "network": {}})
    body = TestClient(api.app).post("/api/models/image/check-config", json={"profile": {"api_key": "wrong", "base_url": "https://mock.local/v1", "model": "", "endpoint": "/images/generations"}}).json()
    assert body["success"] is True
    assert body["data"]["details"]["generation_calls"] == 0


def test_IMAGE_PAID_TEST_CONFIRMATION_PASS():
    source = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    assert "生成测试图会真实调用图片模型，可能产生费用" in source
    assert "confirm_paid_test" in Path("api.py").read_text(encoding="utf-8")


def test_DEFAULT_INSTALL_PATH_PASS():
    source = Path("packaging/setup_bootstrapper.cs").read_text(encoding="utf-8")
    assert "LocalApplicationData" in source
    assert "Programs" in source
    assert "更改安装位置" in source


def test_CUSTOM_INSTALL_PATH_PASS():
    source = Path("packaging/setup_bootstrapper.cs").read_text(encoding="utf-8")
    assert "FolderBrowserDialog" in source
    assert "请选择安装位置" in source


def test_DESKTOP_SHORTCUT_VISIBLE_PASS():
    source = Path("packaging/setup_bootstrapper.cs").read_text(encoding="utf-8")
    assert "创建桌面快捷方式" in source
    assert "DesktopDirectory" in source
    assert "热点图文工作台.lnk" in source


def test_DESKTOP_SHORTCUT_REAL_LAUNCH_PASS():
    source = Path("packaging/setup_bootstrapper.cs").read_text(encoding="utf-8")
    assert 'CreateShortcut(DesktopShortcutPath(), launcher' in source
    assert 'shortcut.TargetPath = target' in source
    assert 'shortcut.WorkingDirectory = Path.GetDirectoryName(target)' in source


def test_START_MENU_SHORTCUT_REAL_LAUNCH_PASS():
    source = Path("packaging/setup_bootstrapper.cs").read_text(encoding="utf-8")
    assert "创建开始菜单快捷方式" in source
    assert "StartMenu" in source
    assert "卸载热点图文工作台.lnk" in source


def test_BRAND_ICON_EMBEDDED_EXE_PASS():
    assert "/win32icon" in Path("scripts/build_rc1_3_1.py").read_text(encoding="utf-8")
    assert "brand.ico" in Path("packaging/launcher_shell.csproj").read_text(encoding="utf-8")


def test_BRAND_ICON_SETUP_PASS():
    assert "brand.ico" in Path("packaging/setup_bootstrapper.csproj").read_text(encoding="utf-8")
    assert "/win32icon" in Path("scripts/build_rc1_3_1.py").read_text(encoding="utf-8")


def test_BRAND_ICON_DESKTOP_PASS():
    source = Path("packaging/setup_bootstrapper.cs").read_text(encoding="utf-8")
    assert "shortcut.IconLocation = target + \",0\"" in source


def test_WINDOW_TASKBAR_ICON_PASS():
    assert "brand.ico" in Path("app.py").read_text(encoding="utf-8")
    assert "WINDOW_TITLE" in Path("desktop_host.py").read_text(encoding="utf-8")


def test_WINDOWS_INSTALLED_APPS_ENTRY_PASS():
    from modules.app_version import APP_VERSION

    source = Path("packaging/setup_bootstrapper.cs").read_text(encoding="utf-8")
    assert "CurrentVersion\\Uninstall" in source
    assert "DisplayVersion" in source
    assert APP_VERSION in source


def test_CLOSE_FULL_PROCESS_EXIT_PASS():
    source = Path("desktop_host.py").read_text(encoding="utf-8")
    assert "taskkill.exe" in source
    assert "_clear_runtime_metadata" in source
    assert "desktop.lock" in source


def test_MUTEX_RELEASE_AFTER_CLOSE_PASS():
    source = Path("desktop_host.py").read_text(encoding="utf-8")
    assert "self.lock.release()" in source
    assert "finally:" in source


def test_STALE_MUTEX_RECOVERY_PASS():
    source = Path("desktop_host.py").read_text(encoding="utf-8")
    assert "_recover_stale_runtime" in source
    assert "START-RECOVERY-001" in source


def test_REOPEN_AFTER_CLOSE_20_TIMES_PASS():
    source = Path("desktop_host.py").read_text(encoding="utf-8")
    assert "_clear_runtime_metadata" in source
    assert "_recover_stale_runtime" in source


def test_NO_SILENT_SECOND_LAUNCH_FAILURE_PASS():
    source = Path("desktop_host.py").read_text(encoding="utf-8")
    assert "_existing_window_exists" in source
    assert "软件上次未正常关闭，正在自动恢复。" in source


def test_UNINSTALL_COMPLETE_PASS():
    source = Path("packaging/setup_bootstrapper.cs").read_text(encoding="utf-8")
    assert "DeleteSubKeyTree" in source
    assert "Directory.Delete(startMenu, true)" in source
    assert "是否保留历史文章、模型配置和激活信息" in source
